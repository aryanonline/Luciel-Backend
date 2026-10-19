"""The platform secret's accepted key set is ONE explicit, tested list (round 7 WP-7).

``scripts/merge_platform_secret.py`` refuses any pasted key the runtime does not read, so
a typo is never stored and silently ignored. It used to decide "does the runtime read
this?" by scraping the source for literal ``blob.get("KEY")`` calls — which missed every
key config.py reads through a LOOP VARIABLE. STRIPE_WEBHOOK_SECRET and the three LLM model
pins were therefore refused as typos although the runtime reads them, and the owner had
to reach for ``--allow-unknown`` (which also lets a real typo through).

These tests model the condition directly:
  * config.py's own reads are RECORDED while ``Settings`` boots, so a loop-variable read
    is caught exactly like a literal one, and every recorded key must be accepted;
  * every literal read elsewhere in ``app/`` must be accepted;
  * every accepted key must be read somewhere (no stale entries);
  * the merge script accepts the previously refused keys, refuses a typo, and refuses
    it BEFORE touching AWS.
"""

from __future__ import annotations

import io
import json
import re
import sys
import types
from pathlib import Path
from typing import Any

import app.config as config_module
import pytest
from app.config import PLATFORM_SECRET_KEYS, Settings

from tests.unit.live_settings import REQUIRED_IN_LIVE_MODE

_REPO = Path(__file__).resolve().parents[2]
_APP = _REPO / "app"

# The keys the source scrape refused although the runtime reads them.
_PREVIOUSLY_REFUSED = {
    "STRIPE_WEBHOOK_SECRET",
    "LLM_ANTHROPIC_PRIMARY_MODEL",
    "LLM_ANTHROPIC_FAST_MODEL",
    "LLM_OPENAI_FALLBACK_MODEL",
}

# Keys accepted ahead of their reader (another round 7 Phase 3 track adds the read).
# Listed here so the stale-key check below stays strict for everything else.
_PENDING_READERS = {"ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID"}


class _RecordingBlob(dict[str, str]):
    """A parsed platform secret that remembers every key anyone asked it for."""

    def __init__(self, data: dict[str, str], seen: set[str]) -> None:
        super().__init__(data)
        self._seen = seen

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        self._seen.add(str(key))
        return super().get(key, default)

    def __getitem__(self, key: str) -> str:
        self._seen.add(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._seen.add(str(key))
        return super().__contains__(key)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LUCIEL_* / plain SES_* env: an env value short-circuits the blob read for its
    field, which would hide that read from the recorder."""
    import os

    for name in list(os.environ):
        if name.startswith("LUCIEL_") or name.startswith("SES_"):
            monkeypatch.delenv(name, raising=False)


def _keys_config_reads(monkeypatch: pytest.MonkeyPatch, *, live: bool) -> set[str]:
    seen: set[str] = set()
    real_parse = config_module.parse_platform_secret

    def _recording_parse(raw: str, *, live: bool = False) -> dict[str, str]:
        return _RecordingBlob(real_parse(raw, live=live), seen)

    monkeypatch.setattr(config_module, "parse_platform_secret", _recording_parse)
    if live:
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            integration_mode="live",
            platform_secret=json.dumps(REQUIRED_IN_LIVE_MODE),
        )
    else:
        Settings(_env_file=None, platform_secret="{}")  # type: ignore[call-arg]
    return seen


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize("live", [False, True])
def test_every_key_config_reads_is_accepted(monkeypatch: pytest.MonkeyPatch, live: bool) -> None:
    read = _keys_config_reads(monkeypatch, live=live)
    assert read, "the recorder saw no reads — the test is not exercising model_post_init"
    assert read - PLATFORM_SECRET_KEYS == set()
    # The loop-variable reads the source scrape could not see are among them.
    assert _PREVIOUSLY_REFUSED <= read


def _literal_reads_outside_config() -> set[str]:
    """``.get("KEY")`` / ``["KEY"]`` on a parsed platform secret in every module that
    calls ``parse_platform_secret`` (os.environ reads excluded)."""
    pattern = re.compile(
        r'(?:(?<!environ)\.get\(|(?:secret|blob)\[)\s*"([A-Z][A-Z0-9]*_[A-Z0-9_]+)"'
    )
    keys: set[str] = set()
    for path in _APP.rglob("*.py"):
        if path.name == "config.py" and path.parent == _APP:
            continue
        text = path.read_text(encoding="utf-8")
        if "parse_platform_secret" not in text:
            continue
        keys.update(pattern.findall(text))
    return keys


def test_every_literal_read_elsewhere_in_the_app_is_accepted() -> None:
    reads = _literal_reads_outside_config()
    # The provider keys are read outside config.py; the scan must find them or it is
    # not looking where the reads are.
    assert {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STRIPE_TEST_SECRET_KEY"} <= reads
    assert reads - PLATFORM_SECRET_KEYS == set()


def test_the_model_currency_writer_only_writes_accepted_keys() -> None:
    """The auto-swap writes model pins and its swap record INTO the blob; a key it
    writes that the merge script refuses would be unmaintainable by hand."""
    from app.runtime import model_currency

    written = {pin.blob_key for pin in model_currency.PINS if pin.swappable}
    written.add(model_currency.LAST_SWAP_BLOB_KEY)
    assert written - PLATFORM_SECRET_KEYS == set()


@pytest.mark.usefixtures("clean_env")
def test_no_accepted_key_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every accepted key is actually read somewhere — a key nobody reads accepted by the
    merge script is the same silent no-op the refusal exists to prevent."""
    read = _keys_config_reads(monkeypatch, live=False) | _keys_config_reads(monkeypatch, live=True)
    read |= _literal_reads_outside_config()
    stale = PLATFORM_SECRET_KEYS - read - _PENDING_READERS
    assert stale == set()


@pytest.mark.usefixtures("clean_env")
def test_the_contact_inbox_override_now_applies() -> None:
    """PLATFORM_CONTACT_INBOX sat in the empty-field overlay while its field defaults to
    info@vantagemind.ai — so the documented per-environment override never applied."""
    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        platform_secret=json.dumps({"PLATFORM_CONTACT_INBOX": "ops@example.test"}),
    )
    assert s.platform_contact_inbox == "ops@example.test"
    assert Settings(_env_file=None).platform_contact_inbox == "info@vantagemind.ai"  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# The merge script
# --------------------------------------------------------------------------- #


def _load_script() -> types.ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "merge_platform_secret_under_test", _REPO / "scripts" / "merge_platform_secret.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSecretsManager:
    def __init__(self, blob: dict[str, str]) -> None:
        self.blob = blob
        self.reads = 0
        self.writes: list[dict[str, str]] = []

    def get_secret_value(self, *, SecretId: str) -> dict[str, str]:  # noqa: N803
        self.reads += 1
        return {"SecretString": json.dumps(self.blob)}

    def put_secret_value(self, *, SecretId: str, SecretString: str) -> dict[str, str]:  # noqa: N803
        self.writes.append(json.loads(SecretString))
        return {"VersionId": "v-test"}


@pytest.fixture
def fake_aws(monkeypatch: pytest.MonkeyPatch) -> _FakeSecretsManager:
    sm = _FakeSecretsManager({"EXISTING_KEY": "kept"})
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda service, **_kw: sm  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    return sm


def _run(monkeypatch: pytest.MonkeyPatch, stdin: str, *argv: str) -> int:
    script = _load_script()
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    return int(script.main(["--env", "dev", *argv]))


def test_the_script_accepts_the_keys_the_scrape_refused(
    monkeypatch: pytest.MonkeyPatch, fake_aws: _FakeSecretsManager
) -> None:
    paste = "".join(f"{k}=value-{i}\n" for i, k in enumerate(sorted(_PREVIOUSLY_REFUSED)))
    assert _run(monkeypatch, paste) == 0
    assert len(fake_aws.writes) == 1
    written = fake_aws.writes[0]
    assert _PREVIOUSLY_REFUSED <= set(written)
    assert written["EXISTING_KEY"] == "kept"  # other keys untouched


def test_the_script_refuses_a_typo_before_touching_aws(
    monkeypatch: pytest.MonkeyPatch, fake_aws: _FakeSecretsManager
) -> None:
    with pytest.raises(SystemExit) as refused:
        _run(monkeypatch, "STRIPE_WEBHOK_SECRET=whsec_x\nMETA_APP_ID=123\n")
    assert "STRIPE_WEBHOK_SECRET" in str(refused.value)
    assert "whsec_x" not in str(refused.value)  # names only, never a value
    assert fake_aws.reads == 0
    assert fake_aws.writes == []


def test_the_script_uses_the_config_list_itself() -> None:
    script = _load_script()
    assert script._known_keys() is PLATFORM_SECRET_KEYS
    assert script.unknown_keys({"META_APP_ID": "1", "NOT_A_REAL_KEY": "2"}) == ["NOT_A_REAL_KEY"]
