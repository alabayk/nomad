from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import User
from app.config import settings
from app.auth import verify_password


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_register_logout_and_login(test_session_factory) -> None:
    with TestClient(app) as client:
        page = client.get("/register")
        response = client.post(
            "/register",
            data={
                "username": "Traveller",
                "full_name": "Ivan Traveller",
                "password": "correct horse battery staple",
                "password_confirm": "correct horse battery staple",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        assert client.get("/dashboard").status_code == 200

        dashboard = client.get("/dashboard")
        logout = client.post(
            "/logout",
            data={"csrf_token": csrf_from(dashboard)},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/login"

        page = client.get("/login")
        login = client.post(
            "/login",
            data={
                "username": "TRAVELLER",
                "password": "correct horse battery staple",
                "csrf_token": csrf_from(page),
            },
            follow_redirects=False,
        )
        assert login.status_code == 303

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "traveller"))
        assert user is not None
        assert user.full_name == "Ivan Traveller"
        assert user.initials == "IT"
        assert user.password_hash != "correct horse battery staple"


def test_rejects_bad_password_and_duplicate_username(test_session_factory) -> None:
    with TestClient(app) as client:
        page = client.get("/register")
        first = {
            "username": "nomad_user",
            "full_name": "Nomad User",
            "password": "long-enough-password",
            "password_confirm": "long-enough-password",
            "csrf_token": csrf_from(page),
        }
        assert client.post("/register", data=first).status_code == 200

        client.cookies.clear()
        page = client.get("/register")
        duplicate = first | {"username": "NOMAD_USER", "csrf_token": csrf_from(page)}
        response = client.post("/register", data=duplicate)
        assert response.status_code == 409

        page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "username": "nomad_user",
                "password": "wrong-password",
                "csrf_token": csrf_from(page),
            },
        )
        assert response.status_code == 401
        assert "Неверный логин или пароль" in response.text


def test_registration_accepts_optional_profile_photo(test_session_factory) -> None:
    with TestClient(app) as client:
        page = client.get("/register")
        response = client.post(
            "/register",
            data={
                "username": "photo_user",
                "full_name": "Photo User",
                "password": "long-enough-password",
                "password_confirm": "long-enough-password",
                "csrf_token": csrf_from(page),
            },
            files={"profile_photo": ("avatar.png", b"\x89PNG\r\n\x1a\nprofile", "image/png")},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "photo_user"))
        assert user is not None and user.profile_photo_url
        saved_path = settings.upload_dir / str(user.id) / user.profile_photo_url.rsplit("/", 1)[1]
        assert saved_path.exists()
        saved_path.unlink()


def test_account_profile_and_password_can_be_updated(test_session_factory) -> None:
    with TestClient(app) as client:
        page = client.get("/register")
        client.post(
            "/register",
            data={
                "username": "account_user", "full_name": "Old Name",
                "password": "original-password", "password_confirm": "original-password",
                "csrf_token": csrf_from(page),
            },
        )
        account = client.get("/account")
        assert account.status_code == 200
        assert "Аккаунт зарегистрирован" in account.text

        updated = client.post(
            "/account/profile",
            data={"username": "renamed_user", "full_name": "New Name", "csrf_token": csrf_from(account)},
            follow_redirects=False,
        )
        assert updated.status_code == 303

        account = client.get("/account")
        changed = client.post(
            "/account/password",
            data={
                "current_password": "original-password", "new_password": "replacement-password",
                "new_password_confirm": "replacement-password", "csrf_token": csrf_from(account),
            },
            follow_redirects=False,
        )
        assert changed.status_code == 303

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "renamed_user"))
        assert user is not None
        assert user.full_name == "New Name"
        assert verify_password("replacement-password", user.password_hash)


def test_user_preferences_are_saved_and_applied(test_session_factory) -> None:
    with TestClient(app) as client:
        page = client.get("/register")
        client.post(
            "/register",
            data={
                "username": "settings_user", "full_name": "Settings User",
                "password": "settings-password", "password_confirm": "settings-password",
                "csrf_token": csrf_from(page),
            },
        )
        settings_page = client.get("/settings")
        saved = client.post(
            "/settings/preferences",
            data={"language": "en", "theme": "light", "csrf_token": csrf_from(settings_page)},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        updated_page = client.get("/settings")
        assert '<html lang="en" data-theme="light">' in updated_page.text
        assert "Appearance" in updated_page.text

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "settings_user"))
        assert user is not None
        assert (user.language, user.theme) == ("en", "light")
