"""
repair_crawl.py — Rellena description_full en ofertas con crawling pendiente.

Consulta en la base de datos todas las ofertas activas que tienen URL pero no
tienen description_full, y vuelve a intentar el crawling. Diseñado para
ejecutarse el día siguiente al pipeline principal, cuando el throttling de
la noche anterior ya se ha disipado.

Aplica el mismo sistema de backoff y circuit breaker que el pipeline principal:
3 reintentos con backoff ante 429/503, y parada automática si 10 URLs
consecutivas siguen fallando por throttling.

Uso:
    python -m scripts.repair_crawl                     # todas las ofertas pendientes
    python -m scripts.repair_crawl --country pl        # solo un país
    python -m scripts.repair_crawl --limit 2000        # máximo N ofertas
    python -m scripts.repair_crawl --country pl --limit 1000
"""

import argparse
import logging
import time

import psycopg2.extras
import requests
from dotenv import load_dotenv

from scripts.extract import (
    CRAWL_CIRCUIT_BREAKER_THRESHOLD,
    CRAWL_DELAY_SECONDS,
    CRAWL_USER_AGENT,
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


# =============================================================================
# Operaciones de base de datos
# =============================================================================


def _fetch_pending(
    conn,
    country_code: str = None,
    limit: int = None,
) -> list[tuple[int, str]]:
    """
    Recupera de la BD las ofertas activas con URL pero sin description_full.

    Ordena por posted_at DESC para priorizar las más recientes, que son las
    que el enriquecimiento con Ollama necesitará antes.

    Args:
        conn: Conexión psycopg2 activa.
        country_code (str | None): Si se indica, filtra por ese país.
        limit (int | None): Número máximo de ofertas a recuperar.

    Returns:
        list[tuple[int, str]]: Lista de (job_id, url).
    """
    query = """
        SELECT id, url
        FROM   jobs
        WHERE  description_full IS NULL
          AND  url IS NOT NULL
          AND  is_active = TRUE
    """
    params = []

    if country_code:
        query += " AND country_code = %s"
        params.append(country_code)

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


# =============================================================================
# Crawling y lógica principal
# =============================================================================


def run_repair(
    country_code: str = None,
    limit: int = None,
    crawl_delay: float = CRAWL_DELAY_SECONDS,
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
    """
    conn = _get_connection()

    pending = _fetch_pending(conn, country_code=country_code, limit=limit)
    total = len(pending)

    if not pending:
        filtro = f" para país '{country_code}'" if country_code else ""
        logger.info("No hay ofertas pendientes de crawling%s.", filtro)
        conn.close()
        return

    filtro = f" (país: {country_code})" if country_code else ""
    logger.info("Ofertas pendientes: %d%s (delay=%.1fs)", total, filtro, crawl_delay)

    success_count = 0
    consecutive_throttled = 0
    pending_updates: list[tuple[int, str]] = []

    with requests.Session() as session:
        session.headers.update({"User-Agent": CRAWL_USER_AGENT})

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
                        _flush_updates(conn, pending_updates)
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
        _flush_updates(conn, pending_updates)
        logger.info("  %d actualizaciones finales persistidas en BD.", len(pending_updates))

    conn.close()

    success_rate = (success_count / total * 100) if total else 0
    logger.info(
        "Repair crawl completado: %d/%d ofertas actualizadas (%.1f%%)",
        success_count, total, success_rate,
    )


# =============================================================================
# Punto de entrada para ejecución directa: python -m scripts.repair_crawl
# =============================================================================

if __name__ == "__main__":
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

    args = parser.parse_args()

    run_repair(
        country_code=args.country,
        limit=args.limit,
        crawl_delay=args.delay,
    )
