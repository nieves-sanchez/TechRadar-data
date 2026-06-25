-- =============================================================================
-- data_quality_audit.sql — TechRadar Data Quality Audit Suite
-- =============================================================================
-- Paquete de consultas SQL para auditoría de datos en Supabase/PostgreSQL.
-- Ejecutar todas las consultas en orden en Supabase SQL Editor.
--
-- Propósito: Verificar el estado real de los datos contra hallazgos del análisis
-- de calidad de datos del 2026-06-19.
--
-- Secciones:
--   1. Cobertura de descripciones
--   2. Cobertura y calidad de skills
--   3. Clasificación de roles (role_category)
--   4. Datos salariales
--   5. Agregados geográficos y análisis cruzados
-- =============================================================================


-- =============================================================================
-- SECCIÓN 1 — COBERTURA DE DESCRIPCIONES
-- =============================================================================

-- Q01 — Cobertura de description_full por país
-- Verifica qué proporción de ofertas activas en cada país tienen el texto completo.
SELECT
    country_code,
    COUNT(*)                                                            AS total_activas,
    COUNT(*) FILTER (WHERE description_full IS NOT NULL)               AS con_desc_full,
    COUNT(*) FILTER (WHERE description_full IS NULL)                   AS sin_desc_full,
    COUNT(*) FILTER (WHERE description_short IS NOT NULL)              AS con_desc_short,
    ROUND(
        COUNT(*) FILTER (WHERE description_full IS NOT NULL)
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_desc_full,
    ROUND(
        COUNT(*) FILTER (WHERE description_full IS NULL
                           AND description_short IS NULL)
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_sin_ningun_texto
FROM jobs
WHERE is_active = TRUE
GROUP BY country_code
ORDER BY pct_desc_full ASC;


-- =============================================================================
-- SECCIÓN 2 — COBERTURA Y CALIDAD DE SKILLS
-- =============================================================================

-- Q02 — Cobertura de skills por país
-- Calcula qué proporción de ofertas en cada país tiene al menos una skill extraída.
SELECT
    j.country_code,
    COUNT(DISTINCT j.id)                                                AS total_jobs_activos,
    COUNT(DISTINCT js.job_id)                                           AS jobs_con_skills,
    COUNT(DISTINCT j.id) - COUNT(DISTINCT js.job_id)                   AS jobs_sin_skills,
    ROUND(
        COUNT(DISTINCT js.job_id)
        * 100.0 / NULLIF(COUNT(DISTINCT j.id), 0), 1
    )                                                                   AS pct_con_skills,
    ROUND(
        COUNT(js.skill_id)::numeric
        / NULLIF(COUNT(DISTINCT js.job_id), 0), 2
    )                                                                   AS avg_skills_por_oferta_con_skills,
    ROUND(
        COUNT(js.skill_id)::numeric
        / NULLIF(COUNT(DISTINCT j.id), 0), 2
    )                                                                   AS avg_skills_sobre_total
FROM jobs j
LEFT JOIN job_skills js ON js.job_id = j.id
WHERE j.is_active = TRUE
GROUP BY j.country_code
ORDER BY pct_con_skills ASC;


-- Q06 — Número de jobs sin skills (desglose)
-- Cuantifica el tamaño exacto del universo sin skills extraídas.
SELECT
    'con al menos 1 skill'                                             AS segmento,
    COUNT(DISTINCT j.id)                                               AS num_jobs
FROM jobs j
INNER JOIN job_skills js ON js.job_id = j.id
WHERE j.is_active = TRUE

UNION ALL

SELECT
    'sin ninguna skill',
    COUNT(*)
FROM jobs j
LEFT JOIN job_skills js ON js.job_id = j.id
WHERE j.is_active = TRUE
  AND js.job_id IS NULL

UNION ALL

SELECT
    'total activos',
    COUNT(*)
FROM jobs
WHERE is_active = TRUE;


-- Q07 — Skills sin uso (en catálogo pero sin job_skill)
-- Identifica skills que existen en el catálogo pero nunca han sido relacionadas con una oferta.
SELECT
    s.id,
    s.name,
    s.category,
    COALESCE(usage.total, 0)                                           AS veces_usada
FROM skills s
LEFT JOIN (
    SELECT skill_id, COUNT(*) AS total
    FROM job_skills
    GROUP BY skill_id
) usage ON usage.skill_id = s.id
WHERE COALESCE(usage.total, 0) = 0
ORDER BY s.name;


-- Q08a — Skills duplicadas semánticamente (variantes conocidas)
-- Busca variantes case-insensitive de skills de alto riesgo de duplicación.
SELECT
    s.name,
    s.category,
    COUNT(js.job_id)                                                    AS num_jobs
FROM skills s
LEFT JOIN job_skills js ON js.skill_id = s.id
WHERE
    s.name ILIKE 'react%'
    OR s.name ILIKE 'github%'
    OR s.name ILIKE 'python%'
    OR (s.name ILIKE 'java%' AND s.name NOT ILIKE 'javascript%')
    OR s.name ILIKE 'docker%'
    OR s.name ILIKE 'kubernetes%' OR s.name ILIKE 'k8s%'
    OR s.name ILIKE 'sql%'
    OR s.name ILIKE 'typescript%'
GROUP BY s.name, s.category
ORDER BY s.name;


-- Q08b — Detección amplia de skills duplicadas (primer token)
-- Identifica cualquier skill cuyo primer token aparece en múltiples nombres distintos.
SELECT
    LOWER(SPLIT_PART(s.name, ' ', 1))                                  AS primer_token,
    COUNT(DISTINCT s.name)                                             AS variantes,
    STRING_AGG(s.name, ' | ' ORDER BY s.name)                         AS nombres
FROM skills s
GROUP BY LOWER(SPLIT_PART(s.name, ' ', 1))
HAVING COUNT(DISTINCT s.name) > 1
ORDER BY variantes DESC, primer_token;


-- =============================================================================
-- SECCIÓN 3 — CLASIFICACIÓN DE ROLES (role_category)
-- =============================================================================

-- Q03 — Porcentaje real de role_category = 'other'
-- Desagrega el universo activo en categorizado, 'other', y NULL.
SELECT
    COUNT(*)                                                            AS total_activas,
    COUNT(*) FILTER (WHERE role_category = 'other')                    AS otras,
    COUNT(*) FILTER (WHERE role_category IS NULL)                      AS sin_clasificar,
    COUNT(*) FILTER (WHERE role_category NOT IN ('other')
                      AND role_category IS NOT NULL)                   AS clasificadas,
    ROUND(
        COUNT(*) FILTER (WHERE role_category = 'other')
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_other,
    ROUND(
        COUNT(*) FILTER (WHERE role_category IS NULL)
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_null,
    ROUND(
        COUNT(*) FILTER (WHERE role_category NOT IN ('other')
                          AND role_category IS NOT NULL)
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_clasificadas
FROM jobs
WHERE is_active = TRUE;


-- Q04 — Distribución completa de role_category
-- Ranking de todas las categorías por número de ofertas.
SELECT
    COALESCE(role_category, '(NULL — no-IT / pendiente Ollama)')       AS categoria,
    COUNT(*)                                                            AS ofertas,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1)                 AS pct_del_total
FROM jobs
WHERE is_active = TRUE
GROUP BY role_category
ORDER BY ofertas DESC;


-- Q14 — Distribución de role_category por país
-- Muestra qué proporción de cada país cae en cada categoría.
SELECT
    j.country_code,
    COALESCE(j.role_category, '(NULL)')                                AS categoria,
    COUNT(*)                                                            AS ofertas,
    ROUND(
        COUNT(*) * 100.0
        / SUM(COUNT(*)) OVER (PARTITION BY j.country_code), 1
    )                                                                   AS pct_dentro_pais
FROM jobs j
WHERE j.is_active = TRUE
GROUP BY j.country_code, j.role_category
ORDER BY j.country_code, ofertas DESC;


-- =============================================================================
-- SECCIÓN 4 — DATOS SALARIALES
-- =============================================================================

-- Q05 — Cobertura real de salary_min, salary_max y salary_mid
-- Desglose de disponibilidad de datos salariales por país y tipo (declarado vs predicho).
SELECT
    country_code,
    COUNT(*)                                                            AS total_activas,
    COUNT(*) FILTER (WHERE salary_mid IS NOT NULL)                     AS con_salary_mid,
    ROUND(
        COUNT(*) FILTER (WHERE salary_mid IS NOT NULL)
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_salary_mid,
    COUNT(*) FILTER (WHERE salary_mid IS NOT NULL
                      AND salary_is_predicted = FALSE)                 AS salario_declarado,
    COUNT(*) FILTER (WHERE salary_mid IS NOT NULL
                      AND salary_is_predicted = TRUE)                  AS salario_predicho,
    COUNT(*) FILTER (WHERE salary_min IS NULL
                      AND salary_max IS NULL)                          AS sin_ningun_salario
FROM jobs
WHERE is_active = TRUE
GROUP BY country_code
ORDER BY pct_salary_mid ASC;


-- Q09 — Mediana salarial real de Polonia
-- Calcula la mediana de Polonia separando salarios declarados de predichos.
SELECT
    COUNT(*) FILTER (WHERE salary_mid IS NOT NULL
                      AND salary_is_predicted = FALSE)                 AS n_con_salario_declarado,
    COUNT(*) FILTER (WHERE salary_mid IS NOT NULL
                      AND salary_is_predicted = TRUE)                  AS n_con_salario_predicho,
    ROUND(
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY salary_mid
        ) FILTER (WHERE salary_is_predicted = FALSE)::numeric
    )                                                                   AS mediana_declarada_eur,
    ROUND(AVG(salary_mid)
        FILTER (WHERE salary_is_predicted = FALSE))                    AS media_declarada_eur,
    ROUND(
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY salary_mid
        ) FILTER (WHERE salary_mid IS NOT NULL)::numeric
    )                                                                   AS mediana_total_eur
FROM jobs
WHERE country_code  = 'pl'
  AND is_active     = TRUE
  AND salary_mid    IS NOT NULL;


-- Q10 — Distribución salarial completa de Polonia (percentiles)
-- Percentiles P0, P25, P50, P75, P100 de los salarios polacos.
SELECT
    country_code,
    COUNT(*) FILTER (WHERE salary_is_predicted = FALSE)                AS n_declarados,
    ROUND(MIN(salary_mid)
        FILTER (WHERE salary_is_predicted = FALSE))                    AS minimo,
    ROUND(
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY salary_mid)
        FILTER (WHERE salary_is_predicted = FALSE)::numeric
    )                                                                   AS p25,
    ROUND(
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary_mid)
        FILTER (WHERE salary_is_predicted = FALSE)::numeric
    )                                                                   AS mediana_p50,
    ROUND(
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY salary_mid)
        FILTER (WHERE salary_is_predicted = FALSE)::numeric
    )                                                                   AS p75,
    ROUND(MAX(salary_mid)
        FILTER (WHERE salary_is_predicted = FALSE))                    AS maximo
FROM jobs
WHERE country_code = 'pl'
  AND is_active    = TRUE
  AND salary_mid   IS NOT NULL
GROUP BY country_code;


-- Q15 — Registros salariales anómalos
-- Identifica ofertas con valores salariales fuera de rangos plausibles.
SELECT
    id,
    title,
    company,
    country_code,
    salary_min,
    salary_max,
    salary_mid,
    salary_is_predicted,
    posted_at::DATE                                                     AS fecha,
    CASE
        WHEN salary_min > salary_max
            THEN 'min > max'
        WHEN salary_min < 0 OR salary_max < 0
            THEN 'valor negativo'
        WHEN salary_mid > 400000 AND salary_is_predicted = FALSE
            THEN 'outlier alto declarado (> 400k EUR)'
        WHEN salary_mid > 400000 AND salary_is_predicted = TRUE
            THEN 'outlier alto predicho (> 400k EUR)'
        WHEN salary_mid > 0 AND salary_mid < 2000
            THEN 'outlier bajo (< 2k EUR — posible tarifa diaria sin anualizar)'
        WHEN country_code = 'pl' AND salary_mid > 200000
            AND salary_is_predicted = FALSE
            THEN 'PL outlier alto declarado (posible PLN residual)'
    END                                                                 AS tipo_anomalia
FROM jobs
WHERE is_active = TRUE
  AND (
       salary_min > salary_max
    OR salary_min < 0
    OR salary_max < 0
    OR (salary_mid > 400000)
    OR (salary_mid > 0 AND salary_mid < 2000)
    OR (country_code = 'pl' AND salary_mid > 200000
        AND salary_is_predicted = FALSE)
  )
ORDER BY country_code, salary_mid DESC NULLS LAST;


-- =============================================================================
-- SECCIÓN 5 — AGREGADOS GEOGRÁFICOS Y ANÁLISIS CRUZADOS
-- =============================================================================

-- Q11 — Top países por porcentaje de description_full
-- Ranking de cobertura de description_full con métrica de calidad (longitud de texto).
SELECT
    country_code,
    COUNT(*)                                                            AS total_activas,
    COUNT(*) FILTER (WHERE description_full IS NOT NULL)               AS con_desc_full,
    ROUND(
        COUNT(*) FILTER (WHERE description_full IS NOT NULL)
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_desc_full,
    ROUND(AVG(LENGTH(description_full))
        FILTER (WHERE description_full IS NOT NULL))                   AS chars_medio_desc_full,
    ROUND(AVG(LENGTH(description_short))
        FILTER (WHERE description_short IS NOT NULL))                  AS chars_medio_desc_short
FROM jobs
WHERE is_active = TRUE
GROUP BY country_code
ORDER BY pct_desc_full DESC;


-- Q12 — Top países por porcentaje de skills
-- Cobertura de skills por país en orden descendente.
SELECT
    j.country_code,
    COUNT(DISTINCT j.id)                                                AS total_jobs,
    COUNT(DISTINCT js.job_id)                                           AS jobs_con_skills,
    COUNT(js.skill_id)                                                  AS total_skill_rows,
    ROUND(
        COUNT(DISTINCT js.job_id)
        * 100.0 / NULLIF(COUNT(DISTINCT j.id), 0), 1
    )                                                                   AS pct_jobs_con_skills,
    ROUND(
        COUNT(js.skill_id)::numeric
        / NULLIF(COUNT(DISTINCT js.job_id), 0), 1
    )                                                                   AS avg_skills_si_tiene
FROM jobs j
LEFT JOIN job_skills js ON js.job_id = j.id
WHERE j.is_active = TRUE
GROUP BY j.country_code
ORDER BY pct_jobs_con_skills DESC;


-- Q13 — Ofertas activas por país
-- Volumen de ofertas activas con desagregación temporal y datos de oferta reciente.
SELECT
    j.country_code,
    c.name                                                              AS pais,
    COUNT(*)                                                            AS activas,
    COUNT(*) FILTER (WHERE j.posted_at >= NOW() - INTERVAL '7 days')   AS ultimos_7_dias,
    COUNT(*) FILTER (WHERE j.posted_at >= NOW() - INTERVAL '30 days')  AS ultimos_30_dias,
    COUNT(*) FILTER (WHERE j.posted_at >= NOW() - INTERVAL '90 days')  AS ultimos_90_dias,
    COUNT(*) FILTER (WHERE j.posted_at IS NULL)                        AS sin_fecha,
    ROUND(
        COUNT(*) FILTER (WHERE j.posted_at IS NULL)
        * 100.0 / NULLIF(COUNT(*), 0), 1
    )                                                                   AS pct_sin_fecha
FROM jobs j
JOIN countries c ON c.code = j.country_code
WHERE j.is_active = TRUE
GROUP BY j.country_code, c.name
ORDER BY activas DESC;


-- =============================================================================
-- FIN DEL PAQUETE DE AUDITORÍA
-- =============================================================================
