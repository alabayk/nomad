from __future__ import annotations

import json
import secrets
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import csrf_token_for_request, current_user, set_csrf_cookie, valid_csrf_token
from app.config import settings
from app.database import get_db
from app.geocoding import GeocodeResult, GeocodingError, geocode_place, reverse_geocode
from app.models import CountryShare, Memory, MemoryShare, User, VisitedCountry, WishlistCountry
from app.photos import (
    PhotoError,
    delete_local_photos,
    external_photo_urls,
    local_photo_urls,
    parse_photo_urls,
    save_uploads,
)

router = APIRouter()
APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=APP_DIR / "templates")


def _load_country_choices() -> list[tuple[str, str]]:
    with (APP_DIR / "static" / "data" / "countries.geojson").open(encoding="utf-8") as file:
        features = json.load(file)["features"]
    choices: dict[str, str] = {}
    for feature in features:
        properties = feature["properties"]
        code = properties.get("ISO_A2_EH") or properties.get("ISO_A2")
        if not isinstance(code, str) or len(code) != 2:
            continue
        name = properties.get("NAME_RU") or properties.get("ADMIN") or code
        choices[code.upper()] = name
    return sorted(choices.items(), key=lambda item: item[1])


COUNTRY_CHOICES = _load_country_choices()
COUNTRY_NAMES = dict(COUNTRY_CHOICES)
RU_MONTHS = ("", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь")
EN_MONTHS = ("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


def _month_label(value: date, en: bool) -> str:
    return f"{(EN_MONTHS if en else RU_MONTHS)[value.month]} {value.year}"


def _country_source_label(country: VisitedCountry, has_memories: bool, en: bool) -> str:
    if country.source == "manual" and has_memories:
        return "manual + memories" if en else "вручную + из воспоминаний"
    if country.source == "manual":
        return "added manually" if en else "добавлено вручную"
    return "from memories" if en else "из воспоминаний"


def _redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _memory_for_user(db: Session, memory_id: int, user_id: int) -> Memory | None:
    return db.scalar(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
    )


def _form_response(
    request: Request,
    *,
    user: User,
    title: str,
    action: str,
    memory: Memory | None = None,
    error: str | None = None,
    values: dict[str, object] | None = None,
    status_code: int = 200,
    country_hint: tuple[str, str] | None = None,
) -> HTMLResponse:
    if user.language == "en":
        title = "Edit memory" if memory else "New memory"
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="memory_form.html",
        context={
            "user": user,
            "title": title,
            "action": action,
            "memory": memory,
            "error": error,
            "values": values or {},
            "external_urls": "\n".join(
                external_photo_urls(memory.photo_urls) if memory else []
            ),
            "local_urls": local_photo_urls(memory.photo_urls) if memory else [],
            "csrf_token": csrf_token,
            "country_hint": country_hint,
        },
        status_code=status_code,
    )
    set_csrf_cookie(response, csrf_token)
    return response


def _validate_memory_fields(
    place_name: str, location_name: str, description: str
) -> str | None:
    if not place_name.strip():
        return "Введите название воспоминания."
    if len(place_name.strip()) > 200:
        return "Название воспоминания не должно превышать 200 символов."
    if not location_name.strip():
        return "Введите локацию или выберите точку на карте."
    if len(location_name.strip()) > 250:
        return "Название локации не должно превышать 250 символов."
    if len(description) > 5_000:
        return "Текст воспоминания не должен превышать 5000 символов."
    return None


def _parse_visit_month(value: str) -> date | None:
    """Convert an HTML month value to the first day of that month.

    Full ISO dates remain accepted for compatibility with older clients, but
    their day is deliberately discarded because Nomad stores month precision.
    """
    try:
        parsed = date.fromisoformat(value if len(value) == 10 else f"{value}-01")
    except ValueError:
        return None
    return parsed.replace(day=1)


def _manual_coordinates(
    *,
    location_name: str,
    resolved_location_name: str,
    latitude: str,
    longitude: str,
    country_code: str,
    country_name: str,
) -> GeocodeResult | None:
    if resolved_location_name.strip().casefold() != location_name.strip().casefold():
        return None
    if not latitude.strip() and not longitude.strip():
        return None
    try:
        parsed_latitude = float(latitude)
        parsed_longitude = float(longitude)
    except ValueError as exc:
        raise GeocodingError("Некорректные координаты выбранной точки.") from exc
    if not -90 <= parsed_latitude <= 90 or not -180 <= parsed_longitude <= 180:
        raise GeocodingError("Выбранная точка находится за пределами карты.")
    code = country_code.strip().upper()
    if len(code) != 2:
        code = ""
    return GeocodeResult(
        latitude=parsed_latitude,
        longitude=parsed_longitude,
        country_code=code or None,
        country_name=country_name.strip() or None,
        display_name=location_name.strip(),
    )


def _geocode_json(result: GeocodeResult) -> dict[str, object]:
    return {
        "ok": True,
        "latitude": result.latitude,
        "longitude": result.longitude,
        "country_code": result.country_code,
        "country_name": result.country_name,
        "display_name": result.display_name,
    }


@router.get("/api/geocode")
async def geocode_api(
    request: Request, query: str, db: Session = Depends(get_db)
) -> JSONResponse:
    if current_user(db, request) is None:
        return JSONResponse({"ok": False, "error": "Требуется вход."}, status_code=401)
    try:
        result = await geocode_place(db, query)
    except GeocodingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return JSONResponse(_geocode_json(result))


@router.get("/api/reverse-geocode")
async def reverse_geocode_api(
    request: Request,
    latitude: float,
    longitude: float,
    db: Session = Depends(get_db),
) -> JSONResponse:
    if current_user(db, request) is None:
        return JSONResponse({"ok": False, "error": "Требуется вход."}, status_code=401)
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return JSONResponse({"ok": False, "error": "Некорректные координаты."}, status_code=422)
    try:
        result = await reverse_geocode(latitude, longitude)
    except GeocodingError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return JSONResponse(_geocode_json(result))


def _ensure_visited_country(
    db: Session, user_id: int, code: str | None, name: str | None, source: str = "memory"
) -> None:
    if not code:
        return
    code = code.upper()
    country = db.scalar(
        select(VisitedCountry).where(
            VisitedCountry.user_id == user_id,
            VisitedCountry.country_code == code,
        )
    )
    if country:
        if name:
            country.country_name = name
        return
    db.add(
        VisitedCountry(
            user_id=user_id,
            country_code=code,
            country_name=name or COUNTRY_NAMES.get(code, code),
            source=source,
        )
    )


def _remove_orphan_memory_country(
    db: Session, user_id: int, code: str | None
) -> None:
    """Remove an automatically inferred country after its last memory disappears."""
    if not code:
        return
    normalized_code = code.upper()
    has_memory = db.scalar(
        select(Memory.id).where(
            Memory.user_id == user_id,
            Memory.country_code == normalized_code,
        ).limit(1)
    )
    if has_memory:
        return
    country = db.scalar(
        select(VisitedCountry).where(
            VisitedCountry.user_id == user_id,
            VisitedCountry.country_code == normalized_code,
            VisitedCountry.source == "memory",
        )
    )
    if country:
        db.delete(country)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    year: str | None = None,
    country: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()

    all_memories = list(
        db.scalars(
            select(Memory)
            .where(Memory.user_id == user.id)
            .order_by(Memory.visit_date.desc(), Memory.id.desc())
        )
    )
    known_country_codes = set(
        db.scalars(
            select(VisitedCountry.country_code).where(VisitedCountry.user_id == user.id)
        )
    )
    missing_countries = {
        (item.country_code, item.country_name)
        for item in all_memories
        if item.country_code and item.country_code not in known_country_codes
    }
    for code, name in missing_countries:
        _ensure_visited_country(db, user.id, code, name)
    if missing_countries:
        db.commit()
    try:
        selected_year = int(year) if year and year.strip() else None
    except ValueError:
        selected_year = None
    selected_country = country.strip().upper() if country and country.strip() else ""

    years = sorted(
        {
            item.visit_date.year
            for item in all_memories
            if not selected_country or item.country_code == selected_country
        },
        reverse=True,
    )
    countries = sorted(
        {
            (item.country_code, item.country_name)
            for item in all_memories
            if item.country_code
            and (selected_year is None or item.visit_date.year == selected_year)
        },
        key=lambda item: item[1] or item[0],
    )
    memories = [
        item
        for item in all_memories
        if (selected_year is None or item.visit_date.year == selected_year)
        and (not selected_country or item.country_code == selected_country)
    ]
    filter_pairs = [
        {
            "year": item.visit_date.year,
            "code": item.country_code,
            "name": item.country_name or item.country_code,
        }
        for item in all_memories
        if item.country_code
    ]
    map_memories = [
        {
            "id": item.id,
            "place_name": item.place_name,
            "latitude": item.latitude,
            "longitude": item.longitude,
            "visit_date": item.visit_date.strftime("%Y-%m"),
            "photo": item.photo_urls[0] if item.photo_urls else None,
        }
        for item in memories
    ]
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "memories": memories,
            "map_memories": map_memories,
            "years": years,
            "countries": countries,
            "filter_pairs": filter_pairs,
            "selected_year": selected_year,
            "selected_country": selected_country,
            "csrf_token": csrf_token,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/countries/{country_code}/share")
def create_country_share(country_code: str, request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    user = current_user(db, request); code = country_code.upper()
    country = db.scalar(select(VisitedCountry).where(VisitedCountry.user_id == user.id, VisitedCountry.country_code == code)) if user else None
    if user and country and valid_csrf_token(request, csrf_token) and db.scalar(select(CountryShare).where(CountryShare.user_id == user.id, CountryShare.country_code == code)) is None:
        db.add(CountryShare(user_id=user.id, country_code=code, token=secrets.token_urlsafe(32))); db.commit()
    return RedirectResponse(f"/countries/{code}", status_code=303)


@router.post("/countries/{country_code}/share/delete")
def delete_country_share(country_code: str, request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    user = current_user(db, request); code = country_code.upper()
    share = db.scalar(select(CountryShare).where(CountryShare.user_id == user.id, CountryShare.country_code == code)) if user else None
    if share and valid_csrf_token(request, csrf_token): db.delete(share); db.commit()
    return RedirectResponse(f"/countries/{code}", status_code=303)


@router.get("/shared/country/{token}", response_class=HTMLResponse)
def shared_country(token: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    share = db.scalar(select(CountryShare).where(CountryShare.token == token))
    owner = db.get(User, share.user_id) if share else None
    if not owner: return templates.TemplateResponse(request=request, name="not_found.html", context={"user": None}, status_code=404)
    memories = list(db.scalars(select(Memory).where(Memory.user_id == owner.id, Memory.country_code == share.country_code).order_by(Memory.visit_date.desc())))
    country = db.scalar(select(VisitedCountry).where(VisitedCountry.user_id == owner.id, VisitedCountry.country_code == share.country_code))
    photos = [{"url": url, "name": item.place_name} for item in memories for url in item.photo_urls]
    return templates.TemplateResponse(request=request, name="shared_country.html", context={"user": None, "owner": owner, "country": country, "memories": memories, "photos": photos})


@router.get("/timeline", response_class=HTMLResponse)
def travel_timeline(
    request: Request,
    year: str | None = None,
    country: str | None = None,
    order: str = "newest",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    all_memories = list(db.scalars(
        select(Memory).where(Memory.user_id == user.id)
        .order_by(Memory.visit_date.asc(), Memory.id.asc())
    ))
    try:
        selected_year = int(year) if year and year.strip() else None
    except ValueError:
        selected_year = None
    selected_country = country.strip().upper() if country and country.strip() else ""
    selected_order = "oldest" if order == "oldest" else "newest"
    years = sorted({
        item.visit_date.year for item in all_memories
        if not selected_country or item.country_code == selected_country
    }, reverse=True)
    countries = sorted({
        (item.country_code, item.country_name or item.country_code)
        for item in all_memories if item.country_code
        and (selected_year is None or item.visit_date.year == selected_year)
    }, key=lambda item: item[1])
    first_country_memory_ids: set[int] = set()
    seen_countries: set[str] = set()
    for item in all_memories:
        if item.country_code and item.country_code not in seen_countries:
            seen_countries.add(item.country_code)
            first_country_memory_ids.add(item.id)
    filtered = [
        item for item in all_memories
        if (selected_year is None or item.visit_date.year == selected_year)
        and (not selected_country or item.country_code == selected_country)
    ]
    filtered.sort(
        key=lambda item: (item.visit_date, item.id),
        reverse=selected_order == "newest",
    )
    en = user.language == "en"
    groups: list[dict[str, object]] = []
    for item in filtered:
        key = (item.visit_date.year, item.visit_date.month)
        if not groups or groups[-1]["key"] != key:
            groups.append({
                "key": key,
                "year": item.visit_date.year,
                "month": (EN_MONTHS if en else RU_MONTHS)[item.visit_date.month],
                "items": [],
            })
        groups[-1]["items"].append({
            "memory": item,
            "first_country_visit": item.id in first_country_memory_ids,
        })
    return templates.TemplateResponse(
        request=request,
        name="timeline.html",
        context={
            "user": user,
            "groups": groups,
            "memory_count": len(filtered),
            "years": years,
            "countries": countries,
            "all_years": years,
            "selected_year": selected_year,
            "selected_country": selected_country,
            "selected_order": selected_order,
            "filter_pairs": [
                {"year": item.visit_date.year, "code": item.country_code, "name": item.country_name or item.country_code}
                for item in all_memories if item.country_code
            ],
        },
    )


@router.get("/memories/new", response_class=HTMLResponse)
def new_memory_page(request: Request, country: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    code = country.strip().upper()
    visited = db.scalar(select(VisitedCountry).where(
        VisitedCountry.user_id == user.id, VisitedCountry.country_code == code
    )) if code in COUNTRY_NAMES else None
    return _form_response(
        request,
        user=user,
        title="Новое воспоминание",
        action="/memories",
        country_hint=(visited.country_code, visited.country_name) if visited else None,
    )


@router.post("/memories", response_class=HTMLResponse)
async def create_memory(
    request: Request,
    place_name: str = Form(...),
    location_name: str = Form(...),
    visit_date: str = Form(...),
    description: str = Form(""),
    photo_urls: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    country_code: str = Form(""),
    country_name: str = Form(""),
    resolved_location_name: str = Form(""),
    csrf_token: str = Form(...),
    photos: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    values = {
        "place_name": place_name,
        "location_name": location_name,
        "visit_date": visit_date,
        "description": description,
        "photo_urls": photo_urls,
        "latitude": latitude,
        "longitude": longitude,
        "country_code": country_code,
        "country_name": country_name,
        "resolved_location_name": resolved_location_name,
    }
    error = None if valid_csrf_token(request, csrf_token) else "Форма устарела. Отправьте её ещё раз."
    error = error or _validate_memory_fields(place_name, location_name, description)
    parsed_visit_date = _parse_visit_month(visit_date)
    error = error or (None if parsed_visit_date else "Выберите месяц и год посещения.")
    if error:
        return _form_response(
            request, user=user, title="Новое воспоминание", action="/memories",
            error=error, values=values, status_code=400,
        )

    saved_urls: list[str] = []
    try:
        links = parse_photo_urls(photo_urls)
        coordinates = _manual_coordinates(
            location_name=location_name,
            resolved_location_name=resolved_location_name,
            latitude=latitude,
            longitude=longitude,
            country_code=country_code,
            country_name=country_name,
        )
        if coordinates is None:
            coordinates = await geocode_place(db, location_name)
        saved_urls = await save_uploads(user.id, photos)
        combined_urls = links + saved_urls
        if len(combined_urls) > settings.max_photos_per_memory:
            raise PhotoError("Можно добавить не более 10 фотографий.")
    except (PhotoError, GeocodingError) as exc:
        delete_local_photos(user.id, saved_urls)
        return _form_response(
            request, user=user, title="Новое воспоминание", action="/memories",
            error=f"{exc} Выберите нужную точку вручную на карте.", values=values, status_code=400,
        )

    memory = Memory(
        user_id=user.id,
        place_name=place_name.strip(),
        location_name=location_name.strip(),
        visit_date=parsed_visit_date,
        description=description.strip(),
        latitude=coordinates.latitude,
        longitude=coordinates.longitude,
        country_code=coordinates.country_code,
        country_name=coordinates.country_name,
        photo_urls=combined_urls,
    )
    db.add(memory)
    _ensure_visited_country(
        db, user.id, coordinates.country_code, coordinates.country_name
    )
    db.commit()
    db.refresh(memory)
    return RedirectResponse(f"/memories/{memory.id}", status_code=303)


@router.get("/memories/{memory_id}", response_class=HTMLResponse)
def memory_detail(
    memory_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    memory = _memory_for_user(db, memory_id, user.id)
    if memory is None:
        return templates.TemplateResponse(
            request=request, name="not_found.html", context={"user": user}, status_code=404
        )
    csrf_token = csrf_token_for_request(request)
    share = db.scalar(
        select(MemoryShare).where(
            MemoryShare.memory_id == memory.id, MemoryShare.user_id == user.id
        )
    )
    response = templates.TemplateResponse(
        request=request,
        name="memory_detail.html",
        context={"user": user, "memory": memory, "share": share, "csrf_token": csrf_token},
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/memories/{memory_id}/share")
def create_memory_share(memory_id: int, request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    memory = _memory_for_user(db, memory_id, user.id)
    if memory is None or not valid_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", status_code=303)
    if db.scalar(select(MemoryShare).where(MemoryShare.memory_id == memory.id)) is None:
        db.add(MemoryShare(memory_id=memory.id, user_id=user.id, token=secrets.token_urlsafe(32)))
        db.commit()
    return RedirectResponse(f"/memories/{memory.id}?shared=1", status_code=303)


@router.post("/memories/{memory_id}/share/delete")
def delete_memory_share(memory_id: int, request: Request, csrf_token: str = Form(...), db: Session = Depends(get_db)) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    share = db.scalar(select(MemoryShare).where(MemoryShare.memory_id == memory_id, MemoryShare.user_id == user.id))
    if share is not None and valid_csrf_token(request, csrf_token):
        db.delete(share)
        db.commit()
    return RedirectResponse(f"/memories/{memory_id}", status_code=303)


@router.get("/shared/{token}", response_class=HTMLResponse)
def shared_memory(token: str, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    share = db.scalar(select(MemoryShare).where(MemoryShare.token == token))
    memory = db.get(Memory, share.memory_id) if share else None
    if memory is None:
        return templates.TemplateResponse(request=request, name="not_found.html", context={"user": None}, status_code=404)
    return templates.TemplateResponse(request=request, name="shared_memory.html", context={"user": None, "memory": memory, "owner": db.get(User, share.user_id)})


@router.get("/memories/{memory_id}/edit", response_class=HTMLResponse)
def edit_memory_page(
    memory_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    memory = _memory_for_user(db, memory_id, user.id)
    if memory is None:
        return templates.TemplateResponse(
            request=request, name="not_found.html", context={"user": user}, status_code=404
        )
    return _form_response(
        request,
        user=user,
        title="Редактировать воспоминание",
        action=f"/memories/{memory.id}/edit",
        memory=memory,
    )


@router.post("/memories/{memory_id}/edit", response_class=HTMLResponse)
async def edit_memory(
    memory_id: int,
    request: Request,
    place_name: str = Form(...),
    location_name: str = Form(...),
    visit_date: str = Form(...),
    description: str = Form(""),
    photo_urls: str = Form(""),
    latitude: str = Form(""),
    longitude: str = Form(""),
    country_code: str = Form(""),
    country_name: str = Form(""),
    resolved_location_name: str = Form(""),
    csrf_token: str = Form(...),
    remove_photo: list[str] = Form(default=[]),
    photos: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    memory = _memory_for_user(db, memory_id, user.id)
    if memory is None:
        return templates.TemplateResponse(
            request=request, name="not_found.html", context={"user": user}, status_code=404
        )
    values = {
        "place_name": place_name,
        "location_name": location_name,
        "visit_date": visit_date,
        "description": description,
        "photo_urls": photo_urls,
        "latitude": latitude,
        "longitude": longitude,
        "country_code": country_code,
        "country_name": country_name,
        "resolved_location_name": resolved_location_name,
    }
    error = None if valid_csrf_token(request, csrf_token) else "Форма устарела. Отправьте её ещё раз."
    error = error or _validate_memory_fields(place_name, location_name, description)
    parsed_visit_date = _parse_visit_month(visit_date)
    error = error or (None if parsed_visit_date else "Выберите месяц и год посещения.")
    if error:
        return _form_response(
            request, user=user, title="Редактировать воспоминание",
            action=f"/memories/{memory.id}/edit", memory=memory, error=error,
            values=values, status_code=400,
        )

    saved_urls: list[str] = []
    try:
        links = parse_photo_urls(photo_urls)
        coordinates = _manual_coordinates(
            location_name=location_name,
            resolved_location_name=resolved_location_name,
            latitude=latitude,
            longitude=longitude,
            country_code=country_code,
            country_name=country_name,
        )
        if coordinates is None:
            coordinates = await geocode_place(db, location_name)
        existing_local = local_photo_urls(memory.photo_urls)
        approved_removals = [url for url in remove_photo if url in existing_local]
        kept_local = [url for url in existing_local if url not in approved_removals]
        saved_urls = await save_uploads(user.id, photos)
        combined_urls = links + kept_local + saved_urls
        if len(combined_urls) > settings.max_photos_per_memory:
            raise PhotoError("Можно добавить не более 10 фотографий.")
    except (PhotoError, GeocodingError) as exc:
        delete_local_photos(user.id, saved_urls)
        return _form_response(
            request, user=user, title="Редактировать воспоминание",
            action=f"/memories/{memory.id}/edit", memory=memory,
            error=f"{exc} Выберите нужную точку вручную на карте.",
            values=values, status_code=400,
        )

    previous_country_code = memory.country_code
    memory.place_name = place_name.strip()
    memory.location_name = location_name.strip()
    memory.visit_date = parsed_visit_date
    memory.description = description.strip()
    memory.photo_urls = combined_urls
    memory.latitude = coordinates.latitude
    memory.longitude = coordinates.longitude
    memory.country_code = coordinates.country_code
    memory.country_name = coordinates.country_name
    _ensure_visited_country(
        db, user.id, coordinates.country_code, coordinates.country_name
    )
    db.flush()
    if previous_country_code != coordinates.country_code:
        _remove_orphan_memory_country(db, user.id, previous_country_code)
    db.commit()
    delete_local_photos(user.id, approved_removals)
    return RedirectResponse(f"/memories/{memory.id}", status_code=303)


@router.post("/memories/{memory_id}/delete")
def delete_memory(
    memory_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    memory = _memory_for_user(db, memory_id, user.id)
    if memory is None or not valid_csrf_token(request, csrf_token):
        return RedirectResponse("/dashboard", status_code=303)
    local_urls = local_photo_urls(memory.photo_urls)
    previous_country_code = memory.country_code
    db.delete(memory)
    db.flush()
    _remove_orphan_memory_country(db, user.id, previous_country_code)
    db.commit()
    delete_local_photos(user.id, local_urls)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/countries", response_class=HTMLResponse)
def visited_countries_page(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    en = user.language == "en"
    countries = list(
        db.scalars(
            select(VisitedCountry)
            .where(VisitedCountry.user_id == user.id)
            .order_by(VisitedCountry.country_name)
        )
    )
    memory_country_codes = set(
        db.scalars(
            select(Memory.country_code)
            .where(Memory.user_id == user.id, Memory.country_code.is_not(None))
            .distinct()
        )
    )
    visited_codes = [country.country_code for country in countries]
    available_countries = [
        (code, name) for code, name in COUNTRY_CHOICES if code not in visited_codes
    ]
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="countries.html",
        context={
            "user": user,
            "countries": countries,
            "visited_codes": visited_codes,
            "memory_country_codes": memory_country_codes,
            "available_countries": available_countries,
            "page_mode": "visited",
            "page_title": "Visited countries" if en else "Посещённые страны",
            "page_eyebrow": "Travel map" if en else "Карта путешествий",
            "page_description": ("Countries from memories are added automatically. You can mark others manually." if en else "Страны из воспоминаний добавляются автоматически. Другие можно отметить вручную."),
            "form_action": "/countries",
            "delete_prefix": "/countries",
            "country_items": [
                {
                    "code": item.country_code,
                    "name": item.country_name,
                    "source": _country_source_label(item, item.country_code in memory_country_codes, en),
                    "deletable": item.source == "manual" or item.country_code not in memory_country_codes,
                    "detail_url": f"/countries/{item.country_code}",
                }
                for item in countries
            ],
            "csrf_token": csrf_token,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.get("/countries/{country_code}", response_class=HTMLResponse)
def country_detail_page(
    country_code: str, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    code = country_code.strip().upper()
    if code not in COUNTRY_NAMES:
        return templates.TemplateResponse(
            request=request, name="country_not_found.html", context={"user": user}, status_code=404
        )
    memories = list(db.scalars(
        select(Memory).where(Memory.user_id == user.id, Memory.country_code == code)
        .order_by(Memory.visit_date.desc(), Memory.id.desc())
    ))
    country = db.scalar(select(VisitedCountry).where(
        VisitedCountry.user_id == user.id, VisitedCountry.country_code == code
    ))
    if country is None and memories:
        country = VisitedCountry(
            user_id=user.id, country_code=code,
            country_name=memories[0].country_name or COUNTRY_NAMES[code], source="memory"
        )
        db.add(country)
        db.commit()
        db.refresh(country)
    if country is None:
        return templates.TemplateResponse(
            request=request, name="country_not_found.html", context={"user": user}, status_code=404
        )
    en = user.language == "en"
    unique_places = len({(item.location_name or item.place_name).strip().casefold() for item in memories})
    photo_items = [
        {"url": url, "memory_id": item.id, "place_name": item.place_name}
        for item in memories for url in item.photo_urls
    ]
    map_memories = [
        {"id": item.id, "name": item.place_name, "longitude": item.longitude, "latitude": item.latitude}
        for item in memories
    ]
    csrf_token = csrf_token_for_request(request)
    country_share = db.scalar(select(CountryShare).where(
        CountryShare.user_id == user.id, CountryShare.country_code == code
    ))
    response = templates.TemplateResponse(
        request=request,
        name="country_detail.html",
        context={
            "user": user,
            "country": country,
            "memories": memories,
            "memory_count": len(memories),
            "unique_places": unique_places,
            "first_visit": _month_label(memories[-1].visit_date, en) if memories else None,
            "last_visit": _month_label(memories[0].visit_date, en) if memories else None,
            "timeline": [{"memory": item, "date_label": _month_label(item.visit_date, en)} for item in memories],
            "photo_items": photo_items,
            "map_memories": map_memories,
            "source_label": _country_source_label(country, bool(memories), en),
            "is_wishlist": db.scalar(select(WishlistCountry.id).where(
                WishlistCountry.user_id == user.id, WishlistCountry.country_code == code
            )) is not None,
            "can_remove_manual": country.source == "manual",
            "csrf_token": csrf_token,
            "country_share": country_share,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/countries/{country_code}/note")
def update_country_note(
    country_code: str,
    request: Request,
    note: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    code = country_code.strip().upper()
    country = db.scalar(select(VisitedCountry).where(
        VisitedCountry.user_id == user.id, VisitedCountry.country_code == code
    ))
    if country and valid_csrf_token(request, csrf_token) and len(note) <= 2000:
        country.note = note.strip()
        db.commit()
    return RedirectResponse(f"/countries/{code}", status_code=303)


@router.post("/countries")
def add_visited_country(
    request: Request,
    country_code: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    code = country_code.strip().upper()
    if valid_csrf_token(request, csrf_token) and code in COUNTRY_NAMES:
        _ensure_visited_country(db, user.id, code, COUNTRY_NAMES[code], source="manual")
        db.commit()
    return RedirectResponse("/countries", status_code=303)


@router.post("/countries/{country_code}/delete")
def delete_visited_country(
    country_code: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    code = country_code.upper()
    if not valid_csrf_token(request, csrf_token):
        return RedirectResponse("/countries", status_code=303)
    has_memory = db.scalar(
        select(Memory.id).where(
            Memory.user_id == user.id, Memory.country_code == code
        ).limit(1)
    )
    country = db.scalar(
        select(VisitedCountry).where(
            VisitedCountry.user_id == user.id,
            VisitedCountry.country_code == code,
        )
    )
    if country:
        if has_memory and country.source == "manual":
            country.source = "memory"
        elif not has_memory:
            db.delete(country)
        db.commit()
    return RedirectResponse("/countries", status_code=303)


@router.get("/wishlist", response_class=HTMLResponse)
def wishlist_countries_page(
    request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    en = user.language == "en"
    countries = list(
        db.scalars(
            select(WishlistCountry)
            .where(WishlistCountry.user_id == user.id)
            .order_by(WishlistCountry.country_name)
        )
    )
    selected_codes = [country.country_code for country in countries]
    available_countries = [
        (code, name) for code, name in COUNTRY_CHOICES if code not in selected_codes
    ]
    csrf_token = csrf_token_for_request(request)
    response = templates.TemplateResponse(
        request=request,
        name="countries.html",
        context={
            "user": user,
            "countries": countries,
            "visited_codes": selected_codes,
            "memory_country_codes": set(),
            "available_countries": available_countries,
            "page_mode": "wishlist",
            "page_title": "Want to visit" if en else "Хочу посетить",
            "page_eyebrow": "Wishlist map" if en else "Карта желаний",
            "page_description": ("Mark countries for future journeys. This list is independent of your memories." if en else "Отмечайте страны для будущих путешествий. Этот список не зависит от воспоминаний."),
            "form_action": "/wishlist",
            "delete_prefix": "/wishlist",
            "country_items": [
                {
                    "code": item.country_code,
                    "name": item.country_name,
                    "source": "wishlist" if en else "в списке желаний",
                    "deletable": True,
                }
                for item in countries
            ],
            "csrf_token": csrf_token,
        },
    )
    set_csrf_cookie(response, csrf_token)
    return response


@router.post("/wishlist")
def add_wishlist_country(
    request: Request,
    country_code: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    code = country_code.strip().upper()
    if valid_csrf_token(request, csrf_token) and code in COUNTRY_NAMES:
        existing = db.scalar(
            select(WishlistCountry).where(
                WishlistCountry.user_id == user.id,
                WishlistCountry.country_code == code,
            )
        )
        if existing is None:
            db.add(
                WishlistCountry(
                    user_id=user.id,
                    country_code=code,
                    country_name=COUNTRY_NAMES[code],
                )
            )
            db.commit()
    return RedirectResponse("/wishlist", status_code=303)


@router.post("/wishlist/{country_code}/delete")
def delete_wishlist_country(
    country_code: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    user = current_user(db, request)
    if user is None:
        return _redirect_to_login()
    if valid_csrf_token(request, csrf_token):
        country = db.scalar(
            select(WishlistCountry).where(
                WishlistCountry.user_id == user.id,
                WishlistCountry.country_code == country_code.upper(),
            )
        )
        if country:
            db.delete(country)
            db.commit()
    return RedirectResponse("/wishlist", status_code=303)
