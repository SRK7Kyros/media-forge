"""Confidence competition between plugins for one source root."""

from __future__ import annotations

from dataclasses import dataclass

from .plugin_api import DetectContext
from .plugin_loader import LoadedPlugin, safe_detect


class NoPluginError(RuntimeError):
    """No plugin cleared the floor, or an override named one that is not installed."""


@dataclass(frozen=True, slots=True)
class Candidate:
    plugin_id: str
    confidence: float
    reasons: tuple[str, ...]
    priority: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Selection:
    plugin_id: str
    plugin: object
    confidence: float
    reason: str
    candidates: tuple[Candidate, ...]
    overridden: bool

    def losers(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.plugin_id != self.plugin_id)


def _format_candidates(candidates: tuple[Candidate, ...]) -> str:
    lines = []
    for candidate in candidates:
        if candidate.error:
            lines.append(f"  {candidate.plugin_id}: error ({candidate.error})")
        else:
            detail = "; ".join(candidate.reasons) or "no reasons"
            lines.append(f"  {candidate.plugin_id}: {candidate.confidence:.2f} ({detail})")
    return "\n".join(lines) or "  (no plugins installed)"


def select(
    root_id: str,
    plugins: tuple[LoadedPlugin, ...],
    ctx: DetectContext,
    *,
    override: str | None = None,
    min_confidence: float = 0.60,
) -> Selection:
    """Pick the plugin with the highest confidence, or honour an explicit override."""
    candidates: list[Candidate] = []
    modules: dict[str, object] = {}
    for loaded in plugins:
        if not loaded.ok:
            candidates.append(Candidate(loaded.plugin_id, 0.0, (), 0, loaded.error))
            continue
        module = loaded.module
        detected, error = safe_detect(module, ctx)
        priority = int(getattr(module, "priority", 0))
        modules[loaded.plugin_id] = module
        if detected is None:
            candidates.append(Candidate(loaded.plugin_id, 0.0, (), priority, error))
            continue
        candidates.append(
            Candidate(loaded.plugin_id, detected.confidence, detected.reasons, priority, None)
        )
    ordered = tuple(sorted(candidates, key=lambda c: (-c.confidence, -c.priority, c.plugin_id)))

    if override:
        module = modules.get(override)
        if module is None:
            raise NoPluginError(
                f"MF_PLUGIN_OVERRIDE_{root_id.upper()}={override} names a plugin that is not "
                f"installed. Candidates:\n{_format_candidates(ordered)}"
            )
        chosen = next((c for c in ordered if c.plugin_id == override), None)
        confidence = chosen.confidence if chosen else 0.0
        reasons = chosen.reasons if chosen else ()
        return Selection(
            plugin_id=override,
            plugin=module,
            confidence=confidence,
            reason=f"override; scored {confidence:.2f} ({'; '.join(reasons) or 'no reasons'})",
            candidates=ordered,
            overridden=True,
        )

    best = ordered[0] if ordered else None
    if best is None or best.error is not None or best.confidence < min_confidence:
        raise NoPluginError(
            f"No plugin cleared MF_PLUGIN_MIN_CONFIDENCE={min_confidence:.2f} for root "
            f"'{root_id}'. Refusing to guess. Candidates:\n{_format_candidates(ordered)}"
        )
    return Selection(
        plugin_id=best.plugin_id,
        plugin=modules[best.plugin_id],
        confidence=best.confidence,
        reason="; ".join(best.reasons) or "highest confidence",
        candidates=ordered,
        overridden=False,
    )


def summarise(candidates: tuple[Candidate, ...]) -> str:
    return _format_candidates(candidates)
