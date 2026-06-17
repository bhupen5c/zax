"""Unit tests for Telegram integration and the voice config layer."""
from zax import db, telegram, voice


# ---------------------------------------------------------------- telegram

def test_telegram_status_defaults():
    s = telegram.status()
    assert s["has_token"] is False and s["connected"] is False and s["notify"] is True


def test_telegram_config_reads_settings():
    db.set_setting("telegram.token", "123:abc")
    db.set_setting("telegram.chat_id", "999")
    assert telegram.configured() is True
    assert telegram.chat_id() == "999"


async def test_handle_start_registers_owner(monkeypatch):
    db.set_setting("telegram.token", "123:abc")
    db.set_setting("telegram.chat_id", "")
    sent = []
    async def fake_send(text, to=""): sent.append((to, text))
    monkeypatch.setattr(telegram, "send", fake_send)
    await telegram._handle_message({"chat": {"id": 555}, "text": "/start"})
    assert telegram.chat_id() == "555"  # first /start links the owner
    assert sent and "connected" in sent[0][1].lower()


async def test_handle_rejects_non_owner(monkeypatch):
    db.set_setting("telegram.token", "123:abc")
    db.set_setting("telegram.chat_id", "555")  # owner already linked
    sent = []
    async def fake_send(text, to=""): sent.append((to, text))
    monkeypatch.setattr(telegram, "send", fake_send)
    await telegram._handle_message({"chat": {"id": 777}, "text": "hello"})
    assert any("privately linked" in t.lower() for _, t in sent)


async def test_handle_status_command(monkeypatch):
    from zax import ceo
    ceo.ensure_org_seeded()
    db.set_setting("telegram.token", "123:abc")
    db.set_setting("telegram.chat_id", "555")
    sent = []
    async def fake_send(text, to=""): sent.append(text)
    monkeypatch.setattr(telegram, "send", fake_send)
    async def fake_call(method, params, timeout=35): return {}
    monkeypatch.setattr(telegram, "_call", fake_call)
    await telegram._handle_message({"chat": {"id": 555}, "text": "/status"})
    assert any("agents" in t.lower() for t in sent)


# ---------------------------------------------------------------- voice

def test_voice_config_defaults():
    c = voice.get_config()
    assert c["provider"] == "edge"
    assert c["edge_voice"].startswith("en-")


def test_voice_public_config_lists_voices():
    pub = voice.public_config()
    assert len(pub["edge_voices"]) >= 5
    assert len(pub["eleven_presets"]) >= 1
    assert "provider" in pub and "eleven_connected" in pub


def test_voice_provider_switch_persists():
    db.set_setting("voice.provider", "elevenlabs")
    db.set_setting("voice.edge_voice", "en-US-GuyNeural")
    c = voice.get_config()
    assert c["provider"] == "elevenlabs" and c["edge_voice"] == "en-US-GuyNeural"
