"""
Tests de scripts/data_retention.py.

Ninguno de estos tests toca Supabase real: la serialización, la
canonicalización, el export a JSONL.GZ y verify_backup() son funciones
puras/basadas en archivos temporales; la validación de schema usa un cursor
falso; los tests de CLI parchean las funciones que sí requieren conexión.
"""

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import data_retention as dr


# =============================================================================
# _json_value / canonical_line — serialización sin BD
# =============================================================================


def test_json_value_none_is_null():
    assert dr._json_value(None) is None


def test_json_value_bool_stays_bool():
    assert dr._json_value(True) is True
    assert dr._json_value(False) is False


def test_json_value_int_stays_int():
    assert dr._json_value(5892737565) == 5892737565
    assert isinstance(dr._json_value(5892737565), int)


def test_json_value_negative_int():
    assert dr._json_value(-42) == -42


def test_json_value_str_unicode_quotes_newlines():
    text = 'Título con "comillas", ñ/é/ü, salto\nde línea y emoji 🚀'
    assert dr._json_value(text) == text


def test_json_value_datetime_isoformat_utc():
    dt = datetime(2026, 7, 23, 18, 54, 55, tzinfo=timezone.utc)
    assert dr._json_value(dt) == "2026-07-23T18:54:55+00:00"


def test_json_value_unsupported_type_raises():
    class Weird:
        pass

    with pytest.raises(TypeError):
        dr._json_value(Weird())


def test_json_value_does_not_silently_stringify_float():
    # No hay columnas float reales en jobs hoy -- si alguna vez aparece una,
    # queremos que falle de forma visible, no que se convierta en texto.
    with pytest.raises(TypeError):
        dr._json_value(3.14)


def test_canonical_line_same_content_different_dict_order():
    row_a = {"id": 1, "title": "Data Engineer", "remote": None}
    row_b = {"remote": None, "id": 1, "title": "Data Engineer"}
    assert dr.canonical_line(row_a) == dr.canonical_line(row_b)


def test_canonical_line_changed_field_changes_line():
    row_a = {"id": 1, "title": "Data Engineer"}
    row_b = {"id": 1, "title": "Backend Engineer"}
    assert dr.canonical_line(row_a) != dr.canonical_line(row_b)


def test_canonical_line_is_valid_json():
    row = {"id": 1, "posted_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "company": None}
    parsed = json.loads(dr.canonical_line(row))
    assert parsed == {"id": 1, "posted_at": "2026-01-01T00:00:00+00:00", "company": None}


# =============================================================================
# export_rows_to_jsonl_gz — streaming a archivo, determinismo, hashes
# =============================================================================


def _read_gz_lines(path: Path) -> list[dict]:
    raw = gzip.decompress(path.read_bytes())
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]


def test_export_rows_writes_expected_content(tmp_path):
    rows = [(1, "Data Engineer", None), (2, "Backend", True)]
    columns = ["id", "title", "remote"]
    path = tmp_path / "jobs_test.jsonl.gz"

    result = dr.export_rows_to_jsonl_gz(rows, columns, path)

    assert result["count"] == 2
    lines = _read_gz_lines(path)
    assert lines == [
        {"id": 1, "title": "Data Engineer", "remote": None},
        {"id": 2, "title": "Backend", "remote": True},
    ]


def test_export_rows_empty_iterable(tmp_path):
    result = dr.export_rows_to_jsonl_gz([], ["id"], tmp_path / "empty.jsonl.gz")
    assert result["count"] == 0
    assert _read_gz_lines(tmp_path / "empty.jsonl.gz") == []


def test_export_rows_deterministic_hashes_same_content(tmp_path):
    rows = [(1, "A"), (2, "B"), (3, "C")]
    columns = ["id", "name"]

    r1 = dr.export_rows_to_jsonl_gz(rows, columns, tmp_path / "a.jsonl.gz")
    r2 = dr.export_rows_to_jsonl_gz(rows, columns, tmp_path / "b.jsonl.gz")

    # mismo contenido -> mismo hash de contenido Y mismo hash de archivo
    # (gzip con mtime=0 hace que los bytes .gz tambien sean deterministas).
    assert r1["sha256_content"] == r2["sha256_content"]
    assert r1["sha256_file"] == r2["sha256_file"]
    assert r1["bytes_raw"] == r2["bytes_raw"]


def test_export_rows_changed_row_changes_content_hash(tmp_path):
    columns = ["id", "name"]
    r1 = dr.export_rows_to_jsonl_gz([(1, "A")], columns, tmp_path / "a.jsonl.gz")
    r2 = dr.export_rows_to_jsonl_gz([(1, "B")], columns, tmp_path / "b.jsonl.gz")
    assert r1["sha256_content"] != r2["sha256_content"]
    assert r1["sha256_file"] != r2["sha256_file"]


def test_export_rows_sha256_file_matches_actual_file_bytes(tmp_path):
    path = tmp_path / "x.jsonl.gz"
    result = dr.export_rows_to_jsonl_gz([(1,)], ["id"], path)
    import hashlib

    assert result["sha256_file"] == hashlib.sha256(path.read_bytes()).hexdigest()


# =============================================================================
# validated_jobs_columns — schema real vs. baseline esperado (cursor falso)
# =============================================================================


class _FakeCursor:
    def __init__(self, columns_rows):
        self._rows = columns_rows

    def execute(self, *args, **kwargs):
        pass

    def fetchall(self):
        return self._rows


def _baseline_rows():
    return [(name, dtype) for name, dtype in dr.EXPECTED_JOBS_COLUMNS.items()]


def test_validated_columns_matches_baseline_exactly():
    cur = _FakeCursor(_baseline_rows())
    columns, warnings = dr.validated_jobs_columns(cur)
    assert columns == list(dr.EXPECTED_JOBS_COLUMNS.keys())
    assert warnings == []


def test_validated_columns_missing_column_raises():
    rows = [r for r in _baseline_rows() if r[0] != "description_full"]
    cur = _FakeCursor(rows)
    with pytest.raises(dr.SchemaMismatchError):
        dr.validated_jobs_columns(cur)


def test_validated_columns_type_mismatch_raises():
    rows = [
        (name, "text" if name == "salary_mid" else dtype)
        for name, dtype in _baseline_rows()
    ]
    cur = _FakeCursor(rows)
    with pytest.raises(dr.SchemaMismatchError):
        dr.validated_jobs_columns(cur)


def test_validated_columns_extra_column_warns_but_does_not_raise():
    rows = _baseline_rows() + [("nueva_columna", "text")]
    cur = _FakeCursor(rows)
    columns, warnings = dr.validated_jobs_columns(cur)
    assert "nueva_columna" in columns
    assert len(warnings) == 1
    assert "nueva_columna" in warnings[0]


def test_validated_columns_no_real_columns_raises():
    cur = _FakeCursor([])
    with pytest.raises(dr.SchemaMismatchError):
        dr.validated_jobs_columns(cur)


# =============================================================================
# verify_backup — end to end sobre un backup sintético en tmp_path
# =============================================================================


def _build_tiny_backup(tmp_path, *, corrupt_job_skills=False, drop_candidate_id=False):
    ts = "20260921T190000Z"
    out_dir = tmp_path / f"retention_{ts}"

    jobs_rows = [(1, "Data Engineer"), (2, "Backend Engineer"), (3, "QA")]
    job_skills_rows = [(1, 10), (1, 11), (2, 10)]
    skills_rows = [(10, "Python", "language"), (11, "SQL", "language")]
    candidate_rows = [(1,), (2,), (3,)]
    if drop_candidate_id:
        candidate_rows = [(1,), (2,)]  # falta el id=3 que si esta en jobs
    if corrupt_job_skills:
        job_skills_rows.append((1, 10))  # duplicado (job_id, skill_id)

    file_results = {}
    file_paths = {}

    file_paths["jobs"] = out_dir / f"jobs_{ts}.jsonl.gz"
    file_results["jobs"] = dr.export_rows_to_jsonl_gz(jobs_rows, ["id", "title"], file_paths["jobs"])

    file_paths["job_skills"] = out_dir / f"job_skills_{ts}.jsonl.gz"
    file_results["job_skills"] = dr.export_rows_to_jsonl_gz(
        job_skills_rows, dr.JOB_SKILLS_COLUMNS, file_paths["job_skills"]
    )

    file_paths["skills_snapshot"] = out_dir / f"skills_snapshot_{ts}.jsonl.gz"
    file_results["skills_snapshot"] = dr.export_rows_to_jsonl_gz(
        skills_rows, dr.SKILLS_COLUMNS, file_paths["skills_snapshot"]
    )

    file_paths["candidate_ids"] = out_dir / f"candidate_ids_{ts}.jsonl.gz"
    file_results["candidate_ids"] = dr.export_rows_to_jsonl_gz(
        candidate_rows, dr.CANDIDATE_IDS_COLUMNS, file_paths["candidate_ids"]
    )

    manifest = {
        "format_version": "1.0",
        "jobs_count": file_results["jobs"]["count"],
        "job_skills_count": file_results["job_skills"]["count"],
        "skills_count": file_results["skills_snapshot"]["count"],
        "candidate_ids_count": file_results["candidate_ids"]["count"],
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
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def test_verify_backup_clean_set_has_no_problems(tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    assert dr.verify_backup(manifest_path) == []


def test_verify_backup_detects_duplicate_job_skills(tmp_path):
    manifest_path = _build_tiny_backup(tmp_path, corrupt_job_skills=True)
    problems = dr.verify_backup(manifest_path)
    assert any("duplicados" in p and "job_skills" in p for p in problems)


def test_verify_backup_detects_candidate_ids_mismatch(tmp_path):
    manifest_path = _build_tiny_backup(tmp_path, drop_candidate_id=True)
    problems = dr.verify_backup(manifest_path)
    assert any("candidate_ids no coincide" in p for p in problems)


def test_verify_backup_detects_missing_file(tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    # Borramos uno de los archivos referenciados por el manifiesto.
    (manifest_path.parent / "skills_snapshot_20260921T190000Z.jsonl.gz").unlink()
    problems = dr.verify_backup(manifest_path)
    assert any("no encontrado" in p for p in problems)


def test_verify_backup_detects_corrupted_file_hash(tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    target = manifest_path.parent / "jobs_20260921T190000Z.jsonl.gz"
    corrupted = bytearray(target.read_bytes())
    corrupted[-1] ^= 0xFF  # flip de un byte al final, sigue siendo gzip valido en la mayoria de casos
    target.write_bytes(bytes(corrupted))
    problems = dr.verify_backup(manifest_path)
    assert any("sha256" in p for p in problems)


def test_verify_backup_detects_wrong_count_in_manifest(tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["jobs_count"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    problems = dr.verify_backup(manifest_path)
    assert any("jobs" in p and "999" in p for p in problems)


def test_verify_backup_does_not_require_database_url(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    manifest_path = _build_tiny_backup(tmp_path)
    # Si verify_backup intentase conectar a Supabase, esto fallaria por falta
    # de DATABASE_URL. El hecho de que pase confirma que no lo necesita.
    assert dr.verify_backup(manifest_path) == []


# =============================================================================
# _capture_cutoff — override no toca Supabase, sin override consulta NOW()
# =============================================================================


class _BoomConn:
    """Conexión falsa que revienta si algo intenta abrir un cursor: prueba
    de que, con override, _capture_cutoff no toca Supabase en absoluto."""

    def cursor(self, *a, **kw):
        raise AssertionError("_capture_cutoff no deberia abrir un cursor con override")


class _FakeNowCursor:
    def __init__(self, now_value):
        self._now_value = now_value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        assert sql == dr.NOW_QUERY

    def fetchone(self):
        return (self._now_value,)


class _FakeNowConn:
    def __init__(self, now_value):
        self._now_value = now_value

    def cursor(self, *a, **kw):
        return _FakeNowCursor(self._now_value)


def test_capture_cutoff_with_override_does_not_touch_connection():
    override = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert dr._capture_cutoff(_BoomConn(), override) == override


def test_capture_cutoff_without_override_queries_postgres_now():
    now_value = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    conn = _FakeNowConn(now_value)
    assert dr._capture_cutoff(conn, None) == now_value


# =============================================================================
# _atomic_backup_dir — publicación atómica del directorio de backup
# =============================================================================


def test_atomic_backup_dir_renames_tmp_to_final_on_success(tmp_path):
    ts = "20260921T120000Z"
    with dr._atomic_backup_dir(tmp_path, ts) as out_dir:
        assert out_dir.name == f".retention_{ts}.incomplete"
        (out_dir / "manifest.json").write_text("{}", encoding="utf-8")

    final_dir = tmp_path / f"retention_{ts}"
    assert final_dir.exists()
    assert (final_dir / "manifest.json").exists()
    assert not (tmp_path / f".retention_{ts}.incomplete").exists()


def test_atomic_backup_dir_removes_tmp_dir_on_exception(tmp_path):
    ts = "20260921T120000Z"
    with pytest.raises(RuntimeError):
        with dr._atomic_backup_dir(tmp_path, ts) as out_dir:
            (out_dir / "jobs.jsonl.gz").write_text("parcial", encoding="utf-8")
            raise RuntimeError("conexion perdida a mitad del backup")

    assert not (tmp_path / f"retention_{ts}").exists()
    assert not (tmp_path / f".retention_{ts}.incomplete").exists()


def test_atomic_backup_dir_refuses_to_overwrite_existing_final(tmp_path):
    ts = "20260921T120000Z"
    final_dir = tmp_path / f"retention_{ts}"
    final_dir.mkdir()
    (final_dir / "manifest.json").write_text("ya existe", encoding="utf-8")

    with pytest.raises(FileExistsError):
        with dr._atomic_backup_dir(tmp_path, ts):
            pass

    # El backup previo, ya completo, no se toca.
    assert (final_dir / "manifest.json").read_text(encoding="utf-8") == "ya existe"


def test_atomic_backup_dir_clears_stale_incomplete_dir_before_starting(tmp_path):
    ts = "20260921T120000Z"
    stale_tmp = tmp_path / f".retention_{ts}.incomplete"
    stale_tmp.mkdir()
    (stale_tmp / "restos_de_un_intento_anterior.jsonl.gz").write_text("basura", encoding="utf-8")

    with dr._atomic_backup_dir(tmp_path, ts) as out_dir:
        assert list(out_dir.iterdir()) == []

    final_dir = tmp_path / f"retention_{ts}"
    assert [p.name for p in final_dir.iterdir()] == []


# =============================================================================
# restore — clasificacion, batching, idempotencia, sobre un fake fiel
# =============================================================================
#
# No hay contrasena conocida del PostgreSQL local detectado en esta maquina
# (puerto 5432, instalacion previa ajena a este proyecto) y no se ha
# intentado averiguarla -- seria un acceso indebido al sistema del usuario.
# Tocar Supabase real para testear INSERT esta fuera de alcance este turno.
# Estos fakes son deliberadamente fieles a las consultas EXACTAS que emite
# el modulo (mismas constantes SQL), pero no sustituyen una prueba contra
# Postgres real: no verifican quoting/escaping real de psycopg2, ni FKs, ni
# constraints reales de Supabase. Limitacion documentada tambien en
# PROJECT_MASTER_CONTEXT.md.


class _FakeRestoreDB:
    """Estado en memoria: jobs (dict id->dict de columnas), job_skills
    (set de pares) y skills (set de ids) -- suficiente para ejercitar la
    logica real de clasificacion/batching/idempotencia sin Postgres.

    `fail_on_delete_call` (1-indexado) permite simular una desconexion a
    mitad de un DELETE concreto, para probar que el batch en curso hace
    rollback y los batches anteriores ya commiteados permanecen borrados.

    `on_for_update_lock` (callback opcional, recibe el set de ids del
    batch) se invoca exactamente cuando se ejecuta un SELECT ... FOR
    UPDATE sobre `jobs` -- el instante en que Postgres bloquearia esas
    filas frente a escritores concurrentes. Sirve para simular una
    escritura concurrente justo en ese momento (ver test de TOCTOU) sin
    construir un simulador de Postgres completo: el callback puede mutar
    `self.jobs` antes de que el propio SELECT calcule su resultado, igual
    que ocurriria si Postgres nos entregase la fila ya actualizada al
    conceder el lock.

    `on_job_skills_for_update_lock` (mismo patron, para el SELECT ... FOR
    UPDATE de `job_skills`) permite simular una insercion/eliminacion
    concurrente de un link justo en el instante en que se bloquean las
    filas de job_skills del job -- el callback puede mutar
    `self.job_skills` antes de que el SELECT calcule su resultado."""

    def __init__(self, jobs=None, job_skills=None, skills=None):
        self.jobs: dict = {int(k): dict(v) for k, v in (jobs or {}).items()}
        self.job_skills: set = set(job_skills or set())
        self.skills: set = set(skills or set())
        self.delete_calls = 0
        self.fail_on_delete_call: int | None = None
        self.on_for_update_lock = None
        self.on_job_skills_for_update_lock = None

    def insert_job(self, row: dict) -> None:
        self.jobs.setdefault(row["id"], dict(row))  # ON CONFLICT (id) DO NOTHING

    def insert_job_skill(self, pair) -> None:
        self.job_skills.add(tuple(pair))  # ON CONFLICT (job_id, skill_id) DO NOTHING

    def delete_jobs(self, ids) -> None:
        ids = set(ids)
        for i in ids:
            self.jobs.pop(i, None)
        self.job_skills = {(j, s) for j, s in self.job_skills if j not in ids}  # ON DELETE CASCADE


class _FakeRestoreCursor:
    def __init__(self, db: _FakeRestoreDB, conn=None):
        self.db = db
        self.conn = conn
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql in (dr.SET_TIMEZONE_SQL, dr.SET_ISOLATION_SQL):
            self._result = []
        elif sql.startswith("SELECT") and " FROM jobs WHERE id = ANY" in sql:
            ids = params[0]
            if sql.rstrip().endswith("FOR UPDATE") and self.db.on_for_update_lock is not None:
                # Momento exacto en que Postgres concederia el lock: una
                # escritura concurrente simulada aqui debe ser lo que
                # veamos al "leer bajo bloqueo", no una version anterior.
                self.db.on_for_update_lock(set(ids))
            cols_part = sql[len("SELECT "): sql.index(" FROM jobs")]
            cols = [c.strip() for c in cols_part.split(",")]
            self._result = [
                tuple(self.db.jobs[i][c] for c in cols) for i in ids if i in self.db.jobs
            ]
        elif sql == dr.JOB_SKILLS_BY_JOB_IDS_QUERY:
            ids = set(params[0])
            self._result = [pair for pair in self.db.job_skills if pair[0] in ids]
        elif sql == dr.JOB_SKILLS_BY_JOB_IDS_FOR_UPDATE_QUERY:
            ids = set(params[0])
            if self.db.on_job_skills_for_update_lock is not None:
                # Momento exacto en que Postgres bloquearia estas filas de
                # job_skills: una escritura concurrente simulada aqui debe
                # ser lo que veamos al leer bajo bloqueo.
                self.db.on_job_skills_for_update_lock(ids)
            self._result = [pair for pair in self.db.job_skills if pair[0] in ids]
        elif sql == dr.SKILLS_BY_IDS_QUERY:
            ids = set(params[0])
            self._result = [(sid,) for sid in ids if sid in self.db.skills]
        elif sql == dr.DELETE_JOBS_BY_IDS_QUERY:
            self.db.delete_calls += 1
            if self.db.fail_on_delete_call == self.db.delete_calls:
                raise RuntimeError("conexion perdida durante DELETE")
            ids = set(params[0])
            if self.conn is not None:
                # Igual que Postgres real: no se aplica hasta el COMMIT de
                # la transaccion del batch (ver _write_batch_transaction).
                if self.conn._pending_delete_ids is None:
                    self.conn._pending_delete_ids = set()
                self.conn._pending_delete_ids |= ids
            else:
                self.db.delete_jobs(ids)
            self._result = []
        else:
            raise AssertionError(f"query no soportada por el fake: {sql!r}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class _FakeRestoreConn:
    def __init__(self, db: _FakeRestoreDB):
        self.db = db
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self._pending_delete_ids: set | None = None

    def cursor(self, *a, **kw):
        return _FakeRestoreCursor(self.db, self)

    def commit(self):
        self.commits += 1
        if self._pending_delete_ids is not None:
            self.db.delete_jobs(self._pending_delete_ids)
        self._pending_delete_ids = None

    def rollback(self):
        self.rollbacks += 1
        self._pending_delete_ids = None


def _fake_execute_values(cur, sql, values, page_size=None):
    """Sustituye psycopg2.extras.execute_values contra el fake: aplica el
    efecto (ON CONFLICT DO NOTHING) directamente sobre _FakeRestoreDB, sin
    reimplementar el quoting/escaping real de psycopg2."""
    if sql.startswith("INSERT INTO jobs ("):
        cols = [c.strip() for c in sql[len("INSERT INTO jobs ("): sql.index(")")].split(",")]
        for row_values in values:
            cur.db.insert_job(dict(zip(cols, row_values)))
    elif sql == dr.INSERT_JOB_SKILLS_QUERY:
        for pair in values:
            cur.db.insert_job_skill(pair)
    else:
        raise AssertionError(f"execute_values con SQL no soportado por el fake: {sql!r}")


@pytest.fixture
def patched_execute_values(monkeypatch):
    monkeypatch.setattr(dr.psycopg2.extras, "execute_values", _fake_execute_values)


RESTORE_COLUMNS = ["id", "title", "posted_at", "remote", "description_full"]
_TS = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _job_row(id_, title="Data Engineer", posted_at=_TS, remote=None, description_full=None):
    return {
        "id": id_,
        "title": title,
        "posted_at": posted_at.isoformat(),
        "remote": remote,
        "description_full": description_full,
    }


# --- classify_jobs_batch: comparacion columna a columna -----------------


def test_classify_jobs_batch_missing_is_insert_candidate():
    db = _FakeRestoreDB(jobs={})
    cur = _FakeRestoreCursor(db)
    backup_rows = [_job_row(1)]
    insert_rows, identical_ids, conflict_ids, conflicts = dr.classify_jobs_batch(
        cur, RESTORE_COLUMNS, backup_rows
    )
    assert insert_rows == backup_rows
    assert identical_ids == set()
    assert conflict_ids == set()
    assert conflicts == []


def test_classify_jobs_batch_existing_identical_is_safe():
    backup_row = _job_row(1, remote=True)
    db = _FakeRestoreDB(jobs={1: backup_row})
    cur = _FakeRestoreCursor(db)
    insert_rows, identical_ids, conflict_ids, conflicts = dr.classify_jobs_batch(
        cur, RESTORE_COLUMNS, [backup_row]
    )
    assert insert_rows == []
    assert identical_ids == {1}
    assert conflict_ids == set()
    assert conflicts == []


def test_classify_jobs_batch_existing_different_is_conflict_with_diagnosis():
    backup_row = _job_row(1, title="Data Engineer")
    live_row = _job_row(1, title="Senior Data Engineer")
    db = _FakeRestoreDB(jobs={1: live_row})
    cur = _FakeRestoreCursor(db)
    insert_rows, identical_ids, conflict_ids, conflicts = dr.classify_jobs_batch(
        cur, RESTORE_COLUMNS, [backup_row]
    )
    assert insert_rows == []
    assert identical_ids == set()
    assert conflict_ids == {1}
    assert conflicts == [
        {"id": 1, "diffs": [("title", "Data Engineer", "Senior Data Engineer")]}
    ]


def test_classify_jobs_batch_null_vs_null_is_identical():
    backup_row = _job_row(1, description_full=None)
    db = _FakeRestoreDB(jobs={1: _job_row(1, description_full=None)})
    cur = _FakeRestoreCursor(db)
    _, identical_ids, conflict_ids, _ = dr.classify_jobs_batch(cur, RESTORE_COLUMNS, [backup_row])
    assert identical_ids == {1}
    assert conflict_ids == set()


def test_classify_jobs_batch_null_vs_value_is_conflict():
    backup_row = _job_row(1, description_full=None)
    live_row = _job_row(1, description_full="texto crawleado despues del backup")
    db = _FakeRestoreDB(jobs={1: live_row})
    cur = _FakeRestoreCursor(db)
    _, identical_ids, conflict_ids, conflicts = dr.classify_jobs_batch(
        cur, RESTORE_COLUMNS, [backup_row]
    )
    assert identical_ids == set()
    assert conflict_ids == {1}
    assert conflicts[0]["diffs"] == [
        ("description_full", None, "texto crawleado despues del backup")
    ]


def test_classify_jobs_batch_equivalent_timestamp_is_identical():
    ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    backup_row = _job_row(1, posted_at=ts)
    live_row = _job_row(1, posted_at=ts)
    db = _FakeRestoreDB(jobs={1: live_row})
    cur = _FakeRestoreCursor(db)
    _, identical_ids, conflict_ids, _ = dr.classify_jobs_batch(cur, RESTORE_COLUMNS, [backup_row])
    assert identical_ids == {1}
    assert conflict_ids == set()


def test_classify_jobs_batch_bool_int_string_preserved_when_identical():
    backup_row = _job_row(1, title="QA Engineer", remote=False)
    db = _FakeRestoreDB(jobs={1: _job_row(1, title="QA Engineer", remote=False)})
    cur = _FakeRestoreCursor(db)
    _, identical_ids, conflict_ids, _ = dr.classify_jobs_batch(cur, RESTORE_COLUMNS, [backup_row])
    assert identical_ids == {1}
    assert conflict_ids == set()


def test_classify_jobs_batch_mixed_batch_all_three_outcomes():
    missing = _job_row(1)
    identical = _job_row(2)
    conflict_backup = _job_row(3, title="Backend")
    db = _FakeRestoreDB(jobs={2: _job_row(2), 3: _job_row(3, title="Backend Senior")})
    cur = _FakeRestoreCursor(db)
    insert_rows, identical_ids, conflict_ids, conflicts = dr.classify_jobs_batch(
        cur, RESTORE_COLUMNS, [missing, identical, conflict_backup]
    )
    assert insert_rows == [missing]
    assert identical_ids == {2}
    assert conflict_ids == {3}
    assert len(conflicts) == 1 and conflicts[0]["id"] == 3


# --- find_missing_skill_ids: gate obligatorio antes de escribir nada -----


def _write_gz_dataset(path, rows, columns):
    dr.export_rows_to_jsonl_gz(rows, columns, path)


def test_find_missing_skill_ids_empty_when_all_present(tmp_path):
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    skills_snapshot_path = tmp_path / "skills_snapshot.jsonl.gz"
    _write_gz_dataset(job_skills_path, [(1, 10), (2, 11)], dr.JOB_SKILLS_COLUMNS)
    _write_gz_dataset(skills_snapshot_path, [(10, "Python", "language"), (11, "SQL", "language")],
                       dr.SKILLS_COLUMNS)
    db = _FakeRestoreDB(skills={10, 11})
    conn = _FakeRestoreConn(db)
    assert dr.find_missing_skill_ids(conn, job_skills_path, skills_snapshot_path) == []


def test_find_missing_skill_ids_reports_missing_with_name_and_category(tmp_path):
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    skills_snapshot_path = tmp_path / "skills_snapshot.jsonl.gz"
    _write_gz_dataset(job_skills_path, [(1, 10), (2, 99)], dr.JOB_SKILLS_COLUMNS)
    _write_gz_dataset(
        skills_snapshot_path,
        [(10, "Python", "language"), (99, "Rust", "language")],
        dr.SKILLS_COLUMNS,
    )
    db = _FakeRestoreDB(skills={10})  # falta el 99
    conn = _FakeRestoreConn(db)
    missing = dr.find_missing_skill_ids(conn, job_skills_path, skills_snapshot_path)
    assert missing == [{"id": 99, "name": "Rust", "category": "language"}]


def test_find_missing_skill_ids_empty_job_skills_is_empty(tmp_path):
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    skills_snapshot_path = tmp_path / "skills_snapshot.jsonl.gz"
    _write_gz_dataset(job_skills_path, [], dr.JOB_SKILLS_COLUMNS)
    _write_gz_dataset(skills_snapshot_path, [], dr.SKILLS_COLUMNS)
    db = _FakeRestoreDB()
    conn = _FakeRestoreConn(db)
    assert dr.find_missing_skill_ids(conn, job_skills_path, skills_snapshot_path) == []


# --- _write_batch_transaction: commit en exito, rollback ante excepcion --


def test_write_batch_transaction_commits_on_success():
    conn = _FakeRestoreConn(_FakeRestoreDB())
    with dr._write_batch_transaction(conn):
        pass
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_write_batch_transaction_rolls_back_on_exception():
    conn = _FakeRestoreConn(_FakeRestoreDB())
    with pytest.raises(RuntimeError):
        with dr._write_batch_transaction(conn):
            raise RuntimeError("conexion perdida a mitad del batch")
    assert conn.commits == 0
    assert conn.rollbacks == 1


# --- job_skills: restaurable / ya existe / bloqueado por conflicto ------


def test_process_job_skills_restore_plan_classifies_all_three_outcomes(tmp_path):
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    # (1,10) falta y es restaurable; (2,11) ya existe; (3,12) esta bloqueado
    # porque el job 3 esta en conflict_ids.
    _write_gz_dataset(job_skills_path, [(1, 10), (2, 11), (3, 12)], dr.JOB_SKILLS_COLUMNS)
    db = _FakeRestoreDB(job_skills={(2, 11)})
    conn = _FakeRestoreConn(db)
    result = dr._process_job_skills_restore(
        conn, job_skills_path, batch_size=500, conflict_ids={3}, write=False
    )
    assert result == {"restorable": 1, "existing": 1, "blocked_by_conflict": 1}
    # read-only: no debe haber escrito nada en el fake
    assert db.job_skills == {(2, 11)}


def test_process_job_skills_restore_write_inserts_only_restorable(tmp_path, patched_execute_values):
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    _write_gz_dataset(job_skills_path, [(1, 10), (2, 11), (3, 12)], dr.JOB_SKILLS_COLUMNS)
    db = _FakeRestoreDB(job_skills={(2, 11)})
    conn = _FakeRestoreConn(db)
    result = dr._process_job_skills_restore(
        conn, job_skills_path, batch_size=500, conflict_ids={3}, write=True
    )
    assert result == {"restorable": 1, "existing": 1, "blocked_by_conflict": 1}
    assert db.job_skills == {(1, 10), (2, 11)}  # (3,12) nunca se toca


def test_process_job_skills_restore_never_inserts_duplicate(tmp_path, patched_execute_values):
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    _write_gz_dataset(job_skills_path, [(1, 10)], dr.JOB_SKILLS_COLUMNS)
    db = _FakeRestoreDB(job_skills={(1, 10)})  # ya existe
    conn = _FakeRestoreConn(db)
    result = dr._process_job_skills_restore(
        conn, job_skills_path, batch_size=500, conflict_ids=set(), write=True
    )
    assert result["restorable"] == 0
    assert result["existing"] == 1
    assert db.job_skills == {(1, 10)}  # sin duplicar


# --- run_restore / build_restore_plan: orquestacion end-to-end ----------


def _write_tiny_restore_backup(tmp_path, *, jobs_rows, job_skills_rows, skills_rows, columns=None):
    """`columns` por defecto RESTORE_COLUMNS (compatibilidad con los tests
    existentes); los tests de SAFE_BENIGN_DEACTIVATION pasan DELETE_COLUMNS
    (incluye `is_active`, que RESTORE_COLUMNS no tiene)."""
    columns = columns or RESTORE_COLUMNS
    jobs_path = tmp_path / "jobs.jsonl.gz"
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    skills_path = tmp_path / "skills_snapshot.jsonl.gz"
    candidate_ids_path = tmp_path / "candidate_ids.jsonl.gz"

    jobs_result = dr.export_rows_to_jsonl_gz(jobs_rows, columns, jobs_path)
    job_skills_result = dr.export_rows_to_jsonl_gz(job_skills_rows, dr.JOB_SKILLS_COLUMNS, job_skills_path)
    skills_result = dr.export_rows_to_jsonl_gz(skills_rows, dr.SKILLS_COLUMNS, skills_path)
    candidate_rows = [(row[0],) for row in jobs_rows]
    candidate_result = dr.export_rows_to_jsonl_gz(candidate_rows, dr.CANDIDATE_IDS_COLUMNS, candidate_ids_path)

    manifest = {
        "format_version": "1.0",
        "jobs_columns": columns,
        "jobs_count": jobs_result["count"],
        "job_skills_count": job_skills_result["count"],
        "skills_count": skills_result["count"],
        "candidate_ids_count": candidate_result["count"],
        "files": {
            "jobs": {"filename": jobs_path.name, **{k: jobs_result[k] for k in
                     ("bytes_raw", "bytes_gz", "sha256_file", "sha256_content")}},
            "job_skills": {"filename": job_skills_path.name, **{k: job_skills_result[k] for k in
                           ("bytes_raw", "bytes_gz", "sha256_file", "sha256_content")}},
            "skills_snapshot": {"filename": skills_path.name, **{k: skills_result[k] for k in
                                ("bytes_raw", "bytes_gz", "sha256_file", "sha256_content")}},
            "candidate_ids": {"filename": candidate_ids_path.name, **{k: candidate_result[k] for k in
                              ("bytes_raw", "bytes_gz", "sha256_file", "sha256_content")}},
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest, manifest_path


def test_build_restore_plan_end_to_end_read_only(tmp_path, patched_execute_values):
    rows = [
        (1, "Data Engineer", _TS.isoformat(), None, None),  # falta -> insert
        (2, "Backend", _TS.isoformat(), True, None),        # existe identico
        (3, "QA", _TS.isoformat(), False, None),             # existe distinto -> conflicto
    ]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path,
        jobs_rows=rows,
        job_skills_rows=[(1, 10), (2, 11), (3, 12)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language"), (12, "AWS", "tool")],
    )
    db = _FakeRestoreDB(
        jobs={
            2: dict(zip(RESTORE_COLUMNS, rows[1])),
            3: dict(zip(RESTORE_COLUMNS, (3, "QA Senior", _TS.isoformat(), False, None))),
        },
        skills={10, 11, 12},
    )
    conn = _FakeRestoreConn(db)

    plan = dr.build_restore_plan(conn, manifest, tmp_path, batch_size=500)

    assert plan["jobs_total"] == 3
    assert plan["jobs_insertable"] == 1
    assert plan["jobs_identical"] == 1
    assert plan["jobs_conflict"] == 1
    assert plan["job_skills_restorable"] == 2  # (1,10) e (2,11): job 3 esta bloqueado
    assert plan["job_skills_blocked_by_conflict"] == 1
    assert plan["missing_skill_ids"] == []
    assert plan["would_abort"] is False
    # READ-ONLY de verdad: nada debe haber cambiado en el fake
    assert db.jobs == {
        2: dict(zip(RESTORE_COLUMNS, rows[1])),
        3: dict(zip(RESTORE_COLUMNS, (3, "QA Senior", _TS.isoformat(), False, None))),
    }
    assert db.job_skills == set()


def test_run_restore_writes_only_missing_and_skips_conflicts(tmp_path, patched_execute_values):
    rows = [
        (1, "Data Engineer", _TS.isoformat(), None, None),
        (2, "Backend", _TS.isoformat(), True, None),
        (3, "QA", _TS.isoformat(), False, None),
    ]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path,
        jobs_rows=rows,
        job_skills_rows=[(1, 10), (2, 11), (3, 12)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language"), (12, "AWS", "tool")],
    )
    db = _FakeRestoreDB(
        jobs={
            2: dict(zip(RESTORE_COLUMNS, rows[1])),
            3: dict(zip(RESTORE_COLUMNS, (3, "QA Senior", _TS.isoformat(), False, None))),
        },
        skills={10, 11, 12},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_restore(conn, manifest, tmp_path, batch_size=500)

    assert result["jobs_insertable"] == 1
    assert result["jobs_conflict"] == 1
    assert 1 in db.jobs  # se inserto
    assert dict(zip(RESTORE_COLUMNS, rows[0])) == db.jobs[1]
    assert db.jobs[3]["title"] == "QA Senior"  # el conflictivo NUNCA se toca
    assert db.job_skills == {(1, 10), (2, 11)}  # (3,12) nunca se restaura


def test_run_restore_aborts_before_writing_if_skill_id_missing(tmp_path, patched_execute_values):
    rows = [(1, "Data Engineer", _TS.isoformat(), None, None)]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path,
        jobs_rows=rows,
        job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language")],
    )
    db = _FakeRestoreDB(skills=set())  # falta el skill 10
    conn = _FakeRestoreConn(db)

    with pytest.raises(dr.RestoreAbortedError):
        dr.run_restore(conn, manifest, tmp_path, batch_size=500)

    # CERO escrituras: ni el job (que si era insertable) se llego a tocar
    assert db.jobs == {}
    assert db.job_skills == set()


def test_run_restore_is_idempotent_across_two_full_runs(tmp_path, patched_execute_values):
    """
    Simula: primera ejecucion inserta jobs pero se interrumpe antes de
    terminar job_skills (estado de partida ya con jobs insertados y solo
    ALGUNOS job_skills). Segunda ejecucion (run_restore de nuevo, desde
    cero, reclasificando todo) debe completar exactamente lo que falta,
    sin duplicar nada y llegando al mismo estado final que una unica
    ejecucion limpia habria producido.
    """
    rows = [
        (1, "Data Engineer", _TS.isoformat(), None, None),
        (2, "Backend", _TS.isoformat(), None, None),
    ]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path,
        jobs_rows=rows,
        job_skills_rows=[(1, 10), (1, 11), (2, 10)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    # Estado tras una "primera ejecucion interrumpida": los jobs ya se
    # insertaron (idempotente por id), pero job_skills se quedo a medias.
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, rows[0])), 2: dict(zip(RESTORE_COLUMNS, rows[1]))},
        job_skills={(1, 10)},
        skills={10, 11},
    )
    conn = _FakeRestoreConn(db)

    result_2 = dr.run_restore(conn, manifest, tmp_path, batch_size=500)
    assert result_2["jobs_insertable"] == 0  # ya estaban, nada que insertar
    assert result_2["jobs_identical"] == 2
    assert result_2["job_skills_restorable"] == 2  # (1,11) y (2,10) faltaban
    assert db.job_skills == {(1, 10), (1, 11), (2, 10)}  # sin duplicados

    # Una TERCERA ejecucion sobre el estado ya completo no debe cambiar nada.
    result_3 = dr.run_restore(conn, manifest, tmp_path, batch_size=500)
    assert result_3["jobs_insertable"] == 0
    assert result_3["job_skills_restorable"] == 0
    assert result_3["job_skills_existing"] == 3
    assert db.job_skills == {(1, 10), (1, 11), (2, 10)}


def test_run_restore_batches_respect_restore_batch_size(tmp_path, patched_execute_values):
    """batch_size pequeno no debe cambiar el resultado, solo el numero de
    idas y vueltas -- confirma que _batched() no pierde ni duplica filas."""
    rows = [(i, f"Job {i}", _TS.isoformat(), None, None) for i in range(1, 8)]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=rows, job_skills_rows=[(i, 10) for i in range(1, 8)],
        skills_rows=[(10, "Python", "language")],
    )
    db = _FakeRestoreDB(skills={10})
    conn = _FakeRestoreConn(db)

    result = dr.run_restore(conn, manifest, tmp_path, batch_size=2)  # 7 filas, batches de 2

    assert result["jobs_insertable"] == 7
    assert len(db.jobs) == 7
    assert result["job_skills_restorable"] == 7
    assert len(db.job_skills) == 7


# =============================================================================
# delete — autoridad exclusiva: candidate_ids/jobs del manifiesto, nunca un
# recalculo de NOW()-retention_days. Reutiliza classify_jobs_batch (misma
# comparacion que restore) y _write_batch_transaction (mismo patron de
# batch+commit inmediato). Fakes: mismas limitaciones documentadas arriba
# (sin PostgreSQL local accesible, sin contrasena conocida y sin intentar
# averiguarla).
# =============================================================================


def test_build_delete_plan_classifies_missing_identical_drift_read_only(
    tmp_path, patched_execute_values
):
    rows = [
        (1, "Data Engineer", _TS.isoformat(), None, None),  # ya no existe -> already_missing
        (2, "Backend", _TS.isoformat(), True, None),        # existe identico -> deletable
        (3, "QA", _TS.isoformat(), False, None),             # existe distinto -> drift
    ]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path,
        jobs_rows=rows,
        job_skills_rows=[(2, 10), (3, 11)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    db = _FakeRestoreDB(
        jobs={
            2: dict(zip(RESTORE_COLUMNS, rows[1])),
            3: dict(zip(RESTORE_COLUMNS, (3, "QA Senior", _TS.isoformat(), False, None))),
        },
        job_skills={(2, 10), (3, 11)},
        skills={10, 11},
    )
    conn = _FakeRestoreConn(db)

    plan = dr.build_delete_plan(conn, manifest, tmp_path, batch_size=500)

    assert plan["candidate_ids_total"] == 3
    assert plan["already_missing"] == 1
    assert plan["existing_identical"] == 1
    assert plan["benign_deactivation"] == 0  # el drift es en "title", no is_active
    assert plan["material_drift"] == 1
    assert plan["safe_delete_total"] == 1
    assert plan["job_skills_cascade"] == 1  # solo el job_skill del deletable (job 2)
    assert len(plan["drift_sample"]) == 1 and plan["drift_sample"][0]["id"] == 3
    # READ-ONLY de verdad: nada debe haber cambiado en el fake
    assert 2 in db.jobs and 3 in db.jobs
    assert db.job_skills == {(2, 10), (3, 11)}


def test_run_delete_deletes_only_identical_never_missing_never_drift(
    tmp_path, patched_execute_values
):
    rows = [
        (1, "Data Engineer", _TS.isoformat(), None, None),  # already_missing
        (2, "Backend", _TS.isoformat(), True, None),        # deletable
        (3, "QA", _TS.isoformat(), False, None),             # drift
    ]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path,
        jobs_rows=rows,
        job_skills_rows=[(2, 10), (3, 11)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    db = _FakeRestoreDB(
        jobs={
            2: dict(zip(RESTORE_COLUMNS, rows[1])),
            3: dict(zip(RESTORE_COLUMNS, (3, "QA Senior", _TS.isoformat(), False, None))),
        },
        job_skills={(2, 10), (3, 11)},
        skills={10, 11},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["already_missing"] == 1
    assert result["existing_identical"] == 1
    assert result["benign_deactivation"] == 0  # el drift es en "title", no is_active
    assert result["material_drift"] == 1
    assert result["safe_delete_total"] == 1
    assert result["job_skills_cascade"] == 1
    # el deletable (2) ya no existe; el drift (3) NUNCA se toca
    assert 2 not in db.jobs
    assert 3 in db.jobs
    assert db.jobs[3]["title"] == "QA Senior"
    # ON DELETE CASCADE: el job_skill del borrado desaparece, el del drift no
    assert db.job_skills == {(3, 11)}


def test_run_delete_already_missing_candidate_is_pure_noop(tmp_path, patched_execute_values):
    rows = [(1, "Data Engineer", _TS.isoformat(), None, None)]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=rows, job_skills_rows=[], skills_rows=[]
    )
    db = _FakeRestoreDB()  # el job 1 nunca existio (o ya se borro antes)
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["already_missing"] == 1
    assert result["existing_identical"] == 0
    assert result["benign_deactivation"] == 0
    assert result["material_drift"] == 0
    assert db.delete_calls == 0  # ni siquiera se intento un DELETE


def test_run_delete_respects_delete_batch_size(tmp_path, patched_execute_values):
    rows = [(i, f"Job {i}", _TS.isoformat(), None, None) for i in range(1, 8)]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=rows, job_skills_rows=[], skills_rows=[]
    )
    db = _FakeRestoreDB(jobs={i: dict(zip(RESTORE_COLUMNS, rows[i - 1])) for i in range(1, 8)})
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=2)  # 7 filas, batches de 2

    assert result["existing_identical"] == 7
    assert db.jobs == {}
    assert db.delete_calls == 4  # ceil(7/2) batches con al menos un deletable


def test_run_delete_is_idempotent_after_batch_failure(tmp_path, patched_execute_values):
    """
    Simula: batch 1 (job 1) se borra y hace commit; batch 2 (job 2) falla
    a mitad del DELETE (conexion perdida) y hace rollback -- job 2 sigue
    vivo. Se relanza `run_delete` desde cero: job 1 ahora es
    `already_missing` (no-op), job 2 se borra sin error. Nunca se toca el
    job 3, que tiene drift.
    """
    rows = [
        (1, "Data Engineer", _TS.isoformat(), None, None),
        (2, "Backend", _TS.isoformat(), None, None),
        (3, "QA", _TS.isoformat(), False, None),
    ]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=rows, job_skills_rows=[], skills_rows=[]
    )
    db = _FakeRestoreDB(
        jobs={
            1: dict(zip(RESTORE_COLUMNS, rows[0])),
            2: dict(zip(RESTORE_COLUMNS, rows[1])),
            3: dict(zip(RESTORE_COLUMNS, (3, "QA Senior", _TS.isoformat(), False, None))),
        }
    )
    db.fail_on_delete_call = 2  # el DELETE del batch de job 2 revienta
    conn = _FakeRestoreConn(db)

    with pytest.raises(RuntimeError, match="conexion perdida"):
        dr.run_delete(conn, manifest, tmp_path, batch_size=1)  # 3 batches de 1

    assert conn.commits == 1  # batch de job 1
    assert conn.rollbacks == 1  # batch de job 2
    assert 1 not in db.jobs  # job 1 ya borrado y persistido
    assert 2 in db.jobs  # job 2 sigue vivo -- el rollback lo protegio
    assert 3 in db.jobs and db.jobs[3]["title"] == "QA Senior"  # drift, nunca tocado

    # Relanzamos desde cero: sin errores, job 1 -> already_missing (no-op),
    # job 2 se borra esta vez, job 3 sigue protegido.
    result_2 = dr.run_delete(conn, manifest, tmp_path, batch_size=1)

    assert result_2["already_missing"] == 1  # job 1
    assert result_2["existing_identical"] == 1  # job 2
    assert result_2["material_drift"] == 1  # job 3
    assert db.jobs == {3: db.jobs[3]}  # solo el drift sigue vivo


# --- TOCTOU: el DELETE real debe comparar y borrar la MISMA fila --------


def test_run_delete_uses_for_update_and_never_deletes_a_row_changed_at_lock_time(
    tmp_path, patched_execute_values
):
    """
    Caso obligatorio de TOCTOU: el backup tiene la version antigua del
    job 1, y `jobs` HOY todavia tiene esa misma version antigua (por eso
    "parece" identical al arrancar el batch). Justo en el instante en que
    el DELETE real ejecuta su SELECT ... FOR UPDATE -- el momento exacto
    en que Postgres bloquearia esa fila frente a escritores concurrentes
    -- simulamos que una escritura concurrente ya cambio la fila (p.ej.
    una reingesta de Pipeline A). Sin FOR UPDATE (o si se comparase con
    una lectura anterior en vez de con la fila recien bloqueada), el
    codigo podria borrar silenciosamente la version nueva sin haberla
    comparado nunca. Con FOR UPDATE, la comparacion usa la fila fresca en
    el mismo instante del lock -> se detecta como drift y NUNCA se borra.
    """
    old_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[old_row], job_skills_rows=[], skills_rows=[]
    )
    db = _FakeRestoreDB(jobs={1: dict(zip(RESTORE_COLUMNS, old_row))})

    def concurrent_write(ids):
        if 1 in ids:
            db.jobs[1] = dict(
                zip(RESTORE_COLUMNS, (1, "Data Engineer (reingestado)", _TS.isoformat(), None, None))
            )

    db.on_for_update_lock = concurrent_write
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["existing_identical"] == 0
    assert result["material_drift"] == 1  # detectado como drift, no como identical
    assert 1 in db.jobs  # NUNCA se borro
    assert db.jobs[1]["title"] == "Data Engineer (reingestado)"  # version fresca, no la antigua


def test_run_delete_locks_only_the_current_batch_ids_for_update(tmp_path, patched_execute_values):
    """El DELETE real debe pedir el lock (FOR UPDATE) exactamente sobre
    los ids del batch en curso -- nunca sobre las 197K filas a la vez."""
    rows = [(i, f"Job {i}", _TS.isoformat(), None, None) for i in range(1, 4)]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=rows, job_skills_rows=[], skills_rows=[]
    )
    db = _FakeRestoreDB(jobs={i: dict(zip(RESTORE_COLUMNS, rows[i - 1])) for i in range(1, 4)})
    locked_batches = []
    db.on_for_update_lock = lambda ids: locked_batches.append(ids)
    conn = _FakeRestoreConn(db)

    dr.run_delete(conn, manifest, tmp_path, batch_size=1)

    assert locked_batches == [{1}, {2}, {3}]  # un batch (1 id) por llamada, nunca los 3 juntos


def test_delete_plan_never_locks_rows_for_update(tmp_path, patched_execute_values):
    """--delete-plan sigue siendo SELECT normal, sin FOR UPDATE -- no
    borra nada, asi que no hay nada que proteger con un lock."""
    rows = [(1, "Data Engineer", _TS.isoformat(), None, None)]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=rows, job_skills_rows=[], skills_rows=[]
    )
    db = _FakeRestoreDB(jobs={1: dict(zip(RESTORE_COLUMNS, rows[0]))})
    locked_batches = []
    db.on_for_update_lock = lambda ids: locked_batches.append(ids)
    conn = _FakeRestoreConn(db)

    dr.build_delete_plan(conn, manifest, tmp_path, batch_size=500)

    assert locked_batches == []


def test_restore_never_locks_rows_for_update(tmp_path, patched_execute_values):
    """El restore (INSERT ... ON CONFLICT DO NOTHING) tampoco necesita
    FOR UPDATE -- solo el DELETE real bloquea filas."""
    rows = [(1, "Data Engineer", _TS.isoformat(), None, None)]
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=rows, job_skills_rows=[(1, 10)], skills_rows=[(10, "Python", "language")]
    )
    db = _FakeRestoreDB(skills={10})
    locked_batches = []
    db.on_for_update_lock = lambda ids: locked_batches.append(ids)
    conn = _FakeRestoreConn(db)

    dr.run_restore(conn, manifest, tmp_path, batch_size=500)

    assert locked_batches == []


# =============================================================================
# SAFE_BENIGN_DEACTIVATION — is_active TRUE->FALSE en solitario, exclusiva
# del DELETE de retention. Auditoria 2026-09-22: 3.220 conflictos reales de
# un --delete-plan eran 100% esta transicion, explicada por
# _deactivate_old_jobs() (scripts/load.py), que SOLO modifica is_active.
# Restore/--restore-plan NUNCA aplican esta excepcion -- columnas propias
# (DELETE_COLUMNS, con is_active) para no tocar RESTORE_COLUMNS ni ninguno
# de los tests que ya lo usan.
# =============================================================================

DELETE_COLUMNS = ["id", "title", "posted_at", "is_active", "description_full"]


# --- _is_benign_deactivation: funcion pura, sin BD -----------------------


def test_is_benign_deactivation_true_to_false_only():
    assert dr._is_benign_deactivation([("is_active", True, False)]) is True


def test_is_benign_deactivation_false_to_true_is_material():
    assert dr._is_benign_deactivation([("is_active", False, True)]) is False


def test_is_benign_deactivation_with_extra_column_diff_is_material():
    assert dr._is_benign_deactivation(
        [("is_active", True, False), ("title", "A", "B")]
    ) is False


def test_is_benign_deactivation_null_transitions_are_material():
    assert dr._is_benign_deactivation([("is_active", None, False)]) is False
    assert dr._is_benign_deactivation([("is_active", True, None)]) is False
    assert dr._is_benign_deactivation([("is_active", None, True)]) is False


def test_is_benign_deactivation_other_column_only_is_material():
    assert dr._is_benign_deactivation([("title", "Data Engineer", "Senior Data Engineer")]) is False


def test_is_benign_deactivation_no_diffs_is_material():
    assert dr._is_benign_deactivation([]) is False


# --- DELETE: los 6 casos de clasificacion pedidos -------------------------


def test_run_delete_case1_true_to_false_only_is_benign_and_deleted(tmp_path, patched_execute_values):
    """Caso 1: backup TRUE, actual FALSE, resto identico -> benign, se borra."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language")], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), False, None)))},
        job_skills={(1, 10)},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["existing_identical"] == 0
    assert result["benign_deactivation"] == 1
    assert result["material_drift"] == 0
    assert result["safe_delete_total"] == 1
    assert 1 not in db.jobs  # se borro
    assert db.job_skills == set()  # cascade


def test_run_delete_case2_false_to_true_is_material_never_deleted(tmp_path, patched_execute_values):
    """Caso 2: backup FALSE, actual TRUE -> material drift, NUNCA se borra
    (posible reactivacion real via UPSERT)."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), False, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), True, None)))},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["benign_deactivation"] == 0
    assert result["material_drift"] == 1
    assert result["safe_delete_total"] == 0
    assert 1 in db.jobs  # nunca se borro


def test_run_delete_case3_true_to_false_plus_other_column_is_material(tmp_path, patched_execute_values):
    """Caso 3: is_active TRUE->FALSE PERO ademas cambia otra columna -> material drift."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Senior Data Engineer", _TS.isoformat(), False, None)))},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["benign_deactivation"] == 0
    assert result["material_drift"] == 1
    assert 1 in db.jobs


def test_run_delete_case4_null_is_active_transition_is_material(tmp_path, patched_execute_values):
    """Caso 4: NULL de por medio en is_active -> material drift, nunca benign."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), False, None)))},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["benign_deactivation"] == 0
    assert result["material_drift"] == 1
    assert 1 in db.jobs


def test_run_delete_case5_identical_is_safe_identical_not_benign(tmp_path, patched_execute_values):
    """Caso 5: fila identica normal -> existing_identical, no benign_deactivation."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(jobs={1: dict(zip(DELETE_COLUMNS, backup_row))})
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["existing_identical"] == 1
    assert result["benign_deactivation"] == 0
    assert result["safe_delete_total"] == 1
    assert 1 not in db.jobs


def test_run_delete_case6_other_column_diff_with_is_active_equal_is_material(
    tmp_path, patched_execute_values
):
    """Caso 6: is_active igual en ambos lados, pero otra columna distinta -> material drift."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), True, "texto nuevo")))},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["benign_deactivation"] == 0
    assert result["material_drift"] == 1
    assert 1 in db.jobs


# --- RESTORE: sigue estricto, nunca aplica la excepcion -------------------


def test_case7_restore_still_treats_true_to_false_only_as_conflict(tmp_path, patched_execute_values):
    """Caso 7: RESTORE debe seguir tratando TRUE->FALSE-only como CONFLICTO
    estricto -- la excepcion de benign deactivation es exclusiva del DELETE."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language")], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), False, None)))},
        skills={10},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_restore(conn, manifest, tmp_path, batch_size=500)

    assert result["jobs_conflict"] == 1  # sigue siendo conflicto, no "identical"
    assert result["jobs_identical"] == 0
    assert db.jobs[1]["is_active"] is False  # nunca se toca
    assert db.job_skills == set()  # bloqueados por conflicto, nunca restaurados


def test_case8_restore_plan_also_treats_true_to_false_only_as_conflict(tmp_path, patched_execute_values):
    """Caso 8: --restore-plan tampoco aplica la excepcion de benign deactivation."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), False, None)))},
    )
    conn = _FakeRestoreConn(db)

    plan = dr.build_restore_plan(conn, manifest, tmp_path, batch_size=500)

    assert plan["jobs_conflict"] == 1
    assert plan["jobs_identical"] == 0


# --- TOCTOU/destructivo con la nueva clasificacion -------------------------


def test_case9_delete_locks_and_deletes_row_that_becomes_benign_at_lock_time(
    tmp_path, patched_execute_values
):
    """Caso 9: la fila es identical al arrancar el batch, pero justo en el
    instante del FOR UPDATE una escritura concurrente (simulada) la
    desactiva (TRUE->FALSE). Sigue siendo benign -> se borra igual."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(jobs={1: dict(zip(DELETE_COLUMNS, backup_row))})  # TRUE, "identical" al arrancar

    def concurrent_deactivation(ids):
        if 1 in ids:
            db.jobs[1] = dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), False, None)))

    db.on_for_update_lock = concurrent_deactivation
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["benign_deactivation"] == 1
    assert 1 not in db.jobs  # se borro: la version bloqueada era benign


def test_case10_delete_locks_and_protects_row_that_becomes_material_at_lock_time(
    tmp_path, patched_execute_values
):
    """Caso 10: la fila parecia identical al leer el backup, pero justo en
    el instante del FOR UPDATE cambia de forma NO benigna (otra columna).
    NUNCA se borra."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(jobs={1: dict(zip(DELETE_COLUMNS, backup_row))})

    def concurrent_material_change(ids):
        if 1 in ids:
            db.jobs[1] = dict(
                zip(DELETE_COLUMNS, (1, "Senior Data Engineer", _TS.isoformat(), True, None))
            )

    db.on_for_update_lock = concurrent_material_change
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["material_drift"] == 1
    assert 1 in db.jobs  # protegido


def test_case11_delete_plan_still_never_locks_with_benign_logic(tmp_path, patched_execute_values):
    """Caso 11: --delete-plan sigue siendo READ-ONLY puro (sin FOR UPDATE)
    incluso con la nueva clasificacion benign/material."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), True, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[], columns=DELETE_COLUMNS,
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(DELETE_COLUMNS, (1, "Data Engineer", _TS.isoformat(), False, None)))}
    )
    locked_batches = []
    db.on_for_update_lock = lambda ids: locked_batches.append(ids)
    conn = _FakeRestoreConn(db)

    plan = dr.build_delete_plan(conn, manifest, tmp_path, batch_size=500)

    assert locked_batches == []
    assert plan["benign_deactivation"] == 1  # clasifica correctamente sin bloquear


# =============================================================================
# job_skills protegido dentro de la transaccion destructiva: comparacion
# EXACTA de sets (nunca solo COUNT), granularidad por job (no por batch),
# TOCTOU cerrado con FOR UPDATE tambien sobre job_skills, y --delete-plan
# hace el mismo chequeo sin bloquear (solo diagnostico).
# =============================================================================


def test_js1_links_backup_equal_current_job_is_deletable(tmp_path, patched_execute_values):
    """Caso 1: BACKUP_SET(job) == CURRENT_SET(job) exactamente -> se borra."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10)},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["job_skills_exact_match_jobs"] == 1
    assert result["job_skills_drift_jobs"] == 0
    assert result["safe_delete_total"] == 1
    assert 1 not in db.jobs
    assert db.job_skills == set()  # cascade


def test_js2_same_count_different_skill_id_blocks_delete(tmp_path, patched_execute_values):
    """Caso 2: mismo COUNT (1 link) pero distinto skill_id -> NO se borra."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 11)},  # 11, no 10
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["job_skills_exact_match_jobs"] == 0
    assert result["job_skills_drift_jobs"] == 1
    assert result["safe_delete_total"] == 0
    assert 1 in db.jobs  # nunca se borro
    assert db.job_skills == {(1, 11)}  # nunca se toca


def test_js3_new_link_in_current_blocks_delete(tmp_path, patched_execute_values):
    """Caso 3: aparece un link nuevo en la BD que no estaba en el backup -> NO se borra."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10), (1, 11)},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["job_skills_drift_jobs"] == 1
    assert 1 in db.jobs
    assert db.job_skills == {(1, 10), (1, 11)}  # intacto


def test_js4_backup_link_missing_from_current_blocks_delete(tmp_path, patched_execute_values):
    """Caso 4: un link del backup ya no existe en la BD -> NO se borra."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10), (1, 11)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10)},  # falta el 11
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["job_skills_drift_jobs"] == 1
    assert 1 in db.jobs
    assert db.job_skills == {(1, 10)}


def test_js5_both_sets_empty_is_deletable(tmp_path, patched_execute_values):
    """Caso 5: el job nunca tuvo skills, ni en el backup ni ahora -> se borra."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[], skills_rows=[],
    )
    db = _FakeRestoreDB(jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))})
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["job_skills_exact_match_jobs"] == 1
    assert result["safe_delete_total"] == 1
    assert 1 not in db.jobs


def test_js6_granularity_per_job_not_per_batch(tmp_path, patched_execute_values):
    """
    Caso 6: dos jobs en el MISMO batch -- A con links exactos, B con drift
    de links. A se borra, B se queda (granularidad por job, no por batch).
    """
    row_a = (1, "Data Engineer", _TS.isoformat(), None, None)
    row_b = (2, "Backend", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[row_a, row_b], job_skills_rows=[(1, 10), (2, 11)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, row_a)), 2: dict(zip(RESTORE_COLUMNS, row_b))},
        job_skills={(1, 10), (2, 99)},  # A exacto, B con drift (99 en vez de 11)
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)  # ambos en el mismo batch

    assert result["job_skills_exact_match_jobs"] == 1
    assert result["job_skills_drift_jobs"] == 1
    assert result["safe_delete_total"] == 1
    assert 1 not in db.jobs  # A borrado
    assert 2 in db.jobs  # B protegido
    assert db.job_skills == {(2, 99)}  # solo queda el de B


def test_js7_concurrent_insert_at_lock_time_blocks_delete(tmp_path, patched_execute_values):
    """
    Caso 7 (TOCTOU): el job parece exacto al empezar el batch (backup y
    BD ambos con el mismo link), pero justo cuando se adquiere el FOR
    UPDATE de job_skills, una escritura concurrente (simulada) inserta un
    link nuevo. La lectura bajo lock ya refleja ese link -> mismatch ->
    NO se borra.
    """
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10)},  # parece exacto
    )

    def concurrent_insert(ids):
        if 1 in ids:
            db.job_skills.add((1, 11))  # insercion concurrente justo al bloquear

    db.on_job_skills_for_update_lock = concurrent_insert
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["job_skills_drift_jobs"] == 1
    assert result["safe_delete_total"] == 0
    assert 1 in db.jobs  # nunca se borro


def test_js8_concurrent_delete_at_lock_time_blocks_delete(tmp_path, patched_execute_values):
    """
    Caso 8 (TOCTOU): el job parece exacto al empezar el batch, pero justo
    al adquirir el FOR UPDATE de job_skills una escritura concurrente
    (simulada) borra el link. La lectura bajo lock ya lo refleja ausente
    -> mismatch -> NO se borra.
    """
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10)},
    )

    def concurrent_delete(ids):
        if 1 in ids:
            db.job_skills.discard((1, 10))  # eliminacion concurrente justo al bloquear

    db.on_job_skills_for_update_lock = concurrent_delete
    conn = _FakeRestoreConn(db)

    result = dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert result["job_skills_drift_jobs"] == 1
    assert 1 in db.jobs


def test_js9_for_update_on_job_skills_only_in_delete_write_path(tmp_path, patched_execute_values):
    """Caso 9: el FOR UPDATE de job_skills solo se usa en --delete real."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10)},
    )
    locked = []
    db.on_job_skills_for_update_lock = lambda ids: locked.append(set(ids))
    conn = _FakeRestoreConn(db)

    dr.run_delete(conn, manifest, tmp_path, batch_size=500)

    assert locked == [{1}]


def test_js10_delete_plan_never_locks_job_skills(tmp_path, patched_execute_values):
    """Caso 10: --delete-plan hace la misma comparacion de sets, pero
    nunca con FOR UPDATE -- solo diagnostico, sigue siendo READ-ONLY puro."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10)],
        skills_rows=[(10, "Python", "language")],
    )
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10)},
    )
    locked = []
    db.on_job_skills_for_update_lock = lambda ids: locked.append(set(ids))
    conn = _FakeRestoreConn(db)

    plan = dr.build_delete_plan(conn, manifest, tmp_path, batch_size=500)

    assert locked == []  # nunca se bloqueo nada
    assert plan["job_skills_exact_match_jobs"] == 1  # pero clasifico correctamente


def test_js11_restore_semantics_unchanged_by_job_skills_protection(tmp_path, patched_execute_values):
    """Caso 11: el restore sigue exactamente igual -- no gana ninguna
    nocion de comparacion exacta de job_skills, solo resta/completa lo
    que falta para jobs no conflictivos, como siempre."""
    backup_row = (1, "Data Engineer", _TS.isoformat(), None, None)
    manifest, manifest_path = _write_tiny_restore_backup(
        tmp_path, jobs_rows=[backup_row], job_skills_rows=[(1, 10), (1, 11)],
        skills_rows=[(10, "Python", "language"), (11, "SQL", "language")],
    )
    # El job ya existe e identico, pero le falta uno de los dos links del backup.
    db = _FakeRestoreDB(
        jobs={1: dict(zip(RESTORE_COLUMNS, backup_row))}, job_skills={(1, 10)}, skills={10, 11},
    )
    conn = _FakeRestoreConn(db)

    result = dr.run_restore(conn, manifest, tmp_path, batch_size=500)

    # Restore completa el link que faltaba -- comportamiento de siempre,
    # sin exigir que el set ya fuera identico de entrada.
    assert result["job_skills_restorable"] == 1
    assert db.job_skills == {(1, 10), (1, 11)}


# =============================================================================
# CLI — dry-run por defecto, backup requiere opt-in, verify no toca Supabase
# =============================================================================


class _FakeConn:
    """Conexión falsa: solo necesita responder a .close() para estos tests
    de CLI, que parchean dry_run_report/run_backup y nunca ejecutan SQL
    real sobre este objeto."""

    def close(self):
        pass


def test_cli_default_calls_dry_run_not_backup(monkeypatch):
    calls = []
    monkeypatch.setattr(dr, "dry_run_report", lambda *a, **kw: calls.append("dry_run"))
    monkeypatch.setattr(dr, "run_backup", lambda *a, **kw: calls.append("backup") or Path("."))
    monkeypatch.setattr(dr, "_get_connection", lambda: _FakeConn())

    dr.main([])

    assert calls == ["dry_run"]


def test_cli_backup_without_dest_exits_with_error(monkeypatch, capsys):
    monkeypatch.setattr(dr, "_get_connection", lambda: object())
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--backup"])
    assert exc_info.value.code == 2
    assert "--dest" in capsys.readouterr().err


def test_cli_backup_with_dest_calls_run_backup_not_dry_run(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(dr, "run_backup", lambda *a, **kw: calls.append("backup") or tmp_path)
    monkeypatch.setattr(dr, "dry_run_report", lambda *a, **kw: calls.append("dry_run"))
    monkeypatch.setattr(dr, "_get_connection", lambda: _FakeConn())

    dr.main(["--backup", "--dest", str(tmp_path)])

    assert calls == ["backup"]


def test_cli_verify_does_not_call_get_connection(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)

    def _boom():
        raise AssertionError("verify no deberia necesitar conexion a Supabase")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    dr.main(["--verify", str(manifest_path)])  # no debe lanzar


def test_cli_verify_exits_nonzero_on_problems(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path, drop_candidate_id=True)
    monkeypatch.setattr(dr, "_get_connection", lambda: object())
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--verify", str(manifest_path)])
    assert exc_info.value.code == 1


# =============================================================================
# CLI — restore-plan / restore: ningun argumento destructivo por defecto
# =============================================================================


def test_cli_restore_plan_and_restore_together_is_rejected(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)

    def _boom():
        raise AssertionError("no deberia conectar si los flags son incompatibles")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--restore-plan", str(manifest_path), "--restore", str(manifest_path)])
    assert exc_info.value.code == 2


def test_cli_restore_without_confirm_restore_never_connects(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)

    def _boom():
        raise AssertionError("--restore sin --confirm-restore no debe abrir conexion")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    monkeypatch.setattr(dr, "run_restore", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("run_restore no deberia llamarse")
    ))
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--restore", str(manifest_path)])
    assert exc_info.value.code == 2


def test_cli_restore_plan_invalid_manifest_aborts_before_connecting(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path, drop_candidate_id=True)  # falla --verify

    def _boom():
        raise AssertionError("restore-plan no debe conectar si --verify falla")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--restore-plan", str(manifest_path)])
    assert exc_info.value.code == 1


def test_cli_restore_confirmed_invalid_manifest_aborts_before_connecting(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path, corrupt_job_skills=True)  # falla --verify

    def _boom():
        raise AssertionError("--restore --confirm-restore no debe conectar si --verify falla")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--restore", str(manifest_path), "--confirm-restore"])
    assert exc_info.value.code == 1


def test_cli_restore_plan_calls_build_restore_plan_not_run_restore(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    calls = []
    monkeypatch.setattr(dr, "_get_connection", lambda: _FakeConn())
    monkeypatch.setattr(dr, "build_restore_plan", lambda *a, **kw: calls.append("plan") or {
        "jobs_total": 0, "jobs_insertable": 0, "jobs_identical": 0, "jobs_conflict": 0,
        "conflicts_sample": [], "job_skills_restorable": 0, "job_skills_existing": 0,
        "job_skills_blocked_by_conflict": 0, "missing_skill_ids": [], "would_abort": False,
    })
    monkeypatch.setattr(dr, "run_restore", lambda *a, **kw: calls.append("restore"))

    dr.main(["--restore-plan", str(manifest_path)])

    assert calls == ["plan"]


def test_cli_restore_confirmed_calls_run_restore_not_plan(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    calls = []
    monkeypatch.setattr(dr, "_get_connection", lambda: _FakeConn())
    monkeypatch.setattr(dr, "build_restore_plan", lambda *a, **kw: calls.append("plan"))
    monkeypatch.setattr(dr, "run_restore", lambda *a, **kw: calls.append("restore") or {
        "jobs_total": 0, "jobs_insertable": 0, "jobs_identical": 0, "jobs_conflict": 0,
        "conflicts_sample": [], "job_skills_restorable": 0, "job_skills_existing": 0,
        "job_skills_blocked_by_conflict": 0, "missing_skill_ids": [], "would_abort": False,
    })

    dr.main(["--restore", str(manifest_path), "--confirm-restore"])

    assert calls == ["restore"]


def test_cli_restore_aborted_error_propagates_with_no_partial_success_message(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    monkeypatch.setattr(dr, "_get_connection", lambda: _FakeConn())

    def _abort(*a, **kw):
        raise dr.RestoreAbortedError("skill_id faltante")

    monkeypatch.setattr(dr, "run_restore", _abort)
    with pytest.raises(dr.RestoreAbortedError):
        dr.main(["--restore", str(manifest_path), "--confirm-restore"])


# =============================================================================
# CLI — delete-plan / delete: ningun DELETE por defecto ni por accidente
# =============================================================================


def test_cli_delete_plan_and_delete_together_is_rejected(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)

    def _boom():
        raise AssertionError("no deberia conectar si los flags son incompatibles")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--delete-plan", str(manifest_path), "--delete", str(manifest_path)])
    assert exc_info.value.code == 2


def test_cli_delete_without_confirm_delete_never_connects(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)

    def _boom():
        raise AssertionError("--delete sin --confirm-delete no debe abrir conexion")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    monkeypatch.setattr(dr, "run_delete", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("run_delete no deberia llamarse")
    ))
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--delete", str(manifest_path)])
    assert exc_info.value.code == 2


def test_cli_delete_plan_invalid_manifest_aborts_before_connecting(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path, drop_candidate_id=True)  # falla --verify

    def _boom():
        raise AssertionError("delete-plan no debe conectar si --verify falla")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--delete-plan", str(manifest_path)])
    assert exc_info.value.code == 1


def test_cli_delete_confirmed_invalid_manifest_aborts_before_connecting(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path, corrupt_job_skills=True)  # falla --verify

    def _boom():
        raise AssertionError("--delete --confirm-delete no debe conectar si --verify falla")

    monkeypatch.setattr(dr, "_get_connection", _boom)
    with pytest.raises(SystemExit) as exc_info:
        dr.main(["--delete", str(manifest_path), "--confirm-delete"])
    assert exc_info.value.code == 1


_EMPTY_DELETE_PLAN = {
    "candidate_ids_total": 0, "already_missing": 0, "existing_identical": 0,
    "benign_deactivation": 0, "material_drift": 0, "drift_sample": [],
    "job_skills_exact_match_jobs": 0, "job_skills_drift_jobs": 0,
    "job_skills_drift_sample": [], "backup_links": 0, "current_links": 0,
    "safe_delete_total": 0, "job_skills_cascade": 0,
}


def test_cli_delete_plan_calls_build_delete_plan_not_run_delete(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    calls = []
    monkeypatch.setattr(dr, "_get_connection", lambda: _FakeConn())
    monkeypatch.setattr(
        dr, "build_delete_plan",
        lambda *a, **kw: calls.append("plan") or dict(_EMPTY_DELETE_PLAN),
    )
    monkeypatch.setattr(dr, "run_delete", lambda *a, **kw: calls.append("delete"))

    dr.main(["--delete-plan", str(manifest_path)])

    assert calls == ["plan"]


def test_cli_delete_confirmed_calls_run_delete_not_plan(monkeypatch, tmp_path):
    manifest_path = _build_tiny_backup(tmp_path)
    calls = []
    monkeypatch.setattr(dr, "_get_connection", lambda: _FakeConn())
    monkeypatch.setattr(dr, "build_delete_plan", lambda *a, **kw: calls.append("plan"))
    monkeypatch.setattr(
        dr, "run_delete",
        lambda *a, **kw: calls.append("delete") or dict(_EMPTY_DELETE_PLAN),
    )

    dr.main(["--delete", str(manifest_path), "--confirm-delete"])

    assert calls == ["delete"]


# =============================================================================
# Seguridad — ninguna constante SQL del módulo contiene palabras prohibidas
# =============================================================================
#
# INSERT ya NO está en esta lista desde que existe --restore, y DELETE ya
# NO está desde que existe --delete: ambas son operaciones legítimas y
# deliberadas, pero cada una CONFINADA exclusivamente a sus constantes de
# ALLOWED_INSERT_CONSTANTS / ALLOWED_DELETE_CONSTANTS — ver los tests
# dedicados más abajo. INSERT siempre "ON CONFLICT ... DO NOTHING" (nunca
# "DO UPDATE"); el único DELETE apunta siempre a `jobs` por `id`, nunca a
# `job_skills`/`skills` (`ON DELETE CASCADE` hace ese trabajo).
# "UPDATE" a secas (una escritura real, ej. "UPDATE jobs SET ...") sigue
# absolutamente prohibida, pero "FOR UPDATE" (cláusula de bloqueo de filas
# de un SELECT, sin escribir nada) es legítima y está confinada a
# JOBS_BY_IDS_FOR_UPDATE_TEMPLATE — ver test_for_update_lock_is_read_only_
# and_confined_to_one_constant. El escaneo de abajo excluye "FOR UPDATE"
# antes de buscar "UPDATE" a secas, precisamente para no perder esta red
# de seguridad en el resto del módulo.
# ALTER/DROP/TRUNCATE/VACUUM/REINDEX/CLUSTER siguen absolutamente
# prohibidos en todo el módulo, sin excepción.

FORBIDDEN_SQL_KEYWORDS = (
    "UPDATE",
    "TRUNCATE",
    "ALTER",
    "DROP",
    "VACUUM",
    "REINDEX",
    "CLUSTER",
)

ALLOWED_INSERT_CONSTANTS = {"INSERT_JOBS_TEMPLATE", "INSERT_JOB_SKILLS_QUERY"}
ALLOWED_DELETE_CONSTANTS = {"DELETE_JOBS_BY_IDS_QUERY"}
FOR_UPDATE_LOCK_CONSTANTS = {
    "JOBS_BY_IDS_FOR_UPDATE_TEMPLATE",
    "JOB_SKILLS_BY_JOB_IDS_FOR_UPDATE_QUERY",
}


def _sql_constants():
    return {
        name: value
        for name, value in vars(dr).items()
        if name.isupper()
        and isinstance(value, str)
        and name.endswith(("_SQL", "_QUERY", "_TEMPLATE"))
    }


def test_no_forbidden_keywords_in_sql_constants():
    sql_constants = _sql_constants()
    # Si esto viene vacio, el propio test esta mal escrito (falso verde).
    assert len(sql_constants) >= 5
    for name, sql in sql_constants.items():
        # "FOR UPDATE" (bloqueo de filas de un SELECT) no es la escritura
        # "UPDATE" que este test prohibe -- se excluye antes de comparar.
        upper = sql.upper().replace("FOR UPDATE", "")
        for kw in FORBIDDEN_SQL_KEYWORDS:
            assert kw not in upper, f"{name} contiene la palabra prohibida {kw!r}: {sql!r}"


def test_for_update_locks_are_read_only_and_confined_to_two_constants():
    """FOR UPDATE debe ser siempre un SELECT de bloqueo (nunca una
    escritura real: sin SET, sin DELETE/INSERT), y las dos constantes de
    FOR_UPDATE_LOCK_CONSTANTS deben ser las UNICAS del modulo que
    contienen la palabra UPDATE -- cualquier otra aparicion seria una
    escritura real no auditada."""
    for name in FOR_UPDATE_LOCK_CONSTANTS:
        sql = getattr(dr, name).upper()
        assert sql.startswith("SELECT"), f"{name} no es un SELECT: {sql!r}"
        assert sql.rstrip().endswith("FOR UPDATE"), f"{name} no termina en FOR UPDATE: {sql!r}"
        assert " SET " not in sql

    assert FOR_UPDATE_LOCK_CONSTANTS <= set(_sql_constants())
    for name, value in _sql_constants().items():
        if name in FOR_UPDATE_LOCK_CONSTANTS:
            continue
        assert "UPDATE" not in value.upper(), f"{name} contiene UPDATE inesperado: {value!r}"


def test_insert_sql_is_confined_to_whitelisted_restore_constants():
    """INSERT solo puede aparecer en los dos constantes del restore -- en
    cualquier otra constante SQL, sería una regresión grave (escritura no
    prevista fuera del mecanismo de restore ya auditado)."""
    for name, sql in _sql_constants().items():
        if "INSERT" in sql.upper():
            assert name in ALLOWED_INSERT_CONSTANTS, (
                f"{name} contiene INSERT fuera de la lista blanca: {sql!r}"
            )
    # Si la lista blanca ya no coincide con lo que existe de verdad en el
    # modulo, el test tambien esta mal (evita un falso verde silencioso).
    assert ALLOWED_INSERT_CONSTANTS <= set(_sql_constants())


def test_insert_constants_are_always_on_conflict_do_nothing():
    """Los dos INSERT del restore nunca pueden convertirse en un UPSERT que
    sobrescriba una fila existente -- DO NOTHING es la unica salida posible
    de un conflicto de PK/unicidad."""
    for name in ALLOWED_INSERT_CONSTANTS:
        sql = getattr(dr, name).upper()
        assert "ON CONFLICT" in sql, f"{name} no tiene ON CONFLICT: {sql!r}"
        assert "DO NOTHING" in sql, f"{name} no es DO NOTHING: {sql!r}"
        assert "DO UPDATE" not in sql, f"{name} permite sobrescribir (DO UPDATE): {sql!r}"


def test_delete_sql_is_confined_to_whitelisted_constant():
    """DELETE solo puede aparecer en el unico constante del delete -- en
    cualquier otra constante SQL séria una regresion grave (escritura
    destructiva no prevista fuera del mecanismo de delete ya auditado)."""
    for name, sql in _sql_constants().items():
        if "DELETE" in sql.upper():
            assert name in ALLOWED_DELETE_CONSTANTS, (
                f"{name} contiene DELETE fuera de la lista blanca: {sql!r}"
            )
    assert ALLOWED_DELETE_CONSTANTS <= set(_sql_constants())


def test_delete_constant_only_targets_jobs_by_id():
    """El unico DELETE del modulo debe apuntar exclusivamente a `jobs` por
    `id` -- nunca a `job_skills`/`skills` (ON DELETE CASCADE hace ese
    trabajo) y nunca sin condicion WHERE (borraria la tabla entera)."""
    sql = dr.DELETE_JOBS_BY_IDS_QUERY.upper()
    assert sql.startswith("DELETE FROM JOBS"), f"No apunta a jobs: {sql!r}"
    assert "WHERE ID = ANY" in sql, f"Sin condicion segura por id: {sql!r}"
    assert "SKILLS" not in sql  # ni job_skills ni skills -- ON DELETE CASCADE hace ese trabajo


def test_module_source_has_no_write_cursor_execute_calls():
    """
    Red de seguridad adicional: ninguna llamada real a cur.execute()/
    conn.commit() en el modulo debe coexistir con las palabras prohibidas
    en la MISMA linea (cubre el caso de que alguien construya SQL inline
    en vez de en una constante con nombre). INSERT y DELETE quedan fuera de
    esta lista por el mismo motivo que en los tests anteriores.
    """
    source = Path(dr.__file__).read_text(encoding="utf-8")
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        upper = line.upper()
        if "EXECUTE(" not in upper and ".COMMIT(" not in upper:
            continue
        for kw in FORBIDDEN_SQL_KEYWORDS:
            assert kw not in upper, f"Línea {lineno} combina execute()/commit() con {kw!r}: {line!r}"
