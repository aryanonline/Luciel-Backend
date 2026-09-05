"""The image CVE gate fails closed and admits only governed, unexpired exceptions.

The gate this replaced had two dishonest edges: it could not represent a reviewed
exception (a no-fix critical in an Essential base package meant weeks of red pipeline,
which also blocked PAUSING the environment), and its error paths failed OPEN — an ECR
API error or an unfinished scan became a PASS via `|| echo None`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_image_scan", Path(__file__).parents[2] / "scripts" / "check_image_scan.py"
)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

TODAY = "2026-08-10"


def _scan(*criticals: str, status: str = "COMPLETE") -> dict:
    return {
        "imageScanStatus": {"status": status},
        "imageScanFindings": {
            "findings": [{"name": cve, "severity": "CRITICAL"} for cve in criticals]
            + [{"name": "CVE-LOW-1", "severity": "LOW"}]
        },
    }


def _allowlist(*entries: dict) -> dict:
    return {"allow": list(entries)}


def _entry(cve: str, *, expires: str = "2026-09-09") -> dict:
    return {
        "cve": cve,
        "package": "perl",
        "reason": "Essential base package; no perl execution path; no distro fix yet.",
        "added": "2026-08-10",
        "expires": expires,
    }


def _run(tmp_path: Path, scan: dict, allowlist: dict, *, today: str = TODAY) -> int:
    scan_file = tmp_path / "scan.json"
    allow_file = tmp_path / "allow.json"
    scan_file.write_text(json.dumps(scan), encoding="utf-8")
    allow_file.write_text(json.dumps(allowlist), encoding="utf-8")
    return gate.main([str(scan_file), str(allow_file), "--today", today])


def test_a_listed_unexpired_critical_passes(tmp_path):
    assert _run(tmp_path, _scan("CVE-2026-12087"), _allowlist(_entry("CVE-2026-12087"))) == 0


def test_an_unlisted_critical_fails(tmp_path):
    assert _run(tmp_path, _scan("CVE-2026-99999"), _allowlist(_entry("CVE-2026-12087"))) == 1


def test_an_expired_entry_with_the_cve_still_present_fails(tmp_path):
    """Expiry forces a human re-review — it never silently extends."""
    scan = _scan("CVE-2026-12087")
    allow = _allowlist(_entry("CVE-2026-12087", expires="2026-08-01"))
    assert _run(tmp_path, scan, allow) == 1


def test_a_stale_entry_whose_cve_is_gone_only_warns(tmp_path, capsys):
    """Housekeeping is visible without turning cleanup into an outage."""
    assert _run(tmp_path, _scan(), _allowlist(_entry("CVE-2026-12087"))) == 0
    assert "no longer matches" in capsys.readouterr().out


def test_an_incomplete_scan_fails_closed(tmp_path):
    """The old gate's `|| echo None` turned exactly this into a PASS."""
    assert _run(tmp_path, _scan(status="IN_PROGRESS"), _allowlist()) == 1
    assert _run(tmp_path, _scan(status="FAILED"), _allowlist()) == 1
    assert _run(tmp_path, {"imageScanFindings": {}}, _allowlist()) == 1


def test_a_malformed_allowlist_fails_closed(tmp_path):
    entry = _entry("CVE-2026-12087")
    del entry["reason"]  # an exception without a justification is not governed
    assert _run(tmp_path, _scan("CVE-2026-12087"), _allowlist(entry)) == 1
    assert _run(tmp_path, _scan(), {"allow": "not-a-list"}) == 1


def test_unreadable_scan_json_fails_closed(tmp_path):
    scan_file = tmp_path / "scan.json"
    allow_file = tmp_path / "allow.json"
    scan_file.write_text("{not json", encoding="utf-8")
    allow_file.write_text(json.dumps(_allowlist()), encoding="utf-8")
    assert gate.main([str(scan_file), str(allow_file), "--today", TODAY]) == 1


def test_medium_and_low_never_block(tmp_path):
    scan = {
        "imageScanStatus": {"status": "COMPLETE"},
        "imageScanFindings": {
            "findings": [
                {"name": "CVE-MED-1", "severity": "MEDIUM"},
                {"name": "CVE-LOW-2", "severity": "LOW"},
            ]
        },
    }
    assert _run(tmp_path, scan, _allowlist()) == 0


def test_an_unlisted_high_blocks_and_a_listed_high_passes(tmp_path):
    """§5.14: HIGH blocks promotion the same way CRITICAL does, under the same governance."""
    scan = {
        "imageScanStatus": {"status": "COMPLETE"},
        "imageScanFindings": {"findings": [{"name": "CVE-HIGH-1", "severity": "HIGH"}]},
    }
    assert _run(tmp_path, scan, _allowlist()) == 1
    assert _run(tmp_path, scan, _allowlist(_entry("CVE-HIGH-1"))) == 0


def test_severity_scope_is_configurable(tmp_path):
    scan = {
        "imageScanStatus": {"status": "COMPLETE"},
        "imageScanFindings": {"findings": [{"name": "CVE-HIGH-1", "severity": "HIGH"}]},
    }
    scan_file = tmp_path / "scan.json"
    allow_file = tmp_path / "allow.json"
    scan_file.write_text(json.dumps(scan), encoding="utf-8")
    allow_file.write_text(json.dumps(_allowlist()), encoding="utf-8")
    argv = [str(scan_file), str(allow_file), "--today", TODAY, "--severities", "CRITICAL"]
    assert gate.main(argv) == 0


def test_the_shipped_allowlist_is_valid_and_every_entry_carries_a_reason():
    """The real .security/cve-allowlist.json must always parse under the strict loader."""
    shipped = json.loads(
        (Path(__file__).parents[2] / ".security" / "cve-allowlist.json").read_text(encoding="utf-8")
    )
    entries = gate.load_allowlist(shipped)
    assert entries, "shipped allowlist should not be empty while the base CVEs persist"
    for cve, entry in entries.items():
        assert len(entry["reason"]) > 40, f"{cve}: a governed exception needs a real reason"
        # A re-reviewed entry names the tracker check so the next reviewer can diff it.
        assert "security tracker" in entry["reason"], f"{cve}: reason must cite the tracker check"


@pytest.mark.parametrize("severity", ["critical", "Critical"])
def test_severity_matching_is_case_insensitive(tmp_path, severity):
    scan = {
        "imageScanStatus": {"status": "COMPLETE"},
        "imageScanFindings": {"findings": [{"name": "CVE-X", "severity": severity}]},
    }
    assert _run(tmp_path, scan, _allowlist()) == 1
