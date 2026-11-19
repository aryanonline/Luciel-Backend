"""The local inbox: platform email over plain SMTP on a developer's own computer.

The Docker stack points ``LUCIEL_SMTP_HOST`` at Mailpit so sign-up verification,
password resets and hand-off pages can be read and clicked at http://localhost:8025.
Mock mode + development only; live mode keeps SES and the suite keeps the Null sender.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Iterator
from email import message_from_bytes, policy

import pytest
from app.config import Settings, get_settings
from app.notifications.email_sender import (
    NullEmailSender,
    SesEmailSender,
    SmtpEmailSender,
    build_email_sender,
)

from tests.unit.live_settings import live_settings


class _Inbox(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _SmtpHandler)
        self.messages: list[tuple[str, list[str], bytes]] = []


class _SmtpHandler(socketserver.StreamRequestHandler):
    """Just enough RFC 5321 for smtplib.send_message: EHLO, MAIL, RCPT, DATA, QUIT."""

    def _reply(self, line: str) -> None:
        self.wfile.write(f"{line}\r\n".encode())

    def handle(self) -> None:
        server: _Inbox = self.server  # type: ignore[assignment]
        sender, recipients = "", []
        self._reply("220 test-inbox ready")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            command = raw.decode().strip()
            verb = command[:4].upper()
            if verb in ("EHLO", "HELO"):
                self._reply("250 test-inbox")
            elif verb == "MAIL":
                sender = command.split(":", 1)[1].strip()
                self._reply("250 OK")
            elif verb == "RCPT":
                recipients.append(command.split(":", 1)[1].strip())
                self._reply("250 OK")
            elif verb == "DATA":
                self._reply("354 end with <CRLF>.<CRLF>")
                body = b""
                while True:
                    line = self.rfile.readline()
                    if line in (b".\r\n", b".\n", b""):
                        break
                    body += line
                server.messages.append((sender, recipients, body))
                sender, recipients = "", []
                self._reply("250 queued")
            elif verb == "QUIT":
                self._reply("221 bye")
                return
            else:
                self._reply("250 OK")


@pytest.fixture
def inbox() -> Iterator[_Inbox]:
    server = _Inbox()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def clean_settings() -> Iterator[None]:
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_send_reaches_the_local_inbox(inbox: _Inbox) -> None:
    sender = SmtpEmailSender(
        host="127.0.0.1", port=inbox.server_address[1], sender="Luciel <luciel@example.com>"
    )
    message_id = await sender.send(
        to="owner@example.com",
        subject="Verify your email",
        body_text="Open http://localhost:3000/verify?token=abc to finish signing up.",
    )

    assert message_id and "@luciel.localhost" in message_id
    assert len(inbox.messages) == 1
    envelope_from, envelope_to, body = inbox.messages[0]
    assert envelope_from == "<luciel@example.com>"
    assert envelope_to == ["<owner@example.com>"]
    parsed = message_from_bytes(body, policy=policy.default)
    assert parsed["Subject"] == "Verify your email"
    assert parsed["Message-ID"].strip("<>") == message_id
    assert "verify?token=abc" in parsed.get_content()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_from_address_overrides_the_platform_identity(inbox: _Inbox) -> None:
    sender = SmtpEmailSender(
        host="127.0.0.1", port=inbox.server_address[1], sender="luciel@example.com"
    )
    await sender.send(
        to="customer@example.com",
        subject="Re: hours",
        body_text="We open at 9.",
        from_address="clinic@example.com",
    )
    assert inbox.messages[0][0] == "<clinic@example.com>"


@pytest.mark.asyncio
async def test_an_unreachable_inbox_raises_rather_than_reporting_a_send() -> None:
    """The dispatcher turns the exception into failed:<Class>; nothing claims delivery."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    sender = SmtpEmailSender(host="127.0.0.1", port=free_port, sender="luciel@example.com")
    with pytest.raises(OSError):
        await sender.send(to="owner@example.com", subject="x", body_text="y")


def test_the_local_inbox_is_bound_in_mock_mode_on_a_developer_computer(
    monkeypatch: pytest.MonkeyPatch, clean_settings: None
) -> None:
    monkeypatch.setenv("LUCIEL_SMTP_HOST", "mailpit")
    monkeypatch.setenv("LUCIEL_SMTP_PORT", "1025")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    sender = build_email_sender()
    assert isinstance(sender, SmtpEmailSender)
    assert (sender.host, sender.port) == ("mailpit", 1025)


def test_without_a_host_mock_mode_keeps_the_null_sender(
    monkeypatch: pytest.MonkeyPatch, clean_settings: None
) -> None:
    monkeypatch.delenv("LUCIEL_SMTP_HOST", raising=False)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert isinstance(build_email_sender(), NullEmailSender)


def test_live_mode_sends_through_ses_even_with_a_host_named(
    monkeypatch: pytest.MonkeyPatch, clean_settings: None
) -> None:
    live = live_settings()
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", live.platform_secret)
    monkeypatch.setenv("LUCIEL_SMTP_HOST", "mailpit")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    assert isinstance(build_email_sender(), SesEmailSender)


def test_the_local_inbox_is_refused_in_a_deployed_environment() -> None:
    with pytest.raises(RuntimeError, match="LUCIEL_SMTP_HOST is only for"):
        Settings(smtp_host="mailpit", environment="dev")
