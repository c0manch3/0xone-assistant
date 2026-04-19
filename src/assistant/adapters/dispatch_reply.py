"""Shared reply-dispatcher for media-aware delivery (phase 7, commit 6).

`dispatch_reply` is the single seam through which the daemon converts a
model/worker/scheduler-produced text reply into actual Telegram traffic.
It lives on the adapter layer (not inside `TelegramAdapter`) because
THREE different call-sites need it:

  1. `TelegramAdapter._on_text`         — owner-initiated turn final reply.
  2. `SchedulerDispatcher._deliver`     — scheduled trigger materialisation.
  3. `subagent/hooks.py::on_subagent_stop` — worker subagent completion.

All three paths can see the same artefact path (e.g. main-turn mentions
`<outbox>/abc.png` while the worker is still finishing — the worker's
`SubagentStop` hook re-sends the same path moments later). The shared
`_DedupLedger` (§2.6, invariant I-7.5) keyed on
`(resolved_path_str, chat_id)` with a 300 s TTL + LRU cap of 256 entries
guarantees at-most-once network delivery in the double-delivery race
window without coordination between the three call-sites — all they
share is one `_DedupLedger` instance hanging off `Daemon._dedup_ledger`.

Flow per call:
    1. Scan `text` with `ARTEFACT_RE` (media/artefacts, v3 from S-2 spike).
    2. For each candidate:
       a. `Path(raw).resolve()` — `OSError`/`ValueError` → skip.
       b. Path-guard: `is_relative_to(outbox_root_resolved)` AND `exists()`
          (pitfall #10 — both checks are load-bearing; regex v3 can match
           `/abs/outbox/x.png` from `/abs/outbox/x.png/y`).
       c. `classify_artefact` (photo/audio/document).
       d. `dedup.mark_and_check` → if SEEN: skip network send, still
          strip the raw token from `cleaned`.
       e. Else: await the appropriate `adapter.send_*`. Wrap each send in
          `try/except Exception` (L-20): a bad file read surfaces as a
          `log.warning("artefact_send_failed", ...)` and we CONTINUE —
          remaining artefacts + the cleaned text still go out. The
          artefact file stays in outbox so the sweeper cleans it later.
    3. Send `cleaned.strip()` via `adapter.send_text` iff non-empty.

Non-goals:
  * `dispatch_reply` is NOT path-building — it never constructs outbox
    paths. The model (via SKILL.md §4.5) emits absolute paths; the
    sub-agent tools (`tools/genimage`, `tools/render_doc`) write to
    `<outbox>` directly.
  * `dispatch_reply` does NOT re-route telegram rate-limit errors — the
    `send_*` methods own `TelegramRetryAfter` retries. We simply swallow
    other `Exception`s to keep the text path alive.

See `plan/phase7/implementation.md` §2.6 for the canonical spec and
§0 pitfalls #9/#10 for the dedup-and-exists rationale.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from assistant.adapters.base import MessengerAdapter
from assistant.logger import get_logger
from assistant.media.artefacts import ARTEFACT_RE, classify_artefact

log = get_logger("adapters.dispatch_reply")

# Ledger defaults — match plan §2.6 verbatim. TTL rationale (L-19,
# Q-v2-2): 300 s covers the expected double-delivery window (main turn
# final text + worker SubagentStop completion within ~seconds for fast
# tools, ~minutes for slow LLM image jobs). Upper bound validated by
# phase-7 spike-findings; revisit in phase-8 telemetry.
_DEDUP_TTL_S: float = 300.0
# Max 256 entries × ~100 B/entry = ~25 KB RAM ceiling, per-daemon.
_DEDUP_MAX_ENTRIES: int = 256


class _DedupLedger:
    """Per-daemon in-process LRU + TTL ledger for dispatch_reply.

    Key: ``(resolved_path_str, chat_id)``. Value: monotonic timestamp
    of the last ``mark_and_check`` call.

    ``mark_and_check(key, now)``:
      * If the key was recorded within the last ``ttl_s`` seconds:
        return ``True`` (caller SKIPS the network send), refresh the
        LRU position so it moves to the end.
      * Else: record the key, LRU-trim to ``max_entries``, return
        ``False``.

    The caller injects ``now`` (a monotonic float) rather than having
    the ledger call ``time.monotonic()`` internally — this keeps the
    class deterministic and testable under a mock clock (H-12
    authoritative variant) without resorting to ``freezegun`` or
    ``unittest.mock.patch('time.monotonic')`` tricks.

    Side-effects:
      * ``_evict_expired(now)`` runs on every call (O(n) worst case,
        bounded by ``max_entries=256``). Cheap enough at 256 keys to
        not warrant an expiry heap.
      * LRU trim is ``O(k)`` where ``k`` is the excess over cap; in
        steady state ``k<=1`` because we trim after each insertion.
    """

    def __init__(
        self,
        *,
        ttl_s: float = _DEDUP_TTL_S,
        max_entries: int = _DEDUP_MAX_ENTRIES,
    ) -> None:
        if ttl_s <= 0.0:
            raise ValueError(f"ttl_s must be positive, got {ttl_s!r}")
        if max_entries <= 0:
            raise ValueError(
                f"max_entries must be positive, got {max_entries!r}"
            )
        self._entries: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._ttl_s = ttl_s
        self._max_entries = max_entries

    def mark_and_check(self, key: tuple[str, int], now: float) -> bool:
        """Record ``key`` at timestamp ``now``; return True iff recently seen.

        Semantics:
          * First call for ``key`` → record, return ``False``.
          * Repeat within TTL → refresh LRU, return ``True``.
          * Repeat after TTL   → record afresh, return ``False``.

        ``now`` MUST be monotonic (ie ``time.monotonic()`` or a mock
        float driven by the test). Wall-clock ``time.time()`` would
        break under NTP slew (phase-5 scheduler precedent).

        Fix-pack I2: the inner TTL re-check is unreachable because
        ``_evict_expired(now)`` has already purged every entry older
        than ``now - ttl_s``. If ``key`` survives the eviction pass,
        by construction ``now - self._entries[key] < self._ttl_s``
        so the membership test alone is authoritative.
        """
        self._evict_expired(now)
        if key in self._entries:
            # Post-eviction: a present key is provably within the TTL
            # window (_evict_expired dropped everyone older). Refresh
            # LRU position so subsequent trims evict truly-oldest first.
            self._entries.move_to_end(key)
            return True
        # Record (insertion order on a fresh key; move_to_end covers
        # the update case identically).
        self._entries[key] = now
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            # `popitem(last=False)` pops the LRU (front-of-OrderedDict).
            self._entries.popitem(last=False)
        return False

    def _evict_expired(self, now: float) -> None:
        # Walk a snapshot of keys to avoid "mutated during iteration".
        # 256-entry ceiling keeps this cheap.
        expired = [
            k for k, t in self._entries.items() if now - t >= self._ttl_s
        ]
        for k in expired:
            del self._entries[k]


async def dispatch_reply(
    adapter: MessengerAdapter,
    chat_id: int,
    text: str,
    *,
    outbox_root: Path,
    dedup: _DedupLedger,
    log_ctx: dict[str, Any] | None = None,
) -> None:
    """Extract artefacts from ``text``, deliver them, send cleaned text.

    Parameters:
      * ``adapter`` — the `MessengerAdapter`; MUST implement
        ``send_photo`` / ``send_document`` / ``send_audio`` / ``send_text``.
      * ``chat_id`` — destination chat.
      * ``text`` — model/worker/scheduler reply body.
      * ``outbox_root`` — `<data_dir>/media/outbox/`. `resolve()`d
        internally for robust `is_relative_to` comparison (symlinks,
        ``..`` components).
      * ``dedup`` — shared `_DedupLedger`. Per-daemon singleton.
      * ``log_ctx`` — optional extra structlog key/value pairs merged
        into every log line from this call (e.g. ``{"trigger_id": 12}``
        at the scheduler call-site, ``{"job_id": "x"}`` at the subagent
        hook call-site).

    Invariants preserved:
      * I-7.5 — idempotent over `(resolved_path, chat_id)` within the
        ledger TTL. Repeat calls clean the text the same way; only the
        first call actually sends bytes.
      * L-20  — a failed `send_*` never aborts the remaining work;
        text is ALWAYS attempted at the end.
      * Path-guard — every send target is both under `outbox_root`
        AND exists on disk (pitfall #10).
    """

    ctx = dict(log_ctx or {})
    cleaned = text

    # Resolve the outbox root once so we don't pay the symlink
    # traversal on every artefact. We DO NOT require the root to
    # exist here — `ensure_media_dirs` runs at Daemon startup; if
    # something nuked it between startup and this call, each
    # per-artefact `is_relative_to` still works (resolve() of a
    # non-existent path is path-only, not an fs check).
    outbox_resolved = outbox_root.resolve()

    # `findall` returns the single capture-group contents per match
    # (the whole artefact path string). Order is left-to-right as
    # emitted by the model.
    for raw in ARTEFACT_RE.findall(text):
        # Path resolution can fail on weird inputs; defensively skip
        # rather than let the whole dispatch crash on one bad match.
        try:
            resolved = Path(raw).resolve()
        except (OSError, ValueError) as exc:
            log.debug(
                "artefact_resolve_failed",
                raw=raw,
                chat_id=chat_id,
                error=repr(exc),
                **ctx,
            )
            continue

        # Pitfall #10: BOTH `is_relative_to` AND `exists()` are needed.
        # * `is_relative_to` alone would let `/abs/outbox/x.png/y` through
        #   as a match for `/abs/outbox/x.png` (regex v3 extracts the
        #   prefix; the "y" is trailing noise).
        # * `exists()` alone would not stop a path-traversal attempt
        #   that happens to point at a real file outside outbox.
        try:
            inside_outbox = resolved.is_relative_to(outbox_resolved)
        except ValueError:
            # `is_relative_to` pre-3.12 raised ValueError rather than
            # returning False; keep the belt-and-braces for safety.
            inside_outbox = False
        if not inside_outbox:
            log.debug(
                "artefact_outside_outbox",
                raw=raw,
                resolved=str(resolved),
                outbox_root=str(outbox_resolved),
                chat_id=chat_id,
                **ctx,
            )
            continue
        if not resolved.exists():
            log.debug(
                "artefact_missing",
                raw=raw,
                resolved=str(resolved),
                chat_id=chat_id,
                **ctx,
            )
            continue

        # Classify — raises ValueError on unknown ext, which would mean
        # ARTEFACT_RE and the classifier extension tables drifted. Treat
        # as a hard programmer error: surface via warning and skip.
        try:
            kind = classify_artefact(resolved)
        except ValueError as exc:
            log.warning(
                "artefact_classify_failed",
                raw=raw,
                resolved=str(resolved),
                chat_id=chat_id,
                error=repr(exc),
                **ctx,
            )
            continue

        key: tuple[str, int] = (str(resolved), int(chat_id))
        # Inject the clock into the ledger (not the other way round)
        # so tests can freeze time via a mock ledger while this function
        # still talks to a real `time.monotonic()`. Per plan §2.6 +
        # H-12: mock-clock variant is authoritative.
        if dedup.mark_and_check(key, time.monotonic()):
            # I-7.5: already delivered within the dedup window. Don't
            # re-send, but DO strip the raw token from the cleaned
            # text — the user should not see a bare absolute path in
            # the follow-up message.
            log.info(
                "artefact_send_deduped",
                resolved=str(resolved),
                chat_id=chat_id,
                kind=kind,
                **ctx,
            )
            cleaned = cleaned.replace(raw, "")
            continue

        try:
            if kind == "photo":
                await adapter.send_photo(chat_id, resolved)
            elif kind == "audio":
                await adapter.send_audio(chat_id, resolved)
            elif kind == "document":
                await adapter.send_document(chat_id, resolved)
            else:  # pragma: no cover - classify_artefact enumerates exhaustively
                log.warning(
                    "artefact_unknown_kind",
                    resolved=str(resolved),
                    chat_id=chat_id,
                    kind=kind,
                    **ctx,
                )
                continue
            # Only strip on successful send so a retried dispatch (say
            # from a transient crash between here and the text send)
            # does not silently drop the artefact reference from the
            # text if the ledger entry gets pruned.
            cleaned = cleaned.replace(raw, "")
        except Exception:  # noqa: BLE001 — intentional: see docstring
            # L-20: any `send_*` failure (FileNotFoundError from a
            # concurrent sweeper unlink, Telegram 500, CancelledError
            # from shielded-but-cancelled caller, etc.) is logged and
            # swallowed. The artefact path is NOT stripped: user sees
            # the raw path, can retry/re-prompt the assistant. The
            # text cleaned-send below still fires so the user gets
            # SOMETHING rather than total silence.
            log.warning(
                "artefact_send_failed",
                resolved=str(resolved),
                chat_id=chat_id,
                kind=kind,
                exc_info=True,
                **ctx,
            )

    cleaned_stripped = cleaned.strip()
    if cleaned_stripped:
        await adapter.send_text(chat_id, cleaned_stripped)


__all__ = [
    "_DedupLedger",
    "dispatch_reply",
]
