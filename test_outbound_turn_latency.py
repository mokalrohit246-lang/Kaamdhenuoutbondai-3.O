import asyncio
import time
import sys
from unittest.mock import MagicMock
from dotenv import load_dotenv

load_dotenv(".env", override=True)

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from tools import RealEstateTools

async def test_tools_latency():
    print("=" * 60)
    print("RUNNING OUTBOUND TURN LATENCY & TOOL EXECUTION AUDIT")
    print("=" * 60)

    # Mock JobContext and Room
    mock_ctx = MagicMock()
    mock_room = MagicMock()
    mock_ctx.room = mock_room
    mock_ctx.room.name = "test-call-room-1234"
    async def _fake_disconnect():
        pass
    mock_ctx.room.disconnect = _fake_disconnect

    done_event = asyncio.Event()

    # 1. Test instantiation
    tool_ctx = RealEstateTools(
        ctx=mock_ctx,
        phone_number="9892057717",
        lead_name="Test Lead",
        direction="outbound",
        call_id="test-call-room-1234",
        campaign_id="test-campaign-1",
        done_event=done_event
    )

    # Assert qualification attributes exist on instantiation
    assert hasattr(tool_ctx, "client_name"), "Missing client_name attribute!"
    assert hasattr(tool_ctx, "bhk_requirement"), "Missing bhk_requirement attribute!"
    assert hasattr(tool_ctx, "budget"), "Missing budget attribute!"
    assert hasattr(tool_ctx, "site_visit_date"), "Missing site_visit_date attribute!"
    assert hasattr(tool_ctx, "lead_score"), "Missing lead_score attribute!"
    assert hasattr(tool_ctx, "outcome"), "Missing outcome attribute!"
    print("✅ RealEstateTools attributes verified successfully on instance.")

    # 2. Test record_client_qualification latency
    t0 = time.perf_counter()
    res_qual = await tool_ctx.record_client_qualification(
        client_name="Aarav Mehta",
        bhk="3BHK",
        budget="1.5 Cr",
        location="Andheri West",
        possession="Within 3 months"
    )
    t_qual = (time.perf_counter() - t0) * 1000
    print(f"record_client_qualification: {t_qual:.2f} ms | Result: {res_qual}")
    assert t_qual < 300, f"record_client_qualification exceeded 300ms: {t_qual:.2f}ms"
    assert tool_ctx.bhk_requirement == "3BHK"
    assert tool_ctx.budget == "1.5 Cr"
    assert tool_ctx.lead_score == "Warm"

    # 3. Test remember_details latency
    t0 = time.perf_counter()
    res_rem = await tool_ctx.remember_details(insight="Prefers higher floor with city view")
    t_rem = (time.perf_counter() - t0) * 1000
    print(f"remember_details: {t_rem:.2f} ms | Result: {res_rem}")
    assert t_rem < 300, f"remember_details exceeded 300ms: {t_rem:.2f}ms"

    # 4. Test schedule_callback latency
    t0 = time.perf_counter()
    res_cb = await tool_ctx.schedule_callback(
        time_description="kal shaam 5 baje",
        reason="In client meeting",
        notes="Discuss payment schedule"
    )
    t_cb = (time.perf_counter() - t0) * 1000
    print(f"schedule_callback: {t_cb:.2f} ms | Result: {res_cb}")
    assert t_cb < 300, f"schedule_callback exceeded 300ms: {t_cb:.2f}ms"
    assert "Done sir" in res_cb
    assert tool_ctx.outcome == "callback_requested"

    # 5. Test book_appointment latency
    t0 = time.perf_counter()
    res_apt = await tool_ctx.book_appointment(
        name="Aarav Mehta",
        phone="9892057717",
        date="2026-10-26",
        time="11:00",
        service="Site Visit",
        budget="1.5 Cr",
        property_type="3BHK"
    )
    t_apt = (time.perf_counter() - t0) * 1000
    print(f"book_appointment: {t_apt:.2f} ms | Result: {res_apt}")
    assert t_apt < 300, f"book_appointment exceeded 300ms: {t_apt:.2f}ms"
    assert "Site visit confirmed" in res_apt
    assert tool_ctx.appointment_booked is True

    # 6. Test book_site_visit latency
    t0 = time.perf_counter()
    res_sv = await tool_ctx.book_site_visit(
        client_name="Aarav Mehta",
        visit_datetime="2026-10-26 15:00",
        pickup_required=True,
        pickup_address="Bandra Kurla Complex"
    )
    t_sv = (time.perf_counter() - t0) * 1000
    print(f"book_site_visit: {t_sv:.2f} ms | Result: {res_sv}")
    assert t_sv < 300, f"book_site_visit exceeded 300ms: {t_sv:.2f}ms"
    assert "Site visit confirmed" in res_sv

    # 7. Test book_calcom latency
    t0 = time.perf_counter()
    res_cal = await tool_ctx.book_calcom(
        name="Aarav Mehta",
        email="aarav@example.com",
        date="2026-10-26",
        start_time="16:00"
    )
    t_cal = (time.perf_counter() - t0) * 1000
    print(f"book_calcom: {t_cal:.2f} ms | Result: {res_cal}")
    assert t_cal < 300, f"book_calcom exceeded 300ms: {t_cal:.2f}ms"

    # 8. Test send_project_brochure latency
    t0 = time.perf_counter()
    res_broch = await tool_ctx.send_project_brochure(phone_number="9892057717")
    t_broch = (time.perf_counter() - t0) * 1000
    print(f"send_project_brochure: {t_broch:.2f} ms | Result: {res_broch}")
    assert t_broch < 300, f"send_project_brochure exceeded 300ms: {t_broch:.2f}ms"

    # 9. Test send_whatsapp_brochure latency
    t0 = time.perf_counter()
    res_wabroch = await tool_ctx.send_whatsapp_brochure(phone_number="9892057717")
    t_wabroch = (time.perf_counter() - t0) * 1000
    print(f"send_whatsapp_brochure: {t_wabroch:.2f} ms | Result: {res_wabroch}")
    assert t_wabroch < 300, f"send_whatsapp_brochure exceeded 300ms: {t_wabroch:.2f}ms"

    # 10. Test send_broker_hot_lead_alert latency
    t0 = time.perf_counter()
    res_alert = await tool_ctx.send_broker_hot_lead_alert(
        name="Aarav Mehta",
        phone="9892057717",
        budget="1.5 Cr",
        property_type="3BHK"
    )
    t_alert = (time.perf_counter() - t0) * 1000
    print(f"send_broker_hot_lead_alert: {t_alert:.2f} ms | Result: {res_alert}")
    assert t_alert < 300, f"send_broker_hot_lead_alert exceeded 300ms: {t_alert:.2f}ms"

    # 11. Test end_call latency
    t0 = time.perf_counter()
    res_end = await tool_ctx.end_call(
        reason="conversation_completed",
        outcome="booked",
        lead_score="Hot",
        summary="Aarav confirmed site visit for tomorrow 3 PM."
    )
    t_end = (time.perf_counter() - t0) * 1000
    print(f"end_call: {t_end:.2f} ms | Result: {res_end}")
    assert t_end < 300, f"end_call exceeded 300ms: {t_end:.2f}ms"

    print("\n" + "=" * 60)
    print("ALL TOOLS PASSED SUB-300MS LATENCY BENCHMARK (0 FREEZE)")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(test_tools_latency())
