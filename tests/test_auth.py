from __future__ import annotations

import re
import importlib

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import User
from app.config import settings
from app.auth import new_oauth_state, verify_password

main_module = importlib.import_module("app.main")


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def test_static_assets_are_https_proxy_safe() -> None:
    with TestClient(app) as client:
        page = client.get("/about")
        assert 'href="/static/css/app.css' in page.text
        assert 'http://testserver/static/' not in page.text


def test_opening_another_auth_form_does_not_expire_the_first() -> None:
    with TestClient(app) as client:
        first_page = client.get("/login")
        first_token = csrf_from(first_page)
        second_page = client.get("/register")
        assert csrf_from(second_page) == first_token

        response = client.post(
            "/login",
            data={"username": "missing", "password": "wrong-password", "csrf_token": first_token},
        )
        assert response.status_code == 401
        assert "Форма устарела" not in response.text


def test_login_form_works_when_mobile_browser_drops_csrf_cookie() -> None:
    with TestClient(app) as client:
        page = client.get("/login")
        token = csrf_from(page)
        client.cookies.clear()
        response = client.post(
            "/login",
            data={"username": "missing", "password": "wrong-password", "csrf_token": token},
        )
        assert response.status_code == 401
        assert "Форма устарела" not in response.text


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
        assert response.headers["location"].startswith("/auth/complete?ticket=")
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
        assert '<html lang="en" data-theme="dark">' in updated_page.text
        assert "Language" in updated_page.text
        assert "My memories" in client.get("/dashboard").text
        assert "Account management" in client.get("/account").text
        assert "Visited countries" in client.get("/countries").text

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "settings_user"))
        assert user is not None
        assert user.language == "en"


def test_guest_can_open_settings_and_change_language() -> None:
    with TestClient(app) as client:
        page = client.get("/settings")
        assert page.status_code == 200
        assert "Войдите, чтобы управлять данными" in page.text
        saved = client.post(
            "/settings/preferences",
            data={"language": "en", "csrf_token": csrf_from(page)},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        assert '<html lang="en" data-theme="dark">' in client.get("/settings").text


def _google_profile(sub: str = "google-123", email: str = "traveller@example.com") -> dict:
    return {
        "sub": sub,
        "email": email,
        "email_verified": True,
        "name": "Google Traveller",
        "picture": "https://example.com/avatar.jpg",
    }


def _google_callback(client: TestClient, monkeypatch, mode: str, profile: dict, user_id: int = 0):
    async def fake_profile(code: str, redirect_uri: str) -> dict:
        assert code == "test-code"
        return profile

    monkeypatch.setattr(main_module, "fetch_google_profile", fake_profile)
    state = new_oauth_state(mode, user_id)
    client.cookies.set("nomad_google_state", state, path="/")
    return client.get(
        "/auth/google/callback",
        params={"code": "test-code", "state": state},
        follow_redirects=False,
    )


def test_google_registration_creates_passwordless_account_and_first_password(test_session_factory, monkeypatch) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        registered = _google_callback(client, monkeypatch, "register", _google_profile())
        assert registered.status_code == 303
        account = client.get("/account")
        assert "Google Traveller" in account.text
        assert "Не задан" in account.text
        assert "Подтвердить через Google" in account.text

        with test_session_factory() as db:
            user = db.scalar(select(User).where(User.google_sub == "google-123"))
            assert user is not None and user.password_enabled is False
            user_id = user.id

        confirmed = _google_callback(client, monkeypatch, "set_password", _google_profile(), user_id)
        assert confirmed.status_code == 303
        setup_page = client.get(confirmed.headers["location"])
        assert setup_page.status_code == 200
        ticket = re.search(r'name="ticket" value="([^"]+)"', setup_page.text).group(1)
        created = client.post(
            "/account/password/setup",
            data={
                "username": "google_traveller",
                "new_password": "new-google-password",
                "new_password_confirm": "new-google-password",
                "ticket": ticket,
                "csrf_token": csrf_from(setup_page),
            },
            follow_redirects=False,
        )
        assert created.status_code == 303

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.google_sub == "google-123"))
        assert user is not None and user.password_enabled is True
        assert verify_password("new-google-password", user.password_hash)


def test_google_link_refuses_identity_owned_by_another_account(test_session_factory, monkeypatch) -> None:
    with TestClient(app, base_url="https://testserver") as google_client:
        _google_callback(google_client, monkeypatch, "register", _google_profile())

    with TestClient(app, base_url="https://testserver") as local_client:
        page = local_client.get("/register")
        local_client.post("/register", data={
            "username": "local_owner", "full_name": "Local Owner",
            "password": "local-owner-password", "password_confirm": "local-owner-password",
            "csrf_token": csrf_from(page),
        })
        with test_session_factory() as db:
            local_user = db.scalar(select(User).where(User.username == "local_owner"))
            local_id = local_user.id
        conflict = _google_callback(local_client, monkeypatch, "link", _google_profile(), local_id)
        assert conflict.status_code == 409
        assert "другим профилем Nomad" in conflict.text


def test_google_cannot_be_disconnected_when_it_is_the_only_login(test_session_factory, monkeypatch) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        _google_callback(client, monkeypatch, "register", _google_profile())
        account = client.get("/account")
        response = client.post(
            "/account/google/disconnect",
            data={"current_password": "anything", "csrf_token": csrf_from(account)},
        )
        assert response.status_code == 400
        assert "Сначала добавьте вход по паролю" in response.text


def test_local_account_can_link_and_safely_disconnect_google(test_session_factory, monkeypatch) -> None:
    with TestClient(app, base_url="https://testserver") as client:
        page = client.get("/register")
        client.post("/register", data={
            "username": "link_owner", "full_name": "Link Owner",
            "password": "link-owner-password", "password_confirm": "link-owner-password",
            "csrf_token": csrf_from(page),
        })
        with test_session_factory() as db:
            user_id = db.scalar(select(User.id).where(User.username == "link_owner"))

        linked = _google_callback(client, monkeypatch, "link", _google_profile("link-sub", "link@example.com"), user_id)
        assert linked.status_code == 303
        account = client.get("/account")
        assert "link@example.com" in account.text

        wrong = client.post("/account/google/disconnect", data={
            "current_password": "wrong-password", "csrf_token": csrf_from(account),
        })
        assert wrong.status_code == 400

        account = client.get("/account")
        disconnected = client.post("/account/google/disconnect", data={
            "current_password": "link-owner-password", "csrf_token": csrf_from(account),
        }, follow_redirects=False)
        assert disconnected.status_code == 303

    with test_session_factory() as db:
        user = db.scalar(select(User).where(User.username == "link_owner"))
        assert user is not None and user.google_sub is None and user.password_enabled is True
