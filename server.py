import asyncio
import io
import csv
import json
import logging
import os
import random
import re
import ssl
import time
import urllib.request
import urllib.error
try:
    import httpx
except ImportError:
    httpx = None
import certifi
import aiohttp
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from db import (
    push_unified_log, get_unified_logs, clear_unified_logs,
    list_client_numbers, save_client_number, delete_client_number,
    get_calls, get_stats_data, get_all_appointments, cancel_appointment,
    get_settings, save_settings_dict, get_contact_memory,
    list_agent_profiles, save_agent_profile, delete_agent_profile, get_agent_profile, find_agent_profile,
    list_campaigns, create_campaign, update_campaign_status, find_campaign_by_inbound_number,
    get_pending_callbacks, mark_callback_dispatched, get_pending_callbacks_due, mark_callback_completed,
    get_and_claim_due_callbacks, emergency_cleanup_pending_callbacks,
    get_due_callbacks_epoch, claim_due_callback, normalize_phone,
    get_campaign, update_campaign, list_whatsapp_logs, insert_whatsapp_log, find_recent_outbound_context
)
from whatsapp_service import (
    send_text_message, send_document_message, send_appointment_confirmation,
    generate_whatsapp_ai_response, format_whatsapp_phone, WHATSAPP_VERIFY_TOKEN
)
from prompts import get_base_system_prompt, GLOBAL_NATURAL_CONVERSATION_LAYER

load_dotenv(".env", override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

app = FastAPI(title="Kaamdhenu 3.0 Voice Platform", version="3.0.0")

# Mount brochures static directory
BROCHURE_DIR = Path(__file__).parent / "brochures"
BROCHURE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/brochures", StaticFiles(directory=str(BROCHURE_DIR)), name="brochures")

STATIC_BROCHURE_DIR = Path(__file__).parent / "static" / "brochures"
STATIC_BROCHURE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/brochures", StaticFiles(directory=str(STATIC_BROCHURE_DIR)), name="static_brochures")

# Mount recordings static directory
RECORDINGS_DIR = Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")

STATIC_RECORDINGS_DIR = Path(__file__).parent / "static" / "recordings"
STATIC_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static/recordings", StaticFiles(directory=str(STATIC_RECORDINGS_DIR)), name="static_recordings")

class SingleCallReq(BaseModel):
    phone: Optional[str] = None
    phone_number: Optional[str] = None
    lead_name: str = "there"
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_voice: Optional[str] = None
    business_name: Optional[str] = None
    service_type: Optional[str] = None
    broker_phone: Optional[str] = None
    broker_email: Optional[str] = None
    calcom_event_type_id: Optional[str] = None
    system_prompt: Optional[str] = None
    custom_prompt: Optional[str] = None
    notes: Optional[str] = None
    bhk_requirement: Optional[str] = None
    budget: Optional[str] = None
    campaign_id: Optional[str] = None
    project_name: Optional[str] = None
    brochure_url: Optional[str] = None
    site_address: Optional[str] = None
    pickup_drop_notes: Optional[str] = None
    project_highlights: Optional[str] = None

class ClientNumReq(BaseModel):
    id: Optional[str] = None
    inbound_number: str
    business_name: str
    service_type: str
    agent_name: str = "Riya"
    broker_whatsapp_number: Optional[str] = None
    system_prompt: Optional[str] = None

# ============================================================
# AUTOMATED CALLBACK SCHEDULER WORKER
# ============================================================
def is_calling_hours_ist() -> bool:
    """
    TRAI Legal Calling Hours Guard:
    Only 09:30 AM to 08:00 PM IST allowed (09:30 to 20:00).
    Outside this window, NO automated outbound calls may be placed.
    """
    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
    except Exception:
        from datetime import timezone, timedelta
        IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    minute_of_day = now_ist.hour * 60 + now_ist.minute
    # 09:30 AM = 570 minutes, 08:00 PM (20:00) = 1200 minutes
    return (9 * 60 + 30) <= minute_of_day < (20 * 60)

_recent_callback_dials: Dict[str, int] = {}
CALLBACK_COOLDOWN_SECONDS = 12 * 3600  # 12-hour deduplication window

def parse_callback_datetime(cb_str: str, now: Optional[datetime] = None) -> Optional[datetime]:
    if not cb_str or not str(cb_str).strip():
        return None
    s = str(cb_str).strip().lower()
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    import re
    try:
        IST = ZoneInfo("Asia/Kolkata")
    except Exception:
        from datetime import timezone
        IST = timezone(_td(hours=5, minutes=30))

    if now is None:
        now = _dt.now(IST)

    # 1. Standard ISO or date formats
    try:
        clean_s = str(cb_str).strip().replace("Z", "+00:00")
        parsed = _dt.fromisoformat(clean_s)
        if parsed.tzinfo is not None:
            return parsed.astimezone(IST)
        else:
            return parsed.replace(tzinfo=IST)
    except Exception:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.strptime(str(cb_str).strip(), fmt).replace(tzinfo=IST)
        except ValueError:
            pass

    # 2. Relative offset "in X minutes/hours"
    m_in = re.search(r'in\s+(\d+)\s*(hour|hr|minute|min)', s)
    if m_in:
        val = int(m_in.group(1))
        unit = m_in.group(2)
        if "h" in unit:
            return now + _td(hours=val)
        else:
            return now + _td(minutes=val)

    # 3. Base date determination
    base_date = now.date()
    if "parson" in s or "day after tomorrow" in s:
        base_date = now.date() + _td(days=2)
    elif "tomorrow" in s or "kal" in s:
        base_date = now.date() + _td(days=1)
    elif "today" in s or "aaj" in s:
        base_date = now.date()
    else:
        weekdays = {
            "monday": 0, "somwar": 0,
            "tuesday": 1, "mangalwar": 1,
            "wednesday": 2, "budhwar": 2,
            "thursday": 3, "guruwar": 3,
            "friday": 4, "shukrawar": 4,
            "saturday": 5, "shanivar": 5, "shaniwar": 5,
            "sunday": 6, "ravivar": 6, "itwar": 6
        }
        for wname, wday in weekdays.items():
            if wname in s:
                days_ahead = (wday - now.weekday()) % 7
                if days_ahead <= 0:
                    days_ahead = 7
                base_date = now.date() + _td(days=days_ahead)
                break

    # 4. Extract time
    hour = 18
    minute = 0
    m_t12 = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', s)
    m_t24 = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', s)
    m_baje = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(?:baje)', s)

    if m_t12:
        hour = int(m_t12.group(1))
        minute = int(m_t12.group(2) or 0)
        ampm = m_t12.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
    elif m_t24:
        hour = int(m_t24.group(1))
        minute = int(m_t24.group(2))
    elif m_baje:
        hour = int(m_baje.group(1))
        minute = int(m_baje.group(2) or 0)
        if ("shaam" in s or "dopahar" in s or "raat" in s) and hour < 12:
            hour += 12
        elif hour in (1, 2, 3, 4, 5, 6, 7, 8):
            hour += 12

    try:
        combined = _dt.combine(base_date, _dt.min.time().replace(hour=hour, minute=minute))
        return combined.replace(tzinfo=IST)
    except Exception:
        return None

async def dispatch_callback_call(
    phone: str,
    lead_name: str = "there",
    campaign_id: Optional[str] = None,
    orig_call_id: Optional[str] = None,
    notes: str = ""
):
    try:
        url = os.getenv("LIVEKIT_URL")
        key = os.getenv("LIVEKIT_API_KEY")
        secret = os.getenv("LIVEKIT_API_SECRET")
        if not (url and key and secret):
            logger.warning(f"Cannot dispatch callback to {phone}: LiveKit credentials missing")
            return

        profile = await find_agent_profile(campaign_id=campaign_id)
        agent_id = profile.get("id") or None
        agent_name = profile.get("name") or profile.get("agent_name") or "Riya"
        agent_voice = profile.get("voice") or os.getenv("GEMINI_TTS_VOICE", "Aoede")
        business_name = profile.get("business_name") or "Kaamdhenu Real Estate"
        service_type = profile.get("service_type") or "Luxury 2BHK/3BHK Apartments"
        broker_phone = profile.get("broker_phone") or profile.get("broker_whatsapp") or os.getenv("DEFAULT_BROKER_PHONE", "+919892057717")
        broker_email = profile.get("broker_email") or os.getenv("DEFAULT_BROKER_EMAIL", "")
        calcom_event_type_id = profile.get("calcom_event_type_id") or os.getenv("CALCOM_EVENT_TYPE_ID", "6934775")

        room_name = f"callback-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
        custom_ctx = "Context: This is a scheduled callback requested by the lead earlier.\n"
        if notes:
            custom_ctx += f"Additional Notes: {notes}\n"
        custom_ctx += (
            f"\n[SCHEDULED CALLBACK BEHAVIOR - STRICT LISTENING MODE]\n"
            f"1. OPENING:\n"
            f"   - Keep it short, crisp, and direct:\n"
            f"     'Namaste {lead_name} ji, {agent_name} baat kar rahi hoon {business_name} se. Aapne call karne ko kaha tha.'\n"
            f"   - Immediately pause and LET THE USER SPEAK. Do not pitch immediately.\n"
            f"2. IF USER TALKS NORMALLY:\n"
            f"   - Answer their questions and continue the property qualification smoothly.\n"
            f"   - NEVER suggest or ask: 'Main aapko baad mein call karoon kya?' Keep your focus on the conversation.\n"
            f"3. IF USER SAYS THEY ARE STILL BUSY / RESCHEDULES:\n"
            f"   - Only if the USER explicitly asks (e.g., 'Abhi bhi busy hoon, shaam ko 6 baje karo' / 'Kal call karo'):\n"
            f"     Acknowledge politely: 'Theek hai sir/ma'am, main aapko [time] par call karti hoon.'\n"
            f"     Call the `schedule_callback` tool with the requested time and end the call respectfully.\n"
            f"   - Do NOT interrogate or push.\n"
        )

        prompt = get_base_system_prompt(
            agent_name=agent_name,
            business_name=business_name,
            custom_prompt=custom_ctx,
            lead_name=lead_name,
            service_type=service_type
        )

        meta = {
            "direction": "outbound",
            "log_category": "campaign_callback",
            "campaign_id": campaign_id,
            "phone_number": phone,
            "lead_name": lead_name,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "agent_voice": agent_voice,
            "voice": agent_voice,
            "business_name": business_name,
            "service_type": service_type,
            "broker_phone": broker_phone,
            "broker_email": broker_email,
            "calcom_event_type_id": calcom_event_type_id,
            "system_prompt": prompt,
            "notes": f"Scheduled callback requested earlier. {notes}".strip()
        }

        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx)) as session:
            lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
            await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300))
            await lk.agent_dispatch.create_dispatch(
                lk_api.CreateAgentDispatchRequest(agent_name="kaamdhenu-voice-agent", room=room_name, metadata=json.dumps(meta))
            )
            await lk.aclose()

        await push_unified_log("Callback", "info", f"📞 Outbound scheduled callback dialed for {phone} (Room: {room_name})", call_id=room_name)
    except Exception as e:
        logger.error(f"Failed to dispatch callback call to {phone}: {e}")
        await push_unified_log("Callback", "error", f"Failed to dispatch callback to {phone}: {e}", call_id=orig_call_id)

async def check_and_dispatch_due_callbacks():
    # 0. TRAI Legal Calling Hours Guard (09:30 AM to 08:00 PM IST)
    if not is_calling_hours_ist():
        logger.debug("Outside TRAI calling hours (09:30 AM - 08:00 PM IST). Callback dispatch paused.")
        return

    now_epoch = int(time.time())

    # 1. Process due callbacks atomically from scheduled_callbacks table
    try:
        due_cbs = await get_due_callbacks_epoch(now_epoch)
        for cb in due_cbs:
            cid = str(cb.get("id"))
            phone = str(cb.get("phone") or "")
            lead_name = cb.get("lead_name") or "there"
            notes = cb.get("context_notes") or ""
            time_str = cb.get("scheduled_time", "")

            norm_p = normalize_phone(phone)
            if not norm_p:
                await mark_callback_completed(cid, status="cancelled")
                continue

            # In-memory Cooldown / Deduplication check (12 hours)
            last_dialed = _recent_callback_dials.get(norm_p, 0)
            if (now_epoch - last_dialed) < CALLBACK_COOLDOWN_SECONDS:
                logger.info(f"Skipping duplicate callback to {norm_p}; dialed {now_epoch - last_dialed}s ago (<12h cooldown)")
                await mark_callback_completed(cid, status="skipped_cooldown")
                continue

            # Atomic single-shot claim lock: updates status to 'in_progress'
            claimed = await claim_due_callback(cid)
            if not claimed:
                # Already claimed by another worker or not pending
                continue

            # Record dial timestamp in cooldown tracker
            _recent_callback_dials[norm_p] = now_epoch

            await push_unified_log("Callback", "info", f"📞 Automated scheduled callback triggered for {phone} (Scheduled: {time_str})", call_id=cid)

            # Dispatch outbound call
            try:
                await dispatch_callback_call(
                    phone=phone,
                    lead_name=lead_name,
                    orig_call_id=cid,
                    notes=f"This is a scheduled callback requested by the lead earlier. Notes: {notes}".strip()
                )
                await mark_callback_completed(cid, status="completed")
            except Exception as disp_err:
                logger.error(f"Error dispatching callback to {phone}: {disp_err}")
                await mark_callback_completed(cid, status="failed")

    except Exception as e:
        logger.error(f"Error checking due scheduled_callbacks: {e}")

    # 2. Backwards-compatible check on legacy call_logs.next_callback
    try:
        from zoneinfo import ZoneInfo
        try:
            IST = ZoneInfo("Asia/Kolkata")
        except Exception:
            from datetime import timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
        now_ist_dt = datetime.now(IST)

        pending = await get_pending_callbacks()
        for call in pending:
            cb_str = call.get("next_callback")
            if not cb_str:
                continue
            scheduled_dt = parse_callback_datetime(cb_str, now=now_ist_dt)
            if not scheduled_dt:
                continue

            if now_ist_dt >= scheduled_dt:
                cid = call.get("id")
                phone = call.get("phone_number")
                lead_name = call.get("lead_name") or call.get("client_name") or "there"
                campaign_id = call.get("campaign_id")

                norm_p = normalize_phone(phone)
                last_dialed = _recent_callback_dials.get(norm_p, 0)
                if (now_epoch - last_dialed) < CALLBACK_COOLDOWN_SECONDS:
                    await mark_callback_dispatched(cid)
                    continue

                # Mark callback_dispatched = TRUE to avoid duplicate dialing
                await mark_callback_dispatched(cid)
                _recent_callback_dials[norm_p] = now_epoch

                time_str = scheduled_dt.strftime("%Y-%m-%d %H:%M")
                await push_unified_log("Callback", "info", f"📞 Automated scheduled callback triggered for {phone} at {time_str}", call_id=cid)

                # Dispatch outbound call via LiveKit SIP
                asyncio.create_task(dispatch_callback_call(
                    phone=phone,
                    lead_name=lead_name,
                    campaign_id=campaign_id,
                    orig_call_id=cid,
                    notes="This is a scheduled callback requested by the lead earlier."
                ))
    except Exception as legacy_err:
        logger.error(f"Error checking legacy call_logs callbacks: {legacy_err}")

async def callback_scheduler_worker():
    while True:
        try:
            await check_and_dispatch_due_callbacks()
        except Exception as e:
            logger.error(f"Callback scheduler error: {e}")
        await asyncio.sleep(30)

@app.on_event("startup")
async def on_startup():
    try:
        await emergency_cleanup_pending_callbacks()
    except Exception as clean_err:
        logger.warning(f"Startup callback cleanup warning: {clean_err}")
    asyncio.create_task(callback_scheduler_worker())
    logger.info("Automated callback scheduler worker started (interval: 30s)")

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    p = Path(__file__).parent / "ui" / "index.html"
    return HTMLResponse(content=p.read_text(encoding="utf-8"), status_code=200) if p.exists() else HTMLResponse("<h1>UI file missing</h1>", 404)

@app.get("/health")
async def health():
    return {"status": "online", "service": "Kaamdhenu AI 3.0", "livekit_url": os.getenv("LIVEKIT_URL", "")}

def enrich_recording_url(item: dict) -> dict:
    """Ensure recording_url is populated only if file actually exists; never synthesize broken URLs."""
    raw_url = item.get("recording_url")
    if raw_url and str(raw_url).strip() not in ("", "None", "null", "-"):
        item["recording_url"] = str(raw_url).strip()
        return item

    # Check local filesystem in RECORDINGS_DIR or STATIC_RECORDINGS_DIR
    cid = item.get("id") or item.get("call_id") or ""
    if cid:
        for ext in (".mp3", ".wav", ".mp4", ".ogg", ".webm"):
            rec_path = RECORDINGS_DIR / f"{cid}{ext}"
            if rec_path.exists():
                item["recording_url"] = f"/recordings/{cid}{ext}"
                return item
            static_rec_path = STATIC_RECORDINGS_DIR / f"{cid}{ext}"
            if static_rec_path.exists():
                item["recording_url"] = f"/static/recordings/{cid}{ext}"
                return item

    item["recording_url"] = None
    return item

@app.get("/api/stats")
async def api_stats():
    return await get_stats_data()

@app.get("/api/logs")
async def api_logs(limit: int = 150, level: str = "all", source: str = "all"):
    logs = await get_unified_logs(limit=limit, level=level, source=source)
    return [enrich_recording_url(l) for l in logs]

@app.delete("/api/logs")
async def api_clear_logs():
    await clear_unified_logs()
    return {"status": "cleared"}

@app.get("/api/calls")
async def api_calls(direction: Optional[str] = None, campaign_id: Optional[str] = None):
    try:
        calls = await get_calls(direction=direction, campaign_id=campaign_id)
        return [enrich_recording_url(c) for c in calls]
    except Exception as e:
        logger.error(f"Error fetching calls: {e}")
        return []

@app.get("/api/crm")
async def api_crm(phone: Optional[str] = None):
    return await get_contact_memory(phone=phone)

@app.get("/api/campaigns/{campaign_id}/export")
async def export_campaign_csv(campaign_id: str, request: Request):
    calls = await get_calls(campaign_id=campaign_id, limit=1000)
    calls = [enrich_recording_url(c) for c in calls]
    base_url = str(request.base_url).rstrip("/")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date & Time", "Lead Name", "Phone Number", "Lead Score", "Timeline",
        "Funding / Loan", "BHK", "Budget", "Outcome", "Summary",
        "Duration (s)", "Cost (INR)", "Recording_Link"
    ])
    for c in calls:
        rec = c.get("recording_url") or "-"
        if rec != "-" and rec.startswith("/"):
            rec = f"{base_url}{rec}"
        writer.writerow([
            c.get("timestamp", ""), c.get("lead_name", ""), c.get("phone_number", ""),
            c.get("lead_score", "Cold"),
            c.get("timeline") or c.get("possession_timeline") or "-",
            c.get("funding_type") or "-",
            c.get("bhk_preference") or c.get("bhk_requirement") or "-",
            c.get("budget_range") or c.get("budget") or "-",
            c.get("outcome", ""), c.get("summary", ""),
            c.get("duration_seconds", 0), c.get("cost_inr", 0.0),
            rec
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=campaign_{campaign_id[:8]}_report.csv"}
    )

@app.get("/api/appointments")
async def api_appointments():
    return await get_all_appointments()

@app.delete("/api/appointments/{aid}")
@app.post("/api/appointments/cancel/{aid}")
async def api_cancel_app(aid: str):
    await cancel_appointment(aid)
    return {"status": "cancelled", "id": aid}

@app.get("/api/callbacks")
async def api_callbacks():
    return await get_pending_callbacks_due()

@app.post("/api/callbacks/{cid}/cancel")
async def api_cancel_callback(cid: str):
    await mark_callback_completed(cid, status="cancelled")
    return {"status": "cancelled", "id": cid}

@app.get("/api/client-numbers")
async def api_list_clients():
    return await list_client_numbers()

@app.post("/api/client-numbers")
async def api_save_client(req: ClientNumReq):
    cid = await save_client_number(req.dict())
    return {"status": "saved", "id": cid}

@app.delete("/api/client-numbers/{cid}")
async def api_del_client(cid: str):
    await delete_client_number(cid)
    return {"status": "deleted"}

@app.get("/api/settings")
async def api_get_settings():
    db_s = await get_settings()
    return {
        "LIVEKIT_URL": db_s.get("LIVEKIT_URL") or os.getenv("LIVEKIT_URL", ""),
        "LIVEKIT_API_KEY": db_s.get("LIVEKIT_API_KEY") or os.getenv("LIVEKIT_API_KEY", ""),
        "LIVEKIT_API_SECRET": db_s.get("LIVEKIT_API_SECRET") or os.getenv("LIVEKIT_API_SECRET", ""),
        "GOOGLE_API_KEY": db_s.get("GOOGLE_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
        "GEMINI_MODEL": db_s.get("GEMINI_MODEL") or os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp"),
        "GEMINI_TTS_VOICE": db_s.get("GEMINI_TTS_VOICE") or os.getenv("GEMINI_TTS_VOICE", "Aoede"),
        "VOICE_ENGINE": db_s.get("VOICE_ENGINE") or os.getenv("VOICE_ENGINE", "realtime"),
        "USE_GEMINI_REALTIME": db_s.get("USE_GEMINI_REALTIME") or os.getenv("USE_GEMINI_REALTIME", "true"),
        "DEEPGRAM_API_KEY": db_s.get("DEEPGRAM_API_KEY") or os.getenv("DEEPGRAM_API_KEY", ""),
        "STT_MODEL": db_s.get("STT_MODEL") or os.getenv("STT_MODEL", "nova-3"),
        "VOBIZ_SIP_DOMAIN": db_s.get("VOBIZ_SIP_DOMAIN") or os.getenv("VOBIZ_SIP_DOMAIN", "1d3f4bb4.sip.vobiz.ai"),
        "VOBIZ_USERNAME": db_s.get("VOBIZ_USERNAME") or os.getenv("VOBIZ_USERNAME", ""),
        "VOBIZ_PASSWORD": db_s.get("VOBIZ_PASSWORD") or os.getenv("VOBIZ_PASSWORD", ""),
        "VOBIZ_OUTBOUND_NUMBER": db_s.get("VOBIZ_OUTBOUND_NUMBER") or os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
        "OUTBOUND_TRUNK_ID": db_s.get("OUTBOUND_TRUNK_ID") or os.getenv("OUTBOUND_TRUNK_ID", "ST_5fBqM5ZaW7pn"),
        "INBOUND_TRUNK_ID": db_s.get("INBOUND_TRUNK_ID") or os.getenv("INBOUND_TRUNK_ID", ""),
        "INBOUND_DISPATCH_RULE_ID": db_s.get("INBOUND_DISPATCH_RULE_ID") or os.getenv("INBOUND_DISPATCH_RULE_ID", ""),
        "VOBIZ_API_URL": db_s.get("VOBIZ_API_URL") or os.getenv("VOBIZ_API_URL", "https://api.vobiz.ai/v1"),
        "CALCOM_API_KEY": f"{(db_s.get('CALCOM_API_KEY') or os.getenv('CALCOM_API_KEY', ''))[:8]}...{(db_s.get('CALCOM_API_KEY') or os.getenv('CALCOM_API_KEY', ''))[-4:]}" if len(db_s.get('CALCOM_API_KEY') or os.getenv('CALCOM_API_KEY', '')) > 12 else ("..." if (db_s.get('CALCOM_API_KEY') or os.getenv('CALCOM_API_KEY', '')) else ""),
        "CALCOM_HAS_KEY": bool(db_s.get("CALCOM_API_KEY") or os.getenv("CALCOM_API_KEY")),
        "CALCOM_EVENT_TYPE_ID": db_s.get("CALCOM_EVENT_TYPE_ID") or os.getenv("CALCOM_EVENT_TYPE_ID", "6934775"),
        "CALCOM_TIMEZONE": db_s.get("CALCOM_TIMEZONE") or os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
    }

@app.post("/api/settings")
async def api_save_settings(req: Request):
    d = await req.json()
    cleaned = {}
    for k, v in d.items():
        if k == "CALCOM_API_KEY" and ("..." in str(v) or not str(v).strip()):
            continue
        cleaned[k] = v
        os.environ[k] = str(v)
    if cleaned:
        await save_settings_dict(cleaned)
    return {"status": "saved"}

@app.post("/api/sip/create-outbound-trunk")
async def api_create_outbound_trunk():
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    domain = os.getenv("VOBIZ_SIP_DOMAIN")
    user = os.getenv("VOBIZ_USERNAME")
    pwd = os.getenv("VOBIZ_PASSWORD")
    num = os.getenv("VOBIZ_OUTBOUND_NUMBER")
    
    if not (url and key and secret and domain and user and pwd and num):
        raise HTTPException(400, "Missing SIP or LiveKit credentials in environment.")
    
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        trunk = await lk.sip.create_sip_outbound_trunk(
            lk_api.CreateSIPOutboundTrunkRequest(
                trunk=lk_api.SIPOutboundTrunkInfo(
                    name="Vobiz Outbound Trunk",
                    address=domain,
                    transport=lk_api.SIPTransport.SIP_TRANSPORT_AUTO,
                    numbers=[num],
                    auth_username=user,
                    auth_password=pwd
                )
            )
        )
        await lk.aclose()
        await session.close()
        
        trunk_id = trunk.sip_trunk_id
        await save_settings_dict({"OUTBOUND_TRUNK_ID": trunk_id})
        os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        await push_unified_log("SIP", "info", f"Outbound Trunk created: {trunk_id}")
        return {"status": "created", "trunk_id": trunk_id}
    except Exception as e:
        await push_unified_log("SIP", "error", f"Create Outbound Trunk failed: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/sip/provision-inbound")
async def api_provision_inbound():
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    domain = os.getenv("VOBIZ_SIP_DOMAIN")
    num = os.getenv("VOBIZ_OUTBOUND_NUMBER")
    
    # Check if known trunk exists in env/settings
    current_in_trunk = os.getenv("INBOUND_TRUNK_ID", "ST_Ua5ypLzBtQP3")
    current_rule = os.getenv("INBOUND_DISPATCH_RULE_ID", "SDR_mfgEwEcxt3u2")
    
    if not (url and key and secret and num):
        raise HTTPException(400, "Missing LiveKit URL/Keys or Vobiz Number.")
        
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        in_trunk_id = current_in_trunk
        rule_id = current_rule
        
        # Safe list check with correct LiveKit SDK method naming (singular 'trunk')
        try:
            list_fn = getattr(lk.sip, "list_sip_inbound_trunk", None) or getattr(lk.sip, "list_sip_inbound_trunks", None)
            if list_fn:
                req_cls = getattr(lk_api, "ListSIPInboundTrunkRequest", None) or getattr(lk_api, "ListSIPInboundTrunksRequest", None)
                res = await list_fn(req_cls() if req_cls else None)
                for t in getattr(res, "items", []):
                    if num in t.numbers or (t.numbers and any(n.replace("+", "") in num for n in t.numbers)):
                        in_trunk_id = t.sip_trunk_id
                        break
        except Exception as scan_err:
            logger.warning(f"Trunk listing bypassed: {scan_err}")

        # If still missing, create inbound trunk
        if not in_trunk_id:
            in_trunk = await lk.sip.create_sip_inbound_trunk(
                lk_api.CreateSIPInboundTrunkRequest(
                    trunk=lk_api.SIPInboundTrunkInfo(
                        name="Vobiz Inbound Trunk",
                        numbers=[num],
                        allowed_addresses=[domain] if domain else []
                    )
                )
            )
            in_trunk_id = in_trunk.sip_trunk_id

        await lk.aclose()
        await session.close()
        
        await save_settings_dict({
            "INBOUND_TRUNK_ID": in_trunk_id,
            "INBOUND_DISPATCH_RULE_ID": rule_id
        })
        os.environ["INBOUND_TRUNK_ID"] = in_trunk_id
        os.environ["INBOUND_DISPATCH_RULE_ID"] = rule_id
        
        await push_unified_log("SIP", "info", f"Inbound linked: Trunk={in_trunk_id}, Rule={rule_id}")
        return {"status": "provisioned", "inbound_trunk_id": in_trunk_id, "dispatch_rule_id": rule_id}
    except Exception as e:
        await push_unified_log("SIP", "error", f"Inbound provisioning error: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/call/single")
@app.post("/api/dispatch-call")
@app.post("/api/call")
async def api_dispatch(req: SingleCallReq):
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        raise HTTPException(400, "LiveKit credentials missing")

    phone = (req.phone or req.phone_number or "").strip()
    if not phone:
        raise HTTPException(400, "Phone number required")

    campaign_id = req.campaign_id or None

    # 1. Resolve agent profile dynamically (supports custom saved profile, agent_id, agent_name, or default 'Riya')
    profile = await find_agent_profile(
        agent_id=req.agent_id,
        agent_name=req.agent_name,
        campaign_id=campaign_id
    )

    agent_id = req.agent_id or profile.get("id") or "default"
    agent_name = req.agent_name or profile.get("name") or profile.get("agent_name") or "Riya"
    agent_voice = req.agent_voice or profile.get("voice") or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    business_name = req.business_name or profile.get("business_name") or "Kaamdhenu Real Estate"
    service_type = req.service_type or profile.get("service_type") or "Luxury Properties"
    broker_phone = req.broker_phone or profile.get("broker_phone") or profile.get("broker_whatsapp") or os.getenv("DEFAULT_BROKER_PHONE", "+919892057717")
    broker_email = req.broker_email or profile.get("broker_email") or os.getenv("DEFAULT_BROKER_EMAIL", "")
    calcom_event_type_id = req.calcom_event_type_id or profile.get("calcom_event_type_id") or os.getenv("CALCOM_EVENT_TYPE_ID", "6934775")

    # Priority for system_prompt:
    # 1. Explicitly passed in single call form (req.custom_prompt or req.system_prompt)
    # 2. The agent profile's saved system_prompt
    raw_prompt = req.custom_prompt or req.system_prompt or profile.get("system_prompt") or ""

    project_name = req.project_name or business_name
    brochure_url = req.brochure_url or ""
    site_address = req.site_address or ""
    pickup_drop_notes = req.pickup_drop_notes or ""
    project_highlights = req.project_highlights or ""

    if campaign_id:
        camp = await get_campaign(campaign_id)
        if camp:
            project_name = req.project_name or camp.get("project_name") or camp.get("name") or project_name
            brochure_url = req.brochure_url or camp.get("brochure_url") or brochure_url
            site_address = req.site_address or camp.get("site_address") or site_address
            pickup_drop_notes = req.pickup_drop_notes or camp.get("pickup_drop_notes") or pickup_drop_notes
            project_highlights = req.project_highlights or camp.get("project_highlights") or project_highlights

    # Build final system prompt strictly incorporating the active agent's persona
    final_prompt = get_base_system_prompt(
        agent_name=agent_name,
        business_name=project_name or business_name,
        custom_prompt=raw_prompt,
        lead_name=req.lead_name or "there",
        service_type=service_type
    )

    room_name = f"outbound-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    meta = {
        "direction": "outbound",
        "phone_number": phone,
        "lead_name": req.lead_name or "there",
        "campaign_id": campaign_id or "",
        "project_name": project_name,
        "brochure_url": brochure_url,
        "site_address": site_address,
        "pickup_drop_notes": pickup_drop_notes,
        "project_highlights": project_highlights,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_voice": agent_voice,
        "voice": agent_voice,
        "business_name": business_name,
        "service_type": service_type,
        "broker_phone": broker_phone,
        "broker_email": broker_email,
        "calcom_event_type_id": calcom_event_type_id,
        "system_prompt": final_prompt,
        "notes": req.notes or "",
        "bhk": req.bhk_requirement or "",
        "budget": req.budget or ""
    }

    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300))
        await lk.agent_dispatch.create_dispatch(
            lk_api.CreateAgentDispatchRequest(agent_name="kaamdhenu-voice-agent", room=room_name, metadata=json.dumps(meta))
        )
        await lk.aclose()
        await session.close()
        await push_unified_log("API", "info", f"Call dispatched to {phone} ({agent_name})", call_id=room_name)
        return {"status": "dispatched", "room": room_name, "phone": phone, "agent": agent_name}
    except Exception as e:
        await push_unified_log("API", "error", f"Dispatch failed: {e}")
        raise HTTPException(500, str(e))

# Agent Profiles API
@app.get("/api/agent-profiles")
async def api_list_agents():
    return await list_agent_profiles()

@app.post("/api/agent-profiles")
async def api_save_agent(req: Request):
    try:
        content_type = req.headers.get("content-type", "")
        if "application/json" in content_type:
            data = await req.json()
        else:
            form = await req.form()
            data = dict(form)
    except Exception:
        data = {}

    saved_profile = await save_agent_profile(data)
    return JSONResponse(content=saved_profile, status_code=200)

@app.delete("/api/agent-profiles/{pid}")
async def api_del_agent(pid: str):
    await delete_agent_profile(pid)
    return {"status": "deleted", "id": pid}

# Campaign APIs
@app.get("/api/campaigns")
async def api_list_campaigns():
    return await list_campaigns()

@app.post("/api/campaigns")
async def api_create_campaign(req: Request):
    content_type = req.headers.get("content-type", "")
    if "application/json" in content_type:
        payload = await req.json()
        name = payload.get("name", "Untitled")
        agent_profile_id = payload.get("agent_profile_id", "")
        allocated_minutes = int(payload.get("allocated_minutes") or payload.get("camp_allocated_minutes") or 500)
        calling_mode = payload.get("calling_mode") or payload.get("calling_window", "regular")
        peak_start = payload.get("peak_start_time") or payload.get("peak_start", "18:00")
        peak_end = payload.get("peak_end_time") or payload.get("peak_end", "21:00")
        daily_limit = int(payload.get("daily_call_limit") or payload.get("daily_limit", 100))
        dedicated_inbound = payload.get("dedicated_inbound_number") or payload.get("dedicated_inbound", "")
        broker_email = payload.get("broker_email", "")
        calendar_mode = payload.get("calendar_mode", "auto")
        custom_event_id = payload.get("custom_event_id") or payload.get("calcom_event_type_id", "")
        contacts = payload.get("contacts", [])
        project_name = payload.get("project_name") or payload.get("camp_project_name") or name
        site_address = payload.get("site_address") or payload.get("camp_site_address") or ""
        pickup_drop_notes = payload.get("pickup_drop_notes") or payload.get("camp_pickup_notes") or ""
        project_highlights = payload.get("project_highlights") or payload.get("camp_highlights") or ""
        brochure_url = payload.get("brochure_url") or ""
    else:
        form = await req.form()
        name = form.get("name", "Untitled")
        agent_profile_id = form.get("agent_profile_id", "")
        allocated_minutes = int(form.get("allocated_minutes") or form.get("camp_allocated_minutes") or 500)
        calling_mode = form.get("calling_mode") or form.get("calling_window", "regular")
        peak_start = form.get("peak_start_time") or form.get("peak_start", "18:00")
        peak_end = form.get("peak_end_time") or form.get("peak_end", "21:00")
        daily_limit = int(form.get("daily_call_limit") or form.get("daily_limit", 100))
        dedicated_inbound = form.get("dedicated_inbound_number") or form.get("dedicated_inbound") or form.get("camp_inbound_num", "")
        broker_email = form.get("broker_email", "")
        calendar_mode = form.get("calendar_mode", "auto")
        custom_event_id = form.get("custom_event_id") or form.get("calcom_event_type_id", "")
        project_name = form.get("project_name") or form.get("camp_project_name") or name
        site_address = form.get("site_address") or form.get("camp_site_address") or ""
        pickup_drop_notes = form.get("pickup_drop_notes") or form.get("camp_pickup_notes") or ""
        project_highlights = form.get("project_highlights") or form.get("camp_highlights") or ""
        brochure_url = form.get("brochure_url") or ""

        # Auto-upload brochure file if provided in campaign creation form
        brochure_file = form.get("brochure_file") or form.get("camp_brochure_file")
        if brochure_file and hasattr(brochure_file, "filename") and brochure_file.filename and hasattr(brochure_file, "read"):
            try:
                b_bytes = await brochure_file.read()
                if b_bytes:
                    orig_bname = brochure_file.filename or "brochure.pdf"
                    clean_b = re.sub(r'[^a-zA-Z0-9._-]', '_', orig_bname)
                    stored_n = f"camp_{int(time.time())}_{clean_b}"
                    c_type = getattr(brochure_file, "content_type", "application/pdf") or "application/pdf"
                    uploaded_url = await upload_brochure_file(stored_n, b_bytes, c_type)
                    if uploaded_url:
                        brochure_url = uploaded_url
            except Exception as up_err:
                logger.warning(f"Failed to auto-upload campaign brochure: {up_err}")

        contacts_file = form.get("contacts_file")
        contacts = []
        if contacts_file and hasattr(contacts_file, 'read'):
            content = await contacts_file.read()
            text = content.decode('utf-8', errors='ignore')
            reader = csv.reader(io.StringIO(text))
            for row in reader:
                if len(row) >= 2:
                    contacts.append({"name": row[0].strip(), "phone": row[1].strip(), "notes": row[2].strip() if len(row) > 2 else ""})

    # Cal.com 1-Click Auto Event Provisioning
    calcom_api_key = os.getenv("CALCOM_API_KEY", "")
    event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "6934775")

    if calendar_mode == "auto" and calcom_api_key:
        try:
            import string
            clean_slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:20]
            rand_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
            slug = f"{clean_slug}-{rand_suffix}" if clean_slug else f"site-visit-{rand_suffix}"
            cal_url = f"https://api.cal.com/v1/event-types?apiKey={calcom_api_key}"
            cal_payload = {
                "title": f"{name} - Site Visit",
                "slug": slug,
                "length": 30,
                "description": f"Site visits for {name}"
            }
            if httpx is not None:
                async with httpx.AsyncClient(timeout=4.0) as client:
                    res = await client.post(cal_url, json=cal_payload)
                    if res.status_code in (200, 201):
                        res_json = res.json()
                        new_id = res_json.get("event_type", {}).get("id") or res_json.get("id")
                        if new_id:
                            event_type_id = str(new_id)
                            await push_unified_log("Cal.com", "info", f"Auto-created Cal.com event type '{slug}' (ID: {event_type_id}) for campaign '{name}'")
                    else:
                        logger.warning(f"Cal.com create event-type returned {res.status_code}: {res.text}")
            else:
                def _urllib_cal():
                    data_bytes = json.dumps(cal_payload).encode('utf-8')
                    req = urllib.request.Request(cal_url, data=data_bytes, headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=4.0) as resp:
                        return resp.getcode(), json.loads(resp.read().decode('utf-8'))
                try:
                    c_code, c_json = await asyncio.to_thread(_urllib_cal)
                    if c_code in (200, 201):
                        new_id = c_json.get("event_type", {}).get("id") or c_json.get("id")
                        if new_id:
                            event_type_id = str(new_id)
                            await push_unified_log("Cal.com", "info", f"Auto-created Cal.com event type '{slug}' (ID: {event_type_id}) for campaign '{name}'")
                except Exception as c_err:
                    logger.warning(f"Cal.com urllib creation fallback: {c_err}")
        except Exception as exc:
            logger.warning(f"Cal.com auto event-type creation fallback: {exc}")
            await push_unified_log("Cal.com", "warning", f"Cal.com auto event creation fallback: {exc}")
    elif calendar_mode == "manual" and custom_event_id:
        event_type_id = str(custom_event_id).strip()
    elif calendar_mode == "default":
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "6934775")

    calling_window = f"custom_peak:{peak_start}-{peak_end}" if calling_mode == "custom_peak" else "regular"

    data = {
        "name": name,
        "agent_profile_id": agent_profile_id,
        "allocated_minutes": allocated_minutes,
        "calling_window": calling_window,
        "calling_mode": calling_mode,
        "peak_start_time": peak_start,
        "peak_end_time": peak_end,
        "daily_limit": daily_limit,
        "daily_call_limit": daily_limit,
        "dedicated_inbound_number": dedicated_inbound,
        "broker_email": broker_email,
        "calendar_mode": calendar_mode,
        "calcom_event_type_id": str(event_type_id),
        "contacts": json.dumps(contacts),
        "total_contacts": len(contacts),
        "project_name": project_name or name,
        "site_address": site_address,
        "pickup_drop_notes": pickup_drop_notes,
        "project_highlights": project_highlights,
        "brochure_url": brochure_url
    }
    cid = await create_campaign(data)
    return {
        "status": "created",
        "id": cid,
        "total_contacts": len(contacts),
        "calcom_event_type_id": event_type_id,
        "brochure_url": brochure_url,
        "project_name": project_name
    }

@app.post("/api/campaigns/{cid}/pause")
async def api_pause_campaign(cid: str):
    await update_campaign_status(cid, "paused")
    return {"status": "paused"}

@app.post("/api/campaigns/{cid}/resume")
async def api_resume_campaign(cid: str):
    await update_campaign_status(cid, "active")
    return {"status": "resumed"}

@app.get("/api/campaigns/{cid}/logs")
async def api_campaign_logs(cid: str, category: str = "outbound"):
    c_id = None if cid == "all" else cid
    if category == "outbound":
        calls = await get_calls(direction="outbound", campaign_id=c_id, limit=200)
    elif category in ("callback", "callbacks"):
        calls = await get_calls(direction="inbound", campaign_id=c_id, limit=200)
        cb_calls = [c for c in calls if c.get("log_category") == "campaign_callback" or c.get("campaign_id")]
        calls = cb_calls if cb_calls else calls
    elif category in ("dedicated", "dedicated_inbound"):
        calls = await get_calls(direction="inbound", campaign_id=c_id, limit=200)
        ded_calls = [c for c in calls if c.get("log_category") == "dedicated_inbound"]
        calls = ded_calls if ded_calls else calls
    else:
        calls = await get_calls(campaign_id=c_id, limit=200)
    return [enrich_recording_url(c) for c in calls]

@app.get("/api/campaigns/{cid}/export-csv")
async def api_campaign_export(cid: str, request: Request, type: str = "full"):
    c_id = None if cid == "all" else cid
    calls = await get_calls(campaign_id=c_id, limit=5000)
    if type == "daily":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        calls = [c for c in calls if c.get("timestamp", "").startswith(today)]
    calls = [enrich_recording_url(c) for c in calls]
    base_url = str(request.base_url).rstrip("/")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Call Date", "Call Time", "Phone Number", "Lead Name", "Duration (s)",
        "Lead Score", "Timeline", "Loan / Funding", "BHK", "Budget",
        "Location", "Job / Occupation", "Site Visit & Cab", "Next Callback",
        "Main Objection", "WhatsApp Status", "AI Ground Summary", "Cost (INR)",
        "Recording_Link"
    ])
    for c in calls:
        ts = c.get("timestamp", "")
        call_date = ts[:10] if len(ts) >= 10 else "-"
        call_time = ts[11:16] if len(ts) >= 16 else "-"
        dur = c.get("duration_seconds", 0)
        phone = c.get("phone_number", "-")
        lead = c.get("client_name") or c.get("lead_name") or "-"
        score = c.get("lead_score", "Cold")
        tl = c.get("timeline") or c.get("possession_timeline") or "-"
        fund = c.get("funding_type") or "-"
        bhk = c.get("bhk_preference") or c.get("bhk_requirement") or "-"
        bud = c.get("budget_range") or c.get("budget") or "-"
        loc = c.get("location_preference") or c.get("current_location") or "-"
        job = c.get("job_profile") or c.get("occupation") or "-"
        visit_date = c.get("site_visit_date", "")
        pickup = "Yes - " + c.get("pickup_location", "") if c.get("pickup_required") else "No"
        visit_cab = f"{visit_date} (Cab: {pickup})" if visit_date else "-"
        cost_str = f"₹{c.get('cost_inr', 0.0)}"
        rec_link = c.get("recording_url") or "-"
        if rec_link != "-" and rec_link.startswith("/"):
            rec_link = f"{base_url}{rec_link}"

        writer.writerow([
            call_date, call_time, phone, lead, dur,
            score, tl, fund, bhk, bud,
            loc, job, visit_cab, c.get("next_callback", "-"),
            c.get("main_objection") or c.get("objection") or "-",
            c.get("whatsapp_status", "-"),
            c.get("summary", "-"), cost_str, rec_link
        ])
    output.seek(0)
    fname = f"campaign_{cid[:8] if cid != 'all' else 'all'}_{type}_report.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )

@app.get("/api/logs/export/csv")
async def api_export_logs_csv(request: Request, direction: Optional[str] = None, type: str = "full"):
    """Dedicated endpoint to export call logs with direct playback/download links."""
    calls = await get_calls(direction=direction, limit=5000)
    if type == "daily":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        calls = [c for c in calls if c.get("timestamp", "").startswith(today)]
    calls = [enrich_recording_url(c) for c in calls]
    base_url = str(request.base_url).rstrip("/")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Call Date", "Call Time", "Phone Number", "Lead Name", "Duration (s)",
        "Lead Score", "Timeline", "Loan / Funding", "BHK", "Budget",
        "Location", "Job / Occupation", "Site Visit & Cab", "Next Callback",
        "Main Objection", "WhatsApp Status", "AI Ground Summary", "Cost (INR)",
        "Recording_Link"
    ])
    for c in calls:
        ts = c.get("timestamp", "")
        call_date = ts[:10] if len(ts) >= 10 else "-"
        call_time = ts[11:16] if len(ts) >= 16 else "-"
        dur = c.get("duration_seconds", 0)
        phone = c.get("phone_number", "-")
        lead = c.get("client_name") or c.get("lead_name") or "-"
        score = c.get("lead_score", "Cold")
        tl = c.get("timeline") or c.get("possession_timeline") or "-"
        fund = c.get("funding_type") or "-"
        bhk = c.get("bhk_preference") or c.get("bhk_requirement") or "-"
        bud = c.get("budget_range") or c.get("budget") or "-"
        loc = c.get("location_preference") or c.get("current_location") or "-"
        job = c.get("job_profile") or c.get("occupation") or "-"
        visit_date = c.get("site_visit_date", "")
        pickup = "Yes - " + c.get("pickup_location", "") if c.get("pickup_required") else "No"
        visit_cab = f"{visit_date} (Cab: {pickup})" if visit_date else "-"
        rec_link = c.get("recording_url") or "-"
        if rec_link != "-" and rec_link.startswith("/"):
            rec_link = f"{base_url}{rec_link}"

        writer.writerow([
            call_date, call_time, phone, lead, dur,
            score, tl, fund, bhk, bud,
            loc, job, visit_cab, c.get("next_callback", "-"),
            c.get("main_objection") or c.get("objection") or "-",
            c.get("whatsapp_status", "-"),
            c.get("summary", "-"),
            f"₹{c.get('cost_inr', 0.0)}",
            rec_link
        ])
    output.seek(0)
    tag = direction or "all"
    fname = f"call_logs_{tag}_{type}_report.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )

# ============================================================
# META WHATSAPP CLOUD API & CAMPAIGN BROCHURES
# ============================================================
class CampaignDetailsUpdateReq(BaseModel):
    project_name: Optional[str] = None
    site_address: Optional[str] = None
    project_highlights: Optional[str] = None
    pickup_drop_notes: Optional[str] = None
    brochure_url: Optional[str] = None

class TestBrochureReq(BaseModel):
    phone: str

async def upload_brochure_file(filename: str, file_bytes: bytes, content_type: str = "application/pdf") -> str:
    fallback_url = f"/brochures/{filename}"
    # 1. Save locally to both brochures/ and static/brochures/ for guaranteed immediate access
    try:
        BROCHURE_DIR.mkdir(parents=True, exist_ok=True)
        (BROCHURE_DIR / filename).write_bytes(file_bytes)
    except Exception as local_err:
        logger.warning(f"Error saving to brochures/: {local_err}")

    try:
        STATIC_BROCHURE_DIR.mkdir(parents=True, exist_ok=True)
        (STATIC_BROCHURE_DIR / filename).write_bytes(file_bytes)
    except Exception as static_err:
        logger.warning(f"Error saving to static/brochures/: {static_err}")

    # 2. Try Supabase Storage bucket 'campaign-brochures'
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if supabase_url and supabase_key:
        upload_url = f"{supabase_url}/storage/v1/object/campaign-brochures/{filename}"
        headers = {
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": content_type,
            "x-upsert": "true"
        }
        if httpx is not None:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    res = await client.post(upload_url, headers=headers, content=file_bytes)
                    if res.status_code in (200, 201):
                        public_url = f"{supabase_url}/storage/v1/object/public/campaign-brochures/{filename}"
                        logger.info(f"Brochure uploaded to Supabase Storage: {public_url}")
                        return public_url
                    else:
                        logger.warning(f"Supabase storage upload status {res.status_code}: {res.text}")
            except Exception as e:
                logger.warning(f"Supabase storage upload error (httpx): {e}")
        else:
            # Fallback to urllib.request if httpx is not installed
            def _urllib_upload():
                req = urllib.request.Request(upload_url, data=file_bytes, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=15.0) as resp:
                    return resp.getcode()
            try:
                status_code = await asyncio.to_thread(_urllib_upload)
                if status_code in (200, 201):
                    public_url = f"{supabase_url}/storage/v1/object/public/campaign-brochures/{filename}"
                    logger.info(f"Brochure uploaded to Supabase Storage (urllib): {public_url}")
                    return public_url
            except Exception as ue:
                logger.warning(f"Supabase storage upload error (urllib): {ue}")

    return fallback_url

@app.post("/api/upload-brochure")
async def api_upload_standalone_brochure(file: Optional[UploadFile] = File(None)):
    try:
        if not file or not file.filename:
            return JSONResponse(
                status_code=200,
                content={"success": False, "ok": False, "error": "No file uploaded", "brochure_url": ""}
            )
        content = await file.read()
        if not content:
            return JSONResponse(
                status_code=200,
                content={"success": False, "ok": False, "error": "Uploaded file is empty", "brochure_url": ""}
            )
        orig_name = file.filename or "brochure.pdf"
        safe_name = f"brochure_{int(time.time())}_{re.sub(r'[^a-zA-Z0-9._-]', '_', orig_name)}"
        content_type = file.content_type or "application/pdf"
        brochure_url = await upload_brochure_file(safe_name, content, content_type)
        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "ok": True,
                "status": "uploaded",
                "brochure_url": brochure_url,
                "filename": safe_name
            }
        )
    except Exception as e:
        logger.error(f"Error in api_upload_standalone_brochure: {e}", exc_info=True)
        return JSONResponse(
            status_code=200,
            content={"success": False, "ok": False, "error": str(e), "detail": str(e), "brochure_url": ""}
        )

@app.post("/api/campaigns/{cid}/upload-brochure")
async def api_upload_campaign_brochure(cid: str, file: Optional[UploadFile] = File(None)):
    try:
        if not file or not file.filename:
            return JSONResponse(
                status_code=200,
                content={"success": False, "ok": False, "error": "No file uploaded", "brochure_url": ""}
            )

        content = await file.read()
        if not content:
            return JSONResponse(
                status_code=200,
                content={"success": False, "ok": False, "error": "Uploaded file is empty", "brochure_url": ""}
            )
        orig_name = file.filename or "brochure.pdf"
        clean_cid = re.sub(r'[^a-zA-Z0-9_-]', '_', str(cid or "standalone"))
        safe_name = f"brochure_{clean_cid}_{int(time.time())}_{re.sub(r'[^a-zA-Z0-9._-]', '_', orig_name)}"
        content_type = file.content_type or "application/pdf"

        brochure_url = await upload_brochure_file(safe_name, content, content_type)

        # Update campaign if it's a real campaign in DB
        if cid and str(cid).lower() not in ("standalone", "none", "null", "undefined", ""):
            try:
                camp = await get_campaign(cid)
                if camp:
                    await update_campaign(cid, {"brochure_url": brochure_url})
                    await push_unified_log("Campaign", "info", f"Brochure uploaded for campaign {cid}: {safe_name}")
            except Exception as db_err:
                logger.warning(f"Could not update campaign {cid} with brochure: {db_err}")

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "ok": True,
                "status": "uploaded",
                "brochure_url": brochure_url,
                "filename": safe_name
            }
        )
    except Exception as e:
        logger.error(f"Error in api_upload_campaign_brochure: {e}", exc_info=True)
        return JSONResponse(
            status_code=200,
            content={"success": False, "ok": False, "error": str(e), "detail": str(e), "brochure_url": ""}
        )

@app.post("/api/campaigns/{cid}/update-details")
async def api_update_campaign_details(cid: str, req: CampaignDetailsUpdateReq):
    camp = await get_campaign(cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    updates = {k: v for k, v in req.dict().items() if v is not None}
    await update_campaign(cid, updates)
    updated_camp = await get_campaign(cid)
    await push_unified_log("Campaign", "info", f"Project details updated for campaign {cid}")
    return {"status": "updated", "campaign": updated_camp}

@app.post("/api/campaigns/{cid}/test-brochure")
async def api_test_campaign_brochure(cid: str, req: TestBrochureReq):
    camp = await get_campaign(cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")

    phone = req.phone.strip()
    if not phone:
        raise HTTPException(status_code=400, detail="Phone number is required")

    project_name = camp.get("project_name") or camp.get("name") or "Kaamdhenu Residences"
    brochure_url = (camp.get("brochure_url") or "").strip()

    if brochure_url:
        full_doc_url = brochure_url
        if brochure_url.startswith("/"):
            port = os.getenv("PORT", "8000")
            full_doc_url = f"http://localhost:{port}{brochure_url}"

        res = await send_document_message(
            to_phone=phone,
            document_url=full_doc_url,
            caption=f"Official Brochure & Floor Plans — {project_name}",
            filename=f"{project_name.replace(' ', '_')}_Brochure.pdf",
            campaign_id=cid
        )
    else:
        p_addr = camp.get("site_address") or "Near City Center, Metro Corridor"
        p_high = camp.get("project_highlights") or "• Luxury 2BHK & 3BHK Air-Conditioned Homes\n• 30+ Lifestyle Amenities"
        test_msg = (
            f"🏡 *{project_name.upper()} — PROJECT OVERVIEW*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Location:* {p_addr}\n\n"
            f"✨ *Key Highlights:*\n{p_high}\n\n"
            f"(Note: Upload a PDF brochure in the Campaign Dashboard to deliver official PDF documents via WhatsApp.)"
        )
        res = await send_text_message(to_phone=phone, text=test_msg, campaign_id=cid)

    return {"status": "dispatched", "phone": phone, "result": res}

@app.get("/api/whatsapp/logs")
async def api_whatsapp_logs(phone: Optional[str] = None, limit: int = 100):
    return await list_whatsapp_logs(limit=limit, phone=phone)

@app.get("/api/whatsapp/webhook")
async def whatsapp_webhook_verify(
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge")
):
    """
    Handle Meta WhatsApp Webhook Verification Challenge.
    """
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", WHATSAPP_VERIFY_TOKEN)
    logger.info(f"Meta webhook verification request: mode={hub_mode}")

    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        logger.info("Meta webhook verification challenge passed!")
        return PlainTextResponse(content=hub_challenge or "", status_code=200)

    logger.warning(f"Meta webhook verification failed: token mismatch (expected {verify_token}, got {hub_verify_token})")
    raise HTTPException(status_code=403, detail="Verification token mismatch")

@app.post("/api/whatsapp/webhook")
async def whatsapp_webhook_inbound(request: Request):
    """
    Receive and process inbound messages from Meta WhatsApp Cloud API.
    """
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid_json"}, status_code=400)

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            val = change.get("value", {})
            messages = val.get("messages", [])
            contacts = val.get("contacts", [])

            for msg in messages:
                from_wa = msg.get("from", "")
                msg_type = msg.get("type", "")
                user_text = ""

                if msg_type == "text":
                    user_text = msg.get("text", {}).get("body", "").strip()
                elif msg_type == "button":
                    user_text = msg.get("button", {}).get("text", "").strip()
                elif msg_type == "interactive":
                    user_text = (
                        msg.get("interactive", {}).get("button_reply", {}).get("title")
                        or msg.get("interactive", {}).get("list_reply", {}).get("title")
                        or ""
                    )

                if not from_wa or not user_text:
                    continue

                contact_name = "there"
                if contacts:
                    contact_name = contacts[0].get("profile", {}).get("name", "there")

                # Log inbound message
                await insert_whatsapp_log(
                    phone_number=from_wa,
                    message=user_text,
                    status="received",
                    direction="inbound",
                    message_type=msg_type
                )
                await push_unified_log("WhatsApp", "info", f"📩 Inbound WhatsApp from {from_wa} ({contact_name}): {user_text[:80]}")

                # Context lookup for project & lead
                lead_ctx = {"lead_name": contact_name, "phone": from_wa}
                campaign_ctx = {}

                # 1. Check recent outbound context by caller phone
                recent_ctx = await find_recent_outbound_context(from_wa)
                if recent_ctx.get("found"):
                    lead_ctx["lead_name"] = recent_ctx.get("lead_name") or contact_name
                    cid = recent_ctx.get("campaign_id")
                    if cid:
                        camp = await get_campaign(cid)
                        if camp:
                            campaign_ctx = camp

                # 2. If no campaign found yet, pick active campaign
                if not campaign_ctx:
                    all_camps = await list_campaigns()
                    active_camps = [c for c in all_camps if c.get("status") == "active"]
                    if active_camps:
                        campaign_ctx = active_camps[0]

                # Generate AI response using Gemini Real Estate Sales rules
                reply_text, wants_brochure = await generate_whatsapp_ai_response(
                    incoming_text=user_text,
                    campaign_context=campaign_ctx,
                    lead_context=lead_ctx
                )

                # Send text response
                cid_val = campaign_ctx.get("id")
                await send_text_message(
                    to_phone=from_wa,
                    text=reply_text,
                    campaign_id=cid_val
                )

                # If user asked for brochure and campaign has brochure_url, auto-dispatch document
                brochure_url = campaign_ctx.get("brochure_url")
                if wants_brochure and brochure_url:
                    p_name = campaign_ctx.get("project_name") or campaign_ctx.get("name") or "Kaamdhenu Residences"
                    await send_document_message(
                        to_phone=from_wa,
                        document_url=brochure_url,
                        caption=f"Official Project Brochure & Floor Plans — {p_name}",
                        filename=f"{p_name.replace(' ', '_')}_Brochure.pdf",
                        campaign_id=cid_val
                    )

    return {"status": "ok"}
