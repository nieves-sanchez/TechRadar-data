"""
Protege la conversión PLN → EUR en _parse_job_record (extract.py).

Fallos ya ocurridos en producción:
- 5.693 registros con salarios en PLN sin convertir (corregidos retroactivamente Jun-17).
- Bug CR-01: el Caso 3 (B2B diario) solo se aplicaba cuando min==max,
  dejando rangos (min!=max) sin convertir.
"""

from scripts.extract import _parse_job_record

# Constantes del módulo — si cambian, los tests lo detectan
_PLN_TO_EUR = 0.2342
_WORKING_DAYS = 220


def test_pln_annual_range(raw_job_pl_base):
    """PLN anual con rango min!=max: ambos extremos convertidos × PLN_TO_EUR."""
    raw_job_pl_base["salary_min"] = 80_000
    raw_job_pl_base["salary_max"] = 120_000
    result = _parse_job_record(raw_job_pl_base, "pl")
    assert result["salary_min"] == round(80_000 * _PLN_TO_EUR)   # 18736
    assert result["salary_max"] == round(120_000 * _PLN_TO_EUR)  # 28104


def test_pln_b2b_daily_fixed(raw_job_pl_base):
    """B2B tarifa diaria fija (min==max, ref < 5000): anualizado × 220 × PLN_TO_EUR."""
    raw_job_pl_base["salary_min"] = 1_800
    raw_job_pl_base["salary_max"] = 1_800
    result = _parse_job_record(raw_job_pl_base, "pl")
    expected = round(1_800 * _WORKING_DAYS * _PLN_TO_EUR)  # 92743
    assert result["salary_min"] == expected
    assert result["salary_max"] == expected


def test_pln_b2b_daily_range(raw_job_pl_base):
    """B2B tarifa diaria con rango min!=max (ref < 5000): el bug CR-01.

    Antes del fix, la condición era `ref < 5000 AND salary_min == salary_max`,
    por lo que rangos como 1500-2000 PLN/día pasaban sin convertir.
    """
    raw_job_pl_base["salary_min"] = 1_500
    raw_job_pl_base["salary_max"] = 2_000
    result = _parse_job_record(raw_job_pl_base, "pl")
    assert result["salary_min"] == round(1_500 * _WORKING_DAYS * _PLN_TO_EUR)  # 77286
    assert result["salary_max"] == round(2_000 * _WORKING_DAYS * _PLN_TO_EUR)  # 103048


def test_pln_not_applied_to_non_pl(raw_job_de_base):
    """País no-PL: los salarios no se modifican."""
    result = _parse_job_record(raw_job_de_base, "de")
    assert result["salary_min"] == 70_000
    assert result["salary_max"] == 90_000


def test_pln_corrupt_value_nulled(raw_job_pl_base):
    """Valor por encima de 1.500.000 PLN: se considera corrupto y se nullea."""
    raw_job_pl_base["salary_min"] = 500_000
    raw_job_pl_base["salary_max"] = 1_600_000
    result = _parse_job_record(raw_job_pl_base, "pl")
    assert result["salary_min"] is None
    assert result["salary_max"] is None
