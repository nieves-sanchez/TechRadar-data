"""
Protege _extract_skills() en transform.py contra regresiones del algoritmo longest-match.

Riesgo: un cambio en el catálogo o en el algoritmo puede producir double-match
(GitHub y GitHub Actions como dos skills distintas) o duplicados (Python × 3).
"""

from scripts.transform import _extract_skills


def test_github_actions_no_double_match():
    """GitHub Actions no debe generar también GitHub como skill separada.

    El catálogo define GitHub Actions ANTES que GitHub precisamente para que
    el longest-match descarte el span más corto de 'GitHub' cuando ya está
    cubierto por 'GitHub Actions'.
    """
    skills = _extract_skills("Experience with GitHub Actions for CI/CD pipelines required.")
    names = [s["name"] for s in skills]
    assert "GitHub Actions" in names
    assert "GitHub" not in names


def test_same_skill_not_duplicated():
    """La misma skill mencionada varias veces en el texto aparece exactamente una vez."""
    skills = _extract_skills("Python developer with Python experience and Python skills.")
    names = [s["name"] for s in skills]
    assert names.count("Python") == 1
