"""
ai_classifier.py — Clasificación de roles y extracción de skills con IA local (Ollama).

Usa el modelo qwen2.5:1.5b corriendo en local vía Ollama.
Procesa ofertas en lotes de 10 para reducir el overhead de llamadas HTTP
y acelerar el procesamiento global ~3x respecto a llamadas individuales.

REQUISITO: Ollama arrancado con qwen2.5:1.5b descargado.
    ollama pull qwen2.5:1.5b
"""

import json
import logging
import re
from typing import Optional

import requests

logger = logging.getLogger("techradar.ai_classifier")

OLLAMA_URL        = "http://localhost:11434/api/generate"
OLLAMA_MODEL      = "qwen2.5:1.5b"
OLLAMA_TIMEOUT    = 15    # segundos para llamadas individuales
BATCH_TIMEOUT     = 120   # segundos para lotes de 10 ofertas con descripción completa
DESCRIPTION_LIMIT = 1500  # caracteres máximos de descripción por oferta en el prompt

VALID_CATEGORIES = {
    "backend", "frontend", "fullstack", "data_engineering", "data_science",
    "ai_ml", "devops", "cloud", "security", "qa_testing", "sysadmin",
    "erp_sap", "mobile", "management", "data_analyst", "other",
}

_CATEGORIES_STR = ", ".join(sorted(VALID_CATEGORIES))


# =============================================================================
# Utilidades internas
# =============================================================================

def _is_ollama_available() -> bool:
    """Comprueba si el servidor Ollama está arrancado."""
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _call_ollama(prompt: str, timeout: int = OLLAMA_TIMEOUT) -> Optional[str]:
    """Llama al modelo y devuelve el texto de respuesta, o None si falla."""
    try:
        payload = {
            "model":  OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.0,
                "num_predict": 800,   # límite de tokens de respuesta (10 resultados con más skills)
            },
        }
        resp = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except requests.exceptions.ConnectionError:
        logger.warning("Ollama no disponible (ConnectionError).")
        return None
    except requests.exceptions.Timeout:
        logger.warning("Ollama timeout (%ds).", timeout)
        return None
    except Exception as e:
        logger.warning("Error Ollama: %s", e)
        return None


def _extract_json(text: str) -> Optional[dict]:
    """Extrae el primer objeto JSON válido de un texto."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _clean_result(raw: dict) -> dict:
    """Valida y limpia un resultado individual del modelo."""
    category = raw.get("role_category", "other")
    if category not in VALID_CATEGORIES:
        category = "other"

    skills = []
    for s in raw.get("skills", []):
        if isinstance(s, str) and s.strip():
            s_clean = s.strip()[:80]  # máx 80 chars — el campo es VARCHAR(100)
            if len(s_clean) > 2:      # descartar respuestas de 1-2 chars (ruido)
                skills.append(s_clean)

    is_tech = raw.get("is_tech", True)
    if not isinstance(is_tech, bool):
        is_tech = str(is_tech).lower() not in ("false", "0", "no")

    return {"role_category": category, "skills": skills[:8], "is_tech": is_tech}


# =============================================================================
# Función principal: lote de hasta 10 ofertas
# =============================================================================

def classify_batch(jobs: list[dict]) -> list[dict]:
    """
    Clasifica un lote de hasta 10 ofertas en una sola llamada a Ollama.

    Args:
        jobs: lista de dicts con claves 'title' y 'description'.

    Returns:
        Lista de dicts con 'role_category', 'skills' e 'is_tech', en el
        mismo orden que la entrada. Si Ollama falla, devuelve fallbacks.
    """
    # Crear una lista de dicts independientes (no referencias al mismo objeto).
    # [dict] * N crearía N alias al mismo dict — mutar uno mutaría todos.
    fallback = [{"role_category": None, "skills": [], "is_tech": True} for _ in range(len(jobs))]
    if not jobs:
        return []

    jobs_text = ""
    for i, job in enumerate(jobs, start=1):
        title = (job.get("title") or "").strip()
        desc  = (job.get("description") or "")[:DESCRIPTION_LIMIT].strip()
        jobs_text += f"JOB_{i} | Title: {title} | Desc: {desc}\n"

    n = len(jobs)
    prompt = f"""You are a job classifier for European tech jobs. Analyze these {n} job postings.

{jobs_text}
Return a JSON object with a "results" array containing exactly {n} objects in order:
{{"results":[{{"id":1,"role_category":"...","skills":[...],"is_tech":true}}, ...]}}

Rules:
- role_category: one of [{_CATEGORIES_STR}]
- skills: up to 6 specific tech skills (languages, frameworks, tools, databases, cloud). Empty list if none found.
- is_tech: false ONLY if clearly non-IT (nurse, sales agent, driver, teacher, construction, etc.)

Return ONLY the JSON object."""

    raw = _call_ollama(prompt, timeout=BATCH_TIMEOUT)
    if raw is None:
        return fallback

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        logger.debug("Respuesta no es objeto JSON para lote de %d: %s", n, str(raw)[:300])
        return fallback

    # Extraer el array de resultados
    items = parsed.get("results", [])

    # A veces el modelo devuelve las claves numeradas en lugar de array
    if not items:
        items = [v for k, v in parsed.items() if isinstance(v, dict)]

    if not items:
        logger.debug("Array 'results' vacío en respuesta: %s", str(raw)[:300])
        return fallback

    results = [_clean_result(item) for item in items]
    # Rellenar con fallbacks si el modelo devolvió menos de N
    while len(results) < n:
        results.append({"role_category": None, "skills": [], "is_tech": True})
    return results[:n]


def classify_and_extract(title: str, description: Optional[str] = None) -> dict:
    """Wrapper individual — usa classify_batch internamente."""
    results = classify_batch([{"title": title, "description": description or ""}])
    return results[0] if results else {"role_category": None, "skills": [], "is_tech": True}
