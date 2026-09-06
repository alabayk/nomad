from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    create_session,
    auth_ticket_user_id,
    csrf_token_for_request,
    current_user,
    delete_session,
    hash_password,
    normalize_username,
    new_auth_ticket,
    set_csrf_cookie,
    valid_csrf_token,
    validate_password,
    validate_username,
    verify_password,
)
from app.config import settings
from app.database import create_database_schema, get_db
from app.memories import router as memories_router
from app.models import Memory, User, VisitedCountry, WishlistCountry
from app.photos import PhotoError, delete_local_photos, save_uploads

APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_database_schema()
    yield


app = FastAPI(title=settings.app_name, version="0.3.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")
app.include_router(memories_router)


def placeholder_page(
    request: Request,
    db: Session,
    *,
    title: str,
    description: str,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="section_placeholder.html",
        context={
            "user": current_user(db, request),
            "title": title,
            "description": description,
        },
    )


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user(db, request)
    return templates.TemplateResponse(
        request=request,
        name="about.html",
        context={"user": user},
    )


def account_response(
    request: Request, user: User, *, notice: str = "", error: str = "", status_code: int = 200
) -> HTMLResponse:
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="account.html",
        context={"user": user, "csrf_token": csrf_token, "notice": notice, "error": error},
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    notices = {
        "profile": "Данные профиля сохранены.",
        "password": "Пароль изменён.",
    }
    return account_response(request, user, notice=notices.get(request.query_params.get("saved", ""), ""))


@app.post("/account/profile", response_class=HTMLResponse)
async def update_account_profile(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    csrf_token: str = Form(...),
    remove_photo: bool = Form(False),
    profile_photo: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    normalized = normalize_username(username)
    cleaned_name = " ".join(full_name.split())
    error = None if valid_csrf_token(request, csrf_token) else "Форма устарела. Отправьте её ещё раз."
    error = error or validate_username(normalized)
    if not error and not cleaned_name:
        error = "Введите имя."
    if not error and len(cleaned_name) > 100:
        error = "Имя не должно превышать 100 символов."
    if error:
        return account_response(request, user, error=error, status_code=400)

    user.username = normalized
    user.full_name = cleaned_name
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        user = current_user(db, request)
        return account_response(request, user, error="Этот логин уже занят.", status_code=409)

    old_photo = user.profile_photo_url
    saved_urls: list[str] = []
    try:
        if profile_photo and profile_photo.filename:
            saved_urls = await save_uploads(user.id, [profile_photo])
            user.profile_photo_url = saved_urls[0]
        elif remove_photo:
            user.profile_photo_url = None
        db.commit()
    except PhotoError as exc:
        db.rollback()
        delete_local_photos(user.id, saved_urls)
        user = current_user(db, request)
        return account_response(request, user, error=str(exc), status_code=400)
    if old_photo and old_photo != user.profile_photo_url:
        delete_local_photos(user.id, [old_photo])
    return RedirectResponse("/account?saved=profile", status_code=303)


@app.post("/account/password", response_class=HTMLResponse)
def update_account_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    new_password_confirm: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    error = None if valid_csrf_token(request, csrf_token) else "Форма устарела. Отправьте её ещё раз."
    if not error and not verify_password(current_password, user.password_hash):
        error = "Текущий пароль указан неверно."
    error = error or validate_password(new_password)
    if not error and new_password != new_password_confirm:
        error = "Новые пароли не совпадают."
    if error:
        return account_response(request, user, error=error, status_code=400)
    user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse("/account?saved=password", status_code=303)


@app.post("/account/photo/delete")
def delete_account_photo(request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not valid_csrf_token(request, csrf_token):
        return RedirectResponse("/account", status_code=303)
    old_photo = user.profile_photo_url
    user.profile_photo_url = None
    db.commit()
    if old_photo:
        delete_local_photos(user.id, [old_photo])
    return RedirectResponse("/account?saved=profile", status_code=303)


@app.post("/account/delete")
def delete_account(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not valid_csrf_token(request, csrf_token):
        return RedirectResponse("/account", status_code=303)
    photo_urls = ([user.profile_photo_url] if user.profile_photo_url else []) + [
        url for memory in user.memories for url in memory.photo_urls if url.startswith("/uploads/")
    ]
    user_id = user.id
    db.delete(user)
    db.commit()
    delete_local_photos(user_id, photo_urls)
    response = RedirectResponse("/about", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(settings.csrf_cookie_name, path="/")
    return response


def settings_response(request: Request, user: User | None, *, error: str = "") -> HTMLResponse:
    csrf_token = csrf_token_for_request(request)
    language = user.language if user else request.cookies.get("nomad_language", "ru")
    response = templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={"user": user, "language": language, "csrf_token": csrf_token, "error": error},
    )
    set_csrf_cookie(response, csrf_token)
    return response


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user(db, request)
    return settings_response(request, user)


@app.post("/settings/preferences")
def update_preferences(
    request: Request,
    language: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = current_user(db, request)
    if not valid_csrf_token(request, csrf_token) or language not in {"ru", "en"}:
        return settings_response(request, user, error="Не удалось сохранить настройки.")
    response = RedirectResponse("/settings", status_code=303)
    if user:
        user.language = language
        db.commit()
    else:
        response.set_cookie("nomad_language", language, max_age=31536000, samesite="lax", path="/")
    return response


@app.post("/settings/delete-data")
def delete_travel_data(
    request: Request,
    scope: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not valid_csrf_token(request, csrf_token) or scope not in {"visits", "wishlist", "all"}:
        return RedirectResponse("/settings", status_code=303)
    photo_urls: list[str] = []
    if scope in {"visits", "all"}:
        memories = list(db.scalars(select(Memory).where(Memory.user_id == user.id)))
        photo_urls = [url for memory in memories for url in memory.photo_urls if url.startswith("/uploads/")]
        for memory in memories:
            db.delete(memory)
        for country in list(db.scalars(select(VisitedCountry).where(VisitedCountry.user_id == user.id))):
            db.delete(country)
    if scope in {"wishlist", "all"}:
        for country in list(db.scalars(select(WishlistCountry).where(WishlistCountry.user_id == user.id))):
            db.delete(country)
    db.commit()
    delete_local_photos(user.id, photo_urls)
    return RedirectResponse(f"/settings?saved={scope}", status_code=303)


def auth_form(
    request: Request,
    template_name: str,
    *,
    error: str | None = None,
    username: str = "",
    full_name: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context={"error": error, "username": username, "full_name": full_name, "csrf_token": csrf_token},
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def auth_success_response(db: Session, user: User) -> RedirectResponse:
    response = RedirectResponse(f"/auth/complete?ticket={new_auth_ticket(user.id)}", status_code=303)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    create_session(db, user, response)
    return response


@app.get("/auth/complete", include_in_schema=False)
def complete_auth(request: Request, ticket: str, db: Session = Depends(get_db)) -> RedirectResponse:
    response = RedirectResponse("/dashboard", status_code=303)
    if current_user(db, request):
        return response
    user_id = auth_ticket_user_id(ticket)
    user = db.get(User, user_id) if user_id else None
    if user is None:
        return RedirectResponse("/login", status_code=303)
    create_session(db, user, response)
    return response


@app.get("/auth/csrf", include_in_schema=False)
def fresh_auth_csrf(request: Request) -> JSONResponse:
    token = csrf_token_for_request(request)
    response = JSONResponse({"csrf_token": token})
    response.headers["Cache-Control"] = "no-store, max-age=0"
    set_csrf_cookie(response, token)
    return response


@app.get("/", include_in_schema=False)
def index(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    return RedirectResponse("/about", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if current_user(db, request):
        return RedirectResponse("/dashboard", status_code=303)
    return auth_form(request, "register.html")


@app.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    csrf_token: str = Form(...),
    profile_photo: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    normalized = normalize_username(username)
    error = None if valid_csrf_token(request, csrf_token) else "Форма устарела. Пожалуйста, отправьте её ещё раз."
    error = error or validate_username(normalized) or validate_password(password)
    if not error and password != password_confirm:
        error = "Пароли не совпадают."
    cleaned_name = " ".join(full_name.split())
    if not error and not cleaned_name:
        error = "Введите имя."
    if not error and len(cleaned_name) > 100:
        error = "Имя не должно превышать 100 символов."
    if error:
        return auth_form(
            request, "register.html", error=error, username=normalized,
            full_name=cleaned_name, status_code=400
        )

    user = User(username=normalized, password_hash=hash_password(password), full_name=cleaned_name)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return auth_form(
            request,
            "register.html",
            error="Этот логин уже занят.",
            username=normalized,
            full_name=cleaned_name,
            status_code=409,
        )
    saved_profile_urls: list[str] = []
    try:
        if profile_photo and profile_photo.filename:
            saved_profile_urls = await save_uploads(user.id, [profile_photo])
            user.profile_photo_url = saved_profile_urls[0] if saved_profile_urls else None
        db.commit()
        db.refresh(user)
    except PhotoError as exc:
        db.rollback()
        delete_local_photos(user.id, saved_profile_urls)
        return auth_form(
            request, "register.html", error=str(exc), username=normalized,
            full_name=cleaned_name, status_code=400
        )

    return auth_success_response(db, user)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    if current_user(db, request):
        return RedirectResponse("/dashboard", status_code=303)
    return auth_form(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    normalized = normalize_username(username)
    if not valid_csrf_token(request, csrf_token):
        return auth_form(request, "login.html", error="Форма устарела. Пожалуйста, отправьте её ещё раз.", username=normalized, status_code=400)
    user = db.scalar(select(User).where(User.username == normalized))
    if user is None or not verify_password(password, user.password_hash):
        return auth_form(
            request,
            "login.html",
            error="Неверный логин или пароль.",
            username=normalized,
            status_code=401,
        )

    return auth_success_response(db, user)


@app.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not valid_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", status_code=303)
    response = RedirectResponse("/login", status_code=303)
    delete_session(db, request, response)
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
