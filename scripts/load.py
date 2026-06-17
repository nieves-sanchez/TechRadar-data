"""
load.py — Carga de datos en PostgreSQL (Supabase) para TechRadar.

Implementa la estrategia UPSERT incremental sobre la tabla jobs:
  - Ofertas nuevas se insertan con is_active=TRUE
  - Ofertas ya existentes actualizan last_seen_at y sus campos de contenido
  - Si una oferta fue marcada inactiva y reaparece en la API, se reactiva
  - Tras cada carga se marcan como is_active=FALSE las ofertas con
    posted_at anterior a INACTIVE_AFTER_DAYS días

Funciones principales:
    load_jobs(jobs_df, job_skills_df)   → None
    load_eurostat(eurostat_df)          → None

Uso:
    from scripts.load import load_jobs, load_eurostat
"""

import logging
import os

import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

logger = logging.getLogger("techradar.load")

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")

# Número de días tras los cuales una oferta se considera inactiva.
# Debe coincidir con el filtro de las vistas del dashboard (90 días).
INACTIVE_AFTER_DAYS = 90

# Filas por INSERT en execute_values. 500 es un buen balance entre
# memoria y rendimiento para ~30k filas con campos de texto largos.
BATCH_SIZE = 500


# =============================================================================
# Utilidades de conexión y datos
# =============================================================================


def _get_connection() -> psycopg2.extensions.connection:
    """
    Abre y devuelve una conexión a la base de datos.

    Raises:
        EnvironmentError: si DATABASE_URL no está configurada en .env.
        psycopg2.OperationalError: si la conexión falla (credenciales, red...).
    """
    if not DATABASE_URL:
        raise EnvironmentError(
            "Variable de entorno DATABASE_URL no configurada. "
            "Copia .env.example a .env y rellena la connection string de Supabase."
        )
    return psycopg2.connect(DATABASE_URL)


def _clean(value):
    """
    Convierte los tipos NA de pandas a Python None para psycopg2.

    psycopg2 traduce Python None → SQL NULL correctamente.
    Los tipos pd.NA, pd.NaT y float('nan') no son reconocidos por psycopg2
    y causarían errores de tipo o insertarían valores inesperados.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _df_to_rows(df: pd.DataFrame, columns: list[str]) -> list[tuple]:
    """
    Convierte las columnas indicadas de un DataFrame a una lista de tuplas
    lista para pasar a execute_values, aplicando _clean() a cada celda.

    Usa itertuples() en lugar de iterrows() — 5-10x más rápido para DataFrames
    con muchas filas o columnas de texto largo (como description_full).
    """
    return [
        tuple(_clean(v) for v in row)
        for row in df[columns].itertuples(index=False, name=None)
    ]


# =============================================================================
# Operaciones de carga (privadas)
# =============================================================================


def _upsert_jobs(cur, jobs_df: pd.DataFrame) -> int:
    """
    UPSERT de la tabla jobs.

    INSERT si la oferta es nueva. UPDATE si ya existe (ON CONFLICT en la PK id):
      - Actualiza todos los campos de contenido por si algún dato cambió
      - Actualiza last_seen_at = NOW()
      - Reactiva is_active = TRUE si la oferta reaparece tras haber caducado
      - Nunca toca first_seen_at ni ingested_at (preservan la fecha de primera ingesta)
    """
    cols = [
        "id", "source", "title", "company", "location_display", "city",
        "country_code", "role_category",
        "salary_min", "salary_max", "salary_mid", "salary_is_predicted",
        "contract_type", "contract_time", "remote",
        "description_short", "description_full", "url", "posted_at",
    ]
    cols = [c for c in cols if c in jobs_df.columns]
    rows = _df_to_rows(jobs_df, cols)

    # EXCLUDED es una tabla especial de PostgreSQL que contiene los valores
    # propuestos por el INSERT que provocó el conflicto. Así actualizamos
    # cada campo con el nuevo valor sin repetir los datos en la query.
    update_set = ", ".join(f"{col} = EXCLUDED.{col}" for col in cols if col != "id")

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


def _upsert_skills(cur, job_skills_df: pd.DataFrame) -> dict[str, int]:
    """
    UPSERT del catálogo de skills.

    Inserta las skills nuevas del batch. Si ya existen, actualiza su categoría
    por si se hubiera reclasificado alguna. Devuelve un dict {nombre: id}
    con todas las skills del batch para usarlo en _upsert_job_skills.
    """
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

    # Recuperar los IDs asignados por la BD (tanto de skills nuevas como preexistentes)
    names = [row[0] for row in unique_skills]
    cur.execute("SELECT id, name FROM skills WHERE name = ANY(%s)", (names,))
    return {name: sid for sid, name in cur.fetchall()}


def _upsert_job_skills(
    cur,
    job_skills_df: pd.DataFrame,
    skill_id_map: dict[str, int],
) -> int:
    """
    UPSERT de la tabla job_skills (relación M:N entre jobs y skills).

    Vincula cada oferta con sus skills resolviendo los IDs del catálogo.
    ON CONFLICT DO NOTHING porque el par (job_id, skill_id) es la PK
    y no hay nada que actualizar si el vínculo ya existe.
    """
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
    """
    Marca como is_active=FALSE las ofertas publicadas hace más de INACTIVE_AFTER_DAYS días.

    Se ejecuta tras cada UPSERT como paso de mantenimiento.
    Las vistas de tendencias históricas no filtran por is_active, así que estos
    registros siguen siendo visibles para análisis de evolución temporal.
    """
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


# =============================================================================
# Funciones públicas
# =============================================================================


def load_jobs(jobs_df: pd.DataFrame, job_skills_df: pd.DataFrame) -> None:
    """
    Carga las ofertas y sus skills en PostgreSQL dentro de una sola transacción.

    Orden de operaciones:
      1. UPSERT jobs           — inserta nuevas, actualiza existentes
      2. UPSERT skills         — amplía el catálogo con skills nuevas
      3. UPSERT job_skills     — vincula ofertas con sus skills
      4. Mantenimiento         — is_active=FALSE en ofertas > 90 días

    Si cualquier paso falla se hace rollback completo: la BD queda como estaba.

    Args:
        jobs_df:       DataFrame de salida de transform_jobs()[0].
        job_skills_df: DataFrame de salida de transform_jobs()[1].
    """
    if jobs_df.empty:
        logger.warning("load_jobs: jobs_df vacío, nada que cargar.")
        return

    conn = _get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                n_jobs = _upsert_jobs(cur, jobs_df)
                logger.info("UPSERT jobs: %d filas.", n_jobs)

                skill_id_map = _upsert_skills(cur, job_skills_df)
                logger.info("UPSERT skills: %d skills en catálogo.", len(skill_id_map))

                n_links = _upsert_job_skills(cur, job_skills_df, skill_id_map)
                logger.info("UPSERT job_skills: %d vínculos.", n_links)

                n_deactivated = _deactivate_old_jobs(cur)
                logger.info(
                    "Mantenimiento: %d ofertas marcadas como inactivas.", n_deactivated
                )

    except Exception as exc:
        logger.error("load_jobs falló — se hizo rollback completo: %s", exc)
        raise
    finally:
        conn.close()


def load_eurostat(eurostat_df: pd.DataFrame) -> None:
    """
    Carga los datos de Eurostat en labor_market_context.

    ON CONFLICT actualiza el valor por si Eurostat revisara una cifra publicada
    (ocurre ocasionalmente con datos preliminares que se corrigen al año siguiente).

    Args:
        eurostat_df: DataFrame de salida de transform_eurostat().
    """
    if eurostat_df.empty:
        logger.warning("load_eurostat: DataFrame vacío, nada que cargar.")
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
        logger.error("load_eurostat falló — se hizo rollback completo: %s", exc)
        raise
    finally:
        conn.close()
