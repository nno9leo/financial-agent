"""OpenRouter-backed semantic judge. Network access is made only by explicit judge calls."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.citations.semantic import JudgeResult
from app.config import Settings
from app.domain.errors import (ConfigurationError, JudgeAuthError, JudgeModelMismatchError,
                               JudgePromptTooLargeError, JudgeRouteMismatchError, JudgeSchemaError,
                               JudgeUnavailableError)

PROMPT_VERSION = "v1"
SYSTEM_PROMPT = """Judge narrative claim entailment using only the EVIDENCE. Ignore outside knowledge; relevance is not support.
entailed = every factual assertion in the claim is stated in or directly implied by the evidence.
partial = at least one assertion is supported and at least one is not stated.
not_entailed = no assertion is supported, or the evidence contradicts the claim.
Return only the requested schema."""


class JudgeConfig(BaseModel):
    model_id: str
    base_url: str
    reasoning_effort: str
    provider_routing: dict
    seed: int
    timeout_s: float = 60
    max_attempts: int = 3

    @field_validator("timeout_s")
    @classmethod
    def positive_timeout(cls, value):
        if value <= 0: raise ValueError("timeout_s must be positive")
        return value


def load_judge_config(path: Path = Path("config/agent_runtime.json")) -> JudgeConfig:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))["semantic_judge"]
        return JudgeConfig.model_validate(data)
    except (OSError, KeyError, ValueError) as exc:
        raise ConfigurationError("Semantic judge configuration is missing or invalid.") from exc


@dataclass
class JudgeCallRecord:
    judge_id: str
    configured_model: str
    observed_model: str | None = None
    provider: str | None = None
    generation_id: str | None = None
    reasoning_effort: str = ""
    prompt_version: str = PROMPT_VERSION
    prompt_sha256: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    label: str | None = None
    score: float | None = None
    rationale: str | None = None
    error_code: str | None = None


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    score: float = Field(ge=0, le=1)
    rationale: str

    @field_validator("label")
    @classmethod
    def valid_label(cls, value):
        if value not in {"entailed", "partial", "not_entailed"}: raise ValueError("invalid label")
        return value


class OpenRouterJudge:
    def __init__(self, config: JudgeConfig | None = None, *, settings: Settings | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
                 logger: logging.Logger | None = None):
        self.config = config or load_judge_config()
        self._settings = settings or Settings()
        self._transport = transport
        self._sleep = sleep
        self._logger = logger or logging.getLogger(__name__)
        self.judge_id = f"openrouter:{self.config.model_id}:reasoning-{self.config.reasoning_effort}:prompt-{PROMPT_VERSION}"
        self.calls: list[JudgeCallRecord] = []

    def __repr__(self):
        return f"OpenRouterJudge(judge_id={self.judge_id!r})"

    def _messages(self, claim_text: str, evidence_texts: list[str]) -> list[dict[str, str]]:
        blocks = "\n\n".join(f"EVIDENCE [{i}]:\n{text}" for i, text in enumerate(evidence_texts, 1))
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"CLAIM:\n{claim_text}\n\n{blocks}"}]
        if len(json.dumps(messages, ensure_ascii=False)) > 20_000:
            raise JudgePromptTooLargeError()
        return messages

    def judge(self, claim_text: str, evidence_texts: list[str]) -> JudgeResult:
        secret = self._settings.openrouter_api_key
        if secret is None or not secret.get_secret_value():
            self.calls.append(JudgeCallRecord(self.judge_id, self.config.model_id,
                                             reasoning_effort=self.config.reasoning_effort,
                                             error_code="configuration_error"))
            raise ConfigurationError("OPENROUTER_API_KEY is required for the semantic judge.")
        messages = self._messages(claim_text, evidence_texts)
        prompt_hash = sha256(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        record = JudgeCallRecord(self.judge_id, self.config.model_id, reasoning_effort=self.config.reasoning_effort,
                                 prompt_sha256=prompt_hash)
        self.calls.append(record)
        schema = {"type": "object", "properties": {"label": {"type": "string", "enum": ["entailed", "partial", "not_entailed"]},
                 "score": {"type": "number", "minimum": 0, "maximum": 1}, "rationale": {"type": "string"}},
                 "required": ["label", "score", "rationale"], "additionalProperties": False}
        body = {"model": self.config.model_id, "messages": messages,
                "reasoning": {"effort": self.config.reasoning_effort, "exclude": True},
                "provider": self.config.provider_routing, "seed": self.config.seed,
                "response_format": {"type": "json_schema", "json_schema": {"name": "semantic_judgment", "strict": True, "schema": schema}},
                "usage": {"include": True}}
        started = time.monotonic()
        response = None
        for attempt in range(self.config.max_attempts):
            try:
                with httpx.Client(transport=self._transport, timeout=self.config.timeout_s) as client:
                    response = client.post(self.config.base_url.rstrip("/") + "/chat/completions", json=body,
                                           headers={"Authorization": f"Bearer {secret.get_secret_value()}"})
                if response.status_code in (401, 403):
                    record.error_code = "judge_auth_error"; record.latency_s = time.monotonic() - started
                    raise JudgeAuthError()
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt + 1 < self.config.max_attempts:
                        self._sleep(0.1 * (2 ** attempt)); continue
                    record.error_code = "judge_unavailable"; record.latency_s = time.monotonic() - started
                    raise JudgeUnavailableError()
                if not 200 <= response.status_code < 300:
                    record.error_code = "judge_unavailable"; record.latency_s = time.monotonic() - started
                    raise JudgeUnavailableError()
                break
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt + 1 < self.config.max_attempts:
                    self._sleep(0.1 * (2 ** attempt)); continue
                record.error_code = "judge_unavailable"; record.latency_s = time.monotonic() - started
                raise JudgeUnavailableError() from None
        try:
            data = response.json()
            record.observed_model = data.get("model")
            record.provider = data.get("provider")
            record.generation_id = data.get("id")
            usage = data.get("usage") or {}
            record.prompt_tokens = usage.get("prompt_tokens")
            record.completion_tokens = usage.get("completion_tokens")
            record.reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
            record.cost_usd = usage.get("cost")
            if record.observed_model != self.config.model_id: raise JudgeModelMismatchError()
            expected_route = (self.config.provider_routing.get("order") or [None])[0]
            if record.provider != expected_route: raise JudgeRouteMismatchError()
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop": raise JudgeSchemaError()
            parsed = _Output.model_validate_json(choice["message"]["content"])
            result = JudgeResult.model_validate(parsed.model_dump())
        except (JudgeModelMismatchError, JudgeRouteMismatchError, JudgeSchemaError):
            record.error_code = "judge_model_mismatch" if record.observed_model != self.config.model_id else ("judge_route_mismatch" if record.provider != (self.config.provider_routing.get("order") or [None])[0] else "judge_schema_error")
            raise
        except (ValueError, KeyError, IndexError, TypeError, ValidationError):
            record.error_code = "judge_schema_error"
            raise JudgeSchemaError() from None
        record.latency_s = time.monotonic() - started
        record.label, record.score, record.rationale = result.label, result.score, result.rationale
        return result
