"""Test-session setup.

WeasyPrint dlopens Pango/Cairo/GObject. On macOS those live under the Homebrew
prefix, which dyld does not search by default, so `pytest` fails with
`OSError: cannot load library 'libgobject-2.0-0'` -- directly in test_pdf, and
indirectly in test_api, where publish_report raises and no report event is
emitted.  ctypes reads DYLD_FALLBACK_LIBRARY_PATH on each lookup, so setting it
before any test imports app.pdf is enough; app/pdf.py imports weasyprint lazily.

Linux and the container image put these libraries on the default search path,
so this is a no-op there.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "darwin":
    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    for prefix in ("/opt/homebrew/lib", "/usr/local/lib"):
        if (Path(prefix) / "libgobject-2.0.0.dylib").exists():
            if prefix not in existing.split(":"):
                # Keep dyld's implicit defaults, which setting this variable replaces.
                parts = [prefix, existing or "/usr/local/lib:/usr/lib"]
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(p for p in parts if p)
            break


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------
# Persistence is required in production, but a contributor running `pytest` should
# get an instruction rather than an obscure connection error, so the database tests
# skip with a message naming what to start.
import asyncio  # noqa: E402

import pytest  # noqa: E402

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv("DATABASE_URL", "postgresql+asyncpg://localhost:5432/report_agent_test"),
)
SKIP_REASON = (
    f"no database at {TEST_DB_URL.rsplit('@', 1)[-1]} — start one with "
    "`docker compose up -d postgres` (or set TEST_DATABASE_URL)"
)


def _database_available() -> bool:
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(TEST_DB_URL.replace("postgresql+asyncpg", "postgresql"))
    host, port = parsed.hostname or "localhost", parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def database_url() -> str:
    if not _database_available():
        pytest.skip(SKIP_REASON, allow_module_level=True)
    return TEST_DB_URL


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """A schema built by Alembic, so the tests exercise the real migration."""
    import subprocess

    env = {**os.environ, "DATABASE_URL": database_url}
    repo_root = Path(__file__).resolve().parent
    # sys.executable, not a bare `alembic`: the entry point lives in the venv and
    # may not be on PATH when pytest is invoked by an editor or a hook.
    proc = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                          cwd=str(repo_root), env=env, capture_output=True, text=True)
    if proc.returncode:
        pytest.skip(f"alembic upgrade failed: {proc.stderr.strip().splitlines()[-1:]}")
    return database_url


@pytest.fixture
def db_loop():
    """One event loop per test.

    asyncpg binds its connections to the loop that created them, so a pool opened
    in one loop and used from another fails with "attached to a different loop".
    Every coroutine in a test therefore runs on this single loop.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def db_conn(migrated_database, db_loop):
    """A connected engine for one test, with its rows removed afterwards.

    Truncate rather than a wrapping transaction: the code under test opens its own
    transactions through the pool, so an outer one would not contain them.
    """
    from sqlalchemy import text

    import app.config as cfg
    from app.db.engine import db as _db

    object.__setattr__(cfg.settings, "database_url", migrated_database)
    db_loop.run_until_complete(_db.connect())

    async def _teardown():
        async with _db.begin() as conn:
            await conn.execute(text(
                "TRUNCATE conversations, turns, events, reports, analyses,"
                " sdk_transcript_entries RESTART IDENTITY CASCADE"))
        await _db.close()

    try:
        yield _db
    finally:
        db_loop.run_until_complete(_teardown())


@pytest.fixture
def run(db_loop):
    """Run a coroutine on the test's event loop — see `db_loop` for why it matters."""
    return db_loop.run_until_complete
