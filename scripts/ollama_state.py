"""
ollama_state.py — Tracking local en SQLite para Pipeline C (Ollama).

Registra qué ofertas ya fueron procesadas, junto con el hash del contenido
enviado a Ollama, para garantizar idempotencia entre tandas y sesiones.

Ruta por defecto: data/ollama_state/ollama_review_state.sqlite
(sobreescribible con --state-path en retro_classify.py)

Esta base de datos es LOCAL y nunca se sube a git (.gitignore incluye la carpeta).
"""

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger("techradar.ollama_state")

DEFAULT_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "ollama_state", "ollama_review_state.sqlite",
)

_DDL = """
CREATE TABLE IF NOT EXISTS ollama_reviews (
    job_id               INTEGER NOT NULL,
    input_hash           TEXT    NOT NULL,
    model                TEXT    NOT NULL,
    text_source          TEXT    NOT NULL,
    reviewed_at          TEXT    NOT NULL,
    status               TEXT    NOT NULL
                             CHECK (status IN ('processed', 'failed', 'skipped')),
    role_category_before TEXT,
    role_category_after  TEXT,
    skills_before_count  INTEGER DEFAULT 0,
    skills_added         INTEGER DEFAULT 0,
    error                TEXT,
    PRIMARY KEY (job_id, input_hash, model)
)
"""


def compute_input_hash(title: str, description: str, text_source: str, model: str) -> str:
    """
    Calcula un hash SHA-256 (16 chars hex) del contenido real enviado a Ollama.

    Incluye title, text_source, model y el texto de descripción para que el hash
    cambie automáticamente cuando Pipeline B añade description_full a una oferta
    que antes solo tenía description_short.

    Args:
        title: Título de la oferta.
        description: Texto seleccionado (full o short), ya truncado al límite.
        text_source: 'description_full', 'description_short' o 'empty'.
        model: Nombre del modelo Ollama (p.ej. 'qwen2.5:1.5b').

    Returns:
        Primeros 16 caracteres del hash SHA-256 en hexadecimal.
    """
    content = f"{title}\x00{text_source}\x00{model}\x00{description}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def open_state_db(path: str) -> sqlite3.Connection:
    """
    Abre (o crea) la base de datos SQLite de tracking.

    Crea los directorios intermedios si no existen. Activa WAL mode para mayor
    robustez ante interrupciones. Acepta ':memory:' para tests unitarios.

    Args:
        path: Ruta al archivo .sqlite o ':memory:'.

    Returns:
        Conexión SQLite con la tabla ollama_reviews ya creada.
    """
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_DDL)
    conn.commit()
    logger.debug("State DB abierta en: %s", path)
    return conn


def is_already_processed(
    conn: sqlite3.Connection,
    job_id: int,
    input_hash: str,
    model: str,
) -> bool:
    """
    Comprueba si una oferta fue procesada exitosamente con el mismo contenido.

    Un 'failed' o 'skipped' no bloquea el reprocesado. Un 'processed' con
    diferente hash tampoco: significa que el contenido cambió (p.ej. Pipeline B
    añadió description_full) y la oferta es elegible de nuevo.

    Args:
        conn: Conexión SQLite.
        job_id: ID de la oferta.
        input_hash: Hash del contenido enviado a Ollama.
        model: Modelo Ollama utilizado.

    Returns:
        True solo si existe status='processed' con el mismo triplete.
    """
    row = conn.execute(
        """
        SELECT 1 FROM ollama_reviews
        WHERE job_id = ? AND input_hash = ? AND model = ? AND status = 'processed'
        """,
        (job_id, input_hash, model),
    ).fetchone()
    return row is not None


def record_result(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    input_hash: str,
    model: str,
    text_source: str,
    status: str,
    role_category_before: str | None = None,
    role_category_after: str | None = None,
    skills_before_count: int = 0,
    skills_added: int = 0,
    error: str | None = None,
) -> None:
    """
    Inserta o reemplaza el resultado del procesamiento de una oferta.

    INSERT OR REPLACE permite reintentar ofertas fallidas sin violaciones de PK.

    Args:
        conn: Conexión SQLite.
        job_id: ID de la oferta.
        input_hash: Hash del contenido enviado a Ollama.
        model: Modelo Ollama utilizado.
        text_source: 'description_full', 'description_short' o 'empty'.
        status: 'processed', 'failed' o 'skipped'.
        role_category_before: Categoría antes de la clasificación.
        role_category_after: Categoría asignada (puede ser None si is_tech=False).
        skills_before_count: Skills que tenía la oferta antes.
        skills_added: Skills nuevas añadidas.
        error: Mensaje de error si status='failed'.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR REPLACE INTO ollama_reviews
            (job_id, input_hash, model, text_source, reviewed_at, status,
             role_category_before, role_category_after,
             skills_before_count, skills_added, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id, input_hash, model, text_source, now, status,
            role_category_before, role_category_after,
            skills_before_count, skills_added, error,
        ),
    )
    conn.commit()
