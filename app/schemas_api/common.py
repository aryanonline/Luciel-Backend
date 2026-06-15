"""Base Pydantic config for wire models — camelCase aliases, exact field names."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class WireModel(BaseModel):
    """All response/request bodies serialize/parse using camelCase keys.

    populate_by_name lets handlers construct with snake_case kwargs while the JSON
    on the wire is camelCase (matching the frozen Zod schemas in packages/api-client).
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
