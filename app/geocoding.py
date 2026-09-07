from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GeocodeCache


class GeocodingError(Exception):
    pass


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    country_code: str | None
    country_name: str | None
    display_name: str


_request_lock = asyncio.Lock()
_last_request_at = 0.0


def _normalized_query(place_name: str) -> str:
    return " ".join(place_name.strip().casefold().split())


def _from_cache(item: GeocodeCache) -> GeocodeResult:
    return GeocodeResult(
        latitude=item.latitude,
        longitude=item.longitude,
        country_code=item.country_code,
        country_name=item.country_name,
        display_name=item.display_name,
    )


async def geocode_place(db: Session, place_name: str) -> GeocodeResult:
    global _last_request_at

    query = _normalized_query(place_name)
    if not query:
        raise GeocodingError("Введите название локации для воспоминания.")

    cached = db.scalar(select(GeocodeCache).where(GeocodeCache.query == query))
    if cached:
        return _from_cache(cached)

    async with _request_lock:
        # Recheck after waiting: another request may have populated the cache.
        cached = db.scalar(select(GeocodeCache).where(GeocodeCache.query == query))
        if cached:
            return _from_cache(cached)

        delay = 1.05 - (time.monotonic() - _last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)

        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": settings.nominatim_user_agent}, timeout=12.0
            ) as client:
                response = await client.get(
                    settings.nominatim_url,
                    params={
                        "q": place_name.strip(),
                        "format": "jsonv2",
                        "limit": 1,
                        "addressdetails": 1,
                        "accept-language": "ru,en",
                    },
                )
                _last_request_at = time.monotonic()
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeocodingError(
                "Геокодер временно недоступен. Попробуйте ещё раз чуть позже."
            ) from exc

        if not payload:
            raise GeocodingError(
                "Место не найдено. Добавьте город или страну и попробуйте снова."
            )

        item = payload[0]
        address = item.get("address") or {}
        result = GeocodeResult(
            latitude=float(item["lat"]),
            longitude=float(item["lon"]),
            country_code=(address.get("country_code") or "").upper() or None,
            country_name=address.get("country"),
            display_name=item.get("display_name") or place_name.strip(),
        )
        cache_item = GeocodeCache(
            query=query,
            display_name=result.display_name,
            latitude=result.latitude,
            longitude=result.longitude,
            country_code=result.country_code,
            country_name=result.country_name,
        )
        db.add(cache_item)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        return result


async def reverse_geocode(latitude: float, longitude: float) -> GeocodeResult:
    global _last_request_at

    async with _request_lock:
        delay = 1.05 - (time.monotonic() - _last_request_at)
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": settings.nominatim_user_agent}, timeout=12.0
            ) as client:
                response = await client.get(
                    settings.nominatim_reverse_url,
                    params={
                        "lat": latitude,
                        "lon": longitude,
                        "format": "jsonv2",
                        "addressdetails": 1,
                        "accept-language": "ru,en",
                    },
                )
                _last_request_at = time.monotonic()
                response.raise_for_status()
                item = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeocodingError("Не удалось определить страну для выбранной точки.") from exc

        if not item or item.get("error"):
            raise GeocodingError("Для выбранной точки не найдена локация.")
        address = item.get("address") or {}
        return GeocodeResult(
            latitude=latitude,
            longitude=longitude,
            country_code=(address.get("country_code") or "").upper() or None,
            country_name=address.get("country"),
            display_name=item.get("display_name") or f"{latitude:.5f}, {longitude:.5f}",
        )
