import logging
import os
import uuid
import httpx
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("kaamdhenu-db")

def _adb():
    from supabase._async.client import create_client
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    return create_client(url, key)

async def push_unified_log(source: str, level: str, message: str, detail: str = "", call_id: Optional[str] = None):
    try:
        db = await _adb()
        await db.table("unified_logs").insert({
            "id": str(uuid.uuid4()), "source": source, "level": level.lower(),
            "message": message[:500], "detail": detail[:2000], "call_id": call_id,
            "timestamp": datetime.utcnow().isoformat()
        }).execute()
    except Exception:
        pass

async def get_unified_logs(limit: int = 150, level: Optional[str] = None, source: Optional[str] = None):
    db = await _adb()
    q = db.table("unified_logs").select("*").order("timestamp", desc=True).limit(limit)
    if level and level != "all": q = q.eq("level", level.lower())
    if source and source != "all": q = q.eq("source", source)
    res = await q.execute()
    return res.data or []

async def clear_unified_logs():
    db = await _adb()
    await db.table("unified_logs").delete().neq("id", "").execute()

async def get_client_number_config(inbound_number: str):
    db = await _adb()
    clean = inbound_number.replace("+", "").replace("sip_", "").strip()
    res = await db.table("client_numbers").select("*").ilike("inbound_number", f"%{clean}%").maybe_single().execute()
    return res.data if res else None

async def list_client_numbers():
    db = await _adb()
    res = await db.table("client_numbers").select("*").order("created_at", desc=True).execute()
    return res.data or []

async def save_client_number(data: dict):
    db = await _adb()
    cid = data.get("id") or str(uuid.uuid4())
    data["id"] = cid
    data["created_at"] = datetime.utcnow().isoformat()
    await db.table("client_numbers").upsert(data, on_conflict="inbound_number").execute()
    return cid

async def delete_client_number(cid: str):
    db = await _adb()
    await db.table("client_numbers").delete().eq("id", cid).execute()

async def insert_appointment(name: str, phone: str, date: str, time: str, service: str, budget: str = "", property_type: str = ""):
    full_id = str(uuid.uuid4())
    db = await _adb()
    await db.table("appointments").insert({
        "id": full_id, "name": name, "phone": phone, "date": date, "time": time,
        "service": service, "budget": budget, "property_type": property_type,
        "status": "booked", "created_at": datetime.utcnow().isoformat()
    }).execute()
    return full_id[:8].upper()

async def check_slot(date: str, time: str) -> bool:
    db = await _adb()
    res = await db.table("appointments").select("id").eq("date", date).eq("time", time).eq("status", "booked").maybe_single().execute()
    return res.data is None

async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except Exception:
        dt = datetime.utcnow() + timedelta(hours=1)
    for _ in range(24):
        dt += timedelta(hours=1)
        if 10 <= dt.hour < 19:
            if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
                return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "Tomorrow at 11:00 AM"

async def get_all_appointments():
    db = await _adb()
    res = await db.table("appointments").select("*").order("date", desc=True).order("time").execute()
    return res.data or []

async def cancel_appointment(aid: str):
    db = await _adb()
    await db.table("appointments").update({"status": "cancelled"}).eq("id", aid).execute()
    return True

async def log_call(
    call_id: str, phone_number: str, called_to: str, lead_name: str,
    direction: str, campaign_id: Optional[str], outcome: str, lead_score: str,
    summary: str, reason: str, duration_seconds: int, cost_inr: float,
    recording_url: Optional[str] = None,
    client_name: str = "", current_location: str = "", occupation: str = "",
    bhk_requirement: str = "", budget: str = "", purpose: str = "Self-Use",
    possession_timeline: str = "Ready-to-Move", funding_type: str = "Bank Loan",
    commitment_risk: str = "Low", site_visit_date: str = "",
    pickup_required: bool = False, pickup_location: str = "",
    next_callback: str = "", objection: str = "", whatsapp_status: str = "— Not Requested",
    **kwargs
):
    try:
        db = await _adb()
        row = {
            "id": call_id or str(uuid.uuid4()),
            "phone_number": phone_number,
            "called_to": called_to,
            "lead_name": lead_name,
            "direction": direction,
            "outcome": outcome,
            "lead_score": lead_score,
            "summary": summary,
            "duration_seconds": int(duration_seconds),
            "cost_inr": float(cost_inr),
            "timestamp": datetime.utcnow().isoformat(),
            "client_name": client_name or lead_name,
            "current_location": current_location,
            "occupation": occupation,
            "bhk_requirement": bhk_requirement,
            "budget": budget,
            "purpose": purpose,
            "possession_timeline": possession_timeline,
            "funding_type": funding_type,
            "commitment_risk": commitment_risk,
            "site_visit_date": site_visit_date,
            "pickup_required": pickup_required,
            "pickup_location": pickup_location,
            "next_callback": next_callback,
            "objection": objection,
            "whatsapp_status": whatsapp_status
        }
        for k, v in kwargs.items():
            row[k] = v
        if campaign_id:
            row["campaign_id"] = campaign_id
        if recording_url:
            row["recording_url"] = recording_url
        await db.table("call_logs").upsert(row, on_conflict="id").execute()
    except Exception as e:
        logger.error(f"Error executing log_call upsert: {e}")

async def get_calls(direction: Optional[str] = None, campaign_id: Optional[str] = None, limit: int = 100):
    db = await _adb()
    q = db.table("call_logs").select("*").order("timestamp", desc=True).limit(limit)
    if direction: q = q.eq("direction", direction)
    if campaign_id: q = q.eq("campaign_id", campaign_id)
    res = await q.execute()
    return res.data or []

async def get_stats_data():
    db = await _adb()
    res = await db.table("call_logs").select("*").execute()
    rows = res.data or []
    total = len(rows)
    booked = sum(1 for r in rows if r.get("outcome") == "booked")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    total_spent_inr = sum(float(r.get("cost_inr") or 0) for r in rows)
    durations = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    avg_dur = round(sum(durations)/len(durations), 1) if durations else 0
    rate = round((booked / total * 100), 1) if total else 0.0
    return {
        "total_calls": total, "booked": booked, "not_interested": not_interested,
        "total_spent_inr": round(total_spent_inr, 2), "booking_rate": rate, "avg_duration": avg_dur
    }

async def add_contact_memory(phone: str, insight: str):
    db = await _adb()
    await db.table("contact_memory").insert({
        "id": str(uuid.uuid4()), "phone_number": phone, "insight": insight, "created_at": datetime.utcnow().isoformat()
    }).execute()

async def get_contact_memory(phone: Optional[str] = None):
    db = await _adb()
    q = db.table("contact_memory").select("*").order("created_at", desc=True).limit(50)
    if phone: q = q.ilike("phone_number", f"%{phone}%")
    res = await q.execute()
    return res.data or []

async def sync_google_sheets_row(webhook_url: str, payload: dict):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook_url, json=payload)
    except Exception as e:
        await push_unified_log("Webhook", "warning", f"Google Sheets sync failed: {e}")

async def get_settings():
    db = await _adb()
    res = await db.table("settings").select("key, value").execute()
    return {r["key"]: r["value"] for r in (res.data or [])}

async def save_settings_dict(data: dict):
    db = await _adb()
    now_iso = datetime.utcnow().isoformat()
    rows = [{"key": k, "value": str(v), "updated_at": now_iso} for k, v in data.items() if v is not None]
    if rows:
        await db.table("settings").upsert(rows, on_conflict="key").execute()


# Agent Profiles CRUD
async def list_agent_profiles():
    db = await _adb()
    res = await db.table("agent_profiles").select("*").order("created_at", desc=True).execute()
    return res.data or []

async def save_agent_profile(data: dict):
    db = await _adb()
    pid = data.get("id") or str(uuid.uuid4())
    data["id"] = pid
    data["created_at"] = datetime.utcnow().isoformat()
    await db.table("agent_profiles").upsert(data, on_conflict="id").execute()
    return pid

async def delete_agent_profile(pid: str):
    db = await _adb()
    await db.table("agent_profiles").delete().eq("id", pid).execute()

async def get_agent_profile(pid: str):
    db = await _adb()
    res = await db.table("agent_profiles").select("*").eq("id", pid).maybe_single().execute()
    return res.data if res else None

# Campaigns CRUD
async def list_campaigns():
    db = await _adb()
    res = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
    return res.data or []

async def create_campaign(data: dict):
    db = await _adb()
    cid = data.get("id") or str(uuid.uuid4())
    data["id"] = cid
    data["created_at"] = datetime.utcnow().isoformat()
    data.setdefault("status", "active")
    data.setdefault("consumed_minutes", 0)
    await db.table("campaigns").upsert(data, on_conflict="id").execute()
    return cid

async def update_campaign_status(cid: str, status: str):
    db = await _adb()
    await db.table("campaigns").update({"status": status}).eq("id", cid).execute()

async def add_campaign_minutes(campaign_id: str, minutes: float):
    """Add consumed minutes. Auto-sets status to quota_exhausted if over limit."""
    try:
        db = await _adb()
        res = await db.table("campaigns").select("consumed_minutes, allocated_minutes").eq("id", campaign_id).maybe_single().execute()
        if res and res.data:
            current = float(res.data.get("consumed_minutes", 0))
            allocated = float(res.data.get("allocated_minutes", 999999))
            new_total = round(current + minutes, 2)
            update = {"consumed_minutes": new_total}
            if new_total >= allocated:
                update["status"] = "quota_exhausted"
            await db.table("campaigns").update(update).eq("id", campaign_id).execute()
    except Exception as e:
        logger.error(f"Error adding campaign minutes: {e}")

# Smart Context Routing
async def find_recent_outbound_context(caller_phone: str):
    """Find if this caller was recently called in an outbound campaign (last 7 days)."""
    try:
        db = await _adb()
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        clean = caller_phone.replace("+", "").replace("sip_", "").strip()
        res = await db.table("call_logs").select("*").eq("direction", "outbound").ilike("phone_number", f"%{clean}%").gte("timestamp", cutoff).order("timestamp", desc=True).limit(1).execute()
        if res.data and len(res.data) > 0:
            row = res.data[0]
            return {
                "found": True,
                "campaign_id": row.get("campaign_id", ""),
                "lead_name": row.get("lead_name", "there"),
                "project_name": row.get("client_name", "") or row.get("lead_name", ""),
                "broker_phone": "",
                "prompt": ""
            }
        return {"found": False}
    except Exception as e:
        logger.error(f"Error finding outbound context: {e}")
        return {"found": False}

async def find_campaign_by_inbound_number(number: str):
    """Find campaign by dedicated inbound number."""
    try:
        db = await _adb()
        clean = number.replace("+", "").replace("sip_", "").strip()
        res = await db.table("campaigns").select("*").ilike("dedicated_inbound_number", f"%{clean}%").eq("status", "active").maybe_single().execute()
        return res.data if res else None
    except Exception:
        return None
