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


# =============================================================================
# DQ-15 — "jest" es el verbo "ser/estar" en polaco
# =============================================================================
#
# El patrón original r"\bjest\b" se compila con re.IGNORECASE y coincidía con el
# verbo polaco, presente en el 31% de las ofertas de PL de la muestra auditada.
# Se detectaron 9.735 vínculos falsos en Supabase (12,96% de las activas de PL).
# El patrón nuevo es sensible a mayúsculas vía (?-i:...): acepta "Jest" y "JEST".
# Todas las frases polacas de estos tests son texto REAL de ofertas de la BD.


def _names(text):
    """Atajo: devuelve solo los nombres de las skills detectadas en el texto."""
    return [s["name"] for s in _extract_skills(text)]


def test_jest_verbo_polaco_no_se_detecta():
    """Frase real de una oferta polaca: 'jest' es el verbo, no el framework."""
    texto = "rolą Project Managera jest stworzenie warunków, w których właściwe decyzje"
    assert "Jest" not in _names(texto)


def test_jest_varias_frases_polacas_normales_no_detectan():
    """Frases reales de ofertas polacas con 'jest' en uso lingüístico normal."""
    frases = [
        "TeamQuest jest polską, dynamicznie rozwijającą się firmą",
        "PTD Partner jest niezależną firmą współpracującą z Glovo",
        "Sprawdź, czy ta rola jest dla Ciebie: Nasze wymagania",
        "Celem projektu jest zaprojektowanie i nadzór nad budową nowego systemu",
        "Oferta jest skierowana wyłącznie do osób pełnoletnich",
    ]
    for frase in frases:
        assert "Jest" not in _names(frase), f"falso positivo en: {frase}"


def test_jest_repetido_en_texto_polaco_no_genera_skill():
    """Un texto polaco con 'jest' muchas veces no debe producir la skill Jest."""
    texto = (
        "Naszym celem jest rozwój. Firma jest liderem rynku. Praca jest zdalna. "
        "Zespół jest międzynarodowy. Wynagrodzenie jest atrakcyjne. To jest oferta."
    )
    assert "Jest" not in _names(texto)


def test_jest_tecnologico_mayuscula_si_se_detecta():
    """Contexto tecnológico inequívoco: 'Jest' capitalizado en un stack real."""
    texto = "TypeScript HTML CSS SCSS npm yarn React Router Formik Bootstrap Redux Jest Vitest"
    assert "Jest" in _names(texto)


def test_jest_junto_a_testing_y_javascript_si_se_detecta():
    """Jest citado junto a testing/JavaScript, el caso de uso más habitual."""
    texto = "Writing unit tests with Jest and React Testing Library for our JavaScript codebase."
    names = _names(texto)
    assert "Jest" in names
    assert "React" in names


def test_jest_mayusculas_completas_si_se_detecta():
    """Caso real de una oferta francesa: 'JEST' en mayúsculas dentro del stack."""
    texto = "L'écriture de tests unitaires et fonctionnels (JEST, Playwright)"
    names = _names(texto)
    assert "Jest" in names
    assert "Playwright" in names


def test_jest_js_variante_explicita_si_se_detecta():
    """La variante escrita 'jest.js' o 'jestjs' se acepta aunque vaya en minúscula."""
    assert "Jest" in _names("Testing stack based on jest.js and supertest")
    assert "Jest" in _names("Experience with JestJS is a plus")


def test_jest_falso_positivo_no_rompe_otras_skills_de_la_oferta():
    """
    Una oferta polaca real con 'jest' lingüístico sigue detectando su stack.

    El fix no debe tener efectos colaterales sobre el resto del catálogo.
    """
    texto = (
        "TeamQuest jest polską firmą. Poszukujemy Python developera. "
        "Wymagania: Docker, Kubernetes, PostgreSQL oraz znajomość AWS."
    )
    names = _names(texto)
    assert "Jest" not in names
    for esperada in ("Python", "Docker", "Kubernetes", "PostgreSQL", "AWS"):
        assert esperada in names, f"se perdió {esperada}"
