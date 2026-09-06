from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.database import Base
from app.main import app
from app import models  # noqa: F401


def test_health_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_about_is_public_default_page() -> None:
    with TestClient(app) as client:
        index = client.get("/", follow_redirects=False)
        about = client.get("/about")

    assert index.status_code == 303
    assert index.headers["location"] == "/about"
    assert about.status_code == 200
    assert "Сохраните воспоминания на карте" in about.text
    assert 'href="/register"' in about.text


def test_database_contains_stage_one_tables(tmp_path: Path) -> None:
    test_engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(test_engine)

    assert set(inspect(test_engine).get_table_names()) == {
        "auth_sessions",
        "geocode_cache",
        "memories",
        "users",
        "visited_countries",
        "wishlist_countries",
    }
    assert "location_name" in {
        column["name"] for column in inspect(test_engine).get_columns("memories")
    }
