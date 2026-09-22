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
    logica real de clasificacion/batching/idempotencia sin Postgres."""

    def __init__(self, jobs=None, job_skills=None, skills=None):
        self.jobs: dict = {int(k): dict(v) for k, v in (jobs or {}).items()}
        self.job_skills: set = set(job_skills or set())
        self.skills: set = set(skills or set())

    def insert_job(self, row: dict) -> None:
        self.jobs.setdefault(row["id"], dict(row))  # ON CONFLICT (id) DO NOTHING

    def insert_job_skill(self, pair) -> None:
        self.job_skills.add(tuple(pair))  # ON CONFLICT (job_id, skill_id) DO NOTHING


class _FakeRestoreCursor:
    def __init__(self, db: _FakeRestoreDB):
        self.db = db
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if sql in (dr.SET_TIMEZONE_SQL, dr.SET_ISOLATION_SQL):
            self._result = []
        elif " FROM jobs WHERE id = ANY" in sql:
            cols = [c.strip() for c in sql[len("SELECT "): sql.index(" FROM jobs")].split(",")]
            ids = params[0]
            self._result = [
                tuple(self.db.jobs[i][c] for c in cols) for i in ids if i in self.db.jobs
            ]
        elif sql == dr.JOB_SKILLS_BY_JOB_IDS_QUERY:
            ids = set(params[0])
            self._result = [pair for pair in self.db.job_skills if pair[0] in ids]
        elif sql == dr.SKILLS_BY_IDS_QUERY:
            ids = set(params[0])
            self._result = [(sid,) for sid in ids if sid in self.db.skills]
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

    def cursor(self, *a, **kw):
        return _FakeRestoreCursor(self.db)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


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


def _write_tiny_restore_backup(tmp_path, *, jobs_rows, job_skills_rows, skills_rows):
    jobs_path = tmp_path / "jobs.jsonl.gz"
    job_skills_path = tmp_path / "job_skills.jsonl.gz"
    skills_path = tmp_path / "skills_snapshot.jsonl.gz"
    candidate_ids_path = tmp_path / "candidate_ids.jsonl.gz"

    jobs_result = dr.export_rows_to_jsonl_gz(jobs_rows, RESTORE_COLUMNS, jobs_path)
    job_skills_result = dr.export_rows_to_jsonl_gz(job_skills_rows, dr.JOB_SKILLS_COLUMNS, job_skills_path)
    skills_result = dr.export_rows_to_jsonl_gz(skills_rows, dr.SKILLS_COLUMNS, skills_path)
    candidate_rows = [(row[0],) for row in jobs_rows]
    candidate_result = dr.export_rows_to_jsonl_gz(candidate_rows, dr.CANDIDATE_IDS_COLUMNS, candidate_ids_path)

    manifest = {
        "format_version": "1.0",
        "jobs_columns": RESTORE_COLUMNS,
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
# Seguridad — ninguna constante SQL del módulo contiene palabras prohibidas
# =============================================================================
#
# INSERT ya NO está en esta lista desde que existe --restore: es una
# operación legítima y deliberada (restaurar desde el backup), pero
# CONFINADA exclusivamente a los dos constantes de ALLOWED_INSERT_CONSTANTS,
# y SIEMPRE "ON CONFLICT ... DO NOTHING" (nunca "DO UPDATE", nunca podría
# sobrescribir una fila existente) — ver los dos tests dedicados más abajo.
# DELETE/UPDATE/ALTER/DROP/TRUNCATE/VACUUM/REINDEX/CLUSTER siguen
# absolutamente prohibidos en todo el módulo, sin excepción.

FORBIDDEN_SQL_KEYWORDS = (
    "DELETE",
    "UPDATE",
    "TRUNCATE",
    "ALTER",
    "DROP",
    "VACUUM",
    "REINDEX",
    "CLUSTER",
)

ALLOWED_INSERT_CONSTANTS = {"INSERT_JOBS_TEMPLATE", "INSERT_JOB_SKILLS_QUERY"}


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
        upper = sql.upper()
        for kw in FORBIDDEN_SQL_KEYWORDS:
            assert kw not in upper, f"{name} contiene la palabra prohibida {kw!r}: {sql!r}"


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


def test_module_source_has_no_write_cursor_execute_calls():
    """
    Red de seguridad adicional: ninguna llamada real a cur.execute()/
    conn.commit() en el modulo debe coexistir con las palabras prohibidas
    en la MISMA linea (cubre el caso de que alguien construya SQL inline
    en vez de en una constante con nombre). INSERT queda fuera de esta
    lista por el mismo motivo que en el test anterior.
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
