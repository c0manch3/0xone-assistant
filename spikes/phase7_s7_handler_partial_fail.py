"""Phase 7 spike S-7 — handler partial-attachment failure (devil Gap #8).

Characterize handler behaviour when a multi-attachment turn has one
attachment fail mid-processing. Per detailed-plan §6.1:

    if att.kind == "photo" and photo_mode == "inline_base64":
        b64 = base64.b64encode(att.local_path.read_bytes()).decode()
        image_blocks.append(...)
        notes.append(...)

If attachment #2's `read_bytes()` raises (FileNotFoundError because
sweeper unlinked it, or OSError for IO err), the current pseudo-code
would crash the whole turn.

We want:
  * image_blocks contains successfully-processed attachments
  * notes contains ALL attachments (with a failure-note in place of the
    bad one) — so the model understands what was attempted
  * no envelope-level crash
  * logging of the failure

Simulate with 3 MediaAttachment-shaped objects; inject IOError on #2.
Implement the "safe" handler pseudo-code proposed below and verify.

Run:  uv run python spikes/phase7_s7_handler_partial_fail.py
"""

from __future__ import annotations

import base64
import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s7_report.json"


@dataclass(frozen=True)
class MediaAttachment:
    kind: Literal["voice", "photo", "document", "audio", "video_note"]
    local_path: Path
    mime_type: str | None = None
    file_size: int | None = None
    duration_s: int | None = None
    width: int | None = None
    height: int | None = None
    filename_original: str | None = None


def build_envelope_safe(
    attachments: list[MediaAttachment],
    *,
    photo_mode: str = "inline_base64",
    photo_cap_bytes: int = 5 * 1024 * 1024,
    log_warn: list[dict],
) -> tuple[list[dict], list[str]]:
    """Safe version of the plan §6.1 pseudocode.

    Returns (image_blocks, notes). Never raises on a single-attachment
    failure; pushes a failure-note instead.
    """
    image_blocks: list[dict] = []
    notes: list[str] = []
    for idx, att in enumerate(attachments):
        if att.kind == "photo" and photo_mode == "inline_base64":
            if att.file_size is not None and att.file_size > photo_cap_bytes:
                notes.append(
                    f"user attached photo at {att.local_path} "
                    f"but size {att.file_size} exceeds inline cap {photo_cap_bytes}; skipped."
                )
                continue
            try:
                raw = att.local_path.read_bytes()
            except (FileNotFoundError, PermissionError, OSError) as exc:
                notes.append(
                    f"user attempted to attach photo at {att.local_path} "
                    f"but read failed: {type(exc).__name__}."
                )
                log_warn.append({
                    "kind": att.kind,
                    "path": str(att.local_path),
                    "error": repr(exc),
                    "index": idx,
                })
                continue
            mime = att.mime_type or "image/jpeg"
            b64 = base64.b64encode(raw).decode("ascii")
            image_blocks.append(
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
            )
            notes.append(
                f"user attached photo at {att.local_path} ({att.width}x{att.height})"
            )
        elif att.kind in ("voice", "audio"):
            # Paths get passed to CLI via system-note; no read_bytes() here
            # so failure-mode is the CLI's problem. Document only.
            notes.append(
                f"user attached {att.kind} (duration={att.duration_s}s) at {att.local_path}."
            )
        elif att.kind == "document":
            notes.append(
                f"user attached document '{att.filename_original}' at {att.local_path}."
            )
        elif att.kind == "video_note":
            notes.append(
                f"user attached video_note (duration={att.duration_s}s) at {att.local_path}."
            )
        else:
            notes.append(f"unknown attachment kind={att.kind!r}")
    return image_blocks, notes


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="phase7_s7_"))
    findings: dict[str, object] = {}

    # Build 3 attachments: photo #1 exists, photo #2 MISSING, photo #3 exists
    p1 = tmp / "photo1.jpg"
    p2 = tmp / "photo2.jpg"  # never created
    p3 = tmp / "photo3.jpg"
    p1.write_bytes(b"\xff\xd8\xff\xe0" + b"A" * 100 + b"\xff\xd9")
    p3.write_bytes(b"\xff\xd8\xff\xe0" + b"C" * 100 + b"\xff\xd9")
    # p2 does NOT exist → FileNotFoundError

    attachments = [
        MediaAttachment(kind="photo", local_path=p1, mime_type="image/jpeg", file_size=108, width=64, height=64),
        MediaAttachment(kind="photo", local_path=p2, mime_type="image/jpeg", file_size=108, width=64, height=64),
        MediaAttachment(kind="photo", local_path=p3, mime_type="image/jpeg", file_size=108, width=64, height=64),
    ]

    warns: list[dict] = []
    image_blocks, notes = build_envelope_safe(attachments, log_warn=warns)

    findings["scenario"] = "3_photos_middle_missing"
    findings["image_blocks_count"] = len(image_blocks)
    findings["notes_count"] = len(notes)
    findings["warns_recorded"] = warns
    findings["notes"] = notes

    ok_shape = (
        len(image_blocks) == 2
        and len(notes) == 3
        and "read failed" in notes[1]
        and len(warns) == 1
        and warns[0]["index"] == 1
    )
    findings["shape_ok"] = ok_shape

    # Secondary scenario: one photo oversize
    p_big = tmp / "big.jpg"
    p_big.write_bytes(b"\xff\xd8" + b"X" * 1024 + b"\xff\xd9")

    attachments2 = [
        MediaAttachment(
            kind="photo", local_path=p_big, mime_type="image/jpeg",
            file_size=20 * 1024 * 1024, width=4000, height=4000,
        ),
        MediaAttachment(
            kind="photo", local_path=p1, mime_type="image/jpeg",
            file_size=108, width=64, height=64,
        ),
    ]
    warns2: list[dict] = []
    ib2, nt2 = build_envelope_safe(attachments2, log_warn=warns2)
    findings["oversize_scenario"] = {
        "image_blocks_count": len(ib2),
        "notes_count": len(nt2),
        "first_note_has_skip": "exceeds inline cap" in nt2[0],
        "second_image_present": len(ib2) == 1,
    }

    # Mixed-kind: photo + voice + document, photo fails
    p_miss = tmp / "missing_photo.jpg"
    p_voice = tmp / "voice.oga"  # we don't read this
    p_doc = tmp / "doc.pdf"
    attachments3 = [
        MediaAttachment(kind="photo", local_path=p_miss, mime_type="image/jpeg", file_size=108, width=64, height=64),
        MediaAttachment(kind="voice", local_path=p_voice, duration_s=12),
        MediaAttachment(kind="document", local_path=p_doc, filename_original="contract.pdf"),
    ]
    warns3: list[dict] = []
    ib3, nt3 = build_envelope_safe(attachments3, log_warn=warns3)
    findings["mixed_kind_scenario"] = {
        "image_blocks_count": len(ib3),
        "notes_count": len(nt3),
        "failure_note_present_for_photo": "read failed" in nt3[0],
        "voice_note_clean": "voice" in nt3[1],
        "doc_note_clean": "contract.pdf" in nt3[2],
    }

    # Final verdict
    all_ok = (
        findings["shape_ok"]
        and findings["oversize_scenario"]["first_note_has_skip"]
        and findings["oversize_scenario"]["second_image_present"]
        and findings["mixed_kind_scenario"]["failure_note_present_for_photo"]
        and findings["mixed_kind_scenario"]["voice_note_clean"]
        and findings["mixed_kind_scenario"]["doc_note_clean"]
    )
    findings["verdict"] = "PASS" if all_ok else "PARTIAL"

    REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False, default=str))
    print(f"verdict: {findings['verdict']}")
    print(f"scenario 1 shape_ok: {findings['shape_ok']}")
    print(f"scenario 1 notes: {notes}")
    print(f"Report -> {REPORT}")


if __name__ == "__main__":
    main()
