"""Shared test fixtures.

Every test runs against a FRESH temp SQLite DB and the deterministic mock LLM
provider, so tests are isolated, fast, and need no API keys or network.
"""
import os

# Force the mock provider before any zax module imports config.
os.environ["ZAX_PROVIDER"] = "mock"
os.environ.setdefault("ZAX_ALLOW_SHELL", "0")
os.environ.setdefault("ZAX_ALLOW_CODE", "0")

import pytest  # noqa: E402

from zax import config, db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Point the whole app at a per-test temp data dir + DB and reconnect."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "zax.db")
    monkeypatch.setattr(config, "PROVIDER", "mock")
    db._conn = None
    db.connect()
    yield
    db._conn = None


@pytest.fixture
def seeded(fresh_db):
    """A DB with the founding team hired (mirrors first boot)."""
    from zax import ceo
    ceo.ensure_org_seeded()
    return None
