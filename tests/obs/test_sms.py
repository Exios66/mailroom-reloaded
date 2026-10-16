"""Operator SMS pager transports: Twilio, email-gateway and console transports,
recipient validation, masking and segment counting. Hermetic: Twilio runs
through httpx.MockTransport, email through an injected SMTP fake."""

import base64
import email
import io
import urllib.parse

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from mailroom_reloaded.obs import sms as sms_notify
from mailroom_reloaded.obs.sms import (
    ConsoleTransport,
    EmailGatewayTransport,
    SendResult,
    TwilioTransport,
    build_transport,
    count_segments,
    mask_number,
    set_transport_override,
    valid_recipients,
)

GATEWAY_CFG = {
    "address": "mailroom@example.com",
    "password": "app-pw",
    "smtp_host": "smtp.example.com",
    "smtp_port": 465,
}


class _FakeSMTP:
    def __init__(self):
        self.logins: list[str] = []
        self.sent: list[tuple[str, list[str], bytes]] = []

    def login(self, user, _password):
        self.logins.append(user)

    def sendmail(self, from_addr, to_addrs, payload):
        self.sent.append((from_addr, list(to_addrs), payload))

    def quit(self):
        pass


def _twilio(handler, *, from_number="+15550001111", service_sid=None):
    return TwilioTransport(
        "AC123",
        "tok-SECRET",
        from_number=from_number,
        service_sid=service_sid,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.fixture(autouse=True)
def _clean_override():
    set_transport_override(None)
    yield
    set_transport_override(None)


@pytest.fixture
def fresh_logger(monkeypatch):
    # cache_logger_on_first_use pins the module logger once setup_logging()
    # has run; bind a fresh proxy so capture_logs() sees every event.
    monkeypatch.setattr(sms_notify, "logger", structlog.get_logger(sms_notify.__name__))


def test_twilio_posts_form_and_reports_sid():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["form"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        seen["timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(201, json={"sid": "SM1"})

    res = _twilio(handler).send("+16085551234", "hello")
    assert res == SendResult(ok=True, transport="twilio", segments=1, detail="SM1")
    assert seen["url"] == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    assert seen["auth"] == "Basic " + base64.b64encode(b"AC123:tok-SECRET").decode()
    assert seen["form"] == {"To": "+16085551234", "Body": "hello", "From": "+15550001111"}
    assert seen["timeout"] == 10.0


def test_twilio_uses_messaging_service_when_set():
    seen = {}

    def handler(request):
        seen["form"] = dict(urllib.parse.parse_qsl(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM2"})

    assert _twilio(handler, from_number=None, service_sid="MG999").send("+16085551234", "hi").ok
    assert seen["form"]["MessagingServiceSid"] == "MG999"
    assert "From" not in seen["form"]


def test_twilio_error_and_timeout_are_soft():
    def rejected(request):
        return httpx.Response(400, json={"code": 21211, "message": "Invalid 'To' Phone Number"})

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    res = _twilio(rejected).send("+16085551234", "hi")
    assert res.ok is False and res.detail == "HTTP 400: Invalid 'To' Phone Number"
    res = _twilio(timeout).send("+16085551234", "hi")
    assert res.ok is False and res.detail == "ReadTimeout"


def test_twilio_scrubs_number_echoed_by_provider(fresh_logger):
    def rejected(request):
        return httpx.Response(400, json={"message": "The 'To' number +16085551234 is not valid"})

    with capture_logs() as logs:
        res = _twilio(rejected).send("+16085551234", "hi")
    assert res.detail == "HTTP 400: The 'To' number +1•••••1234 is not valid"
    assert "+16085551234" not in repr(res) + repr(logs)


def test_twilio_201_is_success_even_without_json():
    # A delivered text must never report failure, or the pager re-sends it.
    res = _twilio(lambda request: httpx.Response(201, text="oops")).send("+16085551234", "hi")
    assert res == SendResult(ok=True, transport="twilio", segments=1, detail="")


def test_secret_never_logged_or_returned(fresh_logger, capsys, caplog):
    def rejected(request):
        return httpx.Response(401, json={"message": "Authenticate"})

    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with capture_logs() as logs:
        results = [_twilio(h).send("+16085551234", "hi") for h in (rejected, timeout)]
    captured = capsys.readouterr()
    blob = repr(results) + repr(logs) + caplog.text + captured.out + captured.err
    assert "tok-SECRET" not in blob
    assert "+16085551234" not in blob  # numbers appear only masked
    assert [e["event"] for e in logs] == ["pager_send_failed", "pager_send_failed"]


def test_valid_recipients_mobile_and_google_voice():
    assert valid_recipients(["+16085551234", "+14145550000"], "twilio") == [
        "+16085551234",
        "+14145550000",
    ]
    assert valid_recipients(["608-555-1234", ""], "twilio") == []
    assert valid_recipients(["6085551234@vtext.com", "+16085551234"], "email_gateway") == [
        "6085551234@vtext.com"
    ]
    assert valid_recipients(["+16085551234"], "console") == ["+16085551234"]


def test_invalid_recipients_warn_masked(fresh_logger):
    with capture_logs() as logs:
        assert valid_recipients(["608-555-1234", "+16085551234"], "twilio") == ["+16085551234"]
    assert [(e["event"], e["log_level"]) for e in logs] == [("pager_recipient_invalid", "warning")]
    assert "608-555-1234" not in repr(logs)


def test_email_gateway_never_mails_status_inbox(monkeypatch):
    monkeypatch.setenv("MAILROOM_STATUS_EMAIL", "human@example.com")
    assert valid_recipients(["Human@Example.com", "6085551234@vtext.com"], "email_gateway") == [
        "6085551234@vtext.com"
    ]
    smtp = _FakeSMTP()
    res = EmailGatewayTransport(GATEWAY_CFG, smtp_factory=lambda: smtp).send(
        "human@example.com", "hi"
    )
    assert res.ok is False and smtp.sent == []


def test_count_segments():
    assert count_segments("a" * 160) == ("GSM-7", 1)
    assert count_segments("a" * 161) == ("GSM-7", 2)
    assert count_segments("🟢" + "a" * 68) == ("UCS-2", 1)
    assert count_segments("🟢" + "a" * 69) == ("UCS-2", 2)


def test_count_segments_extension_multipart_and_empty():
    assert count_segments("€" * 80) == ("GSM-7", 1)  # extension characters cost two septets
    assert count_segments("€" * 81) == ("GSM-7", 2)
    assert count_segments("a" * 306) == ("GSM-7", 2)
    assert count_segments("a" * 307) == ("GSM-7", 3)
    assert count_segments("a—b") == ("UCS-2", 1)  # em dash is outside GSM-7
    assert count_segments("") == ("GSM-7", 1)


def test_mask_number():
    assert mask_number("+16085551234") == "+1•••••1234"
    assert mask_number("6085551234@vtext.com") == "60•••34@vtext.com"


def test_build_transport_missing_creds_returns_none(fresh_logger):
    with capture_logs() as logs:
        assert build_transport("twilio", {}) is None
    [event] = logs
    assert event["event"] == "pager_transport_unconfigured" and event["log_level"] == "warning"
    assert {"TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"} <= set(event["missing"])


def test_build_transport_builds_configured_transports(monkeypatch):
    twilio_env = {
        "TWILIO_ACCOUNT_SID": "AC123",
        "TWILIO_AUTH_TOKEN": "tok-SECRET",
        "TWILIO_MESSAGING_SERVICE_SID": "MG999",
    }
    assert isinstance(build_transport("twilio", twilio_env), TwilioTransport)
    assert isinstance(build_transport("console", {}), ConsoleTransport)
    monkeypatch.setattr(sms_notify, "_smtp_config", lambda: GATEWAY_CFG)
    assert isinstance(build_transport("email_gateway", {}), EmailGatewayTransport)
    monkeypatch.setattr(sms_notify, "_smtp_config", lambda: None)
    assert build_transport("email_gateway", {}) is None


def test_override_wins():
    fake = ConsoleTransport(stream=io.StringIO())
    set_transport_override(fake)
    assert build_transport("twilio", {}) is fake
    set_transport_override(None)
    assert build_transport("twilio", {}) is None


def test_email_gateway_sends_plain_text_with_empty_subject():
    smtp = _FakeSMTP()
    res = EmailGatewayTransport(GATEWAY_CFG, smtp_factory=lambda: smtp).send(
        "6085551234@vtext.com", "Mailroom LIVE"
    )
    assert res == SendResult(ok=True, transport="email_gateway", segments=1, detail="")
    from_addr, to_addrs, payload = smtp.sent[0]
    msg = email.message_from_bytes(payload)
    assert from_addr == "mailroom@example.com" and smtp.logins == ["mailroom@example.com"]
    assert to_addrs == ["6085551234@vtext.com"] and msg["To"] == "6085551234@vtext.com"
    assert msg["Subject"] == "" and msg.get_content_type() == "text/plain"
    assert msg.get_payload(decode=True).decode().strip() == "Mailroom LIVE"


def test_email_gateway_failure_is_soft(fresh_logger):
    class Down(_FakeSMTP):
        def sendmail(self, *args):
            raise OSError("connection refused")

    with capture_logs() as logs:
        res = EmailGatewayTransport(GATEWAY_CFG, smtp_factory=Down).send(
            "6085551234@vtext.com", "hi"
        )
    assert res == SendResult(ok=False, transport="email_gateway", segments=1, detail="OSError")
    assert [e["event"] for e in logs] == ["pager_send_failed"]
    assert "6085551234" not in repr(logs)


def test_console_frames_body_and_reports_segments():
    buf = io.StringIO()
    res = ConsoleTransport(stream=buf).send("+16085551234", "🟢 All clear — no action needed.")
    out = buf.getvalue()
    assert res == SendResult(ok=True, transport="console", segments=1, detail="")
    assert out.startswith("─" * 10) and "🟢 All clear — no action needed." in out
    assert out.rstrip().endswith("UCS-2 · 1 segments")


def test_taxonomy_has_pager_block():
    from mailroom_reloaded.settings import load_taxonomy

    pager = load_taxonomy().raw["pager"]
    assert {"digest", "scan", "alerts", "quiet_hours", "rules", "providers", "machine"} <= set(pager)
    assert pager["enabled"] is False and pager["recipients"] == [] and pager["transport"] == "twilio"


def test_email_gateway_unavailable_without_smtp_env(monkeypatch):
    for k in ("MAILROOM_SMTP_HOST", "MAILROOM_SMTP_USER", "MAILROOM_SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert sms_notify._smtp_config() is None
    monkeypatch.setenv("MAILROOM_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("MAILROOM_SMTP_USER", "ops@example.com")
    monkeypatch.setenv("MAILROOM_SMTP_PASSWORD", "pw")
    assert sms_notify._smtp_config() == {"smtp_host": "smtp.example.com", "smtp_port": 465,
                                         "address": "ops@example.com", "password": "pw"}
