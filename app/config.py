from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus


def _database_url_from_environment(data_dir: Path) -> str:
    explicit_url = os.getenv("DATABASE_URL", "").strip()
    if explicit_url:
        return explicit_url

    # Wasmer injects these variables for its managed database.
    required = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USERNAME", "DB_PASSWORD")
    if all(os.getenv(key) for key in required):
        username = quote_plus(os.environ["DB_USERNAME"])
        password = quote_plus(os.environ["DB_PASSWORD"])
        host = os.environ["DB_HOST"]
        port = os.environ["DB_PORT"]
        name = quote_plus(os.environ["DB_NAME"])
        return f"postgresql+pg8000://{username}:{password}@{host}:{port}/{name}"

    sqlite_path = (data_dir / "nomad.db").resolve()
    return f"sqlite:///{sqlite_path.as_posix()}"


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    data_dir: Path
    upload_dir: Path
    database_url: str
    session_cookie_name: str
    csrf_cookie_name: str
    session_days: int
    nominatim_url: str
    nominatim_reverse_url: str
    nominatim_user_agent: str
    max_photo_bytes: int
    max_photos_per_memory: int
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str


def load_settings() -> Settings:
    # /data is a persistent Wasmer volume; .data is convenient for local work.
    default_data_dir = "/data" if Path("/data").is_dir() else ".data"
    data_dir = Path(os.getenv("DATA_DIR", default_data_dir)).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    upload_dir = data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        app_name="Nomad",
        app_env=os.getenv("APP_ENV", "development"),
        data_dir=data_dir,
        upload_dir=upload_dir,
        database_url=_database_url_from_environment(data_dir),
        session_cookie_name="nomad_session",
        csrf_cookie_name="nomad_csrf",
        session_days=30,
        nominatim_url=os.getenv(
            "NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
        ),
        nominatim_reverse_url=os.getenv(
            "NOMINATIM_REVERSE_URL", "https://nominatim.openstreetmap.org/reverse"
        ),
        nominatim_user_agent=os.getenv(
            "NOMINATIM_USER_AGENT",
            "Nomad/0.3 (personal travel memory application)",
        ),
        max_photo_bytes=5 * 1024 * 1024,
        max_photos_per_memory=10,
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
        google_redirect_uri=os.getenv("GOOGLE_REDIRECT_URI", "").strip(),
    )


settings = load_settings()
