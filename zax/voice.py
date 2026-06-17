"""Zax's voice.

Two engines, picked at runtime from Settings:
  • edge-tts (free, offline-ish) — a curated set of natural neural voices. The
    default is a refined British voice for a JARVIS-like feel, at near-natural
    pitch (heavy pitch-shifting sounds robotic).
  • ElevenLabs (premium) — genuinely human, expressive voices. Needs an API key.

If both are unavailable the UI falls back to the browser's Web Speech API.
Voice is only produced when the client asks for it (the app no longer auto-talks).
"""
import io

import httpx

from . import config, db

try:
    import edge_tts

    EDGE_AVAILABLE = True
except ImportError:
    EDGE_AVAILABLE = False

AVAILABLE = EDGE_AVAILABLE  # static hint; use available() for the runtime check


def available() -> bool:
    """Can the server synthesize right now? edge-tts installed, OR an ElevenLabs key
    configured at runtime (settings or env)."""
    if EDGE_AVAILABLE:
        return True
    return bool(db.get_setting("voice.eleven_key", "") or config.ELEVEN_KEY_ENV)

# Curated edge-tts voices — characterful, human-sounding. id -> label.
EDGE_VOICES = [
    {"id": "en-GB-RyanNeural", "label": "Ryan · British, refined (JARVIS)"},
    {"id": "en-US-GuyNeural", "label": "Guy · American, warm"},
    {"id": "en-US-AndrewMultilingualNeural", "label": "Andrew · natural, conversational"},
    {"id": "en-GB-ThomasNeural", "label": "Thomas · British, calm"},
    {"id": "en-AU-WilliamNeural", "label": "William · Australian"},
    {"id": "en-US-ChristopherNeural", "label": "Christopher · deep"},
    {"id": "en-US-BrianMultilingualNeural", "label": "Brian · easy-going"},
]

# A few well-known ElevenLabs preset voices (name -> voice_id).
ELEVEN_PRESETS = [
    {"id": "pNInz6obpgDQGcFmaJgB", "label": "Adam · deep, measured (JARVIS)"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni · warm"},
    {"id": "onwK4e9ZLuTAKqWW03F9", "label": "Daniel · British news"},
    {"id": "TxGEqnHWrfWFTfGW9XjX", "label": "Josh · young, casual"},
]


# ---------------------------------------------------------------- config

def get_config() -> dict:
    return {
        "provider": db.get_setting("voice.provider", "edge"),  # edge | elevenlabs
        "edge_voice": db.get_setting("voice.edge_voice", "en-GB-RyanNeural"),
        "edge_pitch": db.get_setting("voice.edge_pitch", "-2Hz"),
        "edge_rate": db.get_setting("voice.edge_rate", "+0%"),
        "eleven_voice": db.get_setting("voice.eleven_voice", "pNInz6obpgDQGcFmaJgB"),
        "eleven_key": db.get_setting("voice.eleven_key", "") or config.ELEVEN_KEY_ENV,
    }


def public_config() -> dict:
    c = get_config()
    return {
        "provider": c["provider"],
        "edge_voice": c["edge_voice"],
        "eleven_voice": c["eleven_voice"],
        "edge_available": EDGE_AVAILABLE,
        "eleven_connected": bool(c["eleven_key"]),
        "edge_voices": EDGE_VOICES,
        "eleven_presets": ELEVEN_PRESETS,
    }


# ---------------------------------------------------------------- synthesis

async def synthesize(text: str) -> tuple[bytes, str]:
    """Return (audio_bytes, mime). Tries the chosen provider, falls back to edge."""
    c = get_config()
    if c["provider"] == "elevenlabs" and c["eleven_key"]:
        try:
            return await _eleven(text, c), "audio/mpeg"
        except Exception:
            pass  # fall back to edge
    return await _edge(text, c), "audio/mpeg"


async def _edge(text: str, c: dict) -> bytes:
    if not EDGE_AVAILABLE:
        raise RuntimeError("edge-tts not installed")
    communicate = edge_tts.Communicate(
        text, voice=c["edge_voice"], pitch=c["edge_pitch"], rate=c["edge_rate"]
    )
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buf.write(chunk["data"])
    audio = buf.getvalue()
    if not audio:
        raise RuntimeError("edge-tts returned no audio")
    return audio


async def _eleven(text: str, c: dict) -> bytes:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{c['eleven_voice']}",
            headers={"xi-api-key": c["eleven_key"], "accept": "audio/mpeg"},
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.35},
            },
        )
        r.raise_for_status()
        return r.content


async def verify_eleven(key: str) -> bool:
    """Validate an ElevenLabs key by listing voices."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get("https://api.elevenlabs.io/v1/user",
                             headers={"xi-api-key": key})
        return r.status_code == 200
