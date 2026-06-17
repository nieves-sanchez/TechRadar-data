"""
retro_classify.py — Reclasificación retroactiva y limpieza de ofertas en Supabase.

Pasos que ejecuta:
  1. LIMPIEZA RÁPIDA — borra ofertas no-IT detectadas por patrones regex
     (enfermeros, agentes de seguros, conductores, etc.) antes de tocar Ollama.
  2. CLASIFICACIÓN con Ollama en lotes de 10 ofertas por llamada:
     - Actualiza role_category en jobs
     - Añade skills nuevas a job_skills
     - Marca como role_category = NULL las ofertas que Ollama identifica
       como posiblemente no-IT (para revisión manual, nunca borra)

MODOS (mutuamente excluyentes):
  (sin flags)   Solo ofertas con role_category = NULL o 'other'
  --days N      Todas las ofertas ingestadas en los últimos N días
  --all         Toda la base de datos

USO:
  # Solo ofertas con role_category=other o NULL (recomendado primero)
  py -3.12 -m scripts.retro_classify

  # Ofertas de los últimos 7 días (workflow semanal tras cada carga)
  py -3.12 -m scripts.retro_classify --days 7

  # Todas las ofertas (mejora también skills de las ya clasificadas)
  py -3.12 -m scripts.retro_classify --all

  # Prueba con 50 ofertas antes de lanzar todo
  py -3.12 -m scripts.retro_classify --limit 50
"""

import argparse
import logging
import os
import sys
import time

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from scripts.ai_classifier import classify_batch, _is_ollama_available, OLLAMA_MODEL
from scripts.skills_catalog import NON_IT_PATTERNS

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
# retro_classify.py se ejecuta como script independiente (no como módulo importado
# por pipeline.py), por lo que sí configura basicConfig para su propio uso en CLI.
logger = logging.getLogger("techradar.retro_classify")

BATCH_SIZE   = 10   # ofertas por llamada a Ollama
COMMIT_EVERY = 10   # commit cada N ofertas (= 1 batch de Ollama); reduce pérdida en crash


# =============================================================================
# Conexión
# =============================================================================

def _get_connection():
    if not DATABASE_URL:
        logger.error("DATABASE_URL no configurado en .env")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL)


# =============================================================================
# PASO 1: Limpieza rápida por patrones (sin Ollama)
# =============================================================================

def delete_non_it_by_patterns(conn) -> int:
    """
    Marca como is_active=FALSE las ofertas cuyos títulos coinciden con NON_IT_PATTERNS.

    Usa soft-delete en lugar de DELETE hard para preservar la integridad de los
    datos históricos y permitir recuperación si algún patrón fuera demasiado agresivo.
    El pipeline semanal no las reactivará porque el UPSERT solo reactiva ofertas
    que siguen apareciendo en la API de Adzuna.

    Son patrones de alta precisión (enfermero, agente de seguros, mecánico...)
    que identifican ofertas claramente fuera del sector IT.

    Returns:
        Número de filas marcadas como inactivas.
    """
    # Construir condición SQL: title ILIKE '%patrón%' OR ...
    # Procesamos en bloques de 100 para no generar una query kilométrica
    total_deactivated = 0
    chunk_size = 100

    with conn.cursor() as cur:
        for i in range(0, len(NON_IT_PATTERNS), chunk_size):
            chunk = NON_IT_PATTERNS[i : i + chunk_size]
            conditions = " OR ".join(["title ILIKE %s"] * len(chunk))
            params = [f"%{p}%" for p in chunk]

            # Loguear una muestra de los IDs afectados antes de modificar
            cur.execute(
                f"SELECT id, title FROM jobs WHERE is_active = TRUE AND ({conditions}) LIMIT 5",
                params,
            )
            sample = cur.fetchall()
            if sample:
                logger.debug(
                    "  Muestra de ofertas no-IT a desactivar (chunk %d): %s",
                    i // chunk_size + 1,
                    [(row[0], row[1][:60]) for row in sample],
                )

            cur.execute(
                f"UPDATE jobs SET is_active = FALSE WHERE is_active = TRUE AND ({conditions})",
                params,
            )
            total_deactivated += cur.rowcount

    conn.commit()
    return total_deactivated


# =============================================================================
# PASO 2: Consultas a la BD
# =============================================================================

def _build_where(reclassify_all: bool, days: int) -> str:
    """Devuelve la cláusula WHERE según el modo activo."""
    if reclassify_all:
        return ""
    if days > 0:
        return f"WHERE ingested_at >= NOW() - INTERVAL '{days} days'"
    return "WHERE role_category IS NULL OR role_category = 'other'"


def count_jobs(conn, reclassify_all: bool, days: int = 0) -> int:
    where = _build_where(reclassify_all, days)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM jobs {where}")
        return cur.fetchone()[0]


def fetch_jobs_batch(
    conn,
    reclassify_all: bool,
    last_id: int,
    limit: int,
    days: int = 0,
) -> list[dict]:
    """
    Carga un bloque de ofertas desde la BD para procesarlas.

    Paginación por ID (en lugar de OFFSET) para evitar saltar registros
    cuando el WHERE cambia a medida que se procesan ofertas (bug de OFFSET).
    """
    where = _build_where(reclassify_all, days)
    # En modo default (other/NULL) el WHERE ya filtra; añadimos AND id > last_id.
    # En los demás modos el WHERE puede estar vacío; usamos WHERE o AND según corresponda.
    if where:
        id_clause = f"AND id > %s"
    else:
        id_clause = f"WHERE id > %s"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT id, title,
                   COALESCE(description_full, description_short, '') AS description
            FROM   jobs
            {where}
            {id_clause}
            ORDER BY id
            LIMIT  %s
            """,
            (last_id, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def get_existing_skills_for_jobs(cur, job_ids: list[int]) -> dict[int, set[str]]:
    """Devuelve un dict {job_id: set(skill_names_lower)} para un conjunto de ofertas."""
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
# PASO 2: Escritura en BD
# =============================================================================

def update_role_categories(cur, updates: list[tuple[str, int]]) -> None:
    """Actualiza role_category para varios jobs de golpe."""
    if updates:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE jobs SET role_category = %s WHERE id = %s",
            updates,
        )


def upsert_skills_and_links(cur, skill_records: list[dict]) -> int:
    """
    Inserta skills nuevas en el catálogo y crea los vínculos job_skills.

    skill_records: lista de {job_id, skill_name}
    """
    if not skill_records:
        return 0

    # Insertar skills nuevas (ignorar duplicadas)
    unique_names = list({r["skill_name"][:80] for r in skill_records if r["skill_name"].strip()})
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO skills (name, category) VALUES %s ON CONFLICT (name) DO NOTHING",
        [(name, "tool") for name in unique_names],
    )

    # Recuperar IDs de todas las skills del lote
    cur.execute("SELECT id, name FROM skills WHERE name = ANY(%s)", (unique_names,))
    skill_id_map = {name: sid for sid, name in cur.fetchall()}

    # Insertar vínculos job_skills
    links = [
        (r["job_id"], skill_id_map[r["skill_name"]])
        for r in skill_records
        if r["skill_name"] in skill_id_map
    ]
    if links:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO job_skills (job_id, skill_id) VALUES %s ON CONFLICT DO NOTHING",
            links,
        )
    return len(links)


# =============================================================================
# Proceso principal
# =============================================================================

def process_batch(cur, jobs: list[dict], reclassify_all: bool) -> dict:
    """
    Procesa un lote de hasta 10 ofertas con Ollama y actualiza la BD.

    Returns: estadísticas del lote.
    """
    stats = {"roles_changed": 0, "skills_added": 0, "deleted": 0, "errors": 0}

    try:
        results = classify_batch(jobs)
    except Exception as e:
        logger.warning("Error en classify_batch: %s", e)
        stats["errors"] = len(jobs)
        return stats

    job_ids = [j["id"] for j in jobs]
    existing_skills = get_existing_skills_for_jobs(cur, job_ids)

    role_updates  = []
    skill_records = []

    for job, result in zip(jobs, results):
        job_id = job["id"]

        # Si Ollama dice que no es IT, marcar con NULL para revisión manual
        # (NO se borra — el usuario decide qué hacer con ellas)
        if not result.get("is_tech", True):
            role_updates.append((None, job_id))
            stats["roles_changed"] += 1
            continue

        # Actualizar role_category
        cat = result.get("role_category")
        if cat and (reclassify_all or cat != "other"):
            role_updates.append((cat, job_id))
            stats["roles_changed"] += 1

        # Añadir skills nuevas no existentes para esta oferta
        seen = existing_skills.get(job_id, set())
        for skill_name in result.get("skills", []):
            if skill_name and skill_name.lower() not in seen:
                skill_records.append({"job_id": job_id, "skill_name": skill_name})
                seen.add(skill_name.lower())

    # Escribir en BD (nunca borra — solo actualiza y añade)
    update_role_categories(cur, role_updates)
    stats["skills_added"] = upsert_skills_and_links(cur, skill_records)

    return stats


def run(reclassify_all: bool = False, days: int = 0, limit: int = 0) -> None:
    """Función principal."""

    # --- Comprobar Ollama y hacer warmup del modelo ---
    if not _is_ollama_available():
        logger.error("Ollama no está arrancado. Abre la aplicación Ollama.")
        sys.exit(1)
    logger.info("Ollama disponible · modelo: %s", OLLAMA_MODEL)

    # Warmup: la primera llamada al modelo puede tardar 30-60s mientras carga
    # qwen2.5:1.5b en memoria. Lo hacemos antes del cronómetro para que los
    # tiempos estimados sean precisos y el primer batch no timeout.
    logger.info("Calentando modelo Ollama (puede tardar hasta 60s la primera vez)...")
    from scripts.ai_classifier import classify_batch as _warmup_fn
    _warmup_fn([{"title": "warmup", "description": ""}])
    logger.info("Modelo listo.")

    conn = _get_connection()

    try:
        # ---------------------------------------------------------------
        # PASO 1: Limpieza rápida por patrones (sin Ollama, instantánea)
        # ---------------------------------------------------------------
        print()
        logger.info("PASO 1 — Limpieza rápida de ofertas no-IT por patrones (soft-delete)...")
        n_deleted_patterns = delete_non_it_by_patterns(conn)
        logger.info("  → %d ofertas no-IT marcadas como inactivas (is_active=FALSE).", n_deleted_patterns)

        # ---------------------------------------------------------------
        # PASO 2: Clasificación con Ollama en lotes de 10 ofertas
        # ---------------------------------------------------------------
        total = count_jobs(conn, reclassify_all, days)
        if limit > 0:
            total = min(total, limit)

        if reclassify_all:
            mode = "TODAS las ofertas"
        elif days > 0:
            mode = f"ofertas ingestadas en los últimos {days} días"
        else:
            mode = "ofertas con role_category=other o NULL"

        secs_estimate = total * 4  # ~4 seg por oferta en lote de 10 (conservador)
        hours = secs_estimate // 3600
        mins  = (secs_estimate % 3600) // 60

        print()
        logger.info("PASO 2 — Clasificación con Ollama (lotes de %d)", BATCH_SIZE)
        logger.info("  Modo:             %s", mode)
        logger.info("  Ofertas:          %d", total)
        logger.info("  Tiempo estimado:  ~%dh %dm", hours, mins)
        print()

        if total == 0:
            logger.info("No hay ofertas que clasificar.")
            return

        confirm = input(f"¿Proceder con {total} ofertas? [s/N]: ").strip().lower()
        if confirm != "s":
            logger.info("Cancelado.")
            return

        # --- Bucle principal (paginación por ID, robusto ante WHERE cambiante) ---
        processed    = 0
        total_roles  = 0
        total_skills = 0
        total_errors = 0
        t_start      = time.time()
        last_id      = 0  # paginación por ID en lugar de OFFSET

        while processed < total:
            batch_jobs = fetch_jobs_batch(conn, reclassify_all, last_id, BATCH_SIZE, days)
            if not batch_jobs:
                break

            with conn.cursor() as cur:
                stats = process_batch(cur, batch_jobs, reclassify_all)

            conn.commit()

            processed    += len(batch_jobs)
            total_roles  += stats["roles_changed"]
            total_skills += stats["skills_added"]
            total_errors += stats["errors"]
            last_id       = batch_jobs[-1]["id"]  # avanzar el cursor por ID

            # Log de progreso cada 10 ofertas procesadas
            if processed % 10 == 0 or processed >= total:
                elapsed   = time.time() - t_start
                avg       = elapsed / processed
                remaining = int((total - processed) * avg)
                logger.info(
                    "[%d/%d] roles: %d | skills: +%d | errores: %d | restante: ~%ds",
                    processed, total,
                    total_roles, total_skills, total_errors, remaining,
                )

            if processed >= total:
                break

        # --- Resumen ---
        elapsed_total = time.time() - t_start
        print()
        logger.info("=" * 60)
        logger.info("COMPLETADO")
        logger.info("  Limpieza por patrones:  %d desactivadas (soft-delete)", n_deleted_patterns)
        logger.info("  Roles corregidos:       %d", total_roles)
        logger.info("  Skills añadidas:        %d", total_skills)
        logger.info("  Errores:                %d", total_errors)
        logger.info("  Tiempo total:           %.0f segundos", elapsed_total)
        logger.info("=" * 60)

    finally:
        conn.close()


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reclasifica ofertas en Supabase con limpieza no-IT y Ollama en lotes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modos (mutuamente excluyentes):
  (sin flags)   Solo ofertas con role_category = NULL o 'other'
  --days N      Todas las ofertas ingestadas en los últimos N días
  --all         Toda la base de datos
        """,
    )
    parser.add_argument(
        "--all", action="store_true", dest="reclassify_all",
        help="Reclasifica TODAS las ofertas, no solo other/NULL.",
    )
    parser.add_argument(
        "--days", type=int, default=0, metavar="N",
        help="Procesar todas las ofertas ingestadas en los últimos N días.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Procesar máximo N ofertas (0 = sin límite).",
    )
    args = parser.parse_args()

    if args.reclassify_all and args.days > 0:
        parser.error("--all y --days son mutuamente excluyentes.")

    run(reclassify_all=args.reclassify_all, days=args.days, limit=args.limit)
