"""DB-backed tests use a separate `carscout_test` database in the same Postgres server.
Run inside compose:  docker compose run --rm api pytest"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DB = "carscout_test"


def _base_url() -> str:
    from carscout.core.config import Settings

    s = Settings()
    return s.database_url.rsplit("/", 1)[0]


@pytest.fixture(scope="session")
def db_url() -> str:
    try:
        import psycopg

        admin = _base_url().replace("postgresql+psycopg://", "postgresql://") + "/postgres"
        with psycopg.connect(admin, autocommit=True, connect_timeout=3) as c:
            if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (TEST_DB,)).fetchone():
                c.execute(f'CREATE DATABASE "{TEST_DB}"')
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable for DB tests: {e}")
    return f"{_base_url()}/{TEST_DB}"


@pytest.fixture(scope="session")
def engine(db_url: str):
    from sqlalchemy import create_engine

    from carscout.models import Base

    eng = create_engine(db_url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def sm(engine, monkeypatch):
    """Clean tables + point the app's engine/sessionmaker at the test DB."""
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    from carscout.core import db as dbmod
    from carscout.models import Base

    with engine.begin() as c:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        c.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "get_engine", lambda: engine)
    monkeypatch.setattr(dbmod, "get_sessionmaker", lambda: factory)
    return factory


@pytest.fixture(autouse=True)
def sources_file(tmp_path: Path, monkeypatch):
    """Every test gets its own sources.yaml so tests can flip modes safely."""
    from carscout.core.config import get_settings

    path = tmp_path / "sources.yaml"
    path.write_text(
        """
sources:
  demo:
    label: Demo
    synthetic: true
    enabled: true
    delay: 0
    jitter: 0
    options: {count: 5, mode: ok}
  carsales:
    label: Carsales
    enabled: true
    implemented: false
  facebook:
    label: Facebook Marketplace
    enabled: false
    implemented: false
"""
    )
    monkeypatch.setenv("SOURCES_FILE", str(path))
    monkeypatch.delenv("ENABLED_SOURCES", raising=False)
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def set_demo_mode(path: Path, mode: str) -> None:
    path.write_text(path.read_text().replace("mode: ok", f"mode: {mode}"))


os.environ.setdefault("LOG_LEVEL", "WARNING")
