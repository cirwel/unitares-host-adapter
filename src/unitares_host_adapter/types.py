"""Shared types for the UNITARES host adapter surface."""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Action = Literal["proceed", "guide", "pause", "reject"]
ResponseMode = Literal["minimal", "compact", "standard", "full", "auto", "lite"]


@dataclass(frozen=True)
class Verdict:
    """A governance verdict returned from an explicit check-in."""

    action: Action
    message: str = ""
    margin: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.action in ("pause", "reject")


@dataclass(frozen=True)
class BlockDirective:
    """A block directive returned from the gated delivery mode.

    Shape matches the Hermes pre_tool_call contract:
        {"action": "block", "message": "<reason>"}
    """

    message: str

    def as_dict(self) -> dict[str, str]:
        return {"action": "block", "message": self.message}


@dataclass(frozen=True)
class AnnotatedResult:
    """A tool result annotated with UNITARES proprioceptive state (ambient mode)."""

    result: Any
    annotation: str

    def render(self) -> str:
        """Render the annotated result for injection back into the agent's tool-result stream."""
        if not self.annotation:
            return str(self.result)
        return f"{self.result}\n\n---\n[UNITARES] {self.annotation}"
