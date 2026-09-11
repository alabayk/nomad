from __future__ import annotations

import re
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import Memory, User


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _register(client: TestClient, username: str) -> None:
    page = client.get("/register")
    client.post("/register", data={"username": username, "full_name": username.title(), "password": "secure-test-password", "password_confirm": "secure-test-password", "csrf_token": _csrf(page)})


def test_admin_panel_is_invisible_to_other_users(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAMES", "owner")
    with TestClient(app) as client:
        _register(client, "ordinary")
        assert client.get("/admin").status_code == 404
        assert 'href="/admin"' not in client.get("/dashboard").text


def test_configured_admin_can_open_private_dashboard(monkeypatch, test_session_factory) -> None:
    monkeypatch.setenv("ADMIN_USERNAMES", "owner")
    with TestClient(app) as client:
        _register(client, "owner")
        with test_session_factory() as db:
            user_id = db.scalar(select(User.id).where(User.username == "owner"))
            db.add(Memory(user_id=user_id, place_name="Admin memory", location_name="Rome", latitude=1, longitude=2, visit_date=date(2026, 1, 1)))
            db.commit()
        page = client.get("/admin")
        assert page.status_code == 200
        assert "Управление" in page.text and "@owner" in page.text and "Admin memory" not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert 'href="/admin"' in page.text
