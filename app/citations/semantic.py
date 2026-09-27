"""Scripted semantic boundary. No model, network, or entailment heuristics here."""

from collections.abc import Mapping
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.domain.errors import JudgeNotScriptedError


class JudgeResult(BaseModel):
    label: Literal["entailed", "partial", "not_entailed"]
    score: float = Field(ge=0, le=1)
    rationale: str


class SemanticJudge(Protocol):
    judge_id: str

    def judge(self, claim_text: str, evidence_texts: list[str]) -> JudgeResult: ...


class ScriptedJudge:
    judge_id = "scripted-fake/1"

    def __init__(self, script: Mapping[str | tuple[str, tuple[str, ...]], JudgeResult]):
        self.script = dict(script)
        self._evidence_ids: tuple[str, ...] | None = None

    def bind_evidence_ids(self, evidence_ids: list[str]) -> None:
        """Verifier side channel to disambiguate equal text with different citations."""
        self._evidence_ids = tuple(evidence_ids)

    def judge(self, claim_text: str, evidence_texts: list[str]) -> JudgeResult:
        key = (claim_text, self._evidence_ids) if self._evidence_ids is not None else claim_text
        try:
            return self.script[key]
        except KeyError as exc:
            raise JudgeNotScriptedError(claim_text) from exc
