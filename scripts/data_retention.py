"""
data_retention.py — Backup y verificación para la retención de Adzuna a 60 días.

Arquitectura aprobada: notes/PROJECT_MASTER_CONTEXT.md Parte 10 (§87-96).
Adzuna pasa a representar solo el mercado reciente (ventana de 60 días por
posted_at, independiente de is_active). Antes de poder borrar nada, este
script exporta un backup lossless de las filas que quedarían fuera de esa
ventana, para poder deshacer el borrado si algo sale mal.

Modos disponibles en este turno (ver notes/PENDING_CHANGES.md para el estado):
    dry-run (por defecto, sin flags) — mide el conjunto candidato, no escribe
        nada, no requiere --dest.
    --backup --dest RUTA — exporta jobs/job_skills/skills/candidate_ids a
        JSONL.GZ + manifest.json bajo RUTA/retention_<timestamp>/.
    --verify RUTA/manifest.json — verifica un backup ya creado usando
        exclusivamente los archivos locales (no requiere Supabase).

NO implementado en este turno, a propósito (ver notas de diseño en
notes/PROJECT_MASTER_CONTEXT.md Parte 10 §96 para el porqué):
    - retention / DELETE real contra Supabase
    - restore real
    - VACUUM
    - política de backups para la retención recurrente (sin decidir todavía)

Este módulo NUNCA ejecuta DELETE, UPDATE, INSERT, ALTER, DROP, TRUNCATE,
VACUUM ni REINDEX. Toda la interacción con Supabase es SELECT / catálogo de
PostgreSQL, dentro de una única transacción con aislamiento REPEATABLE READ
(transaccional, no de sesión — ver `snapshot_transaction()`). No se usa
`conn.set_session(readonly=True)` ni `SET SESSION CHARACTERISTICS ...` ni
`SET default_transaction_read_only` — esa combinación ya contaminó conexiones
reutilizadas del Transaction Pooler de Supabase en el pasado (regla
permanente, PROJECT_MASTER_CONTEXT.md §82).

Cutoff: por defecto se captura con `SELECT NOW()` dentro de la misma
transacción/snapshot (ver `_capture_cutoff()`), no con el reloj local de
Python — misma autoridad temporal que evalúa `posted_at`, sin depender de
que el reloj del PC esté bien sincronizado (auditoría 2026-09-21, tras un
apagado inesperado del equipo). `--cutoff` lo sustituye explícitamente para
reproducibilidad (tests, verificación manual) sin tocar Supabase.

Atomicidad local: `--backup` escribe primero en un directorio marcado como
incompleto (`.retention_<ts>.incomplete`) y solo lo renombra al nombre final
(`retention_<ts>`) tras completar los 4 archivos y el manifiesto (ver
`_atomic_backup_dir()`). Si el proceso muere a mitad (excepción, caída de
conexión, apagado del PC) nunca queda un `retention_<ts>/` a medias que
parezca un backup válido.

Uso:
    python -m scripts.data_retention
    python -m scripts.data_retention --backup --dest "C:\\ruta\\TechRadar-data-backups"
    python -m scripts.data_retention --verify "C:\\ruta\\retention_.../manifest.json"
"""

import argparse
import gzip
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras

from scripts.load import _get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("techradar.data_retention")

RETENTION_DAYS_DEFAULT = 60
ITERSIZE = 2000

# Baseline esperado de columnas de `jobs`, verificado contra el catálogo real
# de PostgreSQL (no contra sql/schema.sql) el 2026-09-21 — ver
# PROJECT_MASTER_CONTEXT.md Parte 10 §96. Se usa SOLO como red de seguridad
# para detectar cambios incompatibles del schema real; el export usa siempre
# las columnas REALES introspeccionadas en tiempo de ejecución, nunca esta
# lista como subset fijo (ver validated_jobs_columns()).
EXPECTED_JOBS_COLUMNS = {
    "id": "bigint",
    "source": "character varying",
    "title": "character varying",
    "company": "character varying",
    "location_display": "character varying",
    "city": "character varying",
    "country_code": "character varying",
    "role_category": "character varying",
    "salary_min": "integer",
    "salary_max": "integer",
    "salary_mid": "integer",
    "salary_is_predicted": "boolean",
    "contract_type": "character varying",
    "contract_time": "character varying",
    "remote": "boolean",
    "description_short": "text",
    "description_full": "text",
    "url": "text",
    "is_active": "boolean",
    "first_seen_at": "timestamp with time zone",
    "last_seen_at": "timestamp with time zone",
    "posted_at": "timestamp with time zone",
    "ingested_at": "timestamp with time zone",
}

JOB_SKILLS_COLUMNS = ["job_id", "skill_id"]
SKILLS_COLUMNS = ["id", "name", "category"]
CANDIDATE_IDS_COLUMNS = ["id"]

# =============================================================================
# SQL — todo vive aquí, en constantes con nombre, nunca como texto disperso
# por el código. Solo SELECT y control de transacción. El test
# test_no_forbidden_keywords_in_sql_constants (tests/test_data_retention.py)
# escanea estas constantes y falla si aparece DELETE/UPDATE/ALTER/etc.
# =============================================================================

SET_ISOLATION_SQL = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
SET_TIMEZONE_SQL = "SET LOCAL TIME ZONE 'UTC'"
NOW_QUERY = "SELECT NOW()"

COLUMNS_INTROSPECTION_QUERY = """
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = %s
    ORDER BY ordinal_position
"""

# {cols} se rellena con nombres de columna reales (nunca con entrada externa)
JOBS_QUERY_TEMPLATE = "SELECT {cols} FROM jobs WHERE posted_at < %s ORDER BY id"

CANDIDATE_IDS_QUERY = "SELECT id FROM jobs WHERE posted_at < %s ORDER BY id"

JOB_SKILLS_QUERY = """
    SELECT job_skills.job_id, job_skills.skill_id
    FROM job_skills
    JOIN jobs ON jobs.id = job_skills.job_id
    WHERE jobs.posted_at < %s
    ORDER BY job_skills.job_id, job_skills.skill_id
"""

SKILLS_QUERY = "SELECT id, name, category FROM skills ORDER BY id"

CANDIDATE_STATS_QUERY = """
    SELECT
        COUNT(*)                            AS jobs_count,
        MIN(id)                             AS id_min,
        MAX(id)                             AS id_max,
        MIN(posted_at)                      AS posted_at_min,
        MAX(posted_at)                      AS posted_at_max,
        SUM(pg_column_size(jobs.*))         AS bytes_logicos_estimado
    FROM jobs
    WHERE posted_at < %s
"""

JOB_SKILLS_COUNT_QUERY = """
    SELECT COUNT(*) AS n
    FROM job_skills js
    JOIN jobs j ON j.id = js.job_id
    WHERE j.posted_at < %s
"""

SKILLS_COUNT_QUERY = "SELECT COUNT(*) AS n FROM skills"


class SchemaMismatchError(RuntimeError):
    """El schema real de `jobs` cambió de forma incompatible con lo esperado."""


# =============================================================================
# Serialización / canonicalización — sin BD, testeable con datos en memoria
# =============================================================================


def _json_value(value):
    """
    Convierte un valor de fila de Postgres a un tipo JSON nativo.

    Deliberadamente NO usa `default=str`: cualquier tipo que no sepamos
    manejar aquí debe fallar de forma visible (el schema pudo cambiar de un
    modo que no esperábamos), no convertirse en texto en silencio.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(
        f"Tipo no soportado en la canonicalización del backup: "
        f"{type(value).__name__} ({value!r}). Revisa si el schema de origen "
        "cambió de forma inesperada antes de continuar."
    )


def canonical_line(row: dict) -> str:
    """
    Línea JSON canónica de una fila: claves ordenadas alfabéticamente,
    separadores compactos, UTF-8 literal (sin escapar a \\uXXXX).

    Mismo contenido lógico -> misma línea, siempre, con independencia del
    orden en que se construyó el diccionario de entrada.
    """
    return json.dumps(
        {k: _json_value(v) for k, v in row.items()},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def export_rows_to_jsonl_gz(rows, columns: list[str], path: Path) -> dict:
    """
    Escribe `rows` (iterable de tuplas, en el orden de `columns`) a un
    archivo JSONL.GZ en `path`, fila a fila, sin acumular el resultado
    completo en memoria — compatible con un cursor con nombre que va
    entregando lotes desde Postgres, o con una lista en memoria en tests.

    El orden de `rows` es responsabilidad de quien las genera (ORDER BY en
    SQL) — esta función no reordena, precisamente para poder trabajar en
    streaming sin materializar el conjunto completo.

    Devuelve count, tamaños y DOS hashes distintos:
      - sha256_file: sobre los bytes finales .gz — sirve para comprobar
        integridad de copia (Desktop vs. OneDrive) y corrupción física.
      - sha256_content: sobre el contenido lógico SIN comprimir (las líneas
        JSONL canónicas, en el mismo orden que se escriben) — sirve para
        verificar que el DATO es el mismo con independencia del gzip.

    El gzip se escribe con mtime=0 y filename="" para que sea determinista:
    el mismo contenido produce siempre los mismos bytes .gz, con
    independencia de la hora de ejecución o del nombre de archivo elegido
    (por defecto, gzip incrusta ambos en la cabecera — sin esto, dos backups
    con el mismo contenido lógico tendrían sha256_file distintos, lo que
    rompería la promesa de "mismo contenido -> mismo hash").
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content_hash = hashlib.sha256()
    count = 0
    bytes_raw = 0
    with open(path, "wb") as raw_file:
        with gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=raw_file) as gz:
            for row in rows:
                record = dict(zip(columns, row))
                data = (canonical_line(record) + "\n").encode("utf-8")
                gz.write(data)
                content_hash.update(data)
                bytes_raw += len(data)
                count += 1
    bytes_gz = path.stat().st_size
    sha256_file = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "count": count,
        "bytes_raw": bytes_raw,
        "bytes_gz": bytes_gz,
        "sha256_file": sha256_file,
        "sha256_content": content_hash.hexdigest(),
    }


# =============================================================================
# Schema real — autoridad: PostgreSQL, nunca sql/schema.sql
# =============================================================================


def introspect_columns(cur, table: str) -> list[tuple[str, str]]:
    cur.execute(COLUMNS_INTROSPECTION_QUERY, (table,))
    return cur.fetchall()


def validated_jobs_columns(cur) -> tuple[list[str], list[str]]:
    """
    Introspecciona las columnas REALES de `jobs` y las valida contra
    EXPECTED_JOBS_COLUMNS.

    Devuelve (columnas_a_exportar, avisos). `columnas_a_exportar` son
    SIEMPRE las columnas reales completas, en su orden real — nunca un
    subset fijo, por eso "exportar todas las columnas" se cumple aunque el
    baseline se quede desactualizado.

    Lanza SchemaMismatchError (y por tanto aborta el backup) si falta una
    columna esperada, o si una columna esperada cambió de tipo — eso sí es
    un cambio incompatible real. Una columna REAL nueva que no estaba en el
    baseline NO es un error: se exporta igualmente y se devuelve como aviso.
    """
    real = introspect_columns(cur, "jobs")
    if not real:
        raise SchemaMismatchError("La tabla jobs no devolvió ninguna columna real.")
    real_types = {name: dtype for name, dtype in real}

    missing = [c for c in EXPECTED_JOBS_COLUMNS if c not in real_types]
    if missing:
        raise SchemaMismatchError(
            f"Columnas esperadas ausentes en jobs: {missing}. "
            "El schema real cambió de forma incompatible — no se genera el backup."
        )

    mismatched = [
        f"{c}: esperado {EXPECTED_JOBS_COLUMNS[c]!r}, real {real_types[c]!r}"
        for c in EXPECTED_JOBS_COLUMNS
        if real_types[c] != EXPECTED_JOBS_COLUMNS[c]
    ]
    if mismatched:
        raise SchemaMismatchError(
            "Columnas de jobs con tipo incompatible respecto al baseline esperado: "
            + "; ".join(mismatched)
        )

    warnings = []
    extra = [name for name, _ in real if name not in EXPECTED_JOBS_COLUMNS]
    if extra:
        warnings.append(
            f"jobs tiene {len(extra)} columna(s) nueva(s) no contempladas en el "
            f"baseline (se exportan igualmente, sin excluir nada): {extra}"
        )

    columns = [name for name, _ in real]
    return columns, warnings


# =============================================================================
# Snapshot transaccional — REPEATABLE READ transaccional, nunca de sesión
# =============================================================================


@contextmanager
def snapshot_transaction(conn):
    """
    Abre una única transacción con aislamiento REPEATABLE READ, acotado a
    ESA transacción (`SET TRANSACTION`, no `SET SESSION`) — todas las
    consultas dentro ven exactamente el mismo snapshot lógico de la BD, con
    independencia de que Pipeline A siga insertando/actualizando jobs en
    paralelo. `SET LOCAL TIME ZONE 'UTC'` por el mismo motivo: acotado a la
    transacción, para que los timestamptz se sirvan en UTC sin tocar nada a
    nivel de sesión.

    Nunca hace commit (este módulo no escribe nada) — siempre rollback al
    salir, se complete con éxito o no.

    Deliberadamente NO usa `conn.set_session(readonly=True)` ni
    `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` ni
    `SET default_transaction_read_only` — prohibido por la regla permanente
    de Supavisor (PROJECT_MASTER_CONTEXT.md §82): esas instrucciones
    contaminan conexiones reutilizadas del Transaction Pooler.
    """
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(SET_ISOLATION_SQL)
        cur.execute(SET_TIMEZONE_SQL)
    try:
        yield conn
    finally:
        conn.rollback()


def _stream_query(conn, name: str, sql: str, params):
    """
    Cursor con nombre (server-side) dentro de la transacción actual:
    Postgres entrega los resultados en lotes de ITERSIZE en vez de mandarlos
    todos de golpe, evitando cargar el resultado completo en memoria del
    lado Python. Requiere estar dentro de una transacción sin autocommit
    (lo garantiza snapshot_transaction).
    """
    cur = conn.cursor(name=name)
    cur.itersize = ITERSIZE
    cur.execute(sql, params)
    return cur


def _capture_cutoff(conn, override: datetime | None) -> datetime:
    """
    Cutoff del backup/dry-run: por defecto `SELECT NOW()` dentro de la
    transacción/snapshot ya abierta (misma autoridad temporal que evalúa
    `posted_at`, sin depender del reloj local del PC). `override` (viene de
    `--cutoff`) lo sustituye explícitamente sin tocar Supabase — pensado
    para reproducibilidad en tests o verificación manual.

    Debe llamarse ya dentro de `snapshot_transaction()` (aislamiento y
    huso horario UTC ya fijados para esta transacción) para que el valor
    devuelto por Postgres esté en UTC.
    """
    if override is not None:
        return override
    with conn.cursor() as cur:
        cur.execute(NOW_QUERY)
        return cur.fetchone()[0]


@contextmanager
def _atomic_backup_dir(dest: Path, ts: str):
    """
    Directorio de salida del backup, publicado de forma atómica.

    Expone un directorio temporal marcado como incompleto
    (`.retention_<ts>.incomplete`) y solo lo renombra al nombre final
    (`retention_<ts>`) si el bloque `with` termina sin excepción. Si el
    bloque lanza (conexión perdida, disco lleno, excepción cualquiera),
    borra el directorio temporal (best-effort) y propaga la excepción —
    nunca deja un `retention_<ts>/` con archivos parciales que parezca un
    backup válido. Si el proceso muere de golpe (apagado del PC) antes de
    que el `except` pueda ejecutarse, el directorio temporal sigue
    existiendo pero queda inequívocamente marcado por su nombre
    (`.incomplete`) y por no haber sido renombrado al nombre final.
    """
    final_dir = dest / f"retention_{ts}"
    tmp_dir = dest / f".retention_{ts}.incomplete"
    if final_dir.exists():
        raise FileExistsError(f"Ya existe un backup en {final_dir}; no se sobrescribe.")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        yield tmp_dir
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    tmp_dir.rename(final_dir)


# =============================================================================
# Manifiesto
# =============================================================================


def _utc_compact(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _repo_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).resolve().parent.parent,
        )
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 — informativo, nunca debe romper el backup
        return "unknown"


def build_manifest(
    *,
    cutoff: datetime,
    retention_days: int,
    jobs_columns: list[str],
    stats: dict,
    file_paths: dict[str, Path],
    file_results: dict[str, dict],
) -> dict:
    """
    Construye el manifiesto. No incluye secrets, no incluye DATABASE_URL, no
    incrusta la lista de IDs (eso vive en candidate_ids_<ts>.jsonl.gz).
    """
    return {
        "format_version": "1.0",
        "created_at_utc": cutoff.isoformat(),
        "cutoff_timestamp": cutoff.isoformat(),
        "retention_days": retention_days,
        "select_condition": "posted_at < cutoff_timestamp - retention_days (días)",
        "repo_head": _repo_head(),
        "jobs_count": file_results["jobs"]["count"],
        "job_skills_count": file_results["job_skills"]["count"],
        "skills_count": file_results["skills_snapshot"]["count"],
        "candidate_ids_count": file_results["candidate_ids"]["count"],
        "jobs_id_min": stats["id_min"],
        "jobs_id_max": stats["id_max"],
        "posted_at_min": _json_value(stats["posted_at_min"]),
        "posted_at_max": _json_value(stats["posted_at_max"]),
        "jobs_columns": jobs_columns,
        "job_skills_columns": JOB_SKILLS_COLUMNS,
        "skills_columns": SKILLS_COLUMNS,
        "candidate_ids_columns": CANDIDATE_IDS_COLUMNS,
        "files": {
            key: {
                "filename": file_paths[key].name,
                "bytes_raw": res["bytes_raw"],
                "bytes_gz": res["bytes_gz"],
                "sha256_file": res["sha256_file"],
                "sha256_content": res["sha256_content"],
            }
            for key, res in file_results.items()
        },
    }


# =============================================================================
# dry-run
# =============================================================================


def dry_run_report(
    conn, retention_days: int, dest: str | None, cutoff_override: datetime | None = None
) -> dict:
    """
    Mide el conjunto candidato SIN escribir nada — ni archivos, ni Supabase.
    Devuelve el mismo dict de estadísticas que usa build_manifest, para que
    quien llame pueda imprimirlo o testearlo.
    """
    with snapshot_transaction(conn):
        cutoff = _capture_cutoff(conn, cutoff_override)
        boundary = cutoff - timedelta(days=retention_days)
        ts = _utc_compact(cutoff)
        out_dir = Path(dest or "<DEST>") / f"retention_{ts}"

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(CANDIDATE_STATS_QUERY, (boundary,))
            stats = cur.fetchone()
            cur.execute(JOB_SKILLS_COUNT_QUERY, (boundary,))
            job_skills_count = cur.fetchone()["n"]
            cur.execute(SKILLS_COUNT_QUERY)
            skills_count = cur.fetchone()["n"]

    print("=" * 78)
    print("DRY-RUN — data_retention.py (nada escrito, ni en disco ni en Supabase)")
    print("=" * 78)
    print(f"cutoff (UTC, capturado una sola vez): {cutoff.isoformat()}")
    print(f"ventana de retención: {retention_days} días")
    print(f"fecha límite (cutoff - {retention_days}d): {boundary.isoformat()}")
    print(f"jobs candidatos (posted_at < límite):  {stats['jobs_count']}")
    print(f"job_skills asociados:                  {job_skills_count}")
    print(f"skills en el catálogo (snapshot completo): {skills_count}")
    print(f"id min/max de jobs candidatos: {stats['id_min']} / {stats['id_max']}")
    print(f"posted_at min/max del candidato: {stats['posted_at_min']} / {stats['posted_at_max']}")
    bytes_est = stats["bytes_logicos_estimado"] or 0
    print(f"tamaño lógico estimado de jobs (pg_column_size): ~{bytes_est / 1_048_576:.1f} MB")
    print(f"directorio que se usaría con --backup: {out_dir}")
    print(f"  {out_dir / f'jobs_{ts}.jsonl.gz'}")
    print(f"  {out_dir / f'job_skills_{ts}.jsonl.gz'}")
    print(f"  {out_dir / f'skills_snapshot_{ts}.jsonl.gz'}")
    print(f"  {out_dir / f'candidate_ids_{ts}.jsonl.gz'}")
    print(f"  {out_dir / 'manifest.json'}")
    print("=" * 78)

    return {
        "jobs_count": stats["jobs_count"],
        "job_skills_count": job_skills_count,
        "skills_count": skills_count,
        "id_min": stats["id_min"],
        "id_max": stats["id_max"],
        "posted_at_min": stats["posted_at_min"],
        "posted_at_max": stats["posted_at_max"],
        "bytes_logicos_estimado": bytes_est,
    }


# =============================================================================
# backup
# =============================================================================


def run_backup(
    conn, retention_days: int, dest: str, cutoff_override: datetime | None = None
) -> Path:
    """
    Exporta jobs/job_skills/skills/candidate_ids a JSONL.GZ + manifest.json,
    todo desde el MISMO snapshot transaccional (REPEATABLE READ). Requiere
    `--backup` explícito en el CLI — nunca se llama por accidente desde el
    modo por defecto.

    Publica el resultado de forma atómica (`_atomic_backup_dir`): si algo
    falla a mitad de la exportación, el `retention_<ts>/` final nunca llega
    a existir a medias.
    """
    file_results: dict[str, dict] = {}
    file_paths: dict[str, Path] = {}

    with snapshot_transaction(conn):
        cutoff = _capture_cutoff(conn, cutoff_override)
        boundary = cutoff - timedelta(days=retention_days)
        ts = _utc_compact(cutoff)

        with _atomic_backup_dir(Path(dest), ts) as out_dir:
            with conn.cursor() as cur:
                columns, warnings = validated_jobs_columns(cur)
            for w in warnings:
                logger.warning(w)

            logger.info("Exportando jobs (%d columnas reales)...", len(columns))
            path = out_dir / f"jobs_{ts}.jsonl.gz"
            jobs_cur = _stream_query(
                conn,
                "export_jobs",
                JOBS_QUERY_TEMPLATE.format(cols=", ".join(columns)),
                (boundary,),
            )
            try:
                file_results["jobs"] = export_rows_to_jsonl_gz(jobs_cur, columns, path)
            finally:
                jobs_cur.close()
            file_paths["jobs"] = path
            logger.info("jobs: %d filas exportadas.", file_results["jobs"]["count"])

            logger.info("Exportando job_skills...")
            path = out_dir / f"job_skills_{ts}.jsonl.gz"
            js_cur = _stream_query(conn, "export_job_skills", JOB_SKILLS_QUERY, (boundary,))
            try:
                file_results["job_skills"] = export_rows_to_jsonl_gz(
                    js_cur, JOB_SKILLS_COLUMNS, path
                )
            finally:
                js_cur.close()
            file_paths["job_skills"] = path
            logger.info("job_skills: %d filas exportadas.", file_results["job_skills"]["count"])

            logger.info(
                "Exportando snapshot completo de skills (referencia, no se restaura sola)..."
            )
            path = out_dir / f"skills_snapshot_{ts}.jsonl.gz"
            sk_cur = _stream_query(conn, "export_skills", SKILLS_QUERY, None)
            try:
                file_results["skills_snapshot"] = export_rows_to_jsonl_gz(
                    sk_cur, SKILLS_COLUMNS, path
                )
            finally:
                sk_cur.close()
            file_paths["skills_snapshot"] = path
            logger.info(
                "skills_snapshot: %d filas exportadas.", file_results["skills_snapshot"]["count"]
            )

            logger.info("Exportando candidate_ids (autoridad del futuro DELETE)...")
            path = out_dir / f"candidate_ids_{ts}.jsonl.gz"
            cid_cur = _stream_query(conn, "export_candidate_ids", CANDIDATE_IDS_QUERY, (boundary,))
            try:
                file_results["candidate_ids"] = export_rows_to_jsonl_gz(
                    cid_cur, CANDIDATE_IDS_COLUMNS, path
                )
            finally:
                cid_cur.close()
            file_paths["candidate_ids"] = path
            logger.info(
                "candidate_ids: %d filas exportadas.", file_results["candidate_ids"]["count"]
            )

            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(CANDIDATE_STATS_QUERY, (boundary,))
                stats = cur.fetchone()

            manifest = build_manifest(
                cutoff=cutoff,
                retention_days=retention_days,
                jobs_columns=columns,
                stats=stats,
                file_paths=file_paths,
                file_results=file_results,
            )
            manifest_path = out_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Manifiesto escrito en %s", manifest_path)

    final_dir = Path(dest) / f"retention_{ts}"
    logger.info("Backup publicado en %s", final_dir)
    return final_dir


# =============================================================================
# verify — solo archivos locales, no requiere Supabase ni DATABASE_URL
# =============================================================================


def verify_backup(manifest_path: Path) -> list[str]:
    """
    Verifica un backup ya creado usando EXCLUSIVAMENTE los archivos locales
    del manifiesto — no abre ninguna conexión a Supabase.

    Devuelve la lista de problemas encontrados (vacía si todo está bien).
    """
    problems: list[str] = []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent

    datasets: dict[str, list[dict]] = {}
    for key, meta in manifest.get("files", {}).items():
        path = base / meta["filename"]
        if not path.exists():
            problems.append(f"{key}: archivo no encontrado ({path})")
            continue
        raw_gz = path.read_bytes()
        sha_file = hashlib.sha256(raw_gz).hexdigest()
        if sha_file != meta["sha256_file"]:
            problems.append(f"{key}: sha256_file no coincide con el manifiesto")
        try:
            raw = gzip.decompress(raw_gz)
        except OSError as exc:
            problems.append(f"{key}: no se pudo descomprimir ({exc})")
            continue
        sha_content = hashlib.sha256(raw).hexdigest()
        if sha_content != meta["sha256_content"]:
            problems.append(f"{key}: sha256_content no coincide con el manifiesto")
        try:
            rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]
        except json.JSONDecodeError as exc:
            problems.append(f"{key}: contenido no es JSONL válido ({exc})")
            continue
        datasets[key] = rows

    count_fields = {
        "jobs": "jobs_count",
        "job_skills": "job_skills_count",
        "skills_snapshot": "skills_count",
        "candidate_ids": "candidate_ids_count",
    }
    for key, field in count_fields.items():
        if key in datasets and field in manifest and len(datasets[key]) != manifest[field]:
            problems.append(
                f"{key}: count real {len(datasets[key])} != manifest[{field}]={manifest[field]}"
            )

    if "jobs" in datasets:
        ids = [r["id"] for r in datasets["jobs"]]
        if len(ids) != len(set(ids)):
            problems.append("jobs: hay id duplicados en el archivo")

    if "candidate_ids" in datasets:
        cand_ids_list = [r["id"] for r in datasets["candidate_ids"]]
        if len(cand_ids_list) != len(set(cand_ids_list)):
            problems.append("candidate_ids: hay id duplicados en el archivo")
        if "jobs" in datasets:
            cand_ids = set(cand_ids_list)
            jobs_ids = {r["id"] for r in datasets["jobs"]}
            if cand_ids != jobs_ids:
                faltan = jobs_ids - cand_ids
                sobran = cand_ids - jobs_ids
                problems.append(
                    "candidate_ids no coincide exactamente con los IDs de jobs "
                    f"(faltan {len(faltan)} en candidate_ids, sobran {len(sobran)})"
                )

    if "job_skills" in datasets:
        pairs = [(r["job_id"], r["skill_id"]) for r in datasets["job_skills"]]
        if len(pairs) != len(set(pairs)):
            problems.append("job_skills: hay pares (job_id, skill_id) duplicados")
        if "candidate_ids" in datasets:
            cand_ids = {r["id"] for r in datasets["candidate_ids"]}
            huerfanos_job = {jid for jid, _ in pairs if jid not in cand_ids}
            if huerfanos_job:
                problems.append(
                    f"job_skills: {len(huerfanos_job)} job_id fuera de candidate_ids"
                )
        if "skills_snapshot" in datasets:
            skill_ids = {r["id"] for r in datasets["skills_snapshot"]}
            huerfanos_skill = {sid for _, sid in pairs if sid not in skill_ids}
            if huerfanos_skill:
                problems.append(
                    f"job_skills: {len(huerfanos_skill)} skill_id no existen en skills_snapshot"
                )

    return problems


# =============================================================================
# CLI
# =============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backup/verificación de la retención de Adzuna a 60 días. "
            "Sin flags: dry-run (no escribe nada)."
        )
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Crea el backup real (jobs/job_skills/skills/candidate_ids + manifest). "
        "Requiere --dest. Sin este flag, el script nunca escribe archivos.",
    )
    parser.add_argument(
        "--dest",
        type=str,
        default=None,
        help="Directorio raíz donde crear retention_<timestamp>/. Obligatorio con --backup.",
    )
    parser.add_argument(
        "--verify",
        type=str,
        default=None,
        metavar="MANIFEST",
        help="Verifica un backup ya creado a partir de su manifest.json. "
        "No requiere Supabase ni DATABASE_URL.",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=RETENTION_DAYS_DEFAULT,
        help=f"Ventana de retención en días (por defecto {RETENTION_DAYS_DEFAULT}).",
    )
    parser.add_argument(
        "--cutoff",
        type=str,
        default=None,
        help="Override ISO 8601 del cutoff UTC (uso avanzado / reproducibilidad en tests, no "
        "requiere Supabase). Por defecto: SELECT NOW() de PostgreSQL, capturado una sola vez "
        "dentro de la misma transacción/snapshot que el resto del backup.",
    )
    return parser


def _resolve_cutoff_override(raw: str | None) -> datetime | None:
    """
    None si no se pasó `--cutoff` (comportamiento por defecto: el cutoff lo
    captura `_capture_cutoff()` desde Postgres, dentro de la transacción).
    Si se pasó, lo parsea aquí para no necesitar Supabase para validarlo.
    """
    if not raw:
        return None
    cutoff = datetime.fromisoformat(raw)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff


def main(argv: list[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)

    if args.verify:
        problems = verify_backup(Path(args.verify))
        if problems:
            print("VERIFY: FALLÓ")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)
        print("VERIFY: OK — todo coincide (archivos, hashes, counts y relaciones).")
        return

    cutoff_override = _resolve_cutoff_override(args.cutoff)

    if args.backup:
        if not args.dest:
            print("ERROR: --backup requiere --dest RUTA", file=sys.stderr)
            sys.exit(2)
        conn = _get_connection()
        try:
            out_dir = run_backup(conn, args.retention_days, args.dest, cutoff_override)
        finally:
            conn.close()
        print(f"Backup creado en: {out_dir}")
        return

    conn = _get_connection()
    try:
        dry_run_report(conn, args.retention_days, args.dest, cutoff_override)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
