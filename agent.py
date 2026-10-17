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
    find_agent_profile, insert_appointment, book_appointment, normalize_phone,
    save_callback
)
from prompts import build_prompt, get_base_system_prompt, GLOBAL_NATURAL_CONVERSATION_LAYER, DYNAMIC_LANGUAGE_MIRRORING_LAYER
from tools import RealEstateTools
from crm import lookup_inbound_callback_context, build_inbound_callback_context_block

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

    model_name = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview").strip()
    if not model_name:
        model_name = "gemini-3.1-flash-live-preview"

    google_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None
    gemini_voice = voice or os.getenv("GEMINI_TTS_VOICE", "Aoede")
    voice_engine = os.getenv("VOICE_ENGINE", "realtime").lower()
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false" and voice_engine == "realtime"

    # Turn handling options for ultra-low latency (<600-800ms) turn taking
    turn_handling_config = {
        "endpointing": {
            "min_delay": 0.4,
            "max_delay": 1.5,
        }
    }

    # ULTRA-FAST PURE GEMINI REALTIME — clean plugin defaults with tuned 400ms endpointing
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
                tools=unique_tools,
                turn_handling=turn_handling_config,
            )
        except Exception as e:
            logger.warning(f"Error initializing Google Realtime: {e}")

    # PIPELINE FALLBACK — tuned Silero VAD for stable turn detection
    stt = _deepgram_stt(model=os.getenv("STT_MODEL", "nova-3"), language="multi") if _deepgram_stt and os.getenv("DEEPGRAM_API_KEY") else None
    tts = _google_tts(voice_name=gemini_voice) if _google_tts else None
    vad_inst = None
    if silero:
        try:
            vad_inst = silero.VAD.load(
                min_speech_duration=0.15,
                min_silence_duration=0.4,
                prefix_padding_duration=0.1
            )
        except Exception:
            try:
                vad_inst = silero.VAD.load(
                    min_silence_duration=0.4,
                    prefix_padding_duration=0.1
                )
            except Exception:
                pass

    return AgentSession(
        stt=stt,
        llm=_google_llm(model="gemini-2.0-flash") if _google_llm else None,
        tts=tts,
        vad=vad_inst,
        turn_handling=turn_handling_config,
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
    agent_id = None
    agent_name = os.getenv("AGENT_NAME", "Assistant")
    agent_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    active_agent_profile = None
    campaign_id = None
    broker_phone = None
    broker_email = None
    calcom_api_key = None
    calcom_event_type_id = None
    custom_prompt = None
    tool_ctx = None
    session = None
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
                agent_id = m.get("agent_id") or m.get("agent_profile_id")
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
        # PHASE 3: ASYNC DB LOOKUPS (Dynamic Persona & Callback Memory)
        # ===================================================================
        inbound_prior_project = ""  # Tracks prior project context for returning callers
        is_inbound_callback = False
        sip_participant = None

        if direction == "inbound":
            # 1. Extract caller's normalized phone number from sip_participant.identity if present
            for p in ctx.room.remote_participants.values():
                if p.identity:
                    sip_participant = p
                    break

            caller_identity = getattr(sip_participant, "identity", "")
            if caller_identity:
                extracted_digits = normalize_phone(caller_identity)
                if extracted_digits:
                    phone_number = f"+91{extracted_digits}" if not caller_identity.startswith("+") and len(extracted_digits) == 10 else f"+{re.sub(r'[^0-9]', '', caller_identity)}"

            norm_caller_phone = normalize_phone(phone_number)

            # 2. Async lookup for last interaction in Supabase with 1.0s timeout guard
            cb_ctx = None
            if norm_caller_phone:
                try:
                    cb_ctx = await asyncio.wait_for(
                        lookup_inbound_callback_context(norm_caller_phone),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"Inbound callback CRM lookup timed out (1.0s) for {norm_caller_phone} - using defaults")
                    cb_ctx = {"found": False}
                except Exception as cb_err:
                    logger.warning(f"Inbound callback CRM lookup failed for {norm_caller_phone}: {cb_err} - using defaults")
                    cb_ctx = {"found": False}

            # 3. Dynamic Persona & System Prompt Injection if prior interaction exists
            if cb_ctx and cb_ctx.get("found"):
                is_inbound_callback = True
                log_category = "campaign_callback"

                prior_lead = cb_ctx.get("lead_name") or ""
                if prior_lead and prior_lead.strip().lower() not in ("there", "caller", "unknown", "lead", ""):
                    lead_name = prior_lead

                if cb_ctx.get("agent_id"):
                    agent_id = cb_ctx.get("agent_id")
                if cb_ctx.get("agent_name"):
                    agent_name = cb_ctx.get("agent_name")
                if cb_ctx.get("agent_voice"):
                    agent_voice = cb_ctx.get("agent_voice")
                if cb_ctx.get("business_name"):
                    business_name = cb_ctx.get("business_name")
                if cb_ctx.get("service_type"):
                    service_type = cb_ctx.get("service_type")
                if cb_ctx.get("campaign_id"):
                    campaign_id = cb_ctx.get("campaign_id")
                if cb_ctx.get("broker_phone"):
                    broker_phone = cb_ctx.get("broker_phone")
                if cb_ctx.get("broker_email"):
                    broker_email = cb_ctx.get("broker_email")
                if cb_ctx.get("calcom_event_type_id"):
                    calcom_event_type_id = cb_ctx.get("calcom_event_type_id")

                if agent_id:
                    try:
                        active_agent_profile = await get_agent_profile(agent_id)
                    except Exception:
                        pass

                prior_proj = cb_ctx.get("project_name") or cb_ctx.get("service_type") or business_name
                if prior_proj and prior_lead and prior_proj.strip().lower() == prior_lead.strip().lower():
                    prior_proj = business_name
                inbound_prior_project = prior_proj
                project_name = prior_proj or business_name

                # Overwrite default prompt with specific agent's configured prompt from agents table
                agent_sys_prompt = cb_ctx.get("system_prompt") or ""
                if agent_sys_prompt and agent_sys_prompt.strip():
                    custom_prompt = agent_sys_prompt.strip()

                # Append explicit Callback Context block
                cb_block = build_inbound_callback_context_block(
                    campaign_name=cb_ctx.get("campaign_name") or "",
                    agent_name=agent_name,
                    last_call_summary=cb_ctx.get("last_call_summary") or "",
                    lead_name=lead_name,
                    business_name=business_name
                )
                if custom_prompt:
                    custom_prompt = custom_prompt + "\n\n" + cb_block
                else:
                    custom_prompt = cb_block

                asyncio.create_task(push_unified_log(
                    "CRM", "info",
                    f"Returning callback caller identified: {lead_name} ({phone_number}) -> Persona: {agent_name} ({business_name})",
                    call_id=call_id
                ))
            else:
                # Fresh unknown caller -> Check dedicated campaign by inbound DID or fallback to default agent
                camp = None
                try:
                    camp = await asyncio.wait_for(find_campaign_by_inbound_number(phone_number), timeout=1.0)
                except Exception:
                    camp = None

                if camp:
                    campaign_id = camp.get("id")
                    log_category = "dedicated_inbound"
                    ag_id = camp.get("agent_profile_id")
                    if ag_id:
                        ag_prof = await get_agent_profile(ag_id)
                        if ag_prof:
                            active_agent_profile = ag_prof
                            agent_id = ag_id
                            agent_name = ag_prof.get("agent_name") or agent_name
                            agent_voice = ag_prof.get("voice") or agent_voice
                            business_name = ag_prof.get("business_name") or business_name
                            service_type = ag_prof.get("service_type") or service_type
                            broker_phone = ag_prof.get("broker_phone") or broker_phone
                            broker_email = ag_prof.get("broker_email") or broker_email
                            custom_prompt = ag_prof.get("system_prompt") or custom_prompt
                            calcom_event_type_id = ag_prof.get("calcom_event_type_id") or camp.get("calcom_event_type_id") or calcom_event_type_id
                    asyncio.create_task(push_unified_log("CRM", "info", f"Dedicated inbound matched campaign: {camp.get('name')}", call_id=call_id))
                else:
                    asyncio.create_task(push_unified_log("CRM", "info", f"Fresh inbound caller — falling back to standard default agent ({agent_name})", call_id=call_id))
        else:
            # Outbound call: resolve profile if agent_id passed
            if agent_id and not active_agent_profile:
                try:
                    active_agent_profile = await get_agent_profile(agent_id)
                    if active_agent_profile:
                        if not custom_prompt and active_agent_profile.get("system_prompt"):
                            custom_prompt = active_agent_profile.get("system_prompt")
                        if not agent_name or agent_name == "Assistant":
                            agent_name = active_agent_profile.get("name") or active_agent_profile.get("agent_name") or agent_name
                        if not agent_voice:
                            agent_voice = active_agent_profile.get("voice") or agent_voice
                        if not business_name:
                            business_name = active_agent_profile.get("business_name") or ""
                        if not service_type:
                            service_type = active_agent_profile.get("service_type") or ""
                except Exception as out_err:
                    logger.debug(f"Outbound agent profile lookup: {out_err}")

        # ===================================================================
        # PHASE 4: BUILD SYSTEM PROMPT (Dynamic Persona — Zero Hardcoded Pollution)
        # ===================================================================
        valid_lead_name = lead_name and lead_name.strip() and lead_name.strip().lower() not in ("there", "caller", "unknown", "lead", "")

        has_custom_prompt = False
        if is_inbound_callback and custom_prompt and custom_prompt.strip():
            # ── RETURNING CALLBACK CALLER ──
            # Use the exact agent system_prompt fetched from agent_profiles + callback context block.
            system_prompt = custom_prompt.strip()
            has_custom_prompt = True
            asyncio.create_task(push_unified_log("Agent", "info",
                f"Inbound callback persona ISOLATED — using agent's own prompt only ({agent_name})",
                call_id=call_id))
        elif custom_prompt and custom_prompt.strip():
            # ── OUTBOUND OR CAMPAIGN WITH EXPLICIT PROMPT ──
            system_prompt = custom_prompt.strip()
            has_custom_prompt = True
        else:
            # ── FRESH CALLER — NO PRIOR CONTEXT, NO CAMPAIGN ──
            # Resolve default agent from Supabase instead of hardcoded real-estate prompts.
            try:
                default_profile = await asyncio.wait_for(find_agent_profile(), timeout=1.5)
                if default_profile and default_profile.get("system_prompt", "").strip():
                    system_prompt = default_profile["system_prompt"].strip()
                    has_custom_prompt = True
                    # Also adopt the default agent's identity if not already set from metadata
                    if default_profile.get("agent_name"):
                        agent_name = default_profile["agent_name"]
                    if default_profile.get("voice"):
                        agent_voice = default_profile["voice"]
                    if default_profile.get("business_name"):
                        business_name = default_profile["business_name"]
                    if default_profile.get("service_type"):
                        service_type = default_profile["service_type"]
                    if default_profile.get("broker_phone"):
                        broker_phone = default_profile["broker_phone"]
                    if default_profile.get("broker_email"):
                        broker_email = default_profile["broker_email"]
                    asyncio.create_task(push_unified_log("Agent", "info",
                        f"Fresh caller — resolved default agent from Supabase: {agent_name} ({business_name})",
                        call_id=call_id))
                else:
                    # Supabase returned an empty prompt — use generic industry-agnostic fallback
                    system_prompt = get_base_system_prompt(
                        agent_name=agent_name,
                        business_name=business_name,
                        custom_prompt=None,
                        lead_name=lead_name,
                        service_type=service_type
                    )
                    asyncio.create_task(push_unified_log("Agent", "info",
                        f"Fresh caller — Supabase default agent has no prompt, using generic fallback",
                        call_id=call_id))
            except Exception as default_err:
                logger.warning(f"Default agent profile lookup failed: {default_err} — using generic fallback")
                system_prompt = get_base_system_prompt(
                    agent_name=agent_name,
                    business_name=business_name,
                    custom_prompt=None,
                    lead_name=lead_name,
                    service_type=service_type
                )

        # Append ONLY essential dynamic call context if outbound (zero corporate/telephony wrappers bloat)
        if has_custom_prompt:
            if direction == "outbound" and valid_lead_name:
                notes_str = lead_notes.strip() if lead_notes else ""
                context_parts = []
                if lead_bhk and lead_bhk.strip():
                    context_parts.append(f"BHK: {lead_bhk.strip()}")
                if lead_budget and lead_budget.strip():
                    context_parts.append(f"Budget: {lead_budget.strip()}")
                if notes_str and notes_str != "None":
                    context_parts.append(f"Notes: {notes_str}")
                ctx_summary = " | ".join(context_parts)
                lead_tag = f"\n\n[CALL DETAILS] Prospect: {lead_name} ({phone_number})"
                if ctx_summary:
                    lead_tag += f" | {ctx_summary}"
                system_prompt = system_prompt + lead_tag
                await push_unified_log("Agent", "info", f"Dynamic lead details attached for {lead_name}", call_id=call_id)

        # === PERSONA LOGGING ===
        asyncio.create_task(push_unified_log("Agent", "info",
            f"Loaded Persona: {agent_name} ({business_name}) | Direction: {direction} | Callback: {is_inbound_callback} | Prompt Chars: {len(system_prompt)}",
            call_id=call_id))

        session_cleaned = False
        async def _teardown_session():
            nonlocal session_cleaned
            if session_cleaned or not session:
                return
            session_cleaned = True
            try:
                await asyncio.wait_for(session.aclose(), timeout=2.0)
                logger.info(f"Agent session closed cleanly for {call_id}")
            except Exception as ce:
                logger.debug(f"Session aclose error: {ce}")
            if hasattr(session, "llm") and hasattr(session.llm, "aclose"):
                try:
                    await asyncio.wait_for(session.llm.aclose(), timeout=1.0)
                except Exception:
                    pass

        done_event = asyncio.Event()
        def _on_part_disconnected(p: rtc.RemoteParticipant):
            if ctx.room.local_participant and p.identity == ctx.room.local_participant.identity:
                return
            done_event.set()
            asyncio.create_task(_teardown_session())

        ctx.room.on("participant_disconnected", _on_part_disconnected)
        ctx.room.on("disconnected", lambda: (done_event.set(), asyncio.create_task(_teardown_session())))

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

        # Filter tools by active agent's configuration
        all_available_tools = tool_ctx.get_all_tools()
        enabled_tools_cfg = None
        if active_agent_profile and "enabled_tools" in active_agent_profile:
            raw_tools = active_agent_profile.get("enabled_tools")
            if isinstance(raw_tools, str):
                try:
                    enabled_tools_cfg = json.loads(raw_tools)
                except Exception:
                    enabled_tools_cfg = None
            elif isinstance(raw_tools, list):
                enabled_tools_cfg = raw_tools

        # Always retain core universal telephony tools
        core_tool_names = {"schedule_callback", "end_call", "transfer_to_human", "remember_details", "record_client_qualification"}
        if enabled_tools_cfg is not None:
            allowed_names = set(enabled_tools_cfg) | core_tool_names
            filtered_tools = [
                t for t in all_available_tools
                if (getattr(t, "name", None) or getattr(t, "__name__", None) or str(t)) in allowed_names
            ]
        else:
            filtered_tools = all_available_tools

        # Deduplicate tools by name
        seen_names = set()
        unique_tools = []
        for t in filtered_tools:
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

            recovering_1011 = False
            @session.on("error")
            def _on_session_error(error):
                nonlocal recovering_1011
                err_msg = str(error)
                is_1011 = "1011" in err_msg or "Internal error encountered" in err_msg
                if is_1011:
                    logger.warning(f"Gemini Realtime 1011 internal error detected for {call_id}: {err_msg}")
                    asyncio.create_task(
                        push_unified_log("Gemini", "warning", f"1011 internal error encountered: {err_msg[:120]}", call_id=call_id)
                    )
                    if not recovering_1011:
                        recovering_1011 = True
                        async def _handle_1011_recovery():
                            try:
                                recovery_task = None
                                if hasattr(session, "generate_reply") and not done_event.is_set():
                                    try:
                                        recovery_task = session.generate_reply(
                                            instructions="Continue the conversation naturally in the mandated language."
                                        )
                                    except Exception:
                                        recovery_task = None
                                if recovery_task:
                                    await asyncio.wait_for(recovery_task, timeout=2.0)
                                    logger.info(f"1011 recovery succeeded for {call_id}")
                                    return
                            except Exception as rec_err:
                                logger.warning(f"1011 recovery failed or timed out: {rec_err}")
                            finally:
                                if not done_event.is_set() and recovering_1011:
                                    asyncio.create_task(
                                        push_unified_log("Gemini", "warning", f"1011 recovery timed out (>2s) - terminating call gracefully", call_id=call_id)
                                    )
                                    try:
                                        await ctx.room.disconnect()
                                    except Exception:
                                        pass
                                    done_event.set()
                        asyncio.create_task(_handle_1011_recovery())
                    return

                logger.error(f"Gemini Session error for {call_id}: {err_msg}")
                asyncio.create_task(
                    push_unified_log("Gemini", "error", f"Session error: {err_msg}", call_id=call_id)
                )
        except Exception:
            pass

        # Start Gemini Live Realtime session concurrently while dialing
        async def _prewarm_session():
            await session.start(
                room=ctx.room,
                agent=KaamdhenuAssistant(instructions=system_prompt),
            )
            asyncio.create_task(push_unified_log("Gemini", "info", f"Gemini Live session pre-warmed & ready for {agent_name}", call_id=call_id))

        session_start_task = asyncio.create_task(_prewarm_session())

        if direction == "outbound" and phone_number:
            sip_participant = None
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

        # Instant Greeting Trigger: Direct model to execute ONLY the user-mandated opening line & language from prompt
        try:
            greeting_instruction = (
                "Initiate the call by executing ONLY the opening instructions and first spoken line "
                "specified in your system prompt. Strictly speak in the exact language mandated by your instructions. "
                "Do NOT invent your own greeting, do NOT mention any company or service unless instructed."
            )

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
                                instructions="Gently ask if the caller can hear you, using the language of the current conversation."
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

    except Exception as general_err:
        err_str = str(general_err)
        if "1011" in err_str or "Internal error encountered" in err_str:
            logger.warning(f"Handled 1011 runtime error for {call_id}: {err_str}")
            await push_unified_log("Agent", "warning", f"1011 runtime error caught: {err_str[:120]}", call_id=call_id)
        else:
            await push_unified_log("Agent", "error", f"Call runtime error: {general_err}", call_id=call_id)
            logger.error(f"General error in entrypoint: {general_err}", exc_info=True)

    finally:
        # Explicit teardown of session and WebSocket resources to prevent zombie connections
        try:
            await _teardown_session()
        except Exception:
            pass
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
                agent_id=agent_id or "",
                agent_name=agent_name or "",
                notes=lead_notes or "",
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
