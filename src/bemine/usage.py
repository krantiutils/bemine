from __future__ import annotations
from typing import Sequence


def render_usage(rollup: Sequence[dict]) -> str:
    if not rollup:
        return "No usage recorded."
    lines = ["**Token usage by provider**"]
    for r in rollup:
        lines.append(
            f"- {r['provider']}: {r['samples']} samples, "
            f"in {r['input_tokens']:,} / out {r['output_tokens']:,} / total {r['total_tokens']:,}"
        )
    return "\n".join(lines)
