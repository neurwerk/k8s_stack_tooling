"""Strict JSON document primitives shared by gateway consumers."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
