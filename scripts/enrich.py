"""
enrich.py — Enriquecimiento determinista de ofertas tras la llegada de description_full.

FASE B (Paso 2 de §57.6.11 en notes/PROJECT_MASTER_CONTEXT.md): cuando Pipeline B consigue
description_full para una oferta, este módulo calcula qué cambios de skills, remote y
role_category corresponderían aplicar, siguiendo las reglas conservadoras ya cerradas en
§57.6.4-§57.6.6. Reutiliza las funciones deterministas de transform.py (title/description →
skills/remote/role_category); no reimplementa ninguna lógica de extracción ni clasificación,
y no depende de Ollama en ningún punto.

Este módulo (Paso 2) es SOLO la capa de CÁLCULO PURO: no lee ni escribe en Supabase.
La capa de persistencia (SELECT de candidatos, UPSERT de skills, UPDATE de remote/role_category)
es el Paso 3, todavía no implementado.

Reglas cerradas que este módulo hace cumplir (no reabrir sin nueva evidencia — ver §57.6.14):
    - Skills:        siempre aditivas. Este módulo solo DETECTA; nunca decide qué borrar.
    - remote:        se propone un nuevo valor solo si el actual es NULL.
    - role_category: se propone una nueva categoría solo si la actual es exactamente 'other'.
                      NULL nunca se toca (protege el marcado de revisión manual de Pipeline C).

Uso:
    from scripts.enrich import compute_enrichment
"""

from typing import Optional

from scripts.skills_catalog import ROLE_KEYWORDS
from scripts.transform import (
    _classify_role,
    _clean_description_text,
    _detect_remote,
    _extract_skills,
)

# Categorías técnicas válidas del schema (15). Fuente única: el catálogo de skills_catalog.py.
# Deliberadamente NO se reutiliza ai_classifier.VALID_CATEGORIES: ese set además incluye 'other'
# y pertenece al módulo de Ollama — FASE B no debe depender de él ni con un import inocuo.
_VALID_TECH_CATEGORIES: frozenset[str] = frozenset(ROLE_KEYWORDS.keys())


def compute_new_skills(title: Optional[str], description_full: Optional[str]) -> list[dict]:
    """
    Detecta las skills presentes en title + description_full (regla de texto, §57.6.4).

    Devuelve la lista completa detectada en el texto nuevo; no compara contra las skills
    ya vinculadas en BD ni decide cuáles insertar — esa comparación (y el filtrado de
    duplicados vía ON CONFLICT DO NOTHING de upsert_skills_and_links) es responsabilidad
    de la capa de persistencia del Paso 3. Este resultado es SIEMPRE aditivo: nunca se usa
    para retirar vínculos existentes (§57.6.4 — no se mezcla con la limpieza de DQ-15B).

    Args:
        title: Título de la oferta.
        description_full: Descripción completa recién conseguida por Pipeline B.

    Returns:
        list[dict]: [{"name": ..., "category": ...}] por skill detectada. Puede ser [].
    """
    parts = [title, _clean_description_text(description_full)]
    text = " ".join(p for p in parts if isinstance(p, str) and p.strip())
    return _extract_skills(text)


def compute_remote_update(
    current_remote: Optional[bool],
    title: Optional[str],
    description_full: Optional[str],
    location_display: Optional[str] = None,
) -> Optional[bool]:
    """
    Decide si `remote` debe actualizarse (regla cerrada, §57.6.5).

    Solo se propone un valor nuevo cuando el actual es NULL. Un TRUE/FALSE ya existente
    NUNCA se sobrescribe en FASE B, aunque el recálculo sobre el texto nuevo diera un
    resultado distinto — medido: 1/600 caso TRUE→NULL habría sido una pérdida real si se
    recalculara siempre (ver §57.6.5).

    Nota de implementación (no es una decisión de diseño nueva, replica el contrato ya
    usado por transform_jobs()/_detect_remote: description SIN limpiar, a diferencia de
    skills y role_category que sí usan _clean_description_text).

    Args:
        current_remote: Valor actual de remote en BD (True, False o None).
        title, description_full, location_display: Texto disponible de la oferta.

    Returns:
        bool: nuevo valor a escribir (True o False), solo si current_remote era None
              y el texto aporta señal.
        None: no debe tocarse — porque ya había un valor, o porque el texto nuevo
              tampoco aporta señal.
    """
    if current_remote is not None:
        return None
    return _detect_remote(title, description_full, location_display)


def compute_role_category_update(
    current_role_category: Optional[str],
    title: Optional[str],
    description_full: Optional[str],
) -> Optional[str]:
    """
    Decide si `role_category` debe actualizarse (regla cerrada, §57.6.6).

    Solo se propone una categoría nueva cuando el valor actual es EXACTAMENTE 'other'.
    `NULL` nunca se toca: protege la marca de revisión manual que deja Pipeline C cuando
    Ollama detecta is_tech=False sobre una categoría no válida (Parte 6, modo conservador).
    'other' y NULL NO son equivalentes — ver §57.6.6, decisión cerrada.

    Una categoría técnica válida ya existente tampoco se toca nunca (solo 'other' es
    candidato), y una propuesta que no sea una de las 15 categorías técnicas válidas del
    catálogo se descarta silenciosamente.

    Args:
        current_role_category: Valor actual en BD ('other', NULL, o una categoría técnica).
        title, description_full: Texto disponible de la oferta.

    Returns:
        str: nueva categoría técnica válida a escribir, solo si current_role_category
             era 'other' y la propuesta es canónica.
        None: no debe tocarse.
    """
    if current_role_category != "other":
        return None
    propuesta = _classify_role(title, _clean_description_text(description_full))
    if propuesta in _VALID_TECH_CATEGORIES:
        return propuesta
    return None


def compute_enrichment(job: dict) -> dict:
    """
    Calcula, de forma pura, el enriquecimiento que correspondería aplicar a una oferta.

    No lee ni escribe en Supabase — combina los tres cálculos anteriores sobre los
    valores actuales que se le pasen. La capa de persistencia (Paso 3) es quien traduce
    este resultado a UPSERTs/UPDATEs reales, y quien decide qué ofertas son candidatas.

    Args:
        job: dict con los valores ACTUALES de la oferta. Claves esperadas:
             "title", "description_full", "location_display", "remote", "role_category".
             Las claves ausentes se tratan como None.

    Returns:
        dict con:
            "new_skills": list[dict]           — skills detectadas (siempre aditivo).
            "remote_update": bool | None        — nuevo valor de remote, o None si no se toca.
            "role_category_update": str | None  — nueva categoría, o None si no se toca.
    """
    title = job.get("title")
    description_full = job.get("description_full")
    location_display = job.get("location_display")

    return {
        "new_skills": compute_new_skills(title, description_full),
        "remote_update": compute_remote_update(
            job.get("remote"), title, description_full, location_display
        ),
        "role_category_update": compute_role_category_update(
            job.get("role_category"), title, description_full
        ),
    }
