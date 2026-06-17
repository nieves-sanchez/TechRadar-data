# TechRadar — EU Tech Job Market Pipeline

Weekly ETL pipeline that collects tech job listings from 8 EU countries, enriches them with NLP and a local LLM, and loads everything into PostgreSQL. Built to power a dashboard covering skill demand, salary comparisons, remote trends, and market evolution over time.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-3ECF8E?logo=supabase&logoColor=white)
![Pandas](https://img.shields.io/badge/pandas-150458?logo=pandas&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-2088FF?logo=github-actions&logoColor=white)
![Ollama](https://img.shields.io/badge/Ollama-qwen2.5:1.5b-black)
![React](https://img.shields.io/badge/React-61DAFB?logo=react&logoColor=black)
![Node.js](https://img.shields.io/badge/Node.js-339933?logo=node.js&logoColor=white)

---

## What it does

Runs automatically every Monday via GitHub Actions. Each run:

1. Fetches job listings from the [Adzuna API](https://developer.adzuna.com/) — 8 EU countries, IT category
2. Crawls each listing's full description (the API truncates to ~500 chars)
3. Classifies role category via regex NLP on the title + description
4. Extracts skills from a curated keyword catalog (230+ tools, languages, frameworks)
5. Fetches employment rate data from the [Eurostat API](https://ec.europa.eu/eurostat/web/main/data/web-services) for macroeconomic context
6. Upserts everything into Supabase PostgreSQL
7. Sends an HTML email summary — offers by country, crawling rate, pipeline status

After each automated run, an AI enrichment step runs locally:

8. `retro_classify.py --days 7` uses [Ollama](https://ollama.com/) (local LLM) to improve role classification and extract skills missed by regex

On the 1st of each month, a second GitHub Actions job sends a DB health report — storage usage, data quality metrics, top skills, salary coverage by country.

---

## Countries covered

🇩🇪 Germany · 🇫🇷 France · 🇪🇸 Spain · 🇳🇱 Netherlands · 🇵🇱 Poland · 🇮🇹 Italy · 🇦🇹 Austria · 🇧🇪 Belgium

> UK excluded: left the EU in 2021, and Eurostat doesn't publish UK employment data post-Brexit. All salaries normalized to EUR — Poland (PLN) converted at extraction time.

---

## Architecture

```
Adzuna API (8 EU countries)
  + crawling redirect_url for full descriptions
  │
  ▼
GitHub Actions — Monday 6AM UTC
  extract.py → transform.py → load.py → notify.py
  │
  ▼
Supabase PostgreSQL
  │
  ▼  (manual, after email)
retro_classify.py --days 7
  + Ollama qwen2.5:1.5b — role classification + skill extraction
  │
  ▼  (manual, after retro)
weekly_load_diagnostics.sql — verification in Supabase SQL Editor
  │
  ▼
GitHub Actions — 1st of month
  monthly_health_report.py → DB health email
  │
  ▼
Node.js + Express API → React Dashboard → Render
```

---

## Database

5 tables (3NF), 10 pre-built views for the dashboard API:

| Table | Description |
|-------|-------------|
| `countries` | 8 EU countries + currency + Adzuna endpoint |
| `jobs` | ~37k rows, growing ~3k/week |
| `skills` | Normalized skill catalog |
| `job_skills` | M:N — jobs ↔ skills |
| `labor_market_context` | Eurostat employment rate by country/year |

Views cover: skill co-occurrence, salary stats by country, remote percentage, monthly trends, role distribution.

Full schema: [`sql/schema.sql`](sql/schema.sql)

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in ADZUNA_APP_ID, ADZUNA_APP_KEY, DATABASE_URL, GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT
```

Run the schema once in the Supabase SQL Editor:

```sql
-- paste sql/schema.sql and execute
```

Run the pipeline:

```bash
python -m scripts.pipeline --days 30  # initial load
python -m scripts.pipeline --days 7   # weekly incremental
python -m scripts.pipeline --no-crawl # skip crawling (faster, fewer skills extracted)
```

AI enrichment (requires [Ollama](https://ollama.com/) running locally):

```bash
ollama pull qwen2.5:1.5b
python -m scripts.retro_classify --days 7   # enrich latest load
python -m scripts.retro_classify --all      # re-enrich entire DB
```

---

## Project structure

```
├── scripts/
│   ├── pipeline.py                 # ETL orchestrator + resume checkpoints
│   ├── extract.py                  # Adzuna API + crawling + Eurostat
│   ├── transform.py                # cleaning, salary normalization, NLP
│   ├── load.py                     # incremental upsert into Supabase
│   ├── skills_catalog.py           # regex patterns — skills, roles, non-IT filter
│   ├── ai_classifier.py            # Ollama — batch classification (10 jobs/call)
│   ├── retro_classify.py           # AI enrichment of DB records
│   ├── notify.py                   # HTML email summary after each run
│   └── monthly_health_report.py    # monthly DB health report
│
├── sql/
│   ├── schema.sql                   # full PostgreSQL schema + views
│   └── weekly_load_diagnostics.sql  # post-load verification queries
│
├── .github/workflows/
│   └── pipeline.yml                 # weekly + monthly cron jobs
│
└── notebooks/
    └── 00_source_feasibility.ipynb  # data source exploration + EDA
```

---

## Weekly workflow

| Step | When | Command |
|------|------|---------|
| Pipeline ETL + email | Monday 6AM UTC — automatic | GitHub Actions |
| AI enrichment | Manual — after email | `python -m scripts.retro_classify --days 7` |
| Verification | Manual — after enrichment | `sql/weekly_load_diagnostics.sql` in Supabase |
| DB health report | 1st of month — automatic | GitHub Actions |
