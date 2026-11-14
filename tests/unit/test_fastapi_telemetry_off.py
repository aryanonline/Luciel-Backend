"""FastAPI's built-in OpenTelemetry stays off, whatever else configures OpenTelemetry.

fastapi >= 0.142 traces, meters and logs every request by itself as soon as ANY global
OpenTelemetry provider is set. No instrumentation call and no setting in this repository
are needed: an SDK that a dependency or an agent installs is enough. Its server span keeps
url.path and url.query and redacts only cloud-storage signature parameters, so the webhook
``?cap=`` token that ``_RedactCapabilityTokens`` keeps out of the logs (audit F072), the
export token in /api/v1/exports/{token} and OAuth code/state would leave in every trace,
and its exception logs carry messages and stack traces. create_app() switches all three
signals off; turning them on is a decision that has to bring its own redaction.

No SDK is installed, so the providers below stand in for one. They are put exactly where
``set_tracer_provider`` and its siblings put a real one (and taken back afterwards: the
real setters work once per process and would leak into every later test), so FastAPI
finds them as it would find an SDK's. The first test is the control: a default FastAPI app
does pick them up and does write the token into its span, so the second cannot pass just
because the stand-ins went unnoticed. Neither test needs Postgres.
"""

from __future__ import annotations

from typing import Any

import opentelemetry._logs._internal as otel_logs
import opentelemetry.metrics._internal as otel_metrics
import pytest
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry._logs import LoggerProvider, NoOpLogger
from opentelemetry.metrics import MeterProvider, NoOpMeter

TOKEN = "cap-token-that-must-not-leave"


class _Tracer(trace.NoOpTracer):
    def __init__(self, spans: list[dict[str, Any]]) -> None:
        self._spans = spans

    def start_span(self, name: str, *args: Any, **kwargs: Any) -> trace.Span:
        self._spans.append({"name": name, **dict(kwargs.get("attributes") or {})})
        return trace.INVALID_SPAN


class _TracerProvider(trace.TracerProvider):
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.spans: list[dict[str, Any]] = []

    def get_tracer(self, instrumenting_module_name: str, *args: Any, **kwargs: Any) -> trace.Tracer:
        self.acquired.append(instrumenting_module_name)
        return _Tracer(self.spans)


class _MeterProvider(MeterProvider):
    def __init__(self) -> None:
        self.acquired: list[str] = []

    def get_meter(self, name: str, *args: Any, **kwargs: Any) -> NoOpMeter:
        self.acquired.append(name)
        return NoOpMeter(name)


class _LoggerProvider(LoggerProvider):
    def __init__(self) -> None:
        self.acquired: list[str] = []

    def get_logger(self, name: str, *args: Any, **kwargs: Any) -> NoOpLogger:
        self.acquired.append(name)
        return NoOpLogger(name)


@pytest.fixture
def providers(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_TracerProvider, _MeterProvider, _LoggerProvider]:
    tracers, meters, loggers = _TracerProvider(), _MeterProvider(), _LoggerProvider()
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", tracers)
    monkeypatch.setattr(otel_metrics, "_METER_PROVIDER", meters)
    monkeypatch.setattr(otel_logs, "_LOGGER_PROVIDER", loggers)
    return tracers, meters, loggers


def test_a_default_fastapi_app_traces_the_token_once_any_provider_exists(providers) -> None:
    """The control, and the hazard itself: nothing asked FastAPI to trace."""
    tracers, meters, loggers = providers
    app = FastAPI()

    @app.get("/probe")
    async def _probe() -> dict[str, bool]:
        return {"ok": True}

    assert TestClient(app).get(f"/probe?cap={TOKEN}").status_code == 200
    assert tracers.acquired and meters.acquired and loggers.acquired
    assert any(TOKEN in str(span.get("url.query")) for span in tracers.spans), tracers.spans


def test_create_app_keeps_fastapi_telemetry_off(providers) -> None:
    tracers, meters, loggers = providers
    response = TestClient(create_app()).get(f"/api/v1/no-such-route?cap={TOKEN}")

    assert response.status_code == 404
    assert (tracers.acquired, meters.acquired, loggers.acquired) == ([], [], [])
    assert tracers.spans == []
