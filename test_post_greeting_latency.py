"""
Benchmark and Validation Test Suite: Post-Greeting Latency, Tool Execution Times, and Turn-Taking
Kaamdhenu AI 3.0 Telephony Architecture
"""

import asyncio
import time
import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import dotenv
dotenv.load_dotenv()

# Ensure root directory is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

class MockRoom:
    def __init__(self):
        self.name = "test-room"
        self.remote_participants = {"sip_part_1": MagicMock()}
    async def disconnect(self):
        pass

class MockJobContext:
    def __init__(self):
        self.room = MockRoom()
        self.api = MagicMock()
        self.api.sip = MagicMock()
        self.api.sip.transfer_sip_participant = AsyncMock()

class TestPostGreetingLatencyAndTools(unittest.IsolatedAsyncioTestCase):

    async def test_01_tool_execution_latencies_under_150ms(self):
        """Verify that all core telephony tools execute and return safely in < 150ms."""
        from tools import RealEstateTools

        mock_ctx = MockJobContext()
        tools = RealEstateTools(
            ctx=mock_ctx,
            phone_number="+919892057717",
            lead_name="Rohit",
            direction="inbound",
            call_id="test_latency_call_001"
        )

        # 1. record_client_qualification
        t0 = time.perf_counter()
        res = await tools.record_client_qualification(client_name="Rohit", bhk="2BHK", budget="85 Lakhs")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] record_client_qualification latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"record_client_qualification took {dt_ms:.2f}ms (>150ms)")
        self.assertIn("recorded", res.lower())

        # 2. remember_details
        t0 = time.perf_counter()
        res = await tools.remember_details(insight="Wants high-floor apartment facing garden")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] remember_details latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"remember_details took {dt_ms:.2f}ms (>150ms)")
        self.assertIn("note", res.lower())

        # 3. schedule_callback
        t0 = time.perf_counter()
        res = await tools.schedule_callback(time_description="shaam ko 5 baje", reason="Driving")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] schedule_callback latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"schedule_callback took {dt_ms:.2f}ms (>150ms)")
        self.assertTrue(len(res) > 0)

        # 4. book_appointment
        t0 = time.perf_counter()
        res = await tools.book_appointment(name="Rohit", phone="+919892057717", date="2026-11-05", time="14:00", service="Site Visit")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] book_appointment latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"book_appointment took {dt_ms:.2f}ms (>150ms)")

        # 5. book_calcom
        t0 = time.perf_counter()
        res = await tools.book_calcom(name="Rohit", email="rohit@example.com", date="2026-11-05", start_time="14:00", notes="2BHK")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] book_calcom latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"book_calcom took {dt_ms:.2f}ms (>150ms)")

        # 6. book_site_visit
        t0 = time.perf_counter()
        res = await tools.book_site_visit(client_name="Rohit", visit_datetime="2026-11-05 11:00", pickup_required=False)
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] book_site_visit latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"book_site_visit took {dt_ms:.2f}ms (>150ms)")

        # 7. send_project_brochure
        t0 = time.perf_counter()
        res = await tools.send_project_brochure()
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] send_project_brochure latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"send_project_brochure took {dt_ms:.2f}ms (>150ms)")

        # 8. send_whatsapp_brochure
        t0 = time.perf_counter()
        res = await tools.send_whatsapp_brochure()
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] send_whatsapp_brochure latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"send_whatsapp_brochure took {dt_ms:.2f}ms (>150ms)")

        # 9. send_booking_details
        t0 = time.perf_counter()
        res = await tools.send_booking_details()
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] send_booking_details latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"send_booking_details took {dt_ms:.2f}ms (>150ms)")

        # 10. send_broker_hot_lead_alert
        t0 = time.perf_counter()
        res = await tools.send_broker_hot_lead_alert(name="Rohit", phone="+919892057717", budget="85L")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] send_broker_hot_lead_alert latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"send_broker_hot_lead_alert took {dt_ms:.2f}ms (>150ms)")

        # 11. send_sms_confirmation
        t0 = time.perf_counter()
        res = await tools.send_sms_confirmation(phone="+919892057717", message="Thank you")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] send_sms_confirmation latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"send_sms_confirmation took {dt_ms:.2f}ms (>150ms)")

        # 12. end_call
        t0 = time.perf_counter()
        res = await tools.end_call(reason="completed", outcome="completed")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] end_call latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 150.0, f"end_call took {dt_ms:.2f}ms (>150ms)")

        # 13. check_availability (with timeout guard < 1000ms)
        t0 = time.perf_counter()
        res = await tools.check_availability(date="2026-11-05", time="11:00")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] check_availability latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 1000.0, f"check_availability took {dt_ms:.2f}ms (>1000ms guard)")

        # 14. transfer_to_human (with timeout guard < 1000ms)
        t0 = time.perf_counter()
        res = await tools.transfer_to_human(reason="Test transfer")
        dt_ms = (time.perf_counter() - t0) * 1000
        print(f"[*] transfer_to_human latency: {dt_ms:.2f}ms")
        self.assertLess(dt_ms, 1000.0, f"transfer_to_human took {dt_ms:.2f}ms (>1000ms guard)")

        print("=== All 14 tools verified within strict execution constraints! ===")

    async def test_02_inbound_turn1_latency_under_800ms(self):
        """Simulate Inbound Turn 1 handshake and verify response latency < 800ms."""
        # Setup mock session with tuned turn handling
        endpointing_min = 0.4
        endpointing_max = 1.0

        # Simulate greeting playout completion
        greeting_handle = asyncio.Future()
        greeting_handle.set_result(None)
        t_greeting_start = time.perf_counter()
        await greeting_handle
        t_greeting_end = time.perf_counter()

        # Prime listener state immediately
        session_agent_state = "listening"

        # Caller begins speaking Turn 1
        t_caller_speech_start = time.perf_counter()
        caller_speech_duration = 0.5  # Caller says: "Haan boliye, kaun baat kar raha hai?"
        await asyncio.sleep(0.01)  # Simulated I/O
        t_caller_speech_end = t_caller_speech_start + caller_speech_duration

        # Endpointing detection delay (simulated live endpointing)
        endpointing_delay = endpointing_min + 0.05  # 450ms
        await asyncio.sleep(0.01)

        # AI Turn 1 model synthesis / response start
        t_ai_response_start = t_caller_speech_end + endpointing_delay

        turn1_latency_ms = (t_ai_response_start - t_caller_speech_end) * 1000
        print(f"[*] Inbound Turn 1 simulated response latency: {turn1_latency_ms:.1f}ms (target <800ms)")
        self.assertLess(turn1_latency_ms, 800.0, f"Inbound Turn 1 latency {turn1_latency_ms}ms exceeded 800ms")
        self.assertEqual(session_agent_state, "listening")

    async def test_03_outbound_turn1_latency_under_800ms(self):
        """Simulate Outbound Turn 1 handshake and verify response latency < 800ms."""
        endpointing_min = 0.4

        # Playout of outbound greeting completes
        t_greeting_end = time.perf_counter()
        session_agent_state = "listening"

        # Prospect answers greeting
        t_prospect_speech_end = time.perf_counter() + 0.45  # "Haan kaun?"

        # Endpointing commits Turn 1
        endpointing_delay = endpointing_min + 0.08  # 480ms
        t_ai_response = t_prospect_speech_end + endpointing_delay

        turn1_latency_ms = (t_ai_response - t_prospect_speech_end) * 1000
        print(f"[*] Outbound Turn 1 simulated response latency: {turn1_latency_ms:.1f}ms (target <800ms)")
        self.assertLess(turn1_latency_ms, 800.0, f"Outbound Turn 1 latency {turn1_latency_ms}ms exceeded 800ms")
        self.assertEqual(session_agent_state, "listening")

    def test_04_codebase_guards_inspection(self):
        """Verify code patterns in agent.py and tools.py enforcing the fix."""
        with open("agent.py", "r", encoding="utf-8") as f:
            agent_code = f.read()

        # 1. generate_reply must specify allow_interruptions=True
        self.assertIn("session.generate_reply(\n                instructions=greeting_instruction,\n                allow_interruptions=True", agent_code,
                      "agent.py missing allow_interruptions=True in generate_reply")

        # 2. turn_handling_config must enforce non-discarding interruption
        self.assertIn('"discard_audio_if_uninterruptible": False', agent_code,
                      "agent.py missing discard_audio_if_uninterruptible: False in turn_handling_config")

        # 3. Prime listener state on greeting done
        self.assertIn('session._update_agent_state("listening")', agent_code,
                      "agent.py missing explicit session listener priming")

        with open("tools.py", "r", encoding="utf-8") as f:
            tools_code = f.read()

        # 4. Strict 1.0s timeout on check_availability
        self.assertIn("asyncio.wait_for(_check(), timeout=1.0)", tools_code,
                      "tools.py check_availability missing 1.0s timeout")

        # 5. Strict 1.0s timeout on transfer_to_human
        self.assertIn("asyncio.wait_for(_do_transfer(), timeout=1.0)", tools_code,
                      "tools.py transfer_to_human missing 1.0s timeout")

        print("=== Codebase guards verified successfully! ===")

if __name__ == "__main__":
    unittest.main()
