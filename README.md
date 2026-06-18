# TechRadar — EU Tech Job Market Pipeline

> Personal portfolio project. Weekly ETL pipeline that collects tech job listings from 8 EU countries, enriches them with NLP and a local LLM, and loads everything into PostgreSQL — feeding an interactive dashboard for exploring skill demand, salaries, remote trends, and market evolution across Europe.

<p>
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white"/>
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&logoColor=white"/>
  <img src="https://img.shields.io/badge/Supabase-3ECF8E?logo=supabase&logoColor=white"/>
  <img src="https://img.shields.io/badge/pandas-150458?logo=pandas&logoColor=white"/>
  <img src="https://img.shields.io/badge/GitHub_Actions-2088FF?logo=github-actions&logoColor=white"/>
  <img src="https://img.shields.io/badge/Ollama-qwen2.5:1.5b-black"/>
  <img src="https://img.shields.io/badge/Node.js-339933?logo=node.js&logoColor=white"/>
  <img src="https://img.shields.io/badge/React-61DAFB?logo=react&logoColor=black"/>
</p>

---

## Motivation

I wanted a project that combined real-world data engineering with something useful — so I built the backend of a job market tracker for the EU tech sector. The goal was to go beyond tutorials: real API pagination, crawling, currency normalization across countries, NLP skill extraction, incremental upserts, scheduled automation, and a local LLM in the loop. The dataset grows every week and feeds a live dashboard.

---

## How it works

Runs automatically every Monday via GitHub Actions. Each run:

1. Fetches job listings from the [Adzuna API](https://developer.adzuna.com/) — 8 EU countries, IT category, automatic pagination
2. Crawls each listing's full description (the API truncates to ~500 chars)
3. Classifies role category via regex NLP on title + description (16 categories, 6 languages)
4. Extracts skills from a curated keyword catalog — 230+ tools, languages, frameworks, and methodologies
5. Fetches employment rate data from the [Eurostat API](https://ec.europa.eu/eurostat/web/main/data/web-services) for macroeconomic context
6. Upserts everything into Supabase PostgreSQL with full conflict resolution
7. Sends an HTML email summary — offers by country, crawling rate, pipeline status

After each automated run, an AI enrichment step runs locally:

8. `retro_classify.py --days 7` uses [Ollama](https://ollama.com/) (local LLM, `qwen2.5:1.5b`) to improve role classification and catch skills missed by regex — processes offers in batches of 10 to reduce HTTP overhead

On the 1st of each month, a second GitHub Actions job sends a DB health report — storage usage, data quality metrics, NULL rate, top skills, salary coverage by country.

---

## Countries covered

| Country | Code | Currency | Notes |
|---------|------|----------|-------|
| Germany | DE | EUR | |
| France | FR | EUR | |
| Spain | ES | EUR | |
| Netherlands | NL | EUR | |
| Poland | PL | PLN → EUR | Converted at extraction time |
| Italy | IT | EUR | |
| Austria | AT | EUR | |
| Belgium | BE | EUR | |

> **Why not UK?** Left the EU in 2021. Eurostat doesn't publish UK employment data post-Brexit, and GBP would complicate salary comparisons.

---

## Architecture

```
Adzuna API (8 EU countries, IT jobs)
  + crawling redirect_url for full descriptions
  │
  ▼
GitHub Actions — Monday 6AM UTC
  extract.py → transform.py → load.py → notify.py (email)
  │
  ▼
Supabase PostgreSQL
  │
  ▼  (manual — after pipeline email)
retro_classify.py --days 7 + Ollama qwen2.5:1.5b
  → improved role classification + skill extraction
  │
  ▼  (manual — after enrichment)
weekly_load_diagnostics.sql — verification in Supabase SQL Editor
  │
  ▼
GitHub Actions — 1st of month
  monthly_health_report.py → DB health email
  │
  ▼
Node.js + Express REST API → React Dashboard → Render
```

---

## Key technical decisions

**PLN → EUR currency conversion** — Poland uses złoty, not EUR. A naive conversion would make Polish salaries appear ~4x higher. The pipeline distinguishes three cases at extraction time: annual salaries (PLN × 0.2342), daily B2B rates (PLN × 220 working days × 0.2342), and amounts already in EUR published by multinationals. Corrupt/implausible values are nulled.

**Longest-match skill extraction** — a naive regex approach double-counts pairs like GitHub / GitHub Actions or Spark / Spark Streaming. The extractor tracks match spans and, when two patterns overlap, keeps only the longer one.

**Checkpoint + resume** — crawling 5,000+ URLs at 2s/request takes hours. If the run is interrupted, the pipeline saves progress to `data/checkpoints/` and resumes from where it left off (`--resume` flag) without re-hitting the Adzuna API.

**Soft-delete over hard delete** — offers filtered as non-IT (by regex patterns or flagged by Ollama) are marked `is_active = FALSE` instead of deleted. Historical data stays intact for trend analysis; the flag can be reviewed and reverted if a pattern is too aggressive.

**Local LLM via Ollama** — zero cost, no rate limits, no data leaving the machine. The tradeoff is that Ollama can't run in GitHub Actions (it's a desktop app), so the AI enrichment step runs manually after each automated pipeline execution. The swap to a hosted API (Anthropic, OpenAI) would only require changing the endpoint and response parsing in `ai_classifier.py`.

---

## Database

5 tables (3NF), 10 pre-built views for the dashboard API:

| Table | Rows (approx.) | Description |
|-------|---------------|-------------|
| `countries` | 8 | EU countries + currency + Adzuna endpoint |
| `jobs` | ~37k, +3k/week | Job listings with salary, location, role, remote flag |
| `skills` | ~230 | Normalized skill catalog |
| `job_skills` | ~200k | M:N — jobs ↔ skills |
| `labor_market_context` | ~50 | Eurostat employment rate by country/year |

Views cover: skill co-occurrence, salary distribution by country, remote percentage, monthly trends, role breakdown, top skills by country.

Full schema: [`sql/schema.sql`](sql/schema.sql)

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in: ADZUNA_APP_ID, ADZUNA_APP_KEY, DATABASE_URL,
#           GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT
```

Run the schema once in the Supabase SQL Editor:

```sql
-- paste sql/schema.sql and run
```

Run the pipeline:

```bash
python -m scripts.pipeline --days 30  # initial load
python -m scripts.pipeline --days 7   # weekly incremental
python -m scripts.pipeline --resume   # resume after interruption
python -m scripts.pipeline --no-crawl # skip crawling (faster, fewer skills)
```

AI enrichment (requires [Ollama](https://ollama.com/) running locally with `qwen2.5:1.5b`):

```bash
ollama pull qwen2.5:1.5b
python -m scripts.retro_classify --days 7   # enrich latest load
python -m scripts.retro_classify --all      # re-enrich entire DB
python -m scripts.retro_classify --limit 50 # dry run on 50 offers
```

GitHub Actions secrets required: `DATABASE_URL`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `RECIPIENT`.

---

## Project structure

```
├── scripts/
│   ├── pipeline.py                 # ETL orchestrator + checkpoint/resume logic
│   ├── extract.py                  # Adzuna API + crawling + Eurostat
│   ├── transform.py                # cleaning, salary normalization, NLP
│   ├── load.py                     # incremental upsert into Supabase
│   ├── skills_catalog.py           # regex patterns — skills, roles, non-IT filter
│   ├── ai_classifier.py            # Ollama — batch classification (10 jobs/call)
│   ├── retro_classify.py           # AI enrichment of DB records
│   ├── notify.py                   # HTML email after each pipeline run
│   └── monthly_health_report.py    # monthly DB health email
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
| AI enrichment | Manual — after pipeline email | `python -m scripts.retro_classify --days 7` |
| Verification | Manual — after enrichment | `sql/weekly_load_diagnostics.sql` in Supabase |
| DB health report | 1st of month — automatic | GitHub Actions |

---

## Part II — API & Dashboard

The REST API and React dashboard that consume this data are being developed by a colleague as a separate project. The API (Node.js + Express) exposes the pre-built views from this pipeline's database; the frontend (React + Recharts + react-simple-maps) visualizes skill demand, salary ranges, remote percentages, and monthly trends across the 8 EU countries. Both are deployed on Render.

---

## Author

**Nieves Sánchez García** — Data Engineering portfolio project, 2026.

<p>
  <a href="https://github.com/YOUR_GITHUB">
    <img src="https://img.shields.io/badge/GitHub-YOUR__GITHUB-181717?logo=github&logoColor=white"/>
  </a>
  &nbsp;
  <a href="https://linkedin.com/in/YOUR_LINKEDIN">
    <img src="https://img.shields.io/badge/LinkedIn-Nieves_Sánchez_García-0A66C2?logo=linkedin&logoColor=white"/>
  </a>
</p>
