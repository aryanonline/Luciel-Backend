"""Operational truth, deterministic half (2026-09-05 audit WP7).

* One trace id per request, echoed back, honoured when the edge sent one (F035).
* JSON log lines carry the trace id and the acting tenant.
* A metric is one EMF line CloudWatch can extract (F032).
* The in-process mailbox loop survives a failing run and stops on request.
* Engine settings: bounded pool + a statement deadline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import pytest
from app.config import Settings
from app.jobs.inprocess import mailbox_poll_loop, should_run_inprocess_mailbox_poll
from app.main import TraceContext
from app.observability import metrics
from app.observability.context import bind_admin, bind_trace, current_trace_id
from app.observability.logging import StructuredFormatter
from fastapi import FastAPI
from fastapi.testclient import TestClient


# --- F035 trace context ------------------------------------------------------- #
def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo")
    async def echo() -> dict[str, str]:
        return {"trace": current_trace_id()}

    app.add_middleware(TraceContext)
    return app


def test_every_request_gets_a_trace_id_and_echoes_it():
    client = TestClient(_app())
    r = client.get("/echo")
    assert r.status_code == 200
    trace = r.headers["x-request-id"]
    assert len(trace) == 32 and r.json()["trace"] == trace
    # A second request is a different trace.
    assert client.get("/echo").headers["x-request-id"] != trace


def test_the_edges_trace_id_is_honoured():
    client = TestClient(_app())
    r = client.get("/echo", headers={"X-Amzn-Trace-Id": "Root=1-abc-def"})
    assert r.headers["x-request-id"] == "Root=1-abc-def"
    assert r.json()["trace"] == "Root=1-abc-def"
    r = client.get("/echo", headers={"X-Request-Id": "caller-7"})
    assert r.headers["x-request-id"] == "caller-7"


def test_json_log_lines_carry_trace_and_tenant(monkeypatch):
    monkeypatch.setenv("LUCIEL_PLANE", "data-plane")
    t1 = bind_trace("trace-xyz")
    t2 = bind_admin("11111111-1111-4111-8111-111111111111")
    try:
        record = logging.LogRecord("app.x", logging.WARNING, __file__, 1, "hello %s", ("w",), None)
        line = json.loads(StructuredFormatter().format(record))
    finally:
        from app.observability.context import admin_id_var, trace_id_var

        trace_id_var.reset(t1)
        admin_id_var.reset(t2)
    assert line["message"] == "hello w" and line["level"] == "WARNING"
    assert line["trace_id"] == "trace-xyz"
    assert line["admin_id"] == "11111111-1111-4111-8111-111111111111"
    assert line["plane"] == "data-plane" and line["logger"] == "app.x"


# --- F032 metrics --------------------------------------------------------------- #
def test_a_metric_is_one_emf_line(monkeypatch):
    monkeypatch.setenv("LUCIEL_PLANE", "sweep")
    doc = metrics.emf_document(
        "SweepTenantFailure", 1.0, dimensions={"Sweep": "retention"}, environment="dev"
    )
    envelope = doc["_aws"]["CloudWatchMetrics"][0]
    assert envelope["Namespace"] == "Luciel"
    assert envelope["Metrics"] == [{"Name": "SweepTenantFailure", "Unit": "Count"}]
    assert envelope["Dimensions"] == [["Environment"], ["Environment", "Plane", "Sweep"]]
    assert doc["Environment"] == "dev" and doc["Plane"] == "sweep" and doc["Sweep"] == "retention"
    assert doc["SweepTenantFailure"] == 1.0
    json.dumps(doc)  # serialisable as-is


def test_emit_metric_writes_the_line_and_never_raises(capsys):
    metrics.emit_metric("BudgetGateHit", dimensions={"Channel": "widget"})
    out = capsys.readouterr().out.strip().splitlines()
    line = json.loads(out[-1])
    assert "BudgetGateHit" in line and line["Channel"] == "widget"
    # A broken dimension value cannot break a turn.
    metrics.emit_metric("EscalationFired", dimensions={"Signal": object()})  # type: ignore[dict-item]


# --- in-process mailbox poll ------------------------------------------------------ #
def test_inprocess_poll_runs_only_on_the_data_plane_when_asked(monkeypatch):
    monkeypatch.delenv("LUCIEL_PLANE", raising=False)
    assert should_run_inprocess_mailbox_poll(Settings(inprocess_mailbox_poll_seconds=300)) is False
    monkeypatch.setenv("LUCIEL_PLANE", "data-plane")
    assert should_run_inprocess_mailbox_poll(Settings(inprocess_mailbox_poll_seconds=300)) is True
    assert should_run_inprocess_mailbox_poll(Settings(inprocess_mailbox_poll_seconds=0)) is False
    monkeypatch.setenv("LUCIEL_PLANE", "control-plane")
    assert should_run_inprocess_mailbox_poll(Settings(inprocess_mailbox_poll_seconds=300)) is False


@pytest.mark.asyncio
async def test_the_loop_survives_a_failing_run_and_stops_on_request(caplog):
    calls = {"n": 0}

    async def run() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider down")

    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR, logger="app.jobs.inprocess"):
        task = asyncio.create_task(mailbox_poll_loop(stop, interval_seconds=0.01, run=run))
        await asyncio.sleep(0.08)
        stop.set()
        runs = await asyncio.wait_for(task, timeout=2)
    assert runs >= 2 and calls["n"] == runs
    assert any("inprocess_mailbox_poll_failed" in r.getMessage() for r in caplog.records)


# --- engine posture ------------------------------------------------------------- #
def test_engine_settings_are_bounded():
    s = Settings()
    assert 1 <= s.db_pool_size <= 20 and 0 <= s.db_max_overflow <= 20
    assert 5_000 <= s.db_statement_timeout_ms <= 300_000
    assert s.log_format in ("text", "json")
    assert os.environ.get("LUCIEL_INPROCESS_MAILBOX_POLL_SECONDS") is None


def test_statement_timeout_is_the_architectures_thirty_seconds():
    # Architecture §4.7 says 30 seconds; the setting shipped at 60_000 (round 7
    # WP-8). Read the class default, not the environment, so a developer's .env
    # cannot make this pass or fail.
    assert Settings.model_fields["db_statement_timeout_ms"].default == 30_000
