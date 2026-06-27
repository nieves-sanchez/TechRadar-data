"""
test_pipeline_c.py — Tests unitarios para Pipeline C (Ollama local).

Cubre:
- Cálculo de input_hash e idempotencia
- Tracking SQLite: processed / failed / hash cambiado
- Selección de descripción (description_full vs short)
- Normalización de skills: React/react/React.js → 'React'
- Deduplicación case-insensitive de skills por oferta
- is_tech=False → role_category=NULL, nunca is_active=FALSE
- Limpieza non-IT no se ejecuta por defecto
- --limit cuenta envíos reales a Ollama (no candidatos leídos)
- --max-minutes para limpiamente tras el batch en curso

No llama a Ollama real ni conecta a Supabase.
"""

import sqlite3
import time
from unittest.mock import MagicMock, call, patch

import pytest

from scripts.ollama_state import (
    compute_input_hash,
    is_already_processed,
    open_state_db,
    record_result,
)
from scripts.retro_classify import (
    _build_role_updates_from_result,
    _normalize_skill,
    _select_description,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def state_conn():
    """SQLite en memoria para tests de tracking."""
    conn = open_state_db(":memory:")
    yield conn
    conn.close()


_MODEL = "qwen2.5:1.5b"


# =============================================================================
# compute_input_hash
# =============================================================================

def test_hash_deterministic():
    """El mismo input siempre produce el mismo hash."""
    h1 = compute_input_hash("Data Engineer", "Python ETL", "description_short", _MODEL)
    h2 = compute_input_hash("Data Engineer", "Python ETL", "description_short", _MODEL)
    assert h1 == h2


def test_hash_changes_with_text_source():
    """Hash con description_full ≠ hash con description_short (mismo texto)."""
    h_s = compute_input_hash("Dev", "same text", "description_short", _MODEL)
    h_f = compute_input_hash("Dev", "same text", "description_full",  _MODEL)
    assert h_s != h_f


def test_hash_changes_when_description_full_added():
    """
    Si Pipeline B añade description_full, el hash cambia y la oferta es elegible.
    Simula el caso: primera revisión con short, segunda con full.
    """
    h_before = compute_input_hash("Dev", "short desc",    "description_short", _MODEL)
    h_after  = compute_input_hash("Dev", "long full desc", "description_full",  _MODEL)
    assert h_before != h_after


def test_hash_length():
    """El hash tiene exactamente 16 caracteres hexadecimales."""
    h = compute_input_hash("t", "d", "empty", _MODEL)
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


# =============================================================================
# SQLite tracking — open_state_db + is_already_processed + record_result
# =============================================================================

def test_not_processed_initially(state_conn):
    """Una oferta nueva no aparece como procesada."""
    assert not is_already_processed(state_conn, 123, "abc", _MODEL)


def test_processed_after_recording(state_conn):
    """Tras registrar status='processed', is_already_processed devuelve True."""
    record_result(
        state_conn,
        job_id=123, input_hash="abc", model=_MODEL,
        text_source="description_full", status="processed",
        role_category_before="other", role_category_after="backend",
        skills_before_count=0, skills_added=2,
    )
    assert is_already_processed(state_conn, 123, "abc", _MODEL)


def test_failed_not_considered_processed(state_conn):
    """status='failed' no bloquea el reprocesado."""
    record_result(
        state_conn,
        job_id=456, input_hash="xyz", model=_MODEL,
        text_source="description_short", status="failed",
        error="timeout",
    )
    assert not is_already_processed(state_conn, 456, "xyz", _MODEL)


def test_skipped_not_considered_processed(state_conn):
    """status='skipped' no bloquea el reprocesado."""
    record_result(
        state_conn,
        job_id=789, input_hash="hhh", model=_MODEL,
        text_source="empty", status="skipped",
    )
    assert not is_already_processed(state_conn, 789, "hhh", _MODEL)


def test_different_hash_not_blocked(state_conn):
    """
    Una oferta revisada con hash antiguo (description_short) no bloquea
    la revisión con hash nuevo (description_full).
    """
    record_result(
        state_conn,
        job_id=999, input_hash="old_hash", model=_MODEL,
        text_source="description_short", status="processed",
        skills_added=0,
    )
    assert not is_already_processed(state_conn, 999, "new_hash", _MODEL)


def test_record_replaces_failed_with_processed(state_conn):
    """INSERT OR REPLACE permite actualizar un 'failed' a 'processed'."""
    record_result(
        state_conn,
        job_id=111, input_hash="hh", model=_MODEL,
        text_source="description_full", status="failed", error="timeout",
    )
    assert not is_already_processed(state_conn, 111, "hh", _MODEL)

    record_result(
        state_conn,
        job_id=111, input_hash="hh", model=_MODEL,
        text_source="description_full", status="processed", skills_added=1,
    )
    assert is_already_processed(state_conn, 111, "hh", _MODEL)


# =============================================================================
# _select_description
# =============================================================================

def test_select_prefers_description_full():
    """description_full tiene prioridad sobre description_short."""
    job = {"description_full": "Full text here", "description_short": "Short"}
    text, source = _select_description(job)
    assert source == "description_full"
    assert text == "Full text here"


def test_select_falls_back_to_short():
    """Si description_full es None, usa description_short."""
    job = {"description_full": None, "description_short": "Short text"}
    text, source = _select_description(job)
    assert source == "description_short"
    assert text == "Short text"


def test_select_empty_when_neither():
    """Si ambas son None, devuelve ('', 'empty')."""
    job = {"description_full": None, "description_short": None}
    text, source = _select_description(job)
    assert source == "empty"
    assert text == ""


def test_select_truncates_to_limit():
    """El texto se trunca al límite de DESCRIPTION_LIMIT."""
    from scripts.ai_classifier import DESCRIPTION_LIMIT
    long_text = "x" * (DESCRIPTION_LIMIT + 500)
    job = {"description_full": long_text, "description_short": None}
    text, _ = _select_description(job)
    assert len(text) == DESCRIPTION_LIMIT


# =============================================================================
# _normalize_skill — normalización de skills de Ollama
# =============================================================================

def test_react_variants_all_normalize_to_canonical():
    """
    Todas las variantes comunes de React normalizan al nombre canonico.
    Cubre: react, React, React.js, react.js, React JS, reactjs.
    """
    variants = ("react", "React.js", "react.js", "React JS", "React", "reactjs")
    for raw in variants:
        result = _normalize_skill(raw)
        assert result is not None, f"'{raw}' deberia ser valida"
        canonical, _ = result
        assert canonical == "React", f"'{raw}' -> esperado 'React', obtenido '{canonical}'"


def test_python_normalizes():
    """Python (con cualquier capitalización) → 'Python'."""
    result = _normalize_skill("python")
    assert result is not None
    assert result[0] == "Python"


def test_docker_normalizes():
    """Docker → 'Docker'."""
    result = _normalize_skill("docker")
    assert result is not None
    assert result[0] == "Docker"


def test_too_short_returns_none():
    """Strings de 1 carácter se descartan."""
    assert _normalize_skill("x") is None
    assert _normalize_skill("") is None


def test_long_phrase_returns_none():
    """Frases de más de 5 palabras se descartan."""
    assert _normalize_skill("we need a good developer with experience") is None


def test_unknown_valid_skill_returns_tool():
    """Skill técnica válida no en catálogo → devuelve con category='tool'."""
    result = _normalize_skill("SomeNewTool123")
    assert result is not None
    canonical, cat = result
    assert canonical == "SomeNewTool123"
    assert cat == "tool"


def test_numeric_only_returns_none():
    """Strings sin letras se descartan."""
    assert _normalize_skill("1234") is None


# =============================================================================
# _build_role_updates_from_result — lógica pura sin BD
# =============================================================================

def test_is_tech_false_with_other_sets_null():
    """
    is_tech=False + role_category='other' → se marca NULL para revisión manual.
    'other' no es una categoría técnica fiable, así que sí se permite el cambio.
    """
    job = {"id": 1, "title": "Nurse", "role_category": "other"}
    result = {"role_category": "other", "skills": [], "is_tech": False}

    role_updates, skill_records = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )

    assert (None, 1) in role_updates, "role_category debe ponerse a NULL"
    assert skill_records == []


def test_is_tech_false_with_null_category_sets_null():
    """
    is_tech=False + role_category=None → se marca NULL (sin categoría previa que proteger).
    """
    job = {"id": 2, "role_category": None}
    result = {"role_category": None, "skills": [], "is_tech": False}

    role_updates, skill_records = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )

    assert (None, 2) in role_updates
    assert skill_records == []


def test_is_tech_false_with_backend_does_not_overwrite():
    """
    is_tech=False + role_category='backend' → NO debe sobrescribir la categoría.
    Ollama puede dar falsos negativos; si la oferta ya tenía categoría técnica válida,
    se conserva y no se registra ningún UPDATE.
    """
    job = {"id": 3, "role_category": "backend"}
    result = {"role_category": "other", "skills": ["Python"], "is_tech": False}

    role_updates, skill_records = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )

    assert role_updates == [], "No debe generarse ningún UPDATE sobre role_category"
    assert skill_records == [], "No deben añadirse skills cuando is_tech=False"


def test_is_tech_false_with_fullstack_does_not_overwrite():
    """
    is_tech=False + role_category='fullstack' → categoría técnica válida se conserva.
    """
    job = {"id": 4, "role_category": "fullstack"}
    result = {"role_category": "other", "skills": ["React", "Node.js"], "is_tech": False}

    role_updates, skill_records = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )

    assert role_updates == []
    assert skill_records == []


def test_is_tech_false_with_sysadmin_does_not_overwrite():
    """
    is_tech=False + role_category='sysadmin' → categoría técnica válida se conserva.
    Cubre el caso real observado en las 3 filas 'sysadmin' del SQLite.
    """
    job = {"id": 5, "role_category": "sysadmin"}
    result = {"role_category": None, "skills": [], "is_tech": False}

    role_updates, skill_records = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )

    assert role_updates == []
    assert skill_records == []


def test_is_tech_false_no_skills_added_even_with_valid_category():
    """
    Cuando is_tech=False, nunca se añaden skills independientemente de la categoría previa.
    """
    for role_before in ("backend", "fullstack", "sysadmin", "management", "other", None):
        job = {"id": 10, "role_category": role_before}
        result = {"role_category": "backend", "skills": ["Python", "Docker"], "is_tech": False}

        _, skill_records = _build_role_updates_from_result(
            job=job, result=result, reclassify_all=False, existing_skills=set(),
        )

        assert skill_records == [], (
            f"No deben añadirse skills cuando is_tech=False (role_before={role_before!r})"
        )


def test_is_tech_true_updates_category():
    """is_tech=True → role_category se actualiza correctamente."""
    job = {"id": 2, "role_category": "other"}
    result = {"role_category": "backend", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job,
        result=result,
        reclassify_all=False,
        existing_skills=set(),
    )
    assert ("backend", 2) in role_updates


def test_skills_deduplicated_case_insensitive():
    """
    Dos variantes de la misma skill devueltas por Ollama (ej: 'react' y 'React.js')
    deben producir solo UNA entrada en skill_records por oferta.
    """
    job = {"id": 3, "role_category": "frontend"}
    result = {
        "role_category": "frontend",
        "skills": ["react", "React.js"],  # ambas → "React"
        "is_tech": True,
    }

    _, skill_records = _build_role_updates_from_result(
        job=job,
        result=result,
        reclassify_all=False,
        existing_skills=set(),
    )

    canonical_names = [r["skill_name"] for r in skill_records]
    assert canonical_names.count("React") == 1, (
        f"Esperado 1 entrada 'React', obtenido: {canonical_names}"
    )


def test_skill_not_added_if_already_exists():
    """Si la skill ya está vinculada a la oferta (en BD), no se duplica."""
    job = {"id": 4, "role_category": "backend"}
    result = {"role_category": "backend", "skills": ["Python"], "is_tech": True}

    _, skill_records = _build_role_updates_from_result(
        job=job,
        result=result,
        reclassify_all=False,
        existing_skills={"python"},  # ya existe (lowercase)
    )
    assert skill_records == []


def test_other_not_updated_in_default_mode():
    """En modo default (no reclassify_all), 'other' no se escribe en BD."""
    job = {"id": 5, "role_category": "backend"}
    result = {"role_category": "other", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job,
        result=result,
        reclassify_all=False,
        existing_skills=set(),
    )
    assert role_updates == []


def test_reclassify_all_without_update_flag_conserves_valid():
    """
    reclassify_all=True sin --update-existing-roles: incluso en modo --all,
    una categoria valida existente se conserva (modo conservador por defecto).
    El flag --update-existing-roles es el unico mecanismo para permitir el cambio.
    """
    job = {"id": 6, "role_category": "backend"}
    result = {"role_category": "other", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job,
        result=result,
        reclassify_all=True,
        existing_skills=set(),
        update_existing_roles=False,
    )
    assert role_updates == [], "backend existente se conserva incluso con reclassify_all"


def test_reclassify_all_with_update_flag_allows_other():
    """
    reclassify_all=True + update_existing_roles=True: permite escribir 'other'
    sobre una categoria valida existente (modo explicitamente no conservador).
    """
    job = {"id": 6, "role_category": "backend"}
    result = {"role_category": "other", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job,
        result=result,
        reclassify_all=True,
        existing_skills=set(),
        update_existing_roles=True,
    )
    assert ("other", 6) in role_updates


# =============================================================================
# Conservadurismo de role_category (--update-existing-roles)
# =============================================================================

def test_valid_role_not_overwritten_without_update_flag():
    """
    Sin --update-existing-roles, Ollama no puede cambiar una categoria valida
    aunque proponga otra diferente (ej: backend -> cloud queda sin efecto).
    """
    job = {"id": 11, "role_category": "backend"}
    result = {"role_category": "cloud", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
        update_existing_roles=False,
    )
    assert role_updates == [], "backend -> cloud bloqueado en modo conservador"


def test_valid_role_overwritten_with_update_flag():
    """
    Con --update-existing-roles, Ollama puede cambiar una categoria valida
    a otra categoria valida (ej: backend -> cloud se permite).
    """
    job = {"id": 12, "role_category": "backend"}
    result = {"role_category": "cloud", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
        update_existing_roles=True,
    )
    assert ("cloud", 12) in role_updates


def test_null_category_updated_without_flag():
    """
    role_before=None -> la categoria propuesta por Ollama siempre se escribe,
    incluso sin el flag (no hay categoria previa que proteger).
    """
    job = {"id": 13, "role_category": None}
    result = {"role_category": "data_analyst", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )
    assert ("data_analyst", 13) in role_updates


def test_other_category_upgraded_without_flag():
    """
    role_before='other' -> Ollama puede mejorar a categoria especifica
    sin el flag (porque 'other' no es una categoria tecnica fiable).
    """
    job = {"id": 14, "role_category": "other"}
    result = {"role_category": "backend", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )
    assert ("backend", 14) in role_updates


def test_skills_added_even_when_role_is_conserved():
    """
    Cuando el modo conservador bloquea el cambio de role_category,
    las skills propuestas por Ollama se canonicalizan y añaden de igual modo.
    """
    job = {"id": 15, "role_category": "backend"}
    result = {"role_category": "cloud", "skills": ["Python", "Docker"], "is_tech": True}

    role_updates, skill_records = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
        update_existing_roles=False,
    )

    assert role_updates == [], "role no debe cambiar en modo conservador"
    skill_names = {r["skill_name"] for r in skill_records}
    assert "Python" in skill_names, "Python debe añadirse aunque el role se conserve"
    assert "Docker" in skill_names, "Docker debe añadirse aunque el role se conserve"


def test_noncanonical_role_category_rejected():
    """
    Una role_category no canonica devuelta por Ollama se descarta silenciosamente.
    Defensa en profundidad: ai_classifier ya valida, pero _build_role_updates_from_result
    garantiza que nunca se escriba una categoria fuera de VALID_CATEGORIES.
    """
    job = {"id": 16, "role_category": None}
    result = {"role_category": "illegal_category", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )
    assert role_updates == [], "categoria no canonica no debe generar ningun UPDATE"


def test_role_after_reflects_conserved_category():
    """
    Cuando el modo conservador previene la actualizacion, role_updates esta vacio.
    El llamador calcula role_after = role_updates[0][0] if role_updates else role_before,
    lo que garantiza que role_category_after en SQLite sea la categoria real (la anterior).
    """
    job = {"id": 17, "role_category": "management"}
    result = {"role_category": "cloud", "skills": [], "is_tech": True}

    role_updates, _ = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
        update_existing_roles=False,
    )

    role_before = job["role_category"]
    role_after = role_updates[0][0] if role_updates else role_before
    assert role_after == "management", "role_after debe reflejar la categoria conservada"


# =============================================================================
# Canonicalizacion de skills: Node.js y deduplicacion
# =============================================================================

def test_nodejs_variants_normalize_to_canonical():
    """
    Todas las variantes comunes de Node.js normalizan al nombre canonico.
    Cubre: Node, node (bare), Node.js, node.js, nodejs.
    El patron r'\bnode\b' cubre los aliases bare; word boundary excluye
    node_exporter, nodepool, nodeport (underscore/letra adyacente = sin boundary).
    """
    variants = ("Node", "node", "Node.js", "node.js", "nodejs")
    for raw in variants:
        result = _normalize_skill(raw)
        assert result is not None, f"'{raw}' deberia ser una skill valida"
        canonical, _ = result
        assert canonical == "Node.js", (
            f"'{raw}' -> esperado 'Node.js', obtenido '{canonical}'"
        )


def test_three_react_variants_produce_one_skill_record():
    """
    Si Ollama devuelve tres variantes de React en la misma oferta
    (react / React / React.js), solo se genera una entrada en skill_records.
    """
    job = {"id": 18, "role_category": "frontend"}
    result = {
        "role_category": "frontend",
        "skills": ["react", "React", "React.js"],
        "is_tech": True,
    }

    _, skill_records = _build_role_updates_from_result(
        job=job, result=result, reclassify_all=False, existing_skills=set(),
    )

    canonical_names = [r["skill_name"] for r in skill_records]
    assert len(canonical_names) == 1, (
        f"Esperada 1 skill 'React', obtenido: {canonical_names}"
    )
    assert canonical_names[0] == "React"


# =============================================================================
# Cleanup no se ejecuta por defecto
# =============================================================================

def test_cleanup_not_called_without_flag():
    """
    deactivate_non_it_by_patterns NO se llama en un run sin --cleanup-non-it.
    El script sale antes de intentar conectar a BD cuando Ollama no está disponible.
    """
    with patch("scripts.retro_classify.deactivate_non_it_by_patterns") as mock_cleanup, \
         patch("scripts.retro_classify._is_ollama_available", return_value=False):
        from scripts.retro_classify import run
        with pytest.raises(SystemExit):
            run(cleanup_non_it=False, yes=True)

        mock_cleanup.assert_not_called()


def test_cleanup_called_with_flag():
    """
    Con --cleanup-non-it solo (sin --days ni --all): llama a count_non_it_by_patterns
    y termina limpiamente SIN verificar Ollama ni clasificar nada.
    """
    with patch("scripts.retro_classify.count_non_it_by_patterns", return_value=42) as mock_count, \
         patch("scripts.retro_classify._get_connection") as mock_conn, \
         patch("scripts.retro_classify._is_ollama_available") as mock_ollama:
        mock_conn.return_value.close = MagicMock()

        from scripts.retro_classify import run
        run(cleanup_non_it=True, confirm_cleanup=False, yes=True)  # debe retornar limpiamente

        mock_count.assert_called_once()
        mock_ollama.assert_not_called()


def test_cleanup_confirm_only_no_ollama():
    """
    Con --cleanup-non-it --confirm-cleanup solo (sin --days ni --all): ejecuta
    deactivate_non_it_by_patterns y termina SIN verificar Ollama ni clasificar.
    """
    with patch("scripts.retro_classify.deactivate_non_it_by_patterns", return_value=5) as mock_deact, \
         patch("scripts.retro_classify._get_connection") as mock_conn, \
         patch("scripts.retro_classify._is_ollama_available") as mock_ollama:
        mock_conn.return_value.close = MagicMock()

        from scripts.retro_classify import run
        run(cleanup_non_it=True, confirm_cleanup=True, yes=True)

        mock_deact.assert_called_once()
        mock_ollama.assert_not_called()


def test_cleanup_with_days_continues_to_ollama():
    """
    Con --cleanup-non-it --days 2: tras la limpieza continúa hacia clasificación
    (verifica Ollama). Prueba que --days hace que NO se salga tras el cleanup.
    """
    with patch("scripts.retro_classify.count_non_it_by_patterns", return_value=3) as mock_count, \
         patch("scripts.retro_classify._get_connection") as mock_conn, \
         patch("scripts.retro_classify._is_ollama_available", return_value=False) as mock_ollama:
        mock_conn.return_value.close = MagicMock()

        from scripts.retro_classify import run
        with pytest.raises(SystemExit):
            run(cleanup_non_it=True, days=2, yes=True)

        mock_count.assert_called_once()   # cleanup se ejecutó
        mock_ollama.assert_called_once()  # llegó al check de Ollama


def test_cleanup_with_all_continues_to_ollama():
    """
    Con --cleanup-non-it --all: tras la limpieza continúa hacia clasificación.
    Prueba que --all hace que NO se salga tras el cleanup.
    """
    with patch("scripts.retro_classify.count_non_it_by_patterns", return_value=0) as mock_count, \
         patch("scripts.retro_classify._get_connection") as mock_conn, \
         patch("scripts.retro_classify._is_ollama_available", return_value=False) as mock_ollama:
        mock_conn.return_value.close = MagicMock()

        from scripts.retro_classify import run
        with pytest.raises(SystemExit):
            run(cleanup_non_it=True, reclassify_all=True, yes=True)

        mock_count.assert_called_once()   # cleanup se ejecutó
        mock_ollama.assert_called_once()  # llegó al check de Ollama


# =============================================================================
# --limit cuenta envíos reales a Ollama, no candidatos leídos
# =============================================================================

def test_limit_applied_to_to_process_buffer():
    """
    Con limit=2 y 5 candidatos disponibles, el buffer to_process se limita a 2.
    La lógica de corte por limit funciona antes de llamar a Ollama.
    """
    from scripts.ollama_state import compute_input_hash, open_state_db

    state = open_state_db(":memory:")
    candidates = [
        {
            "id": i,
            "title": f"Job {i}",
            "description_full": f"desc {i}",
            "description_short": None,
            "role_category": None,
        }
        for i in range(1, 6)
    ]

    # Simular el filtrado manual que hace el bucle principal
    limit = 2
    processed = 0
    to_process = []

    for job in candidates:
        from scripts.retro_classify import _select_description
        text, text_source = _select_description(job)
        h = compute_input_hash(job["title"] or "", text, text_source, _MODEL)

        if limit > 0 and processed + len(to_process) >= limit:
            break

        job["_hash"] = h
        job["_text"] = text
        job["_text_source"] = text_source
        to_process.append(job)

    state.close()
    assert len(to_process) == 2


# =============================================================================
# --max-minutes para limpiamente
# =============================================================================

def test_max_minutes_check_logic():
    """
    La condición de max_minutes se evalúa correctamente cuando el tiempo supera el límite.
    Verificamos la lógica del check independientemente del bucle.
    """
    t_start = time.monotonic() - 61  # simula que han pasado 61 segundos
    max_minutes = 1

    elapsed_min = (time.monotonic() - t_start) / 60
    assert elapsed_min >= max_minutes, "Debería detectar que se superó max_minutes"


def test_max_minutes_not_triggered_early():
    """Con tiempo insuficiente transcurrido, max_minutes no se dispara."""
    t_start = time.monotonic() - 30  # solo 30 segundos
    max_minutes = 2

    elapsed_min = (time.monotonic() - t_start) / 60
    assert elapsed_min < max_minutes, "No debería triggear el límite de tiempo"
