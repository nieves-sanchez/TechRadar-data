"""
test_enrich.py — Tests 6-14 de §57.6.10 (notes/PROJECT_MASTER_CONTEXT.md) para scripts/enrich.py.

FASE B, Paso 2: capa de cálculo puro (sin BD). Estos tests protegen exclusivamente las reglas
conservadoras de `remote` (6-10) y `role_category` (11-14), tal como quedaron cerradas en
§57.6.5 y §57.6.6. Los tests 1-5, 15-17 (skills, idempotencia, integración — requieren capa de
BD) pertenecen al Paso 3 y no se implementan aquí.

No conecta a Supabase. No usa Ollama.
"""

from unittest.mock import patch

from scripts.enrich import compute_remote_update, compute_role_category_update


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
