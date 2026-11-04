"""Ferry's decisions, with no git, age or share in sight — each one readable, and
testable, on its own. The engine gathers the facts and acts on the answer."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .manifest import Epoch, Manifest

MB = 1048576

FULL = "full"            # one pack of everything, replacing the chain
RESTART = "restart"      # a new base cut where other machines already are, then a delta
EXTEND = "extend"        # one more delta on the chain


def choose_push(old: Manifest | None, *, manifest_exists: bool, present: set[str], recipient: str,
                maxchain: int, history_kept: bool, epoch: Epoch | None, compacted: str) -> tuple[str, str]:
    """What a push writes, and why.

    A push appends one delta pack: the commits this slot doesn't hold yet. A
    chain of deltas never holds anything twice, so its size needs no cap;
    maxchain only bounds how many files a new machine reads.

    When the chain can't or shouldn't be extended — it reached maxchain, a pack
    went missing, the key changed — the slot restarts from a new base, cut where
    other machines already are rather than at the current state. Only when this
    slot's published state is gone (history rewritten, or compacted) or unknown
    is the base the current state, costing readers a full download.

    old is this slot's manifest, None if absent or unreadable; present, the pack
    files the slot still has; history_kept, whether every published tip is still
    a local commit; epoch, the compaction this repo is on here.
    """
    if old is None:
        return FULL, "its manifest is unreadable — a new key?" if manifest_exists else "first push"
    kind, why = EXTEND, ""
    if any(p.name not in present for p in old.packs):
        kind, why = RESTART, "a pack went missing"
    if old.recipient != recipient:
        kind, why = RESTART, "new key"
    if len(old.packs) >= maxchain:
        kind, why = RESTART, f"the chain reached {maxchain} packs"
    if not old.published or not old.packs:
        kind, why = FULL, "its manifest was empty"
    if not history_kept:
        kind, why = FULL, "history was rewritten"
    if old.epoch.epoch != epoch:
        kind, why = FULL, f"compacted — {compacted} of old versions dropped" if compacted else "moved onto compacted history"
    return kind, why


def should_compact(*, dead: int, live: int, threshold_mb: int) -> bool:
    """Compact when old versions pass the threshold and a quarter of the current
    files: each machine then re-uploads its copy once, so less isn't worth it."""
    return dead >= threshold_mb * MB and dead >= live // 4


def peer_epoch(theirs: int, mine: int, newest: int, same_line: bool) -> str:
    """How to treat another machine's copy of an auto repo, by the compaction it's on.

    behind  an older one than the newest known: left alone, not merged, since
            that machine brings its changes across itself
    split   the same number as here but a different root: two machines compacted
            separately, and their histories must never be merged
    ahead   a newer one: this machine moves onto it (adopt) once it holds it
    same    the same one: fetched and merged as usual
    """
    if theirs < newest:
        return "behind"
    if theirs == mine and not same_line:
        return "split"
    if theirs > mine:
        return "ahead"
    return "same"


def newest_epoch(candidates: Iterable[tuple[str, int]], floor: int) -> tuple[str | None, int]:
    """The first (host, epoch) past floor with the highest epoch; (None, floor) if none."""
    best, top = None, floor
    for host, number in candidates:
        if number > top:
            best, top = host, number
    return best, top


@dataclass
class Budget:
    """How much a session start downloads before leaving the rest to the background.
    remain is None when there is no limit."""
    remain: int | None = None
    deferred: list[str] = field(default_factory=list)
    deferred_bytes: int = 0

    def take(self, need: int, what: str) -> bool:
        if self.remain is None:
            return True
        if need > self.remain:
            self.deferred.append(what)
            self.deferred_bytes += need
            return False
        self.remain -= need
        return True
