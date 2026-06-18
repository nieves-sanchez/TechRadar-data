"""
pipeline.py — Orquestador del pipeline ETL de TechRadar.

Ejecuta las tres fases en secuencia:
    1. Extracción  — Adzuna API + crawling redirect_url + Eurostat API
    2. Transformación — limpieza, validación y enriquecimiento (salary_mid, skills, etc.)
    3. Carga       — UPSERT incremental en Supabase PostgreSQL

Modos de ejecución:
    Carga inicial   (primera vez):  max_days_old=30, extrae ~30.000 ofertas
    Carga semanal   (recurrente):   max_days_old=7,  extrae ~3.000 ofertas nuevas

Uso desde línea de comandos:
    python -m scripts.pipeline                        # carga semanal (por defecto)
    python -m scripts.pipeline --days 30              # carga inicial
    python -m scripts.pipeline --days 7 --no-crawl    # sin crawling (más rápido)
    python -m scripts.pipeline --no-eurostat          # sin actualizar Eurostat

Uso desde código (p.ej. tests o notebooks):
    from scripts.pipeline import run
    run(max_days_old=7)
"""

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts.extract import (
    ADZUNA_COUNTRIES,
    extract_adzuna,
    extract_eurostat,
    enrich_with_full_descriptions,
)
from scripts.transform import transform_jobs, transform_eurostat
from scripts.load import load_jobs, load_eurostat

# Directorio y ficheros de checkpoint.
# RAW_CHECKPOINT: datos de la API antes del crawling.
# CRAWL_CHECKPOINT: progreso del crawling (se actualiza cada 200 ofertas).
# Ambos se borran automáticamente tras una carga exitosa en Supabase.
CHECKPOINT_DIR   = Path("data/checkpoints")
RAW_CHECKPOINT   = CHECKPOINT_DIR / "adzuna_raw.csv"
CRAWL_CHECKPOINT = CHECKPOINT_DIR / "adzuna_crawl.csv"

# Fichero de resumen que lee scripts/notify.py para el email post-pipeline.
# Se escribe al final de cada run (éxito o fallo) y se borra al inicio del siguiente.
SUMMARY_PATH = Path("data/pipeline_summary.json")


def _write_summary(summary: dict) -> None:
    """
    Persiste el resumen del pipeline en disco para que notify.py lo lea.

    Args:
        summary: Dict con stats de la ejecución (status, ofertas, crawl, etc.).
    """
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    logger.info("Resumen guardado en %s", SUMMARY_PATH)

# Configuración de logging para ejecución directa del pipeline.
# pipeline.py es el punto de entrada principal, así que configura el
# formato aquí. Los módulos subordinados (extract, transform, load)
# usan getLogger() sin configurar — heredan este formato.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("techradar.pipeline")


def run(
    max_days_old: int = 7,
    crawling: bool = True,
    eurostat: bool = True,
    resume: bool = False,
    countries: list[str] = None,
) -> None:
    """
    Ejecuta el pipeline ETL completo.

    Args:
        max_days_old: Antigüedad máxima de las ofertas a extraer en días.
                      7  → carga semanal incremental (uso habitual).
                      30 → carga inicial o recuperación tras una semana sin ejecutar.
        crawling:     Si True, enriquece las ofertas con description_full via crawling.
                      Desactivar reduce el tiempo de ejecución pero empeora la extracción de skills.
        eurostat:     Si True, descarga y carga los datos de Eurostat.
                      Eurostat publica datos anuales, no es necesario actualizar cada semana,
                      pero es seguro hacerlo (ON CONFLICT DO UPDATE).
        resume:       Si True y existe un checkpoint en data/checkpoints/, reanuda desde él
                      sin volver a llamar a la API de Adzuna. Útil tras un corte inesperado.
        countries:    Lista de códigos de país a extraer (ej: ['pl']).
                      Si es None, extrae los 8 países por defecto.
    """
    start_total = time.time()

    # Borrar resumen anterior para que notify.py no use datos obsoletos.
    if SUMMARY_PATH.exists():
        SUMMARY_PATH.unlink()

    # Dict de resumen que se irá poblando a lo largo del run y se
    # persistirá al final para que notify.py lo lea.
    summary: dict = {
        "status":             "failed",
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "duration_minutes":   0.0,
        "jobs_extracted":     0,
        "jobs_loaded":        0,
        "crawl_total":        0,
        "crawl_success":      0,
        "crawl_success_rate": 0.0,
        "by_country":         {},
        "eurostat_loaded":    False,
        "error":              None,
    }

    logger.info("=" * 60)
    logger.info("Pipeline TechRadar iniciado (max_days_old=%d)", max_days_old)
    logger.info("=" * 60)

    try:
        # -------------------------------------------------------------------------
        # Fase 1: Extracción
        # -------------------------------------------------------------------------
        logger.info("FASE 1 — Extracción")

        # ── Extracción o reanudación desde checkpoint ─────────────────────────────
        if resume and CRAWL_CHECKPOINT.exists():
            # Checkpoint de crawling disponible: saltamos extracción Y crawling ya hechos.
            logger.info("  Reanudando desde checkpoint de crawling: %s", CRAWL_CHECKPOINT)
            t0 = time.time()
            raw_jobs_df = pd.read_csv(CRAWL_CHECKPOINT)
            raw_jobs_df["posted_at"] = pd.to_datetime(raw_jobs_df["posted_at"], utc=True, errors="coerce")
            pending = raw_jobs_df["description_full"].isna().sum()
            logger.info(
                "  %d ofertas cargadas desde checkpoint (%d pendientes de crawling) (%.1fs)",
                len(raw_jobs_df), pending, time.time() - t0,
            )
        elif resume and RAW_CHECKPOINT.exists():
            # Checkpoint raw disponible: saltamos la API pero repetimos el crawling.
            logger.info("  Reanudando desde checkpoint raw (sin crawling previo): %s", RAW_CHECKPOINT)
            t0 = time.time()
            raw_jobs_df = pd.read_csv(RAW_CHECKPOINT)
            raw_jobs_df["posted_at"] = pd.to_datetime(raw_jobs_df["posted_at"], utc=True, errors="coerce")
            logger.info("  %d ofertas cargadas desde checkpoint (%.1fs)", len(raw_jobs_df), time.time() - t0)
        else:
            if resume:
                logger.warning("  --resume activo pero no se encontraron checkpoints. Extrayendo desde la API.")

            # Extracción y crawling por país: cada país se crawlea inmediatamente
            # después de extraerse para minimizar el tiempo entre la llamada a la API
            # y el crawling del redirect_url. Si se crawlea todo al final, los países
            # al final de la lista (ej. PL) acumulan horas de espera y acaban bloqueados.
            target_countries = countries if countries else list(ADZUNA_COUNTRIES.keys())
            all_country_dfs = []
            crawl_total_acc = 0
            crawl_success_acc = 0

            for cc in target_countries:
                t_cc = time.time()
                cc_df = extract_adzuna(max_days_old=max_days_old, countries=[cc])

                if cc_df.empty:
                    logger.warning("  %s: sin ofertas de Adzuna.", cc.upper())
                    continue

                logger.info("  %s: %d ofertas extraídas (%.1fs)", cc.upper(), len(cc_df), time.time() - t_cc)

                if crawling:
                    t_crawl = time.time()
                    cc_df = enrich_with_full_descriptions(cc_df)
                    cc_ok = int(cc_df["description_full"].notna().sum())
                    cc_total = int(cc_df["url"].notna().sum())
                    crawl_total_acc += cc_total
                    crawl_success_acc += cc_ok
                    logger.info(
                        "  %s: crawling completado — %d/%d con descripción (%.1fs)",
                        cc.upper(), cc_ok, cc_total, time.time() - t_crawl,
                    )

                all_country_dfs.append(cc_df)

            raw_jobs_df = pd.concat(all_country_dfs, ignore_index=True) if all_country_dfs else pd.DataFrame()

            if raw_jobs_df.empty:
                logger.warning("Sin ofertas extraídas de Adzuna. Abortando pipeline.")
                summary["error"] = "Adzuna no devolvió ofertas. Verificar credenciales."
                return

            if crawling:
                summary["crawl_total"]        = crawl_total_acc
                summary["crawl_success"]      = crawl_success_acc
                summary["crawl_success_rate"] = round(crawl_success_acc / crawl_total_acc * 100, 1) if crawl_total_acc else 0.0
            else:
                logger.info("  Crawling omitido (--no-crawl activo).")

        # -------------------------------------------------------------------------
        # Fase 2: Transformación de ofertas
        # -------------------------------------------------------------------------
        logger.info("FASE 2 — Transformación")

        t0 = time.time()
        jobs_df, job_skills_df = transform_jobs(raw_jobs_df)
        logger.info(
            "  Jobs: %d ofertas limpias, %d vínculos job-skill (%.1fs)",
            len(jobs_df), len(job_skills_df), time.time() - t0,
        )

        if jobs_df.empty:
            logger.warning("Ninguna oferta superó la transformación. Abortando pipeline.")
            summary["error"] = "Ninguna oferta superó la validación en transform.py."
            return

        # Distribución por país de las ofertas procesadas en esta carga
        summary["by_country"] = (
            jobs_df.groupby("country_code").size().to_dict()
            if "country_code" in jobs_df.columns else {}
        )

        # -------------------------------------------------------------------------
        # Fase 3: Carga de ofertas
        # -------------------------------------------------------------------------
        logger.info("FASE 3 — Carga")

        t0 = time.time()
        load_jobs(jobs_df, job_skills_df)
        logger.info("  load_jobs completado (%.1fs)", time.time() - t0)

        # jobs_loaded se actualiza aquí, tras load_jobs() exitoso.
        # Usar len(jobs_df) como proxy de filas enviadas al UPSERT.
        summary["jobs_loaded"] = len(jobs_df)

        # Borrar checkpoints tras carga exitosa: ya no son necesarios.
        for checkpoint_file in [RAW_CHECKPOINT, CRAWL_CHECKPOINT]:
            if checkpoint_file.exists():
                checkpoint_file.unlink()
                logger.info("  Checkpoint eliminado: %s", checkpoint_file)

        # -------------------------------------------------------------------------
        # Eurostat (extracción + transformación + carga)
        # -------------------------------------------------------------------------
        if eurostat:
            logger.info("EUROSTAT — Extracción, transformación y carga")

            t0 = time.time()
            raw_eurostat_df = extract_eurostat()
            eurostat_df = transform_eurostat(raw_eurostat_df)
            load_eurostat(eurostat_df)
            summary["eurostat_loaded"] = True
            logger.info("  Eurostat completado (%.1fs)", time.time() - t0)
        else:
            logger.info("EUROSTAT omitido (--no-eurostat activo).")

        summary["status"] = "success"

        # Resumen en log (dentro del try — jobs_df está garantizado aquí)
        elapsed = time.time() - start_total
        logger.info("=" * 60)
        logger.info(
            "Pipeline completado en %.1f min — %d ofertas procesadas",
            elapsed / 60,
            len(jobs_df),
        )
        logger.info("=" * 60)

    except Exception as exc:
        summary["status"] = "failed"
        summary["error"]  = str(exc)
        logger.error("Pipeline falló con excepción: %s", exc)
        raise

    finally:
        # Siempre persistir el resumen, tanto en éxito como en fallo.
        summary["duration_minutes"] = round((time.time() - start_total) / 60, 1)
        _write_summary(summary)


# =============================================================================
# Punto de entrada para ejecución directa: python -m scripts.pipeline
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Pipeline ETL de TechRadar — extrae, transforma y carga datos de empleo tech EU."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        metavar="N",
        help="Antigüedad máxima de las ofertas en días (default: 7 para carga semanal, usa 30 para carga inicial).",
    )
    parser.add_argument(
        "--no-crawl",
        action="store_true",
        help="Omite el crawling de description_full. Más rápido pero peor extracción de skills.",
    )
    parser.add_argument(
        "--no-eurostat",
        action="store_true",
        help="Omite la actualización de datos de Eurostat.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Reanuda el pipeline desde el último checkpoint guardado en data/checkpoints/. "
            "Evita repetir llamadas a la API de Adzuna tras un corte inesperado."
        ),
    )
    parser.add_argument(
        "--countries",
        nargs="+",
        metavar="CC",
        help="Extrae solo los países indicados (ej: --countries pl es). Por defecto los 8 países.",
    )

    args = parser.parse_args()

    if args.days < 1 or args.days > 365:
        parser.error("--days debe estar entre 1 y 365")

    run(
        max_days_old=args.days,
        crawling=not args.no_crawl,
        eurostat=not args.no_eurostat,
        resume=args.resume,
        countries=args.countries,
    )
