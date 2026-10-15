"""Operator SMS pager transports: Twilio, carrier email gateway and console, with
recipient validation, log masking and GSM-7/UCS-2 segment counting. Sends are
fail-soft (``SendResult(ok=False)``, never a raise); secrets come from env only."""

import contextlib
import email.message
import math
import os
import re
import smtplib
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Literal, Protocol, TextIO

import httpx
import structlog

logger = structlog.get_logger(__name__)

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
_E164 = re.compile(r"\+[1-9][0-9]{7,14}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# GSM 03.38 basic alphabet; extension-table characters cost two septets each.
_GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM7_EXT = set("\f^{}\\[~]|€")
_RULE = "─" * 40
_OVERRIDE = None


def status_recipient() -> str:
    return os.environ.get("MAILROOM_STATUS_EMAIL", "").strip()


def _smtp_config() -> dict | None:
    host, user, pw = (os.environ.get(f"MAILROOM_SMTP_{k}", "").strip() for k in ("HOST", "USER", "PASSWORD"))
    if not (host and user and pw):
        return None
    return {"smtp_host": host, "smtp_port": int(os.environ.get("MAILROOM_SMTP_PORT") or 465),
            "address": user, "password": pw}


@dataclass(frozen=True)
class SendResult:
    ok: bool
    transport: str
    segments: int
    detail: str = ""


class Transport(Protocol):
    name: str

    def send(self, to: str, body: str) -> SendResult: ...


def set_transport_override(t: Transport | None) -> None:
    """Test seam: when set, ``build_transport`` returns it for every name."""
    global _OVERRIDE
    _OVERRIDE = t


def count_segments(body: str) -> tuple[Literal["GSM-7", "UCS-2"], int]:
    if all(c in _GSM7 or c in _GSM7_EXT for c in body):
        septets = sum(2 if c in _GSM7_EXT else 1 for c in body)
        return "GSM-7", 1 if septets <= 160 else math.ceil(septets / 153)
    units = len(body.encode("utf-16-le")) // 2  # an emoji is two UTF-16 units
    return "UCS-2", 1 if units <= 70 else math.ceil(units / 67)


def mask_number(s: str) -> str:
    if "@" in s:
        local, _, domain = s.partition("@")
        return f"{local[:2]}•••{local[-2:]}@{domain}" if len(local) > 4 else f"•••@{domain}"
    return f"{s[:2]}•••••{s[-4:]}" if len(s) > 6 else "•••"


def valid_recipients(raw: Iterable[str], transport: str) -> list[str]:
    pattern = _EMAIL if transport == "email_gateway" else _E164
    keep = []
    for entry in (str(e).strip() for e in raw):
        # Standing directive: the pager never emails MAILROOM_STATUS_EMAIL.
        inbox = bool(status_recipient()) and entry.lower() == status_recipient().lower()
        if pattern.fullmatch(entry) and not inbox:
            keep.append(entry)
        else:
            logger.warning("pager_recipient_invalid", transport=transport, to=mask_number(entry))
    return keep


def _failed(transport: str, to: str, segments: int, detail: str) -> SendResult:
    detail = detail.replace(to, mask_number(to)) if to else detail  # providers echo numbers
    logger.warning("pager_send_failed", transport=transport, to=mask_number(to), detail=detail)
    return SendResult(False, transport, segments, detail)


def _json(resp: httpx.Response) -> dict:
    try:
        return dict(resp.json())
    except Exception:  # noqa: BLE001 — an unparseable body is just an empty dict
        return {}


class TwilioTransport:
    name = "twilio"

    def __init__(self, sid: str, token: str, *, from_number: str | None,
                 service_sid: str | None, client: httpx.Client | None = None) -> None:
        self._sid, self._token = sid, token
        self._sender = {"MessagingServiceSid": service_sid} if service_sid else {"From": from_number or ""}
        self._client = client or httpx.Client()

    def send(self, to: str, body: str) -> SendResult:
        segments = count_segments(body)[1]
        try:
            resp = self._client.post(TWILIO_URL.format(sid=self._sid), timeout=10.0,
                                     data={"To": to, "Body": body, **self._sender},
                                     auth=(self._sid, self._token))
        except Exception as exc:  # noqa: BLE001 — fail-soft: a send never raises into the pager
            return _failed(self.name, to, segments, type(exc).__name__)
        data = _json(resp)
        if resp.status_code == 201:  # delivered: report success even without a parseable body
            return SendResult(True, self.name, segments, str(data.get("sid", "")))
        msg = data.get("message") or resp.text[:120]
        return _failed(self.name, to, segments, f"HTTP {resp.status_code}" + (f": {msg}" if msg else ""))


class EmailGatewayTransport:
    name = "email_gateway"

    def __init__(self, smtp_cfg: dict, smtp_factory=None) -> None:
        self._cfg = smtp_cfg
        self._factory = smtp_factory or partial(
            smtplib.SMTP_SSL, smtp_cfg["smtp_host"], smtp_cfg["smtp_port"], timeout=30)

    def send(self, to: str, body: str) -> SendResult:
        segments = count_segments(body)[1]
        if status_recipient() and to.lower() == status_recipient().lower():  # never the status inbox
            return SendResult(False, self.name, segments, "blocked: status inbox")
        client = None
        try:
            msg = email.message.EmailMessage()
            msg["From"], msg["To"], msg["Subject"] = self._cfg["address"], to, ""
            msg.set_content(body)
            client = self._factory()
            client.login(self._cfg["address"], self._cfg["password"])
            client.sendmail(self._cfg["address"], [to], msg.as_bytes())
            return SendResult(True, self.name, segments)
        except Exception as exc:  # noqa: BLE001 — fail-soft
            return _failed(self.name, to, segments, type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                client and client.quit()


class ConsoleTransport:
    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def send(self, to: str, body: str) -> SendResult:
        encoding, segments = count_segments(body)
        out = self._stream or sys.stdout
        print(f"{_RULE}\n{body}\n{_RULE}\n{encoding} · {segments} segments", file=out)
        return SendResult(True, self.name, segments)


def build_transport(name: str, env: Mapping[str, str] | None = None) -> Transport | None:
    """The transport called ``name``, or None with a ``pager_transport_unconfigured``
    warning naming the missing env vars when its credentials are absent."""
    if _OVERRIDE is not None:
        return _OVERRIDE
    env = os.environ if env is None else env
    if name == "console":
        return ConsoleTransport()
    if name == "email_gateway":
        cfg = _smtp_config()
        if cfg:
            return EmailGatewayTransport(cfg)
        missing = [f"MAILROOM_SMTP_{k}" for k in ("HOST", "USER", "PASSWORD") if not os.environ.get(f"MAILROOM_SMTP_{k}")]
    elif name == "twilio":
        missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") if not env.get(k)]
        if not (env.get("TWILIO_FROM_NUMBER") or env.get("TWILIO_MESSAGING_SERVICE_SID")):
            missing.append("TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID")
        if not missing:
            return TwilioTransport(env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"],
                                   from_number=env.get("TWILIO_FROM_NUMBER"),
                                   service_sid=env.get("TWILIO_MESSAGING_SERVICE_SID"))
    else:
        return None
    logger.warning("pager_transport_unconfigured", transport=name, missing=missing)
    return None
