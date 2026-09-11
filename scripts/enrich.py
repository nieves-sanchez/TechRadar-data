"""
enrich.py — Enriquecimiento determinista de ofertas tras la llegada de description_full.

FASE B (Pasos 2 y 3 de §57.6.11 en notes/PROJECT_MASTER_CONTEXT.md): cuando Pipeline B consigue
description_full para una oferta, este módulo calcula qué cambios de skills, remote y
role_category corresponden y los persiste, siguiendo las reglas conservadoras ya cerradas en
§57.6.4-§57.6.6. Reutiliza las funciones deterministas de transform.py y la persistencia de
skills de load.py; no reimplementa ninguna lógica de extracción, clasificación ni upsert, y no
depende de Ollama ni hace HTTP en ningún punto.

Dos capas:
  - CÁLCULO PURO (Paso 2): compute_*() — no leen ni escriben en BD, no dependen de nada externo.
  - PERSISTENCIA (Paso 3): enrich_jobs(conn, job_ids) — lee de `jobs`, aplica las decisiones y
    persiste en `job_skills` / `jobs.remote` / `jobs.role_category`. Nada más.

Reglas cerradas que este módulo hace cumplir (no reabrir sin nueva evidencia — ver §57.6.14):
    - Skills:        siempre aditivas. Se DETECTAN y se AÑADEN; nunca se borra un vínculo.
    - remote:        se actualiza solo si el actual es NULL.
    - role_category: se actualiza solo si el actual es exactamente 'other'.
                      NULL nunca se toca (protege el marcado de revisión manual de Pipeline C).

Uso:
    from scripts.enrich import enrich_jobs, compute_enrichment
"""

from typing import Optional

from scripts.load import upsert_skills_and_links
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


# =============================================================================
# Capa de persistencia (Paso 3 de §57.6.11)
# =============================================================================

# Columnas de `jobs` que enrich_jobs() LEE. Solo `remote` y `role_category` pueden
# además escribirse (con guardas). Ninguna otra columna se toca (§57.6.7).
_READ_COLUMNS = ("id", "title", "description_full", "location_display", "remote", "role_category")


def enrich_jobs(conn, job_ids) -> dict:
    """
    Aplica el enriquecimiento determinista de FASE B a un conjunto de ofertas ya identificadas.

    Consumidores previstos, ambos reutilizando ESTA misma función pública sin duplicar reglas
    (ninguno existe todavía; este Paso 3 solo entrega la API compartida — ver §57.6.2/3):
      - Flujo continuo: `repair_crawl.py` tras cada flush de `description_full` (Paso 4).
      - Backfill histórico: `backfill_enrich.py` por lotes (Paso 7).

    Por cada oferta:
      1. Lee title, description_full, location_display, remote, role_category.
      2. Si `description_full` no es utilizable (tras `_clean_description_text()` queda vacío) la
         salta: no hay texto nuevo que aprovechar y Pipeline A ya procesó el título en la ingesta.
      3. Calcula las decisiones con `compute_enrichment()` (capa pura del Paso 2).
      4. Persiste:
         - skills: SIEMPRE aditivo, vía `upsert_skills_and_links()` de `load.py`
           (`ON CONFLICT DO NOTHING` en skills y job_skills → sin duplicados, sin borrar nada).
         - remote: `UPDATE ... WHERE id = %s AND remote IS NULL` — la guarda en el propio SQL
           protege el contrato incluso ante un cambio concurrente entre la lectura y la escritura.
         - role_category: `UPDATE ... WHERE id = %s AND role_category = 'other'` — misma guarda.
      5. Ninguna otra columna se toca.

    Semántica transaccional (detalle de implementación resuelto en §57.6.11, coherente con el
    repositorio): toda la llamada es UNA transacción. `conn.commit()` se ejecuta como última
    sentencia, y SOLO si el lote completo tuvo éxito. Esto garantiza que **no se hace ningún
    commit parcial** — pero no más que eso:
      - Si algo lanza, la excepción propaga y `enrich_jobs()` NO hace rollback por sí misma.
      - Las modificaciones de la transacción fallida quedan sin commitear en la conexión.
        **El caller es responsable de ejecutar `conn.rollback()` antes de reutilizar `conn`**
        (mismo patrón que `repair_crawl._flush_updates` y
        `retro_classify._process_batch_with_tracking`, que tampoco hacen rollback propio).
      - Esto es especialmente relevante en PostgreSQL: tras ciertos errores SQL la transacción
        queda "abortada" (`InFailedSqlTransaction`) hasta que se hace `rollback()` — cualquier
        sentencia posterior sobre esa misma conexión fallará hasta entonces.
      - **Requisito explícito para el Paso 4:** cuando `repair_crawl.py` llame a `enrich_jobs()`,
        deberá capturar la excepción y ejecutar `conn.rollback()` antes de volver a usar esa
        conexión (p. ej. antes del siguiente `_flush_updates()`). Todavía no implementado.

    Idempotente: una segunda ejecución sobre los mismos IDs, tras un commit exitoso, no cambia
    ya nada en `jobs` (las guardas de `remote`/`role_category` ya no se cumplen) ni duplica
    vínculos en `job_skills` (`ON CONFLICT DO NOTHING`).

    Args:
        conn: Conexión psycopg2 activa. El caller es dueño de su ciclo de vida (incluido el
              rollback ante excepción).
        job_ids: Iterable de IDs de oferta. Vacío → no-op. IDs inexistentes → se ignoran.

    Returns:
        dict con contadores:
          - jobs_seen: filas de `jobs` encontradas para los IDs pedidos (real, de `len(rows)`).
          - skills_links_attempted: pares (oferta, skill) detectados y enviados a
            `upsert_skills_and_links()`. NO es el número de filas nuevas realmente insertadas en
            `job_skills` — `ON CONFLICT DO NOTHING` puede descartar en silencio los que ya
            existían (p. ej. en una segunda ejecución). No hay round-trip adicional para contar
            inserciones reales porque nada en el proyecto lo necesita todavía.
          - remote_updated / role_category_updated: filas **realmente** actualizadas, tomado de
            `cur.rowcount` tras el `UPDATE` con guarda — si la guarda no matchea (ya no era NULL/
            'other'), `rowcount` es 0 y no se cuenta.
    """
    ids = [int(i) for i in job_ids]
    stats = {
        "jobs_seen": 0,
        "skills_links_attempted": 0,
        "remote_updated": 0,
        "role_category_updated": 0,
    }
    if not ids:
        return stats

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_READ_COLUMNS)} FROM jobs WHERE id = ANY(%s)",
            (ids,),
        )
        rows = cur.fetchall()
        stats["jobs_seen"] = len(rows)

        skill_records: list[dict] = []
        for job_id, title, description_full, location_display, remote, role_category in rows:
            if not _clean_description_text(description_full):
                continue  # sin texto nuevo utilizable → nada que enriquecer

            decision = compute_enrichment({
                "title": title,
                "description_full": description_full,
                "location_display": location_display,
                "remote": remote,
                "role_category": role_category,
            })

            for skill in decision["new_skills"]:
                skill_records.append({
                    "job_id": job_id,
                    "skill_name": skill["name"],
                    "skill_category": skill["category"],
                })

            if decision["remote_update"] is not None:
                cur.execute(
                    "UPDATE jobs SET remote = %s WHERE id = %s AND remote IS NULL",
                    (decision["remote_update"], job_id),
                )
                stats["remote_updated"] += max(cur.rowcount, 0)

            if decision["role_category_update"] is not None:
                cur.execute(
                    "UPDATE jobs SET role_category = %s WHERE id = %s AND role_category = 'other'",
                    (decision["role_category_update"], job_id),
                )
                stats["role_category_updated"] += max(cur.rowcount, 0)

        # upsert_skills_and_links() devuelve len(links): los pares (job_id, skill_id) ENVIADOS
        # a la INSERT, no los realmente insertados (ON CONFLICT DO NOTHING puede descartarlos
        # en silencio). Por eso el contador se llama "attempted", no "added".
        stats["skills_links_attempted"] = upsert_skills_and_links(cur, skill_records)

    conn.commit()
    return stats
