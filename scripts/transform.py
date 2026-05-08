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

from scripts.skills_catalog import REMOTE_NEGATIVE, REMOTE_POSITIVE, ROLE_KEYWORDS, SKILLS

logger = logging.getLogger("techradar.transform")

# Compilar todos los patrones una sola vez al importar el módulo.
# Iterarlo cada vez que se procesa una oferta sería muy lento a escala.
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


def _classify_role(title: Optional[str]) -> Optional[str]:
    """
    Asigna una categoría de rol a partir del título de la oferta.

    Evalúa las categorías en orden de especificidad (de más a menos específico)
    y devuelve la primera coincidencia. Devuelve 'other' si el título existe pero
    no encaja en ninguna categoría conocida, y None si no hay título.
    """
    if not title or not isinstance(title, str):
        return None
    title_lower = title.lower()
    for category, keywords in ROLE_KEYWORDS.items():
        if any(kw in title_lower for kw in keywords):
            return category
    return "other"


def _extract_skills(text: Optional[str]) -> list[dict]:
    """
    Extrae las skills mencionadas en un texto usando el catálogo de patrones.

    Cada skill aparece como máximo una vez en el resultado aunque se mencione
    varias veces en el texto.

    Args:
        text: Texto sobre el que buscar (título + descripción concatenados).

    Returns:
        list[dict]: Lista de {"name": ..., "category": ...} por skill encontrada.
    """
    if not text or not isinstance(text, str):
        return []
    found = []
    seen: set[str] = set()
    for name, category, compiled_patterns in _SKILLS_COMPILED:
        if name in seen:
            continue
        if any(p.search(text) for p in compiled_patterns):
            found.append({"name": name, "category": category})
            seen.add(name)
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

    # 4. Deduplicar por id dentro del batch
    dupes = df.duplicated(subset=["id"], keep="first")
    if dupes.any():
        logger.warning("Eliminando %d ids duplicados en el batch.", dupes.sum())
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

    # 6. Role category
    df["role_category"] = df["title"].apply(_classify_role)

    # 7. Extracción de skills
    skill_records = []
    for _, row in df.iterrows():
        # Concatenar título y descripción para maximizar la señal de NLP.
        # description_full tiene preferencia sobre description_short.
        parts = [row.get("title") or ""]
        desc = row.get("description_full") or row.get("description_short") or ""
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
