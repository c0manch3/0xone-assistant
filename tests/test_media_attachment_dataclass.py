"""Phase 7 / commit 4 — MediaAttachment dataclass contract.

Tight, runtime-verifiable properties of the dataclass itself:
  1. frozen: mutation raises `FrozenInstanceError`.
  2. slots: instance has no `__dict__` (saves ~56 bytes per instance,
     relevant because Telegram media-group updates fan out to ≤10
     attachments per envelope + the tuple is stored on `IncomingMessage`
     for the turn's lifetime, potentially replayed from history).
  3. equality: value semantics hold (identical fields → equal; any
     differing field → not equal).
  4. kind validation: the dataclass does NOT enforce `Literal` at
     runtime (Python's dataclass machinery ignores type hints). The
     adapter is responsible for only constructing `MediaAttachment`
     with a valid `kind`; we test this invariant at the typing layer
     (mypy --strict on base.py is the authoritative check, run by the
     commit's verification step). Here we merely document via a
     smoke test that a bogus kind is constructible at runtime — this
     pins the behaviour so a future "add runtime validation via
     `__post_init__`" PR is a conscious decision rather than an
     accidental regression.
  5. `IncomingMessage.attachments` defaults to `None` (backward-compat
     with phase-5/6 construction sites).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from assistant.adapters.base import IncomingMessage, MediaAttachment


def _sample(**overrides: object) -> MediaAttachment:
    """Build a fully-populated `MediaAttachment` so equality tests can
    vary exactly one field at a time."""
    kwargs: dict[str, object] = dict(
        kind="photo",
        local_path=Path("/tmp/media/inbox/abc.jpg"),
        mime_type="image/jpeg",
        file_size=1024,
        duration_s=None,
        width=640,
        height=480,
        filename_original="cat.jpg",
        telegram_file_id="AgACAgIA...",
    )
    kwargs.update(overrides)
    return MediaAttachment(**kwargs)  # type: ignore[arg-type]


def test_frozen_rejects_attribute_mutation() -> None:
    att = _sample()
    with pytest.raises(FrozenInstanceError):
        # `frozen=True` routes __setattr__ to raise; this is the
        # contract the handler + dedup ledger rely on (the path is
        # captured by value, not reference).
        att.mime_type = "image/png"  # type: ignore[misc]


def test_slots_means_no_instance_dict() -> None:
    att = _sample()
    # slots=True removes `__dict__`; accessing it raises AttributeError.
    # This is load-bearing for the memory argument in the docstring.
    assert not hasattr(att, "__dict__")


def test_slots_rejects_dynamic_attribute() -> None:
    att = _sample()
    # A slotted frozen dataclass can't gain new attributes at runtime.
    # The exact exception class depends on CPython's implementation
    # detail: the generated frozen `__setattr__` does a `super().
    # __setattr__` which fails with a `TypeError` on unknown-slot
    # names before the frozen guard can raise `FrozenInstanceError`.
    # Either one is fine from the caller's perspective (mutation is
    # refused); we accept both so the test isn't coupled to CPython's
    # internal ordering.
    with pytest.raises((FrozenInstanceError, TypeError, AttributeError)):
        att.extra_field = "nope"  # type: ignore[attr-defined]


def test_equality_identical_instances() -> None:
    assert _sample() == _sample()


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("kind", "document"),
        ("local_path", Path("/tmp/media/inbox/xyz.jpg")),
        ("mime_type", "image/png"),
        ("file_size", 2048),
        ("duration_s", 5),
        ("width", 1024),
        ("height", 768),
        ("filename_original", "dog.jpg"),
        ("telegram_file_id", "DIFFERENT"),
    ],
)
def test_equality_varies_by_single_field(field: str, new_value: object) -> None:
    base = _sample()
    variant = _sample(**{field: new_value})
    assert base != variant, f"equality should vary on {field}"


def test_hashable() -> None:
    # frozen dataclasses without eq=False are hashable by default —
    # required so (resolved_path_str, chat_id) keys in `_DedupLedger`
    # can coexist with potential per-attachment caches downstream.
    att = _sample()
    d = {att: 1}
    assert d[_sample()] == 1


def test_invalid_kind_not_runtime_enforced() -> None:
    # Documents current behaviour: Python's `@dataclass` does NOT
    # enforce Literal types at runtime. Adapters are responsible for
    # only synthesising valid kinds; mypy --strict on base.py enforces
    # this statically at every construction site in the repo.
    # If a future change adds `__post_init__` validation, this test
    # SHOULD be updated to assert ValueError — the change is then a
    # conscious API tightening, not an accidental drift.
    att = MediaAttachment(kind="not_a_kind", local_path=Path("/tmp/x"))  # type: ignore[arg-type]
    assert att.kind == "not_a_kind"


def test_optional_fields_default_to_none() -> None:
    att = MediaAttachment(kind="voice", local_path=Path("/tmp/v.oga"))
    assert att.mime_type is None
    assert att.file_size is None
    assert att.duration_s is None
    assert att.width is None
    assert att.height is None
    assert att.filename_original is None
    assert att.telegram_file_id is None


def test_incoming_message_attachments_defaults_to_none() -> None:
    # Backward-compat: phase-5/6 construction sites call
    # `IncomingMessage(chat_id=..., text=...)` without knowing about
    # attachments. The `None` default (not `()`) makes the
    # "no attachments" vs "empty tuple" distinction observable if we
    # ever need it (e.g. scheduler-origin messages can never carry
    # attachments by design — None documents that intent).
    msg = IncomingMessage(chat_id=1, text="hi")
    assert msg.attachments is None


def test_incoming_message_accepts_attachments_tuple() -> None:
    att = _sample()
    msg = IncomingMessage(chat_id=1, text="look", attachments=(att,))
    assert msg.attachments == (att,)
    # Still frozen on the outer envelope.
    with pytest.raises(FrozenInstanceError):
        msg.attachments = None  # type: ignore[misc]
