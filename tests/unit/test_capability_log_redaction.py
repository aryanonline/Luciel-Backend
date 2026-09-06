"""Webhook capability tokens never reach a log line (2026-09-05 audit F072).

A tenant whose Twilio auth token the platform does not hold authenticates its
webhooks with a per-connection ``?cap=`` token in the registered URL (C11). uvicorn's
access log prints the request path WITH its query string, so every inbound text or
call used to write a bearer-equivalent secret to CloudWatch.
"""

from __future__ import annotations

import logging

from app.main import _RedactCapabilityTokens, install_log_redaction


def _access_record(path: str) -> logging.LogRecord:
    # uvicorn.access logs '%s - "%s %s HTTP/%s" %d' with the path as the third arg.
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.7:4711", "POST", path, "1.1", 200),
        None,
    )


def test_the_access_log_path_loses_the_token_and_keeps_everything_else():
    record = _access_record("/api/v1/webhooks/twilio/sms?cap=vmcap_s3cr3tT0ken123&x=1")
    assert _RedactCapabilityTokens().filter(record) is True
    rendered = record.getMessage()
    assert "vmcap_s3cr3tT0ken123" not in rendered
    assert "/api/v1/webhooks/twilio/sms?cap=REDACTED&x=1" in rendered
    assert '"POST' in rendered and "HTTP/1.1" in rendered and "200" in rendered


def test_a_plain_message_carrying_the_token_is_redacted_too():
    record = logging.LogRecord(
        "app.channels",
        logging.WARNING,
        __file__,
        1,
        "retry https://api/x?cap=abc.def-123 later",
        (),
        None,
    )
    _RedactCapabilityTokens().filter(record)
    assert record.getMessage() == "retry https://api/x?cap=REDACTED later"


def test_paths_without_a_token_are_untouched():
    record = _access_record("/api/v1/webhooks/twilio/sms?From=%2B1555&capacity=3")
    _RedactCapabilityTokens().filter(record)
    assert "/api/v1/webhooks/twilio/sms?From=%2B1555&capacity=3" in record.getMessage()


def test_the_filter_is_installed_once_on_the_url_printing_loggers():
    install_log_redaction()
    install_log_redaction()
    for name in ("uvicorn.access", "uvicorn.error", "app"):
        filters = [
            f for f in logging.getLogger(name).filters if isinstance(f, _RedactCapabilityTokens)
        ]
        assert len(filters) == 1, name
