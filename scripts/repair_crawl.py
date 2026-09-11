"""
repair_crawl.py — Rellena description_full en ofertas con crawling pendiente.

Consulta en la base de datos las ofertas activas que tienen URL /details/ pero
no tienen description_full, y vuelve a intentar el crawling. Excluye las URLs
/land/ (0% de éxito histórico — ver _fetch_pending). Es el motor de Pipeline B
v1 (enriquecimiento de descripciones), independiente del pipeline ETL principal.
Diseñado para ejecutarse cuando el throttling de runs anteriores ya se ha disipado.

Aplica el mismo sistema de backoff y circuit breaker que el pipeline principal:
3 reintentos con backoff ante 429/503, y parada automática si 10 URLs
consecutivas siguen fallando por throttling.

Uso:
    python -m scripts.repair_crawl                     # todas las ofertas pendientes
    python -m scripts.repair_crawl --country pl        # solo un país
    python -m scripts.repair_crawl --limit 2000        # máximo N ofertas
    python -m scripts.repair_crawl --country pl --limit 1000
    python -m scripts.repair_crawl --since-days 2 --limit 3000  # solo inflow reciente
    python -m scripts.repair_crawl --enrich             # además, enriquecimiento
                                                          # determinista de FASE B
                                                          # (skills/remote/role_category)
                                                          # sobre lo que se acaba de
                                                          # crawlear. Desactivado por
                                                          # defecto — opt-in explícito.
    python -m scripts.repair_crawl --job-ids 123 456 789 --enrich  # solo esos IDs
                                                          # (filtro adicional, opt-in, para
                                                          # pilotos/diagnóstico controlados —
                                                          # ver FASE B Paso 5). No se salta
                                                          # ninguna guarda normal de
                                                          # _fetch_pending().
"""

import argparse
import logging
import time

import psycopg2.extras
import requests
from dotenv import load_dotenv

from scripts.enrich import enrich_jobs
from scripts.extract import (
    CRAWL_BROWSER_HEADERS,
    CRAWL_CIRCUIT_BREAKER_THRESHOLD,
    CRAWL_DELAY_SECONDS,
    crawl_description,
)
from scripts.load import _get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("techradar.repair_crawl")

load_dotenv()

# Número de actualizaciones acumuladas antes de hacer flush a la BD.
# Un valor bajo minimiza la pérdida de datos si el script se interrumpe.
# Un valor alto reduce el número de round-trips a la BD.
UPDATE_BATCH_SIZE = 100

# Intentos de enrich_jobs() por batch antes de diferirlo al reintento final de la ejecución
# (1 intento inicial + 1 reintento inmediato). Ver notes/PROJECT_MASTER_CONTEXT.md §57.6.11
# (Paso 4, revisión de fiabilidad 2026-09-11): sin esto, un fallo transitorio de enrich_jobs()
# dejaría permanentemente sin enriquecer ofertas cuya description_full ya se persistió —
# _fetch_pending() ya no las vuelve a seleccionar porque description_full deja de ser NULL.
ENRICH_MAX_ATTEMPTS = 2

# Errores de conexión (no de lógica SQL): tras uno de estos, la conexión puede no ser segura
# de reutilizar ni siquiera para hacer rollback() — hay que reconectar, no reintentar sobre
# la misma conexión.
_CONNECTION_LOST_ERRORS = (psycopg2.OperationalError, psycopg2.InterfaceError)


# =============================================================================
# Operaciones de base de datos
# =============================================================================


def _fetch_pending(
    conn,
    country_code: str = None,
    limit: int = None,
    since_days: int = None,
    job_ids: list = None,
) -> list[tuple[int, str]]:
    """
    Recupera de la BD las ofertas activas con URL pero sin description_full.

    Excluye las URLs /land/ (redirects de Adzuna a portales externos): la
    auditoría 2026-06-22 confirmó 0% de éxito histórico con requests en los 8
    países (27.953 ofertas), porque el destino es JS-rendered o tiene bot
    detection de terceros. Procesarlas solo desperdicia tiempo de ejecución.
    Pipeline B v1 trabaja exclusivamente sobre URLs /details/ (cobertura 71,7%).

    Ordena por posted_at DESC para priorizar las más recientes, que son las
    que el enriquecimiento con Ollama necesitará antes.

    Args:
        conn: Conexión psycopg2 activa.
        country_code (str | None): Si se indica, filtra por ese país.
        limit (int | None): Número máximo de ofertas a recuperar.
        since_days (int | None): Si se indica, solo ofertas con posted_at en los
            últimos N días. Sin tracking de intentos en BD (crawl_attempts), cada
            run reintenta las ofertas que ya fallaron. Acotar por antigüedad evita
            re-procesar la cola dura histórica: los fallos viejos quedan fuera de
            la ventana y solo se reintenta el inflow reciente. None = sin filtro
            temporal (comportamiento original).
        job_ids (list[int] | None): Si se indica, restringe el universo a exactamente
            esos IDs — un job solo se devuelve si además cumple TODAS las guardas
            normales de arriba (`description_full IS NULL`, `is_active`, etc.). Es un
            filtro ADICIONAL (`AND id = ANY(...)`), no un atajo que las salte. Se
            combina por intersección con `country_code`/`since_days` si también se
            pasan — mismo patrón que el resto de filtros de esta función, sin lógica
            especial. Pensado para ejecuciones controladas/diagnósticas (FASE B —
            Paso 5, ver notes/PROJECT_MASTER_CONTEXT.md Parte 8), no para uso normal.

    Returns:
        list[tuple[int, str]]: Lista de (job_id, url).
    """
    # El patrón LIKE se pasa como parámetro (no como literal en la query) para
    # que psycopg2 no interprete los '%' del patrón como marcadores de formato
    # cuando se añaden params de country/limit. Como params nunca queda vacío,
    # execute() siempre hace la sustitución y los '%s' se resuelven igual.
    query = """
        SELECT id, url
        FROM   jobs
        WHERE  description_full IS NULL
          AND  url IS NOT NULL
          AND  is_active = TRUE
          AND  url NOT LIKE %s
    """
    params = ["%/land/%"]

    if country_code:
        query += " AND country_code = %s"
        params.append(country_code)

    if since_days:
        # make_interval(days => %s) construye el intervalo de forma segura desde
        # un parámetro entero, sin interpolar texto en la query.
        query += " AND posted_at >= NOW() - make_interval(days => %s)"
        params.append(since_days)

    if job_ids:
        # ANY(%s) con una lista de enteros como parámetro — psycopg2 la adapta
        # directamente a un array de PostgreSQL, sin interpolar IDs en el SQL.
        query += " AND id = ANY(%s)"
        params.append(list(job_ids))

    query += " ORDER BY posted_at DESC"

    if limit:
        query += " LIMIT %s"
        params.append(limit)

    with conn.cursor() as cur:
        cur.execute(query, params or None)
        return cur.fetchall()


def _flush_updates(conn, updates: list[tuple[int, str]]) -> None:
    """
    Persiste en la BD los description_full obtenidos por el crawling.

    Usa un UPDATE con VALUES para actualizar en batch y reducir round-trips.
    Cada llamada abre su propia transacción (conn.commit al final).

    Args:
        conn: Conexión psycopg2 activa.
        updates: Lista de (job_id, description_full).
    """
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            UPDATE jobs
            SET    description_full = v.description_full
            FROM   (VALUES %s) AS v(job_id, description_full)
            WHERE  jobs.id = v.job_id::bigint
            """,
            updates,
            page_size=500,
        )
    conn.commit()


def _reconnect(old_conn):
    """
    Cierra una conexión rota de forma segura y abre una nueva.

    Tanto el rollback como el close se protegen individualmente: una conexión ya rota puede
    volver a lanzar al intentar cualquier operación sobre ella, y eso no debe impedir abrir la
    conexión nueva. Mismo patrón ya usado en `retro_classify.py::_reconnect_supabase` — no se
    importa directamente desde ahí para no acoplar FASE B al módulo de Ollama (ver
    notes/PROJECT_MASTER_CONTEXT.md Parte 8 §60.5); se reimplementa aquí porque son 6 líneas y
    la alternativa (import cruzado entre repair_crawl.py y retro_classify.py) es peor.
    """
    try:
        old_conn.rollback()
    except Exception:
        pass
    try:
        old_conn.close()
    except Exception:
        pass
    logger.info("Reconectando a la BD tras pérdida de conexión durante el enriquecimiento...")
    new_conn = _get_connection()
    logger.info("Reconexión establecida.")
    return new_conn


def _attempt_enrich(conn, job_ids: list[int]):
    """
    Un único intento de enrich_jobs(conn, job_ids), clasificando el resultado.

    `enrich_jobs()` no hace rollback propio (contrato transaccional cerrado en
    §57.6.11/§57.6.14): ante una excepción de lógica/SQL recuperable, este intento hace el
    `conn.rollback()` por su cuenta, protegido — si el propio rollback también falla, es señal
    de que la conexión está realmente rota, no de un error recuperable, y se reclasifica como tal.

    Returns:
        ("ok", stats): éxito, `stats` es el dict de enrich_jobs().
        ("connection_lost", None): error de conexión (o rollback fallido) — `conn` ya no debe
            reutilizarse ni para un rollback; hace falta `_reconnect()`.
        ("failed", None): error recuperable, ya con `conn.rollback()` aplicado con éxito —
            `conn` sigue siendo utilizable para un reintento.
    """
    try:
        return "ok", enrich_jobs(conn, job_ids)
    except _CONNECTION_LOST_ERRORS:
        logger.error(
            "enrich_jobs() perdió la conexión a la BD (job_ids=%s).", job_ids, exc_info=True
        )
        return "connection_lost", None
    except Exception:
        logger.error(
            "enrich_jobs() falló sobre %d ofertas (job_ids=%s).",
            len(job_ids), job_ids, exc_info=True,
        )
        try:
            conn.rollback()
        except _CONNECTION_LOST_ERRORS:
            logger.error("conn.rollback() también perdió la conexión.", exc_info=True)
            return "connection_lost", None
        except Exception:
            logger.error(
                "conn.rollback() falló de forma inesperada; se trata como conexión perdida.",
                exc_info=True,
            )
            return "connection_lost", None
        return "failed", None


def _record_enrich_success(stats: dict, enrich_totals: dict) -> None:
    for key, value in stats.items():
        enrich_totals[key] += value
    logger.info(
        "  Enriquecidas %d ofertas: skills_links_attempted=%d remote_updated=%d "
        "role_category_updated=%d",
        stats["jobs_seen"], stats["skills_links_attempted"],
        stats["remote_updated"], stats["role_category_updated"],
    )


def _flush_and_enrich(
    conn,
    updates: list[tuple[int, str]],
    enrich: bool,
    enrich_totals: dict,
    failed_enrich_ids: list,
):
    """
    Persiste un batch de description_full y, si --enrich está activo, enriquece
    inmediatamente después esas mismas ofertas (FASE B — Paso 4, ver
    notes/PROJECT_MASTER_CONTEXT.md §57.6.11 y Parte 8).

    `enrich_jobs()` solo se llama con los job_id que ACABAN de persistirse con éxito en este
    batch — nunca sobre ofertas fallidas/no crawleadas, y nunca sobre toda la BD. Si
    `_flush_updates()` lanza, esta función no llega a llamar a `enrich_jobs()` (el
    enriquecimiento solo procede tras un flush correcto).

    Reintenta hasta `ENRICH_MAX_ATTEMPTS` veces (revisión de fiabilidad 2026-09-11): como
    `_fetch_pending()` selecciona por `description_full IS NULL`, una oferta cuyo
    description_full ya se persistió NUNCA vuelve a ser candidata en una ejecución futura —
    perder su enriquecimiento en silencio recrearía el desfase de DQ-06. Si se agotan los
    intentos, el batch se añade a `failed_enrich_ids` para el reintento final consolidado de
    `run_repair()` — no se pierde en silencio, pero tampoco hay garantía absoluta sin tracking
    persistente entre ejecuciones (ver limitación documentada en `run_repair()`).

    Ante un error de conexión, reconecta (`_reconnect()`) antes de reintentar; ante un error
    recuperable, reintenta sobre la misma conexión tras su rollback. En ningún caso una
    excepción de `enrich_jobs()` o de su rollback se propaga fuera de esta función: el crawling
    y los flushes posteriores de repair_crawl continúan con normalidad, igual que si --enrich no
    se hubiera pasado.

    Args:
        conn: Conexión psycopg2 activa.
        updates: Lista de (job_id, description_full) de este batch. Si está vacía, no hace nada.
        enrich: Si False, se comporta exactamente igual que antes del Paso 4.
        enrich_totals: dict mutable donde se acumulan los contadores de enrich_jobs() de toda
            la ejecución.
        failed_enrich_ids: lista mutable donde se acumulan los job_id cuyo enriquecimiento no
            se consiguió tras agotar los reintentos de este batch.

    Returns:
        La conexión activa a partir de ahora (la misma `conn` recibida, o una nueva si hubo
        que reconectar). El caller debe seguir usando el valor devuelto, no el original.
    """
    if not updates:
        return conn

    _flush_updates(conn, updates)

    if not enrich:
        return conn

    job_ids = [job_id for job_id, _ in updates]
    stats = None

    for _ in range(ENRICH_MAX_ATTEMPTS):
        outcome, result = _attempt_enrich(conn, job_ids)
        if outcome == "ok":
            stats = result
            break
        if outcome == "connection_lost":
            try:
                conn = _reconnect(conn)
            except Exception:
                logger.error(
                    "No se pudo reconectar tras perder la conexión durante enrich_jobs() "
                    "(job_ids=%s). Se abandona el reintento de este batch.",
                    job_ids, exc_info=True,
                )
                break
        # "failed": conn sigue siendo válida (rollback ya aplicado) — se reintenta con ella.

    if stats is None:
        logger.warning(
            "  %d ofertas quedan pendientes de enriquecimiento tras %d intento(s) "
            "(job_ids=%s); se reintentará una última vez al final de la ejecución.",
            len(job_ids), ENRICH_MAX_ATTEMPTS, job_ids,
        )
        failed_enrich_ids.extend(job_ids)
        return conn

    _record_enrich_success(stats, enrich_totals)
    return conn


def _retry_failed_enrichment(conn, job_ids: list, enrich_totals: dict):
    """
    Último intento, consolidado, sobre los job_id que agotaron sus reintentos durante los
    batches de la ejecución (ver `_flush_and_enrich`). Se ejecuta una sola vez, al final de
    `run_repair()`.

    Returns:
        (conn, still_failed): `conn` es la conexión utilizable resultante (puede ser nueva si
        hubo reconexión). `still_failed` es la lista de job_id que siguen sin enriquecerse tras
        este último intento — vacía si tuvo éxito.
    """
    outcome, result = _attempt_enrich(conn, job_ids)

    if outcome == "connection_lost":
        try:
            conn = _reconnect(conn)
        except Exception:
            logger.error(
                "Reconexión fallida en el reintento final de enriquecimiento.", exc_info=True
            )
            return conn, list(job_ids)
        outcome, result = _attempt_enrich(conn, job_ids)

    if outcome != "ok":
        return conn, list(job_ids)

    _record_enrich_success(result, enrich_totals)
    logger.info(
        "  Reintento final de enriquecimiento recuperó %d ofertas (job_ids=%s).",
        result["jobs_seen"], job_ids,
    )
    return conn, []


# =============================================================================
# Crawling y lógica principal
# =============================================================================


def run_repair(
    country_code: str = None,
    limit: int = None,
    crawl_delay: float = CRAWL_DELAY_SECONDS,
    since_days: int = None,
    enrich: bool = False,
    job_ids: list = None,
) -> None:
    """
    Recupera las ofertas pendientes de crawling y actualiza description_full en la BD.

    El circuit breaker para el crawling si CRAWL_CIRCUIT_BREAKER_THRESHOLD URLs
    consecutivas fallan por throttling. Las actualizaciones acumuladas hasta ese
    punto se persisten antes de salir.

    Args:
        country_code (str | None): Filtra por país (ej: 'pl'). None para todos.
        limit (int | None): Número máximo de ofertas a procesar en esta ejecución.
        crawl_delay (float): Segundos de pausa entre peticiones. Por defecto 2.0.
        since_days (int | None): Si se indica, solo ofertas con posted_at en los
            últimos N días (ver _fetch_pending). None = sin filtro temporal.
        enrich (bool): Si True, tras cada flush exitoso de description_full llama a
            `enrich_jobs()` (FASE B — Paso 4) con los job_id recién persistidos, para
            recalcular skills/remote/role_category de forma determinista. Desactivado
            por defecto: si es False, el comportamiento es idéntico al de antes del
            Paso 4. Reintenta hasta ENRICH_MAX_ATTEMPTS veces por batch, reconectando si
            la conexión se pierde, y hace un último intento consolidado al final de la
            ejecución sobre lo que siga fallando — ver `_flush_and_enrich()` y
            `_retry_failed_enrichment()` para el detalle, y notes/PROJECT_MASTER_CONTEXT.md
            §57.6.11 para la limitación conocida (sin tracking persistente entre ejecuciones).
        job_ids (list[int] | None): Si se indica, restringe el universo de candidatos a
            exactamente esos IDs (filtro adicional sobre `_fetch_pending()`, no un atajo
            que se salte sus guardas — ver su docstring). Pensado para ejecuciones
            controladas/diagnósticas (FASE B — Paso 5). None = comportamiento normal,
            sin filtro por ID.
    """
    conn = _get_connection()

    pending = _fetch_pending(
        conn, country_code=country_code, limit=limit, since_days=since_days,
        job_ids=job_ids,
    )
    total = len(pending)

    if job_ids:
        # Observabilidad explícita: el pool es dinámico (otras ejecuciones de Pipeline B
        # pueden rellenar description_full entre que se eligen los IDs y que se ejecuta
        # este run), así que cuántos de los solicitados siguen siendo candidatos elegibles
        # es información operativa relevante, no un detalle interno — sin query adicional,
        # ambos números ya están disponibles aquí.
        logger.info("IDs solicitados: %d | pendientes elegibles: %d", len(job_ids), total)

    if not pending:
        filtro = f" para país '{country_code}'" if country_code else ""
        logger.info("No hay ofertas pendientes de crawling%s.", filtro)
        conn.close()
        return

    filtro = f" (país: {country_code})" if country_code else ""
    ventana = f" (últimos {since_days}d)" if since_days else ""
    logger.info(
        "Ofertas pendientes: %d%s%s (delay=%.1fs)", total, filtro, ventana, crawl_delay
    )

    success_count = 0
    consecutive_throttled = 0
    pending_updates: list[tuple[int, str]] = []
    enrich_totals = {
        "jobs_seen": 0,
        "skills_links_attempted": 0,
        "remote_updated": 0,
        "role_category_updated": 0,
    }
    # job_id cuyo enrich_jobs() agotó sus reintentos dentro de un batch (ver
    # _flush_and_enrich); se reintentan una última vez, todos juntos, al final de la ejecución.
    failed_enrich_ids: list[int] = []

    with requests.Session() as session:
        session.headers.update(CRAWL_BROWSER_HEADERS)

        for i, (job_id, url) in enumerate(pending):
            if i > 0:
                time.sleep(crawl_delay)

            full_text, throttled = crawl_description(session, url)

            if throttled:
                consecutive_throttled += 1
                logger.warning(
                    "Throttling para job_id=%d (%d consecutivos).",
                    job_id, consecutive_throttled,
                )
                if consecutive_throttled >= CRAWL_CIRCUIT_BREAKER_THRESHOLD:
                    logger.error(
                        "Circuit breaker activado tras %d throttlings consecutivos. "
                        "Repair crawl detenido — %d ofertas siguen pendientes. "
                        "Volver a ejecutar más tarde.",
                        CRAWL_CIRCUIT_BREAKER_THRESHOLD, total - i,
                    )
                    break
            else:
                consecutive_throttled = 0
                if full_text:
                    pending_updates.append((job_id, full_text))
                    success_count += 1

                    # Flush periódico para no perder trabajo si el script se interrumpe
                    if len(pending_updates) >= UPDATE_BATCH_SIZE:
                        conn = _flush_and_enrich(
                            conn, pending_updates, enrich, enrich_totals, failed_enrich_ids
                        )
                        logger.info(
                            "  %d actualizaciones persistidas en BD.", len(pending_updates)
                        )
                        pending_updates.clear()
                else:
                    logger.debug("Sin descripción para job_id=%d", job_id)

            if (i + 1) % 50 == 0:
                logger.info(
                    "  %d/%d procesadas (%d con éxito)", i + 1, total, success_count
                )

    # Flush final con lo que quede en el buffer
    if pending_updates:
        conn = _flush_and_enrich(
            conn, pending_updates, enrich, enrich_totals, failed_enrich_ids
        )
        logger.info("  %d actualizaciones finales persistidas en BD.", len(pending_updates))

    # Reintento final consolidado: ofertas cuyo description_full ya se persistió pero cuyo
    # enrich_jobs() agotó los reintentos de su propio batch. Ver _retry_failed_enrichment().
    if enrich and failed_enrich_ids:
        logger.warning(
            "Reintentando enriquecimiento diferido para %d ofertas (job_ids=%s).",
            len(failed_enrich_ids), failed_enrich_ids,
        )
        conn, still_failed = _retry_failed_enrichment(conn, failed_enrich_ids, enrich_totals)
        if still_failed:
            logger.error(
                "ATENCIÓN: %d ofertas con description_full ya persistida NO se pudieron "
                "enriquecer tras reintentos: job_ids=%s. No existe tracking persistente entre "
                "ejecuciones para reintentar estos IDs automáticamente — requieren un futuro "
                "backfill (Paso 7 de FASE B, diferido, ver notes/PROJECT_MASTER_CONTEXT.md "
                "Parte 8 §60.3) o volver a ejecutar repair_crawl --enrich cuando la causa del "
                "fallo se haya resuelto.",
                len(still_failed), still_failed,
            )

    try:
        conn.close()
    except Exception:
        logger.warning(
            "conn.close() falló al finalizar (la conexión ya podría estar rota); se ignora.",
            exc_info=True,
        )

    success_rate = (success_count / total * 100) if total else 0
    logger.info(
        "Repair crawl completado: %d/%d ofertas actualizadas (%.1f%%)",
        success_count, total, success_rate,
    )
    if enrich:
        logger.info(
            "Enriquecimiento (--enrich) acumulado: jobs_seen=%d skills_links_attempted=%d "
            "remote_updated=%d role_category_updated=%d",
            enrich_totals["jobs_seen"],
            enrich_totals["skills_links_attempted"],
            enrich_totals["remote_updated"],
            enrich_totals["role_category_updated"],
        )


# =============================================================================
# Punto de entrada para ejecución directa: python -m scripts.repair_crawl
# =============================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    """
    Construye el parser de argumentos. Extraído a función (sin cambio de comportamiento)
    para poder testear el parsing de `--job-ids` directamente, sin invocar el script como
    subproceso.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Rellena description_full en ofertas activas con URL pero sin descripción completa. "
            "Pensado para ejecutarse el día siguiente al pipeline principal."
        )
    )
    parser.add_argument(
        "--country",
        metavar="CC",
        help="Procesa solo el país indicado (ej: pl, de, fr). Por defecto todos.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Número máximo de ofertas a procesar en esta ejecución.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=CRAWL_DELAY_SECONDS,
        metavar="S",
        help=f"Segundos de pausa entre peticiones (default: {CRAWL_DELAY_SECONDS}).",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        metavar="N",
        help=(
            "Procesa solo ofertas con posted_at en los últimos N días. "
            "Evita reintentar la cola dura histórica de /details/. "
            "Por defecto sin filtro temporal."
        ),
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help=(
            "Tras persistir cada batch de description_full, aplica el enriquecimiento "
            "determinista de FASE B (enrich_jobs: skills/remote/role_category) sobre "
            "esas mismas ofertas. Desactivado por defecto."
        ),
    )
    parser.add_argument(
        "--job-ids",
        type=int,
        nargs="+",
        metavar="ID",
        help=(
            "Restringe el universo de candidatos a exactamente estos IDs (separados por "
            "espacio). Filtro ADICIONAL: un job solo se procesa si además sigue cumpliendo "
            "todas las guardas normales (description_full IS NULL, is_active, etc. — no se "
            "salta ninguna). Se combina con --country/--since-days si se pasan también. "
            "Pensado para ejecuciones controladas/diagnósticas (FASE B — Paso 5), no para "
            "uso normal. Por defecto, sin filtro por ID."
        ),
    )

    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()

    run_repair(
        country_code=args.country,
        limit=args.limit,
        crawl_delay=args.delay,
        since_days=args.since_days,
        enrich=args.enrich,
        job_ids=args.job_ids,
    )
