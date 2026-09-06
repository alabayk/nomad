from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuthSession, User

PBKDF2_ITERATIONS = 600_000
CSRF_MAX_AGE_SECONDS = 2 * 60 * 60
_CSRF_SIGNING_KEY = secrets.token_bytes(32)


def normalize_username(username: str) -> str:
    return username.strip().casefold()


def validate_username(username: str) -> str | None:
    if not 3 <= len(username) <= 64:
        return "Логин должен содержать от 3 до 64 символов."
    if not all(character.isalnum() or character in "_-" for character in username):
        return "Используйте в логине только буквы, цифры, дефис и подчёркивание."
    return None


def validate_password(password: str) -> str | None:
    if len(password) < 8:
        return "Пароль должен содержать не менее 8 символов."
    if len(password) > 128:
        return "Пароль не должен быть длиннее 128 символов."
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PBKDF2_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session(db: Session, user: User, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            created_at=now,
            expires_at=now + timedelta(days=settings.session_days),
        )
    )
    db.commit()
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        path="/",
    )


def delete_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        auth_session = db.scalar(
            select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
        )
        if auth_session:
            db.delete(auth_session)
            db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")


def current_user(db: Session, request: Request) -> User | None:
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        return None

    auth_session = db.scalar(
        select(AuthSession).where(AuthSession.token_hash == _token_hash(token))
    )
    if auth_session is None:
        return None

    expires_at = auth_session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        db.delete(auth_session)
        db.commit()
        return None
    return auth_session.user


def new_csrf_token() -> str:
    payload = f"{int(time.time())}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(_CSRF_SIGNING_KEY, payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def csrf_token_for_request(request: Request) -> str:
    """Reuse the browser token so another open form does not become stale."""
    cookie_token = request.cookies.get(settings.csrf_cookie_name, "")
    return cookie_token if _valid_signed_csrf_token(cookie_token) else new_csrf_token()


def set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        max_age=60 * 60,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="strict",
        path="/",
    )


def _valid_signed_csrf_token(token: str) -> bool:
    try:
        timestamp_text, nonce, supplied_signature = token.split(".", 2)
        timestamp = int(timestamp_text)
        payload = f"{timestamp_text}.{nonce}"
        expected_signature = hmac.new(
            _CSRF_SIGNING_KEY, payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
    except (AttributeError, TypeError, ValueError):
        return False
    age = int(time.time()) - timestamp
    return (
        0 <= age <= CSRF_MAX_AGE_SECONDS
        and bool(nonce)
        and hmac.compare_digest(supplied_signature, expected_signature)
    )


def valid_csrf_token(request: Request, form_token: str) -> bool:
    # The signed form token is self-contained. This remains secure even when a
    # mobile browser drops the auxiliary cookie between GET and POST.
    return _valid_signed_csrf_token(form_token)
