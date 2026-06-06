from __future__ import annotations

from typing import Protocol, Sequence

from bemine.providers.types import NormalizedResponse


class LLMClient(Protocol):
    """Bemine's seam onto the vendored provider transports. One call = one model turn."""

    def complete(
        self,
        *,
        messages: Sequence[dict],
        tools: Sequence[dict],
        model: str,
        system: str,
    ) -> NormalizedResponse: ...
