from __future__ import annotations

import re
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.memories as memories_module
from app.config import settings
from app.geocoding import GeocodeResult, GeocodingError
from app.main import app
from app.models import Memory, User, VisitedCountry, WishlistCountry


def csrf_from(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def register(client: TestClient, username: str) -> None:
    page = client.get("/register")
    response = client.post(
        "/register",
        data={
            "username": username,
            "full_name": f"Name {username}",
            "password": "a-secure-test-password",
            "password_confirm": "a-secure-test-password",
            "csrf_token": csrf_from(page),
        },
        follow_redirects=False,
    )
    assert response.status_code == 200


def test_memory_crud_and_account_isolation(monkeypatch, test_session_factory) -> None:
    async def fake_geocode(db, place_name):
        return GeocodeResult(
            latitude=48.85837,
            longitude=2.29448,
            country_code="FR",
            country_name="Франция",
            display_name="Эйфелева башня, Париж, Франция",
        )

    monkeypatch.setattr(memories_module, "geocode_place", fake_geocode)

    with TestClient(app) as owner:
        register(owner, "map_owner")
        page = owner.get("/memories/new")
        created = owner.post(
            "/memories",
            data={
                "place_name": "Вечер в Париже",
                "location_name": "Эйфелева башня, Париж",
                "visit_date": "2024-05",
                "description": "Вечерний вид на город.",
                "photo_urls": "https://example.com/eiffel.jpg",
                "csrf_token": csrf_from(page),
            },
            files={"photos": ("memory.png", b"\x89PNG\r\n\x1a\nnomad", "image/png")},
            follow_redirects=False,
        )
        assert created.status_code == 303

        with test_session_factory() as db:
            memory = db.scalar(select(Memory))
            assert memory is not None
            memory_id = memory.id
            assert memory.visit_date.isoformat() == "2024-05-01"
            assert memory.place_name == "Вечер в Париже"
            assert memory.location_name == "Эйфелева башня, Париж"
            assert memory.country_code == "FR"
            local_url = next(url for url in memory.photo_urls if url.startswith("/uploads/"))
            visited = db.scalar(
                select(VisitedCountry).where(VisitedCountry.user_id == memory.user_id)
            )
            assert visited is not None
            assert visited.country_code == "FR"

        assert owner.get(local_url).status_code == 200

        dashboard = owner.get("/dashboard")
        assert "Вечер в Париже" in dashboard.text
        assert "48.85837" in dashboard.text

        edit_page = owner.get(f"/memories/{memory_id}/edit")
        edited = owner.post(
            f"/memories/{memory_id}/edit",
            data={
                "place_name": "Парижская годовщина",
                "location_name": "Лувр, Париж",
                "visit_date": "2025-05",
                "description": "Обновлённое воспоминание.",
                "photo_urls": "",
                "latitude": "48.86061",
                "longitude": "2.33764",
                "country_code": "FR",
                "country_name": "Франция",
                "resolved_location_name": "Лувр, Париж",
                "csrf_token": csrf_from(edit_page),
            },
            follow_redirects=False,
        )
        assert edited.status_code == 303
        edited_page = owner.get(edited.headers["location"])
        assert "Обновлённое воспоминание" in edited_page.text
        assert "Парижская годовщина" in edited_page.text
        assert "Лувр, Париж" in edited_page.text
        assert "05.2025" in edited_page.text
        with test_session_factory() as db:
            edited_memory = db.get(Memory, memory_id)
            assert edited_memory is not None
            assert edited_memory.latitude == 48.86061
            assert edited_memory.longitude == 2.33764

        with TestClient(app) as stranger:
            register(stranger, "another_user")
            assert stranger.get(f"/memories/{memory_id}").status_code == 404
            countries_page = stranger.get("/countries")
            added = stranger.post(
                "/countries",
                data={"country_code": "PK", "csrf_token": csrf_from(countries_page)},
                follow_redirects=False,
            )
            assert added.status_code == 303
            assert "Пакистан" in stranger.get("/countries").text

        detail = owner.get(f"/memories/{memory_id}")
        deleted = owner.post(
            f"/memories/{memory_id}/delete",
            data={"csrf_token": csrf_from(detail)},
            follow_redirects=False,
        )
        assert deleted.status_code == 303
        assert owner.get(f"/memories/{memory_id}").status_code == 404
        assert not (settings.upload_dir / str(memory.user_id) / local_url.rsplit("/", 1)[1]).exists()


def test_failed_geocoding_offers_manual_map(monkeypatch, test_session_factory) -> None:
    async def failed_geocode(db, place_name):
        raise GeocodingError("Место не найдено.")

    monkeypatch.setattr(memories_module, "geocode_place", failed_geocode)

    with TestClient(app) as client:
        register(client, "manual_map_user")
        page = client.get("/memories/new")
        response = client.post(
            "/memories",
            data={
                "place_name": "Поездка к морю",
                "location_name": "Неизвестная бухта",
                "visit_date": "2026-07",
                "csrf_token": csrf_from(page),
            },
        )

    assert response.status_code == 400
    assert "Выберите нужную точку вручную на карте" in response.text
    assert 'id="location-picker-map"' in response.text


def test_dashboard_filters_are_dependent_and_accept_empty_values(test_session_factory) -> None:
    with TestClient(app) as client:
        register(client, "filter_user")
        with test_session_factory() as db:
            user_id = db.scalar(select(User.id).where(User.username == "filter_user"))
            assert user_id is not None
            db.add_all(
                [
                    Memory(user_id=user_id, place_name="Париж", location_name="Париж", latitude=48.8, longitude=2.3, visit_date=date(2024, 5, 1), country_code="FR", country_name="Франция"),
                    Memory(user_id=user_id, place_name="Стамбул", location_name="Стамбул", latitude=41.0, longitude=28.9, visit_date=date(2025, 6, 1), country_code="TR", country_name="Турция"),
                ]
            )
            db.commit()

        empty = client.get("/dashboard?year=&country=")
        assert empty.status_code == 200
        assert "Париж" in empty.text and "Стамбул" in empty.text

        by_year = client.get("/dashboard?year=2024")
        assert 'value="FR"' in by_year.text
        assert 'value="TR"' not in by_year.text

        by_country = client.get("/dashboard?country=TR")
        assert 'value="2025"' in by_country.text
        assert 'value="2024"' not in by_country.text

        assert client.get("/dashboard?year=not-a-year").status_code == 200


def test_wishlist_is_independent_and_prevents_duplicates(test_session_factory) -> None:
    with TestClient(app) as client:
        register(client, "wishlist_user")
        page = client.get("/wishlist")
        assert page.status_code == 200
        assert "Хочу посетить" in page.text
        assert ">Япония</option>" in page.text
        assert "🇯🇵" not in page.text

        for _ in range(2):
            page = client.get("/wishlist")
            added = client.post(
                "/wishlist",
                data={"country_code": "JP", "csrf_token": csrf_from(page)},
                follow_redirects=False,
            )
            assert added.status_code == 303

        with test_session_factory() as db:
            wishes = list(db.scalars(select(WishlistCountry)))
            assert len(wishes) == 1
            assert wishes[0].country_code == "JP"
            assert db.scalar(select(VisitedCountry)) is None

        page = client.get("/wishlist")
        removed = client.post(
            "/wishlist/JP/delete",
            data={"csrf_token": csrf_from(page)},
            follow_redirects=False,
        )
        assert removed.status_code == 303
        with test_session_factory() as db:
            assert db.scalar(select(WishlistCountry)) is None


def test_manual_country_survives_memories_but_memory_only_country_does_not(
    test_session_factory,
) -> None:
    with TestClient(app) as client:
        register(client, "country_source_user")

    with test_session_factory() as db:
        user_id = db.scalar(select(User.id).where(User.username == "country_source_user"))
        assert user_id is not None
        manual = VisitedCountry(
            user_id=user_id, country_code="FR", country_name="Франция", source="manual"
        )
        automatic = VisitedCountry(
            user_id=user_id, country_code="TR", country_name="Турция", source="memory"
        )
        france_memory = Memory(
            user_id=user_id, place_name="Париж", location_name="Париж",
            latitude=48.8, longitude=2.3, visit_date=date(2024, 5, 1),
            country_code="FR", country_name="Франция",
        )
        turkey_memory = Memory(
            user_id=user_id, place_name="Стамбул", location_name="Стамбул",
            latitude=41.0, longitude=28.9, visit_date=date(2025, 6, 1),
            country_code="TR", country_name="Турция",
        )
        db.add_all([manual, automatic, france_memory, turkey_memory])
        db.commit()

        db.delete(france_memory)
        db.delete(turkey_memory)
        db.flush()
        memories_module._remove_orphan_memory_country(db, user_id, "FR")
        memories_module._remove_orphan_memory_country(db, user_id, "TR")
        db.commit()

        remaining = list(db.scalars(select(VisitedCountry)))
        assert [(item.country_code, item.source) for item in remaining] == [("FR", "manual")]
