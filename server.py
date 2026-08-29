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
    get_settings, save_settings_dict, get_contact_memory
)

load_dotenv(".env", override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

app = FastAPI(title="Kaamdhenu 3.0 Voice Platform", version="3.0.0")

class SingleCallReq(BaseModel):
    phone: str
    lead_name: str = "there"
    agent_name: str = "Priya"
    business_name: str = "Kaamdhenu Real Estate"
    service_type: str = "Luxury 2BHK/3BHK Apartments"
    broker_phone: Optional[str] = None
    system_prompt: Optional[str] = None

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
    return await get_calls(direction=direction, campaign_id=campaign_id)

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
        "VOBIZ_API_URL": db_s.get("VOBIZ_API_URL") or os.getenv("VOBIZ_API_URL", "https://api.vobiz.ai/v1")
    }

@app.post("/api/settings")
async def api_save_settings(req: Request):
    d = await req.json()
    await save_settings_dict(d)
    for k, v in d.items(): os.environ[k] = str(v)
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
    
    if not (url and key and secret and num):
        raise HTTPException(400, "Missing LiveKit URL/Keys or Vobiz Number.")
        
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        
        # 1. Check existing Inbound Trunks
        existing_trunks = await lk.sip.list_sip_inbound_trunks(lk_api.ListSIPInboundTrunksRequest())
        in_trunk_id = None
        for t in existing_trunks.items:
            if num in t.numbers or (t.numbers and any(n.replace("+", "") in num for n in t.numbers)):
                in_trunk_id = t.sip_trunk_id
                break
        
        # Create only if not exists
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
        
        # 2. Check / Create Dispatch Rule
        existing_rules = await lk.sip.list_sip_dispatch_rules(lk_api.ListSIPDispatchRulesRequest())
        rule_id = None
        for r in existing_rules.items:
            if in_trunk_id in r.trunk_ids:
                rule_id = r.sip_dispatch_rule_id
                break
                
        if not rule_id:
            rule = await lk.sip.create_sip_dispatch_rule(
                lk_api.CreateSIPDispatchRuleRequest(
                    rule=lk_api.SIPDispatchRuleInfo(
                        name="Kaamdhenu Inbound Dispatch Rule",
                        trunk_ids=[in_trunk_id],
                        rule=lk_api.SIPDispatchRule(
                            dispatch_rule_direct=lk_api.SIPDispatchRuleDirect(room_name_prefix="inbound-", pin="")
                        )
                    )
                )
            )
            rule_id = rule.sip_dispatch_rule_id
        
        await lk.aclose()
        await session.close()
        
        await save_settings_dict({
            "INBOUND_TRUNK_ID": in_trunk_id,
            "INBOUND_DISPATCH_RULE_ID": rule_id
        })
        os.environ["INBOUND_TRUNK_ID"] = in_trunk_id
        os.environ["INBOUND_DISPATCH_RULE_ID"] = rule_id
        
        await push_unified_log("SIP", "info", f"Inbound successfully linked: Trunk={in_trunk_id}, Rule={rule_id}")
        return {"status": "provisioned", "inbound_trunk_id": in_trunk_id, "dispatch_rule_id": rule_id}
    except Exception as e:
        await push_unified_log("SIP", "error", f"Inbound provisioning failed: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/call")
async def api_dispatch(req: SingleCallReq):
    url = os.getenv("LIVEKIT_URL")
    key = os.getenv("LIVEKIT_API_KEY")
    secret = os.getenv("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        raise HTTPException(400, "LiveKit credentials missing")

    phone = req.phone.strip()
    room_name = f"outbound-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    meta = {
        "direction": "outbound",
        "phone_number": phone,
        "lead_name": req.lead_name,
        "agent_name": req.agent_name,
        "business_name": req.business_name,
        "service_type": req.service_type,
        "broker_phone": req.broker_phone,
        "system_prompt": req.system_prompt
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
        await push_unified_log("API", "info", f"Call dispatched to {phone} ({req.agent_name})", call_id=room_name)
        return {"status": "dispatched", "room": room_name, "phone": phone}
    except Exception as e:
        await push_unified_log("API", "error", f"Dispatch failed: {e}")
        raise HTTPException(500, str(e))
