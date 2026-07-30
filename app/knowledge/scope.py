"""Knowledge SCOPE: which part of a connected source Luciel may read (Decision #9).

Connecting Google Drive or Notion previously meant granting the whole account and having
everything the token could reach swept into knowledge. A small-business owner's Drive holds
payroll, contracts, and personal files next to the FAQ they actually wanted answered from —
"connect Drive" was therefore an all-or-nothing bet most owners should not take.

Scope is stored on the connection's existing ``non_secret_config`` JSONB (§3.8.3: a list of
folder/page ids is a POINTER, not a credential — no migration and no new secret surface):

    non_secret_config["scope"] = {
        "kind": "drive_folders",                       # registry ProviderSpec.scope_kind
        "selections": [{"id": "...", "name": "..."}],  # what the owner picked
    }

An EMPTY or ABSENT scope means the whole connected account, which is the pre-Decision-#9
behavior and stays the default so existing connections keep syncing exactly as before.

Enforcement is deliberately in TWO places (see ``in_scope``): the provider narrows its
remote query so out-of-scope content is never fetched, AND every returned item is checked
against the scope locally. The second check is not redundant — Drive returns shared and
shortcut files that satisfy a query without living under a selected folder, and Notion's
search API has no ancestor filter at all, so the local check is the only thing that can
actually enforce a Notion scope.
"""

from __future__ import annotations

from typing import Any

SCOPE_KEY = "scope"


def scope_of(connection: Any) -> dict[str, Any]:
    """The scope record on a connection row, or ``{}`` when unscoped (whole account)."""
    cfg = getattr(connection, "non_secret_config", None) or {}
    scope = cfg.get(SCOPE_KEY)
    return dict(scope) if isinstance(scope, dict) else {}


def scope_ids(connection: Any) -> list[str]:
    """The selected folder/page ids, in the order the owner chose them.

    ``[]`` means unscoped — callers must treat that as "everything", never as "nothing",
    or an existing connection would silently stop syncing."""
    selections = scope_of(connection).get("selections")
    if not isinstance(selections, list):
        return []
    return [str(s["id"]) for s in selections if isinstance(s, dict) and s.get("id")]


def write_scope(
    connection: Any, *, kind: str, selections: list[dict[str, str]]
) -> dict[str, object]:
    """Persist a scope onto the connection's non_secret_config and return the new config.

    Reassigns the whole dict rather than mutating it in place so SQLAlchemy sees the JSONB
    column as dirty."""
    cfg = dict(getattr(connection, "non_secret_config", None) or {})
    cfg[SCOPE_KEY] = {
        "kind": kind,
        "selections": [
            {"id": str(s["id"]), "name": str(s.get("name", s["id"]))} for s in selections
        ],
    }
    connection.non_secret_config = cfg
    return cfg


def drive_query(ids: list[str]) -> str:
    """The Drive v3 ``q`` narrowing a listing to the selected folders.

    Only DIRECT children are matched: Drive's query language has no recursive-descendant
    operator, so a nested subfolder is out of scope unless the owner selected it too. That
    is the conservative reading of "which folders may Luciel read" — under-including is a
    smaller failure than reading a private subfolder the owner never listed."""
    if not ids:
        return "trashed=false"
    parents = " or ".join(f"'{i}' in parents" for i in ids if "'" not in i)
    if not parents:
        return "trashed=false"
    return f"trashed=false and ({parents})"


def in_scope(item: dict[str, Any], ids: list[str], *, parent_keys: tuple[str, ...]) -> bool:
    """Whether one remote item falls inside the selected scope.

    An item qualifies by BEING a selection (the owner picked that page) or by being
    parented by one. Unscoped connections admit everything."""
    if not ids:
        return True
    wanted = set(ids)
    if str(item.get("id", "")) in wanted:
        return True
    for key in parent_keys:
        value = item.get(key)
        if isinstance(value, list):
            if any(str(v) in wanted for v in value):
                return True
        elif isinstance(value, dict):
            # Notion shapes the parent as {"type": "database_id", "database_id": "..."}.
            if any(str(v) in wanted for v in value.values()):
                return True
        elif value is not None and str(value) in wanted:
            return True
    return False
