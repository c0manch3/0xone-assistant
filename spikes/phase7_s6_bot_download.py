"""Phase 7 spike S-6 — aiogram Bot.download_file size semantics (devil Gap #9).

Characterize the cases we need to defend against in media/download.py:

  * `File.file_size == None` for some attachment kinds (video_note? voice?
    PHOTO from aiogram File API?). Our pre-flight size check can't rely
    solely on file.file_size — we need to handle None.

  * `bot.download_file(file_path, destination=...)` streaming abort —
    if the file is larger than expected, can we stop mid-download?

  * Telegram Bot API hard cap (getFile path): 20 MB. Files over 20 MB
    can't be downloaded via the Bot API regardless of aiogram.

Since we don't have a live Bot, we:
  * Inspect aiogram's File and Bot classes statically (attrs + signatures)
  * Test our defence pattern against an in-process mock Bot
  * Verify the size-check logic (pre-flight + byte counter) works when
    file_size is None

Run:  uv run python spikes/phase7_s6_bot_download.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s6_report.json"


async def main() -> None:
    findings: dict[str, Any] = {}

    # 1. Inspect aiogram's File dataclass / Bot.download_file signature.
    try:
        from aiogram import Bot
        from aiogram.types import File, Voice, VideoNote, Document, PhotoSize, Audio
    except ImportError as exc:
        findings["verdict"] = "FAIL_IMPORT"
        findings["error"] = f"{exc}"
        REPORT.write_text(json.dumps(findings, indent=2))
        return

    findings["aiogram_imported"] = True

    # File field types (file_size nullable?)
    file_fields = {}
    try:
        # aiogram uses pydantic; model_fields exposes required-ness
        for name, field in File.model_fields.items():
            annot = field.annotation
            # dump as string
            file_fields[name] = {
                "annotation": str(annot),
                "required": field.is_required(),
                "default": repr(getattr(field, "default", None))[:60],
            }
    except Exception as exc:  # noqa: BLE001
        file_fields["error"] = repr(exc)
    findings["File_fields"] = file_fields

    # Which media-related classes have file_size?
    for klass in (Voice, VideoNote, Document, PhotoSize, Audio):
        fs = None
        try:
            fs = klass.model_fields.get("file_size")
            fs_info = {
                "annotation": str(fs.annotation) if fs else None,
                "required": fs.is_required() if fs else None,
            }
        except Exception as exc:  # noqa: BLE001
            fs_info = {"error": repr(exc)}
        findings[f"{klass.__name__}_file_size_field"] = fs_info

    # 2. Inspect Bot.download_file signature
    try:
        sig = inspect.signature(Bot.download_file)
        findings["Bot_download_file_signature"] = str(sig)
    except Exception as exc:  # noqa: BLE001
        findings["Bot_download_file_signature_error"] = repr(exc)

    # 3. Mock Bot streaming abort: simulate a download that streams bytes
    #    and our wrapper aborts when over max_bytes.
    #
    #    Real aiogram uses aiohttp under the hood; the `destination`
    #    argument can be a BinaryIO or a filepath. For our purpose we
    #    simulate both: we wrap our own streaming-to-file logic with a
    #    byte-counter that raises if cap exceeded.

    class MockFile:
        def __init__(self, file_path: str, file_size: int | None = None):
            self.file_path = file_path
            self.file_size = file_size

    class MockBot:
        def __init__(self, chunks: list[bytes]):
            self._chunks = chunks

        async def get_file(self, file_id: str) -> MockFile:
            # simulate whatever file_size the server reports
            return self._file

        async def download_file(
            self,
            file_path: str,
            destination: Any = None,
            *,
            chunk_size: int = 65536,
        ) -> Any:
            # If `destination` is BytesIO or file object, write chunks into it.
            # Aiogram also supports None -> return bytes. We mimic both.
            if destination is None:
                out = bytearray()
                for c in self._chunks:
                    out.extend(c)
                return bytes(out)
            for c in self._chunks:
                destination.write(c)
            return destination

    # Defence pattern: pre-check file_size if known; if None OR known-small,
    # stream chunks into a size-tracking sink that raises on overrun.
    class SizeCapExceeded(Exception):
        pass

    class SizeCappedWriter:
        def __init__(self, dest, cap: int) -> None:
            self._dest = dest
            self._cap = cap
            self._written = 0

        def write(self, data: bytes) -> int:
            self._written += len(data)
            if self._written > self._cap:
                raise SizeCapExceeded(
                    f"download exceeded cap {self._cap} (received ~{self._written})"
                )
            return self._dest.write(data)

    async def safe_download(
        bot: MockBot, file: MockFile, dest_path: Path, *, max_bytes: int
    ) -> dict[str, Any]:
        # 1. Pre-flight size check (None-tolerant)
        if file.file_size is not None and file.file_size > max_bytes:
            return {"allowed": False, "reason": f"file_size {file.file_size} > cap {max_bytes}"}
        # 2. Stream into size-capped sink
        with dest_path.open("wb") as fp:
            sink = SizeCappedWriter(fp, max_bytes)
            try:
                await bot.download_file(file.file_path, destination=sink)
            except SizeCapExceeded as exc:
                return {"allowed": False, "reason": str(exc)}
        return {"allowed": True, "bytes": dest_path.stat().st_size}

    # 4. Test cases: file_size None; file_size known + over; streamed over
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="phase7_s6_"))

    # Case A: file_size None + small actual payload → should succeed
    mock = MockBot(chunks=[b"X" * 1024, b"Y" * 1024])
    mock._file = MockFile("file_A", file_size=None)
    case_a = await safe_download(mock, mock._file, tmp / "a.bin", max_bytes=10 * 1024)
    findings["case_a_None_size_small_payload"] = case_a

    # Case B: file_size None + payload overruns cap → should abort mid-stream
    mock = MockBot(chunks=[b"X" * 4096] * 100)  # ~400 KB
    mock._file = MockFile("file_B", file_size=None)
    case_b = await safe_download(mock, mock._file, tmp / "b.bin", max_bytes=100 * 1024)
    findings["case_b_None_size_overruns"] = case_b

    # Case C: file_size KNOWN + over cap → pre-flight reject before any bytes
    mock = MockBot(chunks=[b"Z" * 4096] * 50)
    mock._file = MockFile("file_C", file_size=5_000_000)  # 5MB
    case_c = await safe_download(mock, mock._file, tmp / "c.bin", max_bytes=100 * 1024)
    findings["case_c_known_oversize_preflight"] = case_c

    # Case D: file_size known + in-cap → succeed
    mock = MockBot(chunks=[b"W" * 4096])
    mock._file = MockFile("file_D", file_size=4096)
    case_d = await safe_download(mock, mock._file, tmp / "d.bin", max_bytes=100 * 1024)
    findings["case_d_known_in_cap"] = case_d

    # Verdicts
    a_ok = case_a.get("allowed") is True
    b_ok = case_b.get("allowed") is False and "exceeded" in case_b.get("reason", "").lower()
    c_ok = case_c.get("allowed") is False and "file_size" in case_c.get("reason", "")
    d_ok = case_d.get("allowed") is True

    findings["verdict"] = "PASS" if (a_ok and b_ok and c_ok and d_ok) else "PARTIAL"
    findings["telegram_bot_api_max_bytes_for_getFile"] = 20 * 1024 * 1024
    findings["recommendation"] = (
        "media/download.py MUST: (1) check file.file_size if set AND reject >cap, "
        "(2) stream via a SizeCappedWriter wrapper so None-sized attachments "
        "(video_note, voice in some aiogram versions) abort mid-download, "
        "(3) set media-caps BELOW Telegram's 20MB hard limit (voice 15MB, "
        "photo 10MB, doc 20MB = at-ceiling)."
    )

    REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
    print(f"verdict: {findings['verdict']}")
    print(f"case A: {case_a}")
    print(f"case B: {case_b}")
    print(f"case C: {case_c}")
    print(f"case D: {case_d}")
    print(f"Report -> {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
