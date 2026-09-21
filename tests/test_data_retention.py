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
# Seguridad — ninguna constante SQL del módulo contiene palabras prohibidas
# =============================================================================

FORBIDDEN_SQL_KEYWORDS = (
    "DELETE",
    "UPDATE",
    "INSERT",
    "TRUNCATE",
    "ALTER",
    "DROP",
    "VACUUM",
    "REINDEX",
    "CLUSTER",
)


def test_no_forbidden_keywords_in_sql_constants():
    sql_constants = {
        name: value
        for name, value in vars(dr).items()
        if name.isupper()
        and isinstance(value, str)
        and name.endswith(("_SQL", "_QUERY", "_TEMPLATE"))
    }
    # Si esto viene vacio, el propio test esta mal escrito (falso verde).
    assert len(sql_constants) >= 5
    for name, sql in sql_constants.items():
        upper = sql.upper()
        for kw in FORBIDDEN_SQL_KEYWORDS:
            assert kw not in upper, f"{name} contiene la palabra prohibida {kw!r}: {sql!r}"


def test_module_source_has_no_write_cursor_execute_calls():
    """
    Red de seguridad adicional: ninguna llamada real a cur.execute()/
    conn.commit() en el modulo debe coexistir con las palabras prohibidas
    en la MISMA linea (cubre el caso de que alguien construya SQL inline
    en vez de en una constante con nombre).
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
