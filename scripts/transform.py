"""
transform.py — Transformación, validación y enriquecimiento de datos para TechRadar.

Recibe los DataFrames crudos de extract.py y produce DataFrames limpios
listos para cargar en PostgreSQL.

Funciones principales:
    transform_jobs(raw_jobs_df)  → (jobs_df, job_skills_df)
    transform_eurostat(raw_df)   → eurostat_df

Uso:
    from scripts.transform import transform_jobs, transform_eurostat
"""

import logging
import re
from typing import Optional

import pandas as pd

from scripts.skills_catalog import (
    NON_IT_PATTERNS,
    REMOTE_NEGATIVE,
    REMOTE_POSITIVE,
    ROLE_DESC_KEYWORDS,
    ROLE_KEYWORDS,
    SKILLS,
)

logger = logging.getLogger("techradar.transform")

# Compilar todos los patrones una sola vez al importar el módulo.
_SKILLS_COMPILED = [
    (name, category, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, category, patterns in SKILLS
]

_REMOTE_POS_COMPILED = [re.compile(p, re.IGNORECASE) for p in REMOTE_POSITIVE]
_REMOTE_NEG_COMPILED = [re.compile(p, re.IGNORECASE) for p in REMOTE_NEGATIVE]

# Vocabulario válido según el schema
_VALID_CONTRACT_TYPES = {"permanent", "contract"}
_VALID_CONTRACT_TIMES  = {"full_time", "part_time"}
_VALID_COUNTRIES       = {"de", "fr", "es", "nl", "pl", "it", "at", "be"}


# =============================================================================
# Funciones auxiliares (privadas)
# =============================================================================


def _is_missing(value) -> bool:
    """Devuelve True si el valor es None, NaN o cualquier tipo de NA de pandas."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _normalize_string(value) -> Optional[str]:
    """
    Limpia un string: strip, colapsa espacios internos, None si vacío.

    Maneja correctamente None, float NaN y pd.NA.
    """
    if _is_missing(value):
        return None
    s = re.sub(r"\s+", " ", str(value).strip())
    return s or None


def _normalize_contract_type(value) -> Optional[str]:
    """
    Normaliza contract_type al vocabulario del schema: 'permanent' o 'contract'.

    Adzuna puede devolver el valor ya normalizado o variantes en varios idiomas.
    """
    s = _normalize_string(value)
    if not s:
        return None
    s_lower = s.lower()
    if s_lower in _VALID_CONTRACT_TYPES:
        return s_lower
    if any(k in s_lower for k in ("permanent", "indefinido", "indefinite")):
        return "permanent"
    if any(k in s_lower for k in ("contract", "temporal", "temporary", "freelance")):
        return "contract"
    return None


def _normalize_contract_time(value) -> Optional[str]:
    """
    Normaliza contract_time al vocabulario del schema: 'full_time' o 'part_time'.
    """
    s = _normalize_string(value)
    if not s:
        return None
    s_lower = s.lower()
    if s_lower in _VALID_CONTRACT_TIMES:
        return s_lower
    if any(k in s_lower for k in ("full_time", "full time", "jornada completa", "vollzeit")):
        return "full_time"
    if any(k in s_lower for k in ("part_time", "part time", "media jornada", "teilzeit")):
        return "part_time"
    return None


def _compute_salary_mid(salary_min, salary_max) -> Optional[int]:
    """
    Calcula el punto medio del rango salarial.

    Se persiste en la tabla jobs como campo de referencia para el dashboard.
    Los agregados del dashboard usan la mediana de este campo, no la media.
    Devuelve None si alguno de los dos extremos falta o no es numérico.
    """
    if _is_missing(salary_min) or _is_missing(salary_max):
        return None
    try:
        mid = (float(salary_min) + float(salary_max)) / 2
        if pd.isna(mid):
            return None
        return round(mid)
    except (TypeError, ValueError):
        return None


def _detect_remote(
    title: Optional[str],
    description: Optional[str],
    location: Optional[str],
) -> Optional[bool]:
    """
    Detecta si la oferta es remota a partir del texto disponible.

    Busca primero señales positivas (remoto explícito); si no las hay, busca
    señales negativas (presencial explícito). Si no hay señal clara devuelve None.

    Returns:
        True  — la oferta menciona explícitamente trabajo remoto.
        False — la oferta menciona explícitamente trabajo presencial.
        None  — sin señal suficiente para determinarlo.
    """
    # Filtrar explícitamente strings válidos para evitar que NaN o pd.NA
    # se conviertan en la cadena "nan" y contaminen el texto de búsqueda.
    parts = [x for x in (title, description, location) if isinstance(x, str) and x.strip()]
    if not parts:
        return None
    combined = " ".join(parts)
    if any(p.search(combined) for p in _REMOTE_POS_COMPILED):
        return True
    if any(p.search(combined) for p in _REMOTE_NEG_COMPILED):
        return False
    return None


def _clean_description_text(text: Optional[str]) -> Optional[str]:
    """
    Limpia el texto de una descripción eliminando artefactos típicos de crawling.

    Elimina líneas demasiado cortas (botones, ítems de navegación), líneas
    duplicadas (footers repetidos, encabezados) y colapsa el resultado en un
    único párrafo normalizado listo para la extracción de skills y la clasificación.

    Args:
        text (str | None): Texto crudo de description_full o description_short.

    Returns:
        str | None: Texto limpio en una sola línea, o None si queda vacío.
    """
    if not text or not isinstance(text, str):
        return None

    lines = text.splitlines()
    cleaned_lines: list[str] = []
    seen_lines: set[str] = set()

    for line in lines:
        stripped = line.strip()
        # Descartar líneas muy cortas: casi siempre son ítems de navegación
        if len(stripped) < 15:
            continue
        # Descartar líneas duplicadas (footers, encabezados repetidos)
        normalized = re.sub(r"\s+", " ", stripped.lower())
        if normalized in seen_lines:
            continue
        seen_lines.add(normalized)
        cleaned_lines.append(stripped)

    result = " ".join(cleaned_lines)
    result = re.sub(r"\s+", " ", result).strip()
    return result if result else None


def _classify_role(
    title: Optional[str],
    description: Optional[str] = None,
) -> Optional[str]:
    """
    Asigna una categoría de rol a partir del título y, opcionalmente, la descripción.

    Pasos en orden:
      1. Detecta roles no-IT (NON_IT_PATTERNS) → devuelve None para excluirlos.
      2. Busca en el título usando ROLE_KEYWORDS (más de 600 patrones multilingüe).
      3. Si el título no encaja, busca en la descripción usando ROLE_DESC_KEYWORDS
         (frases compuestas más seguras para evitar falsos positivos en texto largo).
      4. Devuelve 'other' solo si no hay ninguna coincidencia.

    Args:
        title (str | None): Título de la oferta.
        description (str | None): Texto de la descripción (full o short).

    Returns:
        str | None: Categoría del rol, 'other', o None si es rol no-IT o sin título.
    """
    if not title or not isinstance(title, str):
        return None

    title_lower = title.lower().replace("_", " ")

    # Paso 1: descartar roles claramente no-IT antes de clasificar
    if any(pattern in title_lower for pattern in NON_IT_PATTERNS):
        return None

    # Paso 2: clasificar por título (prioridad máxima, más preciso)
    for category, keywords in ROLE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return category

    # Paso 3: fallback a descripción con patrones más conservadores
    if description and isinstance(description, str):
        desc_lower = description.lower()
        for category, desc_keywords in ROLE_DESC_KEYWORDS.items():
            if any(kw in desc_lower for kw in desc_keywords):
                return category

    return "other"


def _extract_skills(text: Optional[str]) -> list[dict]:
    """
    Extrae las skills mencionadas en un texto usando el catálogo de patrones.

    Cada skill aparece como máximo una vez aunque se mencione varias veces.
    Usa estrategia longest-match por span de texto: si un patrón compuesto
    (p.ej. "GitHub Actions") ya consumió un span, los patrones simples que sean
    subpatrones de ese span (p.ej. "GitHub") no generan una skill adicional.
    Esto evita double-match entre pares como GitHub/GitHub Actions, Spark/Spark Streaming.

    Args:
        text: Texto sobre el que buscar (título + descripción concatenados).

    Returns:
        list[dict]: Lista de {"name": ..., "category": ...} por skill encontrada.
    """
    if not text or not isinstance(text, str):
        return []

    # Recopilar todos los matches con sus spans para aplicar longest-match
    # Estructura: lista de (start, end, name, category)
    all_matches: list[tuple[int, int, str, str]] = []
    seen_names: set[str] = set()

    for name, category, compiled_patterns in _SKILLS_COMPILED:
        if name in seen_names:
            continue
        for pattern in compiled_patterns:
            match = pattern.search(text)
            if match:
                all_matches.append((match.start(), match.end(), name, category))
                seen_names.add(name)
                break  # un patrón que encaja es suficiente para esta skill

    if not all_matches:
        return []

    # Ordenar por longitud del span descendente (longest-match primero)
    all_matches.sort(key=lambda m: -(m[1] - m[0]))

    # Seleccionar matches sin solapamiento: un span ya cubierto por un match
    # más largo no genera skill adicional.
    accepted_spans: list[tuple[int, int]] = []
    found: list[dict] = []

    for start, end, name, category in all_matches:
        overlaps = any(s < end and start < e for s, e in accepted_spans)
        if not overlaps:
            accepted_spans.append((start, end))
            found.append({"name": name, "category": category})

    return found


def _validate_jobs(df: pd.DataFrame) -> pd.DataFrame:
    """
    Elimina filas que incumplirían constraints del schema al cargarse en la BD.

    Criterios de rechazo (en orden):
      1. id nulo o igual a 0 — viola la PRIMARY KEY
      2. title nulo o vacío — campo NOT NULL en el schema
      3. country_code no reconocido — viola la FOREIGN KEY a countries
      4. id duplicado dentro del mismo batch — se conserva la primera aparición

    Todos los descartes se registran en el log con el motivo y el recuento.

    Returns:
        DataFrame limpio y sin duplicados, con el índice reseteado.
    """
    initial = len(df)

    # 1. id válido
    mask_bad_id = df["id"].apply(_is_missing) | (df["id"] == 0)
    if mask_bad_id.any():
        logger.warning("Descartando %d filas sin id válido.", mask_bad_id.sum())
        df = df[~mask_bad_id].copy()

    # 2. title no nulo
    mask_no_title = df["title"].apply(_is_missing) | (df["title"].str.strip() == "")
    if mask_no_title.any():
        logger.warning("Descartando %d filas sin title.", mask_no_title.sum())
        df = df[~mask_no_title].copy()

    # 3. country_code reconocido
    if "country_code" in df.columns:
        mask_bad_country = ~df["country_code"].isin(_VALID_COUNTRIES)
        if mask_bad_country.any():
            bad = df.loc[mask_bad_country, "country_code"].unique().tolist()
            logger.warning(
                "Descartando %d filas con country_code inválido: %s",
                mask_bad_country.sum(), bad,
            )
            df = df[~mask_bad_country].copy()

    # 4. Deduplicar por id dentro del batch (los conflictos con BD se resuelven vía UPSERT)
    dupes = df.duplicated(subset=["id"], keep="first")
    if dupes.any():
        logger.warning("Eliminando %d ids duplicados en el batch (primera aparición conservada).", dupes.sum())
        df = df[~dupes].copy()

    discarded = initial - len(df)
    if discarded > 0:
        logger.info(
            "Validación: %d/%d filas conservadas (%d descartadas).",
            len(df), initial, discarded,
        )
    else:
        logger.info("Validación: todas las %d filas superaron los controles.", initial)

    return df.reset_index(drop=True)


# =============================================================================
# Funciones públicas
# =============================================================================


def transform_jobs(
    raw_jobs_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Limpia, valida y enriquece el DataFrame de ofertas de Adzuna.

    Pasos en orden:
      1. Normalización de strings (title, company, location, city)
      2. Validación y descarte de filas inválidas (_validate_jobs)
      3. Normalización de contract_type y contract_time
      4. Cálculo de salary_mid = round((salary_min + salary_max) / 2)
      5. Detección de remote (título, descripción, ubicación)
      6. Clasificación de role_category (palabras clave en el título)
      7. Extracción de skills (NLP sobre title + description_full/short)

    Args:
        raw_jobs_df: DataFrame de salida de extract_adzuna().

    Returns:
        jobs_df:       DataFrame con los campos de la tabla jobs, listo para UPSERT.
        job_skills_df: DataFrame con columnas (job_id, skill_name, skill_category).
                       load.py resuelve los skill_id contra la tabla skills.
    """
    if raw_jobs_df.empty:
        logger.warning("transform_jobs: DataFrame de entrada vacío.")
        return pd.DataFrame(), pd.DataFrame(
            columns=["job_id", "skill_name", "skill_category"]
        )

    df = raw_jobs_df.copy()

    # 1. Normalización de strings
    for col in ("title", "company", "location_display", "city"):
        if col in df.columns:
            df[col] = df[col].apply(_normalize_string)

    # Truncar a los límites VARCHAR del schema
    for col, limit in [("title", 255), ("company", 255), ("location_display", 255), ("city", 100)]:
        if col in df.columns:
            df[col] = df[col].str[:limit]

    # 2. Validación — descarta filas que romperían constraints del schema
    df = _validate_jobs(df)
    if df.empty:
        logger.error("transform_jobs: ninguna fila superó la validación.")
        return pd.DataFrame(), pd.DataFrame(
            columns=["job_id", "skill_name", "skill_category"]
        )

    # 3. Contratos
    if "contract_type" in df.columns:
        df["contract_type"] = df["contract_type"].apply(_normalize_contract_type)
    if "contract_time" in df.columns:
        df["contract_time"] = df["contract_time"].apply(_normalize_contract_time)

    # 4. Salary mid
    # Nota: la conversión PLN → EUR ya la aplica extract.py en _parse_job_record.
    # transform.py recibe los salarios ya en EUR para todos los países.
    df["salary_mid"] = df.apply(
        lambda r: _compute_salary_mid(r.get("salary_min"), r.get("salary_max")),
        axis=1,
    )

    # 5. Remote
    df["remote"] = df.apply(
        lambda r: _detect_remote(
            r.get("title"),
            r.get("description_full") or r.get("description_short"),
            r.get("location_display"),
        ),
        axis=1,
    )

    # 6. Role category — usa título + descripción para cubrir más casos.
    def _classify_role_with_desc(row) -> Optional[str]:
        """Wrapper que pasa título y descripción limpia a _classify_role."""
        title = row.get("title")
        desc_raw = row.get("description_full") or row.get("description_short")
        desc_clean = _clean_description_text(desc_raw)
        return _classify_role(title, desc_clean)

    df["role_category"] = df.apply(_classify_role_with_desc, axis=1)

    # 7. Extracción de skills
    # La descripción se limpia antes de pasarla al extractor para reducir
    # el ruido de navegación, footers y artefactos de crawling.
    skill_records = []
    for _, row in df.iterrows():
        parts = [row.get("title") or ""]
        desc_raw = row.get("description_full") or row.get("description_short") or ""
        desc = _clean_description_text(desc_raw) or ""
        parts.append(desc)
        text = " ".join(p for p in parts if isinstance(p, str) and p.strip())

        for skill in _extract_skills(text):
            skill_records.append({
                "job_id":         row["id"],
                "skill_name":     skill["name"],
                "skill_category": skill["category"],
            })

    job_skills_df = (
        pd.DataFrame(skill_records)
        if skill_records
        else pd.DataFrame(columns=["job_id", "skill_name", "skill_category"])
    )

    # Solo las columnas que corresponden a la tabla jobs del schema
    jobs_cols = [
        "id", "source", "title", "company", "location_display", "city",
        "country_code", "role_category",
        "salary_min", "salary_max", "salary_mid", "salary_is_predicted",
        "contract_type", "contract_time", "remote",
        "description_short", "description_full", "url", "posted_at",
    ]
    jobs_df = df[[c for c in jobs_cols if c in df.columns]].copy()

    skills_per_job = len(job_skills_df) / len(jobs_df) if len(jobs_df) > 0 else 0
    logger.info(
        "transform_jobs: %d ofertas listas, %d skills detectadas (%.1f de media por oferta)",
        len(jobs_df),
        len(job_skills_df),
        skills_per_job,
    )

    return jobs_df, job_skills_df


def transform_eurostat(raw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Limpia el DataFrame de Eurostat antes de cargarlo en labor_market_context.

    Los datos ya vienen bastante limpios desde extract_eurostat(), pero aquí
    se verifica la integridad mínima y se redondea el valor al formato del schema.

    Args:
        raw_df: DataFrame de salida de extract_eurostat().

    Returns:
        DataFrame listo para UPSERT en la tabla labor_market_context.
    """
    if raw_df.empty:
        logger.warning("transform_eurostat: DataFrame de entrada vacío.")
        return raw_df

    df = raw_df.dropna(subset=["country_code", "year", "value"]).copy()

    # Filtrar solo los países del proyecto por si Eurostat devolviera extras
    df = df[df["country_code"].isin(_VALID_COUNTRIES)].copy()

    df["value"] = df["value"].round(2)

    logger.info("transform_eurostat: %d registros procesados.", len(df))
    return df
