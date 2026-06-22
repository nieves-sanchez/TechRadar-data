# CLAUDE.md — TechRadar project context for Claude Code

## Project summary

Daily ETL pipeline (Pipeline A): Adzuna API → transform → upsert Supabase PostgreSQL → Eurostat → email.
8 EU countries, IT jobs, ~18k new offers/run. Crawling currently disabled in scheduled runs.
Production status: **first successful run 2026-06-22** (97.689 total offers in Supabase).
See README.md for full architecture and `notes/PROJECT_MASTER_CONTEXT.md` for full context.

Key files:
- `scripts/extract.py` — Adzuna API extraction + crawling + Eurostat
- `scripts/transform.py` — cleaning, salary normalization, NLP skill extraction
- `scripts/load.py` — UPSERT into Supabase (psycopg2)
- `scripts/pipeline.py` — ETL orchestrator, per-country extract+crawl loop
- `scripts/repair_crawl.py` — standalone script to recrawl offers with description_full IS NULL

---

## Current status (2026-06-22)

Pipeline A is in production. Crawling is intentionally disabled in scheduled runs (`--no-crawl`).
Pipeline B (independent crawling workflow) is pending design — do not modify pipeline.py for this.
Pipeline C (Ollama local enrichment) is pending.

**Do not propose Playwright until Pipeline B is designed. Do not modify pipeline.py, extract.py, or the schema.**

Known bugs:
- BUG-001: `jobs_extracted=0` in email/summary — cosmetic regression in pipeline.py, low priority. Use `jobs_loaded` instead.
- BUG-002: Automatic daily cron not yet confirmed — being monitored.
- BUG-003: `description_full` not populated in scheduled runs (crawling disabled) — pending Pipeline B.

---

## description_full crawling — background (Poland problem, as of 19/06/2026)

### What description_full is

Adzuna's API returns a short description (~200-500 chars). Each offer also has a `redirect_url`
field pointing to `https://www.adzuna.pl/land/ad/{job_id}?se=...&v=...`. The pipeline can crawl
these URLs to get the full job description (`description_full`), which is useful for
Ollama skill/role enrichment.

### Current state in the DB (as of 22/06/2026)

~97.689 total offers. Offers loaded since 2026-06-22 have `description_full IS NULL` (crawling disabled in cron).
Pre-existing offers from the Jun-17 run may have `description_full` where crawling succeeded.

Check with:
```sql
SELECT country_code, COUNT(*) as total,
       SUM(CASE WHEN description_full IS NULL THEN 1 ELSE 0 END) as missing
FROM jobs WHERE is_active = TRUE AND url IS NOT NULL
GROUP BY country_code ORDER BY missing DESC;
```

### Root cause investigation (conducted 18-19/06/2026)

**Step 1 — Initial symptom:** repair_crawl.py reported 1/50 successes consistently.
No circuit breaker firing → NOT a 429/503 throttling issue.

**Step 2 — URL test:** Opened one of the stored redirect URLs in the browser → loaded fine,
redirected to justjoin.it (Polish tech job board). URL is valid and not expired.

**Step 3 — Script test with `requests`:**
```
GET https://www.adzuna.pl/land/ad/5766275549?se=...
→ HTTP 403, body: "Nasze systemy wykryły podejrzane zachowanie" (suspicious behavior detected)
```

**Step 4 — Added full Chrome browser headers (`User-Agent`, `Accept`, `Accept-Language`, etc.):**
Got HTTP 200. The justjoin.it destination URL appears in the HTML as a `<link rel="preconnect">`
and `window.location` JS redirect — NOT as an HTTP redirect. `response.url` stays on adzuna.pl.

**Step 5 — justjoin.it is a Next.js App Router SPA.** When we followed the justjoin.it URL:
- First attempt: response body was binary garbage → cause: brotli compression (`br` in Accept-Encoding).
  Fix: removed `br` from Accept-Encoding headers.
- Second attempt with gzip only: got clean HTML but no `__NEXT_DATA__` (App Router doesn't use it).
  Found 1 JSON script tag (`id="__CONTEXT__"`) with API config, and several inline RSC payload scripts.

**Step 6 — justjoin.it API attempts:**
- Tried `https://justjoin.it/api/offers/{slug}` → 404 "Invalid endpoint"
- `__CONTEXT__` script revealed real API base: `https://api.justjoin.it`
- Tried `https://api.justjoin.it/offers/{slug}` → 404 "Invalid endpoint"

**Step 7 — Inline script extraction (WORKS when we can reach justjoin.it):**
Found that one of the inline RSC scripts (~3-8 KB, varies by offer) contains a JSON object
with a `"description"` field holding the full job text in plain UTF-8 Polish.

Extraction logic (implemented in `_crawl_justjoin` in `extract.py`):
```python
for tag in soup.find_all("script"):
    content = tag.string or ""
    if "description" not in content or len(content) > 50_000:
        continue
    # try json.loads first, then regex + json.loads for string decode
    match = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
    desc = json.loads(f'"{match.group(1)}"')  # correctly decodes \uXXXX and \n
```

Verified working in isolation: 3/3 test offers returned 2619-3891 char descriptions in correct Polish.

**Step 8 — Domain analysis of 50 Polish offers:**
```
HTTP_403_sin_match: 49
HTTP_200: 1
```

49/50 Adzuna redirect URLs return 403 even with full browser headers. The block is not a
simple User-Agent check — it's likely TLS fingerprinting (JA3) or JavaScript-based bot detection
that requires a real browser process. `requests` cannot bypass this.

### What is currently implemented

All changes are committed and compile clean. Verified with `py_compile`.

**`scripts/extract.py`:**
- `CRAWL_BROWSER_HEADERS`: full Chrome-like headers dict (no `br` in Accept-Encoding)
- `_crawl_justjoin(session, url)`: extracts description from justjoin.it inline RSC script
- `crawl_description()`: uses `CRAWL_BROWSER_HEADERS` on the session; after a 200 from Adzuna,
  searches the HTML for a justjoin.it URL via regex and calls `_crawl_justjoin`; backoff + circuit
  breaker on 429/503
- `enrich_with_full_descriptions()`: session uses `CRAWL_BROWSER_HEADERS`

**`scripts/pipeline.py`:**
- Per-country loop: each country is extracted then immediately crawled before moving to the next.
  This minimizes the gap between API extraction and crawling, reducing the chance of being flagged.

**`scripts/repair_crawl.py`:**
- Session uses `CRAWL_BROWSER_HEADERS`
- Queries `description_full IS NULL AND url IS NOT NULL AND is_active = TRUE`
- Flushes to DB every 100 successes
- Same circuit breaker as pipeline (10 consecutive throttle failures)

### Proposed next step: Playwright fallback

The fix is to use Playwright as a fallback when `requests` gets a 403 from Adzuna. Playwright
launches a real Chromium browser with full JS execution, real cookies, and a legitimate TLS
fingerprint — indistinguishable from a human browser session.

**Proposed implementation in `crawl_description()` in `extract.py`:**

```python
# After the requests attempt returns 403:
if response.status_code == 403:
    logger.debug("403 en Adzuna, reintentando con Playwright: %s", url)
    text = _crawl_with_playwright(url)
    return text, False
```

**New function `_crawl_with_playwright(url: str) -> Optional[str]`:**

```python
def _crawl_with_playwright(url: str) -> Optional[str]:
    """
    Fallback de crawling usando Playwright (Chromium headless) para URLs que
    devuelven 403 con requests. Lanza un browser real con JS completo.

    Solo se usa cuando requests falla con 403 — es más lento (~3-5s por URL)
    pero pasa el bot-detection de Adzuna y renderiza SPAs como justjoin.it.

    Requiere: pip install playwright && playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Playwright no instalado. Instalar: pip install playwright && playwright install chromium")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30_000)
            # Esperar a que el contenido dinámico cargue
            content = page.content()
            browser.close()

        # Intentar extraer de justjoin.it (estructura RSC)
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup.find_all("script"):
            script_content = tag.string or ""
            if "description" not in script_content or len(script_content) > 50_000:
                continue
            match = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.)*)"', script_content)
            if match:
                try:
                    desc = json.loads(f'"{match.group(1)}"')
                    if len(desc) > 100:
                        return desc.strip()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

        # Fallback: trafilatura sobre el HTML renderizado
        if _TRAFILATURA_AVAILABLE:
            extracted = trafilatura.extract(content, include_comments=False,
                                            include_tables=False, favor_recall=True)
            if extracted and len(extracted.strip()) > 100:
                return extracted.strip()

    except Exception as exc:
        logger.debug("Playwright fallback fallido para %s: %s", url, exc)

    return None
```

**Installation (add to requirements.txt):**
```
playwright>=1.44.0
```

And one-time setup:
```bash
pip install playwright
playwright install chromium
```

**Important considerations for Playwright:**

1. **Speed**: Playwright is ~3-5s per URL vs ~0.5s for requests. For 14,206 URLs at 3s = ~12 hours.
   Consider running repair_crawl overnight or with `--limit` batches.

2. **GitHub Actions**: Playwright works in GitHub Actions — Chromium can be installed in the
   workflow. Add to `.github/workflows/pipeline.yml`:
   ```yaml
   - name: Install Playwright
     run: |
       pip install playwright
       playwright install chromium
   ```

3. **Headless mode**: `headless=True` is standard for CI. Use `headless=False` only for local debugging.

4. **Session reuse**: To avoid launching a browser per URL (expensive), the implementation should
   reuse a single browser instance across all crawls in a run. Consider refactoring
   `enrich_with_full_descriptions` to pass a Playwright browser instance through the loop, similar
   to how `requests.Session` is currently passed.

5. **Page waiting strategy**: `wait_until="networkidle"` waits until no network requests for 500ms —
   safe but slow. `wait_until="domcontentloaded"` is faster but may miss dynamic content.
   For justjoin.it, `domcontentloaded` is enough since the RSC JSON is in the initial HTML payload.

6. **Playwright for repair_crawl.py**: The `_crawl_with_playwright` fallback can also be called
   from `repair_crawl.py` since it uses `crawl_description` from `extract.py`.

### Test script

`debug_crawl.py` in the project root is a diagnostic script that can be used to test crawling
behavior. It's not part of the pipeline — safe to delete after debugging.

---

## Other resolved issues (for context)

**numpy.int16 psycopg2 error (fixed 17/06/2026):**
`extract.py` used `astype("Int16")` (pandas nullable integer) for the Eurostat year column.
psycopg2 can't adapt pandas nullable integers. Fixed by changing to `astype(int)` in extract.py
and adding a safety net `_clean()` function in load.py that converts `np.integer`/`np.floating`
to Python native int/float via `.item()`.

**Polish salary PLN→EUR conversion:**
Implemented in `extract.py` `_parse_job_record()`. Three cases: annual salary (×0.2342),
daily B2B rate (×220 days×0.2342), already-in-EUR (multinationals). Threshold constants at
top of extract.py: `_PLN_DAILY_RATE_MAX`, `_PLN_ALREADY_EUR_MAX`, `_PLN_MAX_PLAUSIBLE`.

**role_category "other" rate ~52%:**
Expected at this stage — intentionally left for Ollama enrichment in retro_classify.py.

**skills coverage ~16% (83% NULL):**
Also expected — description_full is needed for better skill extraction. Once description_full
coverage improves (via Playwright), re-running transform on existing offers or relying on
Ollama enrichment will fill this in.
