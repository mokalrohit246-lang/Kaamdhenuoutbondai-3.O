import asyncio
import json
import logging
import os
import re
import ssl
import time
from datetime import datetime, timezone, timedelta
import certifi
from dotenv import load_dotenv

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
from livekit.plugins import noise_cancellation, silero

_google_realtime = None
_google_llm = None
_google_tts = None
_deepgram_stt = None

try:
    from livekit.plugins import google as _gp
    _google_realtime = getattr(getattr(_gp, "realtime", None), "RealtimeModel", None) or getattr(getattr(getattr(_gp, "beta", None), "realtime", None), "RealtimeModel", None)
    _google_llm = getattr(_gp, "LLM", None)
    _google_tts = getattr(_gp, "TTS", None)
except ImportError:
    pass

try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = getattr(_dg, "STT", None)
except ImportError:
    pass

from db import (
    push_unified_log, log_call, save_call_log, find_recent_outbound_context,
    add_campaign_minutes, find_campaign_by_inbound_number, get_agent_profile,
    insert_appointment, book_appointment, normalize_phone, save_callback
)
from prompts import build_prompt, get_base_system_prompt, GLOBAL_NATURAL_CONVERSATION_LAYER, DYNAMIC_LANGUAGE_MIRRORING_LAYER
from tools import RealEstateTools

load_dotenv(".env", override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaamdhenu-agent")

def _build_session(tools: list, system_prompt: str, voice: str = "") -> AgentSession:
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")
    gemini_voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    voice_engine = os.getenv("VOICE_ENGINE", "realtime").lower()
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false" and voice_engine == "realtime"

    # ULTRA-FAST PURE GEMINI REALTIME
    if use_realtime and _google_realtime is not None:
        try:
            from google.genai import types as _gt
            input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=500,
                    prefix_padding_ms=100
                )
            )
            return AgentSession(
                llm=_google_realtime(
                    model=gemini_model,
                    voice=gemini_voice,
                    instructions=system_prompt,
                    realtime_input_config=input_cfg
                ),
                tools=tools
            )
        except Exception:
            return AgentSession(
                llm=_google_realtime(model=gemini_model, voice=gemini_voice, instructions=system_prompt),
                tools=tools
            )

    # PIPELINE FALLBACK
    stt = _deepgram_stt(model=os.getenv("STT_MODEL", "nova-3"), language="multi") if _deepgram_stt and os.getenv("DEEPGRAM_API_KEY") else None
    tts = _google_tts(voice_name=gemini_voice) if _google_tts else None
    return AgentSession(
        stt=stt,
        llm=_google_llm(model="gemini-2.0-flash") if _google_llm else None,
        tts=tts,
        vad=silero.VAD.load(),
        tools=tools
    )

class KaamdhenuAssistant(Agent):
    def __init__(self, instructions: str):
        super().__init__(instructions=instructions)

def extract_site_visit_details_from_transcript(transcript: str, client_location: str = "") -> tuple:
    """
    Extracts (site_visit_date, pickup_required, pickup_location) from conversation transcript.
    """
    if not transcript:
        return "", False, ""

    text = transcript.lower()
    from datetime import datetime as _dt, timedelta as _td
    now = _dt.now()

    # 1. Extract Date
    visit_date = None
    m_iso = re.search(r'\b(202\d-[01]\d-[0-3]\d)\b', transcript)
    if m_iso:
        visit_date = m_iso.group(1)

    if not visit_date:
        m_slash = re.search(r'\b([0-3]?\d)[/-]([01]?\d)(?:[/-](202\d))?\b', transcript)
        if m_slash:
            d = int(m_slash.group(1))
            m = int(m_slash.group(2))
            y = int(m_slash.group(3)) if m_slash.group(3) else now.year
            try:
                visit_date = f"{y:04d}-{m:02d}-{d:02d}"
            except Exception:
                pass

    if not visit_date:
        if any(w in text for w in ["parson", "day after tomorrow", "parva", "परवा", "param divse"]):
            visit_date = (now + _td(days=2)).strftime("%Y-%m-%d")
        elif any(w in text for w in ["kal", "tomorrow", "udya", "उद्या", "kaale", "काले", "naale", "repu"]):
            visit_date = (now + _td(days=1)).strftime("%Y-%m-%d")
        elif any(w in text for w in ["aaj", "today", "aaje", "आज", "innu", "eeroju"]):
            visit_date = now.strftime("%Y-%m-%d")
        elif "weekend" in text:
            days = (5 - now.weekday()) % 7
            if days == 0: days = 7
            visit_date = (now + _td(days=days)).strftime("%Y-%m-%d")
        else:
            weekdays = {
                "monday": 0, "somwar": 0, "somvar": 0,
                "tuesday": 1, "mangalwar": 1, "mangalvar": 1,
                "wednesday": 2, "budhwar": 2, "budhvar": 2,
                "thursday": 3, "guruwar": 3, "guruvar": 3,
                "friday": 4, "shukrawar": 4, "shukravar": 4,
                "saturday": 5, "shanivar": 5, "shaniwar": 5,
                "sunday": 6, "ravivar": 6, "raviwar": 6, "itwar": 6
            }
            for wname, wday in weekdays.items():
                if wname in text:
                    days_ahead = (wday - now.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    visit_date = (now + _td(days=days_ahead)).strftime("%Y-%m-%d")
                    break

    if not visit_date:
        visit_date = (now + _td(days=1)).strftime("%Y-%m-%d")

    # 2. Extract Time
    visit_time = "11:00"
    m_t12 = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)', text)
    m_baje = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(?:baje)', text)
    m_t24 = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', text)

    if m_t12:
        h = int(m_t12.group(1))
        mn = int(m_t12.group(2) or 0)
        ampm = m_t12.group(3)
        if ampm == "pm" and h < 12: h += 12
        elif ampm == "am" and h == 12: h = 0
        visit_time = f"{h:02d}:{mn:02d}"
    elif m_t24:
        h = int(m_t24.group(1))
        mn = int(m_t24.group(2))
        visit_time = f"{h:02d}:{mn:02d}"
    elif m_baje:
        h = int(m_baje.group(1))
        mn = int(m_baje.group(2) or 0)
        if ("shaam" in text or "dopahar" in text or "raat" in text) and h < 12:
            h += 12
        elif h in (1, 2, 3, 4, 5, 6, 7):
            h += 12
        visit_time = f"{h:02d}:{mn:02d}"

    full_visit_dt = f"{visit_date} {visit_time}"

    # 3. Extract Pickup
    pickup_required = False
    has_cab_mention = any(k in text for k in ["cab", "pickup", "pick up", "gaadi", "car", "uber", "ola", "drop"])
    if has_cab_mention:
        negative = any(neg in text for neg in ["apni gaadi", "own car", "khud aa", "self drive", "cab nahi", "pickup nahi", "no cab", "no pickup", "nahi chahiye"])
        if not negative:
            pickup_required = True

    # 4. Extract Pickup Location
    pickup_loc = client_location or ""
    m_loc = re.search(r'(?:pickup\s+(?:from|at|address)|from|at|se)\s+([a-zA-Z0-9\s,\-\.]{3,25})(?:se|pe|mein|par|\.|\,|$)', transcript, re.IGNORECASE)
    if m_loc:
        candidate = m_loc.group(1).strip()
        if candidate.lower() not in ["home", "office", "ghar", "site", "project", "tomorrow", "kal", "there", "kaamdhenu", "station"]:
            pickup_loc = candidate

    return full_visit_dt, pickup_required, pickup_loc

async def extract_crm_qualification_from_transcript(transcript: str, fallback_data: dict) -> dict:
    """
    Extracts structured CRM qualification JSON from conversation transcript via Gemini API
    with robust heuristic fallback.
    """
    default_result = {
        "location": fallback_data.get("location", "") or "",
        "job_profession": fallback_data.get("occupation", "") or "",
        "bhk": fallback_data.get("bhk", "") or "",
        "budget": fallback_data.get("budget", "") or "",
        "timeline": fallback_data.get("possession", "Ready-to-Move") or "Ready-to-Move",
        "funding_type": fallback_data.get("funding", "Bank Loan") or "Bank Loan",
        "lead_score": fallback_data.get("lead_score", "Cold") or "Cold",
        "site_visit_interest": f"Yes ({fallback_data.get('site_visit')})" if fallback_data.get("site_visit") else "No",
        "cab_required": "Yes" if fallback_data.get("pickup") else "No",
        "main_objection": fallback_data.get("objection") or "None",
        "whatsapp_consent": False
    }

    if not transcript or len(transcript.strip()) < 10:
        return default_result

    # 1. Attempt structured Gemini LLM extraction via REST API
    google_api_key = os.getenv("GOOGLE_API_KEY", "")
    if google_api_key:
        try:
            import httpx
            prompt_text = f"""You are an expert Real Estate CRM Extraction Analyst.
Analyze the following conversation transcript between a real estate sales agent and a lead.
Extract the customer qualification details into this EXACT JSON structure with these exact keys:
{{
    "location": "extracted location or empty",
    "job_profession": "Job / Business / Self-Employed or empty",
    "bhk": "1BHK / 2BHK / 3BHK or empty",
    "budget": "extracted budget string or empty",
    "timeline": "Ready-to-Move / Under-Construction / 3-6 Months",
    "funding_type": "Bank Loan / Self-Funding",
    "lead_score": "Hot / Warm / Cold",
    "site_visit_interest": "Yes (Date/Time) / No / Maybe",
    "cab_required": "Yes / No",
    "main_objection": "Extracted objection or None",
    "whatsapp_consent": true
}}

Rules:
1. "location": Area where client lives or wants property (e.g. Dombivli, Kalyan, Thane).
2. "job_profession": IT, Corporate, Business, Self-Employed, etc.
3. "bhk": Preferred BHK (e.g. 1BHK, 2BHK, 3BHK).
4. "budget": Budget mentioned (e.g. 50L, 75 Lakhs, 1 Cr, etc.).
5. "timeline": Ready-to-Move, Under-Construction, 3-6 Months, etc.
6. "funding_type": Bank Loan or Self-Funding.
7. "lead_score": "Hot" if site visit agreed, "Warm" if engaged/callback requested, "Cold" if disinterested/no answer.
8. "site_visit_interest": "Yes (Date/Time)", "No", or "Maybe".
9. "cab_required": "Yes" if user wants/agreed to complimentary pickup cab, else "No".
10. "main_objection": Reason for hesitation (e.g. "Price too high", "Looking in different area", "Call later", or "None").
11. "whatsapp_consent": true if client agreed to receive brochure/details on WhatsApp, false otherwise.

Conversation Transcript:
{transcript}
"""
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={google_api_key}"
            payload = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {"response_mime_type": "application/json"}
            }
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        raw_json_str = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                        parsed = json.loads(raw_json_str)
                        if isinstance(parsed, dict):
                            for k, v in parsed.items():
                                if v is not None and v != "":
                                    default_result[k] = v
                            return default_result
        except Exception as llm_err:
            logger.warning(f"LLM structured extraction warning: {llm_err}")

    # 2. Heuristic extraction fallback
    text_lower = transcript.lower()
    if "3bhk" in text_lower or "3 bhk" in text_lower:
        default_result["bhk"] = "3BHK"
    elif "2bhk" in text_lower or "2 bhk" in text_lower:
        default_result["bhk"] = "2BHK"
    elif "1bhk" in text_lower or "1 bhk" in text_lower:
        default_result["bhk"] = "1BHK"

    m_loc = re.search(r'(?:rehte|rehta|rehti|live\s+in|location|staying\s+at)\s+([a-zA-Z\s]{3,20})', transcript, re.IGNORECASE)
    if m_loc and not default_result["location"]:
        cand = m_loc.group(1).strip()
        if cand.lower() not in ("hai", "hoon", "nahi", "yes", "sir", "madam"):
            default_result["location"] = cand

    if any(k in text_lower for k in ["it", "software", "tech", "developer", "engineer", "corporate", "job", "service", "salary"]):
        default_result["job_profession"] = "Corporate / Job"
    elif any(k in text_lower for k in ["business", "vyapar", "dukaan", "shop", "own work", "self"]):
        default_result["job_profession"] = "Business / Self-Employed"

    if any(k in text_lower for k in ["loan", "bank", "emi", "finance"]):
        default_result["funding_type"] = "Bank Loan"
    elif any(k in text_lower for k in ["own funds", "self fund", "cash", "direct"]):
        default_result["funding_type"] = "Self-Funding"

    if any(k in text_lower for k in ["ready", "turant", "immediately"]):
        default_result["timeline"] = "Ready-to-Move"
    elif any(k in text_lower for k in ["under-construction", "construction", "next year"]):
        default_result["timeline"] = "Under-Construction"

    if any(k in text_lower for k in ["whatsapp pe bhejo", "whatsapp bhej do", "whatsapp kar do", "send on whatsapp", "haan whatsapp", "bhej dijiye"]):
        default_result["whatsapp_consent"] = True

    return default_result

async def entrypoint(ctx: agents.JobContext):
    call_id = ctx.room.name
    call_start_time = time.time()
    await push_unified_log("LiveKit", "info", f"Job connected: {call_id}", call_id=call_id)

    direction = "outbound"
    phone_number = ""
    lead_name = "there"
    business_name = "Kaamdhenu Real Estate"
    service_type = "Luxury Properties"
    agent_name = "Priya"
    agent_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    campaign_id = None
    broker_phone = None
    broker_email = None
    calcom_api_key = None
    calcom_event_type_id = None
    custom_prompt = None
    tool_ctx = None
    log_category = "general"
    lead_notes = ""
    lead_bhk = ""
    lead_budget = ""

    try:
        # ===================================================================
        # PHASE 1: PARSE METADATA (instant, no I/O)
        # ===================================================================
        raw_meta = ctx.job.metadata or ""
        if not raw_meta and ctx.room and ctx.room.metadata:
            raw_meta = ctx.room.metadata

        if raw_meta:
            try:
                m = json.loads(raw_meta)
                direction = m.get("direction", "outbound")
                phone_number = m.get("phone_number", "")
                lead_name = m.get("lead_name", lead_name)
                business_name = m.get("business_name", business_name)
                service_type = m.get("service_type", service_type)
                agent_name = m.get("agent_name", agent_name)
                agent_voice = m.get("agent_voice") or agent_voice
                campaign_id = m.get("campaign_id")
                broker_phone = m.get("broker_phone")
                broker_email = m.get("broker_email")
                calcom_api_key = m.get("calcom_api_key")
                calcom_event_type_id = m.get("calcom_event_type_id")
                custom_prompt = m.get("system_prompt")
                lead_notes = m.get("notes") or m.get("context") or m.get("additional_info") or ""
                lead_bhk = m.get("bhk") or m.get("bhk_requirement") or ""
                lead_budget = m.get("budget") or ""
            except Exception as e:
                logger.warning(f"Metadata parse warning: {e}")

        # Inbound detection (instant, no I/O)
        if "inbound" in call_id.lower() or not phone_number:
            direction = "inbound"
            lead_name = "Caller"
            match = re.search(r'(\d{10,12})', call_id)
            phone_number = f"+{match.group(1)}" if match else "+918065353767"
            log_category = "direct_inbound"

        # ===================================================================
        # PHASE 2: CONNECT IMMEDIATELY (minimize latency — no DB before this)
        # ===================================================================
        await ctx.connect()
        await push_unified_log("SIP", "info", f"Room connected ({direction}): {phone_number}", call_id=call_id)

        # ===================================================================
        # PHASE 3: ASYNC DB LOOKUPS (only for inbound, run concurrently)
        # ===================================================================
        if direction == "inbound":
            # Run both lookups concurrently to minimize delay
            camp_task = asyncio.create_task(find_campaign_by_inbound_number(phone_number))
            ctx_task = asyncio.create_task(find_recent_outbound_context(phone_number))

            camp = await camp_task
            if camp:
                campaign_id = camp.get("id")
                log_category = "dedicated_inbound"
                ag_id = camp.get("agent_profile_id")
                if ag_id:
                    ag_prof = await get_agent_profile(ag_id)
                    if ag_prof:
                        agent_name = ag_prof.get("agent_name") or agent_name
                        agent_voice = ag_prof.get("voice") or agent_voice
                        business_name = ag_prof.get("business_name") or business_name
                        service_type = ag_prof.get("service_type") or service_type
                        broker_phone = ag_prof.get("broker_phone") or broker_phone
                        broker_email = ag_prof.get("broker_email") or broker_email
                        custom_prompt = ag_prof.get("system_prompt") or custom_prompt
                        calcom_event_type_id = ag_prof.get("calcom_event_type_id") or camp.get("calcom_event_type_id") or calcom_event_type_id
                await push_unified_log("CRM", "info", f"Dedicated inbound matched campaign: {camp.get('name')}", call_id=call_id)
            else:
                ctx_info = await ctx_task
                if ctx_info.get("found"):
                    campaign_id = ctx_info.get("campaign_id") or campaign_id
                    lead_name = ctx_info.get("lead_name", lead_name)
                    # CRITICAL: project_name must NEVER equal the lead's own name
                    proj_name = ctx_info.get("project_name") or ""
                    if proj_name and lead_name and proj_name.strip().lower() == lead_name.strip().lower():
                        proj_name = ""
                    proj_name = proj_name or business_name
                    log_category = "campaign_callback"
                    custom_prompt = (
                        f"You are {agent_name}, Senior Property Consultant for Kaamdhenu Real Estate.\n"
                        f"Important context: The client {lead_name} was recently called regarding {proj_name}.\n"
                        f"Greeting: Speak this opening line: 'Namaste {lead_name}! Main {agent_name} Kaamdhenu Real Estate se baat kar rahi hoon. Aapko humare {proj_name} ke regarding call gaya tha... Batayein main aapki kya madad kar sakti hoon?'\n"
                        f"Speak naturally, instantly mirroring the caller's language (Hindi, Marathi, Gujarati, English, etc.) without announcing the switch."
                    )
                    await push_unified_log("CRM", "info", f"Callback detected from prior campaign lead: {lead_name}", call_id=call_id)

        # ===================================================================
        # PHASE 4: BUILD SYSTEM PROMPT
        # ===================================================================
        system_prompt = get_base_system_prompt(
            agent_name=agent_name,
            business_name=business_name,
            custom_prompt=custom_prompt,
            lead_name=lead_name,
            service_type=service_type
        )

        # Ensure language mirroring layer is present
        if "DYNAMIC ZERO-SHOT LANGUAGE MIRRORING" not in system_prompt:
            system_prompt = system_prompt + "\n\n" + DYNAMIC_LANGUAGE_MIRRORING_LAYER.strip()

        # Append strict real estate rules to prevent lead-name-as-project confusion
        strict_rules = f"""
[STRICT REAL ESTATE RULES]
- THE CALLER'S NAME IS NEVER A PROPERTY OR PROJECT NAME.
- If the user's name is {lead_name}, NEVER say "{lead_name} project" or "{lead_name} property".
- You represent {business_name} projects. If no specific project was previously chosen, ask open-endedly: "Aap kis location ya project ke baare mein jaankari lena chahte hain?"
- NEVER confuse the person's identity with the property name.
"""
        system_prompt = system_prompt + "\n" + strict_rules

        # === DYNAMIC LEAD CONTEXT INJECTION FOR OUTBOUND CALLS ===
        valid_lead_name = lead_name and lead_name.strip() and lead_name.strip().lower() not in ("there", "caller", "unknown", "lead", "")
        if direction == "outbound" and valid_lead_name:
            notes_str = lead_notes.strip() if lead_notes else "None"
            bhk_str = lead_bhk.strip() if lead_bhk else ""
            budget_str = lead_budget.strip() if lead_budget else ""
            context_parts = [notes_str]
            if bhk_str:
                context_parts.append(f"BHK: {bhk_str}")
            if budget_str:
                context_parts.append(f"Budget: {budget_str}")
            context_detail = " | ".join(p for p in context_parts if p and p != "None")

            dynamic_lead_instruction = f"""
[CRITICAL CALL CONTEXT - OUTBOUND LEAD DETAILS]
- You are placing an outbound call to: {lead_name}
- Phone: {phone_number}
- Known Details / Notes: {context_detail or 'None'}
- STRICT RULE: You ALREADY KNOW the user's name is {lead_name}.
- ABSOLUTELY NEVER ask "Aapka naam kya hai?" or "May I know your name?" or any variation.
- ZERO FALSE CLAIMS: ABSOLUTELY NEVER say "Aapne inquiry ki thi", "Aapne property mein interest dikhaya tha", or claim prior inquiry.
- Introduce yourself as a professional luxury real estate consultant presenting Kaamdhenu's premium residential properties.
- Dynamic Greeting: Greet with IST time of day (good morning / afternoon / evening) and ask permission: "Kya abhi aapse do minute baat ho sakti hai?"
"""
            system_prompt = system_prompt + "\n" + dynamic_lead_instruction
            await push_unified_log("Agent", "info", f"Dynamic lead context injected for {lead_name} ({phone_number})", call_id=call_id)

        tool_ctx = RealEstateTools(
            ctx,
            phone_number=phone_number,
            lead_name=lead_name,
            direction=direction,
            call_id=call_id,
            campaign_id=campaign_id,
            broker_phone=broker_phone,
            broker_email=broker_email,
            calcom_api_key=calcom_api_key,
            calcom_event_type_id=calcom_event_type_id
        )
        # Pre-populate qualification data from metadata so agent has context from the start
        if valid_lead_name:
            tool_ctx.client_name = lead_name
        if lead_bhk:
            tool_ctx.bhk_requirement = lead_bhk
        if lead_budget:
            tool_ctx.budget = lead_budget

        session = _build_session(tools=tool_ctx.get_all_tools(), system_prompt=system_prompt, voice=agent_voice)

        session_start_task = asyncio.create_task(session.start(
            room=ctx.room,
            agent=KaamdhenuAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())
        ))

        transcript_entries = []
        def _record_speech(speaker: str, text: str):
            if text and str(text).strip():
                transcript_entries.append(f"{speaker}: {str(text).strip()}")

        try:
            @session.on("user_speech_committed")
            def _on_user_speech(msg):
                content = getattr(msg, "content", "") or getattr(msg, "text", "")
                if isinstance(content, list):
                    content = " ".join(str(getattr(x, "text", x)) for x in content)
                _record_speech("Client", str(content))

            @session.on("agent_speech_committed")
            def _on_agent_speech(msg):
                content = getattr(msg, "content", "") or getattr(msg, "text", "")
                if isinstance(content, list):
                    content = " ".join(str(getattr(x, "text", x)) for x in content)
                _record_speech("Agent", str(content))
        except Exception:
            pass

        # Outbound dial
        if direction == "outbound" and phone_number:
            trunk_id = os.getenv("OUTBOUND_TRUNK_ID")
            if trunk_id:
                try:
                    await push_unified_log("SIP", "info", f"Pre-warmed dialing to {phone_number}...", call_id=call_id)
                    await ctx.api.sip.create_sip_participant(
                        api.CreateSIPParticipantRequest(
                            room_name=ctx.room.name,
                            sip_trunk_id=trunk_id,
                            sip_call_to=phone_number,
                            participant_identity=f"sip_{phone_number}",
                            wait_until_answered=True
                        )
                    )
                    await push_unified_log("SIP", "info", f"Call answered by {phone_number}", call_id=call_id)
                except Exception as dial_err:
                    await push_unified_log("SIP", "error", f"Dial failed: {dial_err}", call_id=call_id)
                    ctx.shutdown()
                    return

        await session_start_task
        await push_unified_log("Gemini", "info", f"Gemini Live Realtime session active for {agent_name}", call_id=call_id)

        # Opening greeting - dynamic time-based greeting & permission check for outbound calls
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        ist_hour = ist_now.hour
        if 5 <= ist_hour < 12:
            time_greeting = "good morning"
        elif 12 <= ist_hour < 17:
            time_greeting = "good afternoon"
        else:
            time_greeting = "good evening"

        if direction == "inbound":
            greeting_text = f"Namaste! Thank you for calling {business_name}. I am {agent_name}. How can I help you today?"
        elif valid_lead_name:
            greeting_text = f"Namaste {lead_name} ji, {time_greeting}! Main {agent_name} baat kar rahi hoon {business_name} se. Kya abhi aapse do minute baat ho sakti hai?"
        else:
            greeting_text = f"Namaste sir, {time_greeting}! Main {agent_name} baat kar rahi hoon {business_name} se. Kya abhi aapse do minute baat ho sakti hai?"

        try:
            greeting_instruction = (
                f"Speak this opening line naturally: '{greeting_text}'. "
                f"ABSOLUTE BAN: STRICTLY FORBIDDEN to say 'Aapne inquiry ki thi' or claim the lead made a prior inquiry. "
                f"If they say YES or are open to talk, say: 'Thank you! Hum Kalyan-Dombivli aur Thane region mein luxury 1, 2 aur 3 BHK homes offer kar rahe hain. Kya aap filhal apne rehne ke liye ya investment ke purpose se koi residential property dekh rahe hain?' "
                f"If they say NO or BUSY: Respectfully handle callback scheduling without pitching. "
                f"Seamlessly mirror whatever language the user speaks on their reply without announcing or commenting on language changes."
            )
            if valid_lead_name:
                greeting_instruction += f" IMPORTANT: You already know the customer's name is {lead_name}. Do NOT ask for their name."
            await session.generate_reply(instructions=greeting_instruction)
            await push_unified_log("Gemini", "info", f"Autonomous cold-call greeting delivered by {agent_name} to {lead_name}", call_id=call_id)
        except Exception as ge:
            logger.warning(f"Greeting error: {ge}")

        done_event = asyncio.Event()
        def _on_part_disconnected(p: rtc.RemoteParticipant):
            if p.identity.startswith("sip_"):
                done_event.set()
        ctx.room.on("participant_disconnected", _on_part_disconnected)
        ctx.room.on("disconnected", lambda: done_event.set())

        try:
            await asyncio.wait_for(done_event.wait(), timeout=1800)
        except asyncio.TimeoutError:
            pass

    except Exception as general_err:
        await push_unified_log("Agent", "error", f"Call runtime error: {general_err}", call_id=call_id)
        logger.error(f"General error in entrypoint: {general_err}", exc_info=True)

    finally:
        # ALWAYS LOG CALL DATA WITH ALL QUALIFICATION FIELDS
        dur = max(1, int(time.time() - call_start_time))
        cost_inr = round((dur / 60.0) * 1.22, 2)
        clean_phone = phone_number or "Unknown"

        # Minute quota deduction
        dur_mins = round(dur / 60.0, 2)
        if campaign_id:
            try:
                await add_campaign_minutes(campaign_id, dur_mins)
            except Exception:
                pass

        # Extract from tool_ctx if available
        t_client_name = getattr(tool_ctx, "client_name", "") or lead_name
        t_location = getattr(tool_ctx, "current_location", "")
        t_occupation = getattr(tool_ctx, "occupation", "")
        t_bhk = getattr(tool_ctx, "bhk_requirement", "")
        t_budget = getattr(tool_ctx, "budget", "")
        t_purpose = getattr(tool_ctx, "purpose", "Self-Use")
        t_possession = getattr(tool_ctx, "possession_timeline", "Ready-to-Move")
        t_funding = getattr(tool_ctx, "funding_type", "Bank Loan")
        t_lead_score = getattr(tool_ctx, "lead_score", "Warm" if dur > 15 else "Cold")
        t_commitment = getattr(tool_ctx, "commitment_risk", "Low")
        t_site_visit = getattr(tool_ctx, "site_visit_date", "")
        t_pickup = getattr(tool_ctx, "pickup_required", False)
        t_pickup_loc = getattr(tool_ctx, "pickup_location", "")
        t_callback = getattr(tool_ctx, "next_callback", "")
        t_objection = getattr(tool_ctx, "objection", "")
        t_whatsapp = getattr(tool_ctx, "whatsapp_status", "— Not Requested")
        t_outcome = getattr(tool_ctx, "outcome", "completed")

        # Collect full conversation transcript
        if not transcript_entries and session:
            try:
                chat_ctx = getattr(session, "history", None) or getattr(session, "_chat_ctx", None)
                if chat_ctx and hasattr(chat_ctx, "messages"):
                    for m in chat_ctx.messages:
                        r = getattr(m, "role", "")
                        if r in ("user", "assistant"):
                            spk = "Client" if r == "user" else "Agent"
                            c = getattr(m, "content", "") or getattr(m, "text", "")
                            if isinstance(c, list):
                                c = " ".join(str(getattr(x, "text", x)) for x in c)
                            if c:
                                transcript_entries.append(f"{spk}: {c}")
            except Exception:
                pass

        full_transcript = "\n".join(transcript_entries)

        # Fallback / Secondary extraction if visit was agreed but site_visit_date is empty
        if not t_site_visit or not t_site_visit.strip():
            summary_lower = (t_outcome or "").lower() + " " + (t_bhk or "").lower()
            transcript_lower = full_transcript.lower()
            visit_agreed = (
                "booked" in t_outcome.lower() or
                "site visit" in transcript_lower or
                "cab pickup" in transcript_lower or
                "pickup cab" in transcript_lower or
                any(phrase in transcript_lower for phrase in [
                    "visit arrange", "visit confirm", "visit karenge", "visit ke liye", "dekhne aunga", "dekhne aungi",
                    "visit plan", "gaadi bhej", "cab bhej", "bhet dyayla", "baghayla yeto", "baghayla yete",
                    "yeto me", "yete me", "aavish", "joisu", "visit karu", "visit karuya", "bhet gheu"
                ])
            ) and (
                any(pos in transcript_lower for pos in [
                    "haan", "yes", "sure", "bilkul", "theek hai", "thik hai", "done", "confirm", "chalega",
                    "aunga", "aungi", "bhej do", "bhej dena", "thik", "ho", "nakki", "chalel", "barobar",
                    "aaho", "yeto", "yete", "aavish", "ha", "sari"
                ])
                or "booked" in t_outcome.lower()
            )
            if visit_agreed:
                rec_dt, rec_cab, rec_loc = extract_site_visit_details_from_transcript(full_transcript, client_location=t_location)
                if rec_dt:
                    t_site_visit = rec_dt
                    t_pickup = rec_cab
                    t_pickup_loc = rec_loc or t_location
                    t_outcome = "booked"
                    t_lead_score = "Hot"
                    await push_unified_log("Appointments", "info", f"Transcript fallback extracted Site Visit: {t_site_visit} (Cab: {t_pickup}, Loc: {t_pickup_loc})", call_id=call_id)

        # Automatically insert into appointments table if not already created during call
        if t_site_visit and not getattr(tool_ctx, "appointment_booked", False):
            try:
                parts = t_site_visit.strip().replace("T", " ").split(" ")
                v_date = parts[0] if len(parts) > 0 else t_site_visit
                v_time = parts[1] if len(parts) > 1 else "11:00"
                if len(v_time) == 4 and ":" not in v_time:
                    v_time = f"{v_time[:2]}:{v_time[2:]}"
                clean_phone_digits = re.sub(r'\D', '', clean_phone)
                custom_apt_id = f"apt_{clean_phone_digits}_{int(time.time())}"
                await book_appointment(
                    id=custom_apt_id,
                    name=t_client_name or lead_name or "Lead",
                    phone=clean_phone,
                    date=v_date,
                    time=v_time,
                    service=f"Site Visit ({t_bhk or 'Property'})",
                    budget=t_budget,
                    property_type=t_bhk,
                    pickup_required=bool(t_pickup),
                    pickup_address=t_pickup_loc or "",
                    status="booked"
                )
                await push_unified_log("Appointments", "info", f"✅ Site visit appointment auto-inserted into DB for {clean_phone} on {t_site_visit}", call_id=call_id)
            except Exception as appt_err:
                logger.error(f"Error auto-inserting appointment in wrap-up: {appt_err}")

        # Structured CRM qualification extraction from full transcript
        fallback_data = {
            "location": t_location,
            "occupation": t_occupation,
            "bhk": t_bhk,
            "budget": t_budget,
            "possession": t_possession,
            "funding": t_funding,
            "lead_score": t_lead_score,
            "site_visit": t_site_visit,
            "pickup": t_pickup,
            "objection": t_objection,
            "whatsapp": t_whatsapp
        }
        crm_data = await extract_crm_qualification_from_transcript(full_transcript, fallback_data)

        loc_pref = crm_data.get("location") or t_location
        job_prof = crm_data.get("job_profession") or t_occupation
        bhk_pref = crm_data.get("bhk") or t_bhk
        bud_range = crm_data.get("budget") or t_budget
        time_frame = crm_data.get("timeline") or t_possession
        fund_type = crm_data.get("funding_type") or t_funding
        final_score = crm_data.get("lead_score") or t_lead_score
        sv_interest = crm_data.get("site_visit_interest") or (f"Yes ({t_site_visit})" if t_site_visit else "No")
        cab_req = crm_data.get("cab_required") or ("Yes" if t_pickup else "No")
        main_obj = crm_data.get("main_objection") or t_objection or "None"
        wa_consent = crm_data.get("whatsapp_consent", False)
        wa_status = "✅ Consent Given" if wa_consent else (t_whatsapp if t_whatsapp != "— Not Requested" else "— Not Requested")

        # Resolve recording_url
        rec_url = getattr(tool_ctx, "recording_url", None)
        if not rec_url and call_id:
            s3_endpoint = os.getenv("S3_ENDPOINT_URL", "").rstrip("/")
            s3_bucket = os.getenv("S3_BUCKET", "")
            supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
            if s3_endpoint and s3_bucket:
                rec_url = f"{s3_endpoint}/{s3_bucket}/recordings/{call_id}.mp4"
            elif supabase_url:
                rec_url = f"{supabase_url}/storage/v1/object/public/recordings/{call_id}.mp4"

        visit_str = f" | Visit: {t_site_visit}" if t_site_visit else ""
        cab_str = f" (Cab: {t_pickup_loc or 'Yes'})" if t_pickup else ""
        summary = f"{t_client_name} ({clean_phone}): {bhk_pref or t_bhk or 'TBD'} | Budget: {bud_range or t_budget or 'TBD'} | {t_purpose}{visit_str}{cab_str} | Score: {final_score} | Duration: {dur}s"

        try:
            await log_call(
                call_id=call_id,
                phone_number=clean_phone,
                called_to=os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
                lead_name=t_client_name,
                direction=direction,
                campaign_id=campaign_id,
                outcome=t_outcome,
                lead_score=final_score,
                summary=summary,
                reason="",
                duration_seconds=dur,
                cost_inr=cost_inr,
                recording_url=rec_url,
                # Structured CRM columns
                location_preference=loc_pref,
                job_profile=job_prof,
                bhk_preference=bhk_pref,
                budget_range=bud_range,
                timeline=time_frame,
                funding_type=fund_type,
                site_visit_interest=sv_interest,
                cab_required=cab_req,
                main_objection=main_obj,
                whatsapp_status=wa_status,
                # Legacy column mappings for backward compatibility
                client_name=t_client_name,
                current_location=loc_pref,
                occupation=job_prof,
                bhk_requirement=bhk_pref,
                budget=bud_range,
                purpose=t_purpose,
                possession_timeline=time_frame,
                commitment_risk=t_commitment,
                site_visit_date=t_site_visit,
                pickup_required=(cab_req == "Yes" or t_pickup),
                pickup_location=t_pickup_loc,
                next_callback=t_callback,
                objection=main_obj,
                log_category=log_category,
                callback_dispatched=False
            )
            await push_unified_log("CRM", "info", f"Call log saved ({direction}): {clean_phone} - {dur}s, ₹{cost_inr}", call_id=call_id)
        except Exception as log_err:
            logger.error(f"Failed to write call log: {log_err}")

        await push_unified_log("LiveKit", "info", f"Call session completed for {clean_phone}", call_id=call_id)

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="kaamdhenu-voice-agent"))
