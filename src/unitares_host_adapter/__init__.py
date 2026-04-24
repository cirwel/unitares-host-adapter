"""UNITARES host adapter — mount governance into any MCP-capable agent host."""

from unitares_host_adapter.core import UnitaresAdapter
from unitares_host_adapter.types import (
    AnnotatedResult,
    BlockDirective,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "UnitaresAdapter",
    "Verdict",
    "BlockDirective",
    "AnnotatedResult",
    "__version__",
]
