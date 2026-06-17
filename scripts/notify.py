"""
notify.py — Email de resumen tras cada ejecución del pipeline ETL de TechRadar.

Se ejecuta como paso final en el workflow de GitHub Actions (if: always()),
tanto si el pipeline tuvo éxito como si falló.

Lee el resumen generado por pipeline.py en data/pipeline_summary.json
y envía un email HTML al correo electrónico configurado con:
  - Estado global del pipeline (éxito / fallo)
  - Ofertas ingestadas por país en esta carga
  - Tasa de éxito del crawling
  - Total acumulado en la base de datos
  - Tiempo de ejecución
  - Enlace directo al log de GitHub Actions si algo falló

Variables de entorno requeridas:
  GMAIL_USER         — dirección Gmail desde la que se envía
  GMAIL_APP_PASSWORD — App Password de Gmail (16 caracteres, sin espacios)
  DATABASE_URL       — connection string de Supabase (para stats de BD)

Variables inyectadas por GitHub Actions:
  PIPELINE_STATUS    — 'success' | 'failure' | 'cancelled' (estado del job)
  ACTIONS_RUN_URL    — URL directa al log del workflow en GitHub

Uso:
  python -m scripts.notify
"""

import html
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("techradar.notify")

GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
DATABASE_URL       = os.getenv("DATABASE_URL", "")
PIPELINE_STATUS    = os.getenv("PIPELINE_STATUS", "unknown")
ACTIONS_RUN_URL    = os.getenv("ACTIONS_RUN_URL", "")
RECIPIENT          = os.getenv("RECIPIENT", "")

SUMMARY_PATH = Path("data/pipeline_summary.json")

# Emojis de estado para el asunto del email
_STATUS_EMOJI = {
    "success":   "✅",
    "failure":   "❌",
    "cancelled": "⚠️",
}

# Nombres legibles de los países cubiertos
_COUNTRY_NAMES = {
    "de": "Alemania",
    "fr": "Francia",
    "es": "España",
    "nl": "Países Bajos",
    "pl": "Polonia",
    "it": "Italia",
    "at": "Austria",
    "be": "Bélgica",
}


# =============================================================================
# Lectura del resumen generado por pipeline.py
# =============================================================================


def _load_summary() -> dict:
    """
    Carga el resumen JSON escrito por pipeline.py.

    Si el fichero no existe (el pipeline falló antes de escribirlo),
    devuelve un diccionario vacío con el estado de fallo.
    """
    if SUMMARY_PATH.exists():
        try:
            with open(SUMMARY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("No se pudo leer %s: %s", SUMMARY_PATH, exc)

    return {"status": "failed", "error": "No se generó fichero de resumen."}


# =============================================================================
# Consultas a Supabase para contexto adicional
# =============================================================================


def _fetch_db_stats(summary: dict) -> dict:
    """
    Consulta Supabase para obtener el total de ofertas en la BD
    y la distribución de la última carga.

    Si no se puede conectar devuelve un dict con valores None
    sin lanzar excepción (el email se envía igualmente, sin stats de BD).
    """
    if not DATABASE_URL:
        logger.warning("DATABASE_URL no configurado — stats de BD no disponibles.")
        return {"total_in_db": None, "by_country_db": {}}

    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Total acumulado en la BD
        cur.execute("SELECT COUNT(*) FROM jobs")
        total_in_db = cur.fetchone()[0]

        # Distribución de la última carga (últimas 2 horas para mayor seguridad)
        cur.execute("""
            SELECT country_code, COUNT(*) AS n
            FROM   jobs
            WHERE  ingested_at >= NOW() - INTERVAL '2 hours'
            GROUP  BY country_code
            ORDER  BY n DESC
        """)
        by_country_db = {row[0]: row[1] for row in cur.fetchall()}

        conn.close()
        return {"total_in_db": total_in_db, "by_country_db": by_country_db}

    except Exception as exc:
        logger.warning("Error consultando Supabase para stats: %s", exc)
        return {"total_in_db": None, "by_country_db": {}}


# =============================================================================
# Construcción del email HTML
# =============================================================================


def _build_email_html(summary: dict, db_stats: dict) -> tuple[str, str]:
    """
    Genera el asunto y el cuerpo HTML del email de resumen.

    Args:
        summary:  Contenido de pipeline_summary.json.
        db_stats: Stats adicionales consultadas en Supabase.

    Returns:
        (subject, html_body)
    """
    # Determinar estado final
    status = PIPELINE_STATUS if PIPELINE_STATUS != "unknown" else summary.get("status", "unknown")
    emoji  = _STATUS_EMOJI.get(status, "❓")
    now    = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    subject = f"{emoji} TechRadar Pipeline — {status.upper()} — {now}"

    # Stats del summary (con fallbacks seguros)
    jobs_extracted   = summary.get("jobs_extracted", "—")
    jobs_loaded      = summary.get("jobs_loaded", "—")
    crawl_total      = summary.get("crawl_total", 0)
    crawl_success    = summary.get("crawl_success", 0)
    crawl_rate       = summary.get("crawl_success_rate", 0.0)
    duration         = summary.get("duration_minutes", 0.0)
    eurostat_loaded  = summary.get("eurostat_loaded", False)
    error_msg        = summary.get("error")

    # Stats de BD
    total_in_db      = db_stats.get("total_in_db")
    by_country_db    = db_stats.get("by_country_db", {})

    # Preferir distribución por país de la BD (más fresca) sobre la del summary
    by_country = by_country_db if by_country_db else summary.get("by_country", {})

    # Color de cabecera según estado
    header_color = {"success": "#2e7d32", "failure": "#c62828", "cancelled": "#f57c00"}.get(
        status, "#546e7a"
    )

    # Filas de la tabla por país
    country_rows = ""
    for code, count in sorted(by_country.items(), key=lambda x: -x[1]):
        name = html.escape(_COUNTRY_NAMES.get(code, code.upper()))
        country_rows += f"<tr><td>{name} ({html.escape(code.upper())})</td><td style='text-align:right'><b>{count:,}</b></td></tr>"
    if not country_rows:
        country_rows = "<tr><td colspan='2' style='color:#888'>Sin datos de distribución por país</td></tr>"

    # Bloque de error (solo si hubo fallo)
    error_block = ""
    if error_msg:
        error_block = f"""
        <div style='margin:16px 0;padding:12px 16px;background:#fff3f3;border-left:4px solid #c62828;border-radius:4px'>
            <b style='color:#c62828'>Error registrado:</b><br>
            <code style='font-size:13px'>{html.escape(str(error_msg))}</code>
        </div>"""

    # Enlace al log de GitHub Actions
    actions_link = ""
    if ACTIONS_RUN_URL:
        actions_link = f"""
        <p style='margin-top:24px'>
            <a href='{ACTIONS_RUN_URL}'
               style='background:#1565c0;color:#fff;padding:10px 20px;border-radius:4px;
                      text-decoration:none;font-weight:bold;font-size:14px'>
                Ver log completo en GitHub Actions →
            </a>
        </p>"""

    html_body = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"></head>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#212121">

        <!-- Cabecera -->
        <div style="background:{header_color};color:#fff;padding:20px 24px;border-radius:8px 8px 0 0">
            <h2 style="margin:0;font-size:20px">{emoji} TechRadar — Pipeline ETL</h2>
            <p style="margin:4px 0 0;opacity:.85;font-size:13px">{now}</p>
        </div>

        <!-- Cuerpo -->
        <div style="background:#f5f5f5;padding:24px;border-radius:0 0 8px 8px">

            {error_block}

            <!-- Métricas principales -->
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">
                <tr style="background:#e3f2fd">
                    <td style="padding:10px 16px;font-weight:bold;color:#1565c0" colspan="2">📊 Resumen de la carga</td>
                </tr>
                <tr><td style="padding:8px 16px;border-top:1px solid #eee">Ofertas extraídas de Adzuna</td>
                    <td style="padding:8px 16px;border-top:1px solid #eee;text-align:right"><b>{jobs_extracted if isinstance(jobs_extracted, str) else f'{jobs_extracted:,}'}</b></td></tr>
                <tr><td style="padding:8px 16px;border-top:1px solid #eee">Ofertas cargadas en BD</td>
                    <td style="padding:8px 16px;border-top:1px solid #eee;text-align:right"><b>{jobs_loaded if isinstance(jobs_loaded, str) else f'{jobs_loaded:,}'}</b></td></tr>
                <tr><td style="padding:8px 16px;border-top:1px solid #eee">Total acumulado en BD</td>
                    <td style="padding:8px 16px;border-top:1px solid #eee;text-align:right"><b>{f'{total_in_db:,}' if total_in_db else '—'}</b></td></tr>
                <tr><td style="padding:8px 16px;border-top:1px solid #eee">Crawling (description_full)</td>
                    <td style="padding:8px 16px;border-top:1px solid #eee;text-align:right"><b>{crawl_success:,}/{crawl_total:,} ({crawl_rate:.1f}%)</b></td></tr>
                <tr><td style="padding:8px 16px;border-top:1px solid #eee">Datos Eurostat actualizados</td>
                    <td style="padding:8px 16px;border-top:1px solid #eee;text-align:right"><b>{'Sí ✓' if eurostat_loaded else 'No'}</b></td></tr>
                <tr><td style="padding:8px 16px;border-top:1px solid #eee">Duración total</td>
                    <td style="padding:8px 16px;border-top:1px solid #eee;text-align:right"><b>{duration:.1f} min</b></td></tr>
            </table>

            <!-- Distribución por país -->
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-top:16px">
                <tr style="background:#e8f5e9">
                    <td style="padding:10px 16px;font-weight:bold;color:#2e7d32" colspan="2">🌍 Ofertas por país (esta carga)</td>
                </tr>
                {country_rows}
            </table>

            <!-- Próximo paso -->
            <div style="margin-top:16px;padding:12px 16px;background:#fff8e1;border-left:4px solid #f9a825;border-radius:4px;font-size:13px">
                <b>⏭ Siguiente paso manual:</b><br>
                <code>py -3.12 -m scripts.retro_classify --days 7</code><br>
                <span style="color:#555">Enriquece todas las ofertas nuevas con Ollama (role_category + skills).</span>
            </div>

            {actions_link}

            <p style="color:#888;font-size:11px;margin-top:24px">
                Generado automáticamente por TechRadar · GitHub Actions
            </p>
        </div>
    </body>
    </html>
    """

    return subject, html_body


# =============================================================================
# Envío del email
# =============================================================================


def send_email(subject: str, html_body: str) -> None:
    """
    Envía el email HTML usando Gmail SMTP con App Password.

    Args:
        subject:   Asunto del email.
        html_body: Cuerpo HTML completo.

    Raises:
        RuntimeError: si GMAIL_USER o GMAIL_APP_PASSWORD no están configurados.
    """
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_USER y GMAIL_APP_PASSWORD son obligatorios para enviar el email. "
            "Configúralos en GitHub Actions Secrets."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info("Enviando email a %s...", RECIPIENT)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT, msg.as_bytes())

    logger.info("Email enviado correctamente.")


# =============================================================================
# Punto de entrada
# =============================================================================


def run() -> None:
    """Función principal: lee el resumen, consulta la BD y envía el email."""
    summary  = _load_summary()
    db_stats = _fetch_db_stats(summary)
    subject, html_body = _build_email_html(summary, db_stats)

    try:
        send_email(subject, html_body)
    except RuntimeError as exc:
        # Credenciales no configuradas — avisar pero no abortar el workflow
        logger.error("No se pudo enviar el email: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    run()
