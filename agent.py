import asyncio
import json
import logging
import os
import re
import ssl
import time
from datetime import datetime, timezone, timedelta
import certifi
import urllib.request
import urllib.error
import numpy as np
try:
    import httpx
except ImportError:
    httpx = None
from dotenv import load_dotenv

# Google GenAI types for RealtimeInputConfig
try:
    from google.genai import types as _gt
except ImportError:
    _gt = None

_orig_ssl = ssl.create_default_context
def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)
ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession

try:
    from livekit.plugins import silero
except ImportError:
    silero = None

try:
    from livekit.plugins import noise_cancellation
except ImportError:
    noise_cancellation = None

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
    # Deduplicate tools by name
    seen_names = set()
    unique_tools = []
    for t in tools or []:
        name = getattr(t, "name", None) or getattr(t, "__name__", None) or str(t)
        if name not in seen_names:
            seen_names.add(name)
            unique_tools.append(t)

    SUPPORTED_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
    model_name = os.getenv("GEMINI_MODEL", "").strip()
    # If model is invalid, unsupported, or empty, force the supported model
    if model_name != SUPPORTED_LIVE_MODEL:
        model_name = SUPPORTED_LIVE_MODEL

    google_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None
    gemini_voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    voice_engine = os.getenv("VOICE_ENGINE", "realtime").lower()
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false" and voice_engine == "realtime"

    # ULTRA-FAST PURE GEMINI REALTIME — clean plugin defaults (no custom realtime_input_config to prevent 1011 errors)
    if use_realtime and _google_realtime is not None:
        try:
            rt_kwargs = dict(
                model=model_name,
                voice=gemini_voice,
                instructions=system_prompt,
            )
            if google_api_key:
                rt_kwargs["api_key"] = google_api_key

            return AgentSession(
                llm=_google_realtime(**rt_kwargs),
                tools=unique_tools
            )
        except Exception as e:
            logger.warning(f"Error initializing Google Realtime: {e}")

    # PIPELINE FALLBACK — tuned Silero VAD for stable turn detection
    stt = _deepgram_stt(model=os.getenv("STT_MODEL", "nova-3"), language="multi") if _deepgram_stt and os.getenv("DEEPGRAM_API_KEY") else None
    tts = _google_tts(voice_name=gemini_voice) if _google_tts else None
    return AgentSession(
        stt=stt,
        llm=_google_llm(model="gemini-2.0-flash") if _google_llm else None,
        tts=tts,
        vad=silero.VAD.load(
            min_silence_duration=0.8,
            prefix_padding_duration=0.1
        ),
        tools=unique_tools
    )

class KaamdhenuAssistant(Agent):
    def __init__(self, instructions: str, tools: list = None):
        if tools:
            super().__init__(instructions=instructions, tools=tools)
        else:
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
        "timeline": fallback_data.get("possession", "") or "-",
        "funding_type": fallback_data.get("funding", "") or "-",
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
    "timeline": "Ready-to-Move / Under-Construction / 3-6 Months or - if not mentioned",
    "funding_type": "Bank Loan / Self-Funding or - if not mentioned",
    "lead_score": "Hot / Warm / Cold / Dropped",
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
5. "timeline": ONLY if explicitly discussed by the caller (Ready-to-Move, Under-Construction, 3-6 Months). If NOT explicitly mentioned by the caller, return "-".
6. "funding_type": ONLY if explicitly discussed (Bank Loan or Self-Funding). If NOT mentioned by caller, return "-".
7. "lead_score": "Hot" if site visit booked or brochure requested, "Warm" if caller had active, genuine queries, "Cold" if disinterested/no engagement, "Dropped" if caller hung up quickly (<30s) or rejected immediately.
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
            parsed = None
            if httpx is not None:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            raw_json_str = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                            parsed = json.loads(raw_json_str)
            else:
                def _urllib_gemini():
                    data_bytes = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(url, data=data_bytes, headers={"Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=8.0) as resp:
                        return resp.getcode(), json.loads(resp.read().decode("utf-8"))
                try:
                    g_code, g_data = await asyncio.to_thread(_urllib_gemini)
                    if g_code == 200:
                        candidates = g_data.get("candidates", [])
                        if candidates:
                            raw_json_str = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
                            parsed = json.loads(raw_json_str)
                except Exception as ug_err:
                    logger.warning(f"Gemini urllib extraction fallback: {ug_err}")

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

    if any(k in text_lower for k in ["bank loan", "home loan", "loan karwana", "emi pe", "finance karwana"]):
        default_result["funding_type"] = "Bank Loan"
    elif any(k in text_lower for k in ["own funds", "self fund", "self-funding", "direct cash"]):
        default_result["funding_type"] = "Self-Funding"

    if any(k in text_lower for k in ["ready to move", "ready-to-move", "turant shift", "immediate possession"]):
        default_result["timeline"] = "Ready-to-Move"
    elif any(k in text_lower for k in ["under-construction", "under construction", "next year possession"]):
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
    business_name = os.getenv("BUSINESS_NAME", "")
    service_type = os.getenv("SERVICE_TYPE", "")
    agent_name = os.getenv("AGENT_NAME", "Assistant")
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
    project_name = os.getenv("PROJECT_NAME", "")
    brochure_url = ""
    site_address = ""
    pickup_drop_notes = ""
    project_highlights = ""

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
                agent_name = m.get("agent_name") or m.get("name") or agent_name
                agent_voice = m.get("voice") or m.get("agent_voice") or agent_voice
                campaign_id = m.get("campaign_id")
                broker_phone = m.get("broker_phone")
                broker_email = m.get("broker_email")
                calcom_api_key = m.get("calcom_api_key")
                calcom_event_type_id = m.get("calcom_event_type_id")
                custom_prompt = m.get("custom_prompt") or m.get("instructions") or m.get("system_prompt")
                lead_notes = m.get("notes") or m.get("context") or m.get("additional_info") or ""
                lead_bhk = m.get("bhk") or m.get("bhk_requirement") or ""
                lead_budget = m.get("budget") or ""
                project_name = m.get("project_name") or business_name
                brochure_url = m.get("brochure_url") or ""
                site_address = m.get("site_address") or ""
                pickup_drop_notes = m.get("pickup_drop_notes") or ""
                project_highlights = m.get("project_highlights") or ""
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
        inbound_prior_project = ""  # Tracks prior project context for returning callers
        if direction == "inbound":
            # Run both lookups concurrently to minimize delay
            camp_task = asyncio.create_task(find_campaign_by_inbound_number(phone_number))
            ctx_task = asyncio.create_task(find_recent_outbound_context(phone_number))

            camp = await camp_task
            ctx_info = await ctx_task  # Always resolve — we need this for returning caller memory

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

            # ALWAYS check for returning caller context (even if dedicated campaign matched)
            if ctx_info.get("found"):
                prior_lead = ctx_info.get("lead_name", "")
                # CRITICAL: project_name must NEVER equal the lead's own name
                prior_proj = ctx_info.get("project_name") or ""
                if prior_proj and prior_lead and prior_proj.strip().lower() == prior_lead.strip().lower():
                    prior_proj = ""
                prior_proj = prior_proj or business_name

                # Update lead_name if we found a real name from CRM
                if prior_lead and prior_lead.strip().lower() not in ("there", "caller", "unknown", "lead", ""):
                    lead_name = prior_lead

                # Store prior project for greeting injection
                inbound_prior_project = prior_proj
                project_name = prior_proj

                if not campaign_id:
                    campaign_id = ctx_info.get("campaign_id") or campaign_id
                log_category = log_category if camp else "campaign_callback"

                # Build context-aware system prompt for returning callers
                custom_prompt = (
                    f"You are {agent_name} representing {business_name or 'our service'}.\n"
                    f"CRITICAL CONTEXT: The caller ({lead_name}) was RECENTLY contacted regarding '{prior_proj}'.\n"
                    f"They are calling BACK — this is NOT a cold call. Acknowledge the prior conversation and ask how you can help further.\n"
                    f"Speak naturally, instantly mirroring the caller's language without announcing the switch."
                )
                await push_unified_log("CRM", "info", f"Returning caller detected: {lead_name} (prior context: {prior_proj})", call_id=call_id)
            elif not camp:
                await push_unified_log("CRM", "info", f"Fresh inbound caller — no prior CRM context found", call_id=call_id)

        # ===================================================================
        # PHASE 4: BUILD SYSTEM PROMPT
        # ===================================================================
        # Determine if a custom prompt was supplied vs falling back to default blueprint
        has_custom_prompt = bool(
            custom_prompt
            and custom_prompt.strip()
            and "=== CONVERSATION OBJECTIVES & QUALIFICATION ===" not in custom_prompt
            and "[OUTBOUND COLD CALL SALES BLUEPRINT - CRITICAL RULES]" not in custom_prompt
        )

        if custom_prompt and custom_prompt.strip():
            system_prompt = custom_prompt.strip()
        else:
            system_prompt = get_base_system_prompt(
                agent_name=agent_name,
                business_name=business_name,
                custom_prompt=None,
                lead_name=lead_name,
                service_type=service_type
            )

        # Ensure language mirroring layer is present
        if "DYNAMIC ZERO-SHOT LANGUAGE MIRRORING" not in system_prompt:
            system_prompt = system_prompt + "\n\n" + DYNAMIC_LANGUAGE_MIRRORING_LAYER.strip()

        # Ensure caller name is never confused with an organization or project name
        if not has_custom_prompt and valid_lead_name:
            strict_rules = f"""
[CALLER IDENTITY RULE]
- The user's name is {lead_name}. NEVER confuse the caller's name with an organization, project, or product name.
"""
            system_prompt = system_prompt + "\n" + strict_rules

        # === HUMAN BACKCHANNEL & FILLER BEHAVIOR FOR NATURAL CONVERSATION ===
        conversational_speed_rules = """
[CONVERSATIONAL SPEED & HUMAN FILLERS]
- Respond INSTANTLY without thinking pauses. NEVER leave dead air or awkward silences.
- When the user asks a question, IMMEDIATELY start your response with natural Indian conversational fillers:
  'हाँ जी...', 'जी बिल्कुल...', 'हाँ तो सर...', 'हाँ...', 'Achha...', 'Ji sir...', 'Bilkul...' before answering specifics.
- Keep sentences SHORT, CRISP, and conversational — like a real Indian executive on a phone call.
- NEVER monologue. After every 2-3 sentences, pause briefly to let the caller respond.
- Use active listening sounds: 'Hmm', 'Ji', 'Achha' while the user is speaking.
- If the user is silent for more than 2 seconds, gently prompt: 'Sir/Ma'am, aap sun rahe hain na?' or 'Hello? Main sun rahi hoon.'
"""
        system_prompt = system_prompt + "\n" + conversational_speed_rules

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
- Introduce yourself as a professional consultant representing {business_name}.
- Dynamic Greeting: Greet with IST time of day (good morning / afternoon / evening) and ask permission: "Kya abhi aapse do minute baat ho sakti hai?"
"""
            system_prompt = system_prompt + "\n" + dynamic_lead_instruction
            await push_unified_log("Agent", "info", f"Dynamic lead context injected for {lead_name} ({phone_number})", call_id=call_id)

        # === CRITICAL CALL TERMINATION RULE ===
        call_termination_rules = """
[CRITICAL CALL TERMINATION RULE]
- Whenever the conversation concludes (e.g., user says 'theek hai', 'bye', 'thank you', 'baad mein baat karte hain', or a site visit/callback is scheduled and acknowledged), speak a polite, concise closing line (e.g., 'Dhanyawad, aapse baat karke accha laga. Have a great day!') and IMMEDIATELY invoke the `end_call` tool.
- Never remain silent without calling `end_call` at wrap-up.
"""
        system_prompt = system_prompt + "\n" + call_termination_rules

        done_event = asyncio.Event()
        def _on_part_disconnected(p: rtc.RemoteParticipant):
            if ctx.room.local_participant and p.identity == ctx.room.local_participant.identity:
                return
            done_event.set()
        ctx.room.on("participant_disconnected", _on_part_disconnected)
        ctx.room.on("disconnected", lambda: done_event.set())

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
            calcom_event_type_id=calcom_event_type_id,
            brochure_url=brochure_url,
            project_name=project_name,
            site_address=site_address,
            pickup_drop_notes=pickup_drop_notes,
            project_highlights=project_highlights,
            done_event=done_event
        )
        # Pre-populate qualification data from metadata so agent has context from the start
        if valid_lead_name:
            tool_ctx.client_name = lead_name
        if lead_bhk:
            tool_ctx.bhk_requirement = lead_bhk
        if lead_budget:
            tool_ctx.budget = lead_budget

        # Deduplicate tools by name
        seen_names = set()
        unique_tools = []
        for t in tool_ctx.get_all_tools():
            name = getattr(t, "name", None) or getattr(t, "__name__", None) or str(t)
            if name not in seen_names:
                seen_names.add(name)
                unique_tools.append(t)

        session = _build_session(tools=unique_tools, system_prompt=system_prompt, voice=agent_voice)

        transcript_entries = []
        def _record_speech(speaker: str, text: str):
            if text and str(text).strip():
                transcript_entries.append(f"{speaker}: {str(text).strip()}")

        # ===================================================================
        # PHASE 5: PARALLEL GEMINI PRE-WARMING & OUTBOUND DIALING
        # Parallelize Gemini WebSocket handshake during phone ringing to
        # eliminate dead-air latency upon call answer.
        # ===================================================================
        # Silence & dead-air watchdog tracking
        last_audio_activity = asyncio.get_event_loop().time()
        greeting_delivered = False
        user_turns = 0
        prompted_for_silence = False
        prompt_timestamp = 0.0

        try:
            @session.on("user_speech_committed")
            def _on_user_speech(msg):
                nonlocal last_audio_activity, user_turns, prompted_for_silence
                last_audio_activity = asyncio.get_event_loop().time()
                user_turns += 1
                prompted_for_silence = False
                content = getattr(msg, "content", "") or getattr(msg, "text", "")
                if isinstance(content, list):
                    content = " ".join(str(getattr(x, "text", x)) for x in content)
                _record_speech("Client", str(content))
                if content:
                    asyncio.create_task(
                        push_unified_log("STT", "info", f"User: {str(content)[:120]}", call_id=call_id)
                    )

            @session.on("agent_speech_committed")
            def _on_agent_speech(msg):
                nonlocal last_audio_activity
                last_audio_activity = asyncio.get_event_loop().time()
                content = getattr(msg, "content", "") or getattr(msg, "text", "")
                if isinstance(content, list):
                    content = " ".join(str(getattr(x, "text", x)) for x in content)
                _record_speech("Agent", str(content))

            # Keep last_audio_activity alive on ANY session state transition
            @session.on("user_state_changed")
            def _on_user_state(ev):
                nonlocal last_audio_activity
                last_audio_activity = asyncio.get_event_loop().time()

            @session.on("agent_state_changed")
            def _on_agent_state(ev):
                nonlocal last_audio_activity
                last_audio_activity = asyncio.get_event_loop().time()

            @session.on("conversation_item_added")
            def _on_conv_item(ev):
                nonlocal last_audio_activity
                last_audio_activity = asyncio.get_event_loop().time()

            @session.on("error")
            def _on_session_error(error):
                err_msg = str(error)
                logger.error(f"Gemini Session error for {call_id}: {err_msg}")
                asyncio.create_task(
                    push_unified_log("Gemini", "error", f"Session error: {err_msg}", call_id=call_id)
                )
        except Exception:
            pass

        # ---------------------------------------------------------------
        # Lightweight Audio Activity Monitor: detects caller speech energy
        # without event loop starvation (no numpy, throttled sampling)
        # ---------------------------------------------------------------
        monitored_tracks = set()
        audio_monitor_tasks = []

        async def _monitor_audio_track(track: rtc.Track):
            nonlocal last_audio_activity
            try:
                audio_stream = rtc.AudioStream(track)
                last_sample = 0.0
                async for ev in audio_stream:
                    now = time.monotonic()
                    # Downsample: sample at most once every 300ms to keep event loop under 1ms
                    if now - last_sample < 0.3:
                        continue
                    last_sample = now

                    frame = ev.frame
                    if frame and frame.data:
                        pcm_bytes = frame.data
                        # Ultra-fast raw-byte peak amplitude check (no numpy allocations)
                        # 16-bit PCM: speech amplitude typically exceeds 450
                        if any(abs(int.from_bytes(pcm_bytes[i:i+2], "little", signed=True)) > 450 for i in range(0, len(pcm_bytes), 16)):
                            last_audio_activity = asyncio.get_event_loop().time()
            except asyncio.CancelledError:
                pass
            except Exception as am_err:
                logger.debug(f"Audio monitor ended: {am_err}")

        def _subscribe_audio_track(track: rtc.Track, *args):
            sid = getattr(track, "sid", None) or id(track)
            if track.kind == rtc.TrackKind.KIND_AUDIO and sid not in monitored_tracks:
                monitored_tracks.add(sid)
                t = asyncio.create_task(_monitor_audio_track(track))
                audio_monitor_tasks.append(t)

        ctx.room.on("track_subscribed", _subscribe_audio_track)
        # Subscribe to already-present remote audio tracks (deduplicated)
        for rp in ctx.room.remote_participants.values():
            for pub in rp.track_publications.values():
                if pub.track and pub.kind == rtc.TrackKind.KIND_AUDIO:
                    _subscribe_audio_track(pub.track)

        # Start Gemini Live Realtime session concurrently while dialing
        async def _prewarm_session():
            await session.start(
                room=ctx.room,
                agent=KaamdhenuAssistant(instructions=system_prompt),
            )
            asyncio.create_task(push_unified_log("Gemini", "info", f"Gemini Live session pre-warmed & ready for {agent_name}", call_id=call_id))

        session_start_task = asyncio.create_task(_prewarm_session())

        sip_participant = None
        if direction == "outbound" and phone_number:
            trunk_id = os.getenv("OUTBOUND_TRUNK_ID")
            if trunk_id:
                try:
                    asyncio.create_task(push_unified_log("SIP", "info", f"Dialing to {phone_number} (Gemini pre-warming in parallel)...", call_id=call_id))
                    dial_task = asyncio.create_task(
                        ctx.api.sip.create_sip_participant(
                            api.CreateSIPParticipantRequest(
                                room_name=ctx.room.name,
                                sip_trunk_id=trunk_id,
                                sip_call_to=phone_number,
                                participant_identity=f"sip_{phone_number}",
                                wait_until_answered=True
                            )
                        )
                    )
                    # Await dialing answer and session pre-warming concurrently
                    sip_participant, _ = await asyncio.gather(dial_task, session_start_task)
                    asyncio.create_task(push_unified_log("SIP", "info", f"Call answered by {phone_number} (200 OK)", call_id=call_id))
                except Exception as dial_err:
                    await push_unified_log("SIP", "error", f"Dial failed: {dial_err}", call_id=call_id)
                    ctx.shutdown()
                    return
            else:
                await session_start_task
        else:
            # Inbound call: await pre-warm task directly
            await session_start_task

        # Immediately bind the caller's audio track to RoomIO upon answer (zero artificial sleep)
        target_identity = getattr(sip_participant, "identity", None) or (f"sip_{phone_number}" if phone_number else None)
        try:
            if hasattr(session, "room_io") and session.room_io:
                target_p = None
                if target_identity and target_identity in ctx.room.remote_participants:
                    target_p = ctx.room.remote_participants[target_identity]
                elif ctx.room.remote_participants:
                    # Pick first remote participant (the caller)
                    target_p = next(iter(ctx.room.remote_participants.values()))

                bound_identity = None
                if target_p:
                    bound_identity = target_p.identity
                    try:
                        session.room_io.set_participant(target_p.identity)
                    except Exception:
                        session.room_io.set_participant(target_p)
                elif target_identity:
                    bound_identity = target_identity
                    session.room_io.set_participant(target_identity)

                if bound_identity:
                    logger.info(f"RoomIO audio track immediately bound to: {bound_identity}")
                    asyncio.create_task(push_unified_log("Audio", "info", f"RoomIO bound to caller: {bound_identity}", call_id=call_id))
        except Exception as rio_err:
            logger.warning(f"RoomIO set_participant notice: {rio_err}")

        # Also dynamically bind if participant connects or reconnects
        @ctx.room.on("participant_connected")
        def _on_participant_connected(p: rtc.RemoteParticipant):
            if hasattr(session, "room_io") and session.room_io:
                try:
                    session.room_io.set_participant(p.identity)
                    logger.info(f"RoomIO dynamically bound to connected participant: {p.identity}")
                except Exception:
                    try:
                        session.room_io.set_participant(p)
                    except Exception:
                        pass

        # Instant Greeting Trigger: Direct pre-defined opening line to achieve <800ms latency
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        ist_hour = ist_now.hour
        if 5 <= ist_hour < 12:
            time_greeting = "good morning"
        elif 12 <= ist_hour < 17:
            time_greeting = "good afternoon"
        else:
            time_greeting = "good evening"

        is_callback_call = (
            log_category == "campaign_callback" or
            "callback" in call_id.lower() or
            "scheduled callback" in (lead_notes or "").lower()
        )

        valid_lead_name = lead_name and lead_name.strip() and lead_name.strip().lower() not in ("there", "caller", "unknown", "lead", "")

        try:
            if direction == "inbound":
                if inbound_prior_project:
                    greeting_instruction = f"Namaste {lead_name if valid_lead_name else ''}! {business_name} mein aapka swagat hai. Main {agent_name}. Aapne pehle {inbound_prior_project} ke baare mein baat ki thi, batayein main aaj aapki kya madad kar sakti hoon?"
                else:
                    greeting_instruction = f"Namaste! {business_name} mein aapka swagat hai. Main {agent_name} baat kar rahi hoon. Batayein main aapki kya madad kar sakti hoon?"
            elif is_callback_call:
                customer_salutation = f"Namaste {lead_name} ji" if valid_lead_name else "Namaste sir"
                greeting_instruction = f"{customer_salutation}, main {agent_name} baat kar rahi hoon {business_name} se. Aapne callback ke liye bola tha, batayein main aapki kya madad kar sakti hoon?"
            else:
                customer_salutation = f"Namaste {lead_name} ji" if valid_lead_name else "Namaste sir"
                greeting_instruction = f"{customer_salutation}, {time_greeting}! Main {agent_name} baat kar rahi hoon {business_name} se. Kya abhi aapse do minute baat ho sakti hai?"

            # Fire-and-forget: don't await playout (which blocks 3-6s for full TTS)
            speech_handle = session.generate_reply(instructions=greeting_instruction)
            greeting_delivered = True
            last_audio_activity = asyncio.get_event_loop().time()

            # Schedule non-blocking playout-done callback for logging
            async def _greeting_done():
                try:
                    await speech_handle
                except Exception:
                    pass
                asyncio.create_task(push_unified_log("Gemini", "info", f"Autonomous greeting delivered by {agent_name} to {lead_name}", call_id=call_id))
            asyncio.create_task(_greeting_done())
        except Exception as ge:
            logger.warning(f"Greeting error: {ge}")
            greeting_delivered = True
            last_audio_activity = asyncio.get_event_loop().time()

        # Dead-air & silence watchdog coroutine (relaxed to avoid false disconnects)
        async def _silence_watchdog():
            nonlocal last_audio_activity, prompted_for_silence, prompt_timestamp, user_turns
            try:
                # Wait until initial greeting has been delivered
                while not done_event.is_set() and not greeting_delivered:
                    await asyncio.sleep(0.5)

                last_audio_activity = asyncio.get_event_loop().time()

                while not done_event.is_set():
                    await asyncio.sleep(1.0)

                    # If agent or user is actively speaking, reset activity timer
                    if hasattr(session, "agent_state") and session.agent_state == "speaking":
                        last_audio_activity = asyncio.get_event_loop().time()
                        prompted_for_silence = False
                        continue
                    if hasattr(session, "user_state") and session.user_state == "speaking":
                        last_audio_activity = asyncio.get_event_loop().time()
                        prompted_for_silence = False
                        continue

                    # GUARD: Never disconnect within the first 45 seconds of the call
                    call_elapsed = time.time() - call_start_time
                    if call_elapsed < 45.0:
                        continue

                    now = asyncio.get_event_loop().time()
                    silence_duration = now - last_audio_activity

                    # Only consider silence actions after at least 2 user turns
                    if user_turns < 2:
                        continue

                    # If we already prompted for silence, check if 6s has elapsed without response
                    if prompted_for_silence:
                        if now - prompt_timestamp >= 6.0:
                            asyncio.create_task(push_unified_log("Watchdog", "info", f"Dead-air silence watchdog: zero response 6s after prompt ({silence_duration:.1f}s total silence) - disconnecting", call_id=call_id))
                            logger.info(f"Silence watchdog disconnecting call {call_id}: caller unresponsive after prompt")
                            try:
                                await ctx.room.disconnect()
                            except Exception as de:
                                logger.warning(f"Error disconnecting room in watchdog: {de}")
                            done_event.set()
                            break
                        continue

                    # 20s of continuous silence -> prompt once
                    if silence_duration >= 20.0:
                        prompted_for_silence = True
                        prompt_timestamp = asyncio.get_event_loop().time()
                        asyncio.create_task(push_unified_log("Watchdog", "info", f"Silence detected ({silence_duration:.1f}s) - prompting caller", call_id=call_id))
                        try:
                            session.generate_reply(
                                instructions='Hello? Kya aap sun pa rahe hain? Main line pe hoon.'
                            )
                        except Exception as pe:
                            logger.warning(f"Error generating silence prompt: {pe}")

            except asyncio.CancelledError:
                pass
            except Exception as we:
                logger.warning(f"Silence watchdog error: {we}")

        watchdog_task = asyncio.create_task(_silence_watchdog())

        try:
            await asyncio.wait_for(done_event.wait(), timeout=1800)
        except asyncio.TimeoutError:
            pass
        finally:
            if not watchdog_task.done():
                watchdog_task.cancel()
            # Cancel audio monitor tasks
            for amt in audio_monitor_tasks:
                if not amt.done():
                    amt.cancel()

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

        # Extract from tool_ctx if available (Strict: never assume Ready-to-Move or Bank Loan)
        t_client_name = getattr(tool_ctx, "client_name", "") or lead_name
        t_location = getattr(tool_ctx, "current_location", "")
        t_occupation = getattr(tool_ctx, "occupation", "")
        t_bhk = getattr(tool_ctx, "bhk_requirement", "")
        t_budget = getattr(tool_ctx, "budget", "")
        t_purpose = getattr(tool_ctx, "purpose", "") or "-"
        t_possession = getattr(tool_ctx, "possession_timeline", "") or "-"
        t_funding = getattr(tool_ctx, "funding_type", "") or "-"
        
        # Strict lead scoring: calls < 30s or short hang-ups must be Cold or Dropped (never default to Warm)
        if dur < 15:
            default_score = "Dropped"
        elif dur < 30:
            default_score = "Cold"
        else:
            default_score = "Cold"
        t_lead_score = getattr(tool_ctx, "lead_score", "") or default_score
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

        loc_pref = crm_data.get("location") or t_location or "-"
        job_prof = crm_data.get("job_profession") or t_occupation or "-"
        bhk_pref = crm_data.get("bhk") or t_bhk or "-"
        bud_range = crm_data.get("budget") or t_budget or "-"

        # Strict timeline: never default or invent Ready-to-Move
        raw_time = (crm_data.get("timeline") or t_possession or "-").strip()
        time_frame = raw_time if raw_time and raw_time not in ("None", "null", "") else "-"
        lower_tr = full_transcript.lower()
        if time_frame == "Ready-to-Move" and not any(w in lower_tr for w in ["ready to move", "ready-to-move", "turant shift", "immediate possession"]):
            time_frame = "-"

        # Strict funding: never default or invent Bank Loan
        raw_fund = (crm_data.get("funding_type") or t_funding or "-").strip()
        fund_type = raw_fund if raw_fund and raw_fund not in ("None", "null", "") else "-"
        if fund_type == "Bank Loan" and not any(w in lower_tr for w in ["bank loan", "home loan", "loan", "emi", "finance"]):
            fund_type = "-"

        # Strict lead scoring evaluation:
        # Calls < 30s or rejected -> strictly Cold or Dropped
        # Calls asking for brochure / site visit -> Hot
        # Calls with genuine qualification discussion -> Warm
        wa_consent = crm_data.get("whatsapp_consent", False)
        if t_site_visit or wa_consent or (t_whatsapp and "Consent" in t_whatsapp):
            final_score = "Hot"
        elif dur < 25 or t_outcome in ("rejected", "no_answer", "busy", "hung_up"):
            final_score = "Dropped" if dur < 15 else "Cold"
        else:
            cand_score = crm_data.get("lead_score")
            if cand_score in ("Hot", "Warm", "Cold", "Dropped"):
                final_score = cand_score
            else:
                final_score = "Warm" if (bhk_pref != "-" and bud_range != "-") else "Cold"

        sv_interest = crm_data.get("site_visit_interest") or (f"Yes ({t_site_visit})" if t_site_visit else "No")
        cab_req = crm_data.get("cab_required") or ("Yes" if t_pickup else "No")
        main_obj = crm_data.get("main_objection") or t_objection or "None"
        wa_status = "✅ Consent Given" if wa_consent else (t_whatsapp if t_whatsapp != "— Not Requested" else "— Not Requested")

        # Resolve recording_url (Check local file or verified tool context; do NOT synthesize phantom URLs)
        rec_url = getattr(tool_ctx, "recording_url", None)
        if not rec_url and call_id:
            from pathlib import Path
            for ext in (".mp3", ".wav", ".mp4", ".ogg"):
                rec_file = Path("recordings") / f"{call_id}{ext}"
                if rec_file.exists():
                    rec_url = f"/recordings/{call_id}{ext}"
                    break

        visit_str = f" | Visit: {t_site_visit}" if t_site_visit else ""
        cab_str = f" (Cab: {t_pickup_loc or 'Yes'})" if t_pickup else ""
        summary = f"{t_client_name} ({clean_phone}): {bhk_pref or 'TBD'} | Budget: {bud_range or 'TBD'} | {t_purpose}{visit_str}{cab_str} | Score: {final_score} | Duration: {dur}s"

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
