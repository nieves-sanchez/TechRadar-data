"""
retro_classify.py — Pipeline C local: clasificación retroactiva con Ollama.

Procesa ofertas de Supabase en tandas, enriqueciendo role_category y skills
mediante Ollama corriendo localmente. Tracking de progreso en SQLite local
para permitir tandas parciales y reanudar sin reenviar lo ya procesado.

MODOS (mutuamente excluyentes):
  (sin flags)   Activas con role_category = NULL o 'other'
  --days N      Activas ingestadas en los últimos N días
  --all         Todas las activas (histórico completo)

USO TÍPICO:
  # Diario tras Pipeline A+B (40-45 min, ofertas recientes):
  py -3.12 -m scripts.retro_classify --days 2 --max-minutes 45 --yes

  # Histórico por tandas de 1 hora (reanudar al día siguiente):
  py -3.12 -m scripts.retro_classify --all --limit 300 --max-minutes 60 --yes

  # Ver qué desactivaría la limpieza sin ejecutarla:
  py -3.12 -m scripts.retro_classify --cleanup-non-it

  # Ejecutar limpieza y luego clasificar ofertas recientes:
  py -3.12 -m scripts.retro_classify --cleanup-non-it --confirm-cleanup --days 2 --yes

  # Reprocesar aunque el hash no haya cambiado (fuerza):
  py -3.12 -m scripts.retro_classify --days 2 --reprocess --yes

SEGURIDAD:
  - Ollama NUNCA borra ni desactiva ofertas (solo actualiza role_category y skills).
  - is_tech=False → role_category=NULL para revisión manual; is_active no se toca.
  - deactivate_non_it_by_patterns solo se ejecuta con --cleanup-non-it.
  - Con --cleanup-non-it sin --confirm-cleanup: solo muestra recuento (dry run).
"""

import argparse
import logging
import os
import re
import sys
import time

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from scripts.ai_classifier import (
    DESCRIPTION_LIMIT,
    OLLAMA_MODEL,
    VALID_CATEGORIES,
    _is_ollama_available,
    classify_batch,
)
from scripts.ollama_state import (
    DEFAULT_STATE_PATH,
    compute_input_hash,
    is_already_processed,
    open_state_db,
    record_result,
)
from scripts.skills_catalog import NON_IT_PATTERNS, SKILLS

# =============================================================================
# Configuración
# =============================================================================

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("techradar.retro_classify")

_DEFAULT_BATCH_SIZE = 10   # ofertas por llamada a Ollama
_FETCH_MULTIPLIER   = 5    # leer N×batch_size candidatos para filtrar localmente


# =============================================================================
# Lookup de normalización de skills (construido una vez al importar el módulo)
# =============================================================================

_CATALOG_LOWER: dict[str, tuple[str, str]] = {}
_CATALOG_PATTERNS: list[tuple[re.Pattern, str, str]] = []


def _build_catalog_lookup() -> None:
    """Construye índices de normalización de skills a partir de SKILLS."""
    for canonical, category, patterns in SKILLS:
        _CATALOG_LOWER[canonical.lower()] = (canonical, category)
        for p in patterns:
            _CATALOG_PATTERNS.append(
                (re.compile(p, re.IGNORECASE), canonical, category)
            )


_build_catalog_lookup()


def _normalize_skill(raw_name: str) -> tuple[str, str] | None:
    """
    Normaliza el nombre de una skill devuelta por Ollama.

    Prioridad:
    1. Coincidencia exacta case-insensitive con nombre canónico del catálogo.
    2. Coincidencia con patrón regex del catálogo → devuelve nombre canónico.
    3. Skill técnica desconocida con longitud razonable → categoría 'tool'.
    4. None → descartar (demasiado corto, largo, frase, sin letras, etc.).

    Args:
        raw_name: Nombre de skill devuelto por Ollama.

    Returns:
        (nombre_canónico, categoría) o None si debe descartarse.
    """
    raw = raw_name.strip()
    if not raw or len(raw) < 2 or len(raw) > 80:
        return None

    words = raw.split()
    if len(words) > 5:
        return None

    lower = raw.lower()

    # 1. Coincidencia exacta case-insensitive contra catálogo
    if lower in _CATALOG_LOWER:
        return _CATALOG_LOWER[lower]

    # 2. Coincidencia con patrón regex del catálogo
    for pattern, canonical, category in _CATALOG_PATTERNS:
        if pattern.search(raw):
            return (canonical, category)

    # 3. Skill desconocida: al menos una letra y <= 4 palabras
    if re.search(r"[a-zA-Z]", raw) and len(words) <= 4:
        return (raw, "tool")

    return None


# =============================================================================
# Selección de descripción
# =============================================================================

def _select_description(job: dict) -> tuple[str, str]:
    """
    Selecciona la descripción más completa disponible para enviar a Ollama.

    Prefiere description_full (crawling) sobre description_short (API truncada).
    Trunca al límite definido en ai_classifier.DESCRIPTION_LIMIT.

    Args:
        job: Dict con claves 'description_full' y 'description_short'.

    Returns:
        (texto, text_source) donde text_source es 'description_full',
        'description_short' o 'empty'.
    """
    if job.get("description_full"):
        return job["description_full"][:DESCRIPTION_LIMIT], "description_full"
    if job.get("description_short"):
        return job["description_short"][:DESCRIPTION_LIMIT], "description_short"
    return "", "empty"


# =============================================================================
# Lógica pura de resultado (sin acceso a BD — testable)
# =============================================================================

def _build_role_updates_from_result(
    job: dict,
    result: dict,
    reclassify_all: bool,
    existing_skills: set[str],
    update_existing_roles: bool = False,
) -> tuple[list, list]:
    """
    Extrae role_updates y skill_records de un resultado de Ollama.

    No toca la BD. Separado para ser testeado sin conexión a Supabase.

    Modo conservador por defecto (update_existing_roles=False):
      - is_tech=False + role_before valido    -> conserva categoria, no añade skills.
      - is_tech=False + role_before NULL/other -> pone NULL para revision manual.
      - is_tech=True  + role_before valido    -> NO cambia role_category, si añade skills.
      - is_tech=True  + role_before NULL/other -> actualiza role_category si es canonica.

    Con update_existing_roles=True:
      - is_tech=True permite sobrescribir una categoria valida existente.

    En todos los casos, role_category propuesta se valida contra VALID_CATEGORIES
    antes de escribir (defensa en profundidad sobre la validacion de ai_classifier).

    Args:
        job: Dict con claves 'id' y 'role_category'.
        result: Dict con 'role_category', 'skills' e 'is_tech'.
        reclassify_all: Si True, permite escribir 'other' como resultado.
        existing_skills: Set de nombres de skills ya vinculadas (lowercase).
        update_existing_roles: Si True, permite cambiar una categoria valida existente.

    Returns:
        (role_updates, skill_records) listos para pasar a update_role_categories
        y upsert_skills_and_links.
    """
    role_updates: list[tuple] = []
    skill_records: list[dict] = []
    job_id = job["id"]
    role_before = job.get("role_category")

    # is_tech=False — comportamiento conservador para evitar falsos negativos de Ollama:
    # - Sin categoria fiable (NULL o 'other') -> marcar NULL para revision manual.
    # - Con categoria tecnica valida -> conservarla; no anadir skills.
    if not result.get("is_tech", True):
        if role_before is None or role_before == "other":
            role_updates.append((None, job_id))
        return role_updates, skill_records

    # is_tech=True — validar canonicalidad de la categoria propuesta
    cat = result.get("role_category")
    # Defensa en profundidad: ai_classifier._clean_result ya valida, pero lo
    # reforzamos aqui para garantizar que nunca se escriba una categoria invalida
    if cat not in VALID_CATEGORIES:
        cat = None

    # Modo conservador: no sobreescribir categoria tecnica valida sin flag explicito
    has_valid_existing = role_before is not None and role_before != "other"

    if cat and (reclassify_all or cat != "other"):
        if not has_valid_existing or update_existing_roles:
            role_updates.append((cat, job_id))

    # Anadir skills canonicalizadas independientemente de si se actualizo el rol
    seen = set(existing_skills)
    for raw_skill in result.get("skills", []):
        normalized = _normalize_skill(raw_skill)
        if normalized is None:
            continue
        canonical, category = normalized
        if canonical.lower() not in seen:
            skill_records.append({
                "job_id": job_id,
                "skill_name": canonical,
                "skill_category": category,
            })
            seen.add(canonical.lower())

    return role_updates, skill_records


# =============================================================================
# Conexión
# =============================================================================

def _get_connection():
    if not DATABASE_URL:
        logger.error("DATABASE_URL no configurado en .env")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL)


# =============================================================================
# Limpieza por patrones (solo con --cleanup-non-it)
# =============================================================================

def deactivate_non_it_by_patterns(conn) -> int:
    """
    Desactiva (is_active=FALSE) las ofertas cuyo título coincide con NON_IT_PATTERNS.

    Soft-delete: los registros se conservan en BD para análisis histórico y
    no se reactivan porque el UPSERT del Pipeline A solo reactiva las que
    siguen apareciendo en la API de Adzuna.

    Nota: nunca hace DELETE; preserva la integridad referencial (job_skills).

    Args:
        conn: Conexión psycopg2 activa.

    Returns:
        Número de filas desactivadas.
    """
    total = 0
    chunk_size = 100

    with conn.cursor() as cur:
        for i in range(0, len(NON_IT_PATTERNS), chunk_size):
            chunk = NON_IT_PATTERNS[i: i + chunk_size]
            conditions = " OR ".join(["title ILIKE %s"] * len(chunk))
            params = [f"%{p}%" for p in chunk]

            cur.execute(
                f"SELECT id, title FROM jobs WHERE is_active = TRUE AND ({conditions}) LIMIT 5",
                params,
            )
            sample = cur.fetchall()
            if sample:
                logger.debug(
                    "  Muestra no-IT (chunk %d): %s",
                    i // chunk_size + 1,
                    [(row[0], row[1][:60]) for row in sample],
                )

            cur.execute(
                f"UPDATE jobs SET is_active = FALSE WHERE is_active = TRUE AND ({conditions})",
                params,
            )
            total += cur.rowcount

    conn.commit()
    return total


def count_non_it_by_patterns(conn) -> int:
    """
    Cuenta las ofertas activas que coincidirían con NON_IT_PATTERNS (dry run).

    Args:
        conn: Conexión psycopg2 activa.

    Returns:
        Número de ofertas que se desactivarían.
    """
    total = 0
    chunk_size = 100

    with conn.cursor() as cur:
        for i in range(0, len(NON_IT_PATTERNS), chunk_size):
            chunk = NON_IT_PATTERNS[i: i + chunk_size]
            conditions = " OR ".join(["title ILIKE %s"] * len(chunk))
            params = [f"%{p}%" for p in chunk]
            cur.execute(
                f"SELECT COUNT(*) FROM jobs WHERE is_active = TRUE AND ({conditions})",
                params,
            )
            total += cur.fetchone()[0]

    return total


# =============================================================================
# Consultas a la BD
# =============================================================================

def _build_where(
    reclassify_all: bool,
    days: int,
    include_inactive: bool,
) -> str:
    """
    Construye la cláusula WHERE según el modo activo.

    Por defecto incluye is_active=TRUE. Con include_inactive se procesa todo.
    """
    active_filter = "" if include_inactive else "is_active = TRUE"

    if reclassify_all:
        return f"WHERE {active_filter}" if active_filter else ""

    if days > 0:
        date_cond = f"ingested_at >= NOW() - INTERVAL '{days} days'"
        if active_filter:
            return f"WHERE {active_filter} AND {date_cond}"
        return f"WHERE {date_cond}"

    # Modo default: role_category NULL o 'other'
    cat_cond = "(role_category IS NULL OR role_category = 'other')"
    if active_filter:
        return f"WHERE {active_filter} AND {cat_cond}"
    return f"WHERE {cat_cond}"


def count_candidates(
    conn,
    reclassify_all: bool,
    days: int,
    include_inactive: bool,
) -> int:
    """Cuenta candidatos según el modo activo (sin filtrado SQLite)."""
    where = _build_where(reclassify_all, days, include_inactive)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM jobs {where}")
        return cur.fetchone()[0]


def fetch_jobs_page(
    conn,
    last_id: int,
    page_size: int,
    reclassify_all: bool,
    days: int,
    include_inactive: bool,
) -> list[dict]:
    """
    Carga una página de candidatos desde Supabase.

    Paginación por ID (cursor) para evitar OFFSET y ser robusto cuando el WHERE
    cambia durante el procesamiento. Devuelve description_full y description_short
    por separado para que retro_classify pueda calcular el hash de input correctamente.

    Args:
        conn: Conexión psycopg2 activa.
        last_id: Último ID procesado (empieza en 0).
        page_size: Número máximo de filas a devolver.
        reclassify_all: Modo --all.
        days: Modo --days N (0 = sin filtro por fecha).
        include_inactive: Si True, incluye is_active=FALSE.

    Returns:
        Lista de dicts con id, title, description_full, description_short, role_category.
    """
    where = _build_where(reclassify_all, days, include_inactive)
    id_clause = "AND id > %s" if where else "WHERE id > %s"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, title, description_full, description_short, role_category
            FROM   jobs
            {where}
            {id_clause}
            ORDER BY id
            LIMIT  %s
            """,
            (last_id, page_size),
        )
        return [dict(row) for row in cur.fetchall()]


def get_existing_skills_for_jobs(cur, job_ids: list[int]) -> dict[int, set[str]]:
    """Devuelve {job_id: set(skill_names_lower)} para un conjunto de ofertas."""
    if not job_ids:
        return {}
    cur.execute(
        """
        SELECT js.job_id, s.name
        FROM   job_skills js
        JOIN   skills s ON s.id = js.skill_id
        WHERE  js.job_id = ANY(%s)
        """,
        (job_ids,),
    )
    result: dict[int, set[str]] = {jid: set() for jid in job_ids}
    for job_id, name in cur.fetchall():
        result[job_id].add(name.lower())
    return result


# =============================================================================
# Escritura en BD
# =============================================================================

def update_role_categories(cur, updates: list[tuple]) -> None:
    """Actualiza role_category para varios jobs de golpe."""
    if updates:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE jobs SET role_category = %s WHERE id = %s",
            updates,
        )


def upsert_skills_and_links(cur, skill_records: list[dict]) -> int:
    """
    Inserta skills nuevas (con lookup case-insensitive) y crea vínculos job_skills.

    Evita duplicados tipo 'React'/'react'/'React.js': tras normalización, todas
    llegan aquí como 'React'. El SELECT LOWER(name) cubre el caso residual de
    skills no-catálogo con variantes de capitalización ya en BD.

    Args:
        skill_records: lista de {job_id, skill_name, skill_category}.

    Returns:
        Número de vínculos job_skills insertados.
    """
    if not skill_records:
        return 0

    # Dedup por nombre normalizado
    unique_skills: dict[str, str] = {}  # lower_name → (canonical_name, category)
    for r in skill_records:
        nm = r["skill_name"][:80].strip()
        if nm:
            unique_skills[nm.lower()] = (nm, r.get("skill_category", "tool"))

    if not unique_skills:
        return 0

    # Insertar skills nuevas (el UNIQUE constraint en name es case-sensitive en PG)
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO skills (name, category) VALUES %s ON CONFLICT (name) DO NOTHING",
        list(unique_skills.values()),
    )

    # Recuperar IDs via lookup case-insensitive — cubre variantes no normalizadas
    lower_list = list(unique_skills.keys())
    cur.execute(
        "SELECT id, LOWER(name) AS lname FROM skills WHERE LOWER(name) = ANY(%s)",
        (lower_list,),
    )
    skill_id_map: dict[str, int] = {row[1]: row[0] for row in cur.fetchall()}

    # Construir vínculos
    links = []
    for r in skill_records:
        nm = r["skill_name"][:80].strip()
        if not nm:
            continue
        sid = skill_id_map.get(nm.lower())
        if sid:
            links.append((r["job_id"], sid))

    if links:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO job_skills (job_id, skill_id) VALUES %s ON CONFLICT DO NOTHING",
            links,
        )
    return len(links)


# =============================================================================
# Procesamiento de un batch con Ollama y tracking SQLite
# =============================================================================

def _process_batch_with_tracking(
    conn,
    state_conn,
    batch: list[dict],
    reclassify_all: bool,
    model: str,
    update_existing_roles: bool = False,
) -> dict:
    """
    Envía un lote de ofertas a Ollama, escribe resultados en BD y registra en SQLite.

    Cada oferta en batch debe tener las claves extra '_hash', '_text', '_text_source'
    añadidas en el bucle principal.

    Args:
        conn: Conexión psycopg2 a Supabase.
        state_conn: Conexión SQLite de tracking.
        batch: Lista de jobs enriquecidos con _hash, _text, _text_source.
        reclassify_all: Si True, permite escribir 'other' como resultado.
        model: Nombre del modelo Ollama.
        update_existing_roles: Si True, permite cambiar una categoria valida existente.

    Returns:
        Dict con claves 'roles_changed', 'skills_added', 'errors'.
    """
    stats = {"roles_changed": 0, "skills_added": 0, "errors": 0}

    ollama_jobs = [{"title": j["title"], "description": j["_text"]} for j in batch]

    try:
        results = classify_batch(ollama_jobs)
    except Exception as exc:
        logger.warning("Error en classify_batch: %s", exc)
        for job in batch:
            record_result(
                state_conn,
                job_id=job["id"],
                input_hash=job["_hash"],
                model=model,
                text_source=job["_text_source"],
                status="failed",
                role_category_before=job.get("role_category"),
                error=str(exc)[:500],
            )
        stats["errors"] = len(batch)
        return stats

    job_ids = [j["id"] for j in batch]

    with conn.cursor() as cur:
        existing_skills_map = get_existing_skills_for_jobs(cur, job_ids)
        all_role_updates: list[tuple] = []
        all_skill_records: list[dict] = []

        for job, result in zip(batch, results):
            job_id = job["id"]
            role_before = job.get("role_category")
            skills_before_count = len(existing_skills_map.get(job_id, set()))

            role_updates, skill_records = _build_role_updates_from_result(
                job=job,
                result=result,
                reclassify_all=reclassify_all,
                existing_skills=existing_skills_map.get(job_id, set()),
                update_existing_roles=update_existing_roles,
            )

            all_role_updates.extend(role_updates)
            all_skill_records.extend(skill_records)

            role_after = role_updates[0][0] if role_updates else role_before
            skills_n = len(skill_records)

            if role_updates:
                stats["roles_changed"] += 1

            record_result(
                state_conn,
                job_id=job_id,
                input_hash=job["_hash"],
                model=model,
                text_source=job["_text_source"],
                status="processed",
                role_category_before=role_before,
                role_category_after=role_after,
                skills_before_count=skills_before_count,
                skills_added=skills_n,
            )

        update_role_categories(cur, all_role_updates)
        stats["skills_added"] = upsert_skills_and_links(cur, all_skill_records)

    conn.commit()
    return stats


# =============================================================================
# Función principal
# =============================================================================

def run(
    reclassify_all: bool = False,
    days: int = 0,
    limit: int = 0,
    max_minutes: int = 0,
    yes: bool = False,
    state_path: str = DEFAULT_STATE_PATH,
    reprocess: bool = False,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    include_inactive: bool = False,
    cleanup_non_it: bool = False,
    confirm_cleanup: bool = False,
    update_existing_roles: bool = False,
) -> None:
    """
    Función principal del Pipeline C.

    Conecta a Supabase y a Ollama, procesa ofertas en tandas con tracking
    local SQLite para idempotencia. No toca el esquema de Supabase.

    Args:
        reclassify_all: Procesar toda la BD (modo --all).
        days: Procesar ofertas ingestadas en los últimos N días (0 = sin filtro).
        limit: Máximo de ofertas enviadas a Ollama (0 = sin límite).
        max_minutes: Parar tras N minutos transcurridos (0 = sin límite).
        yes: Omitir confirmación interactiva.
        state_path: Ruta al SQLite de tracking.
        reprocess: Ignorar tracking e volver a enviar aunque el hash coincida.
        batch_size: Ofertas por llamada a Ollama (por defecto 10).
        include_inactive: Si True, incluye is_active=FALSE.
        cleanup_non_it: Ejecutar paso de limpieza por patrones antes de Ollama.
        confirm_cleanup: Aplicar cambios de limpieza (si False, solo dry run).
        update_existing_roles: Si True, permite cambiar role_category aunque ya
            tenga una categoria tecnica valida (modo no conservador).
    """
    model = OLLAMA_MODEL

    # ─── PASO 0: Limpieza por patrones (solo si --cleanup-non-it) ────────────
    if cleanup_non_it:
        conn_cleanup = _get_connection()
        try:
            if confirm_cleanup:
                logger.info("LIMPIEZA — desactivando ofertas no-IT por patrones...")
                n = deactivate_non_it_by_patterns(conn_cleanup)
                logger.info("  → %d ofertas desactivadas (is_active=FALSE).", n)
            else:
                logger.info("LIMPIEZA (DRY RUN) — contando ofertas no-IT por patrones...")
                n = count_non_it_by_patterns(conn_cleanup)
                logger.info(
                    "  → %d ofertas se desactivarían. "
                    "Pase --confirm-cleanup para ejecutar.",
                    n,
                )
        finally:
            conn_cleanup.close()
        print()

    # Si no se pidió modo de clasificación explícito, el cleanup fue lo único pedido
    if cleanup_non_it and not reclassify_all and days == 0:
        return

    # ─── PASO 1: Verificar Ollama ─────────────────────────────────────────────
    if not _is_ollama_available():
        logger.error("Ollama no está arrancado. Abre la aplicación Ollama.")
        sys.exit(1)
    logger.info("Ollama disponible · modelo: %s", model)
    logger.info("State path: %s", state_path)

    # Warmup: la primera llamada puede tardar 30-60s mientras el modelo carga
    logger.info("Calentando modelo Ollama (puede tardar hasta 60s la primera vez)...")
    classify_batch([{"title": "warmup", "description": ""}])
    logger.info("Modelo listo.")

    # ─── PASO 2: Mostrar parámetros y pedir confirmación ─────────────────────
    conn = _get_connection()
    state_conn = open_state_db(state_path)

    try:
        n_candidates = count_candidates(conn, reclassify_all, days, include_inactive)

        if reclassify_all:
            mode_label = "TODAS las activas" if not include_inactive else "TODAS (incluye inactivas)"
        elif days > 0:
            mode_label = f"activas ingestadas en los últimos {days} días"
        else:
            mode_label = "activas con role_category=other o NULL"

        print()
        logger.info("─" * 60)
        logger.info("Pipeline C — Ollama local")
        logger.info("  Modo:             %s", mode_label)
        logger.info("  Candidatos BD:    %d (sin filtrar por SQLite)", n_candidates)
        logger.info("  Limit (Ollama):   %s", limit if limit > 0 else "sin límite")
        logger.info("  Max minutos:      %s", max_minutes if max_minutes > 0 else "sin límite")
        logger.info("  Batch size:       %d", batch_size)
        logger.info("  Reprocess:        %s", "si" if reprocess else "no")
        logger.info(
            "  Roles conservador:%s",
            "no (--update-existing-roles activo)" if update_existing_roles else "si (default)",
        )
        logger.info("─" * 60)
        print()

        if n_candidates == 0:
            logger.info("No hay candidatos que clasificar.")
            return

        if not yes:
            secs_est = n_candidates * 4
            h, m = secs_est // 3600, (secs_est % 3600) // 60
            logger.info(
                "Tiempo estimado (sin saltadas): ~%dh %dm", h, m
            )
            confirm = input("¿Proceder? [s/N]: ").strip().lower()
            if confirm != "s":
                logger.info("Cancelado.")
                return

        # ─── PASO 3: Bucle principal ──────────────────────────────────────────
        t_start      = time.time()
        last_id      = 0
        processed    = 0   # enviadas realmente a Ollama
        skipped      = 0   # ya en SQLite con mismo hash → saltadas
        total_roles  = 0
        total_skills = 0
        total_errors = 0
        stop_reason  = "sin más candidatos"

        fetch_page = batch_size * _FETCH_MULTIPLIER

        while True:
            # Comprobaciones de parada (antes de leer otra página)
            if limit > 0 and processed >= limit:
                stop_reason = f"limit ({limit})"
                break
            elapsed_min = (time.time() - t_start) / 60
            if max_minutes > 0 and elapsed_min >= max_minutes:
                stop_reason = f"max-minutes ({max_minutes:.0f}min)"
                break

            # Leer página de candidatos (más grande que batch_size para filtrar localmente)
            candidates = fetch_jobs_page(
                conn, last_id, fetch_page, reclassify_all, days, include_inactive
            )
            if not candidates:
                stop_reason = "sin más candidatos"
                break

            last_id = candidates[-1]["id"]

            # Filtrar por SQLite: calcular hash y saltar las ya procesadas
            to_process: list[dict] = []
            for job in candidates:
                text, text_source = _select_description(job)
                h = compute_input_hash(
                    job["title"] or "", text, text_source, model
                )

                if not reprocess and is_already_processed(state_conn, job["id"], h, model):
                    skipped += 1
                    continue

                # Respetar limit: no añadir más de lo que podemos procesar
                if limit > 0 and processed + len(to_process) >= limit:
                    break

                job["_hash"]        = h
                job["_text"]        = text
                job["_text_source"] = text_source
                to_process.append(job)

            # Procesar en sub-lotes de batch_size
            for i in range(0, len(to_process), batch_size):
                sub_batch = to_process[i: i + batch_size]
                stats = _process_batch_with_tracking(
                    conn, state_conn, sub_batch, reclassify_all, model,
                    update_existing_roles=update_existing_roles,
                )
                processed    += len(sub_batch)
                total_roles  += stats["roles_changed"]
                total_skills += stats["skills_added"]
                total_errors += stats["errors"]

                elapsed_s = time.time() - t_start
                logger.info(
                    "  enviadas:%d | saltadas:%d | roles:%d | skills:+%d | err:%d | %.0fs",
                    processed, skipped, total_roles, total_skills, total_errors, elapsed_s,
                )

                # Comprobar parada tras cada sub-batch
                if limit > 0 and processed >= limit:
                    stop_reason = f"limit ({limit})"
                    break
                elapsed_min = (time.time() - t_start) / 60
                if max_minutes > 0 and elapsed_min >= max_minutes:
                    stop_reason = f"max-minutes ({max_minutes:.0f}min)"
                    break

            if stop_reason.startswith("limit") or stop_reason.startswith("max-minutes"):
                break

        # ─── Resumen ─────────────────────────────────────────────────────────
        elapsed_total = time.time() - t_start
        print()
        logger.info("=" * 60)
        logger.info("PIPELINE C — COMPLETADO")
        logger.info("  Modo:             %s", mode_label)
        logger.info("  State SQLite:     %s", state_path)
        logger.info("  Modelo:           %s", model)
        logger.info("  Candidatos BD:    %d", n_candidates)
        logger.info("  Saltadas (SQLite):%d", skipped)
        logger.info("  Enviadas Ollama:  %d", processed)
        logger.info("  Roles actualizados: %d", total_roles)
        logger.info("  Skills añadidas:  %d", total_skills)
        logger.info("  Errores:          %d", total_errors)
        logger.info("  Tiempo total:     %.0f segundos (%.1f min)", elapsed_total, elapsed_total / 60)
        logger.info("  Motivo parada:    %s", stop_reason)
        logger.info("=" * 60)

    finally:
        conn.close()
        state_conn.close()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pipeline C — clasificación local con Ollama + tracking SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modos (mutuamente excluyentes):
  (sin flags)   Activas con role_category = NULL o 'other'
  --days N      Activas ingestadas en los últimos N días
  --all         Todas las activas (histórico completo)

Ejemplos:
  # Diario tras Pipeline A+B:
  py -3.12 -m scripts.retro_classify --days 2 --max-minutes 45 --yes

  # Histórico por tandas:
  py -3.12 -m scripts.retro_classify --all --limit 300 --max-minutes 60 --yes

  # Ver qué desactivaría la limpieza (dry run):
  py -3.12 -m scripts.retro_classify --cleanup-non-it

  # Ejecutar limpieza + clasificar recientes:
  py -3.12 -m scripts.retro_classify --cleanup-non-it --confirm-cleanup --days 2 --yes
        """,
    )

    # Modos
    parser.add_argument(
        "--all", action="store_true", dest="reclassify_all",
        help="Reclasificar TODAS las ofertas activas.",
    )
    parser.add_argument(
        "--days", type=int, default=0, metavar="N",
        help="Procesar activas ingestadas en los últimos N días.",
    )

    # Límites de ejecución
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Máximo de ofertas reales enviadas a Ollama (0 = sin límite).",
    )
    parser.add_argument(
        "--max-minutes", type=int, default=0, metavar="N",
        help="Parar tras N minutos (al final del batch en curso).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=_DEFAULT_BATCH_SIZE, metavar="N",
        help=f"Ofertas por llamada a Ollama (por defecto {_DEFAULT_BATCH_SIZE}).",
    )

    # Control de ejecución
    parser.add_argument(
        "--yes", action="store_true",
        help="Omitir confirmación interactiva.",
    )
    parser.add_argument(
        "--state-path", default=DEFAULT_STATE_PATH, metavar="PATH",
        help="Ruta al SQLite de tracking (por defecto: data/ollama_state/).",
    )
    parser.add_argument(
        "--reprocess", action="store_true",
        help="Ignorar tracking y reprocesar aunque el hash coincida.",
    )
    parser.add_argument(
        "--include-inactive", action="store_true",
        help="Incluir también ofertas con is_active=FALSE.",
    )

    # Limpieza por patrones
    parser.add_argument(
        "--cleanup-non-it", action="store_true",
        help="Ejecutar limpieza de ofertas no-IT por patrones (antes de Ollama).",
    )
    parser.add_argument(
        "--confirm-cleanup", action="store_true",
        help="Aplicar la limpieza a BD (sin este flag, solo muestra recuento).",
    )

    # Conservadurismo de roles
    parser.add_argument(
        "--update-existing-roles", action="store_true",
        help=(
            "Permitir que Ollama cambie role_category aunque ya tenga una categoria "
            "tecnica valida. Por defecto (sin este flag) se conserva la categoria "
            "existente y solo se añaden skills nuevas."
        ),
    )

    args = parser.parse_args()

    if args.reclassify_all and args.days > 0:
        parser.error("--all y --days son mutuamente excluyentes.")

    if args.batch_size < 1:
        parser.error("--batch-size debe ser >= 1.")

    run(
        reclassify_all=args.reclassify_all,
        days=args.days,
        limit=args.limit,
        max_minutes=args.max_minutes,
        yes=args.yes,
        state_path=args.state_path,
        reprocess=args.reprocess,
        batch_size=args.batch_size,
        include_inactive=args.include_inactive,
        cleanup_non_it=args.cleanup_non_it,
        confirm_cleanup=args.confirm_cleanup,
        update_existing_roles=args.update_existing_roles,
    )
