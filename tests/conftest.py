import pytest


@pytest.fixture
def raw_job_pl_base():
    return {
        "id": "5766275549",
        "title": "Data Engineer",
        "salary_min": None,
        "salary_max": None,
        "salary_is_predicted": "0",
        "redirect_url": "https://www.adzuna.pl/land/ad/5766275549",
        "created": "2026-06-10T08:00:00Z",
    }


@pytest.fixture
def raw_job_de_base():
    return {
        "id": "9900001234",
        "title": "Backend Engineer",
        "salary_min": 70_000,
        "salary_max": 90_000,
        "salary_is_predicted": "0",
        "redirect_url": "https://www.adzuna.de/land/ad/9900001234",
        "created": "2026-06-10T08:00:00Z",
    }
