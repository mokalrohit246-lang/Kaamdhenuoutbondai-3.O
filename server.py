import asyncio
import io
import csv
import json
import logging
import os
import random
import ssl
import certifi
import aiohttp
from pathlib import Path
from typing import Optional, List
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
    list_agent_profiles, save_agent_profile, delete_agent_profile, get_agent_profile,
    list_campaigns, create_campaign, update_campaign_status, find_campaign_by_inbound_number
)

load_dotenv(".env", override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

app = FastAPI(title="Kaamdhenu 3.0 Voice Platform", version="3.0.0")

class SingleCallReq(BaseModel):
    phone: Optional[str] = None
    phone_number: Optional[str] = None
    lead_name: str = "there"
    agent_id: Optional[str] = None
    agent_name: str = "Priya"
    agent_voice: Optional[str] = None
    business_name: str = "Kaamdhenu Real Estate"
    service_type: str = "Luxury 2BHK/3BHK Apartments"
    broker_phone: Optional[str] = None
    broker_email: Optional[str] = None
    calcom_event_type_id: Optional[str] = None
    system_prompt: Optional[str] = None
    custom_prompt: Optional[str] = None

class ClientNumReq(BaseModel):
    id: Optional[str] = None
    inbound_number: str
    business_name: str
    service_type: str
    agent_name: str = "Priya"
    broker_whatsapp_number: Optional[str] = None
    system_prompt: Optional[str] = None

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    p = Path(__file__).parent / "ui" / "index.html"
    return HTMLResponse(content=p.read_text(encoding="utf-8"), status_code=200) if p.exists() else HTMLResponse("<h1>UI file missing</h1>", 404)

@app.get("/health")
async def health():
    return {"status": "online", "service": "Kaamdhenu AI 3.0", "livekit_url": os.getenv("LIVEKIT_URL", "")}

@app.get("/api/stats")
async def api_stats():
    return await get_stats_data()

@app.get("/api/logs")
async def api_logs(limit: int = 150, level: str = "all", source: str = "all"):
    return await get_unified_logs(limit=limit, level=level, source=source)

@app.delete("/api/logs")
async def api_clear_logs():
    await clear_unified_logs()
    return {"status": "cleared"}

@app.get("/api/calls")
async def api_calls(direction: Optional[str] = None, campaign_id: Optional[str] = None):
    try:
        return await get_calls(direction=direction, campaign_id=campaign_id)
    except Exception as e:
        logger.error(f"Error fetching calls: {e}")
        return []

@app.get("/api/crm")
async def api_crm(phone: Optional[str] = None):
    return await get_contact_memory(phone=phone)

@app.get("/api/campaigns/{campaign_id}/export")
async def export_campaign_csv(campaign_id: str):
    calls = await get_calls(campaign_id=campaign_id, limit=1000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date & Time", "Lead Name", "Phone Number", "Lead Score", "Outcome", "Summary", "Duration (s)", "Cost (INR)"])
    for c in calls:
        writer.writerow([
            c.get("timestamp", ""), c.get("lead_name", ""), c.get("phone_number", ""),
            c.get("lead_score", "Cold"), c.get("outcome", ""), c.get("summary", ""),
            c.get("duration_seconds", 0), c.get("cost_inr", 0.0)
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
async def api_cancel_app(aid: str):
    await cancel_appointment(aid)
    return {"status": "cancelled"}

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

    agent_name = req.agent_name
    agent_voice = req.agent_voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    business_name = req.business_name
    service_type = req.service_type
    broker_phone = req.broker_phone
    broker_email = req.broker_email or os.getenv("DEFAULT_BROKER_EMAIL", "")
    calcom_event_type_id = req.calcom_event_type_id or os.getenv("CALCOM_EVENT_TYPE_ID", "6934775")
    system_prompt = req.custom_prompt or req.system_prompt

    if req.agent_id:
        profile = await get_agent_profile(req.agent_id)
        if profile:
            agent_name = profile.get("agent_name") or profile.get("name") or agent_name
            agent_voice = profile.get("voice") or agent_voice
            business_name = profile.get("business_name") or business_name
            service_type = profile.get("service_type") or service_type
            system_prompt = req.custom_prompt or req.system_prompt or profile.get("system_prompt")
            broker_phone = req.broker_phone or profile.get("broker_phone") or profile.get("broker_whatsapp")
            broker_email = req.broker_email or profile.get("broker_email") or broker_email
            calcom_event_type_id = req.calcom_event_type_id or profile.get("calcom_event_type_id") or calcom_event_type_id

    room_name = f"outbound-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    meta = {
        "direction": "outbound",
        "phone_number": phone,
        "lead_name": req.lead_name,
        "agent_id": req.agent_id,
        "agent_name": agent_name,
        "agent_voice": agent_voice,
        "business_name": business_name,
        "service_type": service_type,
        "broker_phone": broker_phone,
        "broker_email": broker_email,
        "calcom_event_type_id": calcom_event_type_id,
        "system_prompt": system_prompt
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
    data = await req.json()
    pid = await save_agent_profile(data)
    return {"status": "saved", "id": pid}

@app.delete("/api/agent-profiles/{pid}")
async def api_del_agent(pid: str):
    await delete_agent_profile(pid)
    return {"status": "deleted"}

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
            async with httpx.AsyncClient(timeout=4.0) as client:
                res = await client.post(
                    f"https://api.cal.com/v1/event-types?apiKey={calcom_api_key}",
                    json={
                        "title": f"{name} - Site Visit",
                        "slug": slug,
                        "length": 30,
                        "description": f"Site visits for {name}"
                    }
                )
                if res.status_code in (200, 201):
                    res_json = res.json()
                    new_id = res_json.get("event_type", {}).get("id") or res_json.get("id")
                    if new_id:
                        event_type_id = str(new_id)
                        await push_unified_log("Cal.com", "info", f"Auto-created Cal.com event type '{slug}' (ID: {event_type_id}) for campaign '{name}'")
                else:
                    logger.warning(f"Cal.com create event-type returned {res.status_code}: {res.text}")
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
        "total_contacts": len(contacts)
    }
    cid = await create_campaign(data)
    return {"status": "created", "id": cid, "total_contacts": len(contacts), "calcom_event_type_id": event_type_id}

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
        return await get_calls(direction="outbound", campaign_id=c_id, limit=200)
    elif category in ("callback", "callbacks"):
        calls = await get_calls(direction="inbound", campaign_id=c_id, limit=200)
        cb_calls = [c for c in calls if c.get("log_category") == "campaign_callback" or c.get("campaign_id")]
        return cb_calls if cb_calls else calls
    elif category in ("dedicated", "dedicated_inbound"):
        calls = await get_calls(direction="inbound", campaign_id=c_id, limit=200)
        ded_calls = [c for c in calls if c.get("log_category") == "dedicated_inbound"]
        return ded_calls if ded_calls else calls
    else:
        return await get_calls(campaign_id=c_id, limit=200)

@app.get("/api/campaigns/{cid}/export-csv")
async def api_campaign_export(cid: str, type: str = "full"):
    c_id = None if cid == "all" else cid
    calls = await get_calls(campaign_id=c_id, limit=5000)
    if type == "daily":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        calls = [c for c in calls if c.get("timestamp", "").startswith(today)]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Time & Duration", "Client & Phone", "Location & Job", "BHK & Budget",
        "Timeline & Funding", "Lead Score", "Site Visit & Cab", "Next Callback",
        "Main Objection", "WhatsApp Status", "AI Ground Summary", "Cost"
    ])
    for c in calls:
        dur = c.get("duration_seconds", 0)
        time_dur = f"{c.get('timestamp', '')[:16]} ({dur}s)"
        client_phone = f"{c.get('client_name', c.get('lead_name', ''))} ({c.get('phone_number', '')})"
        loc = c.get("current_location", "")
        occ = c.get("occupation", "")
        loc_job = f"{loc} / {occ}".strip(" /") or "-"
        bhk = c.get("bhk_requirement", "")
        bud = c.get("budget", "")
        bhk_bud = f"{bhk} | {bud}".strip(" |") or "-"
        time_l = c.get("possession_timeline", "")
        fund = c.get("funding_type", "")
        timeline_fund = f"{time_l} | {fund}".strip(" |") or "-"
        visit_date = c.get("site_visit_date", "")
        pickup = "Yes - " + c.get("pickup_location", "") if c.get("pickup_required") else "No"
        visit_cab = f"{visit_date} (Cab: {pickup})" if visit_date else "-"
        cost_str = f"₹{c.get('cost_inr', 0.0)}"

        writer.writerow([
            time_dur, client_phone, loc_job, bhk_bud, timeline_fund,
            c.get("lead_score", "Cold"), visit_cab, c.get("next_callback", "-"),
            c.get("objection", "-"), c.get("whatsapp_status", "-"),
            c.get("summary", "-"), cost_str
        ])
    output.seek(0)
    fname = f"campaign_{cid[:8] if cid != 'all' else 'all'}_{type}_report.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )
