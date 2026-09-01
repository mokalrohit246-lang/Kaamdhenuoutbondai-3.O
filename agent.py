import asyncio
import json
import logging
import os
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

from db import push_unified_log, get_client_number_config
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

    # ULTRA-FAST 500ms PURE GEMINI REALTIME
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

async def entrypoint(ctx: agents.JobContext):
    call_id = ctx.room.name
    await push_unified_log("LiveKit", "info", f"Job connected: {call_id}", call_id=call_id)

    direction = "outbound"
    phone_number = ""
    lead_name = "there"
    business_name = "Kaamdhenu Real Estate"
    service_type = "Luxury Properties"
    agent_name = "Priya"
    broker_phone = None
    custom_prompt = None

    campaign_id = None
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
        except Exception:
            pass

    if direction == "inbound" or not phone_number:
        direction = "inbound"
        for p in ctx.room.remote_participants.values():
            phone_number = p.identity.replace("sip_", "").strip()
            break

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

    await ctx.connect()

    session = _build_session(tools=tool_ctx.get_all_tools(), system_prompt=system_prompt)

    # Start audio session concurrently
    session_start_task = asyncio.create_task(session.start(
        room=ctx.room,
        agent=KaamdhenuAssistant(instructions=system_prompt),
        room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())
    ))

    # Pre-warmed outbound dial
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

    # Instant greeting
    greeting_text = (
        f"Namaste! Thank you for calling {business_name}. I am {agent_name}. How can I assist you with your property inquiry today?"
        if direction == "inbound" else
        f"Hi {lead_name}! I am {agent_name} from {business_name} calling regarding your property inquiry."
    )
    try:
        await session.generate_reply(instructions=f"Speak immediately: {greeting_text}")
    except Exception:
        pass

    call_start_ts = time.time()

    # Disconnect Lifecycle Guard
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

    # GUARANTEED CALL LOGGING ON HANGUP
    dur = max(1, int(time.time() - call_start_ts))
    cost_inr = round((dur / 60.0) * 1.22, 2)
    
    # Check if end_call was already triggered by tool_ctx
    if not getattr(tool_ctx, "_log_saved", False):
        outcome = getattr(tool_ctx, "outcome", "completed")
        lead_score = "Hot" if outcome == "booked" else ("Warm" if dur > 20 else "Cold")
        summary = f"Call duration: {dur}s with {lead_name}. Outcome: {outcome}."
        
        try:
            from db import log_call
            await log_call(
                call_id=call_id,
                phone_number=phone_number,
                called_to=os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
                lead_name=lead_name,
                direction=direction,
                campaign_id=campaign_id,
                outcome=outcome,
                lead_score=lead_score,
                summary=summary,
                reason="",
                duration_seconds=dur,
                cost_inr=cost_inr,
                recording_url=getattr(tool_ctx, "recording_url", None)
            )
            await push_unified_log("CRM", "info", f"Call logged ({direction}): {phone_number} - {dur}s, ₹{cost_inr}", call_id=call_id)
        except Exception as e:
            logger.error("Failed to save call log: %s", e)

    await push_unified_log("LiveKit", "info", f"Call session finalized: {phone_number}", call_id=call_id)
    await session.aclose()

if __name__ == "__main__":
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="kaamdhenu-voice-agent"))
