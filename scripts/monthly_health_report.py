"""
monthly_health_report.py — Informe mensual de salud de la base de datos TechRadar.

Se ejecuta el día 1 de cada mes via GitHub Actions (cron: 0 8 1 * *).
Consulta Supabase y envía un email detallado a nsanchezgarcia86@gmail.com con:

  - Uso de almacenamiento Supabase (% de los 500MB del plan gratuito)
  - Total de ofertas y crecimiento respecto al mes anterior
  - Distribución de role_category y % de NULLs (indicador de calidad Ollama)
  - Cobertura salarial por país (% de ofertas con salary_mid real)
  - Mediana de salario por país (para detectar anomalías como el bug PLN)
  - Skills promedio por oferta (indicador de calidad de extracción)
  - Ofertas activas vs inactivas

Variables de entorno requeridas:
  GMAIL_USER         — dirección Gmail desde la que se envía
  GMAIL_APP_PASSWORD — App Password de Gmail (16 caracteres, sin espacios)
  DATABASE_URL       — connection string de Supabase

Uso:
  python -m scripts.monthly_health_report
"""

import html
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("techradar.monthly_health")

GMAIL_USER         = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
DATABASE_URL       = os.getenv("DATABASE_URL", "")
RECIPIENT          = "nsanchezgarcia86@gmail.com"

# Capacidad máxima del plan gratuito de Supabase en bytes (500 MB)
SUPABASE_FREE_TIER_BYTES = 500 * 1024 * 1024

_COUNTRY_NAMES = {
    "de": "Alemania 🇩🇪",
    "fr": "Francia 🇫🇷",
    "es": "España 🇪🇸",
    "nl": "Países Bajos 🇳🇱",
    "pl": "Polonia 🇵🇱",
    "it": "Italia 🇮🇹",
    "at": "Austria 🇦🇹",
    "be": "Bélgica 🇧🇪",
}


# =============================================================================
# Consultas a Supabase
# =============================================================================


def _get_connection():
    """Abre y devuelve una conexión a la base de datos."""
    if not DATABASE_URL:
        raise EnvironmentError("DATABASE_URL no configurado en .env")
    return psycopg2.connect(DATABASE_URL)


def _fetch_health_stats() -> dict:
    """
    Ejecuta todas las consultas de salud contra Supabase.

    Returns:
        dict con todas las métricas del informe mensual.
    """
    conn = _get_connection()
    cur  = conn.cursor()
    stats = {}

    # ── 1. Total de ofertas y crecimiento mensual ─────────────────────────────
    cur.execute("SELECT COUNT(*) FROM jobs")
    stats["total_jobs"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM jobs
        WHERE  ingested_at >= NOW() - INTERVAL '30 days'
    """)
    stats["jobs_last_30d"] = cur.fetchone()[0]

    cur.execute("""
        SELECT COUNT(*) FROM jobs
        WHERE  ingested_at >= NOW() - INTERVAL '60 days'
          AND  ingested_at <  NOW() - INTERVAL '30 days'
    """)
    stats["jobs_prev_30d"] = cur.fetchone()[0]

    # ── 2. Ofertas activas vs inactivas ───────────────────────────────────────
    cur.execute("""
        SELECT
            SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS activas,
            SUM(CASE WHEN NOT is_active THEN 1 ELSE 0 END) AS inactivas
        FROM jobs
    """)
    row = cur.fetchone()
    stats["active_jobs"]   = row[0] or 0
    stats["inactive_jobs"] = row[1] or 0

    # ── 3. Distribución de role_category ─────────────────────────────────────
    cur.execute("""
        SELECT
            COALESCE(role_category, 'NULL/pendiente') AS category,
            COUNT(*) AS n
        FROM   jobs
        GROUP  BY category
        ORDER  BY n DESC
    """)
    stats["role_distribution"] = cur.fetchall()

    # ── 4. Cobertura salarial y mediana por país ──────────────────────────────
    cur.execute("""
        SELECT
            country_code,
            COUNT(*) AS total,
            COUNT(salary_mid)  FILTER (WHERE salary_is_predicted = FALSE) AS con_salario_real,
            ROUND(
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary_mid)
                FILTER (WHERE salary_mid IS NOT NULL AND salary_is_predicted = FALSE)::numeric
            ) AS mediana_eur
        FROM   jobs
        GROUP  BY country_code
        ORDER  BY country_code
    """)
    stats["salary_by_country"] = cur.fetchall()

    # ── 5. Skills promedio por oferta ─────────────────────────────────────────
    cur.execute("""
        SELECT
            COUNT(DISTINCT js.job_id)         AS ofertas_con_skills,
            COUNT(js.skill_id)                AS total_vinculos,
            COUNT(js.skill_id)::numeric
                / NULLIF(COUNT(DISTINCT js.job_id), 0) AS avg_skills
        FROM job_skills js
    """)
    row = cur.fetchone()
    stats["jobs_with_skills"] = row[0] or 0
    stats["total_skill_links"] = row[1] or 0
    stats["avg_skills_per_job"] = float(row[2]) if row[2] else 0.0

    # ── 6. Top 10 skills más demandadas ──────────────────────────────────────
    cur.execute("""
        SELECT s.name, COUNT(*) AS n
        FROM   job_skills js
        JOIN   skills s ON s.id = js.skill_id
        GROUP  BY s.name
        ORDER  BY n DESC
        LIMIT  10
    """)
    stats["top_skills"] = cur.fetchall()

    # ── 7. Estimación de uso de almacenamiento ────────────────────────────────
    # pg_total_relation_size incluye datos + índices + TOAST (text largo).
    # En Supabase (plan gratuito) este permiso puede no estar disponible;
    # si falla, continuamos sin el dato en lugar de abortar el informe.
    try:
        cur.execute("""
            SELECT SUM(pg_total_relation_size(schemaname || '.' || tablename))
            FROM   pg_tables
            WHERE  schemaname = 'public'
        """)
        row = cur.fetchone()
        stats["db_size_bytes"] = row[0] if row[0] else 0
    except Exception:
        stats["db_size_bytes"] = None
        logger.warning(
            "No se pudo obtener el tamaño de la BD (sin permisos o error). "
            "La sección de almacenamiento aparecerá como N/D."
        )

    conn.close()
    return stats


# =============================================================================
# Construcción del email HTML
# =============================================================================


def _build_email_html(stats: dict) -> tuple[str, str]:
    """
    Genera el asunto y el cuerpo HTML del informe mensual.

    Args:
        stats: Dict devuelto por _fetch_health_stats().

    Returns:
        (subject, html_body)
    """
    now        = datetime.now(timezone.utc)
    month_name = now.strftime("%B %Y")
    subject    = f"📊 TechRadar — Informe mensual de salud · {month_name}"

    # Crecimiento
    prev   = stats["jobs_prev_30d"]
    curr   = stats["jobs_last_30d"]
    growth = ((curr - prev) / prev * 100) if prev > 0 else 0.0
    growth_str = f"+{growth:.1f}%" if growth >= 0 else f"{growth:.1f}%"
    growth_color = "#2e7d32" if growth >= 0 else "#c62828"

    # Uso de almacenamiento (puede ser None si Supabase no tiene permisos)
    if stats["db_size_bytes"] is not None:
        db_mb  = stats["db_size_bytes"] / (1024 * 1024)
        db_pct = stats["db_size_bytes"] / SUPABASE_FREE_TIER_BYTES * 100
        storage_color = "#c62828" if db_pct > 80 else "#f57c00" if db_pct > 60 else "#2e7d32"
        storage_str = f"{db_mb:.1f} MB / 500 MB ({db_pct:.1f}%)"
    else:
        db_mb = db_pct = 0.0
        storage_color = "#546e7a"
        storage_str = "N/D (sin permisos para pg_total_relation_size)"

    # role_category — % de NULLs
    total_roles   = sum(r[1] for r in stats["role_distribution"])
    null_roles    = next((r[1] for r in stats["role_distribution"] if "NULL" in r[0]), 0)
    null_pct      = null_roles / total_roles * 100 if total_roles > 0 else 0
    null_color    = "#c62828" if null_pct > 20 else "#f57c00" if null_pct > 10 else "#2e7d32"

    # Filas de role_category
    role_rows = ""
    for category, count in stats["role_distribution"][:12]:
        pct = count / total_roles * 100 if total_roles > 0 else 0
        role_rows += (
            f"<tr><td style='padding:6px 12px;border-top:1px solid #eee'>{html.escape(str(category))}</td>"
            f"<td style='padding:6px 12px;border-top:1px solid #eee;text-align:right'>{count:,}</td>"
            f"<td style='padding:6px 12px;border-top:1px solid #eee;text-align:right'>{pct:.1f}%</td></tr>"
        )

    # Filas de salarios por país
    salary_rows = ""
    for row in stats["salary_by_country"]:
        code, total, con_salario, mediana = row
        name      = _COUNTRY_NAMES.get(code, code.upper())
        cobertura = con_salario / total * 100 if total > 0 else 0
        mediana_str = f"{mediana:,.0f} €" if mediana else "—"
        salary_rows += (
            f"<tr><td style='padding:6px 12px;border-top:1px solid #eee'>{name}</td>"
            f"<td style='padding:6px 12px;border-top:1px solid #eee;text-align:right'>{total:,}</td>"
            f"<td style='padding:6px 12px;border-top:1px solid #eee;text-align:right'>{cobertura:.0f}%</td>"
            f"<td style='padding:6px 12px;border-top:1px solid #eee;text-align:right'>{mediana_str}</td></tr>"
        )

    # Top 10 skills
    skill_rows = ""
    for skill_name, count in stats["top_skills"]:
        skill_rows += (
            f"<tr><td style='padding:5px 12px;border-top:1px solid #eee'>{html.escape(str(skill_name))}</td>"
            f"<td style='padding:5px 12px;border-top:1px solid #eee;text-align:right'>{count:,}</td></tr>"
        )

    html_body = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head><meta charset="UTF-8"></head>
    <body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;color:#212121">

        <!-- Cabecera -->
        <div style="background:#1565c0;color:#fff;padding:20px 24px;border-radius:8px 8px 0 0">
            <h2 style="margin:0;font-size:20px">📊 TechRadar — Informe mensual de salud</h2>
            <p style="margin:4px 0 0;opacity:.85;font-size:13px">{now.strftime("%d/%m/%Y %H:%M UTC")}</p>
        </div>

        <div style="background:#f5f5f5;padding:24px;border-radius:0 0 8px 8px">

            <!-- Métricas globales -->
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1)">
                <tr style="background:#e3f2fd">
                    <td colspan="2" style="padding:10px 16px;font-weight:bold;color:#1565c0">🗄️ Estado general de la base de datos</td>
                </tr>
                <tr>
                    <td style="padding:9px 16px;border-top:1px solid #eee">Total de ofertas en BD</td>
                    <td style="padding:9px 16px;border-top:1px solid #eee;text-align:right"><b>{stats["total_jobs"]:,}</b></td>
                </tr>
                <tr>
                    <td style="padding:9px 16px;border-top:1px solid #eee">Cargadas este mes (últimos 30 días)</td>
                    <td style="padding:9px 16px;border-top:1px solid #eee;text-align:right">
                        <b>{curr:,}</b>
                        <span style="color:{growth_color};margin-left:8px;font-size:12px">{growth_str}</span>
                    </td>
                </tr>
                <tr>
                    <td style="padding:9px 16px;border-top:1px solid #eee">Ofertas activas / inactivas</td>
                    <td style="padding:9px 16px;border-top:1px solid #eee;text-align:right">
                        <b>{stats["active_jobs"]:,}</b> / {stats["inactive_jobs"]:,}
                    </td>
                </tr>
                <tr>
                    <td style="padding:9px 16px;border-top:1px solid #eee">Almacenamiento usado</td>
                    <td style="padding:9px 16px;border-top:1px solid #eee;text-align:right">
                        <b style="color:{storage_color}">{storage_str}</b>
                    </td>
                </tr>
                <tr>
                    <td style="padding:9px 16px;border-top:1px solid #eee">Skills promedio por oferta</td>
                    <td style="padding:9px 16px;border-top:1px solid #eee;text-align:right">
                        <b>{stats["avg_skills_per_job"]:.1f}</b>
                        <span style="color:#888;font-size:12px"> ({stats["total_skill_links"]:,} vínculos totales)</span>
                    </td>
                </tr>
                <tr>
                    <td style="padding:9px 16px;border-top:1px solid #eee">
                        role_category = NULL/pendiente
                        <span style="color:#888;font-size:12px"> (indicador de calidad Ollama)</span>
                    </td>
                    <td style="padding:9px 16px;border-top:1px solid #eee;text-align:right">
                        <b style="color:{null_color}">{null_roles:,} ({null_pct:.1f}%)</b>
                    </td>
                </tr>
            </table>

            <!-- Distribución de roles -->
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-top:16px">
                <tr style="background:#f3e5f5">
                    <td style="padding:10px 16px;font-weight:bold;color:#6a1b9a">🎯 Distribución de role_category</td>
                    <td style="padding:10px 16px;font-weight:bold;color:#6a1b9a;text-align:right">Ofertas</td>
                    <td style="padding:10px 16px;font-weight:bold;color:#6a1b9a;text-align:right">%</td>
                </tr>
                {role_rows}
            </table>

            <!-- Salarios por país -->
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-top:16px">
                <tr style="background:#e8f5e9">
                    <td style="padding:10px 16px;font-weight:bold;color:#2e7d32">💶 Salarios por país</td>
                    <td style="padding:10px 16px;font-weight:bold;color:#2e7d32;text-align:right">Ofertas</td>
                    <td style="padding:10px 16px;font-weight:bold;color:#2e7d32;text-align:right">Cobertura</td>
                    <td style="padding:10px 16px;font-weight:bold;color:#2e7d32;text-align:right">Mediana EUR</td>
                </tr>
                {salary_rows}
            </table>

            <!-- Top 10 skills -->
            <table style="width:100%;border-collapse:collapse;background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.1);margin-top:16px">
                <tr style="background:#fff8e1">
                    <td style="padding:10px 16px;font-weight:bold;color:#f57c00">🔧 Top 10 skills más demandadas</td>
                    <td style="padding:10px 16px;font-weight:bold;color:#f57c00;text-align:right">Ofertas</td>
                </tr>
                {skill_rows}
            </table>

            <p style="color:#888;font-size:11px;margin-top:24px">
                Generado automáticamente por TechRadar · GitHub Actions (cron mensual día 1)
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
    """
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        raise RuntimeError(
            "GMAIL_USER y GMAIL_APP_PASSWORD son obligatorios. "
            "Configúralos en GitHub Actions Secrets."
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = RECIPIENT

    msg.attach(MIMEText(html_body, "html", "utf-8"))

    logger.info("Enviando informe mensual a %s...", RECIPIENT)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, RECIPIENT, msg.as_bytes())

    logger.info("Informe mensual enviado correctamente.")


# =============================================================================
# Punto de entrada
# =============================================================================


def run() -> None:
    """Función principal: consulta Supabase, construye y envía el informe."""
    logger.info("Generando informe mensual de salud de la BD...")

    stats = _fetch_health_stats()
    subject, html_body = _build_email_html(stats)
    send_email(subject, html_body)

    logger.info("Informe mensual completado.")


if __name__ == "__main__":
    run()
