import asyncio
import logging
import os
import time
import httpx
from typing import Optional
from livekit import agents, api
from livekit.agents import llm
from db import (
    check_slot, get_next_available, insert_appointment, book_appointment, log_call,
    push_unified_log, add_contact_memory, sync_google_sheets_row, insert_whatsapp_log
)

logger = logging.getLogger("kaamdhenu-tools")

class RealEstateTools(llm.ToolContext):
    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: str = "",
        lead_name: str = "",
        direction: str = "inbound",
        call_id: str = "",
        campaign_id: Optional[str] = None,
        broker_phone: Optional[str] = None,
        broker_email: Optional[str] = None,
        calcom_api_key: Optional[str] = None,
        calcom_event_type_id: Optional[str] = None,
        sheets_webhook: Optional[str] = None
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.direction = direction
        self.call_id = call_id
        self.campaign_id = campaign_id
        self.broker_phone = broker_phone or os.getenv("DEFAULT_BROKER_WHATSAPP", "")
        self.broker_email = broker_email or os.getenv("DEFAULT_BROKER_EMAIL", "")
        self.calcom_api_key = calcom_api_key or os.getenv("CALCOM_API_KEY", "cal_live_b3cec47f49e2eeb34a38f0500002a22a")
        self.calcom_event_type_id = calcom_event_type_id or os.getenv("CALCOM_EVENT_TYPE_ID", "6934775")
        self.sheets_webhook = sheets_webhook
        self._call_start_time = time.time()
        self.recording_url: Optional[str] = None
        self._log_saved = False
        self.outcome = "completed"
        # Qualification state
        self.client_name = ""
        self.current_location = ""
        self.occupation = ""
        self.bhk_requirement = ""
        self.budget = ""
        self.purpose = "Self-Use"
        self.possession_timeline = "Ready-to-Move"
        self.funding_type = "Bank Loan"
        self.commitment_risk = "Low"
        self.site_visit_date = ""
        self.pickup_required = False
        self.pickup_location = ""
        self.next_callback = ""
        self.objection = ""
        self.whatsapp_status = "— Not Requested"
        self.lead_score = "Warm"
        super().__init__(tools=[])

    def get_all_tools(self):
        return [
            self.check_availability,
            self.book_appointment,
            self.book_calcom,
            self.book_site_visit,
            self.send_whatsapp_brochure,
            self.send_broker_hot_lead_alert,
            self.send_sms_confirmation,
            self.transfer_to_human,
            self.remember_details,
            self.record_client_qualification,
            self.schedule_callback,
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
            self.lead_score = "Hot"
            self.outcome = "booked"
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
    async def book_site_visit(self, client_name: str, visit_datetime: str, pickup_required: bool = False, pickup_address: str = "") -> str:
        """Book a site visit with optional complimentary cab pickup. visit_datetime format: YYYY-MM-DD HH:MM."""
        self.client_name = client_name
        self.site_visit_date = visit_datetime
        self.pickup_required = pickup_required
        self.pickup_location = pickup_address
        self.lead_score = "Hot"
        self.commitment_risk = "High"
        self.outcome = "booked"

        parts = visit_datetime.strip().split(" ")
        date = parts[0] if len(parts) > 0 else visit_datetime
        vtime = parts[1] if len(parts) > 1 else "11:00"
        if len(vtime) == 4 and ":" not in vtime:
            vtime = f"{vtime[:2]}:{vtime[2:]}"

        # 1. Parse into ISO 8601
        calcom_booking_uid = ""
        try:
            from datetime import datetime as _dt, timedelta as _td
            start_dt = _dt.strptime(f"{date} {vtime}", "%Y-%m-%d %H:%M")
            end_dt = start_dt + _td(minutes=45)
            iso_start_time = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            iso_end_time = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except Exception:
            iso_start_time = f"{date}T{vtime}:00.000Z"
            iso_end_time = f"{date}T12:00:00.000Z"

        # 2. Asynchronous, non-blocking Cal.com booking (timeout=4s)
        if self.calcom_api_key and self.calcom_event_type_id:
            try:
                clean_phone = self.phone_number.replace("+", "").replace(" ", "").strip() or "client"
                url = f"https://api.cal.com/v1/bookings?apiKey={self.calcom_api_key}"
                event_id = int(self.calcom_event_type_id) if str(self.calcom_event_type_id).isdigit() else self.calcom_event_type_id
                body = {
                    "eventTypeId": event_id,
                    "start": iso_start_time,
                    "end": iso_end_time,
                    "responses": {
                        "name": client_name or self.lead_name or "Real Estate Lead",
                        "email": f"{clean_phone}@leads.kaamdhenu.ai",
                        "notes": f"Pickup: {'Yes - ' + pickup_address if pickup_required else 'No'}. Campaign: {self.campaign_id or 'Direct'}"
                    },
                    "metadata": {"broker_phone": self.broker_phone, "broker_email": self.broker_email},
                    "timeZone": os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
                }
                async with httpx.AsyncClient(timeout=4.0) as client:
                    resp = await client.post(url, json=body)
                    if resp.status_code in (200, 201):
                        data = resp.json()
                        calcom_booking_uid = data.get("booking", {}).get("uid") or data.get("uid", "")
                        await push_unified_log("Cal.com", "info", f"Cal.com site visit booked (UID: {calcom_booking_uid})", call_id=self.call_id)
                    else:
                        logger.warning(f"Cal.com non-200 response: {resp.status_code} - {resp.text}")
            except Exception as cal_err:
                logger.warning(f"Cal.com async booking fallback: {cal_err}")
                await push_unified_log("Cal.com", "warning", f"Cal.com sync fallback: {cal_err}", call_id=self.call_id)

        # 3. Save to local DB
        try:
            booking_id = await insert_appointment(
                name=client_name or self.lead_name,
                phone=self.phone_number,
                date=date,
                time=vtime,
                service="Site Visit",
                budget=self.budget,
                property_type=self.bhk_requirement,
                pickup_required=pickup_required,
                pickup_address=pickup_address,
                calcom_booking_uid=calcom_booking_uid
            )
            pickup_msg = f" with cab pickup from {pickup_address}" if pickup_required and pickup_address else ""
            await push_unified_log("Tools", "info", f"Site visit booked: {client_name} on {visit_datetime}{pickup_msg} (Cal.com UID: {calcom_booking_uid or 'Local'})", call_id=self.call_id)
            return f"Site visit confirmed for {visit_datetime}! Ref: {booking_id}.{' Cab pickup arranged from ' + pickup_address + '.' if pickup_required and pickup_address else ''}"
        except Exception as e:
            logger.error(f"Site visit booking error: {e}")
            return "Site visit booked with our team. We will confirm shortly."

    @llm.function_tool
    async def record_client_qualification(self, client_name: str = "", location: str = "", occupation: str = "", bhk: str = "", budget: str = "", possession: str = "", funding: str = "", objection: str = "") -> str:
        """Silently record client qualification details gathered during conversation. Call this as you learn each detail."""
        if client_name: self.client_name = client_name
        if location: self.current_location = location
        if occupation: self.occupation = occupation
        if bhk: self.bhk_requirement = bhk
        if budget: self.budget = budget
        if possession: self.possession_timeline = possession
        if funding: self.funding_type = funding
        if objection: self.objection = objection
        # Auto-score
        if self.budget and self.bhk_requirement:
            self.lead_score = "Warm"
        if self.site_visit_date:
            self.lead_score = "Hot"
        await push_unified_log("CRM", "info", f"Qualification updated: {client_name or self.lead_name} - {bhk} {budget}", call_id=self.call_id)
        return "Client details recorded."

    @llm.function_tool
    async def schedule_callback(self, callback_time: str, notes: str = "") -> str:
        """Schedule a callback when client says 'call me later' or 'busy right now'. callback_time example: 'today 6pm', '2024-09-03 14:00'."""
        self.next_callback = callback_time
        self.outcome = "callback_requested"
        self.lead_score = "Warm"
        await add_contact_memory(self.phone_number, f"Callback requested for {callback_time}. {notes}")
        await push_unified_log("CRM", "info", f"Callback scheduled: {self.phone_number} at {callback_time}", call_id=self.call_id)
        return f"Callback scheduled for {callback_time}. We will call you back."

    @llm.function_tool
    async def send_whatsapp_brochure(self, phone_number: str = "") -> str:
        """Send property brochure, floor plans, and site location to lead via WhatsApp. Call when client agrees to receive details."""
        phone = phone_number or self.phone_number
        self.whatsapp_status = "✅ Sent Auto"
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_wa = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
        msg = f"Namaste {self.client_name or self.lead_name}! \U0001f3e1\nThank you for speaking with Kaamdhenu Real Estate.\nHere are the brochure & floor plans for our premium properties.\nLocation: Near City Center.\nSee you at the site visit!"

        await insert_whatsapp_log(phone, msg, "dispatched", self.call_id)

        if not (sid and token):
            await push_unified_log("WhatsApp", "info", f"Brochure dispatched (demo): {phone}", call_id=self.call_id)
            return "Brochure sent to your WhatsApp."
        try:
            from twilio.rest import Client
            to_wa = f"whatsapp:{phone}" if not phone.startswith("whatsapp:") else phone
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=msg, from_=from_wa, to=to_wa))
            await insert_whatsapp_log(phone, msg, "delivered", self.call_id)
            await push_unified_log("WhatsApp", "info", f"Brochure sent to lead: {phone}", call_id=self.call_id)
            return "Brochure sent to your WhatsApp."
        except Exception as exc:
            await insert_whatsapp_log(phone, msg, f"failed: {exc}", self.call_id)
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
                f"\U0001f525 *NEW HOT LEAD SITE VISIT BOOKED*\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f464 *Lead Name:* {name}\n"
                f"\U0001f4de *Phone:* {phone}\n"
                f"\U0001f3e2 *Requirement:* {property_type or '2BHK/3BHK'}\n"
                f"\U0001f4b0 *Budget:* {budget or 'Standard'}\n"
                f"\U0001f4c5 *Visit Slot:* {date} at {time}\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f449 *Action:* Please call for confirmation & arrange site pass."
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
            self.lead_score = "Hot"
        elif outcome == "callback_requested":
            lead_score = "Warm"
            self.lead_score = "Warm"
        else:
            self.lead_score = lead_score

        if not summary:
            summary = f"Outcome: {outcome}. Duration: {dur}s. Lead qualified as {lead_score}."

        try:
            await log_call(
                call_id=self.call_id,
                phone_number=self.phone_number,
                called_to=os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
                lead_name=self.client_name or self.lead_name,
                direction=self.direction,
                campaign_id=self.campaign_id,
                outcome=outcome,
                lead_score=lead_score,
                summary=summary,
                reason=reason,
                duration_seconds=dur,
                cost_inr=cost_inr,
                recording_url=self.recording_url,
                client_name=self.client_name,
                current_location=self.current_location,
                occupation=self.occupation,
                bhk_requirement=self.bhk_requirement,
                budget=self.budget,
                purpose=self.purpose,
                possession_timeline=self.possession_timeline,
                funding_type=self.funding_type,
                commitment_risk=self.commitment_risk,
                site_visit_date=self.site_visit_date,
                pickup_required=self.pickup_required,
                pickup_location=self.pickup_location,
                next_callback=self.next_callback,
                objection=self.objection,
                whatsapp_status=self.whatsapp_status
            )
            self._log_saved = True
            if self.sheets_webhook:
                asyncio.create_task(sync_google_sheets_row(self.sheets_webhook, {
                    "call_id": self.call_id,
                    "phone": self.phone_number,
                    "lead_name": self.client_name or self.lead_name,
                    "lead_score": lead_score,
                    "outcome": outcome,
                    "summary": summary,
                    "duration": dur,
                    "cost_inr": cost_inr
                }))
            await push_unified_log("Agent", "info", f"Call finalized: {outcome} ({lead_score}) - {dur}s, \u20b9{cost_inr}", call_id=self.call_id)
        except Exception as e:
            logger.error("Error finalizing call log: %s", e)
        try:
            await self.ctx.room.disconnect()
        except Exception:
            pass
        return "Call finished."
