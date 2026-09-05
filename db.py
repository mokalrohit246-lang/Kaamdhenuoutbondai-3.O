import logging
import os
import uuid
import httpx
import sqlite3
import re
import time
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

async def book_appointment(
    name: str, phone: str, date: str, time: str, service: str = "Site Visit",
    budget: str = "", property_type: str = "", pickup_required: bool = False,
    pickup_address: str = "", calcom_booking_uid: str = "", id: Optional[str] = None,
    status: str = "booked", **kwargs
) -> str:
    clean_phone_digits = re.sub(r'\D', '', str(phone or ""))
    full_id = id or f"apt_{clean_phone_digits[-10:]}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    db = await _adb()

    clean_p = str(phone or "").strip()
    row = {
        "id": full_id,
        "name": name or "Lead",
        "phone": clean_p,
        "date": date,
        "time": time,
        "service": service,
        "status": status or "booked",
        "created_at": datetime.utcnow().isoformat(),
        "pickup_required": bool(pickup_required),
        "pickup_address": pickup_address or ""
    }
    if calcom_booking_uid:
        row["calcom_booking_uid"] = calcom_booking_uid

    try:
        await db.table("appointments").upsert(row, on_conflict="id").execute()
    except Exception as e:
        logger.warning(f"book_appointment upsert error: {e}")
        try:
            await db.table("appointments").insert(row).execute()
        except Exception as e2:
            logger.warning(f"book_appointment insert fallback warning: {e2}")

    return full_id

# Backward compatible alias
insert_appointment = book_appointment

async def insert_whatsapp_log(phone_number: str, message: str, status: str = "sent", call_id: Optional[str] = None):
    try:
        db = await _adb()
        await db.table("whatsapp_logs").insert({
            "id": str(uuid.uuid4()),
            "phone_number": phone_number,
            "message": message[:1000],
            "status": status,
            "call_id": call_id,
            "created_at": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to log whatsapp message: {e}")

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

async def get_all_appointments() -> list:
    """
    Unified appointments fetcher:
    1. Fetches all rows from `appointments` table.
    2. Queries `call_logs` table for any records where `site_visit_date` is not empty/null.
    3. Auto-Backfill Sync: For each call log record with a `site_visit_date`:
       - Extract date_time = record.get('site_visit_date')
       - Split into date and time.
       - Extract name = record.get('lead_name') or record.get('called_to') or 'Lead'
       - Extract phone = record.get('phone_number')
       - Extract pickup_required = record.get('pickup_required', False)
       - Extract pickup_address = record.get('pickup_location', '')
       - Extract budget = record.get('budget', '-')
       - Extract requirement = record.get('bhk_requirement') or record.get('property_type') or 'Site Visit'
       - Check if this appointment already exists in appointments table (match by phone and date).
       - If NOT present, automatically insert it into appointments table so it is permanently saved.
    4. Return the merged, deduplicated list of appointments sorted by date/time descending.
    5. Include fields: id, name, phone, date, time, service, requirement, budget, pickup_required, pickup_address, status, created_at.
    """
    db = await _adb()

    # 1. Fetch appointments table
    appointments = []
    try:
        res = await db.table("appointments").select("*").order("created_at", desc=True).execute()
        appointments = res.data or []
    except Exception as e:
        logger.warning(f"Error fetching appointments: {e}")

    # Track existing appointments by (phone_10_digits, date_str)
    existing_keys = set()
    for a in appointments:
        p_clean = re.sub(r"\D", "", str(a.get("phone") or ""))
        p_10 = p_clean[-10:] if len(p_clean) >= 10 else p_clean
        d_str = str(a.get("date") or "").strip()
        if p_10 and d_str:
            existing_keys.add((p_10, d_str))

    # 2. Query call_logs table for records where site_visit_date is not empty/null
    call_logs = []
    try:
        cl_res = await db.table("call_logs").select("*").neq("site_visit_date", "").execute()
        call_logs = cl_res.data or []
    except Exception as e:
        logger.warning(f"Error querying call_logs for site visits: {e}")

    # Build lookup map for call logs by (phone_10, date) to enrich appointments with requirement and budget
    call_log_map = {}
    newly_inserted = []

    for log in call_logs:
        raw_dt = str(log.get("site_visit_date") or "").strip()
        if not raw_dt or raw_dt.lower() in ("none", "null", "-", "false"):
            continue

        # Split into date and time
        dt_clean = raw_dt.replace("T", " ")
        parts = dt_clean.split()
        date_part = parts[0].strip() if len(parts) > 0 else raw_dt
        time_part = parts[1].strip()[:5] if len(parts) > 1 else "11:00"
        if len(time_part) == 4 and ":" not in time_part:
            time_part = f"{time_part[:2]}:{time_part[2:]}"

        name = log.get("lead_name") or log.get("client_name") or log.get("called_to") or "Lead"
        phone = str(log.get("phone_number") or "")
        p_clean = re.sub(r"\D", "", phone)
        p_10 = p_clean[-10:] if len(p_clean) >= 10 else p_clean
        pickup_req = bool(log.get("pickup_required"))
        pickup_addr = log.get("pickup_location") or ""
        budget = log.get("budget") or "-"
        requirement = log.get("bhk_requirement") or log.get("property_type") or "Site Visit"
        outcome = str(log.get("outcome") or "").lower()
        status = "cancelled" if "cancel" in outcome else "booked"
        created_at = log.get("timestamp") or datetime.utcnow().isoformat()

        if p_10 and date_part:
            call_log_map[(p_10, date_part)] = log

        key = (p_10, date_part)
        if key not in existing_keys and p_10:
            custom_id = f"apt_{p_10}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            service_label = f"Site Visit ({requirement})" if requirement and requirement != "Site Visit" else "Site Visit"
            formatted_phone = phone if phone.startswith("+") else (f"+91{p_10}" if len(p_10) == 10 else phone)

            new_apt = {
                "id": custom_id,
                "name": name,
                "phone": formatted_phone,
                "date": date_part,
                "time": time_part,
                "service": service_label,
                "status": status,
                "created_at": created_at,
                "pickup_required": pickup_req,
                "pickup_address": pickup_addr
            }
            try:
                await db.table("appointments").insert(new_apt).execute()
                logger.info(f"Auto-backfilled site visit appointment from call log {log.get('id')} for {phone} on {date_part}")
            except Exception as ins_err:
                logger.warning(f"Auto-backfill insert warning: {ins_err}")

            new_apt["requirement"] = requirement
            new_apt["budget"] = budget
            existing_keys.add(key)
            newly_inserted.append(new_apt)

    # 4. Merge and deduplicate appointments
    all_raw = appointments + newly_inserted
    merged = []
    seen_ids = set()

    for a in all_raw:
        aid = a.get("id")
        if aid in seen_ids:
            continue
        seen_ids.add(aid)

        p_clean = re.sub(r"\D", "", str(a.get("phone") or ""))
        p_10 = p_clean[-10:] if len(p_clean) >= 10 else p_clean
        d_val = str(a.get("date") or "")
        matched_log = call_log_map.get((p_10, d_val), {})

        req = a.get("requirement") or a.get("property_type") or matched_log.get("bhk_requirement") or matched_log.get("property_type") or ""
        bud = a.get("budget") or matched_log.get("budget") or "-"
        serv = a.get("service") or "Site Visit"

        if not req and "(" in serv and ")" in serv:
            m = re.search(r'\(([^)]+)\)', serv)
            if m:
                req = m.group(1).strip()
        if not req:
            req = "Site Visit"

        merged.append({
            "id": aid,
            "name": a.get("name") or matched_log.get("lead_name") or "Lead",
            "phone": a.get("phone") or matched_log.get("phone_number") or "",
            "date": a.get("date") or "",
            "time": a.get("time") or "11:00",
            "service": serv,
            "requirement": req,
            "budget": bud if bud != "" else "-",
            "pickup_required": bool(a.get("pickup_required") if a.get("pickup_required") is not None else matched_log.get("pickup_required")),
            "pickup_address": a.get("pickup_address") or matched_log.get("pickup_location") or "",
            "status": a.get("status") or "booked",
            "created_at": a.get("created_at") or datetime.utcnow().isoformat()
        })

    # Sort descending by date and time
    merged.sort(key=lambda x: f"{x.get('date', '')} {x.get('time', '')}", reverse=True)
    return merged

async def cancel_appointment(aid: str) -> bool:
    db = await _adb()
    try:
        # 1. Update appointments table
        await db.table("appointments").update({"status": "cancelled"}).eq("id", aid).execute()

        # 2. Also cancel corresponding call_logs if matched
        apt_res = await db.table("appointments").select("phone, date").eq("id", aid).maybe_single().execute()
        if apt_res and apt_res.data:
            p_val = apt_res.data.get("phone") or ""
            d_val = apt_res.data.get("date") or ""
            p_digits = re.sub(r"\D", "", p_val)
            p_10 = p_digits[-10:] if len(p_digits) >= 10 else p_digits
            if p_10:
                cl_res = await db.table("call_logs").select("id, site_visit_date").ilike("phone_number", f"%{p_10}%").execute()
                for cl in (cl_res.data or []):
                    if not d_val or d_val in str(cl.get("site_visit_date") or ""):
                        await db.table("call_logs").update({"outcome": "cancelled"}).eq("id", cl.get("id")).execute()
    except Exception as e:
        logger.warning(f"Error cancelling appointment: {e}")
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
    callback_dispatched: bool = False,
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
            "whatsapp_status": whatsapp_status,
            "callback_dispatched": callback_dispatched
        }
        for k, v in kwargs.items():
            row[k] = v
        if campaign_id:
            row["campaign_id"] = campaign_id
        if recording_url:
            row["recording_url"] = recording_url
        try:
            await db.table("call_logs").upsert(row, on_conflict="id").execute()
        except Exception as up_err:
            if "callback_dispatched" in str(up_err).lower():
                row.pop("callback_dispatched", None)
                await db.table("call_logs").upsert(row, on_conflict="id").execute()
            else:
                raise up_err
    except Exception as e:
        logger.error(f"Error executing log_call upsert: {e}")

async def get_pending_callbacks() -> list:
    """Fetch call logs where next_callback is set and not yet dispatched."""
    try:
        db = await _adb()
        res = await db.table("call_logs").select("*").neq("next_callback", "").order("timestamp", desc=True).limit(200).execute()
        rows = res.data or []
        pending = []
        for r in rows:
            cb = (r.get("next_callback") or "").strip()
            if cb and not r.get("callback_dispatched"):
                pending.append(r)
        return pending
    except Exception as e:
        logger.error(f"Error getting pending callbacks: {e}")
        return []

async def mark_callback_dispatched(call_id: str) -> bool:
    """Mark a call log's callback as dispatched to avoid duplicate dialing."""
    try:
        db = await _adb()
        await db.table("call_logs").update({"callback_dispatched": True}).eq("id", call_id).execute()
        return True
    except Exception as e:
        logger.error(f"Error marking callback dispatched for {call_id}: {e}")
        return False

async def get_calls(direction: Optional[str] = None, campaign_id: Optional[str] = None, limit: int = 100):
    db = await _adb()
    q = db.table("call_logs").select("*").order("timestamp", desc=True).limit(limit)
    if direction: q = q.eq("direction", direction)
    if campaign_id and campaign_id != "all": q = q.eq("campaign_id", campaign_id)
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


# Agent Profiles CRUD (Supabase + Local SQLite Fallback)
LOCAL_DB_FILE = os.path.join(os.path.dirname(__file__), "local_agent_profiles.db")

def _init_local_sqlite():
    try:
        conn = sqlite3.connect(LOCAL_DB_FILE)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                voice TEXT DEFAULT 'Aoede',
                model TEXT DEFAULT 'gemini-2.0-flash-exp',
                system_prompt TEXT DEFAULT '',
                enabled_tools TEXT DEFAULT '[]',
                is_default INTEGER DEFAULT 0,
                business_name TEXT DEFAULT '',
                assigned_did TEXT DEFAULT '',
                broker_whatsapp TEXT DEFAULT '',
                broker_email TEXT DEFAULT '',
                calcom_event_type_id TEXT DEFAULT '6934775',
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Local SQLite init error: {e}")

async def list_agent_profiles():
    profiles = []
    # 1. Try Supabase
    try:
        db = await _adb()
        res = await db.table("agent_profiles").select("*").order("created_at", desc=True).execute()
        profiles = res.data or []
    except Exception as exc:
        logger.warning(f"Supabase list_agent_profiles error, falling back to SQLite: {exc}")

    # 2. If empty or failed, check local SQLite
    if not profiles:
        try:
            _init_local_sqlite()
            conn = sqlite3.connect(LOCAL_DB_FILE)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM agent_profiles ORDER BY created_at DESC")
            rows = c.fetchall()
            profiles = [dict(r) for r in rows]
            conn.close()
        except Exception as sqle:
            logger.warning(f"Local SQLite list error: {sqle}")

    # Decorate with backward/forward compatible aliases
    for p in profiles:
        p["agent_name"] = p.get("name") or p.get("agent_name", "")
        p["broker_phone"] = p.get("broker_whatsapp") or p.get("broker_phone", "")
    return profiles

async def save_agent_profile(data: dict) -> dict:
    pid = data.get("id") or str(uuid.uuid4())
    name = data.get("name") or data.get("agent_name") or "Agent"
    voice = data.get("voice") or "Aoede"
    model = data.get("model") or os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")
    business_name = data.get("business_name") or ""
    assigned_did = data.get("assigned_did") or ""
    broker_whatsapp = data.get("broker_whatsapp") or data.get("broker_phone") or ""
    broker_email = data.get("broker_email") or ""
    calcom_event_type_id = str(data.get("calcom_event_type_id") or "6934775")
    system_prompt = data.get("system_prompt") or ""
    enabled_tools = data.get("enabled_tools") or "[]"
    is_default = int(data.get("is_default") or 0)
    created_at = data.get("created_at") or datetime.utcnow().isoformat()

    clean_profile = {
        "id": pid,
        "name": name,
        "voice": voice,
        "model": model,
        "business_name": business_name,
        "assigned_did": assigned_did,
        "broker_whatsapp": broker_whatsapp,
        "broker_email": broker_email,
        "calcom_event_type_id": calcom_event_type_id,
        "system_prompt": system_prompt,
        "enabled_tools": enabled_tools,
        "is_default": is_default,
        "created_at": created_at
    }

    # 1. Upsert to Supabase
    try:
        db = await _adb()
        await db.table("agent_profiles").upsert(clean_profile, on_conflict="id").execute()
    except Exception as exc:
        logger.warning(f"Supabase save_agent_profile failed, using local SQLite fallback: {exc}")

    # 2. Always persist to local SQLite as reliable backup
    try:
        _init_local_sqlite()
        conn = sqlite3.connect(LOCAL_DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO agent_profiles (id, name, voice, model, system_prompt, enabled_tools, is_default, business_name, assigned_did, broker_whatsapp, broker_email, calcom_event_type_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, voice=excluded.voice, model=excluded.model,
                system_prompt=excluded.system_prompt, enabled_tools=excluded.enabled_tools,
                is_default=excluded.is_default, business_name=excluded.business_name,
                assigned_did=excluded.assigned_did, broker_whatsapp=excluded.broker_whatsapp,
                broker_email=excluded.broker_email, calcom_event_type_id=excluded.calcom_event_type_id
        """, (pid, name, voice, model, system_prompt, enabled_tools, is_default, business_name, assigned_did, broker_whatsapp, broker_email, calcom_event_type_id, created_at))
        conn.commit()
        conn.close()
    except Exception as sqle:
        logger.warning(f"Local SQLite save error: {sqle}")

    # Return profile with helper aliases for frontend and runtime compatibility
    out = dict(clean_profile)
    out["agent_name"] = name
    out["broker_phone"] = broker_whatsapp
    return out

async def delete_agent_profile(pid: str):
    try:
        db = await _adb()
        await db.table("agent_profiles").delete().eq("id", pid).execute()
    except Exception as exc:
        logger.warning(f"Supabase delete_agent_profile error: {exc}")

    try:
        _init_local_sqlite()
        conn = sqlite3.connect(LOCAL_DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM agent_profiles WHERE id = ?", (pid,))
        conn.commit()
        conn.close()
    except Exception as sqle:
        logger.warning(f"Local SQLite delete error: {sqle}")

async def get_agent_profile(pid: str):
    profile = None
    try:
        db = await _adb()
        res = await db.table("agent_profiles").select("*").eq("id", pid).maybe_single().execute()
        profile = res.data if res else None
    except Exception as exc:
        logger.warning(f"Supabase get_agent_profile error: {exc}")

    if not profile:
        try:
            _init_local_sqlite()
            conn = sqlite3.connect(LOCAL_DB_FILE)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM agent_profiles WHERE id = ?", (pid,))
            row = c.fetchone()
            if row:
                profile = dict(row)
            conn.close()
        except Exception as sqle:
            logger.warning(f"Local SQLite get error: {sqle}")

    if profile:
        profile["agent_name"] = profile.get("name") or profile.get("agent_name", "")
        profile["broker_phone"] = profile.get("broker_whatsapp") or profile.get("broker_phone", "")
    return profile

# Campaigns CRUD
async def list_campaigns():
    db = await _adb()
    res = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
    camps = res.data or []
    try:
        call_res = await db.table("call_logs").select("campaign_id, direction, outcome, lead_score, site_visit_date").execute()
        all_calls = call_res.data or []
        for c in camps:
            cid = c.get("id")
            c_calls = [x for x in all_calls if x.get("campaign_id") == cid]
            c["total_dispatched"] = len([x for x in c_calls if x.get("direction") == "outbound"])
            c["answered_count"] = len([x for x in c_calls if x.get("outcome") != "no_answer"])
            c["hot_leads"] = len([x for x in c_calls if x.get("lead_score") == "Hot"])
            c["site_visits"] = len([x for x in c_calls if x.get("site_visit_date")])
    except Exception as e:
        logger.warning(f"Error enriching campaign stats: {e}")
    return camps

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
