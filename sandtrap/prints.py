"""Print values rendered as they are printed.

``snapshot_prints`` deep-copies every print's arguments and hands the
copies back, so a host can render them later; a printed 200MB table is
copied, and out of a worker pickled and sent, whatever the host does with
it. :class:`PrintRendering` renders each print with reprobate at the
moment it is made instead, into a string of bounded length: nothing is
copied, and what crosses is text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["PrintRendering"]


@dataclass(frozen=True)
class PrintRendering:
    """Render each print's values with reprobate as the print is made.

    ``per_print`` is how many characters one print's rendering may take;
    its values share it, each getting at least 40. ``limit`` is how many
    prints are rendered: later ones are only counted
    (``ExecResult.print_count``), so the work and the memory are bounded
    however much the code prints.

    Needs reprobate (``pip install 'sandtrap[render]'``).
    """

    per_print: int = 2000
    limit: int = 200

    def __post_init__(self) -> None:
        for name in ("per_print", "limit"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"PrintRendering.{name} must be a positive int")


def require_reprobate() -> None:
    """Import reprobate, or say how to get it."""
    try:
        import reprobate  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "render_prints renders with reprobate, which is not installed: "
            "pip install 'sandtrap[render]'"
        ) from exc


class PrintRenderer:
    """One execution's renderings. ``append`` takes a print's arguments,
    as a snapshot list would."""

    def __init__(self, rendering: PrintRendering) -> None:
        self._rendering = rendering
        self.rendered: list[str] = []
        self.count = 0

    def append(self, args: tuple[Any, ...]) -> None:
        self.count += 1
        if len(self.rendered) >= self._rendering.limit:
            return
        per_value = max(40, self._rendering.per_print // max(1, len(args)))
        self.rendered.append(" ".join(_render(value, per_value) for value in args))


def _render(value: Any, budget: int) -> str:
    import reprobate

    try:
        return reprobate.render(value, budget=budget)
    except Exception:
        # A value whose own methods raise while being looked at: name its
        # type rather than fail the print.
        return f"<{type(value).__name__}>"


class Fanout:
    """Appends to each of several print sinks that exist."""

    def __init__(self, *sinks: Any) -> None:
        self._sinks = [s for s in sinks if s is not None]

    def append(self, args: tuple[Any, ...]) -> None:
        for sink in self._sinks:
            sink.append(args)
