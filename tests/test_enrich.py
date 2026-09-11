"""
test_enrich.py — Tests 1-17 de §57.6.10 (notes/PROJECT_MASTER_CONTEXT.md) para scripts/enrich.py.

FASE B:
  - Paso 2 (tests 6-14): capa de cálculo puro. Reglas conservadoras de `remote` y `role_category`.
  - Paso 3 (tests 1-5 skills + 15-17 transversales): capa de persistencia `enrich_jobs()`.

Los tests de persistencia ejecutan el SQL REAL de producción (`enrich_jobs` +
`upsert_skills_and_links` + `_resolve_skill_id_map`) contra SQLite en memoria, con una capa de
traducción de dialecto PostgreSQL→SQLite que vive SOLO en este archivo (mismo enfoque que
`tests/test_load_upsert.py` para DQ-14). No hay una segunda implementación de la lógica: solo se
traducen los marcadores de psycopg2 (`%s`, `= ANY(%s)`, `VALUES %s`).

No conecta a Supabase. No usa Ollama. No hace HTTP.
"""

import re
import sqlite3
from unittest.mock import patch

import pytest

from scripts.enrich import (
    compute_remote_update,
    compute_role_category_update,
    enrich_jobs,
)


# =============================================================================
# Infraestructura SQLite — traducción de dialecto PG→SQLite (SOLO tests)
# =============================================================================


def _translate_pg_to_sqlite(sql: str, params):
    """
    Traduce los marcadores de psycopg2 a los de sqlite3 SIN alterar la lógica SQL.

      - `= ANY(%s)`  → `IN (?, ?, ...)` expandiendo la lista de ese parámetro.
      - `%s`         → `?` posicional.
      - Sin `%s`     → el SQL ya trae `?` (p. ej. tras _fake_execute_values); pasa tal cual.

    `ON CONFLICT ... DO NOTHING`, `COALESCE`, `LOWER()` y `IN` los soporta SQLite ≥ 3.24.
    """
    if params is None:
        return sql.replace("%s", "?"), ()
    if "%s" not in sql:
        return sql, list(params)

    params = list(params)
    parts, out_params, idx = [], [], 0
    for token in re.split(r"(=\s*ANY\(%s\)|%s)", sql):
        if re.fullmatch(r"=\s*ANY\(%s\)", token):
            seq = list(params[idx]); idx += 1
            placeholders = ", ".join("?" for _ in seq) or "NULL"
            parts.append(f"IN ({placeholders})")
            out_params.extend(seq)
        elif token == "%s":
            parts.append("?")
            out_params.append(params[idx]); idx += 1
        else:
            parts.append(token)
    return "".join(parts), out_params


class _PGCursor:
    """Cursor mínimo con la API que usan enrich_jobs() y upsert_skills_and_links()."""

    def __init__(self, sqlite_cur):
        self._c = sqlite_cur

    def execute(self, sql, params=None):
        translated_sql, translated_params = _translate_pg_to_sqlite(sql, params)
        self._c.execute(translated_sql, translated_params)
        return self

    def fetchall(self):
        return self._c.fetchall()

    def fetchone(self):
        return self._c.fetchone()

    @property
    def rowcount(self):
        return self._c.rowcount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _PGConn:
    """Conexión sqlite con la API mínima (cursor/commit/rollback) que espera enrich_jobs()."""

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn

    def cursor(self):
        return _PGCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def raw(self):
        """Conexión sqlite cruda — solo para los asserts del test."""
        return self._conn


def _fake_execute_values(cur, sql, argslist, page_size=None, template=None):
    """
    Sustituto de psycopg2.extras.execute_values para SQLite.

    Expande `VALUES %s` a `VALUES (?, ?), (?, ?), ...` con un grupo por fila, aplana los
    parámetros y delega en cur.execute(). El resto del SQL (incluido `ON CONFLICT ... DO
    NOTHING`) pasa sin cambios.
    """
    rows = list(argslist)
    if not rows:
        return
    width = len(rows[0])
    group = "(" + ", ".join("?" for _ in range(width)) + ")"
    values_clause = ", ".join(group for _ in rows)
    sql_sqlite = sql.replace("VALUES %s", "VALUES " + values_clause)
    flat = [value for row in rows for value in row]
    cur.execute(sql_sqlite, flat)


_SCHEMA = """
    CREATE TABLE jobs (
        id                INTEGER PRIMARY KEY,
        title             TEXT,
        company           TEXT,
        location_display  TEXT,
        description_short  TEXT,
        description_full   TEXT,
        remote             INTEGER,          -- BOOLEAN de PG → 0 / 1 / NULL en SQLite
        role_category      TEXT,
        is_active          INTEGER NOT NULL DEFAULT 1,
        salary_min         INTEGER
    );
    CREATE TABLE skills (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        name     TEXT NOT NULL UNIQUE,
        category TEXT
    );
    CREATE TABLE job_skills (
        job_id   INTEGER NOT NULL,
        skill_id INTEGER NOT NULL,
        PRIMARY KEY (job_id, skill_id)
    );
"""


def _new_conn() -> _PGConn:
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    raw.executescript(_SCHEMA)
    return _PGConn(raw)


def _insert_job(conn: _PGConn, **fields) -> int:
    fields.setdefault("id", 1)
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.raw().execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))
    conn.raw().commit()
    return fields["id"]


def _insert_skill(conn: _PGConn, skill_id: int, name: str, category: str = "tool") -> int:
    conn.raw().execute(
        "INSERT INTO skills (id, name, category) VALUES (?, ?, ?)", (skill_id, name, category)
    )
    conn.raw().commit()
    return skill_id


def _link(conn: _PGConn, job_id: int, skill_id: int) -> None:
    conn.raw().execute(
        "INSERT INTO job_skills (job_id, skill_id) VALUES (?, ?)", (job_id, skill_id)
    )
    conn.raw().commit()


def _job_row(conn: _PGConn, job_id: int) -> sqlite3.Row:
    return conn.raw().execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def _skill_names(conn: _PGConn, job_id: int) -> set[str]:
    rows = conn.raw().execute(
        "SELECT s.name FROM job_skills js JOIN skills s ON s.id = js.skill_id WHERE js.job_id = ?",
        (job_id,),
    ).fetchall()
    return {r["name"] for r in rows}


def _dump(conn: _PGConn) -> dict:
    """Snapshot completo de las tres tablas, para comparar estados antes/después."""
    raw = conn.raw()
    return {
        "jobs": raw.execute("SELECT * FROM jobs ORDER BY id").fetchall(),
        "skills": raw.execute("SELECT id, name, category FROM skills ORDER BY id").fetchall(),
        "job_skills": raw.execute(
            "SELECT job_id, skill_id FROM job_skills ORDER BY job_id, skill_id"
        ).fetchall(),
    }


def _run_enrich(conn: _PGConn, job_ids):
    """enrich_jobs() con execute_values parcheado para SQLite. Devuelve el dict de stats."""
    with patch("scripts.load.psycopg2.extras.execute_values", _fake_execute_values):
        return enrich_jobs(conn, job_ids)


# Texto largo real (>15 chars por línea para que _clean_description_text lo conserve) que
# menciona tecnologías inequívocas del catálogo.
_DESC_FULL_KUBERNETES = (
    "We are hiring a platform engineer to run our Kubernetes clusters in production. "
    "You will work with Docker containers and Terraform for infrastructure as code."
)


# =============================================================================
# enrich_jobs() — degradación segura (contrato de §57.6.11 Paso 3, puntos 3 y 9)
# =============================================================================


def test_enrich_jobs_lista_vacia_es_noop():
    """`enrich_jobs(conn, [])` no hace nada y no falla."""
    conn = _new_conn()
    _insert_job(conn, id=1, title="Dev", description_full=_DESC_FULL_KUBERNETES,
                remote=None, role_category="other")
    antes = _dump(conn)
    stats = _run_enrich(conn, [])
    assert stats == {"jobs_seen": 0, "skills_links_attempted": 0, "remote_updated": 0,
                     "role_category_updated": 0}
    assert [tuple(r) for r in _dump(conn)["jobs"]] == [tuple(r) for r in antes["jobs"]]


def test_enrich_jobs_ids_inexistentes_se_ignoran():
    """IDs que no existen en `jobs` se ignoran sin error; los válidos del mismo lote sí se procesan."""
    conn = _new_conn()
    _insert_job(conn, id=5, title="Platform Engineer",
                description_full=_DESC_FULL_KUBERNETES, remote=None, role_category="devops")
    stats = _run_enrich(conn, [5, 999_999, 888_888])
    assert stats["jobs_seen"] == 1
    assert _skill_names(conn, 5) == {"Kubernetes", "Terraform", "Docker"}


def test_enrich_jobs_error_a_mitad_de_lote_no_deja_persistencia_parcial():
    """
    Toda la llamada es UNA transacción: `conn.commit()` es la última sentencia. Si algo lanza
    antes (aquí: `upsert_skills_and_links`), la excepción propaga SIN que `enrich_jobs()` haga
    ningún commit ni rollback por sí misma. El `conn.rollback()` de más abajo lo ejecuta ESTE
    TEST haciendo de caller (exactamente el contrato: "el caller es responsable de hacer
    rollback antes de reutilizar `conn`") — no es parte de `enrich_jobs()`. Tras ese rollback,
    la BD queda como estaba: nada de los UPDATE de remote/role_category que ya se habían
    ejecutado en el cursor llega a persistirse (§57.6.11/§57.6.14, semántica transaccional).

    Límite conocido de este test: SQLite no reproduce el estado "transacción abortada"
    (`InFailedSqlTransaction`) que PostgreSQL impone tras ciertos errores SQL, en el que
    cualquier sentencia posterior falla hasta hacer rollback. Aquí solo se demuestra la parte
    que SÍ es igual en ambos motores: sin commit, sin rollback explícito no hay persistencia,
    y tras el rollback la conexión vuelve a ser usable.
    """
    conn = _new_conn()
    _insert_job(conn, id=60, title="Backend Developer",
                description_full=_DESC_FULL_KUBERNETES + " Fully remote role.",
                remote=None, role_category="other")

    boom = RuntimeError("fallo simulado de persistencia de skills")
    with patch("scripts.enrich.upsert_skills_and_links", side_effect=boom):
        with pytest.raises(RuntimeError):
            _run_enrich(conn, [60])
    conn.rollback()  # el caller (Paso 4/7) es responsable del rollback ante error

    fila = _job_row(conn, 60)
    assert fila["remote"] is None            # el UPDATE no se persistió
    assert fila["role_category"] == "other"  # el UPDATE no se persistió
    assert _skill_names(conn, 60) == set()


# =============================================================================
# Remote (tests 6-10 de §57.6.10)
# =============================================================================


def test_06_remote_null_con_senal_positiva_da_true():
    """6. NULL + señal positiva en el texto → TRUE."""
    resultado = compute_remote_update(
        current_remote=None,
        title="Backend Developer",
        description_full="This is a fully remote position, work from home.",
    )
    assert resultado is True


def test_07_remote_null_con_senal_negativa_da_false():
    """7. NULL + señal negativa → FALSE."""
    resultado = compute_remote_update(
        current_remote=None,
        title="Backend Developer",
        description_full="This is an on-site position, presencial required.",
    )
    assert resultado is False


def test_08_remote_null_sin_senal_sigue_null():
    """8. NULL + sin señal → sigue NULL."""
    resultado = compute_remote_update(
        current_remote=None,
        title="Backend Developer",
        description_full="We are looking for a great developer to join our team.",
    )
    assert resultado is None


def test_09_remote_true_no_se_toca_aunque_recalculo_de_negativo():
    """9. TRUE existente NO se toca aunque el recálculo diera NULL o FALSE.

    El texto contiene una señal explícita de presencial (recalcularía a FALSE),
    pero como ya había un valor, compute_remote_update no debe tocarlo.
    """
    resultado = compute_remote_update(
        current_remote=True,
        title="Backend Developer",
        description_full="This is an on-site position, presencial required.",
    )
    assert resultado is None


def test_10_remote_false_no_se_toca_aunque_recalculo_de_positivo():
    """10. FALSE existente NO se toca aunque el recálculo diera TRUE.

    El texto contiene una señal explícita de remoto (recalcularía a TRUE),
    pero como ya había un valor, compute_remote_update no debe tocarlo.
    """
    resultado = compute_remote_update(
        current_remote=False,
        title="Backend Developer",
        description_full="This is a fully remote position, work from home.",
    )
    assert resultado is None


# =============================================================================
# Role category (tests 11-14 de §57.6.10)
# =============================================================================


def test_11_role_category_tecnica_valida_nunca_se_pisa():
    """11. Una categoría técnica válida existente NUNCA se pisa."""
    resultado = compute_role_category_update(
        current_role_category="backend",
        title="Frontend Developer",  # el titulo sugeriria otra categoria distinta
        description_full="We build UI components with modern frameworks.",
    )
    assert resultado is None


def test_12_role_category_other_puede_mejorar_a_tecnica_valida():
    """12. 'other' puede mejorar a una categoría técnica válida."""
    resultado = compute_role_category_update(
        current_role_category="other",
        title="Backend Developer",
        description_full="Building and maintaining our REST APIs.",
    )
    assert resultado == "backend"


def test_13_role_category_null_nunca_se_toca_convivencia_pipeline_c():
    """
    13. NULL NUNCA se toca — protege explícitamente el marcado de revisión manual
    de Pipeline C (test dedicado a la convivencia, no solo una variante del anterior).

    Pipeline C deja role_category=NULL cuando Ollama detecta is_tech=False sobre una
    categoría no válida, como marca de "pendiente de revisión manual" (Parte 6, modo
    conservador). Si FASE B rellenara automáticamente ese NULL -aunque el título sugiera
    claramente una categoría técnica-, borraría silenciosamente esa marca de revisión:
    el mismo tipo de error que obligó a restaurar 13 ofertas a mano en julio de 2026.
    """
    resultado = compute_role_category_update(
        current_role_category=None,
        title="Backend Developer",  # señal tecnica inequivoca, e igualmente no debe tocarse
        description_full="Building and maintaining our REST APIs.",
    )
    assert resultado is None


def test_14_role_category_propuesta_no_canonica_se_descarta():
    """
    14. Una categoría propuesta no canónica se descarta silenciosamente
    (no rompe, no escribe basura).

    _classify_role() real solo devuelve None, 'other' o una clave de ROLE_KEYWORDS
    (todas canónicas por construcción), así que este test ejerce directamente la
    guarda de compute_role_category_update() simulando una propuesta inesperada,
    para blindar la validación por si esa garantía cambiara en el futuro.
    """
    with patch("scripts.enrich._classify_role", return_value="not_a_real_category"):
        resultado = compute_role_category_update(
            current_role_category="other",
            title="Backend Developer",
            description_full="Building and maintaining our REST APIs.",
        )
    assert resultado is None



# =============================================================================
# Skills — persistencia real en BD (tests 1-5 de §57.6.10)
# =============================================================================


def test_01_description_full_descubre_skills_que_el_short_no_tenia():
    """
    1. Una `description_full` nueva descubre skills que `description_short` no tenía.

    La oferta llega con un `description_short` sin tecnología nombrada (lo que Pipeline A
    tenía en la ingesta) y ningún vínculo previo. Tras `enrich_jobs()` los `job_skills`
    contienen las skills que solo aparecen en `description_full`.
    """
    conn = _new_conn()
    _insert_job(
        conn,
        id=10,
        title="Platform Engineer",
        description_short="Great team, competitive salary, join us now.",
        description_full=_DESC_FULL_KUBERNETES,
        remote=None,
        role_category="devops",
    )
    assert _skill_names(conn, 10) == set()

    stats = _run_enrich(conn, [10])

    assert _skill_names(conn, 10) == {"Kubernetes", "Terraform", "Docker"}
    assert stats["skills_links_attempted"] == 3


def test_02_no_duplica_vinculos_en_job_skills():
    """2. No se duplican vínculos en `job_skills` al ejecutar dos veces sobre la misma oferta/skill."""
    conn = _new_conn()
    _insert_job(
        conn, id=11, title="Platform Engineer",
        description_full=_DESC_FULL_KUBERNETES, remote=None, role_category="devops",
    )

    _run_enrich(conn, [11])
    _run_enrich(conn, [11])

    n_links = conn.raw().execute(
        "SELECT COUNT(*) AS n FROM job_skills WHERE job_id = 11"
    ).fetchone()["n"]
    assert n_links == 3
    assert _skill_names(conn, 11) == {"Kubernetes", "Terraform", "Docker"}


def test_skills_links_attempted_no_es_insertados_reales():
    """
    `skills_links_attempted` cuenta pares (oferta, skill) ENVIADOS a `upsert_skills_and_links()`,
    no filas nuevas realmente insertadas en `job_skills` — `ON CONFLICT DO NOTHING` puede
    descartarlos en silencio. Prueba directa: en la 2.ª ejecución, sin ningún vínculo nuevo real
    (`job_skills` no cambia), el contador reporta el MISMO valor que en la 1.ª.

    Si esta aserción llegara a fallar por un cambio futuro en `upsert_skills_and_links()`, sería
    señal de que el contador pasó a medir inserciones reales — habría que renombrarlo de vuelta.
    """
    conn = _new_conn()
    _insert_job(
        conn, id=15, title="Platform Engineer",
        description_full=_DESC_FULL_KUBERNETES, remote=None, role_category="devops",
    )

    stats_1 = _run_enrich(conn, [15])
    dump_tras_1a = _dump(conn)["job_skills"]

    stats_2 = _run_enrich(conn, [15])
    dump_tras_2a = _dump(conn)["job_skills"]

    assert stats_1["skills_links_attempted"] == 3
    assert stats_2["skills_links_attempted"] == 3  # mismo "intento", NO cero
    assert dump_tras_2a == dump_tras_1a  # pero job_skills no cambió: cero inserciones reales


def test_03_skills_antiguas_se_conservan_aunque_no_aparezcan_en_el_texto_nuevo():
    """
    3. Las skills antiguas se conservan aunque no aparezcan en el texto enriquecido.

    Test crítico: blinda el caso real medido del 3,7% (una skill correcta detectada del
    `description_short` que el cuerpo largo no repite). La estrategia es ADITIVA — `enrich_jobs()`
    nunca compara para borrar.
    """
    conn = _new_conn()
    _insert_job(
        conn, id=12, title="Platform Engineer",
        description_full=_DESC_FULL_KUBERNETES, remote=None, role_category="devops",
    )
    _insert_skill(conn, 50, "Python", "language")
    _link(conn, 12, 50)  # skill previa que NO aparece en _DESC_FULL_KUBERNETES

    _run_enrich(conn, [12])

    assert "Python" in _skill_names(conn, 12)  # preservada
    assert {"Kubernetes", "Terraform", "Docker"}.issubset(_skill_names(conn, 12))  # añadidas


def test_04_canonicalizacion_alias_resuelve_a_fila_canonica_sin_crear_alias():
    """
    4. Un alias como "react" resuelve a la fila canónica `React`, sin crear una fila nueva.

    Se siembran ambas: `React` (canónica) y una fila alias `react` en minúscula. El texto menciona
    "react"; el catálogo lo canonicaliza a "React" y `_resolve_skill_id_map()` (exact-match-first,
    implementación REAL) enlaza a la fila canónica, no al alias ni a una fila nueva.
    """
    conn = _new_conn()
    _insert_job(
        conn, id=13, title="Frontend Engineer",
        description_full=(
            "Senior engineer working with the React library and Redux for our large "
            "customer facing web application every single day."
        ),
        remote=None, role_category="frontend",
    )
    _insert_skill(conn, 1, "React", "framework")
    _insert_skill(conn, 99, "react", "framework")  # alias histórico no canónico
    skills_antes = conn.raw().execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"]

    _run_enrich(conn, [13])

    link = conn.raw().execute(
        "SELECT skill_id FROM job_skills WHERE job_id = 13"
    ).fetchall()
    assert [r["skill_id"] for r in link] == [1]  # canónica, no el alias 99
    skills_despues = conn.raw().execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"]
    assert skills_despues == skills_antes  # no se creó ninguna fila nueva


def test_05_idempotencia_real_en_bd():
    """
    5. Ejecutar `enrich_jobs()` dos veces seguidas deja la BD EXACTAMENTE igual.

    No es el determinismo de `compute_enrichment()` (eso es la capa pura): aquí se comprueba
    el estado persistido de `jobs`, `skills` y `job_skills` tras la 1.ª y la 2.ª ejecución.
    """
    conn = _new_conn()
    _insert_job(
        conn, id=14, title="Backend Developer",
        description_full=(
            _DESC_FULL_KUBERNETES + " The stack also uses Python and PostgreSQL heavily."
        ),
        remote=None, role_category="other",
    )

    _run_enrich(conn, [14])
    snapshot_1 = _dump(conn)

    _run_enrich(conn, [14])
    snapshot_2 = _dump(conn)

    assert [tuple(r) for r in snapshot_2["jobs"]] == [tuple(r) for r in snapshot_1["jobs"]]
    assert [tuple(r) for r in snapshot_2["skills"]] == [tuple(r) for r in snapshot_1["skills"]]
    assert [tuple(r) for r in snapshot_2["job_skills"]] == [tuple(r) for r in snapshot_1["job_skills"]]


# =============================================================================
# Remote / role_category — persistencia real en BD (refuerzo de 6-14 con estado)
# =============================================================================


def test_persistencia_remote_null_se_actualiza_y_no_null_no_se_toca():
    """`remote NULL` -> se persiste el valor detectado; `remote` ya determinado -> intacto (§57.6.5)."""
    conn = _new_conn()
    _insert_job(conn, id=20, title="Dev",
                description_full="Fully remote position, work from home, distributed team here.",
                remote=None, role_category="backend")
    _insert_job(conn, id=21, title="Dev",
                description_full="This role is 100% on-site and presencial, no remote allowed here.",
                remote=1, role_category="backend")  # ya TRUE: no debe tocarse

    _run_enrich(conn, [20, 21])

    assert _job_row(conn, 20)["remote"] == 1     # NULL -> TRUE
    assert _job_row(conn, 21)["remote"] == 1     # seguía TRUE, sin cambio (recalculo daría FALSE)


def test_persistencia_role_category_solo_other_y_null_protegido():
    """`role_category`: 'other' mejora, NULL y tecnica valida quedan intactos (§57.6.6)."""
    conn = _new_conn()
    _insert_job(conn, id=30, title="Backend Developer",
                description_full="Building and maintaining our REST APIs with Python and PostgreSQL.",
                remote=None, role_category="other")
    _insert_job(conn, id=31, title="Backend Developer",
                description_full="Building and maintaining our REST APIs with Python and PostgreSQL.",
                remote=None, role_category=None)      # NULL: protegido
    _insert_job(conn, id=32, title="Backend Developer",
                description_full="Building and maintaining our REST APIs with Python and PostgreSQL.",
                remote=None, role_category="management")  # tecnica valida: intacta

    _run_enrich(conn, [30, 31, 32])

    assert _job_row(conn, 30)["role_category"] == "backend"      # other -> tecnica
    assert _job_row(conn, 31)["role_category"] is None           # NULL intacto
    assert _job_row(conn, 32)["role_category"] == "management"   # tecnica intacta


# =============================================================================
# Transversales (tests 15-17 de §57.6.10)
# =============================================================================


@pytest.mark.parametrize(
    "escenario, setup",
    [
        (
            "sin description_full",
            dict(id=40, title="Backend Developer", description_full=None,
                 remote=None, role_category="other"),
        ),
        (
            "ya con todo determinado",
            dict(id=41, title="Backend Developer",
                 description_full="Building and maintaining our REST APIs with Kubernetes and Docker.",
                 remote=1, role_category="backend"),
        ),
    ],
)
def test_15_registro_fuera_de_criterio_queda_intacto(escenario, setup):
    """
    15. Un registro fuera de criterio (sin `description_full`, o ya con todo determinado)
    queda intacto tras `enrich_jobs()` — nada cambia, sin errores.
    """
    conn = _new_conn()
    setup.setdefault("description_short", "x")
    setup.setdefault("salary_min", 50000)
    _insert_job(conn, **setup)
    job_id = setup["id"]

    if escenario == "ya con todo determinado":
        _run_enrich(conn, [job_id])  # primera pasada enlaza sus skills

    snapshot_1 = _dump(conn)
    _run_enrich(conn, [job_id])
    snapshot_2 = _dump(conn)

    assert [tuple(r) for r in snapshot_2["jobs"]] == [tuple(r) for r in snapshot_1["jobs"]]
    assert [tuple(r) for r in snapshot_2["job_skills"]] == [tuple(r) for r in snapshot_1["job_skills"]]


@pytest.mark.parametrize(
    "campo, valor",
    [
        ("title", ""),
        ("location_display", None),
        ("description_full", "   \n  \t  "),  # tras _clean_description_text queda vacio
    ],
)
def test_16_datos_incompletos_degradan_sin_excepcion(campo, valor):
    """
    16. Datos incompletos (título vacío, `location_display` NULL, `description_full` que tras
    `_clean_description_text()` queda vacío) — no debe lanzar excepción, degrada con seguridad.
    """
    fields = dict(
        id=42, title="Backend Developer",
        location_display="Berlin",
        description_full="Building REST APIs with Python and PostgreSQL and Docker every day here.",
        remote=None, role_category="other",
    )
    fields[campo] = valor
    conn = _new_conn()
    _insert_job(conn, **fields)

    stats = _run_enrich(conn, [42])  # no debe lanzar

    if campo == "description_full":
        # texto inutilizable -> oferta saltada, nada cambia
        assert stats["skills_links_attempted"] == 0
        assert _job_row(conn, 42)["role_category"] == "other"
    else:
        # titulo vacio o location NULL: se procesa igual, description_full sigue siendo utilizable
        assert stats["jobs_seen"] == 1


def test_17_mismo_resultado_desde_ambos_futuros_consumidores():
    """
    17. Nueva ingestión y backfill usan EXACTAMENTE las mismas reglas de negocio.

    Todavía no existen las integraciones reales (Paso 4 repair_crawl, Paso 7 backfill_enrich).
    Se simulan dos puntos de entrada que ambos llaman a la ÚNICA función pública `enrich_jobs()`
    y se comprueba que el estado persistido es idéntico. La finalidad es arquitectónica: blindar
    que ambos consumidores futuros comparten la misma API, no dos implementaciones.
    """

    def _como_pipeline_b(conn, ids):
        # En el Paso 4, repair_crawl.py llamará a esto tras cada flush de description_full.
        return _run_enrich(conn, ids)

    def _como_backfill(conn, ids):
        # En el Paso 7, backfill_enrich.py llamará a esto por lotes.
        return _run_enrich(conn, ids)

    job_fields = dict(
        id=50, title="Backend Developer",
        description_full=(
            _DESC_FULL_KUBERNETES + " We also use Python, PostgreSQL and a fully remote setup."
        ),
        remote=None, role_category="other",
    )

    conn_a = _new_conn(); _insert_job(conn_a, **job_fields)
    conn_b = _new_conn(); _insert_job(conn_b, **job_fields)

    stats_a = _como_pipeline_b(conn_a, [50])
    stats_b = _como_backfill(conn_b, [50])

    assert stats_a == stats_b
    dump_a, dump_b = _dump(conn_a), _dump(conn_b)
    for tabla in ("jobs", "skills", "job_skills"):
        assert [tuple(r) for r in dump_a[tabla]] == [tuple(r) for r in dump_b[tabla]]
