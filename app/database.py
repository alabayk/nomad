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
        migration = "004_google_login"
        if migration not in applied:
            if "google_sub" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN google_sub VARCHAR(255)"))
            if "email" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(320)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub ON users (google_sub)"))
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": migration},
            )
        user_columns = {
            column["name"] for column in inspect(connection).get_columns("users")
        }
        migration = "005_password_login_state"
        if migration not in applied:
            if "password_enabled" not in user_columns:
                connection.execute(text("ALTER TABLE users ADD COLUMN password_enabled BOOLEAN DEFAULT TRUE"))
            connection.execute(text("UPDATE users SET password_enabled = FALSE WHERE google_sub IS NOT NULL"))
            connection.execute(text("UPDATE users SET password_enabled = TRUE WHERE password_enabled IS NULL"))
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
        visited_columns = {
            column["name"] for column in inspect(connection).get_columns("visited_countries")
        }
        migration = "006_country_notes"
        if migration not in applied:
            if "note" not in visited_columns:
                connection.execute(text("ALTER TABLE visited_countries ADD COLUMN note TEXT DEFAULT ''"))
            connection.execute(text("UPDATE visited_countries SET note = '' WHERE note IS NULL"))
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": migration},
            )
        user_columns = {column["name"] for column in inspect(connection).get_columns("users")}
        migration = "007_friend_privacy"
        if migration not in applied:
            for column in ("privacy_profile", "privacy_countries", "privacy_timeline", "privacy_memories"):
                if column not in user_columns:
                    connection.execute(text(f"ALTER TABLE users ADD COLUMN {column} BOOLEAN DEFAULT TRUE"))
                connection.execute(text(f"UPDATE users SET {column} = TRUE WHERE {column} IS NULL"))
            connection.execute(text("INSERT INTO schema_migrations (version) VALUES (:version)"), {"version": migration})
        memory_columns = {column["name"] for column in inspect(connection).get_columns("memories")}
        migration = "008_memory_favorites"
        if migration not in applied:
            if "is_favorite" not in memory_columns:
                connection.execute(text("ALTER TABLE memories ADD COLUMN is_favorite BOOLEAN DEFAULT FALSE"))
            connection.execute(text("UPDATE memories SET is_favorite = FALSE WHERE is_favorite IS NULL"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_memories_is_favorite ON memories (is_favorite)"))
            connection.execute(text("INSERT INTO schema_migrations (version) VALUES (:version)"), {"version": migration})


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
