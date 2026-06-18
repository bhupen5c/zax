"""End-to-end API integration tests through the real FastAPI app (mock provider).

The heartbeat/telegram background loops are disabled so tests are deterministic.
The client uses Host: localhost so the TrustedHost hardening lets it through.
"""
import pytest
from fastapi.testclient import TestClient

from zax import heartbeat, main, telegram


@pytest.fixture
def client(fresh_db, monkeypatch):
    monkeypatch.setattr(heartbeat, "start", lambda: None)
    monkeypatch.setattr(telegram, "start", lambda: None)
    with TestClient(main.app, base_url="http://localhost") as c:
        yield c


# ---------------------------------------------------------------- core

def test_status_ok(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert "provider" in body and "circuit_breaker" in body and "headcount" in body


def test_index_and_static(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_greeting(client):
    r = client.get("/api/greeting")
    assert r.status_code == 200 and r.json()["text"]


# ---------------------------------------------------------------- chat + sessions

def test_chat_and_validation(client):
    assert client.post("/api/chat", json={"message": "status report"}).status_code == 200
    assert client.post("/api/chat", json={"message": ""}).status_code == 422


def test_sessions_flow(client):
    s = client.post("/api/sessions", json={}).json()
    sid = s["id"]
    client.post("/api/chat", json={"message": "plan launch", "session_id": sid})
    assert any(x["id"] == sid for x in client.get("/api/sessions").json())
    assert client.post(f"/api/sessions/{sid}/rename", json={"title": "Launch"}).status_code == 200
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    assert client.delete("/api/sessions/main").status_code == 400  # main protected


# ---------------------------------------------------------------- tasks + run

def test_tasks_crud_and_validation(client):
    assert client.post("/api/tasks", json={"title": "Do X", "priority": 2}).status_code == 200
    assert client.post("/api/tasks", json={"title": "x", "priority": 9}).status_code == 422
    assert client.post("/api/tasks", json={"priority": 1}).status_code == 422
    assert client.get("/api/tasks").status_code == 200


def test_run_now(client):
    assert client.post("/api/run", json={}).status_code == 200


def test_get_single_task(client):
    t = client.post("/api/tasks", json={"title": "trackable", "priority": 1}).json()
    r = client.get(f"/api/tasks/{t['id']}")
    assert r.status_code == 200 and r.json()["title"] == "trackable"
    assert client.get("/api/tasks/nonexistent").status_code == 404


def test_chat_action_returns_task_id(client):
    """Delegated tasks must come back with a task_id so the chat can track them live."""
    from zax import ceo
    reply, actions = ceo._execute_actions(
        '<action>{"type":"create_task","title":"track me","priority":1}</action>')
    assert actions and actions[0].get("task_id")


# ---------------------------------------------------------------- org + skills

def test_skills_catalog_and_hire(client):
    cats = client.get("/api/skills").json()
    assert sum(len(v) for v in cats.values()) >= 16
    r = client.post("/api/skills/hire", json={"skill": "coder"})
    assert r.status_code == 200 and r.json()["skill"] == "coder"
    assert client.post("/api/skills/hire", json={"skill": "nope"}).status_code == 404


def test_hire_and_fire(client):
    a = client.post("/api/hire", json={"role": "market research"}).json()
    assert client.post(f"/api/agents/{a['id']}/fire", json={"reason": "x"}).status_code == 200
    assert client.post("/api/agents/ghost/fire", json={"reason": "x"}).status_code == 404


# ---------------------------------------------------------------- providers + voice + telegram

def test_providers(client):
    assert client.get("/api/providers").status_code == 200
    assert client.post("/api/providers/select", json={"provider": "mock"}).status_code == 200
    assert client.post("/api/providers/select", json={"provider": "nope"}).status_code == 404


def test_voice_config(client):
    assert client.get("/api/voice/config").status_code == 200
    assert client.post("/api/voice/config", json={"edge_voice": "en-US-GuyNeural"}).status_code == 200


def test_telegram_status_and_bad_token(client):
    assert client.get("/api/telegram").status_code == 200
    assert client.post("/api/telegram/connect",
                       json={"token": "000000:invalidinvalidinvalidinvalid"}).status_code == 400


# ---------------------------------------------------------------- graph + memory + learning

def test_graph_endpoints(client):
    data = client.get("/api/graph").json()
    assert "nodes" in data and "links" in data
    assert client.get("/api/graph/stats").status_code == 200
    assert client.get("/api/graph/query?q=").status_code == 400  # empty rejected
    assert client.get("/api/graph/path?from_=a&to=b").status_code == 200
    assert client.get("/api/graph/explain?node=x").status_code == 200
    assert client.get("/api/graph/path?from_=&to=").status_code == 400


def test_memory_and_learning(client):
    assert client.get("/api/memory").status_code == 200
    assert client.get("/api/learning/status").status_code == 200


# ---------------------------------------------------------------- security hardening

def test_non_json_post_blocked(client):
    r = client.post("/api/chat", content="x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_bad_host_blocked(client):
    r = client.get("/api/status", headers={"Host": "evil.example.com"})
    assert r.status_code == 400


# ---------------------------------------------------------------- deploy: health + auth

def test_healthz_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_access_password_gate(fresh_db, monkeypatch):
    import base64

    from zax import config as cfg, heartbeat as hb, main as m, telegram as tg
    monkeypatch.setattr(cfg, "ACCESS_PASSWORD", "s3cret")
    monkeypatch.setattr(hb, "start", lambda: None)
    monkeypatch.setattr(tg, "start", lambda: None)
    with TestClient(m.app, base_url="http://localhost") as c:
        assert c.get("/api/status").status_code == 401          # no creds
        assert c.get("/healthz").status_code == 200             # health stays open
        good = base64.b64encode(b"founder:s3cret").decode()
        assert c.get("/api/status", headers={"Authorization": f"Basic {good}"}).status_code == 200
        bad = base64.b64encode(b"founder:wrong").decode()
        assert c.get("/api/status", headers={"Authorization": f"Basic {bad}"}).status_code == 401
