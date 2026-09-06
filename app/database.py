from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine_options: dict[str, object] = {"pool_pre_ping": True}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_database_schema() -> None:
    # Importing models registers their tables in Base.metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version VARCHAR(80) PRIMARY KEY, "
                "applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        applied = set(
            connection.execute(text("SELECT version FROM schema_migrations")).scalars()
        )
        column_names = {
            column["name"] for column in inspect(connection).get_columns("memories")
        }
        migration = "001_memory_location_name"
        if migration not in applied:
            if "location_name" not in column_names:
                connection.execute(
                    text("ALTER TABLE memories ADD COLUMN location_name VARCHAR(250)")
                )
            connection.execute(
                text(
                    "UPDATE memories SET location_name = place_name "
                    "WHERE location_name IS NULL"
                )
            )
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": migration},
            )
        user_columns = {
            column["name"] for column in inspect(connection).get_columns("users")
        }
        migration = "003_user_preferences"
        if migration not in applied:
            if "language" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN language VARCHAR(5) DEFAULT 'ru'"))
            if "theme" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN theme VARCHAR(10) DEFAULT 'dark'"))
            connection.execute(text("UPDATE users SET language = 'ru' WHERE language IS NULL"))
            connection.execute(text("UPDATE users SET theme = 'dark' WHERE theme IS NULL"))
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": migration},
            )
        user_columns = {
            column["name"] for column in inspect(connection).get_columns("users")
        }
        migration = "002_user_profile"
        if migration not in applied:
            if "full_name" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(100)"))
            if "profile_photo_url" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN profile_photo_url VARCHAR(500)"))
            connection.execute(
                text("UPDATE users SET full_name = username WHERE full_name IS NULL OR full_name = ''")
            )
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": migration},
            )


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
