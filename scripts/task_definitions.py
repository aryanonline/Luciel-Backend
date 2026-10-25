"""ECS task-definition helpers for the deploy pipeline (round 7, Phase 3 infra review).

``describe-task-definition`` returns read-only fields (ARN, revision, status,
registeredAt/By, compatibilities, requiresAttributes) that ``register-task-definition``
refuses. ``registerable`` keeps exactly what register accepts, plus the cost-allocation
tags, so a revision can be re-registered as it was. scripts/migrate-before-roll.sh
carries the same field list inline (it swaps one container's image on top); keep the
two lists identical.
"""

from __future__ import annotations

from typing import Any

#: Every field ``register-task-definition`` accepts from a described definition.
REGISTERABLE_FIELDS: tuple[str, ...] = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "runtimePlatform",
    "ephemeralStorage",
    "pidMode",
    "ipcMode",
    "proxyConfiguration",
    "inferenceAccelerators",
)


def registerable(described: dict[str, Any]) -> dict[str, Any]:
    """The ``register-task-definition`` input that re-creates ``described`` (the output
    of ``describe-task-definition --include TAGS``) as a new revision of its family."""
    td = described["taskDefinition"]
    out = {k: td[k] for k in REGISTERABLE_FIELDS if td.get(k) not in (None, [], {})}
    tags = described.get("tags") or []
    if tags:
        out["tags"] = tags
    return out
