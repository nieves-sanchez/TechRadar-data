"""
extract.py — Extracción de datos para TechRadar.

Fuentes:
  - Adzuna API: ofertas IT en 8 países EU zona Euro (paginación automática).
  - Crawling redirect_url: texto completo de la oferta para NLP.
  - Eurostat API: tasa de empleo 15-64 años por país y año.

Uso:
  Importar las funciones desde pipeline.py:
    from scripts.extract import extract_adzuna, enrich_with_full_descriptions, extract_eurostat
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

try:
    import trafilatura
    _TRAFILATURA_AVAILABLE = True
except ImportError:
    _TRAFILATURA_AVAILABLE = False

# Los módulos subordinados no configuran basicConfig — solo registran su logger.
# La configuración de handlers la gestiona el punto de entrada (pipeline.py).
logger = logging.getLogger("techradar.extract")

load_dotenv()

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")

# Países cubiertos: código ISO → endpoint Adzuna
# UK excluido: no pertenece a la UE y Eurostat no publica datos de UK post-Brexit.
# Los salarios de PL (PLN) se convierten a EUR en _parse_job_record(). El resto ya usan EUR.
ADZUNA_COUNTRIES = {
    "de": "de",
    "fr": "fr",
    "es": "es",
    "nl": "nl",
    "pl": "pl",
    "it": "it",
    "at": "at",
    "be": "be",
}

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs"
ADZUNA_RESULTS_PER_PAGE = 50
ADZUNA_CATEGORY = "it-jobs"

EUROSTAT_URL = (
    "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/lfsi_emp_a"
)
EUROSTAT_INDICATOR = "employment_rate_15_64"

# Delay entre crawling para respetar los ToS de Adzuna
CRAWL_DELAY_SECONDS = 2.0

# Delay entre páginas para mantenernos dentro del rate limit de la API
API_PAGE_DELAY_SECONDS = 0.5

REQUEST_TIMEOUT = 15

# Número de reintentos por página de la API antes de descartar la página.
# Un error HTTP transitorio no debe cortar toda la paginación del país.
API_MAX_RETRIES = 3

CRAWL_USER_AGENT = "TechRadarBot/1.0 (portfolio project; personal research)"

# Headers de navegador para el crawling de redirect_url.
# Adzuna y los portales de destino aplican bot-detection en sus páginas de
# landing (/land/ad/). Un User-Agent de bot genérico devuelve 403; headers
# de navegador completos pasan el filtro correctamente.
CRAWL_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Reintentos ante throttling (429/503) en el crawling de description_full.
# Cada reintento espera CRAWL_BACKOFF_DELAYS[intento-1] segundos antes de continuar.
# Si el tercer intento también falla por throttling, la URL se descarta y
# crawl_description devuelve throttled=True para que el circuit breaker lo contabilice.
CRAWL_MAX_RETRIES = 3
CRAWL_BACKOFF_DELAYS = [30, 120]   # segundos: 1er reintento 30s, 2do reintento 2min

# Códigos HTTP que indican throttling activo.
CRAWL_THROTTLE_STATUSES = frozenset({429, 503})

# Si este número de URLs consecutivas fallan por throttling, el crawling se aborta.
# Las ofertas pendientes quedan con description_full=None para que repair_crawl.py
# las procese más tarde, cuando la IP ya no esté bloqueada.
CRAWL_CIRCUIT_BREAKER_THRESHOLD = 10

# Tipo de cambio PLN → EUR aplicado a salarios de Polonia.
# La API de Adzuna devuelve salarios en moneda local. Polonia usa PLN (złoty),
# no EUR. Sin esta conversión los salarios polacos aparecen ~4x más altos que
# sus equivalentes en los demás países (que sí usan EUR).
# Actualizar si la paridad cambia significativamente (1 EUR ≈ 4.27 PLN).
PLN_TO_EUR: float = 0.2342

# Umbrales para la lógica de conversión PLN → EUR en ofertas polacas.
# Problema: no todos los salarios que devuelve Adzuna PL están en PLN.
# Las empresas multinacionales suelen publicar el salario directamente en EUR.
#
#   _PLN_DAILY_RATE_MAX  — si ref cae en este rango, se trata como tarifa
#                          diaria B2B en PLN (típico 200-800 PLN/día).
#                          Cubre tarifas fijas (min==max) y rangos (min!=max).
#                          Se anualiza × 220 días y luego se convierte a EUR.
#
#   _PLN_ALREADY_EUR_MAX — si ref < este umbral, se asume que el salario ya
#                          está en EUR (empresa multinacional) y NO se convierte.
#                          Rango 5.000-20.000 es ambiguo; se conserva como EUR
#                          porque 10.000 PLN anuales sería irreal para IT.
#
#   _PLN_MAX_PLAUSIBLE   — si ref > este umbral, el valor se descarta como
#                          corrupto (datos de test, error de escala, etc.).
#                          1.500.000 PLN ≈ 351.000 EUR, ya irreal para IT en EU.
_PLN_DAILY_RATE_MAX    = 5_000     # por debajo → tarifa diaria B2B en PLN
_PLN_DAILY_WORKING_DAYS = 220      # días laborables para anualizar tarifas B2B
_PLN_ALREADY_EUR_MAX   = 20_000    # por debajo → asumir EUR, no convertir
_PLN_MAX_PLAUSIBLE     = 1_500_000 # por encima → dato corrupto, nullear


# =============================================================================
# Adzuna API
# =============================================================================


def _build_adzuna_params(page: int, max_days_old: int) -> dict:
    """
    Construye los parámetros de query para una petición a la API de Adzuna.

    Args:
        page (int): Número de página (comienza en 1).
        max_days_old (int): Antigüedad máxima de las ofertas en días.

    Returns:
        dict: Parámetros listos para requests.get().
    """
    return {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": ADZUNA_RESULTS_PER_PAGE,
        "category": ADZUNA_CATEGORY,
        "max_days_old": max_days_old,
    }


def fetch_adzuna_page(
    session: requests.Session,
    country_code: str,
    page: int,
    max_days_old: int,
) -> Optional[dict]:
    """
    Descarga una página de resultados de la API de Adzuna para un país dado.

    Args:
        session (requests.Session): Sesión HTTP reutilizable.
        country_code (str): Código ISO del país (ej: 'es', 'de').
        page (int): Número de página (comienza en 1).
        max_days_old (int): Antigüedad máxima de las ofertas en días.

    Returns:
        dict | None: JSON de respuesta de la API, o None si hubo error.
    """
    endpoint = ADZUNA_COUNTRIES.get(country_code)
    if not endpoint:
        logger.error("País no soportado: %s", country_code)
        return None

    url = f"{ADZUNA_BASE_URL}/{endpoint}/search/{page}"
    params = _build_adzuna_params(page, max_days_old)

    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            if attempt == API_MAX_RETRIES:
                logger.error(
                    "Fallo definitivo en %s página %d tras %d intentos: %s",
                    country_code, page, API_MAX_RETRIES, exc,
                )
                return None
            wait = 2 ** attempt  # backoff: 2s, 4s
            logger.warning(
                "Reintento %d/%d en %s página %d (espera %ds): %s",
                attempt, API_MAX_RETRIES, country_code, page, wait, exc,
            )
            time.sleep(wait)
    return None


def _parse_job_record(raw_job: dict, country_code: str) -> dict:
    """
    Extrae y normaliza los campos relevantes de un registro raw de la API.

    Los salarios de PL (PLN) se convierten a EUR. El resto de países ya usan EUR.

    Args:
        raw_job (dict): Registro tal como llega de la API de Adzuna.
        country_code (str): Código ISO del país de la oferta.

    Returns:
        dict | None: Registro normalizado, o None si el registro no tiene ID válido.
    """
    # Validar que el registro tenga un ID — sin él no se puede hacer UPSERT
    raw_id = raw_job.get("id")
    if not raw_id:
        logger.warning("Registro sin ID descartado: title=%s", raw_job.get("title", ""))
        return None

    # El último elemento del área geográfica suele ser la ciudad más específica
    location_area = raw_job.get("location", {}).get("area", [])
    city = location_area[-1] if location_area else None

    salary_min = _safe_int(raw_job.get("salary_min"))
    salary_max = _safe_int(raw_job.get("salary_max"))

    # Polonia usa PLN, no EUR. Convertimos aquí para que todos los salarios
    # de la BD estén en la misma moneda. Ver constante PLN_TO_EUR en este módulo.
    # La lógica distingue tres casos (ver comentarios de los umbrales arriba):
    #   1. ref > _PLN_MAX_PLAUSIBLE    → dato corrupto, nullear
    #   2. ref >= _PLN_ALREADY_EUR_MAX → PLN anual plausible, convertir × PLN_TO_EUR
    #   3. ref < _PLN_DAILY_RATE_MAX   → tarifa diaria B2B (min==max o rango), anualizar y convertir
    #   4. resto (5k-20k)              → asumir EUR (multinacional), dejar
    #
    # NOTA: el Caso 3 cubre TODOS los valores con ref < 5.000, incluyendo rangos
    # con min != max (p.ej. 1.560-1.800 PLN/día).
    if country_code == "pl":
        # Usar el valor más alto como referencia para detectar PLN.
        # Con salary_min como ref, un salary_min pequeño (< 20k) hacía que
        # un salary_max enorme en PLN pasara sin convertir (bug original).
        _vals = [v for v in (salary_min, salary_max) if v is not None]
        ref = max(_vals) if _vals else None
        if ref is not None and ref > _PLN_MAX_PLAUSIBLE:
            # Caso 1: valor absurdo (error de escala, dato de test) → nullear
            salary_min = None
            salary_max = None
        elif ref is not None and ref >= _PLN_ALREADY_EUR_MAX:
            # Caso 2: PLN anual plausible (20k-1.5M PLN) → convertir a EUR
            if salary_min is not None:
                salary_min = round(salary_min * PLN_TO_EUR)
            if salary_max is not None:
                salary_max = round(salary_max * PLN_TO_EUR)
        elif ref is not None and ref < _PLN_DAILY_RATE_MAX:
            # Caso 3: tarifa diaria B2B en PLN (ref < 5.000).
            # Cubre min==max (tarifa fija) y min!=max (rango de tarifa).
            # Anualizar antes de convertir para que salary_mid sea comparable.
            if salary_min is not None:
                salary_min = round(salary_min * _PLN_DAILY_WORKING_DAYS * PLN_TO_EUR)
            if salary_max is not None:
                salary_max = round(salary_max * _PLN_DAILY_WORKING_DAYS * PLN_TO_EUR)
        # Caso 4: 5k <= ref < 20k → asumir EUR (multinacional), dejar sin convertir

    return {
        "id": int(raw_id),
        "source": "adzuna",
        "title": raw_job.get("title", "").strip(),
        "company": raw_job.get("company", {}).get("display_name", None),
        "location_display": raw_job.get("location", {}).get("display_name", None),
        "city": city,
        "country_code": country_code,
        "salary_min": salary_min,
        "salary_max": salary_max,
        # El campo llega como string "0" o "1" en la respuesta de la API
        "salary_is_predicted": str(raw_job.get("salary_is_predicted", "0")) == "1",
        "contract_type": raw_job.get("contract_type", None),
        "contract_time": raw_job.get("contract_time", None),
        # Descripción corta (500 chars truncados) y URL para crawling posterior
        "description_short": raw_job.get("description", None),
        "description_full": None,
        "url": raw_job.get("redirect_url", None),
        "posted_at": raw_job.get("created", None),
    }


def _safe_int(value) -> Optional[int]:
    """
    Convierte un valor a entero de forma segura.

    Args:
        value: Valor a convertir (puede ser float, str, None...).

    Returns:
        int | None: Entero convertido, o None si el valor es nulo o inválido.
    """
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def fetch_all_jobs_for_country(
    session: requests.Session,
    country_code: str,
    max_days_old: int,
) -> list[dict]:
    """
    Descarga todas las páginas disponibles de ofertas IT para un país.

    Para en cuanto la API devuelve una página vacía o se alcanza el límite
    práctico de resultados (Adzuna limita la paginación a 5.000 resultados
    por categoría en el plan gratuito).

    Args:
        session (requests.Session): Sesión HTTP reutilizable.
        country_code (str): Código ISO del país.
        max_days_old (int): Antigüedad máxima en días.

    Returns:
        list[dict]: Lista de registros normalizados de ese país.
    """
    all_jobs = []
    page = 1
    max_pages = 100  # 100 páginas × 50 resultados = 5.000 registros máximo

    logger.info("Extrayendo [%s] ...", country_code)

    while page <= max_pages:
        raw_response = fetch_adzuna_page(session, country_code, page, max_days_old)

        if raw_response is None:
            break

        raw_jobs = raw_response.get("results", [])

        if not raw_jobs:
            break

        for raw_job in raw_jobs:
            parsed = _parse_job_record(raw_job, country_code)
            if parsed is not None:
                all_jobs.append(parsed)

        logger.info(
            "  [%s] página %d → %d ofertas acumuladas",
            country_code, page, len(all_jobs),
        )

        page += 1
        time.sleep(API_PAGE_DELAY_SECONDS)

    logger.info("  [%s] total: %d ofertas", country_code, len(all_jobs))
    return all_jobs


def extract_adzuna(max_days_old: int = 7, countries: list[str] = None) -> pd.DataFrame:
    """
    Extrae todas las ofertas IT de Adzuna para los 8 países EU (zona Euro).

    Hace paginación automática por cada país. Todos los salarios están
    ya en EUR (moneda única de los 8 países cubiertos).

    Args:
        max_days_old (int): Antigüedad máxima de las ofertas en días.
                            Usa 7 para cargas incrementales semanales.
                            Usa 30 para la carga inicial completa.
        countries (list[str] | None): Lista de códigos de país a extraer.
                            Si es None, extrae los 8 países por defecto.
                            Ejemplo: ['pl'] para solo Polonia.

    Returns:
        pd.DataFrame: DataFrame con una fila por oferta y las columnas
                      definidas en _parse_job_record().
    """
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        raise EnvironmentError(
            "Variables de entorno ADZUNA_APP_ID y ADZUNA_APP_KEY no configuradas. "
            "Copia .env.example a .env y rellena tus credenciales."
        )

    target_countries = countries if countries else list(ADZUNA_COUNTRIES.keys())
    invalid = [c for c in target_countries if c not in ADZUNA_COUNTRIES]
    if invalid:
        raise ValueError(f"Países no soportados: {invalid}. Válidos: {list(ADZUNA_COUNTRIES.keys())}")

    all_jobs = []

    with requests.Session() as session:
        session.headers.update({"User-Agent": CRAWL_USER_AGENT})

        for country_code in target_countries:
            country_jobs = fetch_all_jobs_for_country(
                session, country_code, max_days_old
            )
            all_jobs.extend(country_jobs)

    if not all_jobs:
        logger.warning("extract_adzuna: no se obtuvieron ofertas. Verificar credenciales.")
        return pd.DataFrame()

    raw_jobs_df = pd.DataFrame(all_jobs)

    raw_jobs_df["posted_at"] = pd.to_datetime(
        raw_jobs_df["posted_at"], utc=True, errors="coerce"
    )

    logger.info(
        "extract_adzuna completado: %d ofertas totales de %d países",
        len(raw_jobs_df),
        raw_jobs_df["country_code"].nunique(),
    )

    return raw_jobs_df


# =============================================================================
# Crawling de redirect_url para description_full
# =============================================================================


def _crawl_justjoin(session: requests.Session, url: str) -> Optional[str]:
    """
    Extrae la descripción de una oferta de justjoin.it desde su HTML.

    justjoin.it usa Next.js App Router (RSC): el HTML estático no renderiza
    el cuerpo de la oferta, pero sí incluye un script inline pequeño (~3-8 KB)
    con el JSON de la oferta, que contiene el campo 'description' con el texto
    completo de la descripción en plano.

    Args:
        session: Sesión HTTP activa.
        url: URL de la oferta en justjoin.it (cualquier path: /offers/ o /job-offer/).

    Returns:
        Texto limpio de la descripción, o None si no se pudo extraer.
    """
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as exc:
        logger.debug("justjoin.it fallido para %s: %s", url, exc)
        return None

    if not resp.ok:
        logger.debug("justjoin.it HTTP %d para %s", resp.status_code, url)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # Buscar el script inline que contiene el JSON de la oferta.
    # Es el más pequeño que incluye el campo 'description' con texto largo.
    # Los scripts del framework (>50 KB) también pueden contener 'description'
    # pero son boilerplate de i18n — se descartan por tamaño.
    for tag in soup.find_all("script"):
        content = tag.string or ""
        if "description" not in content or len(content) > 50_000:
            continue

        # Intentar parsear como JSON limpio
        try:
            data = json.loads(content)
            desc = data.get("description", "")
            if isinstance(desc, str) and len(desc) > 100:
                return desc.strip()
        except (json.JSONDecodeError, AttributeError):
            pass

        # Fallback: extraer el valor del campo 'description' con regex.
        # json.loads sobre el match decodifica correctamente escapes \uXXXX y \n.
        match = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
        if match:
            try:
                desc = json.loads(f'"{match.group(1)}"')
                if len(desc) > 100:
                    return desc.strip()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

    return None


def crawl_description(
    session: requests.Session, url: str
) -> tuple[Optional[str], bool]:
    """
    Descarga la página de una oferta y extrae el texto completo de la descripción.

    Ante respuestas de throttling (CRAWL_THROTTLE_STATUSES), reintenta hasta
    CRAWL_MAX_RETRIES veces con una pausa de CRAWL_BACKOFF_DELAYS segundos entre
    intentos. Para cualquier otro error HTTP o de red no reintenta.

    El redirect_url apunta directamente al dominio de Adzuna, así que no hay
    protección anti-bot. El crawling está autorizado por los ToS de Adzuna
    para uso de investigación personal.

    Args:
        session (requests.Session): Sesión HTTP con User-Agent configurado.
        url (str): redirect_url de la oferta (campo de la API).

    Returns:
        tuple: (descripcion, throttled)
            descripcion: texto limpio de la descripción, o None si no se pudo extraer.
            throttled:   True si todos los intentos fallaron por throttling (429/503).
                         False en cualquier otro caso (éxito, 404, error de red...).
    """
    if not url:
        return None, False

    for attempt in range(1, CRAWL_MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.exceptions.RequestException as exc:
            logger.debug("Crawling fallido para %s: %s", url, exc)
            return None, False

        if response.status_code in CRAWL_THROTTLE_STATUSES:
            if attempt < CRAWL_MAX_RETRIES:
                wait = CRAWL_BACKOFF_DELAYS[attempt - 1]
                logger.warning(
                    "Throttling HTTP %d en intento %d/%d — esperando %ds: %s",
                    response.status_code, attempt, CRAWL_MAX_RETRIES, wait, url,
                )
                time.sleep(wait)
                continue
            logger.warning(
                "Throttling persistente (HTTP %d) tras %d intentos: %s",
                response.status_code, CRAWL_MAX_RETRIES, url,
            )
            return None, True

        if not response.ok:
            logger.debug("HTTP %d para %s", response.status_code, url)
            return None, False

        # Portales SPA que no renderizan contenido en el HTML estático.
        # Adzuna redirige a justjoin.it via JavaScript (no via HTTP redirect),
        # así que response.url sigue siendo adzuna.pl. La URL de destino aparece
        # en el HTML de la landing page (link rel="preconnect" o window.location).
        # También cubrimos el caso de redirect HTTP real por si cambia el comportamiento.
        jj_match = re.search(r'https://justjoin\.it/[^\s"\'<>?#]+', response.text)
        if jj_match or "justjoin.it" in response.url:
            jj_url = jj_match.group(0) if jj_match else response.url
            text = _crawl_justjoin(session, jj_url)
            return text, False

        # Opción 1: trafilatura — extrae el cuerpo principal eliminando boilerplate
        # (navegación, footer, anuncios). Mucho más robusto que selectores manuales.
        if _TRAFILATURA_AVAILABLE:
            extracted = trafilatura.extract(
                response.text,
                include_comments=False,
                include_tables=False,
                no_fallback=False,
                favor_recall=True,
            )
            if extracted and len(extracted.strip()) > 100:
                return extracted.strip(), False

        # Opción 2: selectores CSS específicos del DOM de Adzuna (fallback)
        soup = BeautifulSoup(response.text, "html.parser")
        description_text = None

        for selector in [
            "section.adp-body",
            "div.adp-body",
            "[data-automation='jobDescription']",
            "div.jobDescription",
            "div#job-description",
            "div.job-description",
            "article",
            "main",
        ]:
            element = soup.select_one(selector)
            if element:
                description_text = element.get_text(separator="\n", strip=True)
                break

        # Opción 3: body completo como último recurso
        if not description_text:
            body = soup.find("body")
            if body:
                description_text = body.get_text(separator="\n", strip=True)

        if description_text:
            lines = [line for line in description_text.splitlines() if line.strip()]
            description_text = "\n".join(lines)

        return (description_text if description_text else None), False

    return None, False


def enrich_with_full_descriptions(
    raw_jobs_df: pd.DataFrame,
    crawl_delay: float = CRAWL_DELAY_SECONDS,
    checkpoint_path: Optional[str] = None,
    checkpoint_every: int = 200,
) -> pd.DataFrame:
    """
    Enriquece el DataFrame de ofertas con el texto completo de cada oferta
    obtenido via crawling del redirect_url.

    Las ofertas donde el crawling falle mantendrán description_full = None.
    Ante throttling (429/503), crawl_description reintenta con backoff. Si
    CRAWL_CIRCUIT_BREAKER_THRESHOLD URLs consecutivas siguen fallando por
    throttling, el crawling se detiene: las ofertas restantes quedan con
    description_full=None y repair_crawl.py las procesará más tarde.

    Soporta reanudación: si checkpoint_path apunta a un fichero existente,
    carga el progreso previo y solo procesa las ofertas pendientes
    (description_full == None). El checkpoint se actualiza cada
    checkpoint_every ofertas procesadas.

    Args:
        raw_jobs_df (pd.DataFrame): DataFrame de salida de extract_adzuna().
        crawl_delay (float): Segundos de pausa entre peticiones de crawling.
                             Por defecto 2.0 para respetar el servidor.
        checkpoint_path (str | None): Ruta al fichero CSV de checkpoint.
                             Si existe, se reanuda desde él. Si no existe,
                             se crea al procesar las primeras checkpoint_every ofertas.
        checkpoint_every (int): Cada cuántas ofertas procesadas se guarda el checkpoint.

    Returns:
        pd.DataFrame: Mismo DataFrame con la columna description_full rellena
                      donde el crawling fue exitoso.
    """
    if raw_jobs_df.empty:
        return raw_jobs_df

    # ── Reanudación desde checkpoint ─────────────────────────────────────────
    checkpoint_file = Path(checkpoint_path) if checkpoint_path else None

    if checkpoint_file and checkpoint_file.exists():
        logger.info("Cargando progreso de crawling desde checkpoint: %s", checkpoint_file)
        enriched_df = pd.read_csv(checkpoint_file)
        enriched_df["posted_at"] = pd.to_datetime(
            enriched_df["posted_at"], utc=True, errors="coerce"
        )
        already_done = int(enriched_df["description_full"].notna().sum())
        logger.info("  %d ofertas ya procesadas en checkpoint.", already_done)
    else:
        enriched_df = raw_jobs_df.copy()
        already_done = 0

    # Solo procesar las que aún no tienen description_full
    pending_mask = enriched_df["url"].notna() & enriched_df["description_full"].isna()
    total = len(enriched_df)
    pending_count = int(pending_mask.sum())
    success_count = already_done
    consecutive_throttled = 0

    logger.info(
        "Iniciando crawling de %d ofertas pendientes/%d totales (delay=%.1fs)...",
        pending_count, total, crawl_delay,
    )

    with requests.Session() as session:
        session.headers.update(CRAWL_BROWSER_HEADERS)

        crawled_this_run = 0

        for idx in enriched_df[pending_mask].index:
            # Delay antes de cada petición salvo la primera
            if crawled_this_run > 0:
                time.sleep(crawl_delay)

            url = enriched_df.at[idx, "url"]
            full_text, throttled = crawl_description(session, url)

            if throttled:
                consecutive_throttled += 1
                logger.warning(
                    "Throttling confirmado para job_id=%s (%d consecutivos).",
                    enriched_df.at[idx, "id"], consecutive_throttled,
                )
                if consecutive_throttled >= CRAWL_CIRCUIT_BREAKER_THRESHOLD:
                    remaining = pending_count - crawled_this_run
                    logger.error(
                        "Circuit breaker activado tras %d throttlings consecutivos. "
                        "Crawling detenido — %d ofertas pendientes quedan con "
                        "description_full=None. Ejecutar repair_crawl.py para recuperarlas.",
                        CRAWL_CIRCUIT_BREAKER_THRESHOLD, remaining,
                    )
                    break
            else:
                consecutive_throttled = 0
                if full_text:
                    enriched_df.at[idx, "description_full"] = full_text
                    success_count += 1
                else:
                    logger.debug(
                        "Sin descripción completa para job_id=%s", enriched_df.at[idx, "id"]
                    )

            crawled_this_run += 1
            current_pos = already_done + crawled_this_run

            if current_pos % 50 == 0:
                logger.info(
                    "  Crawling: %d/%d procesadas (%d con éxito)",
                    current_pos, total, success_count,
                )

            # Guardar checkpoint periódico
            if checkpoint_file and crawled_this_run % checkpoint_every == 0:
                checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
                enriched_df.to_csv(checkpoint_file, index=False)
                logger.info(
                    "  Checkpoint guardado (%d/%d procesadas).", current_pos, total
                )

    # Checkpoint final con el estado completo (incluye el estado al parar por circuit breaker)
    if checkpoint_file:
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        enriched_df.to_csv(checkpoint_file, index=False)
        logger.info("  Checkpoint final guardado: %s", checkpoint_file)

    success_rate = (success_count / total * 100) if total > 0 else 0
    logger.info(
        "Crawling completado: %d/%d exitosas (%.1f%%)",
        success_count, total, success_rate,
    )

    return enriched_df


# =============================================================================
# Eurostat API
# =============================================================================


def _parse_eurostat_response(response_json: dict) -> list[dict]:
    """
    Parsea la respuesta SDMX-JSON de Eurostat y la convierte en una lista
    de registros planos {country_code, year, indicator, value}.

    La API de Eurostat devuelve un array multidimensional comprimido donde
    los valores se indexan por su posición en el espacio dimensional.

    Args:
        response_json (dict): JSON tal como devuelve la API de Eurostat.

    Returns:
        list[dict]: Lista de registros con country_code, year, indicator, value.
    """
    dimension_order = response_json.get("id", [])
    dimensions = response_json.get("dimension", {})
    values = response_json.get("value", {})
    sizes = response_json.get("size", [])

    if not dimension_order or not values:
        logger.warning("Respuesta de Eurostat vacía o malformada")
        return []

    # Construir diccionarios posición → etiqueta para cada dimensión
    # La API devuelve {"AT": 0, "BE": 1, ...} y necesitamos el inverso
    dim_labels = {}
    for dim_name in dimension_order:
        dim_info = dimensions.get(dim_name, {})
        category = dim_info.get("category", {})
        dim_labels[dim_name] = {
            pos: label
            for label, pos in category.get("index", {}).items()
        }

    # Calcular multiplicadores para reconstruir índices dimensionales desde el índice plano
    # Ej: sizes = [1, 1, 1, 1, 1, 9, 5] → multiplicador de la última dimensión es 1
    multipliers = []
    for i in range(len(sizes)):
        mult = 1
        for j in range(i + 1, len(sizes)):
            mult *= sizes[j]
        multipliers.append(mult)

    records = []

    for flat_idx_str, value in values.items():
        if value is None:
            continue

        flat_idx = int(flat_idx_str)

        remaining = flat_idx
        dim_indices = {}
        for i, dim_name in enumerate(dimension_order):
            dim_idx = remaining // multipliers[i]
            remaining = remaining % multipliers[i]
            dim_indices[dim_name] = dim_idx

        geo_code = dim_labels.get("geo", {}).get(dim_indices.get("geo"), None)
        year_str = dim_labels.get("time", {}).get(dim_indices.get("time"), None)

        if not geo_code or not year_str:
            continue

        records.append({
            "country_code": geo_code.lower(),
            "year": int(year_str),
            "indicator": EUROSTAT_INDICATOR,
            "value": float(value),
        })

    return records


def extract_eurostat(since_year: int = 2019) -> pd.DataFrame:
    """
    Descarga la tasa de empleo 15-64 años por país desde la API de Eurostat.

    Cubre los 8 países del proyecto (DE, FR, ES, NL, PL, IT, AT, BE)
    desde el año indicado hasta el más reciente disponible.

    Dataset: lfsi_emp_a — licencia CC BY 4.0. No requiere autenticación.

    Args:
        since_year (int): Año mínimo a incluir. Por defecto 2019.

    Returns:
        pd.DataFrame: DataFrame con columnas country_code, year, indicator, value.
                      Listo para cargarse en la tabla labor_market_context.
    """
    # UK no forma parte del proyecto (ni de la UE ni de Eurostat post-Brexit)
    eurostat_geo_codes = ["DE", "FR", "ES", "NL", "PL", "IT", "AT", "BE"]

    params = {
        "geo": eurostat_geo_codes,
        "sex": "T",            # Total (hombres + mujeres)
        "age": "Y15-64",       # Franja de edad laboral estándar
        "unit": "PC_POP",      # Porcentaje de población
        "indic_em": "EMP_LFS", # Employment Labour Force Survey
        "sinceTimePeriod": str(since_year),
    }

    logger.info("Descargando datos de Eurostat (desde %d)...", since_year)

    try:
        response = requests.get(
            EUROSTAT_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        response_json = response.json()
    except requests.exceptions.RequestException as exc:
        logger.error("Error descargando Eurostat: %s", exc)
        return pd.DataFrame()

    records = _parse_eurostat_response(response_json)

    if not records:
        logger.warning("extract_eurostat: no se obtuvieron datos.")
        return pd.DataFrame()

    eurostat_df = pd.DataFrame(records)

    # Filtrar solo los países que cubre el proyecto
    valid_countries = set(ADZUNA_COUNTRIES.keys())
    eurostat_df = eurostat_df[eurostat_df["country_code"].isin(valid_countries)]

    eurostat_df["year"] = eurostat_df["year"].astype(int)
    eurostat_df["value"] = eurostat_df["value"].round(2)

    logger.info(
        "extract_eurostat completado: %d registros (%d países, años %d–%d)",
        len(eurostat_df),
        eurostat_df["country_code"].nunique(),
        eurostat_df["year"].min(),
        eurostat_df["year"].max(),
    )

    return eurostat_df
