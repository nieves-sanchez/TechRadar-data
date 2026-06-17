-- =============================================================================
-- weekly_load_diagnostics.sql — TechRadar
--
-- Queries de verificación post-carga semanal.
--
-- CUÁNDO EJECUTAR:
--   Después de correr retro_classify.py (el paso de enriquecimiento con IA).
--   Flujo semanal completo:
--     1. Lunes (automático)  → GitHub Actions ejecuta el pipeline ETL
--     2. Lunes/martes        → Ejecutar retro_classify.py en local
--     3. Después del retro   → Ejecutar estas queries en Supabase SQL Editor
--
-- BLOQUES:
--   0 — Resumen ejecutivo (una sola query, primera lectura)
--   1 — Diagnóstico de role_category
--   2 — Diagnóstico de skills
--   3 — Verificación del enriquecimiento Ollama
--   4 — Diagnóstico de salarios (con alerta Polonia)
--   5 — Chequeos de integridad general
-- =============================================================================


-- =============================================================================
-- BLOQUE 0 — RESUMEN EJECUTIVO
-- Una sola query para ver de un vistazo si algo fue mal.
-- Verde: pct_other < 15%, avg_skills >= 3, pl_median_salary < 50000.
-- =============================================================================

SELECT
    -- Volumen
    COUNT(*)                                                        AS total_jobs,
    COUNT(*) FILTER (WHERE ingested_at >= NOW() - INTERVAL '8 days')
                                                                    AS nuevas_esta_semana,

    -- role_category
    ROUND(COUNT(*) FILTER (WHERE role_category = 'other')  * 100.0 / COUNT(*), 1)
                                                                    AS pct_other,
    ROUND(COUNT(*) FILTER (WHERE role_category IS NULL)    * 100.0 / COUNT(*), 1)
                                                                    AS pct_null_role,

    -- Skills (ratio global)
    ROUND(
        (SELECT COUNT(*) FROM job_skills)::numeric / NULLIF(COUNT(*), 0)
    , 1)                                                            AS avg_skills_global,

    -- Salario Polonia (alerta si mediana >> 50.000 → datos aún en PLN)
    ROUND(
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary_mid) FILTER (
            WHERE country_code = 'pl'
              AND salary_mid IS NOT NULL
              AND salary_is_predicted = FALSE
        )::numeric
    )                                                               AS pl_median_salary

FROM jobs
WHERE is_active = TRUE;


-- =============================================================================
-- BLOQUE 1 — DIAGNÓSTICO DE role_category
-- KPI objetivo: 'other' < 15%, NULL < 5% (NULL = marcados por Ollama para revisión).
-- =============================================================================

-- 1a. Distribución global de categorías
SELECT
    COALESCE(role_category, 'NULL')                                 AS role_category,
    COUNT(*)                                                        AS ofertas,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1)             AS pct
FROM jobs
WHERE is_active = TRUE
GROUP BY role_category
ORDER BY ofertas DESC;


-- 1b. Distribución de 'other' y NULL por país
SELECT
    country_code,
    COUNT(*) FILTER (WHERE role_category = 'other')                 AS other_count,
    COUNT(*) FILTER (WHERE role_category IS NULL)                   AS null_count,
    COUNT(*)                                                        AS total,
    ROUND(COUNT(*) FILTER (WHERE role_category IN ('other') OR role_category IS NULL)
          * 100.0 / NULLIF(COUNT(*), 0), 1)                        AS pct_sin_clasificar
FROM jobs
WHERE is_active = TRUE
GROUP BY country_code
ORDER BY pct_sin_clasificar DESC;


-- 1c. Títulos más frecuentes clasificados como 'other' (activos)
-- Si un título aparece mucho en 'other', es candidato a añadir en ROLE_KEYWORDS
-- dentro de skills_catalog.py para mejorar la detección regex en futuros pipelines.
SELECT
    title,
    country_code,
    COUNT(*) AS ocurrencias
FROM jobs
WHERE role_category = 'other'
  AND is_active = TRUE
GROUP BY title, country_code
HAVING COUNT(*) > 1
ORDER BY ocurrencias DESC
LIMIT 50;


-- 1d. Evolución semanal del % de 'other' (para ver tendencia de mejora)
SELECT
    DATE_TRUNC('week', ingested_at)::DATE                           AS semana,
    COUNT(*)                                                        AS total,
    COUNT(*) FILTER (WHERE role_category = 'other')                 AS other_count,
    ROUND(COUNT(*) FILTER (WHERE role_category = 'other')
          * 100.0 / NULLIF(COUNT(*), 0), 1)                        AS pct_other
FROM jobs
GROUP BY DATE_TRUNC('week', ingested_at)
ORDER BY semana DESC
LIMIT 8;


-- =============================================================================
-- BLOQUE 2 — DIAGNÓSTICO DE SKILLS
-- KPI objetivo: avg_skills >= 3 tras el enriquecimiento con Ollama.
-- =============================================================================

-- 2a. Estadísticas de skills: nueva carga vs histórico
SELECT
    'nueva_carga'                                                   AS periodo,
    COUNT(DISTINCT js.job_id)                                       AS jobs_con_skills,
    COUNT(*)                                                        AS total_skill_rows,
    ROUND(COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT js.job_id), 0), 1)
                                                                    AS avg_skills_por_oferta,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY cnt)::INT           AS median_skills
FROM job_skills js
JOIN jobs j ON j.id = js.job_id
JOIN (SELECT job_id, COUNT(*) AS cnt FROM job_skills GROUP BY job_id) counts
    ON counts.job_id = js.job_id
WHERE j.ingested_at >= NOW() - INTERVAL '8 days'

UNION ALL

SELECT
    'historico',
    COUNT(DISTINCT js.job_id),
    COUNT(*),
    ROUND(COUNT(*) * 1.0 / NULLIF(COUNT(DISTINCT js.job_id), 0), 1),
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY cnt)::INT
FROM job_skills js
JOIN jobs j ON j.id = js.job_id
JOIN (SELECT job_id, COUNT(*) AS cnt FROM job_skills GROUP BY job_id) counts
    ON counts.job_id = js.job_id;


-- 2b. Distribución de skills por oferta (0, 1, 2, 3-5, 6-10, 11+)
SELECT
    CASE
        WHEN skill_count = 0             THEN '0'
        WHEN skill_count = 1             THEN '1'
        WHEN skill_count = 2             THEN '2'
        WHEN skill_count BETWEEN 3 AND 5 THEN '3-5'
        WHEN skill_count BETWEEN 6 AND 10 THEN '6-10'
        ELSE '11+'
    END                                                             AS rango_skills,
    COUNT(*)                                                        AS num_ofertas,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1)             AS pct
FROM (
    SELECT j.id, COALESCE(COUNT(js.skill_id), 0) AS skill_count
    FROM jobs j
    LEFT JOIN job_skills js ON js.job_id = j.id
    WHERE j.is_active = TRUE
    GROUP BY j.id
) counts
GROUP BY rango_skills
ORDER BY MIN(skill_count);


-- 2c. Top 30 skills en la última carga
SELECT
    s.name                                                          AS skill,
    s.category,
    COUNT(*)                                                        AS ocurrencias
FROM job_skills js
JOIN skills s ON s.id = js.skill_id
JOIN jobs j   ON j.id = js.job_id
WHERE j.ingested_at >= NOW() - INTERVAL '8 days'
GROUP BY s.name, s.category
ORDER BY ocurrencias DESC
LIMIT 30;


-- 2d. Cobertura de description_full (necesaria para buena extracción de skills)
-- Si cae por debajo del 60%, el crawling tuvo problemas esa semana.
SELECT
    country_code,
    COUNT(*)                                                        AS total,
    COUNT(*) FILTER (WHERE description_full IS NOT NULL)            AS con_desc_full,
    ROUND(COUNT(*) FILTER (WHERE description_full IS NOT NULL)
          * 100.0 / NULLIF(COUNT(*), 0), 1)                        AS pct_con_desc_full
FROM jobs
WHERE is_active = TRUE
  AND ingested_at >= NOW() - INTERVAL '8 days'
GROUP BY country_code
ORDER BY pct_con_desc_full ASC;


-- =============================================================================
-- BLOQUE 3 — VERIFICACIÓN DEL ENRIQUECIMIENTO OLLAMA (retro_classify.py)
-- Ejecutar después de correr retro_classify.py, no antes.
-- =============================================================================

-- 3a. Ofertas con role_category = NULL: candidatas a revisión manual
-- Ollama las marcó como posiblemente no-IT (is_tech=false).
-- Revisa una muestra antes de decidir si borrarlas.
SELECT
    id,
    title,
    company,
    country_code,
    posted_at::DATE AS fecha
FROM jobs
WHERE role_category IS NULL
  AND is_active = TRUE
ORDER BY posted_at DESC
LIMIT 50;


-- 3b. Resumen de NULLs por país
-- Si hay muchos NULLs en un país concreto → puede ser un idioma que Ollama
-- maneja peor (polaco, italiano) o un problema en esa extracción.
SELECT
    country_code,
    COUNT(*) FILTER (WHERE role_category IS NULL)                   AS null_count,
    COUNT(*)                                                        AS total,
    ROUND(COUNT(*) FILTER (WHERE role_category IS NULL)
          * 100.0 / NULLIF(COUNT(*), 0), 1)                        AS pct_null
FROM jobs
WHERE is_active = TRUE
GROUP BY country_code
ORDER BY pct_null DESC;


-- 3c. Skills añadidas por Ollama (categoría 'tool' sin categorizar — así las crea retro_classify)
-- Sirve para ver si Ollama está añadiendo términos razonables o ruido.
SELECT
    s.name,
    COUNT(js.job_id)                                                AS en_n_ofertas
FROM skills s
JOIN job_skills js ON js.skill_id = s.id
WHERE s.category = 'tool'
  AND LENGTH(s.name) > 3
ORDER BY en_n_ofertas DESC
LIMIT 40;


-- =============================================================================
-- BLOQUE 4 — DIAGNÓSTICO DE SALARIOS
-- Polonia (pl) usa PLN, no EUR. Fix pendiente en transform.py.
-- Mientras no esté aplicado el fix, los salarios de pl están en PLN:
--   mediana PL correcta en EUR ≈ 18.000-25.000 EUR/año
--   mediana PL en PLN sin convertir ≈ 79.000+
-- Tras el fix (÷ 4,25), PL debe ser el país con mediana más baja.
-- =============================================================================

-- 4a. Comparativa de salarios por país
SELECT
    country_code,
    COUNT(*)                                                        AS ofertas_con_salario,
    ROUND(MIN(salary_mid))                                          AS min,
    ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary_mid)::numeric)
                                                                    AS mediana,
    ROUND(MAX(salary_mid))                                          AS max
FROM jobs
WHERE is_active          = TRUE
  AND salary_mid         IS NOT NULL
  AND salary_is_predicted = FALSE
GROUP BY country_code
ORDER BY mediana DESC;


-- 4b. Ofertas con salarios anómalos (outliers o datos erróneos)
-- Tras el fix de PL, no deberían quedar valores > 500.000 EUR.
SELECT
    id, title, country_code,
    salary_min, salary_max, salary_mid,
    CASE
        WHEN salary_min > salary_max  THEN 'min > max'
        WHEN salary_min < 0           THEN 'negativo'
        WHEN salary_mid > 500000      THEN 'outlier alto'
        WHEN salary_mid < 1000 AND salary_mid > 0 THEN 'outlier bajo'
    END                                                             AS anomalia
FROM jobs
WHERE is_active = TRUE
  AND (
       salary_min > salary_max
    OR salary_min < 0
    OR salary_mid > 500000
    OR (salary_mid > 0 AND salary_mid < 1000)
  );


-- =============================================================================
-- BLOQUE 5 — INTEGRIDAD GENERAL
-- =============================================================================

-- 5a. Evolución semanal de ingesta por país
SELECT
    DATE_TRUNC('week', ingested_at)::DATE                           AS semana,
    country_code,
    COUNT(*)                                                        AS nuevas_ofertas
FROM jobs
GROUP BY DATE_TRUNC('week', ingested_at), country_code
ORDER BY semana DESC, country_code;


-- 5b. Ofertas sin posted_at (afecta a las vistas de tendencias)
SELECT
    country_code,
    COUNT(*) FILTER (WHERE posted_at IS NULL)                       AS sin_fecha,
    COUNT(*)                                                        AS total,
    ROUND(COUNT(*) FILTER (WHERE posted_at IS NULL)
          * 100.0 / NULLIF(COUNT(*), 0), 1)                        AS pct_sin_fecha
FROM jobs
WHERE is_active = TRUE
  AND ingested_at >= NOW() - INTERVAL '8 days'
GROUP BY country_code
ORDER BY pct_sin_fecha DESC;


-- 5c. Conteo de skills en catálogo y vínculos totales
SELECT
    (SELECT COUNT(*) FROM skills)                                   AS total_skills_catalogo,
    (SELECT COUNT(*) FROM job_skills)                               AS total_vinculos,
    (SELECT COUNT(*) FROM jobs WHERE is_active = TRUE)              AS jobs_activos,
    (SELECT COUNT(DISTINCT skill_id) FROM job_skills
     JOIN jobs ON jobs.id = job_skills.job_id
     WHERE jobs.is_active = TRUE)                                   AS skills_en_uso;
