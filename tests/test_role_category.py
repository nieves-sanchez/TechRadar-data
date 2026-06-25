"""
Protege _classify_role() en transform.py contra cambios en el orden de prioridad
de ROLE_KEYWORDS y contra fallos en NON_IT_PATTERNS.

Riesgo: añadir o reordenar categorías en ROLE_KEYWORDS puede cambiar silenciosamente
la clasificación de miles de ofertas. La distinción None vs 'other' también es
crítica: None → soft-delete en retro_classify; 'other' → pasa a Ollama.
"""

from scripts.transform import _classify_role


def test_ml_engineer_is_ai_ml():
    """ML Engineer debe clasificarse como ai_ml, no como backend.

    Si el orden de ROLE_KEYWORDS cambia y backend se evalúa antes que ai_ml,
    'engineer' matchearía backend y este test lo detectaría.
    """
    assert _classify_role("ML Engineer") == "ai_ml"


def test_product_manager_is_management():
    """Product Manager debe clasificarse como management, no como otra categoría."""
    assert _classify_role("Product Manager") == "management"


def test_werkstudent_java_is_non_it():
    """NON_IT_PATTERNS tiene prioridad absoluta sobre ROLE_KEYWORDS.

    'Werkstudent' activa NON_IT_PATTERNS y devuelve None aunque el título
    contenga keywords IT como 'Java' y 'Developer'.
    """
    assert _classify_role("Werkstudent Java Developer") is None


def test_fallback_to_description():
    """Si el título no clasifica, el fallback a ROLE_DESC_KEYWORDS funciona."""
    result = _classify_role("Specialist", description="We need a data engineer with ETL experience")
    assert result == "data_engineering"
