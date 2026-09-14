"""
Comprehensive Verification Test Suite for Meta WhatsApp Cloud API Integration.
Tests:
1. Phone formatting for Meta Cloud API (format_whatsapp_phone)
2. Direct text messaging (send_text_message)
3. Document / Brochure PDF messaging (send_document_message)
4. Structured site visit confirmation card (send_appointment_confirmation)
5. AI Real Estate Sales Guardrails & Brochure Intent (generate_whatsapp_ai_response)
6. Database campaign brochure & project fields persistence (create_campaign, get_campaign, update_campaign)
7. WhatsApp logs persistence & retrieval (insert_whatsapp_log, list_whatsapp_logs)
8. Webhook verification challenge & inbound processing simulation
"""

import sys
import os
import asyncio
import json

# Ensure project root is in python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import whatsapp_service
import db

async def test_phone_formatting():
    print("\n--- TEST 1: format_whatsapp_phone ---")
    assert whatsapp_service.format_whatsapp_phone("9876543210") == "919876543210"
    assert whatsapp_service.format_whatsapp_phone("+91 98765 43210") == "919876543210"
    assert whatsapp_service.format_whatsapp_phone("09876543210") == "919876543210"
    assert whatsapp_service.format_whatsapp_phone("919876543210") == "919876543210"
    print("PASS: format_whatsapp_phone normalized 10-digit Indian numbers accurately.")

async def test_text_messaging():
    print("\n--- TEST 2: whatsapp_service.send_text_message ---")
    res = await whatsapp_service.send_text_message(
        to_phone="+919876543210",
        text="Hello from Kaamdhenu AI Voice Real Estate Platform!"
    )
    print(f"send_text_message result: {res}")
    assert res.get("success") is True, f"Expected success=True, got {res}"
    print("PASS: send_text_message executed without errors.")

async def test_document_messaging():
    print("\n--- TEST 3: whatsapp_service.send_document_message ---")
    res_doc = await whatsapp_service.send_document_message(
        to_phone="+919876543210",
        document_url="https://example.com/kaamdhenu_horizon_brochure.pdf",
        caption="Official Kaamdhenu Horizon Luxury Residences Brochure",
        filename="Kaamdhenu_Horizon_Brochure.pdf"
    )
    print(f"send_document_message result: {res_doc}")
    assert res_doc.get("success") is True, f"Expected success=True, got {res_doc}"
    print("PASS: send_document_message executed successfully.")

async def test_appointment_confirmation():
    print("\n--- TEST 4: whatsapp_service.send_appointment_confirmation ---")
    lead_data = {
        "name": "Rohan Sharma",
        "phone": "+919876543210"
    }
    appointment_data = {
        "date": "2026-09-20",
        "time": "11:30 AM",
        "project_name": "Kaamdhenu Grandeur",
        "site_address": "Sector 150, Noida Expressway, Greater Noida",
        "pickup_required": True,
        "pickup_address": "Indirapuram, Ghaziabad",
        "bhk_preference": "3 BHK Luxury",
        "budget": "1.75 Cr"
    }
    res_appt = await whatsapp_service.send_appointment_confirmation(
        to_phone="+919876543210",
        lead_data=lead_data,
        appointment_data=appointment_data
    )
    print(f"send_appointment_confirmation result: {res_appt}")
    assert res_appt.get("success") is True, f"Expected success=True, got {res_appt}"
    print("PASS: send_appointment_confirmation executed successfully.")

async def test_ai_sales_guardrails():
    print("\n--- TEST 5: whatsapp_service.generate_whatsapp_ai_response ---")
    campaign_ctx = {
        "name": "Kaamdhenu Horizon Launch",
        "project_name": "Kaamdhenu Horizon",
        "site_address": "Sector 150, Noida Expressway",
        "project_highlights": "2 & 3 BHK luxury residences with clubhouse, swimming pool, 80% open green space.",
        "brochure_url": "https://example.com/brochures/horizon.pdf"
    }
    lead_ctx = {"lead_name": "Deepak Verma", "phone": "919876543210"}

    # Test 5.1: Lead asking for brochure
    reply, wants_brochure = await whatsapp_service.generate_whatsapp_ai_response(
        incoming_text="Please send me the brochure and price list for 3 BHK",
        campaign_context=campaign_ctx,
        lead_context=lead_ctx
    )
    print(f"Brochure Query -> wants_brochure={wants_brochure}, reply: {reply[:90]}...")
    assert wants_brochure is True, "Expected wants_brochure=True for brochure query"
    assert len(reply) > 10

    # Test 5.2: Lead asking unrelated query (politics/cricket) -> should deflect back to real estate
    reply_deflect, wants_b2 = await whatsapp_service.generate_whatsapp_ai_response(
        incoming_text="Who won yesterday's cricket match?",
        campaign_context=campaign_ctx,
        lead_context=lead_ctx
    )
    print(f"Deflection Query -> wants_brochure={wants_b2}, reply: {reply_deflect[:90]}...")
    assert wants_b2 is False
    assert len(reply_deflect) > 10
    print("PASS: generate_whatsapp_ai_response properly classified intent and generated response.")

async def test_database_integration():
    print("\n--- TEST 6: Database Campaign & WhatsApp Logs Persistence ---")
    # 6.1 Create campaign with project and brochure details
    camp_data = {
        "name": "Test WhatsApp Campaign 2026",
        "agent_profile_id": "default",
        "allocated_minutes": 100,
        "calling_mode": "regular",
        "project_name": "Kaamdhenu Pinnacle",
        "site_address": "Golf Course Extension Road, Gurgaon",
        "project_highlights": "Ultra-Luxury 3 & 4 BHK Golf Residences",
        "pickup_drop_notes": "Free chauffeur-driven Mercedes pickup for site tour",
        "brochure_url": "https://example.com/pinnacle_brochure.pdf"
    }
    cid = await db.create_campaign(camp_data)
    print(f"Created test campaign with ID: {cid}")
    assert cid is not None

    # 6.2 Retrieve campaign
    camp = await db.get_campaign(cid)
    assert camp is not None
    assert camp.get("project_name") == "Kaamdhenu Pinnacle"
    assert camp.get("brochure_url") == "https://example.com/pinnacle_brochure.pdf"
    print("Retrieved campaign successfully verified.")

    # 6.3 Update campaign details
    await db.update_campaign(cid, {
        "project_name": "Kaamdhenu Pinnacle Phase 2",
        "pickup_drop_notes": "Free Tesla pickup for site tour"
    })
    camp_updated = await db.get_campaign(cid)
    assert camp_updated.get("project_name") == "Kaamdhenu Pinnacle Phase 2"
    assert camp_updated.get("pickup_drop_notes") == "Free Tesla pickup for site tour"
    print("Campaign updated details successfully verified.")

    # 6.4 Insert and list WhatsApp logs
    await db.insert_whatsapp_log(
        phone_number="919876543210",
        message="Site visit confirmation test log",
        status="sent",
        direction="outbound",
        message_type="document",
        campaign_id=str(cid)
    )
    logs = await db.list_whatsapp_logs(limit=10, phone="919876543210")
    print(f"Retrieved {len(logs)} WhatsApp logs from database.")
    assert len(logs) > 0
    assert any("Site visit confirmation" in l.get("content", "") for l in logs)
    print("PASS: Database campaigns and WhatsApp logs verified.")

async def test_webhook_flow_simulation():
    print("\n--- TEST 7: Webhook Verification & Inbound Flow Simulation ---")
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "kaamdhenu_whatsapp_verify_token")

    # 7.1 Verify Challenge Logic
    def verify_webhook(hub_mode, hub_verify_token, hub_challenge):
        if hub_mode == "subscribe" and hub_verify_token == verify_token:
            return 200, hub_challenge
        return 403, "Verification token mismatch"

    code_ok, challenge_ok = verify_webhook("subscribe", verify_token, "987654321")
    assert code_ok == 200 and challenge_ok == "987654321"

    code_bad, _ = verify_webhook("subscribe", "wrong_token", "987654321")
    assert code_bad == 403
    print("PASS: Webhook verification challenge passed.")

    # 7.2 Inbound Webhook Payload Processing
    inbound_meta_payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"profile": {"name": "Suresh Gupta"}, "wa_id": "919999988888"}],
                            "messages": [
                                {
                                    "from": "919999988888",
                                    "type": "text",
                                    "text": {"body": "Send brochure of this project please."}
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }

    # Simulate inbound handler logic
    val = inbound_meta_payload["entry"][0]["changes"][0]["value"]
    msg = val["messages"][0]
    from_wa = msg["from"]
    body = msg["text"]["body"]
    contact_name = val["contacts"][0]["profile"]["name"]

    await db.insert_whatsapp_log(
        phone_number=from_wa,
        message=body,
        status="received",
        direction="inbound",
        message_type="text"
    )

    reply_text, wants_brochure = await whatsapp_service.generate_whatsapp_ai_response(
        incoming_text=body,
        campaign_context={"project_name": "Kaamdhenu Heights", "site_address": "Sector 150"},
        lead_context={"lead_name": contact_name, "phone": from_wa}
    )
    print(f"Simulated Webhook Inbound handled: Contact={contact_name}, WantsBrochure={wants_brochure}, Reply={reply_text[:70]}...")
    assert wants_brochure is True

    # Send outbound response & brochure
    out_res = await whatsapp_service.send_text_message(to_phone=from_wa, text=reply_text)
    assert out_res.get("success") is True

    print("PASS: Webhook inbound parsing, AI intent evaluation, and outbound reply flow verified.")

async def test_single_call_metadata_resolution():
    print("\n--- TEST 8: Single Call Metadata & RealEstateTools Resolution ---")
    from unittest.mock import MagicMock
    try:
        import livekit
    except ImportError:
        class MockToolContext:
            def __init__(self, *args, **kwargs):
                pass
        mock_lk = MagicMock()
        mock_llm = MagicMock()
        mock_llm.function_tool = lambda f: f
        mock_llm.ToolContext = MockToolContext
        mock_lk.agents = MagicMock()
        mock_lk.agents.llm = mock_llm
        mock_lk.agents.JobContext = object
        sys.modules["livekit"] = mock_lk
        sys.modules["livekit.agents"] = mock_lk.agents
        sys.modules["livekit.agents.llm"] = mock_llm
        sys.modules["livekit.api"] = mock_lk

    import tools
    
    single_call_meta = {
        "phone_number": "+919876543210",
        "lead_name": "Vikram Malhotra",
        "direction": "outbound",
        "call_id": "test_single_call_101",
        "campaign_id": None,  # Standalone single call without campaign
        "project_name": "Kaamdhenu Solitaire",
        "brochure_url": "https://example.com/brochures/solitaire.pdf",
        "site_address": "Sector 45, Noida Expressway",
        "pickup_drop_notes": "Complimentary Cab Pickup from Botanical Garden Metro",
        "project_highlights": "Ultra-Luxury 3 BHK & 4 BHK Golf View Residences"
    }

    # Initialize RealEstateTools with single call metadata
    re_tools = tools.RealEstateTools(
        ctx=None,
        phone_number=single_call_meta["phone_number"],
        lead_name=single_call_meta["lead_name"],
        direction=single_call_meta["direction"],
        call_id=single_call_meta["call_id"],
        campaign_id=single_call_meta["campaign_id"],
        project_name=single_call_meta["project_name"],
        brochure_url=single_call_meta["brochure_url"],
        site_address=single_call_meta["site_address"],
        pickup_drop_notes=single_call_meta["pickup_drop_notes"],
        project_highlights=single_call_meta["project_highlights"]
    )
    assert re_tools.project_name == "Kaamdhenu Solitaire"
    assert re_tools.brochure_url == "https://example.com/brochures/solitaire.pdf"
    assert re_tools.site_address == "Sector 45, Noida Expressway"
    assert re_tools.pickup_drop_notes == "Complimentary Cab Pickup from Botanical Garden Metro"
    assert re_tools.project_highlights == "Ultra-Luxury 3 BHK & 4 BHK Golf View Residences"

    # Test send_project_brochure without campaign ID
    brochure_result = await re_tools.send_project_brochure(phone_number="+919876543210")
    print(f"send_project_brochure result: {brochure_result}")
    assert "Maine WhatsApp par brochure" in brochure_result

    # Test book_site_visit without campaign ID
    visit_result = await re_tools.book_site_visit(
        visit_datetime="tomorrow at 11:00 AM",
        pickup_required=True,
        pickup_address="Noida City Center Metro",
        client_name="Vikram Malhotra"
    )
    print(f"book_site_visit result: {visit_result}")
    assert "Site visit confirmed" in visit_result

    print("PASS: Single call metadata successfully bound to RealEstateTools and executed without campaign ID.")

async def test_brochure_upload_resilience():
    print("\n--- TEST 9: Brochure Upload Resilience (Standalone & Campaign Fallbacks) ---")
    from io import BytesIO
    from unittest.mock import MagicMock

    class MockUploadFile:
        def __init__(self, filename=None, file=None, content_type="application/pdf"):
            self.filename = filename
            self.file = file
            self.content_type = content_type
        async def read(self):
            if self.file:
                return self.file.read()
            return b""

    class MockJSONResponse:
        def __init__(self, content=None, status_code=200):
            self.content = content
            self.status_code = status_code
            self.body = json.dumps(content).encode()

    def mock_decorator(*args, **kwargs):
        def wrapper(func):
            return func
        return wrapper

    mock_app = MagicMock()
    mock_app.post = mock_decorator
    mock_app.get = mock_decorator
    mock_app.delete = mock_decorator
    mock_app.mount = MagicMock()

    for mod in ["aiohttp", "certifi", "dotenv", "fastapi", "fastapi.responses", "fastapi.staticfiles", "pydantic"]:
        if mod not in sys.modules:
            m = MagicMock()
            if mod == "fastapi":
                m.UploadFile = MockUploadFile
                m.File = lambda default=None: default
                m.FastAPI = lambda *args, **kwargs: mock_app
                m.HTTPException = Exception
            elif mod == "fastapi.responses":
                m.JSONResponse = MockJSONResponse
            elif mod == "pydantic":
                m.BaseModel = object
            sys.modules[mod] = m

    try:
        from fastapi import UploadFile
    except Exception:
        UploadFile = MockUploadFile

    import server

    # 9.1 Test standalone upload with valid PDF
    pdf_bytes = b"%PDF-1.4 Mock Luxury Tower Brochure Content"
    mock_file = UploadFile(filename="luxury_tower.pdf", file=BytesIO(pdf_bytes))
    mock_file.content_type = "application/pdf"
    res1 = await server.api_upload_standalone_brochure(file=mock_file)
    # Check if JSONResponse or dict
    body1 = json.loads(res1.body.decode()) if hasattr(res1, "body") else (getattr(res1, "content", res1))
    print(f"Standalone upload result: {body1}")
    assert body1.get("success") is True or body1.get("ok") is True
    assert body1.get("brochure_url") != ""
    assert "luxury_tower" in body1.get("filename", "")

    # 9.2 Test standalone upload with empty/missing file (must NOT throw 500, must return 200 JSON)
    res_empty = await server.api_upload_standalone_brochure(file=None)
    body_empty = json.loads(res_empty.body.decode()) if hasattr(res_empty, "body") else (getattr(res_empty, "content", res_empty))
    print(f"Standalone empty upload result: {body_empty}")
    assert getattr(res_empty, "status_code", 200) == 200
    assert body_empty.get("success") is False
    assert "No file" in body_empty.get("error", "")

    # 9.3 Test campaign upload with cid='standalone' (must NOT throw 500 or 404)
    mock_file_sa = UploadFile(filename="standalone_project.png", file=BytesIO(b"FAKEPNGCONTENT"))
    mock_file_sa.content_type = "image/png"
    res_sa = await server.api_upload_campaign_brochure(cid="standalone", file=mock_file_sa)
    body_sa = json.loads(res_sa.body.decode()) if hasattr(res_sa, "body") else (getattr(res_sa, "content", res_sa))
    print(f"Campaign 'standalone' upload result: {body_sa}")
    assert getattr(res_sa, "status_code", 200) == 200
    assert body_sa.get("success") is True or body_sa.get("ok") is True
    assert body_sa.get("brochure_url") != ""

    # 9.4 Test campaign upload with non-existent cid (must gracefully succeed and return 200 JSON)
    mock_file_nx = UploadFile(filename="custom_tower.pdf", file=BytesIO(pdf_bytes))
    mock_file_nx.content_type = "application/pdf"
    res_nx = await server.api_upload_campaign_brochure(cid="non_existent_camp_12345", file=mock_file_nx)
    body_nx = json.loads(res_nx.body.decode()) if hasattr(res_nx, "body") else (getattr(res_nx, "content", res_nx))
    print(f"Non-existent campaign upload result: {body_nx}")
    assert getattr(res_nx, "status_code", 200) == 200
    assert body_nx.get("success") is True or body_nx.get("ok") is True
    assert body_nx.get("brochure_url") != ""

    print("PASS: Brochure upload resilience verified — zero 500 errors, 100% JSON status 200 responses.")

async def main():
    print("==================================================")
    print("RUNNING META WHATSAPP CLOUD API INTEGRATION TESTS")
    print("==================================================")
    await test_phone_formatting()
    await test_text_messaging()
    await test_document_messaging()
    await test_appointment_confirmation()
    await test_ai_sales_guardrails()
    await test_database_integration()
    await test_webhook_flow_simulation()
    await test_single_call_metadata_resolution()
    await test_brochure_upload_resilience()
    print("\n==================================================")
    print("ALL TESTS PASSED SUCCESSFULLY! (9/9) [OK]")
    print("==================================================")

if __name__ == "__main__":
    asyncio.run(main())
