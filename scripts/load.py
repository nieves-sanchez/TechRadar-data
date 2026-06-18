"""
load.py — Carga de datos en PostgreSQL (Supabase) para TechRadar.

Implementa la estrategia UPSERT incremental sobre la tabla jobs:
  - Ofertas nuevas se insertan con is_active=TRUE
  - Ofertas ya existentes actualizan last_seen_at y sus campos de contenido
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
