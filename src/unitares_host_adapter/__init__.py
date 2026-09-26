"""UNITARES host adapter — mount governance into any MCP-capable agent host."""

from unitares_host_adapter.core import UnitaresAdapter
from unitares_host_adapter.transport import StreamableHTTPTransport, TransportError
from unitares_host_adapter.types import (
    AnnotatedResult,
    BlockDirective,
    MissingServerURLError,
    Verdict,
)

__version__ = "0.3.1"

__all__ = [
    "UnitaresAdapter",
    "StreamableHTTPTransport",
    "TransportError",
    "MissingServerURLError",
    "Verdict",
    "BlockDirective",
    "AnnotatedResult",
    "__version__",
]
