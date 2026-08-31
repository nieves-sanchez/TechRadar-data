"""
Protege la preservación de description_full en el UPSERT de jobs (load.py).

Bug DQ-14 (confirmado en producción, agosto 2026):
El UPSERT generaba `description_full = EXCLUDED.description_full`. Como Pipeline A
se ejecuta con --no-crawl y envía description_full=NULL, cada re-ingesta borraba el
texto que Pipeline B ya había conseguido por crawling.
Evidencia: ofertas /details/ desde julio 2026 → 90,43% con description_full entre las
no re-ingestadas, frente a solo 66,10% entre las re-ingestadas.

Los casos semánticos se verifican ejecutando el SQL REAL que genera _upsert_jobs()
contra SQLite en memoria, con dos adaptaciones mínimas de dialecto (NOW() y el
marcador VALUES %s de psycopg2). Así se testea la sentencia de producción y no una
reimplementación paralela de la misma lógica.
"""

import sqlite3
from unittest.mock import patch

import pandas as pd

from scripts.load import PRESERVE_ON_NULL, _upsert_jobs

# Columnas mínimas para que el SQL generado sea legible en los tests.
# _upsert_jobs filtra su lista de columnas contra las del DataFrame recibido.
_COLS = ["id", "title", "description_full"]


def _capture_upsert_sql(rows: list[dict]) -> tuple[str, list]:
    """
    Ejecuta _upsert_jobs con un cursor falso y devuelve (sql, filas) sin tocar la BD.

    Args:
        rows: Registros con las claves de _COLS.

    Returns:
        tuple[str, list]: SQL generado y lista de tuplas de valores.
    """
    captured = {}

    def _fake_execute_values(cur, sql, values, page_size=None):
        captured["sql"] = sql
        captured["values"] = values

    df = pd.DataFrame(rows, columns=_COLS)
    with patch("psycopg2.extras.execute_values", _fake_execute_values):
        _upsert_jobs(cur=None, jobs_df=df)

    return captured["sql"], captured["values"]


def _run_upsert_in_sqlite(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """
    Ejecuta el UPSERT real de load.py contra SQLite, adaptando solo el dialecto.

    Adaptaciones necesarias (no alteran la lógica que se está testeando):
      - `VALUES %s`  → marcadores posicionales, porque el `%s` lo expande psycopg2.
      - `NOW()`      → CURRENT_TIMESTAMP, que es el equivalente en SQLite.

    SQLite soporta ON CONFLICT DO UPDATE, EXCLUDED y COALESCE, que es justo lo que
    necesitamos para comprobar la semántica del fix.
    """
    sql, values = _capture_upsert_sql(rows)
    placeholders = "(" + ", ".join("?" for _ in _COLS) + ")"
    sql_sqlite = sql.replace("VALUES %s", f"VALUES {placeholders}").replace(
        "NOW()", "CURRENT_TIMESTAMP"
    )
    conn.executemany(sql_sqlite, values)
    conn.commit()


def _new_db() -> sqlite3.Connection:
    """Crea una tabla jobs mínima con las columnas que intervienen en el UPSERT."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE jobs (
            id               INTEGER PRIMARY KEY,
            title            TEXT,
            description_full TEXT,
            last_seen_at     TEXT,
            is_active        INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    return conn


def _get_description(conn: sqlite3.Connection, job_id: int):
    """Devuelve el description_full almacenado para una oferta."""
    return conn.execute(
        "SELECT description_full FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0]


# =============================================================================
# Nivel SQL — la sentencia generada
# =============================================================================


def test_update_set_protege_description_full_con_coalesce():
    """description_full usa COALESCE para no ser machacado por un NULL entrante."""
    sql, _ = _capture_upsert_sql([{"id": 1, "title": "Dev", "description_full": None}])
    assert "description_full = COALESCE(EXCLUDED.description_full, jobs.description_full)" in sql


def test_resto_de_columnas_siguen_usando_excluded_directo():
    """El fix es acotado: las demás columnas conservan el comportamiento anterior."""
    sql, _ = _capture_upsert_sql([{"id": 1, "title": "Dev", "description_full": None}])
    assert "title = EXCLUDED.title" in sql
    assert "COALESCE(EXCLUDED.title" not in sql


def test_preserve_on_null_solo_cubre_description_full():
    """
    Blinda el alcance del fix de DQ-14.

    Si alguien añade columnas a PRESERVE_ON_NULL debe ser una decisión consciente:
    ampliar la tupla cambia la semántica del UPSERT para esas columnas.
    """
    assert PRESERVE_ON_NULL == ("description_full",)


# =============================================================================
# Nivel semántico — los cuatro casos de DQ-14 sobre SQLite
# =============================================================================


def test_caso1_registro_nuevo_con_description_null():
    """CASO 1: oferta nueva sin descripción → se inserta NULL con normalidad."""
    conn = _new_db()
    _run_upsert_in_sqlite(conn, [{"id": 1, "title": "Dev", "description_full": None}])
    assert _get_description(conn, 1) is None
    conn.close()


def test_caso2_reingesta_con_null_conserva_texto_existente():
    """
    CASO 2 — el bug DQ-14: texto existente + ingesta con NULL → se CONSERVA.

    Es el escenario real: Pipeline B enriquece a las 11:00 UTC y Pipeline A
    re-ingesta la misma oferta al día siguiente a las 06:00 UTC con NULL.
    """
    conn = _new_db()
    _run_upsert_in_sqlite(
        conn, [{"id": 1, "title": "Dev", "description_full": "Texto de Pipeline B"}]
    )
    _run_upsert_in_sqlite(conn, [{"id": 1, "title": "Dev", "description_full": None}])
    assert _get_description(conn, 1) == "Texto de Pipeline B"
    conn.close()


def test_caso3_reingesta_con_texto_rellena_el_null_existente():
    """CASO 3: existente NULL + ingesta con texto → se guarda el texto nuevo."""
    conn = _new_db()
    _run_upsert_in_sqlite(conn, [{"id": 1, "title": "Dev", "description_full": None}])
    _run_upsert_in_sqlite(
        conn, [{"id": 1, "title": "Dev", "description_full": "Texto nuevo"}]
    )
    assert _get_description(conn, 1) == "Texto nuevo"
    conn.close()


def test_caso4_texto_nuevo_sustituye_al_texto_antiguo():
    """
    CASO 4: texto existente + texto nuevo → gana el nuevo.

    Se conserva el comportamiento natural de actualización: el fix solo protege
    frente a NULL, no congela la columna.
    """
    conn = _new_db()
    _run_upsert_in_sqlite(
        conn, [{"id": 1, "title": "Dev", "description_full": "Texto antiguo"}]
    )
    _run_upsert_in_sqlite(
        conn, [{"id": 1, "title": "Dev", "description_full": "Texto actualizado"}]
    )
    assert _get_description(conn, 1) == "Texto actualizado"
    conn.close()


def test_reingesta_sigue_actualizando_el_resto_de_campos():
    """La protección de description_full no bloquea la actualización de otros campos."""
    conn = _new_db()
    _run_upsert_in_sqlite(
        conn, [{"id": 1, "title": "Titulo viejo", "description_full": "Texto de B"}]
    )
    _run_upsert_in_sqlite(
        conn, [{"id": 1, "title": "Titulo nuevo", "description_full": None}]
    )
    title, desc = conn.execute(
        "SELECT title, description_full FROM jobs WHERE id = 1"
    ).fetchone()
    assert title == "Titulo nuevo"
    assert desc == "Texto de B"
    conn.close()
