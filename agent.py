import asyncio
import json
import logging
import os
import re
import ssl
import time
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
    push_unified_log, log_call, find_recent_outbound_context,
    add_campaign_minutes, find_campaign_by_inbound_number, get_agent_profile,
    insert_appointment, book_appointment
)
from prompts import build_prompt, get_base_system_prompt, GLOBAL_NATURAL_CONVERSATION_LAYER
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
        if "parson" in text or "day after tomorrow" in text:
            visit_date = (now + _td(days=2)).strftime("%Y-%m-%d")
        elif "kal" in text or "tomorrow" in text:
            visit_date = (now + _td(days=1)).strftime("%Y-%m-%d")
        elif "aaj" in text or "today" in text:
            visit_date = now.strftime("%Y-%m-%d")
        elif "weekend" in text:
            days = (5 - now.weekday()) % 7
            if days == 0: days = 7
            visit_date = (now + _td(days=days)).strftime("%Y-%m-%d")
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

    try:
        # Outbound Metadata parsing
        if ctx.job.metadata:
            try:
                m = json.loads(ctx.job.metadata)
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
            except Exception as e:
                logger.warning(f"Metadata parse warning: {e}")

        # Inbound detection
        if "inbound" in call_id.lower() or not phone_number:
            direction = "inbound"
            lead_name = "Caller"
            match = re.search(r'(\d{10,12})', call_id)
            phone_number = f"+{match.group(1)}" if match else "+918065353767"
            
            # Smart context routing for inbound
            log_category = "direct_inbound"
            if direction == "inbound":
                # Check dedicated campaign number
                camp = await find_campaign_by_inbound_number(phone_number)
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
                    # Check recent outbound context (last 7 days)
                    ctx_info = await find_recent_outbound_context(phone_number)
                    if ctx_info.get("found"):
                        campaign_id = ctx_info.get("campaign_id") or campaign_id
                        lead_name = ctx_info.get("lead_name", lead_name)
                        proj_name = ctx_info.get("project_name") or business_name
                        log_category = "campaign_callback"
                        custom_prompt = (
                            f"You are {agent_name}, Senior Property Consultant for Kaamdhenu Real Estate.\n"
                            f"Important context: The client {lead_name} was recently called regarding {proj_name}.\n"
                            f"Greeting: Speak this opening line: 'Namaste {lead_name}! Main {agent_name} Kaamdhenu Real Estate se baat kar rahi hoon. Aapko humare {proj_name} ke regarding call gaya tha... Batayein main aapki kya madad kar sakti hoon?'\n"
                            f"Speak naturally in polite Hindi/Hinglish."
                        )
                        await push_unified_log("CRM", "info", f"Callback detected from prior campaign lead: {lead_name}", call_id=call_id)

        # Build prompt safely with global natural human conversation layer
        system_prompt = get_base_system_prompt(
            agent_name=agent_name,
            business_name=business_name,
            custom_prompt=custom_prompt,
            lead_name=lead_name,
            service_type=service_type
        )

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

        # Connect immediately
        await ctx.connect()
        await push_unified_log("SIP", "info", f"Room connected ({direction}): {phone_number}", call_id=call_id)

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

        # Opening greeting
        greeting_text = (
            f"Namaste! Thank you for calling {business_name}. I am {agent_name}. How can I help you today?"
            if direction == "inbound" else
            f"Hi {lead_name}! I am {agent_name} from {business_name} calling regarding your inquiry."
        )
        try:
            await session.generate_reply(instructions=f"Speak opening line: {greeting_text}")
            await push_unified_log("Gemini", "info", f"Autonomous greeting delivered by {agent_name}", call_id=call_id)
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
                any(phrase in transcript_lower for phrase in ["visit arrange", "visit confirm", "visit karenge", "visit ke liye", "dekhne aunga", "dekhne aungi", "visit plan", "gaadi bhej", "cab bhej"])
            ) and (
                any(pos in transcript_lower for pos in ["haan", "yes", "sure", "bilkul", "theek hai", "thik hai", "done", "confirm", "chalega", "aunga", "aungi", "bhej do", "bhej dena", "thik"])
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

        visit_str = f" | Visit: {t_site_visit}" if t_site_visit else ""
        cab_str = f" (Cab: {t_pickup_loc or 'Yes'})" if t_pickup else ""
        summary = f"{t_client_name} ({clean_phone}): {t_bhk or 'TBD'} | Budget: {t_budget or 'TBD'} | {t_purpose}{visit_str}{cab_str} | Score: {t_lead_score} | Duration: {dur}s"

        try:
            await log_call(
                call_id=call_id,
                phone_number=clean_phone,
                called_to=os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
                lead_name=t_client_name,
                direction=direction,
                campaign_id=campaign_id,
                outcome=t_outcome,
                lead_score=t_lead_score,
                summary=summary,
                reason="",
                duration_seconds=dur,
                cost_inr=cost_inr,
                client_name=t_client_name,
                current_location=t_location,
                occupation=t_occupation,
                bhk_requirement=t_bhk,
                budget=t_budget,
                purpose=t_purpose,
                possession_timeline=t_possession,
                funding_type=t_funding,
                commitment_risk=t_commitment,
                site_visit_date=t_site_visit,
                pickup_required=t_pickup,
                pickup_location=t_pickup_loc,
                next_callback=t_callback,
                objection=t_objection,
                whatsapp_status=t_whatsapp,
                log_category=log_category,
                callback_dispatched=False
            )
            await push_unified_log("CRM", "info", f"Call log saved ({direction}): {clean_phone} - {dur}s, ₹{cost_inr}", call_id=call_id)
        except Exception as log_err:
            logger.error(f"Failed to write call log: {log_err}")

        await push_unified_log("LiveKit", "info", f"Call session completed for {clean_phone}", call_id=call_id)

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="kaamdhenu-voice-agent"))
