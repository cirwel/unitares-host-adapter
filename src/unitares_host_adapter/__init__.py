"""UNITARES host adapter — mount governance into any MCP-capable agent host."""

from unitares_host_adapter.core import UnitaresAdapter
from unitares_host_adapter.transport import StreamableHTTPTransport, TransportError
from unitares_host_adapter.types import (
    AnnotatedResult,
    BlockDirective,
    LocalConfigError,
    MissingServerURLError,
    ServerRejectedHostError,
    Verdict,
)

__version__ = "0.4.0a1"

__all__ = [
    "UnitaresAdapter",
    "StreamableHTTPTransport",
    "TransportError",
    "LocalConfigError",
    "MissingServerURLError",
    "ServerRejectedHostError",
    "Verdict",
    "BlockDirective",
    "AnnotatedResult",
    "__version__",
]
