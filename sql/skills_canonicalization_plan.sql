-- =============================================================================
-- skills_canonicalization_plan.sql
-- Plan de auditoría y limpieza de deuda histórica en la tabla skills
-- para React y Node.js (y otras variantes de alta frecuencia).
--
-- ⚠️  NO EJECUTAR sin revisar primero las consultas de auditoría (Sección A).
-- ⚠️  Ejecutar en transacción con ROLLBACK final hasta confirmar que el plan
--     es correcto. Solo sustituir ROLLBACK por COMMIT cuando estés seguro.
-- ⚠️  NO modifica el esquema principal de jobs. Solo skills y job_skills.
-- =============================================================================


-- =============================================================================
-- SECCIÓN A — AUDITORÍA (solo SELECTs, sin riesgo)
-- =============================================================================

-- A1 — Inventario de todas las variantes React y Node.js en la tabla skills
SELECT
    id,
    name,
    category,
    (SELECT COUNT(*) FROM job_skills js WHERE js.skill_id = s.id) AS vinculos
FROM skills s
WHERE
    name ILIKE '%react%'
    OR LOWER(name) IN ('node', 'node.js', 'nodejs', 'node js')
    OR name ILIKE '%node.js%'
    OR name ILIKE '%nodejs%'
ORDER BY name;


-- A2 — Vínculos por variante (frecuencia real de uso en job_skills)
SELECT
    s.id,
    s.name,
    s.category,
    COUNT(js.job_id) AS vinculos
FROM skills s
LEFT JOIN job_skills js ON js.skill_id = s.id
WHERE
    s.name ILIKE '%react%'
    OR LOWER(s.name) IN ('node', 'node.js', 'nodejs', 'node js')
    OR s.name ILIKE '%nodejs%'
GROUP BY s.id, s.name, s.category
ORDER BY vinculos DESC;


-- A3 — Frases compuestas que contienen React o Node.js
--      (más de 15 chars = probablemente frase, no skill individual)
SELECT
    s.id,
    s.name,
    s.category,
    COUNT(js.job_id) AS vinculos
FROM skills s
LEFT JOIN job_skills js ON js.skill_id = s.id
WHERE
    (s.name ILIKE '%react%' OR s.name ILIKE '%node%')
    AND LENGTH(s.name) > 15
    AND s.name NOT IN ('React', 'Node.js', 'React Native', 'NestJS', 'Next.js', 'Nodemailer')
GROUP BY s.id, s.name, s.category
HAVING COUNT(js.job_id) > 0
ORDER BY vinculos DESC;


-- A4 — Jobs que quedarían afectados por migración de React (aliases simples)
SELECT
    j.id AS job_id,
    j.title,
    s.name AS skill_actual,
    s.id   AS skill_id_actual
FROM jobs j
JOIN job_skills js ON js.job_id = j.id
JOIN skills s ON s.id = js.skill_id
WHERE s.name != 'React'
  AND (
      LOWER(s.name) IN ('react', 'reactjs')
      OR s.name IN ('React.js', 'react.js', 'React JS', 'React JS')
  )
  AND s.name NOT LIKE '% %'     -- excluir frases con espacio
ORDER BY j.id;


-- A5 — Jobs que quedarían afectados por migración de Node.js (aliases simples)
SELECT
    j.id AS job_id,
    j.title,
    s.name AS skill_actual,
    s.id   AS skill_id_actual
FROM jobs j
JOIN job_skills js ON js.job_id = j.id
JOIN skills s ON s.id = js.skill_id
WHERE s.name != 'Node.js'
  AND (
      LOWER(s.name) IN ('node', 'nodejs', 'node js')
      OR s.name IN ('node.js', 'NodeJS')
  )
  AND s.name NOT LIKE '% / %'   -- excluir frases
ORDER BY j.id;


-- A6 — Verificar que existen los canonicos antes de migrar
SELECT id, name FROM skills
WHERE name IN ('React', 'Node.js');


-- =============================================================================
-- SECCIÓN B — PLAN DE MIGRACIÓN (ejecutar en transacción)
-- =============================================================================
-- Orden: 1. Insertar nuevos vínculos con ID canónico.
--        2. Borrar vínculos que apuntaban a aliases.
--        3. Borrar las skills alias que quedaron sin vínculos.
--
-- Se excluyen deliberadamente las frases compuestas (ver Sección A3) porque
-- no se pueden reubicar automáticamente a un único canónico.
-- =============================================================================

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- B1 — REACT: relink aliases simples → 'React' canónico
-- ────────────────────────────────────────────────────────────────────────────

-- Paso B1a: crear nuevos vínculos con el ID de 'React'
INSERT INTO job_skills (job_id, skill_id)
SELECT DISTINCT
    js.job_id,
    (SELECT id FROM skills WHERE name = 'React')
FROM job_skills js
JOIN skills s ON s.id = js.skill_id
WHERE s.name != 'React'
  AND (
      LOWER(s.name) IN ('react', 'reactjs')
      OR s.name IN ('React.js', 'react.js', 'React JS')
  )
  AND s.name NOT LIKE '% %'     -- solo aliases simples, sin frases
ON CONFLICT DO NOTHING;


-- Paso B1b: borrar vínculos antiguos de aliases simples React
DELETE FROM job_skills
WHERE skill_id IN (
    SELECT id FROM skills
    WHERE name != 'React'
      AND (
          LOWER(name) IN ('react', 'reactjs')
          OR name IN ('React.js', 'react.js', 'React JS')
      )
      AND name NOT LIKE '% %'
);


-- Paso B1c: borrar las skills alias React que quedaron sin ningún vínculo
DELETE FROM skills
WHERE name != 'React'
  AND (
      LOWER(name) IN ('react', 'reactjs')
      OR name IN ('React.js', 'react.js', 'React JS')
  )
  AND name NOT LIKE '% %'
  AND id NOT IN (SELECT DISTINCT skill_id FROM job_skills);


-- ────────────────────────────────────────────────────────────────────────────
-- B2 — NODE.JS: relink aliases simples → 'Node.js' canónico
-- ────────────────────────────────────────────────────────────────────────────

-- Paso B2a: crear nuevos vínculos con el ID de 'Node.js'
INSERT INTO job_skills (job_id, skill_id)
SELECT DISTINCT
    js.job_id,
    (SELECT id FROM skills WHERE name = 'Node.js')
FROM job_skills js
JOIN skills s ON s.id = js.skill_id
WHERE s.name != 'Node.js'
  AND (
      LOWER(s.name) IN ('node', 'nodejs', 'node js')
      OR s.name IN ('node.js', 'NodeJS')
  )
  AND s.name NOT LIKE '% / %'   -- excluir frases compuestas
ON CONFLICT DO NOTHING;


-- Paso B2b: borrar vínculos antiguos de aliases simples Node.js
DELETE FROM job_skills
WHERE skill_id IN (
    SELECT id FROM skills
    WHERE name != 'Node.js'
      AND (
          LOWER(name) IN ('node', 'nodejs', 'node js')
          OR name IN ('node.js', 'NodeJS')
      )
      AND name NOT LIKE '% / %'
);


-- Paso B2c: borrar las skills alias Node.js que quedaron sin ningún vínculo
DELETE FROM skills
WHERE name != 'Node.js'
  AND (
      LOWER(name) IN ('node', 'nodejs', 'node js')
      OR name IN ('node.js', 'NodeJS')
  )
  AND name NOT LIKE '% / %'
  AND id NOT IN (SELECT DISTINCT skill_id FROM job_skills);


-- ────────────────────────────────────────────────────────────────────────────
-- B3 — VERIFICACIÓN POSTERIOR (ejecutar antes del COMMIT)
-- ────────────────────────────────────────────────────────────────────────────

-- ¿Quedan aliases después de la migración? Debe devolver 0 filas.
SELECT id, name, category FROM skills
WHERE
    (LOWER(name) IN ('react', 'reactjs') OR name IN ('React.js', 'react.js', 'React JS'))
    AND name != 'React'
UNION ALL
SELECT id, name, category FROM skills
WHERE
    (LOWER(name) IN ('node', 'nodejs', 'node js') OR name IN ('node.js', 'NodeJS'))
    AND name != 'Node.js';


-- ¿Los job_skills ahora apuntan al ID canónico?
SELECT s.id, s.name, COUNT(js.job_id) AS vinculos
FROM skills s
JOIN job_skills js ON js.skill_id = s.id
WHERE s.name IN ('React', 'Node.js')
GROUP BY s.id, s.name;


-- ────────────────────────────────────────────────────────────────────────────
-- Revisar resultados de las verificaciones ANTES de continuar.
-- Si todo es correcto: cambiar ROLLBACK por COMMIT.
-- ────────────────────────────────────────────────────────────────────────────

ROLLBACK;
-- COMMIT;


-- =============================================================================
-- SECCIÓN C — FRASES COMPUESTAS (requieren decisión manual)
-- =============================================================================
-- Ejemplos de deuda que NO se migra automáticamente:
--   'Angular / React / Javascript'
--   'Backend Developer Nodejs with German'
--   'Full Stack JS React / Node.js'
--
-- Opciones:
--   (a) Conservar como están (los vinculos existentes siguen vivos).
--   (b) Borrar solo los vinculos de job_skills (no la skill en sí).
--   (c) Borrar también la fila en skills si no queda ningún vínculo.
--
-- Consulta para revisar el impacto antes de decidir:

SELECT
    s.id,
    s.name,
    s.category,
    COUNT(js.job_id) AS vinculos_activos,
    STRING_AGG(j.title, ' | ' ORDER BY j.id LIMIT 3) AS ejemplos_de_jobs
FROM skills s
JOIN job_skills js ON js.skill_id = s.id
JOIN jobs j ON j.id = js.job_id
WHERE (s.name ILIKE '%react%' OR s.name ILIKE '%node%')
  AND s.name NOT IN ('React', 'Node.js', 'React Native', 'NestJS', 'Next.js', 'Nodemailer')
  AND (LENGTH(s.name) > 15 OR s.name LIKE '% %')
GROUP BY s.id, s.name, s.category
ORDER BY vinculos_activos DESC;
