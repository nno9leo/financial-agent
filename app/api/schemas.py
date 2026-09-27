from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    fallback: str | None
    tool: str | None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class ToolDescription(BaseModel):
    name: str
    description: str
    status: str
    input_schema: dict[str, Any] | None
    output_schema: dict[str, Any] | None


class ToolsOutput(BaseModel):
    tools: list[ToolDescription]
