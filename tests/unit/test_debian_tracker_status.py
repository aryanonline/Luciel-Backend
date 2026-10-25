"""Debian tracker evidence for the CVE review (round 7 WP-9, debian_tracker_status.py).

The monthly steward routine's sandbox cannot reach security-tracker.debian.org
(egress-blocked, found 2026-10-04) and was renewing waivers on search-engine
evidence. The GitHub runner fetches the tracker instead and posts a compact
per-release table on the review issue. The fixtures are tracker pages saved on
2026-10-19 (CVE-2026-95619: five gcc source packages; CVE-2026-19499: glibc with a
no-dsa note and a sid fix; and the tracker's 404 page).
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sys
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "debian_tracker_status", _ROOT / "scripts" / "debian_tracker_status.py"
)
assert _SPEC and _SPEC.loader
tracker = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = tracker  # dataclasses resolve their module by name
_SPEC.loader.exec_module(tracker)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 19, 7, 31, 2, tzinfo=UTC)


def _page(name: str) -> str:
    return (FIXTURES / f"debian_tracker_{name}.html").read_text(encoding="utf-8")


def test_a_named_package_keeps_only_its_rows_for_the_image_releases():
    rows = tracker.parse_tracker(_page("CVE-2026-95619"), "gcc-14", ("trixie", "forky", "sid"))
    assert [(r.package, r.release, r.version, r.status) for r in rows] == [
        ("gcc-14", "trixie", "14.2.0-19", "vulnerable"),
        ("gcc-14", "forky", "14.4.0-2", "vulnerable"),
        ("gcc-14", "sid", "14.4.0-3", "fixed"),
    ]
    assert rows[0].note == "trixie: no-dsa (Minor issue)"
    assert rows[2].note == "sid: fixed in 14.4.0-3"


def test_no_dsa_and_fixed_in_notes_are_read_and_bookworm_is_left_out():
    rows = tracker.parse_tracker(_page("CVE-2026-19499"), "glibc", ("trixie", "forky", "sid"))
    assert [(r.release, r.version, r.status, r.note) for r in rows] == [
        ("trixie", "2.41-12+deb13u4", "vulnerable", "trixie: no-dsa (Minor issue)"),
        ("forky, sid", "2.43-7", "fixed", "sid: fixed in 2.43-5"),
    ]


def test_an_unknown_package_hint_shows_every_package_on_the_page():
    rows = tracker.parse_tracker(_page("CVE-2026-95619"), "libstdc++6", ("trixie",))
    assert {r.package for r in rows} == {"gcc-12", "gcc-13", "gcc-14"}


def test_targets_are_every_waiver_and_every_not_allowlisted_gate_line(tmp_path):
    allowlist = tmp_path / "allow.json"
    allowlist.write_text(
        json.dumps(
            {
                "allow": [
                    {"cve": "CVE-2026-85091", "package": "zlib", "expires": "2026-10-31"},
                    {"cve": "CVE-2026-19499", "package": "glibc", "expires": "2026-11-16"},
                ]
            }
        ),
        encoding="utf-8",
    )
    gate = tmp_path / "gate.txt"
    gate.write_text(
        "[gate] linux/amd64 (sha256:aa): scan status 'COMPLETE'\n"
        "[gate] CVE-2026-85091 (zlib 1.3.dfsg+really1.3.1-1) allowlisted until 2026-10-31\n"
        "::error::CVE-2026-95619 (gcc-14 14.2.0-19): HIGH and not allowlisted\n"
        "[gate] linux/arm64 (sha256:bb): scan status 'COMPLETE'\n"
        "::error::CVE-2026-95619 (gcc-14 14.2.0-19): HIGH and not allowlisted\n"
        "::error::CVE-2026-82560: CRITICAL and not allowlisted\n",
        encoding="utf-8",
    )
    targets = tracker.targets_from(allowlist, [gate], [])
    assert [(t.cve, t.why, t.package) for t in targets] == [
        ("CVE-2026-85091", "waived until 2026-10-31", "zlib"),
        ("CVE-2026-19499", "waived until 2026-11-16", "glibc"),
        ("CVE-2026-95619", "not allowlisted (HIGH, installed 14.2.0-19)", "gcc-14"),
        ("CVE-2026-82560", "not allowlisted (CRITICAL)", None),
    ]


def test_the_report_is_a_timestamped_table_and_a_fetch_failure_is_one_row():
    pages = {"CVE-2026-19499": ("ok", _page("CVE-2026-19499"))}
    targets = [
        tracker.Target("CVE-2026-19499", "waived until 2026-11-16", "glibc"),
        tracker.Target("CVE-2026-85091", "waived until 2026-10-31", "zlib"),
        tracker.Target("CVE-2099-99999", "requested", None),
    ]

    def fetcher(cve: str) -> tuple[str, str | None]:
        if cve == "CVE-2099-99999":
            return "not in tracker", None
        return pages.get(cve, ("tracker unreachable", None))

    report = tracker.build_report(targets, ("trixie", "forky", "sid"), fetcher, NOW)
    assert report.startswith("#### Debian tracker status (fetched 2026-10-19T07:31:02Z)")
    assert (
        "| glibc | trixie | 2.41-12+deb13u4 | vulnerable | trixie: no-dsa (Minor issue) |" in report
    )
    zlib = next(line for line in report.splitlines() if "CVE-2026-85091" in line)
    assert "tracker unreachable" in zlib and "| zlib |" in zlib
    assert "not in tracker" in report


def test_a_network_error_never_fails_the_job(tmp_path, monkeypatch, capsys):
    def refuse(*_args, **_kwargs):
        raise urllib.error.URLError("egress blocked")

    monkeypatch.setattr(tracker.urllib.request, "urlopen", refuse)
    out = tmp_path / "tracker.md"
    argv = ["--cve", "CVE-2026-85091", "--markdown", str(out), "--timeout", "1"]
    assert tracker.main(argv) == 0
    assert "tracker unreachable" in out.read_text(encoding="utf-8")
    assert "tracker unreachable" in capsys.readouterr().out


def test_a_404_reads_as_not_in_tracker(monkeypatch):
    def not_found(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(tracker.urllib.request, "urlopen", not_found)
    assert tracker.fetch("CVE-2099-99999", 1) == ("not in tracker", None)


def test_the_tracker_404_page_parses_to_no_rows():
    assert tracker.parse_tracker(_page("not_found"), None, ("trixie",)) == []


def test_the_shipped_allowlist_yields_one_target_per_waiver():
    shipped = _ROOT / ".security" / "cve-allowlist.json"
    waivers = json.loads(shipped.read_text(encoding="utf-8"))["allow"]
    targets = tracker.targets_from(shipped, [], [])
    assert [t.cve for t in targets] == [w["cve"] for w in waivers]


def test_a_truncated_page_is_one_unreachable_row_not_a_lost_table(monkeypatch):
    """http.client's IncompleteRead (and BadStatusLine, LineTooLong) is an
    HTTPException, not an OSError: it used to escape fetch(), unwind to main() and
    replace the WHOLE evidence table with "(not gathered ...)" (Phase 3 review)."""

    class _Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"<html>")

    monkeypatch.setattr(tracker.urllib.request, "urlopen", lambda *_a, **_k: _Truncated())
    assert tracker.fetch("CVE-2026-85091", 1) == ("tracker unreachable", None)


def test_a_fetcher_surprise_is_one_row_and_the_other_rows_survive():
    targets = [
        tracker.Target("CVE-2026-19499", "waived until 2026-11-16", "glibc"),
        tracker.Target("CVE-2026-85091", "waived until 2026-10-31", "zlib"),
    ]

    def fetcher(cve: str) -> tuple[str, str | None]:
        if cve == "CVE-2026-85091":
            raise http.client.BadStatusLine("garbage")
        return "ok", _page(cve)

    report = tracker.build_report(targets, ("trixie", "forky", "sid"), fetcher, NOW)
    zlib = next(line for line in report.splitlines() if "CVE-2026-85091" in line)
    assert "tracker unreachable (BadStatusLine)" in zlib
    assert "| glibc | trixie | 2.41-12+deb13u4 |" in report
