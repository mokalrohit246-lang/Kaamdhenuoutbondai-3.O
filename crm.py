import logging
import asyncio
import os
import re
from typing import Optional, Dict, Any
from db import (
    _adb, _rest_supabase, normalize_phone,
    get_agent_profile, get_campaign, list_agent_profiles
)

logger = logging.getLogger("kaamdhenu-crm")

async def lookup_inbound_callback_context(caller_phone: str) -> Dict[str, Any]:
    """
    Looks up the most recent outbound interaction or lead record in Supabase
    for the given phone number to retrieve dynamic agent persona, campaign context,
    and prior conversation summary.

    Returns a dict with:
        found: bool
        agent_id: str
        agent_name: str
        agent_voice: str
        business_name: str
        service_type: str
        system_prompt: str
        campaign_id: str
        campaign_name: str
        last_call_summary: str
        lead_name: str
        broker_phone: str
        broker_email: str
        calcom_event_type_id: str
        project_name: str
    """
    clean_10 = normalize_phone(caller_phone)
    if not clean_10:
        return {"found": False}

    db = None
    try:
        db = await _adb()
    except Exception as e:
        logger.warning(f"Could not initialize Supabase SDK client in crm.py: {e}")

    last_record = None

    # 1. Query call_logs in Supabase for the most recent outbound interaction
    if db is not None:
        try:
            res = await db.table("call_logs").select("*") \
                .eq("direction", "outbound") \
                .ilike("phone_number", f"%{clean_10}%") \
                .order("timestamp", desc=True) \
                .limit(1) \
                .execute()
            if res.data and len(res.data) > 0:
                last_record = res.data[0]
        except Exception as e:
            logger.debug(f"call_logs outbound lookup error: {e}")

    # Fallback via REST if SDK didn't return a record
    if not last_record:
        try:
            code, data = await _rest_supabase(
                "GET",
                f"call_logs?direction=eq.outbound&phone_number=ilike.*{clean_10}*&order=timestamp.desc&limit=1"
            )
            if code == 200 and isinstance(data, list) and len(data) > 0:
                last_record = data[0]
        except Exception as e:
            logger.debug(f"REST call_logs outbound lookup error: {e}")

    # 2. If no outbound call_log found, check leads table (if exists)
    if not last_record and db is not None:
        try:
            l_res = await db.table("leads").select("*") \
                .ilike("phone", f"%{clean_10}%") \
                .order("created_at", desc=True) \
                .limit(1) \
                .execute()
            if l_res.data and len(l_res.data) > 0:
                last_record = l_res.data[0]
        except Exception as e:
            logger.debug(f"leads table lookup error: {e}")

    # 3. If still not found, check any call_logs (in case direction wasn't recorded as outbound)
    if not last_record and db is not None:
        try:
            res_any = await db.table("call_logs").select("*") \
                .ilike("phone_number", f"%{clean_10}%") \
                .order("timestamp", desc=True) \
                .limit(1) \
                .execute()
            if res_any.data and len(res_any.data) > 0:
                last_record = res_any.data[0]
        except Exception:
            pass

    if not last_record:
        return {"found": False}

    # Extract base attributes from call_log / lead
    agent_id = str(last_record.get("agent_id") or last_record.get("agent_profile_id") or "").strip()
    agent_name = str(last_record.get("agent_name") or "").strip()
    campaign_id = str(last_record.get("campaign_id") or "").strip()
    lead_name = str(last_record.get("lead_name") or last_record.get("client_name") or last_record.get("name") or "").strip()
    last_call_summary = str(last_record.get("summary") or last_record.get("notes") or last_record.get("reason") or "").strip()
    business_name = str(last_record.get("business_name") or "").strip()
    service_type = str(last_record.get("service_type") or "").strip()
    system_prompt = str(last_record.get("system_prompt") or "").strip()
    agent_voice = str(last_record.get("voice") or last_record.get("agent_voice") or "").strip()
    campaign_name = str(last_record.get("campaign_name") or "").strip()
    broker_phone = str(last_record.get("broker_phone") or "").strip()
    broker_email = str(last_record.get("broker_email") or "").strip()
    calcom_event_type_id = str(last_record.get("calcom_event_type_id") or "").strip()
    project_name = str(last_record.get("project_name") or "").strip()

    # Extract agent metadata from notes field if not directly present in call_logs table
    notes_field = str(last_record.get("notes") or "")
    if notes_field:
        if not agent_id:
            m_id = re.search(r'\[AgentID:\s*([^\]\|]+)', notes_field)
            if m_id:
                agent_id = m_id.group(1).strip()
        if not agent_name:
            m_name = re.search(r'\[Agent:\s*([^\]\|]+)', notes_field)
            if m_name:
                agent_name = m_name.group(1).strip()

    # If campaign_id is present, enrich with campaign metadata
    if campaign_id:
        try:
            camp = await get_campaign(campaign_id)
            if camp:
                if not campaign_name and camp.get("name"):
                    campaign_name = str(camp.get("name")).strip()
                if not agent_id and camp.get("agent_profile_id"):
                    agent_id = str(camp.get("agent_profile_id")).strip()
                if not system_prompt and camp.get("system_prompt"):
                    system_prompt = str(camp.get("system_prompt")).strip()
                if not business_name and camp.get("business_name"):
                    business_name = str(camp.get("business_name")).strip()
                if not service_type and camp.get("service_type"):
                    service_type = str(camp.get("service_type")).strip()
                if not broker_phone and camp.get("broker_phone"):
                    broker_phone = str(camp.get("broker_phone")).strip()
                if not calcom_event_type_id and camp.get("calcom_event_type_id"):
                    calcom_event_type_id = str(camp.get("calcom_event_type_id")).strip()
        except Exception as e:
            logger.debug(f"Campaign enrichment error: {e}")

    # Fetch specific agent profile / persona if agent_id is linked
    agent_record = None
    if agent_id and agent_id.lower() not in ("none", "null", "undefined", "", "default"):
        # Try 'agents' table first
        if db is not None:
            try:
                a_res = await db.table("agents").select("*").eq("id", agent_id).maybe_single().execute()
                if a_res and a_res.data:
                    agent_record = a_res.data
            except Exception as e:
                logger.debug(f"'agents' table lookup error: {e}")

        # Try 'agent_profiles' table in Supabase
        if not agent_record and db is not None:
            try:
                ap_res = await db.table("agent_profiles").select("*").eq("id", agent_id).maybe_single().execute()
                if ap_res and ap_res.data:
                    agent_record = ap_res.data
            except Exception as e:
                logger.debug(f"'agent_profiles' table lookup error: {e}")

        # Fallback to get_agent_profile in db.py (checks SQLite if needed)
        if not agent_record:
            try:
                agent_record = await get_agent_profile(agent_id)
            except Exception as e:
                logger.debug(f"get_agent_profile lookup error: {e}")

    # If no agent_record found by id but we have an agent_name, try to match by name
    if not agent_record and agent_name and db is not None:
        try:
            a_res = await db.table("agent_profiles").select("*").ilike("name", agent_name).limit(1).execute()
            if a_res and a_res.data and len(a_res.data) > 0:
                agent_record = a_res.data[0]
        except Exception:
            pass

    # If STILL no agent_record found, intelligently fallback to the most recent agent profile
    if not agent_record and db is not None:
        try:
            rec_res = await db.table("agent_profiles").select("*").order("updated_at", desc=True).limit(1).execute()
            if not rec_res or not rec_res.data:
                rec_res = await db.table("agent_profiles").select("*").order("created_at", desc=True).limit(1).execute()
            if rec_res and rec_res.data and len(rec_res.data) > 0:
                agent_record = rec_res.data[0]
        except Exception as rec_err:
            logger.debug(f"Fallback to latest agent profile in Supabase failed: {rec_err}")

    if not agent_record:
        try:
            all_p = await list_agent_profiles()
            if all_p:
                sorted_p = sorted(all_p, key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
                agent_record = sorted_p[0]
        except Exception:
            pass

    if agent_record:
        agent_id = str(agent_record.get("id") or agent_id).strip()
        agent_name = str(agent_record.get("name") or agent_record.get("agent_name") or agent_name).strip()
        configured_prompt = str(agent_record.get("system_prompt") or "").strip()
        if configured_prompt:
            system_prompt = configured_prompt
        configured_voice = str(agent_record.get("voice") or agent_record.get("agent_voice") or "").strip()
        if configured_voice:
            agent_voice = configured_voice
        if not business_name and agent_record.get("business_name"):
            business_name = str(agent_record.get("business_name")).strip()
        if not service_type and agent_record.get("service_type"):
            service_type = str(agent_record.get("service_type")).strip()
        if not broker_phone and (agent_record.get("broker_whatsapp") or agent_record.get("broker_phone")):
            broker_phone = str(agent_record.get("broker_whatsapp") or agent_record.get("broker_phone")).strip()
        if not broker_email and agent_record.get("broker_email"):
            broker_email = str(agent_record.get("broker_email")).strip()
        if not calcom_event_type_id and agent_record.get("calcom_event_type_id"):
            calcom_event_type_id = str(agent_record.get("calcom_event_type_id")).strip()

    # Sanity checks
    if not project_name:
        project_name = business_name or service_type

    return {
        "found": True,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "agent_voice": agent_voice,
        "business_name": business_name,
        "service_type": service_type,
        "system_prompt": system_prompt,
        "campaign_id": campaign_id,
        "campaign_name": campaign_name,
        "last_call_summary": last_call_summary,
        "lead_name": lead_name,
        "broker_phone": broker_phone,
        "broker_email": broker_email,
        "calcom_event_type_id": calcom_event_type_id,
        "project_name": project_name,
    }

def build_inbound_callback_context_block(
    campaign_name: str = "",
    agent_name: str = "",
    last_call_summary: str = "",
    lead_name: str = "",
    business_name: str = ""
) -> str:
    """
    Constructs the exact required [INBOUND CALLBACK CONTEXT] prompt block
    to inject into the system prompt for returning callback callers.
    """
    camp_disp = campaign_name.strip() if campaign_name and campaign_name.strip() else ""
    agent_disp = agent_name.strip() if agent_name and agent_name.strip() else "your dedicated executive"
    lead_disp = lead_name.strip() if lead_name and lead_name.strip().lower() not in ("there", "caller", "unknown", "lead", "") else "Caller"
    biz_disp = business_name.strip() if business_name and business_name.strip() else ""

    # Clean technical tags out of summary display
    clean_summary = last_call_summary.strip() if last_call_summary else ""
    clean_summary = re.sub(r'\[AgentID:[^\]]+\]', '', clean_summary)
    clean_summary = re.sub(r'\[Agent:[^\]]+\]', '', clean_summary).strip()
    if not clean_summary:
        clean_summary = "An outbound call was initiated earlier"

    campaign_ref = f" ({camp_disp})" if camp_disp else ""
    biz_intro = f" representing {biz_disp}" if biz_disp else ""

    return f"""[INBOUND CALLBACK CONTEXT]
The customer is calling back after an earlier outbound call from {agent_disp}{biz_intro}{campaign_ref}.
Prior interaction summary: {clean_summary}.
ROLE CONTINUITY: You are strictly {agent_disp}{biz_intro}. Maintain full persona, language, and topic continuity based on your instructions above.
Acknowledge the callback naturally in your opening sentence in the appropriate conversation language."""
