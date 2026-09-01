"""
load.py — Carga de datos en PostgreSQL (Supabase) para TechRadar.

Implementa la estrategia UPSERT incremental sobre la tabla jobs:
  - Ofertas nuevas se insertan con is_active=TRUE
  - Ofertas ya existentes actualizan last_seen_at y sus campos de contenido
  - description_full se conserva si la nueva ingesta no trae valor (ver DQ-14)
  - Si una oferta fue marcada inactiva y reaparece en la API, se reactiva
  - Tras cada carga se marcan como is_active=FALSE las ofertas con
    posted_at anterior a INACTIVE_AFTER_DAYS dias

Funciones principales:
    load_jobs(jobs_df, job_skills_df)   -> None
    load_eurostat(eurostat_df)          -> None

Uso:
    from scripts.load import load_jobs, load_eurostat
"""

import logging
import os

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

logger = logging.getLogger("techradar.load")

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")

INACTIVE_AFTER_DAYS = 90
BATCH_SIZE = 500

# Columnas cuyo valor ya almacenado NO debe ser machacado por un NULL entrante.
# DQ-14: Pipeline A se ejecuta con --no-crawl, asi que envia description_full=NULL.
# Sin esta proteccion, la re-ingesta de una oferta borraria el texto que Pipeline B
# habia conseguido por crawling. Un valor entrante NO nulo si sustituye al anterior.
PRESERVE_ON_NULL = ("description_full",)


def _get_connection() -> psycopg2.extensions.connection:
    if not DATABASE_URL:
        raise EnvironmentError(
            "Variable de entorno DATABASE_URL no configurada. "
            "Copia .env.example a .env y rellena la connection string de Supabase."
        )
    return psycopg2.connect(DATABASE_URL)


def _clean(value):
    """
    Convierte los tipos NA de pandas a Python None para psycopg2, y normaliza
    los tipos numpy/pandas integer a Python int.

    psycopg2 traduce Python None -> SQL NULL correctamente.
    Los tipos pd.NA, pd.NaT y float(nan) no son reconocidos por psycopg2
    y causarian errores de tipo o insertarian valores inesperados.
    Los tipos numpy integer (int16, int32, int64) y pandas nullable integer
    (Int16, Int32, Int64) tampoco son adaptados por psycopg2 y deben
    convertirse a Python int antes de enviarlos.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    return value


def _df_to_rows(df: pd.DataFrame, columns: list) -> list:
    return [
        tuple(_clean(v) for v in row)
        for row in df[columns].itertuples(index=False, name=None)
    ]


def _upsert_jobs(cur, jobs_df: pd.DataFrame) -> int:
    cols = [
        "id", "source", "title", "company", "location_display", "city",
        "country_code", "role_category",
        "salary_min", "salary_max", "salary_mid", "salary_is_predicted",
        "contract_type", "contract_time", "remote",
        "description_short", "description_full", "url", "posted_at",
    ]
    cols = [c for c in cols if c in jobs_df.columns]
    rows = _df_to_rows(jobs_df, cols)
    update_set = ", ".join(
        f"{col} = COALESCE(EXCLUDED.{col}, jobs.{col})"
        if col in PRESERVE_ON_NULL
        else f"{col} = EXCLUDED.{col}"
        for col in cols
        if col != "id"
    )
    psycopg2.extras.execute_values(
        cur,
        f"""
        INSERT INTO jobs ({", ".join(cols)})
        VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            {update_set},
            last_seen_at = NOW(),
            is_active    = TRUE
        """,
        rows,
        page_size=BATCH_SIZE,
    )
    return len(rows)


def _upsert_skills(cur, job_skills_df: pd.DataFrame) -> dict:
    if job_skills_df.empty:
        return {}
    unique_skills = (
        job_skills_df[["skill_name", "skill_category"]]
        .drop_duplicates(subset=["skill_name"])
        .values.tolist()
    )
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO skills (name, category) VALUES %s
        ON CONFLICT (name) DO UPDATE SET category = EXCLUDED.category
        """,
        unique_skills,
        page_size=BATCH_SIZE,
    )
    names = [row[0] for row in unique_skills]
    cur.execute("SELECT id, name FROM skills WHERE name = ANY(%s)", (names,))
    return {name: sid for sid, name in cur.fetchall()}


def _upsert_job_skills(cur, job_skills_df: pd.DataFrame, skill_id_map: dict) -> int:
    if job_skills_df.empty or not skill_id_map:
        return 0
    rows = [
        (int(row["job_id"]), skill_id_map[row["skill_name"]])
        for _, row in job_skills_df.iterrows()
        if row["skill_name"] in skill_id_map
    ]
    if not rows:
        return 0
    psycopg2.extras.execute_values(
        cur,
        """
        INSERT INTO job_skills (job_id, skill_id) VALUES %s
        ON CONFLICT (job_id, skill_id) DO NOTHING
        """,
        rows,
        page_size=BATCH_SIZE,
    )
    return len(rows)


def _deactivate_old_jobs(cur) -> int:
    cur.execute(
        """
        UPDATE jobs
        SET    is_active = FALSE
        WHERE  is_active = TRUE
          AND  posted_at < NOW() - make_interval(days => %s)
        """,
        (INACTIVE_AFTER_DAYS,),
    )
    return cur.rowcount


def load_jobs(jobs_df: pd.DataFrame, job_skills_df: pd.DataFrame) -> None:
    """
    Carga las ofertas y sus skills en PostgreSQL dentro de una sola transaccion.

    Orden de operaciones:
      1. UPSERT jobs           -- inserta nuevas, actualiza existentes
      2. UPSERT skills         -- amplia el catalogo con skills nuevas
      3. UPSERT job_skills     -- vincula ofertas con sus skills
      4. Mantenimiento         -- is_active=FALSE en ofertas > 90 dias

    Si cualquier paso falla se hace rollback completo: la BD queda como estaba.
    """
    if jobs_df.empty:
        logger.warning("load_jobs: jobs_df vacio, nada que cargar.")
        return
    conn = _get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                n_jobs = _upsert_jobs(cur, jobs_df)
                logger.info("UPSERT jobs: %d filas.", n_jobs)
                skill_id_map = _upsert_skills(cur, job_skills_df)
                logger.info("UPSERT skills: %d skills en catalogo.", len(skill_id_map))
                n_links = _upsert_job_skills(cur, job_skills_df, skill_id_map)
                logger.info("UPSERT job_skills: %d vinculos.", n_links)
                n_deactivated = _deactivate_old_jobs(cur)
                logger.info("Mantenimiento: %d ofertas marcadas como inactivas.", n_deactivated)
    except Exception as exc:
        logger.error("load_jobs fallo -- se hizo rollback completo: %s", exc)
        raise
    finally:
        conn.close()


def load_eurostat(eurostat_df: pd.DataFrame) -> None:
    """
    Carga los datos de Eurostat en labor_market_context.

    ON CONFLICT actualiza el valor por si Eurostat revisara una cifra publicada
    (ocurre ocasionalmente con datos preliminares que se corrigen al anio siguiente).
    """
    if eurostat_df.empty:
        logger.warning("load_eurostat: DataFrame vacio, nada que cargar.")
        return
    cols = ["country_code", "year", "indicator", "value"]
    rows = _df_to_rows(eurostat_df, cols)
    conn = _get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    """
                    INSERT INTO labor_market_context (country_code, year, indicator, value)
                    VALUES %s
                    ON CONFLICT (country_code, year, indicator)
                    DO UPDATE SET value = EXCLUDED.value
                    """,
                    rows,
                    page_size=BATCH_SIZE,
                )
                logger.info("UPSERT labor_market_context: %d registros.", len(rows))
    except Exception as exc:
        logger.error("load_eurostat fallo -- se hizo rollback completo: %s", exc)
        raise
    finally:
        conn.close()


# =============================================================================
# Persistencia de skills — compartida entre Pipeline C y FASE B (scripts/enrich.py)
# =============================================================================
#
# Movidas aquí desde retro_classify.py (FASE B, Paso 1 de §57.6.11 en
# notes/PROJECT_MASTER_CONTEXT.md) para que exista una única implementación
# reutilizable tanto por Pipeline C como por el futuro scripts/enrich.py, sin
# duplicar la lógica de canonicalización. retro_classify.py mantiene
# compatibilidad mediante import directo (ver cabecera de ese módulo).
# Comportamiento SIN CAMBIOS respecto a la versión anterior: exact-match-first,
# fallback case-insensitive, ON CONFLICT DO NOTHING en skills y job_skills.


def _resolve_skill_id_map(
    cur,
    unique_skills: dict[str, tuple[str, str]],
) -> dict[str, int]:
    """
    Resuelve {lower_name -> skill_id} garantizando siempre el ID canonico exacto.

    Estrategia en dos fases:

    Fase 1 — exact match (name = ANY(canonical_names)):
        Tras el INSERT previo con ON CONFLICT (name) DO NOTHING, cada nombre
        canonico debe existir en BD. Esta consulta lo encuentra de forma
        determinista: si 'React' (id=1) y 'react' (id=5) coexisten en BD,
        solo 'React' aparece aqui -> id=1 siempre, sin ambiguedad de orden.

    Fase 2 — fallback case-insensitive (solo para lower_names no resueltos):
        No deberia ejecutarse para skills del catalogo (la fase 1 las encuentra
        todas). Actua de defensa para skills libres cuyo nombre exacto no se
        encontro. Si hay multiples candidatos para el mismo lower_name:
        - Se prefiere el que coincide exactamente con el nombre canonico.
        - Si ninguno coincide, se usa el de menor id (fila mas antigua).

    Args:
        cur: Cursor psycopg2 activo (ejecuta hasta 2 SELECT).
        unique_skills: {lower_name: (canonical_name, category)}.

    Returns:
        {lower_name: skill_id}. Puede tener menos entradas que unique_skills
        si algun nombre no se resuelve (no deberia ocurrir tras INSERT).
    """
    if not unique_skills:
        return {}

    canonical_names = [v[0] for v in unique_skills.values()]

    # Fase 1: exact match por nombre canonico — determinista, no depende de orden
    cur.execute(
        "SELECT id, name FROM skills WHERE name = ANY(%s)",
        (canonical_names,),
    )
    skill_id_map: dict[str, int] = {}
    for row in cur.fetchall():
        skill_id_map[row[1].lower()] = row[0]

    # Fase 2: fallback case-insensitive para cualquier lower_name no resuelto
    missing = [k for k in unique_skills if k not in skill_id_map]
    if missing:
        cur.execute(
            "SELECT id, name, LOWER(name) AS lname FROM skills WHERE LOWER(name) = ANY(%s)",
            (missing,),
        )
        candidates: dict[str, list[tuple[int, str]]] = {}
        for row in cur.fetchall():
            candidates.setdefault(row[2], []).append((row[0], row[1]))

        for lname, rows in candidates.items():
            canonical = unique_skills[lname][0]
            # Preferir la fila cuyo name coincide exactamente con el canonico
            exact = [r for r in rows if r[1] == canonical]
            if exact:
                skill_id_map[lname] = exact[0][0]
            else:
                # Sin coincidencia exacta: usar el id mas bajo (fila mas antigua)
                skill_id_map[lname] = min(rows, key=lambda r: r[0])[0]

    return skill_id_map


def upsert_skills_and_links(cur, skill_records: list[dict]) -> int:
    """
    Inserta skills nuevas (con nombre canonico) y crea vinculos job_skills.

    Garantiza que siempre se usa el ID exacto del nombre canonico ('React',
    'Node.js', ...) mediante resolucion en dos fases: exact-match primero,
    fallback case-insensitive solo si el exacto falla. Ver _resolve_skill_id_map.

    Args:
        skill_records: lista de {job_id, skill_name, skill_category}.

    Returns:
        Numero de vinculos job_skills insertados.
    """
    if not skill_records:
        return 0

    # Dedup por nombre normalizado
    unique_skills: dict[str, tuple[str, str]] = {}  # lower_name -> (canonical, category)
    for r in skill_records:
        nm = r["skill_name"][:80].strip()
        if nm:
            unique_skills[nm.lower()] = (nm, r.get("skill_category", "tool"))

    if not unique_skills:
        return 0

    # Insertar skills nuevas con nombre canonico exacto
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO skills (name, category) VALUES %s ON CONFLICT (name) DO NOTHING",
        list(unique_skills.values()),
    )

    # Resolver IDs: exact-first para evitar aliases historicos no canonicos
    skill_id_map = _resolve_skill_id_map(cur, unique_skills)

    # Construir vinculos
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
