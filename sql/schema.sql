-- =============================================================================
-- TechRadar — Schema PostgreSQL v3
-- Fuentes: Adzuna API (IT jobs, 8 paises EU) + Eurostat (contexto macro)
-- Cobertura: DE, FR, ES, NL, PL, IT, AT, BE
-- UK excluido: no pertenece a la UE y Eurostat no publica datos de UK post-Brexit.
-- Todos los salarios en EUR (moneda unica, sin conversion necesaria).
-- Cumple 3FN.
-- =============================================================================

-- =============================================================================
-- TABLA: countries
-- Catalogo de los 8 paises EU cubiertos.
-- Todos usan EUR como moneda.
-- =============================================================================
CREATE TABLE IF NOT EXISTS countries (
    code            VARCHAR(2)   NOT NULL,
    name            VARCHAR(100) NOT NULL,
    currency        VARCHAR(3)   NOT NULL DEFAULT 'EUR',
    adzuna_endpoint VARCHAR(2)   NOT NULL,

    CONSTRAINT pk_countries PRIMARY KEY (code),
    CONSTRAINT chk_countries_currency
        CHECK (currency = 'EUR'),
    CONSTRAINT chk_countries_code_len
        CHECK (LENGTH(code) = 2)
);

INSERT INTO countries (code, name, currency, adzuna_endpoint) VALUES
    ('de', 'Germany',        'EUR', 'de'),
    ('fr', 'France',         'EUR', 'fr'),
    ('es', 'Spain',          'EUR', 'es'),
    ('nl', 'Netherlands',    'EUR', 'nl'),
    ('pl', 'Poland',         'EUR', 'pl'),
    ('it', 'Italy',          'EUR', 'it'),
    ('at', 'Austria',        'EUR', 'at'),
    ('be', 'Belgium',        'EUR', 'be')
ON CONFLICT (code) DO NOTHING;

-- =============================================================================
-- TABLA: jobs
-- Una fila por oferta de empleo. Fuente: Adzuna API + crawling redirect_url.
-- Cumple 3FN. Todos los salarios estan en EUR (moneda unica de los 8 paises).
-- No se necesita tabla de conversion de moneda: todos los paises usan EUR.
-- =============================================================================
CREATE TABLE IF NOT EXISTS jobs (

    -- -------------------------------------------------------------------------
    -- Identificacion
    -- -------------------------------------------------------------------------
    id              BIGINT       NOT NULL,           -- ID unico de Adzuna
    source          VARCHAR(20)  NOT NULL DEFAULT 'adzuna',

    -- -------------------------------------------------------------------------
    -- Datos del puesto
    -- -------------------------------------------------------------------------
    title           VARCHAR(255) NOT NULL,
    company         VARCHAR(255),                   -- company.display_name
    location_display VARCHAR(255),                  -- location.display_name
    city            VARCHAR(100),                   -- ciudad parseada del display
    country_code    VARCHAR(2)   NOT NULL,          -- FK a countries.code
    role_category   VARCHAR(50),                    -- clasificacion del rol (NLP sobre title)

    -- -------------------------------------------------------------------------
    -- Salario en EUR (moneda unica de todos los paises cubiertos)
    -- salary_mid = (salary_min + salary_max) / 2, calculado en transform.py.
    -- Es el campo de salario de referencia para el dashboard.
    -- En los agregados del dashboard se usa la mediana de salary_mid entre
    -- varias ofertas, no la media, por ser mas robusta frente a outliers.
    -- -------------------------------------------------------------------------
    salary_min          INTEGER,
    salary_max          INTEGER,
    salary_mid          INTEGER,                    -- punto medio del rango salarial
    salary_is_predicted BOOLEAN  NOT NULL DEFAULT FALSE,

    -- -------------------------------------------------------------------------
    -- Condiciones del puesto
    -- -------------------------------------------------------------------------
    contract_type   VARCHAR(50),                    -- 'permanent', 'contract'
    contract_time   VARCHAR(50),                    -- 'full_time', 'part_time'
    remote          BOOLEAN,                        -- derivado en transform.py (no campo de API)

    -- -------------------------------------------------------------------------
    -- Descripcion (dos versiones)
    -- -------------------------------------------------------------------------
    description_short   TEXT,   -- 500 chars truncados de la API
    description_full    TEXT,   -- texto completo via crawling de redirect_url

    -- -------------------------------------------------------------------------
    -- URL
    -- -------------------------------------------------------------------------
    url             TEXT,                           -- redirect_url de Adzuna

    -- -------------------------------------------------------------------------
    -- Control de ciclo de vida (gestion incremental)
    -- -------------------------------------------------------------------------
    is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    posted_at       TIMESTAMPTZ,                         -- campo "created" de Adzuna
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- -------------------------------------------------------------------------
    -- Constraints
    -- -------------------------------------------------------------------------
    CONSTRAINT pk_jobs
        PRIMARY KEY (id),
    CONSTRAINT fk_jobs_country
        FOREIGN KEY (country_code) REFERENCES countries(code),
    CONSTRAINT chk_jobs_salary_range
        CHECK (salary_max IS NULL OR salary_min IS NULL OR salary_max >= salary_min),
    CONSTRAINT chk_jobs_salary_positive
        CHECK (salary_min IS NULL OR salary_min >= 0),
    CONSTRAINT chk_jobs_contract_type
        CHECK (contract_type IS NULL OR contract_type IN ('permanent', 'contract')),
    CONSTRAINT chk_jobs_contract_time
        CHECK (contract_time IS NULL OR contract_time IN ('full_time', 'part_time')),
    CONSTRAINT chk_jobs_source
        CHECK (source IN ('adzuna')),
    CONSTRAINT chk_jobs_role_category
        CHECK (role_category IS NULL OR role_category IN (
            'data_engineering', 'data_science', 'data_analyst',
            'backend', 'frontend', 'fullstack',
            'devops', 'cloud', 'security',
            'mobile', 'ai_ml', 'other'
        ))
);

-- =============================================================================
-- TABLA: skills
-- Catalogo normalizado de tecnologias y habilidades.
-- Se puebla progresivamente a medida que el NLP detecta nuevos terminos.
-- =============================================================================
CREATE TABLE IF NOT EXISTS skills (
    id          SERIAL       NOT NULL,
    name        VARCHAR(100) NOT NULL,
    category    VARCHAR(50),

    CONSTRAINT pk_skills PRIMARY KEY (id),
    CONSTRAINT uq_skills_name UNIQUE (name),
    CONSTRAINT chk_skills_category CHECK (
        category IS NULL OR category IN (
            'language', 'framework', 'cloud',
            'database', 'tool', 'methodology', 'soft'
        )
    ),
    CONSTRAINT chk_skills_name_not_empty
        CHECK (LENGTH(TRIM(name)) > 0)
);

-- =============================================================================
-- TABLA: job_skills
-- Relacion M:N entre ofertas y skills extraidas por NLP.
-- Una fila por cada skill detectada en cada oferta.
-- =============================================================================
CREATE TABLE IF NOT EXISTS job_skills (
    job_id      BIGINT  NOT NULL,
    skill_id    INTEGER NOT NULL,

    CONSTRAINT pk_job_skills
        PRIMARY KEY (job_id, skill_id),
    CONSTRAINT fk_job_skills_job
        FOREIGN KEY (job_id)   REFERENCES jobs(id)   ON DELETE CASCADE,
    CONSTRAINT fk_job_skills_skill
        FOREIGN KEY (skill_id) REFERENCES skills(id) ON DELETE CASCADE
);

-- =============================================================================
-- TABLA: labor_market_context
-- Datos macro de Eurostat. Una fila por pais + año + indicador.
-- Fuente: dataset lfsi_emp_a, licencia CC BY 4.0.
-- Cubre los 8 paises EU del proyecto (no incluye UK post-Brexit).
-- =============================================================================
CREATE TABLE IF NOT EXISTS labor_market_context (
    id              SERIAL       NOT NULL,
    country_code    VARCHAR(2)   NOT NULL,
    year            SMALLINT     NOT NULL,
    indicator       VARCHAR(100) NOT NULL,
    value           NUMERIC(5,2) NOT NULL,

    CONSTRAINT pk_labor_market_context
        PRIMARY KEY (id),
    CONSTRAINT fk_lmc_country
        FOREIGN KEY (country_code) REFERENCES countries(code),
    CONSTRAINT uq_lmc_entry
        UNIQUE (country_code, year, indicator),
    CONSTRAINT chk_lmc_year
        CHECK (year BETWEEN 2000 AND 2100),
    CONSTRAINT chk_lmc_value
        CHECK (value BETWEEN 0 AND 100),
    CONSTRAINT chk_lmc_indicator_not_empty
        CHECK (LENGTH(TRIM(indicator)) > 0)
);

-- =============================================================================
-- INDICES
-- =============================================================================

-- jobs: filtros geograficos frecuentes
CREATE INDEX IF NOT EXISTS idx_jobs_country
    ON jobs (country_code);

-- jobs: ordenacion por fecha de publicacion
CREATE INDEX IF NOT EXISTS idx_jobs_posted_at
    ON jobs (posted_at DESC NULLS LAST);

-- jobs: solo ofertas activas (mayoria de queries del dashboard)
CREATE INDEX IF NOT EXISTS idx_jobs_active
    ON jobs (is_active)
    WHERE is_active = TRUE;

-- jobs: KPI de salarios sobre salary_mid (campo de referencia del dashboard)
CREATE INDEX IF NOT EXISTS idx_jobs_salary_mid
    ON jobs (country_code, salary_mid)
    WHERE salary_mid IS NOT NULL
      AND salary_is_predicted = FALSE;

-- jobs: KPI remote %
CREATE INDEX IF NOT EXISTS idx_jobs_remote
    ON jobs (remote, country_code)
    WHERE remote IS NOT NULL;

-- jobs: tipo de contrato
CREATE INDEX IF NOT EXISTS idx_jobs_contract_type
    ON jobs (contract_type, country_code)
    WHERE contract_type IS NOT NULL;

-- jobs: ingesta incremental
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen
    ON jobs (first_seen_at DESC);

-- jobs: filtro por categoria de rol
CREATE INDEX IF NOT EXISTS idx_jobs_role_category
    ON jobs (role_category, country_code)
    WHERE role_category IS NOT NULL;

-- job_skills: conteo de skills (query mas frecuente del dashboard)
CREATE INDEX IF NOT EXISTS idx_job_skills_skill
    ON job_skills (skill_id);

-- job_skills: habilidades de un job concreto
CREATE INDEX IF NOT EXISTS idx_job_skills_job
    ON job_skills (job_id);

-- labor_market_context: JOIN con jobs por pais y año
CREATE INDEX IF NOT EXISTS idx_lmc_country_year
    ON labor_market_context (country_code, year);

-- =============================================================================
-- VISTAS PARA EL DASHBOARD (10 vistas)
-- Todos los salarios ya estan en EUR. No se necesita conversion en las queries.
-- v_top_skills_by_country   → top skills por pais y rol (90 dias)
-- v_top_skills_global       → top skills agregado EU-8 (para KPI card global)
-- v_offers_by_country       → total ofertas por pais (para mapa EU)
-- v_salary_stats_by_country → estadisticas de salario por pais
-- v_remote_pct_by_country   → % ofertas remote por pais
-- v_job_trends_monthly      → evolucion mensual de ofertas por pais
-- v_salary_by_role_country  → salario medio por rol y pais
-- v_demand_by_role_monthly  → evolucion mensual de ofertas por rol
-- v_skill_cooccurrence      → skills que suelen pedirse juntas
-- v_skills_with_market_context → skills con contexto Eurostat
-- =============================================================================

-- Vista: top skills por pais y rol (ultimos 90 dias, solo ofertas activas)
CREATE OR REPLACE VIEW v_top_skills_by_country AS
SELECT
    j.country_code,
    c.name                  AS country_name,
    j.role_category,
    s.name                  AS skill,
    s.category              AS skill_category,
    COUNT(*)                AS job_count,
    ROUND(
        COUNT(*) * 100.0 /
        SUM(COUNT(*)) OVER (PARTITION BY j.country_code, j.role_category),
    2)                      AS pct_of_segment_jobs
FROM job_skills js
JOIN jobs      j ON j.id   = js.job_id
JOIN skills    s ON s.id   = js.skill_id
JOIN countries c ON c.code = j.country_code
WHERE j.is_active = TRUE
  AND j.posted_at >= NOW() - INTERVAL '90 days'
GROUP BY j.country_code, c.name, j.role_category, s.name, s.category
ORDER BY j.country_code, j.role_category, job_count DESC;

-- Vista: estadisticas de salario por pais en EUR
-- Usa salary_mid (calculado en transform.py) como campo de referencia.
-- La mediana es mas robusta que la media frente a outliers salariales.
CREATE OR REPLACE VIEW v_salary_stats_by_country AS
SELECT
    j.country_code,
    c.name                                              AS country_name,
    j.contract_type,
    COUNT(*)                                            AS job_count,
    ROUND(AVG(j.salary_min))                            AS avg_salary_min,
    ROUND(AVG(j.salary_max))                            AS avg_salary_max,
    ROUND(AVG(j.salary_mid))                            AS avg_salary_mid,
    ROUND(
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY j.salary_mid
        )::numeric
    )                                                   AS median_salary_eur
FROM jobs j
JOIN countries c ON c.code = j.country_code
WHERE j.is_active            = TRUE
  AND j.salary_mid           IS NOT NULL
  AND j.salary_is_predicted  = FALSE
GROUP BY j.country_code, c.name, j.contract_type
ORDER BY median_salary_eur DESC NULLS LAST;

-- Vista: porcentaje de ofertas remote por pais
CREATE OR REPLACE VIEW v_remote_pct_by_country AS
SELECT
    j.country_code,
    c.name                                              AS country_name,
    COUNT(*)                                            AS total_jobs,
    SUM(CASE WHEN j.remote = TRUE THEN 1 ELSE 0 END)   AS remote_jobs,
    ROUND(
        SUM(CASE WHEN j.remote = TRUE THEN 1 ELSE 0 END)
        * 100.0 / COUNT(*),
    2)                                                  AS remote_pct
FROM jobs j
JOIN countries c ON c.code = j.country_code
WHERE j.is_active = TRUE
  AND j.remote    IS NOT NULL
GROUP BY j.country_code, c.name
ORDER BY remote_pct DESC;

-- Vista: evolucion mensual de numero de ofertas por pais
CREATE OR REPLACE VIEW v_job_trends_monthly AS
SELECT
    DATE_TRUNC('month', j.posted_at)    AS month,
    j.country_code,
    c.name                              AS country_name,
    COUNT(*)                            AS job_count
FROM jobs j
JOIN countries c ON c.code = j.country_code
WHERE j.posted_at IS NOT NULL
GROUP BY DATE_TRUNC('month', j.posted_at), j.country_code, c.name
ORDER BY month DESC, job_count DESC;

-- Vista: salario por rol y pais (para grafica de barras agrupadas)
-- Usa mediana de salary_mid como metrica principal (robusta frente a outliers).
CREATE OR REPLACE VIEW v_salary_by_role_country AS
SELECT
    j.country_code,
    c.name                                          AS country_name,
    j.role_category,
    COUNT(*)                                        AS job_count,
    ROUND(AVG(j.salary_mid))                        AS avg_salary_eur,
    ROUND(
        PERCENTILE_CONT(0.5) WITHIN GROUP (
            ORDER BY j.salary_mid
        )::numeric
    )                                               AS median_salary_eur
FROM jobs j
JOIN countries c ON c.code = j.country_code
WHERE j.is_active           = TRUE
  AND j.role_category       IS NOT NULL
  AND j.salary_mid          IS NOT NULL
  AND j.salary_is_predicted = FALSE
GROUP BY j.country_code, c.name, j.role_category
ORDER BY j.country_code, median_salary_eur DESC NULLS LAST;

-- Vista: evolucion mensual de ofertas desglosada por rol
CREATE OR REPLACE VIEW v_demand_by_role_monthly AS
SELECT
    DATE_TRUNC('month', j.posted_at)    AS month,
    j.country_code,
    c.name                              AS country_name,
    j.role_category,
    COUNT(*)                            AS job_count
FROM jobs j
JOIN countries c ON c.code = j.country_code
WHERE j.posted_at    IS NOT NULL
  AND j.role_category IS NOT NULL
GROUP BY DATE_TRUNC('month', j.posted_at), j.country_code, c.name, j.role_category
ORDER BY month DESC, j.country_code, job_count DESC;

-- Vista: co-ocurrencia de skills por rol (que skills suelen pedirse juntas)
-- Util para el usuario que quiere saber que aprender ademas de una skill concreta.
-- La condicion js1.skill_id < js2.skill_id evita duplicar pares (A,B) y (B,A).
CREATE OR REPLACE VIEW v_skill_cooccurrence AS
SELECT
    s1.name                     AS skill,
    s2.name                     AS co_skill,
    j.role_category,
    COUNT(DISTINCT js1.job_id)  AS co_count
FROM job_skills js1
JOIN job_skills js2 ON js1.job_id   = js2.job_id
                   AND js1.skill_id < js2.skill_id
JOIN skills    s1   ON s1.id = js1.skill_id
JOIN skills    s2   ON s2.id = js2.skill_id
JOIN jobs       j   ON j.id  = js1.job_id
WHERE j.is_active   = TRUE
  AND j.posted_at   >= NOW() - INTERVAL '90 days'
GROUP BY s1.name, s2.name, j.role_category
ORDER BY co_count DESC;

-- Vista: top skills global (sin filtro de pais, para KPI card y ranking global)
-- Usada por el dashboard para mostrar las skills mas demandadas en toda la EU-8.
CREATE OR REPLACE VIEW v_top_skills_global AS
SELECT
    s.name                  AS skill,
    s.category              AS skill_category,
    COUNT(*)                AS job_count,
    ROUND(
        COUNT(*) * 100.0 /
        SUM(COUNT(*)) OVER (),
    2)                      AS pct_of_all_jobs
FROM job_skills js
JOIN jobs   j ON j.id = js.job_id
JOIN skills s ON s.id = js.skill_id
WHERE j.is_active = TRUE
  AND j.posted_at >= NOW() - INTERVAL '90 days'
GROUP BY s.name, s.category
ORDER BY job_count DESC;

-- Vista: total de ofertas activas por pais (para mapa EU del dashboard)
-- Usada por el componente de mapa para colorear los paises segun volumen de oferta.
CREATE OR REPLACE VIEW v_offers_by_country AS
SELECT
    j.country_code,
    c.name          AS country_name,
    COUNT(*)        AS total_jobs
FROM jobs      j
JOIN countries c ON c.code = j.country_code
WHERE j.is_active = TRUE
GROUP BY j.country_code, c.name
ORDER BY total_jobs DESC;

-- Vista: top skills con contexto de mercado laboral de Eurostat
CREATE OR REPLACE VIEW v_skills_with_market_context AS
SELECT
    s.name                              AS skill,
    s.category,
    j.country_code,
    c.name                              AS country_name,
    COUNT(*)                            AS demand,
    ROUND(AVG(j.salary_mid))            AS avg_salary_eur,
    lmc.value                           AS employment_rate,
    lmc.year                            AS context_year
FROM job_skills js
JOIN jobs      j   ON j.id          = js.job_id
JOIN skills    s   ON s.id          = js.skill_id
JOIN countries c   ON c.code        = j.country_code
LEFT JOIN labor_market_context lmc
    ON  lmc.country_code = j.country_code
    AND lmc.indicator    = 'employment_rate_15_64'
    AND lmc.year         = EXTRACT(YEAR FROM j.posted_at)::SMALLINT
WHERE j.is_active = TRUE
GROUP BY s.name, s.category, j.country_code, c.name, lmc.value, lmc.year
ORDER BY demand DESC;
