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

from db import push_unified_log, log_call, find_recent_outbound_context, add_campaign_minutes, find_campaign_by_inbound_number
from prompts import build_prompt
from tools import RealEstateTools

load_dotenv(".env", override=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kaamdhenu-agent")

def _build_session(tools: list, system_prompt: str) -> AgentSession:
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-exp")
    gemini_voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
    voice_engine = os.getenv("VOICE_ENGINE", "realtime").lower()
    use_realtime = os.getenv("USE_GEMINI_REALTIME", "true").lower() != "false" and voice_engine == "realtime"

    # ULTRA-FAST PURE GEMINI REALTIME
    if use_realtime and _google_realtime is not None:
        try:
            from google.genai import types as _gt
            input_cfg = _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=600,
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
    campaign_id = None
    broker_phone = None
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
                campaign_id = m.get("campaign_id")
                broker_phone = m.get("broker_phone")
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
                    await push_unified_log("CRM", "info", f"Dedicated inbound matched campaign: {camp.get('name')}", call_id=call_id)
                else:
                    # Check recent outbound context
                    ctx_info = await find_recent_outbound_context(phone_number)
                    if ctx_info.get("found"):
                        campaign_id = ctx_info.get("campaign_id") or campaign_id
                        lead_name = ctx_info.get("lead_name", lead_name)
                        log_category = "campaign_callback"
                        await push_unified_log("CRM", "info", f"Callback detected from prior campaign lead: {lead_name}", call_id=call_id)

        # Build prompt safely
        system_prompt = build_prompt(
            lead_name=lead_name,
            business_name=business_name,
            service_type=service_type,
            agent_name=agent_name,
            custom_prompt=custom_prompt
        )

        tool_ctx = RealEstateTools(
            ctx,
            phone_number=phone_number,
            lead_name=lead_name,
            direction=direction,
            call_id=call_id,
            campaign_id=campaign_id,
            broker_phone=broker_phone
        )

        # Connect immediately
        await ctx.connect()
        await push_unified_log("SIP", "info", f"Room connected ({direction}): {phone_number}", call_id=call_id)

        session = _build_session(tools=tool_ctx.get_all_tools(), system_prompt=system_prompt)

        session_start_task = asyncio.create_task(session.start(
            room=ctx.room,
            agent=KaamdhenuAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())
        ))

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

        summary = f"{t_client_name} ({clean_phone}): {t_bhk or 'TBD'} | Budget: {t_budget or 'TBD'} | {t_purpose} | Score: {t_lead_score} | Duration: {dur}s"

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
                log_category=log_category
            )
            await push_unified_log("CRM", "info", f"Call log saved ({direction}): {clean_phone} - {dur}s, ₹{cost_inr}", call_id=call_id)
        except Exception as log_err:
            logger.error(f"Failed to write call log: {log_err}")

        await push_unified_log("LiveKit", "info", f"Call session completed for {clean_phone}", call_id=call_id)

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="kaamdhenu-voice-agent"))
