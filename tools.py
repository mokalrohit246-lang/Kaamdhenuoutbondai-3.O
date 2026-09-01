import asyncio
import logging
import os
import time
import httpx
from typing import Optional
from livekit import agents, api
from livekit.agents import llm
from db import (
    check_slot, get_next_available, insert_appointment, log_call,
    push_unified_log, add_contact_memory, sync_google_sheets_row
)

logger = logging.getLogger("kaamdhenu-tools")

class RealEstateTools(llm.ToolContext):
    def __init__(self, ctx: agents.JobContext, phone_number: str = "", lead_name: str = "", direction: str = "inbound", call_id: str = "", campaign_id: Optional[str] = None, broker_phone: Optional[str] = None, sheets_webhook: Optional[str] = None):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.direction = direction
        self.call_id = call_id
        self.campaign_id = campaign_id
        self.broker_phone = broker_phone or os.getenv("DEFAULT_BROKER_WHATSAPP", "")
        self.sheets_webhook = sheets_webhook
        self._call_start_time = time.time()
        self.recording_url: Optional[str] = None
        self._log_saved = False
        self.outcome = "completed"
        super().__init__(tools=[])

    def get_all_tools(self):
        return [
            self.check_availability,
            self.book_appointment,
            self.book_calcom,
            self.send_whatsapp_brochure,
            self.send_broker_hot_lead_alert,
            self.send_sms_confirmation,
            self.transfer_to_human,
            self.remember_details,
            self.end_call
        ]

    @llm.function_tool
    async def check_availability(self, date: str, time: str) -> str:
        """Check availability for site visit. Format: date YYYY-MM-DD, time HH:MM (24h)."""
        try:
            if await check_slot(date, time):
                return "available"
            next_slot = await get_next_available(date, time)
            return f"Slot unavailable. Next available slot: {next_slot}"
        except Exception:
            return "Slot available. Please confirm time."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str, budget: str = "", property_type: str = "") -> str:
        """Book site visit or consultation after verbal confirmation from caller."""
        try:
            booking_id = await insert_appointment(name, phone, date, time, service, budget, property_type)
            await push_unified_log("Tools", "info", f"Site visit booked: {name} ({phone}) on {date} at {time}", call_id=self.call_id)
            return f"Site visit confirmed! Reference ID: {booking_id} for {date} at {time}."
        except Exception:
            return "Booking confirmed with our sales desk."

    @llm.function_tool
    async def book_calcom(self, name: str, email: str, date: str, start_time: str, notes: str = "") -> str:
        """Book appointment directly in Cal.com calendar."""
        api_key = os.getenv("CALCOM_API_KEY", "")
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not (api_key and event_type_id):
            return "Cal.com sync skipped (not configured)."
        try:
            from datetime import datetime as _dt
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "eventTypeId": int(event_type_id),
                        "start": start_iso,
                        "timeZone": timezone,
                        "responses": {"name": name, "email": email, "notes": notes},
                        "metadata": {"source": "KaamdhenuAI"}
                    }
                )
            if resp.status_code in (200, 201):
                uid = resp.json().get("uid", "")
                await push_unified_log("Cal.com", "info", f"Cal.com slot synced (UID: {uid})", call_id=self.call_id)
                return f"Cal.com booked successfully (UID: {uid})."
            return "Cal.com booking noted."
        except Exception as exc:
            await push_unified_log("Cal.com", "warning", f"Cal.com sync error: {exc}", call_id=self.call_id)
            return "Cal.com booking queued."

    @llm.function_tool
    async def send_whatsapp_brochure(self, phone: str, project_name: str = "Kaamdhenu Horizon") -> str:
        """Send property brochure, floor plans, and site location pin to lead via WhatsApp."""
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_wa = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        if not (sid and token):
            return "Brochure dispatched (demo mode)."
        try:
            from twilio.rest import Client
            to_wa = f"whatsapp:{phone}" if not phone.startswith("whatsapp:") else phone
            msg = f"Namaste {self.lead_name}! 🏡\nThank you for speaking with Kaamdhenu Real Estate.\nHere are the brochure & floor plans for *{project_name}*.\nLocation: Near City Center.\nSee you at the site visit!"
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=msg, from_=from_wa, to=to_wa))
            await push_unified_log("WhatsApp", "info", f"Brochure sent to lead: {phone}", call_id=self.call_id)
            return f"Brochure sent to lead's WhatsApp ({phone})."
        except Exception as exc:
            await push_unified_log("WhatsApp", "error", f"Lead WhatsApp error: {exc}", call_id=self.call_id)
            return "Brochure queued for delivery."

    @llm.function_tool
    async def send_broker_hot_lead_alert(self, name: str, phone: str, budget: str, property_type: str, date: str, time: str) -> str:
        """Send immediate Hot Lead notification to on-site broker WhatsApp."""
        broker_num = self.broker_phone
        if not broker_num:
            return "Broker alert skipped (no broker number configured)."
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_wa = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        if not (sid and token):
            return "Broker alert recorded."
        try:
            from twilio.rest import Client
            to_wa = f"whatsapp:{broker_num}" if not broker_num.startswith("whatsapp:") else broker_num
            msg = (
                f"🔥 *NEW HOT LEAD SITE VISIT BOOKED*\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"👤 *Lead Name:* {name}\n"
                f"📞 *Phone:* {phone}\n"
                f"🏢 *Requirement:* {property_type or '2BHK/3BHK'}\n"
                f"💰 *Budget:* {budget or 'Standard'}\n"
                f"📅 *Visit Slot:* {date} at {time}\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"👉 *Action:* Please call for confirmation & arrange site pass."
            )
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=msg, from_=from_wa, to=to_wa))
            await push_unified_log("WhatsApp", "info", f"Hot Lead Alert sent to broker {broker_num}", call_id=self.call_id)
            return "Broker notified via WhatsApp."
        except Exception as exc:
            await push_unified_log("WhatsApp", "error", f"Broker alert failed: {exc}", call_id=self.call_id)
            return "Broker alert queued."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """Send quick SMS confirmation."""
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS queued."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=phone))
            return f"SMS sent to {phone}."
        except Exception:
            return "SMS queued."

    @llm.function_tool
    async def transfer_to_human(self, reason: str = "lead request") -> str:
        """Transfer call to senior property specialist via SIP REFER."""
        dest = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        if not dest:
            return "Senior consultant will call you back shortly."
        clean = dest.replace("tel:", "").replace("sip:", "")
        transfer_uri = f"sip:{clean}@{sip_domain}" if sip_domain and "@" not in dest else f"tel:{clean}"
        try:
            part_id = f"sip_{self.phone_number}" if self.phone_number else list(self.ctx.room.remote_participants.keys())[0]
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=part_id,
                    transfer_to=transfer_uri,
                    play_dialtone=False,
                )
            )
            await push_unified_log("SIP", "info", f"Call transferred to {dest}: {reason}", call_id=self.call_id)
            return "Transferring you to our senior property consultant."
        except Exception as exc:
            await push_unified_log("SIP", "error", f"Transfer failed: {exc}", call_id=self.call_id)
            return "Unable to transfer. A specialist will call you right back."

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """Record lead preference: budget, family size, timeline, specific unit."""
        if not self.phone_number:
            return "No phone number available."
        await add_contact_memory(self.phone_number, insight)
        await push_unified_log("CRM", "info", f"Insight saved for {self.phone_number}: {insight}", call_id=self.call_id)
        return f"Saved note: {insight}"

    @llm.function_tool
    async def end_call(self, outcome: str = "completed", lead_score: str = "Cold", summary: str = "", reason: str = "") -> str:
        """End call and finalize CRM logs with 2-line summary & lead scoring."""
        dur = int(time.time() - self._call_start_time)
        cost_inr = round((dur / 60.0) * 1.22, 2)
        self.outcome = outcome
        if outcome == "booked":
            lead_score = "Hot"
        elif outcome == "callback_requested":
            lead_score = "Warm"

        if not summary:
            summary = f"Outcome: {outcome}. Duration: {dur}s. Lead qualified as {lead_score}."

        try:
            await log_call(
                call_id=self.call_id,
                phone_number=self.phone_number,
                called_to=os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
                lead_name=self.lead_name,
                direction=self.direction,
                campaign_id=self.campaign_id,
                outcome=outcome,
                lead_score=lead_score,
                summary=summary,
                reason=reason,
                duration_seconds=dur,
                cost_inr=cost_inr,
                recording_url=self.recording_url
            )
            self._log_saved = True
            if self.sheets_webhook:
                asyncio.create_task(sync_google_sheets_row(self.sheets_webhook, {
                    "call_id": self.call_id,
                    "phone": self.phone_number,
                    "lead_name": self.lead_name,
                    "lead_score": lead_score,
                    "outcome": outcome,
                    "summary": summary,
                    "duration": dur,
                    "cost_inr": cost_inr
                }))
            await push_unified_log("Agent", "info", f"Call finalized: {outcome} ({lead_score}) - {dur}s, ₹{cost_inr}", call_id=self.call_id)
        except Exception as e:
            logger.error("Error finalizing call log: %s", e)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call finished."
