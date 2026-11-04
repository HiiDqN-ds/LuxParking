"""SMS and WhatsApp delivery (Twilio REST API, standard library only).

Nothing is sent until TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN plus a sender are configured:
  SMS       TWILIO_SMS_FROM (or TWILIO_MESSAGING_SERVICE_SID)
  WhatsApp  TWILIO_WHATSAPP_FROM

Important for WhatsApp: a business may only start a conversation with an approved message template. Twilio rejects
free text outside a 24-hour customer window, which this module reports as a failed notification. Register templates
with Twilio/Meta before enabling WHATSAPP_EVENTS in production.
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app


class MessagingError(RuntimeError):
    """The message could not be delivered (reason is safe to store in the notification log)."""


class MessagingNotConfigured(MessagingError):
    """No provider credentials or sender: the message is skipped, not failed."""


def normalize_phone(raw: str) -> str | None:
    """Return an E.164 number (+4915112345678) or None when it cannot be determined safely."""
    text = re.sub(r"[\s().\-/]", "", raw or "")
    if text.startswith("00"):
        text = "+" + text[2:]
    if text.startswith("0"):
        prefix = (current_app.config.get("DEFAULT_PHONE_COUNTRY_CODE") or "").strip()
        if not re.fullmatch(r"\+\d{1,3}", prefix):
            return None
        text = prefix + text[1:]
    if re.fullmatch(r"\+[1-9]\d{6,14}", text):
        return text
    return None


def channel_configured(channel: str) -> bool:
    cfg = current_app.config
    if not (cfg.get("TWILIO_ACCOUNT_SID") and cfg.get("TWILIO_AUTH_TOKEN")):
        return False
    if channel == "sms":
        return bool(cfg.get("TWILIO_SMS_FROM") or cfg.get("TWILIO_MESSAGING_SERVICE_SID"))
    if channel == "whatsapp":
        return bool(cfg.get("TWILIO_WHATSAPP_FROM"))
    return False


def send_message(channel: str, to_number: str, body: str) -> str:
    """Send one SMS or WhatsApp message. Returns the provider message id. Raises MessagingError."""
    cfg = current_app.config
    if not channel_configured(channel):
        raise MessagingNotConfigured(f"{channel} is not configured")
    sid, token = cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"]
    fields = {"Body": body[:1500]}
    if channel == "whatsapp":
        fields["To"] = f"whatsapp:{to_number}"
        fields["From"] = f"whatsapp:{cfg['TWILIO_WHATSAPP_FROM']}"
    else:
        fields["To"] = to_number
        if cfg.get("TWILIO_MESSAGING_SERVICE_SID"):
            fields["MessagingServiceSid"] = cfg["TWILIO_MESSAGING_SERVICE_SID"]
        else:
            fields["From"] = cfg["TWILIO_SMS_FROM"]
    request = urllib.request.Request(
        f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json",
        data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Authorization": "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode()},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310  # nosec B310 - fixed https host (api.twilio.com)
            payload = json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("message", "")
        except Exception:
            pass
        raise MessagingError(f"Twilio rejected the message ({exc.code}) {detail}".strip()[:240]) from exc
    except (OSError, ValueError) as exc:
        raise MessagingError(f"Twilio could not be reached: {exc}"[:240]) from exc
    return str(payload.get("sid", ""))
