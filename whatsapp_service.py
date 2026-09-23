"""
Meta WhatsApp Cloud API Service for Kaamdhenu Real Estate AI.
Handles pure Meta Cloud API messaging (text, documents/brochures, appointment confirmations)
and Gemini AI Real Estate Sales Inbound Agent.
"""

import os
import re
import time
import json
import logging
import asyncio
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, Tuple

try:
    import httpx
except ImportError:
    httpx = None

from db import insert_whatsapp_log, push_unified_log

logger = logging.getLogger("whatsapp-service")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "kaamdhenu_whatsapp_verify_token")
META_GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v20.0")


def _sync_http_post(url: str, headers: dict, json_payload: dict, timeout: float = 10.0) -> Tuple[int, dict]:
    try:
        data = json.dumps(json_payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8")
            return code, json.loads(body) if body else {}
    except urllib.error.HTTPError as he:
        body = he.read().decode("utf-8", errors="replace")
        try:
            return he.code, json.loads(body)
        except Exception:
            return he.code, {"error": {"message": body}}
    except Exception as e:
        return 500, {"error": {"message": str(e)}}


async def _post_json(url: str, headers: Optional[dict], json_payload: dict, timeout: float = 10.0) -> Tuple[int, dict]:
    hdrs = headers or {"Content-Type": "application/json"}
    if httpx is not None:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=hdrs, json=json_payload)
                return resp.status_code, resp.json()
        except Exception as e:
            if hasattr(e, "response") and getattr(e, "response") is not None:
                try:
                    return e.response.status_code, e.response.json()
                except Exception:
                    pass
            logger.error(f"HTTP request error: {e}")
            return 500, {"error": {"message": str(e)}}
    else:
        return await asyncio.to_thread(_sync_http_post, url, hdrs, json_payload, timeout)


def format_whatsapp_phone(phone: str) -> str:
    """
    Format phone number to international format required by Meta Cloud API (digits only, no +).
    Defaults to India prefix (91) for 10-digit numbers.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 10:
        return f"91{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"91{digits[1:]}"
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    return digits


def get_messages_url() -> str:
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", WHATSAPP_PHONE_NUMBER_ID)
    version = os.getenv("META_GRAPH_API_VERSION", META_GRAPH_VERSION)
    return f"https://graph.facebook.com/{version}/{phone_id}/messages"


async def send_text_message(
    to_phone: str,
    text: str,
    campaign_id: Optional[str] = None,
    call_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Send a plain text message to a WhatsApp user using Meta Cloud API.
    """
    clean_to = format_whatsapp_phone(to_phone)
    if not clean_to:
        logger.warning("send_text_message aborted: empty phone number")
        return {"success": False, "error": "Invalid phone number"}

    token = os.getenv("WHATSAPP_TOKEN", WHATSAPP_TOKEN)
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", WHATSAPP_PHONE_NUMBER_ID)

    # Simulated mode if credentials are missing
    if not (token and phone_id):
        logger.info(f"[SIMULATED WHATSAPP] To: {clean_to} | Msg: {text[:80]}...")
        sim_id = f"sim_wamid_{int(time.time())}"
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=text,
            status="simulated",
            call_id=call_id,
            direction="outbound",
            message_type="text",
            campaign_id=campaign_id
        )
        await push_unified_log(
            "WhatsApp", "info", f"💬 [Demo/Simulated] WhatsApp text sent to {clean_to}", call_id=call_id
        )
        return {"success": True, "simulated": True, "message_id": sim_id}

    url = get_messages_url()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean_to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    }

    try:
        status_code, resp_data = await _post_json(url, headers=headers, json_payload=payload, timeout=10.0)

        if status_code in (200, 201):
            msg_id = ""
            messages = resp_data.get("messages", [])
            if messages:
                msg_id = messages[0].get("id", "")
            await insert_whatsapp_log(
                phone_number=clean_to,
                message=text,
                status="sent",
                call_id=call_id,
                direction="outbound",
                message_type="text",
                campaign_id=campaign_id
            )
            await push_unified_log(
                "WhatsApp", "info", f"✅ WhatsApp message delivered to {clean_to} (ID: {msg_id})", call_id=call_id
            )
            return {"success": True, "message_id": msg_id, "data": resp_data}
        else:
            err_msg = resp_data.get("error", {}).get("message", json.dumps(resp_data))
            logger.error(f"Meta WhatsApp API Error ({status_code}): {err_msg}")
            await insert_whatsapp_log(
                phone_number=clean_to,
                message=text,
                status=f"failed: {err_msg}",
                call_id=call_id,
                direction="outbound",
                message_type="text",
                campaign_id=campaign_id
            )
            await push_unified_log(
                "WhatsApp", "error", f"❌ WhatsApp delivery failed to {clean_to}: {err_msg}", call_id=call_id
            )
            return {"success": False, "error": err_msg, "status_code": status_code}
    except Exception as e:
        logger.error(f"Error calling Meta WhatsApp API: {e}")
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=text,
            status=f"exception: {e}",
            call_id=call_id,
            direction="outbound",
            message_type="text",
            campaign_id=campaign_id
        )
        return {"success": False, "error": str(e)}


async def send_project_brochure(
    to_phone: str,
    document_url: str,
    lead_name: str = "",
    project_name: str = "",
    filename: Optional[str] = None,
    caption: str = "",
    campaign_id: Optional[str] = None,
    call_id: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Send the project brochure PDF using Meta's official approved template: 'project_brochure_share'.
    Enables sending brochures autonomously to leads without 24-hour window restrictions.

    Includes automatic fallback retry for Meta Error #132001:
      1. Try: project_brochure_share + en_US
      2. Retry: project_brochure_share + en
      3. Retry: project_brochure_shar  + en_US
      4. Retry: project_brochure_shar  + en

    Meta Template Configuration:
      - Template Name: 'project_brochure_share' (approved name in Meta Business Manager)
      - Language: 'en_US' (primary), 'en' (fallback)
      - Header: document (Dynamic PDF link from campaign / single call context)
      - Body Parameters:
          {{1}}: Lead / Customer Name (Default: "Valued Client")
          {{2}}: Project / Business Name (Default: session's business_name or "Provident Palm Vista")
    """
    clean_to = format_whatsapp_phone(to_phone)
    if not clean_to:
        logger.warning("send_project_brochure aborted: empty phone number")
        return {"success": False, "error": "Invalid phone number"}

    if not document_url:
        logger.warning("send_project_brochure aborted: empty document_url")
        return {"success": False, "error": "Missing brochure document URL"}

    # Dynamic fallback defaults as specified in requirements
    raw_lead = (lead_name or kwargs.get("name") or kwargs.get("client_name") or "").strip()
    if not raw_lead and caption:
        m_name = re.search(r'Namaste\s+([^!]+)!', caption)
        if m_name:
            raw_lead = m_name.group(1).strip()
    if not raw_lead or raw_lead.lower() in ("there", "caller", "unknown", "none", "null", "undefined", "lead"):
        final_lead_name = "Valued Client"
    else:
        final_lead_name = raw_lead

    raw_project = (project_name or kwargs.get("business_name") or "").strip()
    if not raw_project and caption and "—" in caption:
        raw_project = caption.split("—")[-1].strip()
    if not raw_project and filename and filename.endswith("_Brochure.pdf"):
        raw_project = filename.replace("_Brochure.pdf", "").replace("_", " ").strip()
    if not raw_project or raw_project.lower() in ("none", "null", "undefined"):
        final_project_name = os.getenv("DEFAULT_PROJECT_NAME") or os.getenv("BUSINESS_NAME") or "Provident Palm Vista"
    else:
        final_project_name = raw_project

    clean_pname = re.sub(r'[^\w\s-]', '', final_project_name).strip()
    doc_filename = filename.strip() if filename and filename.strip() else f"{clean_pname.replace(' ', '_')}_Brochure.pdf"

    token = os.getenv("WHATSAPP_TOKEN", WHATSAPP_TOKEN)
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", WHATSAPP_PHONE_NUMBER_ID)

    # Primary template name and language (corrected from truncated 'shar' to full 'share')
    primary_name = (kwargs.get("template_name") or os.getenv("WHATSAPP_TEMPLATE_NAME") or "project_brochure_share").strip()
    primary_lang = (kwargs.get("template_lang") or os.getenv("WHATSAPP_TEMPLATE_LANG") or "en_US").strip()

    # Simulated mode if credentials are not configured
    if not (token and phone_id):
        logger.info(f"[SIMULATED WHATSAPP TEMPLATE] To: {clean_to} | Template: {primary_name} | Lang: {primary_lang} | File: {doc_filename} | URL: {document_url}")
        sim_id = f"sim_tmpl_{int(time.time())}"
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=f"[TEMPLATE: {primary_name}] {doc_filename} -> {document_url} | Lead: {final_lead_name} | Project: {final_project_name}",
            status="simulated",
            call_id=call_id,
            direction="outbound",
            message_type="template",
            campaign_id=campaign_id
        )
        await push_unified_log(
            "WhatsApp", "info", f"📄 [Demo/Simulated] Meta template '{primary_name}' ({primary_lang}) sent to {clean_to} ({doc_filename})", call_id=call_id
        )
        return {"success": True, "simulated": True, "message_id": sim_id}

    url = get_messages_url()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    def _build_template_payload(tmpl_name: str, lang_code: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_to,
            "type": "template",
            "template": {
                "name": tmpl_name,
                "language": {
                    "code": lang_code
                },
                "components": [
                    {
                        "type": "header",
                        "parameters": [
                            {
                                "type": "document",
                                "document": {
                                    "link": document_url,
                                    "filename": doc_filename
                                }
                            }
                        ]
                    },
                    {
                        "type": "body",
                        "parameters": [
                            {
                                "type": "text",
                                "text": final_lead_name
                            },
                            {
                                "type": "text",
                                "text": final_project_name
                            }
                        ]
                    }
                ]
            }
        }

    def _is_132001_error(code: int, data: dict) -> bool:
        """Check if the response is Meta Error #132001 (template name/translation mismatch)."""
        if code in (200, 201):
            return False
        err = data.get("error", {})
        err_code = err.get("code", 0)
        err_subcode = err.get("error_subcode", 0)
        err_msg = err.get("message", "").lower()
        return (
            err_code == 132001 or err_subcode == 132001
            or "does not exist" in err_msg
            or "132001" in str(err_code) or "132001" in str(err_subcode)
        )

    # Fallback chain for Meta Error #132001:
    # 1. project_brochure_share + en_US  (most likely correct)
    # 2. project_brochure_share + en     (if en_US not matching)
    # 3. project_brochure_shar  + en_US  (if name was actually truncated in Meta)
    # 4. project_brochure_shar  + en     (last resort)
    fallback_chain = [
        (primary_name, primary_lang),
    ]
    # Add smart fallbacks (avoid duplicates)
    for name, lang in [
        ("project_brochure_share", "en"),
        ("project_brochure_shar", "en_US"),
        ("project_brochure_shar", "en"),
        ("project_brochure_share", "en_US"),
    ]:
        if (name, lang) not in fallback_chain:
            fallback_chain.append((name, lang))

    last_err_msg = ""
    last_status_code = 0

    try:
        for attempt_idx, (tmpl_name, lang_code) in enumerate(fallback_chain):
            payload = _build_template_payload(tmpl_name, lang_code)
            status_code, resp_data = await _post_json(url, headers=headers, json_payload=payload, timeout=12.0)
            last_status_code = status_code

            if status_code in (200, 201):
                msg_id = ""
                messages = resp_data.get("messages", [])
                if messages:
                    msg_id = messages[0].get("id", "")
                used_label = f"{tmpl_name}/{lang_code}"
                await insert_whatsapp_log(
                    phone_number=clean_to,
                    message=f"[TEMPLATE: {used_label}] {doc_filename} -> {document_url} | Lead: {final_lead_name} | Project: {final_project_name}",
                    status="sent",
                    call_id=call_id,
                    direction="outbound",
                    message_type="template",
                    campaign_id=campaign_id
                )
                if attempt_idx > 0:
                    logger.info(f"Meta template succeeded on fallback attempt #{attempt_idx + 1}: {used_label}")
                await push_unified_log(
                    "WhatsApp", "info", f"📄 Meta template brochure delivered to {clean_to} (ID: {msg_id}, tpl: {used_label})", call_id=call_id
                )
                return {"success": True, "message_id": msg_id, "data": resp_data, "template_used": used_label}

            # Check if this is a 132001 error — if so, try the next fallback
            if _is_132001_error(status_code, resp_data) and attempt_idx < len(fallback_chain) - 1:
                err_detail = resp_data.get("error", {}).get("message", "")
                logger.warning(f"Meta Error #132001 with {tmpl_name}/{lang_code}: {err_detail} — retrying with next fallback...")
                continue

            # Non-132001 error or last fallback exhausted — break and report
            last_err_msg = resp_data.get("error", {}).get("message", json.dumps(resp_data))
            break

        # All attempts exhausted or non-retryable error
        if not last_err_msg:
            last_err_msg = "All template name/language fallback combinations failed (Error #132001)"
        logger.error(f"Meta WhatsApp API Error (template): {last_err_msg}")
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=f"[TEMPLATE FAILED] {doc_filename} -> {document_url}",
            status=f"failed: {last_err_msg}",
            call_id=call_id,
            direction="outbound",
            message_type="template",
            campaign_id=campaign_id
        )
        await push_unified_log(
            "WhatsApp", "error", f"❌ Meta template brochure delivery failed to {clean_to}: {last_err_msg}", call_id=call_id
        )
        return {"success": False, "error": last_err_msg, "status_code": last_status_code}
    except Exception as e:
        logger.error(f"Error calling Meta WhatsApp API (template): {e}")
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=f"[TEMPLATE: {primary_name}] {doc_filename} -> {document_url}",
            status=f"exception: {e}",
            call_id=call_id,
            direction="outbound",
            message_type="template",
            campaign_id=campaign_id
        )
        return {"success": False, "error": str(e)}


# Backward-compatible aliases for all callers
send_brochure = send_project_brochure
send_document_message = send_project_brochure


async def send_site_visit_confirmation(
    to_phone: str,
    customer_name: str = "",
    project_name: str = "",
    visit_time: str = "",
    cab_details: str = "",
    campaign_id: Optional[str] = None,
    call_id: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Send site visit / appointment confirmation using Meta's approved Utility template:
    'site_visit_confirmation'.

    Bypasses the 24-hour customer window restriction since Utility templates are
    pre-approved by Meta for transactional/confirmation messages.

    Includes automatic fallback retry for Meta Error #132001:
      1. Try: site_visit_confirmation + en_US
      2. Retry: site_visit_confirmation + en

    Template Body Parameters:
      {{1}}: customer_name (default: "Valued Client")
      {{2}}: project_name (e.g. "Provident Palm Vista")
      {{3}}: visit_time (e.g. "2026-09-25 11:00 AM")
      {{4}}: cab_details (e.g. "Cab pickup from Thane Station" or "Direct Site Visit")
    """
    clean_to = format_whatsapp_phone(to_phone)
    if not clean_to:
        logger.warning("send_site_visit_confirmation aborted: empty phone number")
        return {"success": False, "error": "Invalid phone number"}

    # Resolve parameter defaults
    final_name = (customer_name or kwargs.get("name") or kwargs.get("client_name") or "").strip()
    if not final_name or final_name.lower() in ("there", "caller", "unknown", "none", "null", "undefined", "lead"):
        final_name = "Valued Client"

    final_project = (project_name or kwargs.get("business_name") or "").strip()
    if not final_project or final_project.lower() in ("none", "null", "undefined"):
        final_project = os.getenv("DEFAULT_PROJECT_NAME") or os.getenv("BUSINESS_NAME") or "Kaamdhenu Premium Residences"

    final_visit_time = (visit_time or kwargs.get("date_time") or kwargs.get("visit_datetime") or "").strip()
    if not final_visit_time:
        final_visit_time = "To be confirmed"

    final_cab = (cab_details or kwargs.get("pickup_info") or kwargs.get("pickup_address") or "").strip()
    if not final_cab:
        final_cab = "Direct Site Visit / Self-Arranged"

    token = os.getenv("WHATSAPP_TOKEN", WHATSAPP_TOKEN)
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", WHATSAPP_PHONE_NUMBER_ID)

    primary_name = (kwargs.get("template_name") or os.getenv("WHATSAPP_SITE_VISIT_TEMPLATE") or "site_visit_confirmation").strip()
    primary_lang = (kwargs.get("template_lang") or os.getenv("WHATSAPP_SITE_VISIT_TEMPLATE_LANG") or "en_US").strip()

    # Simulated mode if credentials are not configured
    if not (token and phone_id):
        logger.info(
            f"[SIMULATED SITE VISIT TEMPLATE] To: {clean_to} | Template: {primary_name} | "
            f"Name: {final_name} | Project: {final_project} | Time: {final_visit_time} | Cab: {final_cab}"
        )
        sim_id = f"sim_sv_{int(time.time())}"
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=f"[TEMPLATE: {primary_name}] Site Visit: {final_name} @ {final_project} on {final_visit_time} | Cab: {final_cab}",
            status="simulated",
            call_id=call_id,
            direction="outbound",
            message_type="template",
            campaign_id=campaign_id
        )
        await push_unified_log(
            "WhatsApp", "info",
            f"📍 [Simulated] Site visit confirmation template sent to {clean_to} ({final_project}, {final_visit_time})",
            call_id=call_id
        )
        return {"success": True, "simulated": True, "message_id": sim_id}

    url = get_messages_url()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    def _build_sv_payload(tmpl_name: str, lang_code: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": clean_to,
            "type": "template",
            "template": {
                "name": tmpl_name,
                "language": {
                    "code": lang_code
                },
                "components": [
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": final_name},
                            {"type": "text", "text": final_project},
                            {"type": "text", "text": final_visit_time},
                            {"type": "text", "text": final_cab}
                        ]
                    }
                ]
            }
        }

    def _is_132001_error(code: int, data: dict) -> bool:
        if code in (200, 201):
            return False
        err = data.get("error", {})
        err_code = err.get("code", 0)
        err_subcode = err.get("error_subcode", 0)
        err_msg = err.get("message", "").lower()
        return (
            err_code == 132001 or err_subcode == 132001
            or "does not exist" in err_msg
        )

    # Fallback chain: en_US → en (avoid duplicates)
    fallback_chain = [(primary_name, primary_lang)]
    for name, lang in [
        (primary_name, "en"),
        (primary_name, "en_US"),
    ]:
        if (name, lang) not in fallback_chain:
            fallback_chain.append((name, lang))

    last_err_msg = ""
    last_status_code = 0

    try:
        for attempt_idx, (tmpl_name, lang_code) in enumerate(fallback_chain):
            payload = _build_sv_payload(tmpl_name, lang_code)
            status_code, resp_data = await _post_json(url, headers=headers, json_payload=payload, timeout=12.0)
            last_status_code = status_code

            if status_code in (200, 201):
                msg_id = ""
                messages = resp_data.get("messages", [])
                if messages:
                    msg_id = messages[0].get("id", "")
                used_label = f"{tmpl_name}/{lang_code}"
                await insert_whatsapp_log(
                    phone_number=clean_to,
                    message=f"[TEMPLATE: {used_label}] Site Visit: {final_name} @ {final_project} on {final_visit_time}",
                    status="sent",
                    call_id=call_id,
                    direction="outbound",
                    message_type="template",
                    campaign_id=campaign_id
                )
                if attempt_idx > 0:
                    logger.info(f"Site visit template succeeded on fallback attempt #{attempt_idx + 1}: {used_label}")
                await push_unified_log(
                    "WhatsApp", "info",
                    f"📍 Site visit confirmation delivered to {clean_to} (ID: {msg_id}, tpl: {used_label})",
                    call_id=call_id
                )
                return {"success": True, "message_id": msg_id, "data": resp_data, "template_used": used_label}

            if _is_132001_error(status_code, resp_data) and attempt_idx < len(fallback_chain) - 1:
                err_detail = resp_data.get("error", {}).get("message", "")
                logger.warning(f"Meta Error #132001 with {tmpl_name}/{lang_code}: {err_detail} — retrying...")
                continue

            last_err_msg = resp_data.get("error", {}).get("message", json.dumps(resp_data))
            break

        if not last_err_msg:
            last_err_msg = "All site_visit_confirmation template fallbacks failed (Error #132001)"
        logger.error(f"Meta WhatsApp API Error (site visit template): {last_err_msg}")
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=f"[TEMPLATE FAILED: {primary_name}] Site Visit: {final_name} @ {final_project}",
            status=f"failed: {last_err_msg}",
            call_id=call_id,
            direction="outbound",
            message_type="template",
            campaign_id=campaign_id
        )
        await push_unified_log(
            "WhatsApp", "error",
            f"❌ Site visit confirmation failed to {clean_to}: {last_err_msg}",
            call_id=call_id
        )
        return {"success": False, "error": last_err_msg, "status_code": last_status_code}
    except Exception as e:
        logger.error(f"Error calling Meta WhatsApp API (site visit template): {e}")
        await insert_whatsapp_log(
            phone_number=clean_to,
            message=f"[TEMPLATE: {primary_name}] Site Visit: {final_name} @ {final_project}",
            status=f"exception: {e}",
            call_id=call_id,
            direction="outbound",
            message_type="template",
            campaign_id=campaign_id
        )
        return {"success": False, "error": str(e)}


async def send_appointment_confirmation(
    to_phone: str,
    lead_data: dict,
    appointment_data: dict,
    campaign_id: Optional[str] = None,
    call_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Formats and sends a structured site visit confirmation template/message with date, time,
    site address, and pickup/drop confirmation. If a brochure is attached to the campaign,
    it also automatically attaches the PDF brochure.
    """
    clean_to = format_whatsapp_phone(to_phone)
    lead_name = (
        lead_data.get("name")
        or lead_data.get("lead_name")
        or lead_data.get("client_name")
        or "Valued Client"
    )
    project_name = (
        appointment_data.get("project_name")
        or lead_data.get("project_name")
        or lead_data.get("business_name")
        or "Kaamdhenu Premium Residences"
    )
    site_address = (
        appointment_data.get("site_address")
        or lead_data.get("site_address")
        or "Near City Center, Metro Station Access, Mumbai MMR"
    )
    date = appointment_data.get("date", "Upcoming Weekend")
    time_slot = appointment_data.get("time", "11:00 AM")
    pickup_req = appointment_data.get("pickup_required", False)
    pickup_addr = appointment_data.get("pickup_address", "")

    if pickup_req and pickup_addr:
        pickup_info = f"✅ Arranged (Pickup from: {pickup_addr})"
    elif pickup_req:
        pickup_info = "✅ Complimentary cab pickup requested (our team will confirm pickup point)"
    else:
        pickup_info = "🚗 Self-Drive / Direct Visit (Free visitor parking reserved)"

    highlights = (
        appointment_data.get("project_highlights")
        or lead_data.get("project_highlights")
        or "• Premium 2BHK & 3BHK Air-Conditioned Homes\n• 30+ World-Class Lifestyle Amenities & Grand Clubhouse\n• Zero Stamp Duty & Flexible 20:80 Payment Plan"
    )

    msg_body = (
        f"🏡 *SITE VISIT CONFIRMED — {project_name.upper()}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Namaste *{lead_name}* ji! 🙏\n"
        f"Your private site visit and show flat walkthrough is officially confirmed.\n\n"
        f"📅 *Date:* {date}\n"
        f"⏰ *Time:* {time_slot}\n"
        f"📍 *Site Address:* {site_address}\n"
        f"🚘 *Cab / Transportation:* {pickup_info}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✨ *Project Highlights:*\n"
        f"{highlights}\n\n"
        f"Our senior property consultant will receive you at the site reception. "
        f"If you have any questions or need to modify your visit timing, simply reply to this message directly.\n\n"
        f"Looking forward to hosting you!\n"
        f"— *Kaamdhenu Real Estate Advisory*"
    )

    # 1. Send confirmation via approved Meta Utility template (bypasses 24h window)
    visit_time_str = f"{date} {time_slot}"
    res = await send_site_visit_confirmation(
        to_phone=clean_to,
        customer_name=lead_name,
        project_name=project_name,
        visit_time=visit_time_str,
        cab_details=pickup_info,
        campaign_id=campaign_id,
        call_id=call_id
    )

    # If template fails (e.g. not approved yet), fallback to plain text within 24h window
    if not res.get("success") and not res.get("simulated"):
        logger.warning(f"Site visit template failed, falling back to plain text: {res.get('error')}")
        res = await send_text_message(
            to_phone=clean_to,
            text=msg_body,
            campaign_id=campaign_id,
            call_id=call_id
        )

    # 2. If brochure_url is present, send the PDF document as a follow-up via approved Meta template
    brochure_url = appointment_data.get("brochure_url") or lead_data.get("brochure_url")
    if brochure_url:
        try:
            await send_project_brochure(
                to_phone=clean_to,
                document_url=brochure_url,
                lead_name=lead_name,
                project_name=project_name,
                filename=f"{project_name.replace(' ', '_')}_Brochure.pdf",
                campaign_id=campaign_id,
                call_id=call_id
            )
        except Exception as e:
            logger.warning(f"Follow-up brochure send error: {e}")

    return res


async def generate_whatsapp_ai_response(
    incoming_text: str,
    campaign_context: dict,
    lead_context: dict
) -> Tuple[str, bool]:
    """
    Generate an intelligent, strictly guardrailed real estate response for inbound WhatsApp messages using Gemini.
    CRITICAL GUARDRAIL:
    - Act strictly as a Real Estate Sales Executive for THIS specific project.
    - Discuss only unit configurations, pricing range, amenities, location, and site visits.
    - Deflect unrelated questions politely back to the project.
    - Detect if the user wants the brochure.

    Returns:
      (reply_text: str, wants_brochure: bool)
    """
    text = (incoming_text or "").strip()
    text_lower = text.lower()

    # Fast heuristic for brochure request
    brochure_triggers = [
        "brochure", "floor plan", "floorplan", "pdf", "photos", "photo", "details",
        "pricing", "price list", "quotation", "rate", "cost", "layout", "bhejo", "send", "share"
    ]
    wants_brochure = any(t in text_lower for t in brochure_triggers)

    project_name = (
        campaign_context.get("project_name")
        or campaign_context.get("name")
        or "Kaamdhenu Heights"
    )
    site_address = (
        campaign_context.get("site_address")
        or "Near City Center, Prime Metro Corridor"
    )
    highlights = (
        campaign_context.get("project_highlights")
        or "Premium 2 & 3 BHK residences starting at ₹75 Lakhs with 30+ modern lifestyle amenities, swimming pool, and grand clubhouse."
    )
    lead_name = (
        lead_context.get("lead_name")
        or lead_context.get("client_name")
        or "there"
    )

    google_api_key = os.getenv("GOOGLE_API_KEY", "")
    if not google_api_key:
        # Fallback response
        if wants_brochure:
            return (
                f"Namaste {lead_name}! Sure, here is the official project brochure and floor plans for {project_name}. "
                f"Would you like to schedule a site visit this weekend to see the sample flat?",
                True
            )
        return (
            f"Namaste {lead_name}! Thank you for reaching out regarding {project_name}. "
            f"We offer spacious luxury apartments at {site_address}. How can I assist you with the configurations or booking a site visit?",
            False
        )

    system_prompt = f"""You are a Senior Real Estate Sales Consultant representing the prestigious project: "{project_name}".

PROJECT INFORMATION:
- Project Name: {project_name}
- Site Location: {site_address}
- Key Highlights & Pricing: {highlights}
- Lead Name: {lead_name}

CRITICAL SALES GUARDRAILS (STRICT RULES):
1. REAL ESTATE FOCUS ONLY: You must strictly represent and discuss THIS specific property ({project_name}). Answer questions regarding BHK configurations, price range, amenities, location advantages, possession timeline, and site visits.
2. DEFLECTION RULE: If the user asks general trivia, politics, personal questions, or anything unrelated to real estate or this project, politely deflect in ONE courteous sentence and immediately pivot back to how you can help them with their dream home in {project_name}.
3. CALL TO ACTION: Encourage booking a free site visit (we offer complimentary pickup/drop cabs) or asking for the brochure.
4. TONE & STYLE: Keep your response concise (maximum 2 to 4 sentences), professional, warm, and WhatsApp-friendly with emojis. Do NOT write long essays.
5. LANGUAGE: Mirror the language of the lead naturally (Hindi, English, or Hinglish).
"""

    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={google_api_key}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"System Context:\n{system_prompt}\n\nIncoming WhatsApp Message from {lead_name}:\n\"{text}\"\n\nGenerate your concise WhatsApp sales reply:"}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 250
        }
    }

    try:
        status_code, data = await _post_json(gemini_url, headers={"Content-Type": "application/json"}, json_payload=payload, timeout=8.0)
        if status_code == 200:
            candidates = data.get("candidates", [])
            if candidates:
                reply = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
                if reply:
                    return (reply, wants_brochure)
    except Exception as e:
        logger.warning(f"Gemini WhatsApp response generation error: {e}")

    # Fallback
    return (
        f"Namaste {lead_name}! Thank you for your interest in {project_name}. "
        f"We have exclusive 2BHK & 3BHK units available. Would you like me to share the complete brochure or arrange a site visit with free cab pickup?",
        wants_brochure
    )
