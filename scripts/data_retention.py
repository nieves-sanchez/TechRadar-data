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
    --restore-plan RUTA/manifest.json — READ-ONLY: calcula exactamente qué
        haría un restore real (inserts/conflictos/links faltantes) sin
        escribir nada, ni en disco ni en Supabase.
    --restore RUTA/manifest.json --confirm-restore — restaura jobs/job_skills
        desde un backup ya verificado. `--confirm-restore` es obligatorio;
        sin él, `--restore` no hace nada (ni siquiera abre conexión).
    --delete-plan RUTA/manifest.json — READ-ONLY: calcula exactamente qué
        borraría un DELETE real (already_missing/deletable/drift) usando
        los `candidate_ids` de ESE manifiesto como única autoridad — nunca
        recalcula `NOW() - 60 días`. Sin escribir nada.
    --delete RUTA/manifest.json --confirm-delete — borra de `jobs` los
        candidatos del backup que sigan existiendo e idénticos.
        `--confirm-delete` es obligatorio; sin él, `--delete` no hace nada
        (ni siquiera abre conexión). **NO ejecutado nunca contra Supabase
        real todavía — pendiente de autorización explícita separada.**

NO implementado en este turno, a propósito (ver notas de diseño en
notes/PROJECT_MASTER_CONTEXT.md Parte 10 §96 para el porqué):
    - VACUUM
    - política de backups para la retención recurrente (sin decidir todavía)
    - retención RECURRENTE automatizada (cron) — este turno solo prepara el
      DELETE de un backup concreto, ejecutado manualmente y una vez

Este módulo NUNCA ejecuta UPDATE, ALTER, DROP, TRUNCATE, VACUUM ni
REINDEX/CLUSTER. Los únicos `INSERT`/`DELETE` permitidos son los de
`--restore`/`--delete`, siempre acotados y verificados por test:
`INSERT ... ON CONFLICT ... DO NOTHING` (nunca `DO UPDATE`, nunca
sobrescribe una fila existente) y `DELETE FROM jobs WHERE id = ANY(%s)`
(el único DELETE del módulo, nunca contra `job_skills`/`skills` —
`job_skills.job_id → jobs.id ON DELETE CASCADE`, la única FK real hacia
`jobs`, se encarga de sus vínculos). Ver
test_no_forbidden_keywords_in_sql_constants /
test_insert_sql_is_confined_to_whitelisted_restore_constants /
test_delete_sql_is_confined_to_whitelisted_constant en
tests/test_data_retention.py. Toda lectura contra Supabase es SELECT /
catálogo de PostgreSQL. Los modos de solo lectura (dry-run, --restore-plan,
--delete-plan) usan una única transacción con aislamiento REPEATABLE READ
(transaccional, no de sesión — ver `snapshot_transaction()`); `--restore`/
`--delete` reales usan una transacción propia POR BATCH, con commit
inmediato tras cada batch (ver `_write_batch_transaction()`), para que una
ejecución interrumpida sea reanudable sin duplicar ni corromper nada. No se
usa `conn.set_session(readonly=True)` ni `SET SESSION CHARACTERISTICS ...`
ni `SET default_transaction_read_only` — esa combinación ya contaminó
conexiones reutilizadas del Transaction Pooler de Supabase en el pasado
(regla permanente, PROJECT_MASTER_CONTEXT.md §82).

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

Diseño del restore (§99 de PROJECT_MASTER_CONTEXT.md tiene el contexto del
primer backup real; el diseño de esta sección se documenta en el turno que
lo implementa):
    Orden: jobs primero, luego job_skills (FK real job_skills.job_id →
    jobs.id). `skills` NUNCA se restaura — el snapshot solo sirve para
    diagnóstico y para el gate de "todos los skill_id necesarios existen".

    Por cada job del backup, comparando contra el estado ACTUAL (nunca el
    histórico):
      - No existe hoy       → candidato a INSERT.
      - Existe e idéntico    → no se inserta (ya está), pero sus job_skills
        respaldados SÍ son restaurables (permite reanudar un restore
        parcial sin duplicar nada).
      - Existe pero DISTINTO → CONFLICTO: nunca se sobrescribe, y sus
        job_skills respaldados NUNCA se restauran automáticamente. Se
        reporta para revisión manual (columna a columna, no solo un hash).

    Comparación fila a fila reutilizando `_json_value()`/la misma
    canonicalización del backup (nunca `default=str`) — no se duplica lógica
    de serialización entre export y restore.

    Antes de escribir nada: si algún `skill_id` referenciado por el
    job_skills del backup ya no existe en `skills`, el restore real
    ABORTA por completo (no se recrea el catálogo automáticamente).

    Escritura en batches (`--restore-batch-size`, por defecto
    RESTORE_BATCH_SIZE_DEFAULT), cada batch en su propia transacción con
    commit inmediato — mismo patrón que `enrich_jobs()` en
    `repair_crawl.py` (una transacción por lote, resumible). Si la
    conexión se pierde a mitad, los batches ya commiteados quedan
    persistidos tal cual; relanzar el mismo comando reclasifica todo desde
    cero y completa solo lo que falte (`ON CONFLICT DO NOTHING` + la regla
    "existe e idéntico → restaura job_skills faltantes" de arriba).

Diseño del delete (turno posterior, tras el primer backup+restore reales;
autoridad exclusiva: `candidate_ids`/`jobs` DEL MANIFIESTO indicado, nunca
un recálculo de `NOW() - 60 días` en el momento de borrar — evita drift
temporal entre el snapshot que se decidió preservar y lo que realmente se
borra):
    Reutiliza EXACTAMENTE `classify_jobs_batch()` — la misma comparación
    fila a fila del restore, sin duplicarla — y refina cada conflicto con
    `_is_benign_deactivation()` (exclusiva de esta ruta; el restore nunca
    la llama):
      - No existe hoy       → `already_missing`, no-op (ya no está; puede
        ser un DELETE anterior ya aplicado — relanzar es idempotente).
      - Existe e idéntico    → SAFE_IDENTICAL, candidato REAL a DELETE.
      - Único diff `is_active` TRUE(backup)→FALSE(actual), las otras 22
        columnas idénticas → SAFE_BENIGN_DEACTIVATION (auditoría
        2026-09-22, PROJECT_MASTER_CONTEXT.md §102): también candidato
        REAL a DELETE — es la única transición que
        `_deactivate_old_jobs()` (`scripts/load.py`) puede producir, y la
        produce sin tocar ninguna otra columna; por construcción no puede
        ser una reingesta, actualización de contenido ni reactivación
        (esas siempre tocan `last_seen_at` y ponen `is_active=TRUE`).
      - Cualquier otro diff (`FALSE→TRUE`, NULL de por medio, o
        `is_active` + otra columna) → MATERIAL_DRIFT: NUNCA se borra, se
        reporta columna a columna para revisión manual, igual que un
        conflicto de restore. **El restore/`--restore-plan` NUNCA aplican
        esta excepción** — siguen exigiendo las 23 columnas idénticas sin
        excepción; una fila `is_active TRUE→FALSE` sigue siendo CONFLICTO
        para restore.

    SQL: `DELETE FROM jobs WHERE id = ANY(%s)`, sobre la unión de ids
    SAFE_IDENTICAL ∪ SAFE_BENIGN_DEACTIVATION de cada batch. `job_skills.
    job_id → jobs.id ON DELETE CASCADE` (única FK real hacia `jobs`,
    confirmada contra el catálogo de PostgreSQL — PROJECT_MASTER_CONTEXT.
    md §96) elimina sus vínculos automáticamente: no hay DELETE manual de
    `job_skills`, y `skills` nunca se toca.

    TOCTOU cerrado con `SELECT ... FOR UPDATE` (auditoría 2026-09-22, antes
    de autorizar el primer DELETE real): comparar y borrar ocurrían dentro
    de la misma transacción, pero el `SELECT` de clasificación no
    bloqueaba las filas leídas — nada impedía que otra transacción
    modificara una fila "identical" entre la comparación y el `DELETE`
    posterior. `_process_jobs_delete(write=True)` ahora clasifica con
    `classify_jobs_batch(..., for_update=True)`, que bloquea únicamente
    las filas del batch actual (nunca las 197K a la vez) hasta el
    COMMIT/ROLLBACK de esa transacción — cualquier escritura concurrente
    sobre esas filas queda bloqueada hasta entonces, y al liberarse ve la
    fila ya borrada (si se borró) o intacta (si era drift y nunca se
    tocó). La decisión SAFE_IDENTICAL/SAFE_BENIGN_DEACTIVATION/
    MATERIAL_DRIFT se recalcula sobre la fila YA bloqueada, nunca sobre el
    resultado de un `--delete-plan` anterior — si una fila que parecía
    benign en el plan cambia de forma material justo antes de adquirir el
    lock, se detecta ahí y no se borra. `--delete-plan`/`--restore`/
    `--restore-plan` NUNCA usan `FOR UPDATE` — no borran nada, así que no
    hay nada que proteger; el `INSERT ... ON CONFLICT DO NOTHING` del
    restore ya es seguro ante una inserción concurrente por construcción,
    sin necesitar bloqueo.

    Protección de `job_skills` dentro de la misma transacción (auditoría
    2026-09-24, antes de autorizar el DELETE real): que `jobs` esté
    bloqueado y comparado no bastaba — `job_skills` nunca se verificaba
    contra el backup, solo se contaba. Ahora, para los jobs ya seguros por
    columnas de cada batch, `_process_jobs_delete(write=True)` bloquea
    también sus filas de `job_skills`
    (`JOB_SKILLS_BY_JOB_IDS_FOR_UPDATE_QUERY`, acotado a esos jobs, nunca
    las 214K a la vez) y compara el set EXACTO `BACKUP_SET(job_id) ==
    CURRENT_SET(job_id)` (`_split_by_job_skills_match`) — nunca solo un
    `COUNT`. Un link nuevo, uno faltante, o cualquier diferencia (aunque
    el total coincida) marca ese job como JOB_SKILLS_DRIFT y lo EXCLUYE
    del borrado de ESE job únicamente (granularidad por job, no por
    batch completo) — reportado aparte, nunca mezclado con
    `material_drift` de las 23 columnas. El `FOR UPDATE` de `jobs` ya
    cierra la ventana de "link nuevo colándose" por sí solo: PostgreSQL
    exige `FOR KEY SHARE` sobre la fila padre para insertar en una tabla
    hija con FK, y `FOR KEY SHARE` es incompatible con el `FOR UPDATE`
    que ya tenemos desde el paso anterior — un `INSERT INTO job_skills`
    concurrente para ese job queda bloqueado en cuanto bloqueamos `jobs`.
    El `FOR UPDATE` de `job_skills` cierra además la ventana de "link
    existente borrado por otra vía" (hoy no hay ningún código que borre
    de `job_skills` salvo un script SQL manual sin automatizar, ver
    `sql/skills_canonicalization_plan.sql`). `--delete-plan` hace la
    MISMA comparación de sets, sin ningún `FOR UPDATE` — solo diagnóstico.

    Batches (`--delete-batch-size`, por defecto DELETE_BATCH_SIZE_DEFAULT)
    con transacción propia por lote y commit inmediato — mismo
    `_write_batch_transaction()` que ya usa el restore. Si la conexión se
    pierde a mitad, los batches ya commiteados quedan borrados tal cual;
    relanzar el mismo comando reclasifica todo desde cero: los ya borrados
    cuentan como `already_missing` (no-op), el resto se procesa con
    normalidad, y el drift sigue protegido en cada intento.

    Concurrencia con Pipeline A: no se introducen advisory locks ni
    arquitectura distribuida — el `FOR UPDATE` de arriba ya cierra la
    ventana real de corrupción, acotado siempre al batch en curso (nunca
    197K filas a la vez). Como capa operativa adicional (defensa en
    profundidad, no porque haga falta para la corrección): los candidatos
    tienen `posted_at` >60 días y Pipeline A solo reingesta ofertas
    recientes (`days=1`), así que en la práctica nunca debería colisionar;
    aun así, se recomienda ejecutar `--delete` fuera de la franja del cron
    de Pipeline A (evitar 05:30-07:00 UTC) y no lanzar Pipeline A
    manualmente mientras dura la retención. No se automatiza — gate
    operativo, no un lock adicional.

Uso:
    python -m scripts.data_retention
    python -m scripts.data_retention --backup --dest "C:\\ruta\\TechRadar-data-backups"
    python -m scripts.data_retention --verify "C:\\ruta\\retention_.../manifest.json"
    python -m scripts.data_retention --restore-plan "C:\\ruta\\retention_.../manifest.json"
    python -m scripts.data_retention --restore "C:\\ruta\\retention_.../manifest.json" --confirm-restore
    python -m scripts.data_retention --delete-plan "C:\\ruta\\retention_.../manifest.json"
    python -m scripts.data_retention --delete "C:\\ruta\\retention_.../manifest.json" --confirm-delete
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
RESTORE_BATCH_SIZE_DEFAULT = 500  # mismo orden de magnitud que load.BATCH_SIZE
DELETE_BATCH_SIZE_DEFAULT = 500  # mismo criterio conservador que el restore
MAX_CONFLICT_SAMPLES = 50  # tope de diagnostico detallado, no de deteccion

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

# --- Restore/delete-plan: solo lectura, SIN bloqueo -------------------------
# {cols} se rellena igual que JOBS_QUERY_TEMPLATE, con columnas reales.
JOBS_BY_IDS_TEMPLATE = "SELECT {cols} FROM jobs WHERE id = ANY(%s)"
JOB_SKILLS_BY_JOB_IDS_QUERY = "SELECT job_id, skill_id FROM job_skills WHERE job_id = ANY(%s)"
SKILLS_BY_IDS_QUERY = "SELECT id FROM skills WHERE id = ANY(%s)"

# --- Delete REAL únicamente: SELECT ... FOR UPDATE, para cerrar el TOCTOU
# entre clasificar y borrar (ver classify_jobs_batch(for_update=True) y
# _process_jobs_delete). NUNCA usado por --delete-plan/--restore/--restore-plan
# (todos SELECT normal, sin bloquear filas) -- verificado por
# test_delete_plan_never_locks_rows_for_update /
# test_restore_never_locks_rows_for_update.
JOBS_BY_IDS_FOR_UPDATE_TEMPLATE = "SELECT {cols} FROM jobs WHERE id = ANY(%s) FOR UPDATE"

# --- Restore: los DOS únicos INSERT del módulo, siempre ON CONFLICT DO
# NOTHING -- ver test_insert_sql_is_confined_to_whitelisted_restore_constants
# y test_insert_constants_are_always_on_conflict_do_nothing en
# tests/test_data_retention.py, que fallan si esto deja de cumplirse.
INSERT_JOBS_TEMPLATE = "INSERT INTO jobs ({cols}) VALUES %s ON CONFLICT (id) DO NOTHING"
INSERT_JOB_SKILLS_QUERY = (
    "INSERT INTO job_skills (job_id, skill_id) VALUES %s ON CONFLICT (job_id, skill_id) DO NOTHING"
)

# --- Delete: SELECT ... FOR UPDATE de job_skills, EXCLUSIVO de la ruta
# destructiva (--delete real) -- bloquea únicamente los links de los jobs
# "seguros por columnas" del batch actual (nunca los 214K a la vez), para
# que la comparación exacta contra el backup no pueda quedar desactualizada
# por una escritura concurrente entre la comparación y el DELETE. NUNCA
# usado por --delete-plan/--restore/--restore-plan (ver
# JOB_SKILLS_BY_JOB_IDS_QUERY, sin bloqueo, para esos modos de solo lectura).
JOB_SKILLS_BY_JOB_IDS_FOR_UPDATE_QUERY = (
    "SELECT job_id, skill_id FROM job_skills WHERE job_id = ANY(%s) FOR UPDATE"
)

# --- Delete: el ÚNICO DELETE del módulo. Solo apunta a `jobs` por `id`;
# `job_skills.job_id → jobs.id ON DELETE CASCADE` (única FK real hacia
# `jobs`) hace el resto -- nunca un DELETE manual de job_skills, `skills`
# nunca se toca. Ver test_delete_sql_is_confined_to_whitelisted_constant y
# test_delete_constant_only_targets_jobs_by_id en tests/test_data_retention.py.
DELETE_JOBS_BY_IDS_QUERY = "DELETE FROM jobs WHERE id = ANY(%s)"


class SchemaMismatchError(RuntimeError):
    """El schema real de `jobs` cambió de forma incompatible con lo esperado."""


class RestoreAbortedError(RuntimeError):
    """El restore se abortó antes de escribir nada — ver el motivo en el mensaje."""


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
def _write_batch_transaction(conn):
    """
    Transacción de un solo batch de ESCRITURA para el restore: si el bloque
    `with` termina sin excepción, hace COMMIT; si lanza, hace ROLLBACK y
    propaga. Cada batch es su propia unidad atómica y reanudable — mismo
    patrón que `enrich_jobs()` en `repair_crawl.py` (una transacción por
    lote, resumible vía `ON CONFLICT DO NOTHING` + reclasificación en el
    siguiente intento, nunca reintento automático a mitad de un batch).

    `SET LOCAL TIME ZONE 'UTC'` para que la comparación de timestamptz de
    `classify_jobs_batch()` sea consistente con la que usó el backup.
    """
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(SET_TIMEZONE_SQL)
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _read_jsonl_gz(path: Path):
    """
    Generador que entrega cada fila del backup como dict (los mismos tipos
    JSON nativos que escribió `export_rows_to_jsonl_gz`), línea a línea, sin
    cargar el archivo completo en memoria — simétrico al lado de export.
    Nunca abre en modo escritura: el backup es inmutable.
    """
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def _batched(iterable, size):
    """Agrupa `iterable` en listas de como mucho `size` elementos."""
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _backup_file_path(backup_dir: Path, manifest: dict, key: str) -> Path:
    return backup_dir / manifest["files"][key]["filename"]


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
# restore — lee exclusivamente el backup ya verificado; nunca sobrescribe
# =============================================================================


def classify_jobs_batch(cur, columns: list[str], batch_rows: list[dict], *, for_update: bool = False):
    """
    Clasifica un batch de filas del backup (dicts ya canonicalizados, tal
    cual los devuelve `_read_jsonl_gz`) contra el estado ACTUAL de `jobs`.
    Solo SELECT — no escribe nada por sí misma.

    Compartida entre `--restore`/`--restore-plan` y `--delete`/
    `--delete-plan`: "seguro para restaurar sin pisar nada" y "seguro para
    borrar" son la MISMA comparación — no dos lógicas paralelas que puedan
    divergir. El restore usa `insert_rows` (candidatos a INSERT) e
    `identical_ids` (safe-existing); el delete usa `insert_rows` como
    `already_missing` (ya no existe, nada que borrar) e `identical_ids`
    como candidatos reales a DELETE. En ambos, `conflict_ids` NUNCA se toca.

    `for_update=True` (usado EXCLUSIVAMENTE por el DELETE real,
    `_process_jobs_delete(write=True)`) añade `FOR UPDATE` al SELECT:
    bloquea las filas encontradas hasta que la transacción del batch
    (`_write_batch_transaction`) haga COMMIT o ROLLBACK, cerrando el hueco
    TOCTOU entre "comparar" y "borrar" — ninguna otra transacción puede
    modificar esa fila entre esta lectura y el DELETE posterior en el
    mismo batch/transacción; si lo intenta, se bloquea hasta que la
    nuestra termine. `--restore`/`--restore-plan`/`--delete-plan` NUNCA
    pasan `for_update=True` (no necesitan bloquear nada: no borran, y el
    `INSERT ... ON CONFLICT DO NOTHING` del restore ya es seguro ante una
    inserción concurrente por sí solo).

    Compara columna a columna reutilizando `_json_value()` (la misma
    canonicalización del export) sobre la fila real de Postgres, para que
    "igual" signifique exactamente lo mismo en ambos lados (NULL vs NULL,
    bool, int, timestamptz ya normalizado a UTC por la transacción activa).

    Devuelve (insert_rows, identical_ids, conflict_ids, conflicts):
      - insert_rows: sublista de batch_rows cuyo id NO existe hoy en jobs.
      - identical_ids: ids que existen y son idénticos al backup.
      - conflict_ids: ids que existen pero difieren — nunca se tocan.
      - conflicts: [{"id":, "diffs": [(columna, valor_backup, valor_live), ...]}]
        para CADA conflicto del batch (el llamador decide si acota la
        muestra para diagnóstico; aquí nunca se trunca la detección).
    """
    ids = [row["id"] for row in batch_rows]
    template = JOBS_BY_IDS_FOR_UPDATE_TEMPLATE if for_update else JOBS_BY_IDS_TEMPLATE
    cur.execute(template.format(cols=", ".join(columns)), (ids,))
    live_by_id = {
        row[0]: {col: _json_value(v) for col, v in zip(columns, row)} for row in cur.fetchall()
    }

    insert_rows: list[dict] = []
    identical_ids: set[int] = set()
    conflict_ids: set[int] = set()
    conflicts: list[dict] = []
    for backup_row in batch_rows:
        id_ = backup_row["id"]
        live_row = live_by_id.get(id_)
        if live_row is None:
            insert_rows.append(backup_row)
        elif live_row == backup_row:
            identical_ids.add(id_)
        else:
            conflict_ids.add(id_)
            diffs = [
                (col, backup_row.get(col), live_row.get(col))
                for col in columns
                if backup_row.get(col) != live_row.get(col)
            ]
            conflicts.append({"id": id_, "diffs": diffs})
    return insert_rows, identical_ids, conflict_ids, conflicts


def _process_jobs_restore(conn, columns: list[str], jobs_path: Path, batch_size: int, *, write: bool):
    """
    Recorre el backup de `jobs` en batches y clasifica cada uno contra el
    estado actual. Si `write=True`, inserta los "missing" de cada batch
    (`ON CONFLICT (id) DO NOTHING`) dentro de su propia transacción con
    commit inmediato (`_write_batch_transaction`) — nunca una transacción
    gigante para las 197K filas. Si `write=False` (`--restore-plan`), solo
    hace SELECT, pensado para ejecutarse dentro de un `snapshot_transaction`
    ya abierto por el llamador.

    Devuelve dict con counts + `conflict_ids` (set COMPLETO, sin acotar —
    lo usa `_process_job_skills_restore` para no restaurar skills de un job
    conflictivo) + `conflicts_sample` (acotado a MAX_CONFLICT_SAMPLES, solo
    para diagnóstico impreso).
    """
    total = insert_count = identical_count = 0
    conflict_ids: set[int] = set()
    conflicts_sample: list[dict] = []

    for batch in _batched(_read_jsonl_gz(jobs_path), batch_size):
        total += len(batch)
        if write:
            with _write_batch_transaction(conn):
                with conn.cursor() as cur:
                    insert_rows, identical_ids, batch_conflict_ids, batch_conflicts = (
                        classify_jobs_batch(cur, columns, batch)
                    )
                    if insert_rows:
                        values = [tuple(row[c] for c in columns) for row in insert_rows]
                        psycopg2.extras.execute_values(
                            cur,
                            INSERT_JOBS_TEMPLATE.format(cols=", ".join(columns)),
                            values,
                            page_size=len(values),
                        )
        else:
            with conn.cursor() as cur:
                insert_rows, identical_ids, batch_conflict_ids, batch_conflicts = (
                    classify_jobs_batch(cur, columns, batch)
                )

        insert_count += len(insert_rows)
        identical_count += len(identical_ids)
        conflict_ids |= batch_conflict_ids
        room = MAX_CONFLICT_SAMPLES - len(conflicts_sample)
        if room > 0:
            conflicts_sample.extend(batch_conflicts[:room])
        logger.info(
            "jobs: %d procesados (insert=%d identical=%d conflict=%d)",
            total,
            insert_count,
            identical_count,
            len(conflict_ids),
        )

    return {
        "total": total,
        "insert_count": insert_count,
        "identical_count": identical_count,
        "conflict_ids": conflict_ids,
        "conflicts_sample": conflicts_sample,
    }


def _process_job_skills_restore(
    conn, job_skills_path: Path, batch_size: int, conflict_ids: set[int], *, write: bool
):
    """
    Recorre el backup de `job_skills` en batches. Un par (job_id, skill_id)
    se descarta sin más si `job_id` está en `conflict_ids` (nunca se
    restauran skills de un job conflictivo). Del resto, distingue los que
    ya existen (no se duplican) de los que faltan. Si `write=True`, inserta
    los que faltan (`ON CONFLICT (job_id, skill_id) DO NOTHING`) en su
    propia transacción por batch. Requiere que el preflight de skill_id
    (`find_missing_skill_ids`) ya haya pasado antes de llamar con
    `write=True`.
    """
    restorable = existing = blocked = 0

    for batch in _batched(_read_jsonl_gz(job_skills_path), batch_size):
        pairs = [(row["job_id"], row["skill_id"]) for row in batch]
        candidate_pairs = [(j, s) for j, s in pairs if j not in conflict_ids]
        blocked += len(pairs) - len(candidate_pairs)
        if not candidate_pairs:
            continue
        job_ids_in_batch = list({j for j, _ in candidate_pairs})

        if write:
            with _write_batch_transaction(conn):
                with conn.cursor() as cur:
                    cur.execute(JOB_SKILLS_BY_JOB_IDS_QUERY, (job_ids_in_batch,))
                    existing_pairs = {(r[0], r[1]) for r in cur.fetchall()}
                    missing_pairs = [p for p in candidate_pairs if p not in existing_pairs]
                    if missing_pairs:
                        psycopg2.extras.execute_values(
                            cur, INSERT_JOB_SKILLS_QUERY, missing_pairs, page_size=len(missing_pairs)
                        )
        else:
            with conn.cursor() as cur:
                cur.execute(JOB_SKILLS_BY_JOB_IDS_QUERY, (job_ids_in_batch,))
                existing_pairs = {(r[0], r[1]) for r in cur.fetchall()}
                missing_pairs = [p for p in candidate_pairs if p not in existing_pairs]

        restorable += len(missing_pairs)
        existing += len(candidate_pairs) - len(missing_pairs)

    return {"restorable": restorable, "existing": existing, "blocked_by_conflict": blocked}


def find_missing_skill_ids(conn, job_skills_path: Path, skills_snapshot_path: Path) -> list[dict]:
    """
    Verifica que todos los `skill_id` referenciados por el `job_skills` del
    backup existen HOY en `skills`. Devuelve una lista de dicts
    {id, name, category} (leídos del `skills_snapshot` del propio backup,
    nunca reconstruidos) para los que faltan — vacía si todo está bien.

    No recrea nada. Un restore real debe ABORTAR por completo si esta lista
    no está vacía (ver `run_restore`); recrear el catálogo de skills sería
    una decisión manual distinta, fuera de alcance de este módulo.
    """
    needed = {row["skill_id"] for row in _read_jsonl_gz(job_skills_path)}
    if not needed:
        return []
    with conn.cursor() as cur:
        cur.execute(SKILLS_BY_IDS_QUERY, (list(needed),))
        existing = {row[0] for row in cur.fetchall()}
    missing_ids = needed - existing
    if not missing_ids:
        return []
    snapshot_by_id = {row["id"]: row for row in _read_jsonl_gz(skills_snapshot_path)}
    return [snapshot_by_id[sid] for sid in sorted(missing_ids) if sid in snapshot_by_id]


def build_restore_plan(conn, manifest: dict, backup_dir: Path, batch_size: int) -> dict:
    """
    Modo READ-ONLY (`--restore-plan`): calcula exactamente lo que haría un
    restore real, sin escribir nada, dentro de UNA única transacción
    REPEATABLE READ (`snapshot_transaction`, igual que el dry-run del
    backup). Nunca usa `conn.set_session(readonly=True)`.
    """
    columns = manifest["jobs_columns"]
    jobs_path = _backup_file_path(backup_dir, manifest, "jobs")
    job_skills_path = _backup_file_path(backup_dir, manifest, "job_skills")
    skills_snapshot_path = _backup_file_path(backup_dir, manifest, "skills_snapshot")

    with snapshot_transaction(conn):
        missing_skills = find_missing_skill_ids(conn, job_skills_path, skills_snapshot_path)
        jobs_result = _process_jobs_restore(conn, columns, jobs_path, batch_size, write=False)
        job_skills_result = _process_job_skills_restore(
            conn, job_skills_path, batch_size, jobs_result["conflict_ids"], write=False
        )

    return {
        "jobs_total": jobs_result["total"],
        "jobs_insertable": jobs_result["insert_count"],
        "jobs_identical": jobs_result["identical_count"],
        "jobs_conflict": len(jobs_result["conflict_ids"]),
        "conflicts_sample": jobs_result["conflicts_sample"],
        "job_skills_restorable": job_skills_result["restorable"],
        "job_skills_existing": job_skills_result["existing"],
        "job_skills_blocked_by_conflict": job_skills_result["blocked_by_conflict"],
        "missing_skill_ids": missing_skills,
        "would_abort": bool(missing_skills),
    }


def run_restore(conn, manifest: dict, backup_dir: Path, batch_size: int) -> dict:
    """
    Restore REAL: `INSERT ... ON CONFLICT DO NOTHING`, batch a batch, cada
    batch en su propia transacción con commit inmediato (ver
    `_process_jobs_restore` / `_process_job_skills_restore` /
    `_write_batch_transaction`) — mismo patrón que `enrich_jobs()` en
    `repair_crawl.py`: si se pierde la conexión a mitad, los batches ya
    commiteados quedan persistidos, y relanzar el mismo comando reclasifica
    todo desde cero y completa solo lo que falte, sin duplicar nada.

    ABORTA (`RestoreAbortedError`) antes de escribir nada si falta algún
    `skill_id` necesario en `skills`.
    """
    columns = manifest["jobs_columns"]
    jobs_path = _backup_file_path(backup_dir, manifest, "jobs")
    job_skills_path = _backup_file_path(backup_dir, manifest, "job_skills")
    skills_snapshot_path = _backup_file_path(backup_dir, manifest, "skills_snapshot")

    with snapshot_transaction(conn):
        missing_skills = find_missing_skill_ids(conn, job_skills_path, skills_snapshot_path)
    if missing_skills:
        detalle = ", ".join(f"id={s['id']} name={s['name']!r}" for s in missing_skills[:10])
        raise RestoreAbortedError(
            f"Restore abortado antes de escribir nada: {len(missing_skills)} skill_id "
            f"necesarios ya no existen en `skills` ({detalle}{'...' if len(missing_skills) > 10 else ''})."
        )

    jobs_result = _process_jobs_restore(conn, columns, jobs_path, batch_size, write=True)
    job_skills_result = _process_job_skills_restore(
        conn, job_skills_path, batch_size, jobs_result["conflict_ids"], write=True
    )

    return {
        "jobs_total": jobs_result["total"],
        "jobs_insertable": jobs_result["insert_count"],
        "jobs_identical": jobs_result["identical_count"],
        "jobs_conflict": len(jobs_result["conflict_ids"]),
        "conflicts_sample": jobs_result["conflicts_sample"],
        "job_skills_restorable": job_skills_result["restorable"],
        "job_skills_existing": job_skills_result["existing"],
        "job_skills_blocked_by_conflict": job_skills_result["blocked_by_conflict"],
        "missing_skill_ids": [],
        "would_abort": False,
    }


def print_restore_report(plan: dict, *, executed: bool) -> None:
    """Formato compartido entre `--restore-plan` (hipotético) y `--restore` (ya ejecutado)."""
    title = "RESTORE — ejecutado" if executed else "RESTORE-PLAN — READ-ONLY, nada escrito"
    print("=" * 78)
    print(f"{title} (data_retention.py)")
    print("=" * 78)
    print(f"jobs en el backup: {plan['jobs_total']}")
    verbo_insert = "insertados" if executed else "candidatos a INSERT"
    print(f"  no existían / {verbo_insert}:                 {plan['jobs_insertable']}")
    print(f"  ya existían e idénticos (safe-existing):       {plan['jobs_identical']}")
    print(f"  ya existían pero DIFERENTES (conflicto, no tocados): {plan['jobs_conflict']}")
    print("job_skills en el backup:")
    verbo_links = "insertados" if executed else "restaurables (se insertarían)"
    print(f"  faltaban, {verbo_links}:            {plan['job_skills_restorable']}")
    print(f"  ya existían (no duplicados):                    {plan['job_skills_existing']}")
    print(f"  bloqueados por job conflictivo (no tocados):    {plan['job_skills_blocked_by_conflict']}")
    if plan["missing_skill_ids"]:
        print(
            f"{'ABORTADO' if executed else 'ABORTARÍA'}: "
            f"{len(plan['missing_skill_ids'])} skill_id necesarios ya no existen en `skills`:"
        )
        for s in plan["missing_skill_ids"][:20]:
            print(f"  id={s['id']} name={s['name']!r} category={s['category']!r}")
    else:
        print("Todos los skill_id necesarios existen actualmente en `skills`.")
    if plan["conflicts_sample"]:
        print(f"Muestra de conflictos (hasta {MAX_CONFLICT_SAMPLES}, de {plan['jobs_conflict']} totales):")
        for c in plan["conflicts_sample"][:10]:
            cols = [d[0] for d in c["diffs"]]
            print(f"  job id={c['id']}: {len(c['diffs'])} columna(s) distinta(s) -> {cols}")
    print("=" * 78)


# =============================================================================
# delete — autoridad exclusiva: candidate_ids/jobs de ESTE manifiesto,
# nunca un recálculo de NOW() - retention_days
# =============================================================================


def _is_benign_deactivation(diffs: list[tuple]) -> bool:
    """
    True si el ÚNICO diff de un conflicto es `is_active` pasando de
    `True` (backup) a `False` (actual) — la ÚNICA transición que
    `_deactivate_old_jobs()` (`scripts/load.py`) puede producir, y la
    produce SIN tocar ninguna otra columna (ni `last_seen_at`). Auditoría
    2026-09-22: sobre 3.220 conflictos reales encontrados en un
    `--delete-plan`, el 100% eran exactamente esta transición en solitario
    — ver `notes/PROJECT_MASTER_CONTEXT.md` §102.

    Por qué es seguro tratarla como no-drift SOLO para el DELETE de
    retention: los dos únicos caminos de escritura de `load.py` tienen
    huellas de columnas mutuamente excluyentes con esta transición.
    `_upsert_jobs()` (reingesta/reactivación) SIEMPRE pone `is_active =
    TRUE` y `last_seen_at = NOW()` en el mismo UPDATE — nunca puede dejar
    `is_active = FALSE` con las otras 22 columnas intactas.
    `_deactivate_old_jobs()` SOLO puede poner `is_active = FALSE` y nunca
    toca ninguna otra columna. Por construcción, esta transición en
    solitario no puede ser una reingesta, una actualización de contenido
    real, ni una reactivación.

    EXCLUSIVA del DELETE — NUNCA debe usarse para restore/`--restore-plan`,
    que deben seguir exigiendo las 23 columnas idénticas sin excepción
    (ver `_process_jobs_restore`, que nunca llama a esta función).
    Cualquier otra combinación (NULL de por medio, `FALSE→TRUE`, o
    `is_active` más cualquier otra columna) es `MATERIAL_DRIFT` y bloquea
    el borrado igual que antes.
    """
    if len(diffs) != 1:
        return False
    col, backup_val, live_val = diffs[0]
    return col == "is_active" and backup_val is True and live_val is False


def _load_backup_skills_by_job(job_skills_path: Path) -> dict[int, frozenset[int]]:
    """
    Carga TODO el `job_skills` del backup agrupado por `job_id`, una sola
    vez (214K filas típicas — trivial en memoria, unos pocos MB) — evita
    releer el archivo por cada batch. Usado exclusivamente por el DELETE
    para comparar el set EXACTO de cada job seguro contra su estado actual
    (nunca solo un `COUNT`).
    """
    by_job: dict[int, list[int]] = {}
    for row in _read_jsonl_gz(job_skills_path):
        by_job.setdefault(row["job_id"], []).append(row["skill_id"])
    return {job_id: frozenset(skill_ids) for job_id, skill_ids in by_job.items()}


def _group_pairs_by_job(pairs) -> dict[int, frozenset[int]]:
    """Agrupa filas (job_id, skill_id) -> {job_id: frozenset(skill_id)}."""
    by_job: dict[int, list[int]] = {}
    for job_id, skill_id in pairs:
        by_job.setdefault(job_id, []).append(skill_id)
    return {job_id: frozenset(skill_ids) for job_id, skill_ids in by_job.items()}


def _split_by_job_skills_match(
    job_ids, backup_skills_by_job: dict[int, frozenset[int]], current_by_job: dict[int, frozenset[int]]
):
    """
    Para cada `job_id`, compara `BACKUP_SET(job_id)` contra
    `CURRENT_SET(job_id)` — nunca solo por `len()`/`COUNT`, siempre por
    igualdad exacta de sets (un link faltante, uno nuevo, o uno distinto
    con el mismo total cuentan igual como drift). Un job sin ningún
    `job_skill` en ninguno de los dos lados (ambos vacíos) cuenta como
    exact match. Devuelve (exact_match_ids, drift_ids) — GRANULARIDAD POR
    JOB: un job con drift de links no bloquea a los demás jobs del batch.
    """
    exact_match_ids: set[int] = set()
    drift_ids: set[int] = set()
    empty: frozenset[int] = frozenset()
    for job_id in job_ids:
        if backup_skills_by_job.get(job_id, empty) == current_by_job.get(job_id, empty):
            exact_match_ids.add(job_id)
        else:
            drift_ids.add(job_id)
    return exact_match_ids, drift_ids


def _process_jobs_delete(
    conn, columns: list[str], jobs_path: Path, job_skills_path: Path, batch_size: int, *, write: bool
):
    """
    Recorre el `jobs` del backup (== `candidate_ids`, ya verificado) en
    batches y clasifica cada uno reutilizando `classify_jobs_batch()` — la
    MISMA comparación que gobierna el restore, sin duplicar la lógica de
    las 23 columnas — y refina cada conflicto con `_is_benign_deactivation()`
    (exclusiva de esta función; el restore nunca la usa):

      - No existe hoy       → `already_missing`: no-op, nada que borrar
        (relanzar tras un DELETE parcial cuenta estos como no-op, nunca
        como error — idempotencia).
      - Existe e idéntico al backup → SAFE_IDENTICAL por columnas.
      - Único diff `is_active` TRUE→FALSE → SAFE_BENIGN_DEACTIVATION por
        columnas (ver `_is_benign_deactivation`).
      - Cualquier otro diff (incluida `FALSE→TRUE`, NULL de por medio, o
        `is_active` + otra columna) → MATERIAL_DRIFT: NUNCA se borra.

    Un job "seguro por columnas" (identical ∪ benign) TODAVÍA no es
    suficiente para borrarlo: además se exige que `BACKUP_SET(job_id) ==
    CURRENT_SET(job_id)` de `job_skills` EXACTAMENTE
    (`_split_by_job_skills_match`) — un link nuevo, uno faltante, o
    cualquier diferencia del set (aunque el `COUNT` coincida) marca ese
    job como `JOB_SKILLS_DRIFT` y lo EXCLUYE del borrado de este batch,
    SIN afectar a los demás jobs del batch (granularidad por job, no por
    batch completo) ni mezclarse con `material_drift` de las 23 columnas
    (métrica separada, para no esconder el hallazgo).

    Solo el conjunto final (columnas seguras Y `job_skills` exactos) se
    borra si `write=True` (`DELETE FROM jobs WHERE id = ANY(%s)`), dentro
    de su propia transacción con commit inmediato
    (`_write_batch_transaction`) — `job_skills.job_id → jobs.id ON DELETE
    CASCADE` elimina sus vínculos automáticamente, sin DELETE manual de
    `job_skills`. Si `write=False` (`--delete-plan`), solo SELECT.

    TOCTOU cerrado con DOS locks, solo en `write=True`:
      1. `classify_jobs_batch(..., for_update=True)` bloquea las filas de
         `jobs` del batch — la clasificación de columnas (incluida
         benign/material) se recalcula sobre la fila ya bloqueada.
      2. `JOB_SKILLS_BY_JOB_IDS_FOR_UPDATE_QUERY` bloquea, SOLO para los
         jobs que ya son seguros por columnas, sus filas de `job_skills`
         — nunca las 214K a la vez. PostgreSQL exige que un `INSERT`
         referenciando una fila de `jobs` adquiera `FOR KEY SHARE` sobre
         ella; como ya tenemos `FOR UPDATE` (incompatible con `FOR KEY
         SHARE`) desde el paso 1, un `INSERT INTO job_skills` concurrente
         para ese job queda bloqueado desde que adquirimos el lock de
         `jobs` — cierra la ventana de "link nuevo colándose" incluso
         antes del lock de `job_skills`. El lock de `job_skills` en sí
         cierra la ventana de "link existente borrado por otra vía"
         (aunque hoy no existe ningún DELETE de `job_skills` en el
         repositorio salvo un script SQL manual sin automatizar — ver
         PROJECT_MASTER_CONTEXT.md §103). La comparación de sets se hace
         DESPUÉS de adquirir ambos locks, nunca reutilizando un
         `--delete-plan` previo: si un link cambia justo al adquirir el
         lock, la lectura ya bloqueada es la que se compara.
    `write=False` (`--delete-plan`) nunca bloquea nada — compara los
    mismos sets con `SELECT` normal, solo diagnóstico.
    """
    backup_skills_by_job = _load_backup_skills_by_job(job_skills_path)

    total = already_missing = deletable = benign = material = 0
    js_exact_match_jobs = js_drift_jobs = 0
    backup_links_considered = current_links_considered = 0
    job_skills_cascade = 0
    drift_sample: list[dict] = []  # SOLO material_drift de columnas
    js_drift_sample: list[dict] = []  # SOLO job_skills drift

    for batch in _batched(_read_jsonl_gz(jobs_path), batch_size):
        total += len(batch)
        if write:
            with _write_batch_transaction(conn):
                with conn.cursor() as cur:
                    missing_rows, identical_ids, conflict_ids, conflicts = classify_jobs_batch(
                        cur, columns, batch, for_update=True
                    )
                    benign_ids = {c["id"] for c in conflicts if _is_benign_deactivation(c["diffs"])}
                    material_conflicts = [c for c in conflicts if c["id"] not in benign_ids]
                    column_safe_ids = identical_ids | benign_ids

                    final_safe_ids: set[int] = set()
                    if column_safe_ids:
                        cur.execute(
                            JOB_SKILLS_BY_JOB_IDS_FOR_UPDATE_QUERY, (list(column_safe_ids),)
                        )
                        current_by_job = _group_pairs_by_job(cur.fetchall())
                        exact_ids, js_drift_ids = _split_by_job_skills_match(
                            column_safe_ids, backup_skills_by_job, current_by_job
                        )
                        final_safe_ids = exact_ids
                        js_exact_match_jobs += len(exact_ids)
                        js_drift_jobs += len(js_drift_ids)
                        backup_links_considered += sum(
                            len(backup_skills_by_job.get(j, ())) for j in column_safe_ids
                        )
                        current_links_considered += sum(
                            len(current_by_job.get(j, ())) for j in column_safe_ids
                        )
                        job_skills_cascade += sum(len(current_by_job.get(j, ())) for j in exact_ids)
                        room = MAX_CONFLICT_SAMPLES - len(js_drift_sample)
                        if room > 0:
                            for j in list(js_drift_ids)[:room]:
                                js_drift_sample.append(
                                    {
                                        "id": j,
                                        "backup_skill_ids": sorted(backup_skills_by_job.get(j, ())),
                                        "current_skill_ids": sorted(current_by_job.get(j, ())),
                                    }
                                )

                    if final_safe_ids:
                        cur.execute(DELETE_JOBS_BY_IDS_QUERY, (list(final_safe_ids),))
        else:
            with conn.cursor() as cur:
                missing_rows, identical_ids, conflict_ids, conflicts = classify_jobs_batch(
                    cur, columns, batch
                )
                benign_ids = {c["id"] for c in conflicts if _is_benign_deactivation(c["diffs"])}
                material_conflicts = [c for c in conflicts if c["id"] not in benign_ids]
                column_safe_ids = identical_ids | benign_ids

                if column_safe_ids:
                    cur.execute(JOB_SKILLS_BY_JOB_IDS_QUERY, (list(column_safe_ids),))
                    current_by_job = _group_pairs_by_job(cur.fetchall())
                    exact_ids, js_drift_ids = _split_by_job_skills_match(
                        column_safe_ids, backup_skills_by_job, current_by_job
                    )
                    js_exact_match_jobs += len(exact_ids)
                    js_drift_jobs += len(js_drift_ids)
                    backup_links_considered += sum(
                        len(backup_skills_by_job.get(j, ())) for j in column_safe_ids
                    )
                    current_links_considered += sum(
                        len(current_by_job.get(j, ())) for j in column_safe_ids
                    )
                    job_skills_cascade += sum(len(current_by_job.get(j, ())) for j in exact_ids)
                    room = MAX_CONFLICT_SAMPLES - len(js_drift_sample)
                    if room > 0:
                        for j in list(js_drift_ids)[:room]:
                            js_drift_sample.append(
                                {
                                    "id": j,
                                    "backup_skill_ids": sorted(backup_skills_by_job.get(j, ())),
                                    "current_skill_ids": sorted(current_by_job.get(j, ())),
                                }
                            )

        already_missing += len(missing_rows)
        deletable += len(identical_ids)
        benign += len(benign_ids)
        material += len(material_conflicts)
        room = MAX_CONFLICT_SAMPLES - len(drift_sample)
        if room > 0:
            drift_sample.extend(material_conflicts[:room])
        logger.info(
            "jobs: %d procesados (already_missing=%d identical=%d benign=%d "
            "material_drift=%d js_exact=%d js_drift=%d)",
            total,
            already_missing,
            deletable,
            benign,
            material,
            js_exact_match_jobs,
            js_drift_jobs,
        )

    return {
        "total": total,
        "already_missing": already_missing,
        "deletable": deletable,
        "benign_deactivation": benign,
        "material_drift": material,
        "drift_sample": drift_sample,
        "job_skills_exact_match_jobs": js_exact_match_jobs,
        "job_skills_drift_jobs": js_drift_jobs,
        "job_skills_drift_sample": js_drift_sample,
        "backup_links": backup_links_considered,
        "current_links": current_links_considered,
        "job_skills_cascade": job_skills_cascade,
    }


def _delete_plan_dict(result: dict) -> dict:
    """
    Forma de salida compartida entre build_delete_plan() y run_delete().
    `safe_delete_total` es el número REAL de jobs que se borran/borrarían
    — exige columnas seguras (identical o benign) Y job_skills exactos;
    nunca cuenta un job cuyos links no coinciden, aunque sea
    columna-seguro.
    """
    return {
        "candidate_ids_total": result["total"],
        "already_missing": result["already_missing"],
        "existing_identical": result["deletable"],
        "benign_deactivation": result["benign_deactivation"],
        "material_drift": result["material_drift"],
        "drift_sample": result["drift_sample"],
        "job_skills_exact_match_jobs": result["job_skills_exact_match_jobs"],
        "job_skills_drift_jobs": result["job_skills_drift_jobs"],
        "job_skills_drift_sample": result["job_skills_drift_sample"],
        "backup_links": result["backup_links"],
        "current_links": result["current_links"],
        "safe_delete_total": result["job_skills_exact_match_jobs"],
        "job_skills_cascade": result["job_skills_cascade"],
    }


def build_delete_plan(conn, manifest: dict, backup_dir: Path, batch_size: int) -> dict:
    """
    Modo READ-ONLY (`--delete-plan`): calcula exactamente qué borraría un
    DELETE real usando los `candidate_ids`/`jobs` de ESTE manifiesto como
    única autoridad — nunca recalcula `NOW() - retention_days`, nunca
    busca "el backup más reciente". Una única transacción REPEATABLE READ
    (`snapshot_transaction`), igual que dry-run/`--restore-plan`. Nunca usa
    `conn.set_session(readonly=True)`, nunca `FOR UPDATE` (ni en `jobs` ni
    en `job_skills`) — es solo diagnóstico.
    """
    columns = manifest["jobs_columns"]
    jobs_path = _backup_file_path(backup_dir, manifest, "jobs")
    job_skills_path = _backup_file_path(backup_dir, manifest, "job_skills")

    with snapshot_transaction(conn):
        result = _process_jobs_delete(conn, columns, jobs_path, job_skills_path, batch_size, write=False)

    return _delete_plan_dict(result)


def run_delete(conn, manifest: dict, backup_dir: Path, batch_size: int) -> dict:
    """
    DELETE REAL: `DELETE FROM jobs WHERE id = ANY(%s)`, batch a batch, cada
    batch en su propia transacción con commit inmediato
    (`_process_jobs_delete` / `_write_batch_transaction`) — mismo patrón
    que `run_restore()`/`enrich_jobs()`. Borra candidatos SAFE_IDENTICAL o
    SAFE_BENIGN_DEACTIVATION (ver `_is_benign_deactivation`) CUYOS
    `job_skills` coinciden exactamente con el backup; un candidato ya
    inexistente es no-op idempotente; cualquier MATERIAL_DRIFT de columnas
    o JOB_SKILLS_DRIFT NUNCA se borra. Clasifica con `FOR UPDATE` en
    `jobs` y en `job_skills` (bloqueo por fila, acotado al batch y solo
    para los jobs ya seguros por columnas): la fila/links que se comparan
    son los MISMOS que se borran, sin ventana en la que otra transacción
    pueda modificarlos entre medias — la clasificación completa se
    recalcula sobre el estado ya bloqueado, nunca sobre un `--delete-plan`
    previo. `ON DELETE CASCADE` en `job_skills.job_id` elimina sus
    vínculos automáticamente — no hay DELETE manual de `job_skills`, y
    `skills` nunca se toca.

    Si se pierde la conexión a mitad, los batches ya commiteados quedan
    borrados tal cual; relanzar el mismo comando reclasifica todo desde
    cero — los ya borrados cuentan como `already_missing` (no-op), el
    resto se procesa con normalidad, y material_drift/job_skills_drift
    siguen protegidos.
    """
    columns = manifest["jobs_columns"]
    jobs_path = _backup_file_path(backup_dir, manifest, "jobs")
    job_skills_path = _backup_file_path(backup_dir, manifest, "job_skills")
    result = _process_jobs_delete(conn, columns, jobs_path, job_skills_path, batch_size, write=True)
    return _delete_plan_dict(result)


def print_delete_report(plan: dict, *, executed: bool) -> None:
    """Formato compartido entre `--delete-plan` (hipotético) y `--delete` (ya ejecutado)."""
    title = "DELETE — ejecutado" if executed else "DELETE-PLAN — READ-ONLY, nada borrado"
    print("=" * 78)
    print(f"{title} (data_retention.py)")
    print("=" * 78)
    print(f"candidate_ids del backup: {plan['candidate_ids_total']}")
    print(f"  ya no existían (already_missing, no-op):        {plan['already_missing']}")
    print(f"  --- clasificación por columnas (23 columnas) ---")
    print(f"  idénticos (SAFE_IDENTICAL):                     {plan['existing_identical']}")
    print(f"  is_active TRUE->FALSE, resto igual (SAFE_BENIGN_DEACTIVATION): "
          f"{plan['benign_deactivation']}")
    print(f"  MATERIAL DRIFT de columnas (NUNCA borrados):    {plan['material_drift']}")
    print(f"  --- de los seguros por columnas, comparación exacta de job_skills ---")
    print(f"  job_skills EXACTOS (elegibles para borrar):     {plan['job_skills_exact_match_jobs']}")
    print(f"  JOB_SKILLS_DRIFT (bloqueados, NUNCA borrados):  {plan['job_skills_drift_jobs']}")
    print(f"  backup_links considerados:                      {plan['backup_links']}")
    print(f"  current_links considerados:                     {plan['current_links']}")
    verbo = "borrados" if executed else "se borrarían"
    print(f"  --- TOTAL ---")
    print(f"  TOTAL seguro (columnas Y job_skills exactos), {verbo}: {plan['safe_delete_total']}")
    verbo_js = "eliminados por CASCADE" if executed else "se eliminarían por CASCADE"
    print(f"  job_skills {verbo_js}: {plan['job_skills_cascade']}")
    if plan["drift_sample"]:
        print(
            f"Muestra de MATERIAL DRIFT de columnas (hasta {MAX_CONFLICT_SAMPLES}, de "
            f"{plan['material_drift']} totales):"
        )
        for c in plan["drift_sample"][:10]:
            cols = [d[0] for d in c["diffs"]]
            print(f"  job id={c['id']}: {len(c['diffs'])} columna(s) distinta(s) -> {cols}")
    if plan["job_skills_drift_sample"]:
        print(
            f"Muestra de JOB_SKILLS_DRIFT (hasta {MAX_CONFLICT_SAMPLES}, de "
            f"{plan['job_skills_drift_jobs']} totales):"
        )
        for d in plan["job_skills_drift_sample"][:10]:
            print(
                f"  job id={d['id']}: backup_skill_ids={d['backup_skill_ids']} "
                f"current_skill_ids={d['current_skill_ids']}"
            )
    print("=" * 78)


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
    parser.add_argument(
        "--restore-plan",
        type=str,
        default=None,
        metavar="MANIFEST",
        help="READ-ONLY: calcula qué haría un restore real (inserts/conflictos/links "
        "faltantes) contra el estado actual, sin escribir nada. Solo SELECT.",
    )
    parser.add_argument(
        "--restore",
        type=str,
        default=None,
        metavar="MANIFEST",
        help="Restaura jobs/job_skills desde un backup ya verificado. Nunca sobrescribe filas "
        "existentes (ON CONFLICT DO NOTHING); un job existente pero distinto al backup se deja "
        "como conflicto, sin tocar. Requiere --confirm-restore explícito.",
    )
    parser.add_argument(
        "--confirm-restore",
        action="store_true",
        help="Opt-in obligatorio para que --restore escriba de verdad. Sin este flag, --restore "
        "no hace nada — ni siquiera abre conexión a Supabase.",
    )
    parser.add_argument(
        "--restore-batch-size",
        type=int,
        default=RESTORE_BATCH_SIZE_DEFAULT,
        help=f"Filas por batch/transacción en el restore (por defecto {RESTORE_BATCH_SIZE_DEFAULT}).",
    )
    parser.add_argument(
        "--delete-plan",
        type=str,
        default=None,
        metavar="MANIFEST",
        help="READ-ONLY: calcula qué borraría un DELETE real (already_missing/deletable/"
        "drift) usando los candidate_ids de este manifiesto como única autoridad — nunca "
        "recalcula NOW()-retention_days. Solo SELECT.",
    )
    parser.add_argument(
        "--delete",
        type=str,
        default=None,
        metavar="MANIFEST",
        help="Borra de jobs los candidatos de este backup que sigan existiendo e idénticos "
        "(DELETE ... WHERE id = ANY(...), ON DELETE CASCADE elimina sus job_skills). Nunca "
        "borra un job con drift respecto al backup. Requiere --confirm-delete explícito.",
    )
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Opt-in obligatorio para que --delete borre de verdad. Sin este flag, --delete "
        "no hace nada — ni siquiera abre conexión a Supabase.",
    )
    parser.add_argument(
        "--delete-batch-size",
        type=int,
        default=DELETE_BATCH_SIZE_DEFAULT,
        help=f"Filas por batch/transacción en el delete (por defecto {DELETE_BATCH_SIZE_DEFAULT}).",
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


def _load_verified_manifest(manifest_arg: str, *, action_label: str) -> dict | None:
    """
    Ejecuta `verify_backup()` sobre `manifest_arg` y, si pasa, devuelve el
    manifiesto ya parseado. Si falla, imprime los problemas y devuelve None
    — el llamador debe abortar (`sys.exit`) sin abrir conexión a Supabase.
    Compartido por --restore-plan y --restore: ninguno de los dos escribe
    nada si el backup de origen no pasa --verify primero.
    """
    manifest_path = Path(manifest_arg)
    problems = verify_backup(manifest_path)
    if problems:
        print(f"{action_label} ABORTADO: el backup no pasa --verify:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8"))


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

    if args.restore_plan and args.restore:
        print("ERROR: usa --restore-plan o --restore, no ambos a la vez.", file=sys.stderr)
        sys.exit(2)

    if args.restore_plan:
        manifest_path = Path(args.restore_plan)
        manifest = _load_verified_manifest(args.restore_plan, action_label="RESTORE-PLAN")
        if manifest is None:
            sys.exit(1)
        conn = _get_connection()
        try:
            plan = build_restore_plan(conn, manifest, manifest_path.parent, args.restore_batch_size)
        finally:
            conn.close()
        print_restore_report(plan, executed=False)
        return

    if args.restore:
        if not args.confirm_restore:
            print(
                "ERROR: --restore requiere --confirm-restore explícito. Sin él, no se abre "
                "conexión a Supabase ni se escribe nada. Usa --restore-plan para ver qué haría.",
                file=sys.stderr,
            )
            sys.exit(2)
        manifest_path = Path(args.restore)
        manifest = _load_verified_manifest(args.restore, action_label="RESTORE")
        if manifest is None:
            sys.exit(1)
        conn = _get_connection()
        try:
            result = run_restore(conn, manifest, manifest_path.parent, args.restore_batch_size)
        finally:
            conn.close()
        print_restore_report(result, executed=True)
        return

    if args.delete_plan and args.delete:
        print("ERROR: usa --delete-plan o --delete, no ambos a la vez.", file=sys.stderr)
        sys.exit(2)

    if args.delete_plan:
        manifest_path = Path(args.delete_plan)
        manifest = _load_verified_manifest(args.delete_plan, action_label="DELETE-PLAN")
        if manifest is None:
            sys.exit(1)
        conn = _get_connection()
        try:
            plan = build_delete_plan(conn, manifest, manifest_path.parent, args.delete_batch_size)
        finally:
            conn.close()
        print_delete_report(plan, executed=False)
        return

    if args.delete:
        if not args.confirm_delete:
            print(
                "ERROR: --delete requiere --confirm-delete explícito. Sin él, no se abre "
                "conexión a Supabase ni se borra nada. Usa --delete-plan para ver qué haría.",
                file=sys.stderr,
            )
            sys.exit(2)
        manifest_path = Path(args.delete)
        manifest = _load_verified_manifest(args.delete, action_label="DELETE")
        if manifest is None:
            sys.exit(1)
        conn = _get_connection()
        try:
            result = run_delete(conn, manifest, manifest_path.parent, args.delete_batch_size)
        finally:
            conn.close()
        print_delete_report(result, executed=True)
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
