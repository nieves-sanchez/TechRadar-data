# TechRadar — Pipeline de Datos

Pipeline ETL que recopila ofertas de empleo tech en 8 países de la UE y las carga en una base de datos PostgreSQL. Los datos alimentan un dashboard donde personas que buscan trabajo en tech pueden explorar el mercado: qué skills están más demandadas, cómo se comparan los salarios entre países, qué skills suelen pedirse juntas y cómo evolucionan las tendencias a lo largo del tiempo.

## Qué hace

Se ejecuta semanalmente via GitHub Actions. En cada ejecución:

1. Descarga ofertas de empleo de la API de Adzuna (8 países EU, categoría IT)
2. Hace crawling de la descripción completa de cada oferta desde su URL (la API trunca a 500 chars)
3. Extrae skills de la descripción completa mediante NLP
4. Descarga datos de tasa de empleo de la API de Eurostat para contexto macroeconómico
5. Hace upsert de todo en Supabase PostgreSQL

## Países cubiertos

Alemania, Francia, España, Países Bajos, Polonia, Italia, Austria, Bélgica — todos los salarios en EUR.

**¿Por qué no incluye UK?** Reino Unido salió de la UE en 2021. El proyecto analiza el mercado tech europeo (EU-27), y Eurostat no publica datos de empleo de UK post-Brexit. Además, UK usa GBP en lugar de EUR, lo que complicaría las comparativas salariales directas entre países.

## Stack

- Python 3.10+ — `requests`, `beautifulsoup4`, `pandas`, `psycopg2`
- PostgreSQL via Supabase (plan gratuito)
- GitHub Actions para la automatización semanal

## Configuración

```bash
pip install -r requirements.txt
cp .env.example .env
# Rellena tus credenciales en .env
```

Necesitas una [API key de Adzuna](https://developer.adzuna.com/) (gratuita) y la URL de tu proyecto Supabase.

Ejecuta el schema una vez para crear las tablas:

```bash
psql $DATABASE_URL -f sql/schema.sql
```

Luego ejecuta el pipeline completo:

```bash
python scripts/pipeline.py
```

O ejecuta los pasos individualmente:

```bash
python scripts/extract.py    # descarga datos
python scripts/transform.py  # limpieza + NLP
python scripts/load.py       # upsert en la BD
```

## Base de datos

5 tablas: `countries`, `jobs`, `skills`, `job_skills`, `labor_market_context`

10 vistas pre-construidas para la API del dashboard, incluyendo co-ocurrencia de skills por rol (qué skills suelen pedirse juntas en la misma oferta), top skills global, y distribución de ofertas por país.

Consulta `sql/schema.sql` para el schema completo.

## Estructura del proyecto

```txt
├── notebooks/          # análisis exploratorio de fuentes de datos
├── notes/              # documentación técnica y decisiones del proyecto
├── scripts/
│   ├── extract.py      # Adzuna API + crawling + Eurostat
│   ├── transform.py    # limpieza, extracción de skills NLP, clasificación de rol
│   ├── load.py         # upsert en Supabase
│   └── pipeline.py     # orquesta el ETL completo
└── sql/
    └── schema.sql      # tablas, índices y vistas
```
