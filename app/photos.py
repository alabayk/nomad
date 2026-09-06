from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import UploadFile

from app.config import settings


class PhotoError(Exception):
    pass


def parse_photo_urls(value: str) -> list[str]:
    urls: list[str] = []
    for raw_url in value.splitlines():
        url = raw_url.strip()
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PhotoError(f"Некорректная ссылка на фото: {url}")
        if len(url) > 2_000:
            raise PhotoError("Ссылка на фото слишком длинная.")
        if url not in urls:
            urls.append(url)
    return urls


def external_photo_urls(urls: list[str]) -> list[str]:
    return [url for url in urls if url.startswith(("http://", "https://"))]


def local_photo_urls(urls: list[str]) -> list[str]:
    return [url for url in urls if url.startswith("/uploads/")]


def _image_suffix(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


async def save_uploads(user_id: int, uploads: list[UploadFile]) -> list[str]:
    saved_urls: list[str] = []
    user_dir = settings.upload_dir / str(user_id)
    try:
        for upload in uploads:
            if not upload.filename:
                continue
            data = await upload.read(settings.max_photo_bytes + 1)
            await upload.close()
            if len(data) > settings.max_photo_bytes:
                raise PhotoError("Размер каждого фото не должен превышать 5 МБ.")
            suffix = _image_suffix(data)
            if not suffix:
                raise PhotoError("Поддерживаются изображения JPG, PNG, GIF и WebP.")
            user_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{uuid4().hex}{suffix}"
            (user_dir / filename).write_bytes(data)
            saved_urls.append(f"/uploads/{user_id}/{filename}")
    except OSError as exc:
        delete_local_photos(user_id, saved_urls)
        raise PhotoError("Не удалось сохранить фотографию на сервере.") from exc
    except PhotoError:
        delete_local_photos(user_id, saved_urls)
        raise
    return saved_urls


def delete_local_photos(user_id: int, urls: list[str]) -> None:
    user_dir = (settings.upload_dir / str(user_id)).resolve()
    for url in urls:
        prefix = f"/uploads/{user_id}/"
        if not url.startswith(prefix):
            continue
        candidate = (user_dir / Path(url).name).resolve()
        if candidate.parent == user_dir:
            candidate.unlink(missing_ok=True)
