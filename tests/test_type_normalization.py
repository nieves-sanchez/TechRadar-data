"""
Protege _clean() en load.py contra regresiones de tipos numpy/pandas.

Fallo ya ocurrido en producción (Jun-17):
  "can't adapt type 'numpy.int16'" — el campo year de Eurostat llegaba como
  np.int16 y psycopg2 no lo adaptaba automáticamente. El pipeline falló
  completamente y Eurostat no se cargó.

_clean() es el único punto de conversión de tipos antes del INSERT.
"""

import numpy as np
import pandas as pd

from scripts.load import _clean


def test_clean_np_int16():
    """np.int16 — el tipo exacto del fallo de producción (campo year de Eurostat)."""
    result = _clean(np.int16(2024))
    assert result == 2024
    assert type(result) is int


def test_clean_np_int64():
    """np.int64 — tipo de los IDs de Adzuna en el DataFrame."""
    result = _clean(np.int64(123_456_789))
    assert result == 123_456_789


def test_clean_pd_na():
    """pd.NA — tipo NA de pandas nullable integers y strings."""
    assert _clean(pd.NA) is None


def test_clean_pd_nat():
    """pd.NaT — tipo NA de pandas timestamps (columna posted_at ausente)."""
    assert _clean(pd.NaT) is None


def test_clean_float_nan():
    """float NaN — tipo NA de columnas float (salarios ausentes en la API)."""
    assert _clean(float("nan")) is None
