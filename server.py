import os
import logging
import json
import random
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaamdhenu-server")

app = FastAPI(title="Kaamdhenu AI 3.0 Voice SaaS")

# In-memory settings store (persisted to settings.json if available)
SETTINGS_FILE = Path("settings.json")
_settings_cache = {}

def _load_settings_from_disk():
    """Load persisted settings from settings.json on startup."""
    global _settings_cache
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                _settings_cache = json.load(f)
                logger.info(f"Loaded {len(_settings_cache)} settings from {SETTINGS_FILE}")
        except Exception as e:
            logger.warning(f"Could not load settings.json: {e}")

def _save_settings_to_disk():
    """Persist settings to settings.json."""
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(_settings_cache, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not write settings.json: {e}")

_load_settings_from_disk()

# Mount static directory if ui folder exists
if os.path.exists("ui"):
    app.mount("/ui", StaticFiles(directory="ui"), name="ui")

@app.get("/", response_class=HTMLResponse)
def read_root():
    """Serves the complete Dark Dashboard UI HTML on root URL (/)."""
    ui_path = os.path.join("ui", "index.html")
    if os.path.exists(ui_path):
        try:
            with open(ui_path, "r", encoding="utf-8") as f:
                html_content = f.read()
            return HTMLResponse(content=html_content, status_code=200)
        except Exception as e:
            logger.error(f"Error reading ui/index.html: {e}")
    
    fallback_html = """<!DOCTYPE html><html><head><title>Kaamdhenu 3.0</title></head><body style="background:#090d16;color:#fff;font-family:sans-serif;padding:40px;"><h1>Kaamdhenu 3.0 Real Estate Voice SaaS</h1><p>Status: Online</p></body></html>"""
    return HTMLResponse(content=fallback_html, status_code=200)

@app.get("/health")
def health():
    """Dedicated health check endpoint for Coolify / Traefik proxy."""
    return JSONResponse(
        content={
            "status": "online",
            "service": "Kaamdhenu AI 3.0",
            "health": "ok"
        },
        status_code=200
    )

# ─── Settings API ────────────────────────────────────────────────────────────

@app.get("/api/settings")
def get_settings():
    """Returns merged settings from database cache and current environment variables (.env).
    Cached (user-saved) values take priority over env defaults."""
    def _get(key, default=""):
        return _settings_cache.get(key) or os.getenv(key, default)

    return JSONResponse(content={
        "LIVEKIT_URL": _get("LIVEKIT_URL"),
        "LIVEKIT_API_KEY": _get("LIVEKIT_API_KEY"),
        "LIVEKIT_API_SECRET": _get("LIVEKIT_API_SECRET"),
        "GOOGLE_API_KEY": _get("GOOGLE_API_KEY"),
        "GEMINI_MODEL": _get("GEMINI_MODEL", "gemini-2.0-flash-exp"),
        "GEMINI_TTS_VOICE": _get("GEMINI_TTS_VOICE", "hi-IN-Neural2-A"),
        "USE_GEMINI_REALTIME": _get("USE_GEMINI_REALTIME", "true"),
        "DEEPGRAM_API_KEY": _get("DEEPGRAM_API_KEY"),
        "VOBIZ_SIP_DOMAIN": _get("VOBIZ_SIP_DOMAIN", "1d3f4bb4.sip.vobiz.ai"),
        "VOBIZ_USERNAME": _get("VOBIZ_USERNAME"),
        "VOBIZ_PASSWORD": _get("VOBIZ_PASSWORD"),
        "VOBIZ_OUTBOUND_NUMBER": _get("VOBIZ_OUTBOUND_NUMBER"),
        "OUTBOUND_TRUNK_ID": _get("OUTBOUND_TRUNK_ID", "ST_5fBqM5ZaW7pn"),
    }, status_code=200)

@app.post("/api/settings")
async def save_settings(request: Request):
    """Accepts partial or full settings JSON payloads, merges into cache, and persists to disk."""
    try:
        data = await request.json()
        # Merge incoming keys into cache (only non-empty values)
        for key, value in data.items():
            if value is not None and str(value).strip():
                _settings_cache[key] = str(value).strip()
                # Also set in current process env so agent.py picks it up
                os.environ[key] = str(value).strip()
        _save_settings_to_disk()
        logger.info(f"Settings updated: {list(data.keys())}")
        return JSONResponse(content={"success": True, "updated_keys": list(data.keys())}, status_code=200)
    except Exception as e:
        logger.error(f"Error saving settings: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

# ─── Stats API (stub) ────────────────────────────────────────────────────────

@app.get("/api/stats")
def get_stats():
    return JSONResponse(content={
        "total_calls": 0,
        "booked": 0,
        "not_interested": 0,
        "booking_rate": 0,
        "avg_duration": 0,
        "total_spent_inr": 0
    })

# ─── Calls API (stub) ────────────────────────────────────────────────────────

@app.get("/api/calls")
def get_calls(direction: str = "outbound"):
    return JSONResponse(content=[])

# ─── Logs API (stub) ─────────────────────────────────────────────────────────

@app.get("/api/logs")
def get_logs(limit: int = 50):
    return JSONResponse(content=[])

@app.delete("/api/logs")
def delete_logs():
    return JSONResponse(content={"success": True})

# ─── Appointments API (stub) ─────────────────────────────────────────────────

@app.get("/api/appointments")
def get_appointments():
    return JSONResponse(content=[])

@app.delete("/api/appointments/{appointment_id}")
def delete_appointment(appointment_id: str):
    return JSONResponse(content={"success": True})

# ─── Client Numbers API (stub) ───────────────────────────────────────────────

@app.get("/api/client-numbers")
def get_client_numbers():
    return JSONResponse(content=[])

@app.post("/api/client-numbers")
async def add_client_number(request: Request):
    data = await request.json()
    logger.info(f"Client number added: {data}")
    return JSONResponse(content={"success": True})

@app.delete("/api/client-numbers/{client_id}")
def delete_client_number(client_id: str):
    return JSONResponse(content={"success": True})

# ─── Prompt API (stub) ───────────────────────────────────────────────────────

@app.get("/api/prompt")
def get_prompt():
    return JSONResponse(content={"prompt": _settings_cache.get("SYSTEM_PROMPT", "")})

@app.post("/api/prompt")
async def save_prompt(request: Request):
    data = await request.json()
    _settings_cache["SYSTEM_PROMPT"] = data.get("prompt", "")
    _save_settings_to_disk()
    return JSONResponse(content={"success": True})

# ─── Call Dispatch API ───────────────────────────────────────────────────────

@app.post("/api/call")
async def dispatch_single_call(request: Request):
    """Dispatch a single outbound call from dashboard."""
    try:
        data = await request.json()
        phone_number = data.get("phone")
        if not phone_number:
            return JSONResponse(content={"error": "Phone number is required"}, status_code=400)
        room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"
        logger.info(f"Dispatching call to {phone_number} in room {room_name}")
        return JSONResponse(content={
            "success": True,
            "roomName": room_name,
            "dispatchId": f"outbound-{phone_number.replace('+', '')}-{random.randint(100, 999)}"
        }, status_code=200)
    except Exception as e:
        logger.error(f"Error in call dispatch: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.post("/api/dispatch")
async def dispatch_call_api(request: Request):
    """Legacy API endpoint for dashboard dispatch compatibility."""
    try:
        data = await request.json()
        phone_number = data.get("phoneNumber")
        if not phone_number:
            return JSONResponse(content={"error": "Phone number is required"}, status_code=400)
        room_name = f"call-{phone_number.replace('+', '')}-{random.randint(1000, 9999)}"
        logger.info(f"Dispatching call to {phone_number} in room {room_name}")
        return JSONResponse(content={
            "success": True,
            "roomName": room_name,
            "dispatchId": f"outbound-{phone_number.replace('+', '')}-{random.randint(100, 999)}"
        }, status_code=200)
    except Exception as e:
        logger.error(f"Error in API dispatch: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
