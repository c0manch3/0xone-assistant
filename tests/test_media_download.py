"""Phase 7 / commit 5 — `src/assistant/media/download.py` contract.

Ports spike S-6 A/B/C/D cases to unit tests + covers the two
critical invariants from C-3 / pitfall #3:

  * `_SizeCappedWriter` implements BOTH `write(data: bytes) -> int`
    AND `flush() -> None` (aiogram 3.26's
    `__download_file_binary_io` calls both per chunk).

  * On streaming-cap violation (case B) or any other exception
    mid-download, the partially-written file is `unlink`'d.

S-6 case map (spike `spikes/phase7_s6_bot_download.py`):
  A — `file_size=None` + payload fits cap → SUCCESS
  B — `file_size=None` + payload overruns cap → SizeCapExceeded +
      partial file unlinked
  C — `file_size > cap` → pre-flight SizeCapExceeded before any
      bytes hit disk
  D — `file_size` known and within cap → SUCCESS
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, BinaryIO
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant.media.download import (
    SizeCapExceeded,
    _SizeCappedWriter,
    download_telegram_file,
)

# `timeout` is a signature keyword from aiogram's real `Bot.download_file`
# (not the stdlib `timeout=` coroutine helper). ASYNC109 flags look-alike
# pattern; we need the parameter to match aiogram's contract exactly.
# ruff: noqa: ASYNC109


# --- _SizeCappedWriter unit tests ----------------------------------


def test_writer_accepts_under_cap() -> None:
    buf = io.BytesIO()
    sink = _SizeCappedWriter(buf, cap=100)
    n = sink.write(b"hello")
    assert n == 5
    sink.flush()  # must not raise (C-3)
    assert buf.getvalue() == b"hello"
    assert sink.written == 5


def test_writer_raises_on_overrun() -> None:
    buf = io.BytesIO()
    sink = _SizeCappedWriter(buf, cap=4)
    with pytest.raises(SizeCapExceeded) as info:
        sink.write(b"hello")  # 5 > 4
    assert info.value.cap == 4
    assert info.value.received == 5
    # Sink did NOT delegate the write when the projected count would
    # exceed cap -- buf stays empty.
    assert buf.getvalue() == b""


def test_writer_flush_forwards_to_dest() -> None:
    # Use a MagicMock wrapping BytesIO so we can assert flush()
    # propagated to the wrapped writer (aiogram relies on this for
    # accurate on-disk size).
    backing = MagicMock(wraps=io.BytesIO())
    sink = _SizeCappedWriter(backing, cap=100)
    sink.write(b"x")
    sink.flush()
    backing.flush.assert_called_once()


def test_writer_write_and_flush_both_present() -> None:
    # C-3 invariant: both methods MUST exist. A missing `flush` here
    # would regress silently in integration (aiogram's downloader
    # calls both per chunk).
    assert callable(getattr(_SizeCappedWriter, "write", None))
    assert callable(getattr(_SizeCappedWriter, "flush", None))


def test_writer_rejects_non_positive_cap() -> None:
    buf = io.BytesIO()
    with pytest.raises(ValueError):
        _SizeCappedWriter(buf, cap=0)
    with pytest.raises(ValueError):
        _SizeCappedWriter(buf, cap=-1)


# --- MockFile / MockBot helpers ------------------------------------


class _MockFile:
    """Mimic enough of `aiogram.types.File` for the download helper."""

    def __init__(self, file_size: int | None, file_path: str = "fakes/x.bin") -> None:
        self.file_size = file_size
        self.file_path = file_path


class _MockBot:
    """Mock Bot matching the subset of `aiogram.Bot` used by
    `download_telegram_file`.

    `chunks` is the list of bytes objects the mock will stream into
    the `destination` BinaryIO passed by the caller. This faithfully
    reproduces the aiogram 3.26 pattern:

        async for chunk in stream:
            destination.write(chunk)
            destination.flush()

    -- so `_SizeCappedWriter.write` / `flush` are both exercised.
    """

    def __init__(self, *, chunks: list[bytes], file_size: int | None) -> None:
        self._chunks = chunks
        self._file = _MockFile(file_size=file_size, file_path="fakes/x.bin")

    async def get_file(self, file_id: str, request_timeout: int | None = None) -> _MockFile:
        return self._file

    async def download_file(
        self,
        file_path: str,
        destination: Any = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        seek: bool = True,
    ) -> BinaryIO | None:
        assert destination is not None, "test relies on BinaryIO path"
        for chunk in self._chunks:
            destination.write(chunk)
            destination.flush()  # aiogram 3.26 loop calls both -- C-3
        return destination


# --- S-6 case A: None-size, small payload, None-cap equivalent (respect cap) ---


async def test_case_a_none_size_small_payload(tmp_path: Path) -> None:
    # Two 1 KB chunks, cap 10 KB, file_size None → SUCCESS.
    bot = _MockBot(chunks=[b"X" * 1024, b"Y" * 1024], file_size=None)
    dest_dir = tmp_path
    saved = await download_telegram_file(
        bot,  # type: ignore[arg-type]
        file_id="file_A",
        dest_dir=dest_dir,
        suggested_filename="x.bin",
        max_bytes=10 * 1024,
    )
    assert saved.exists()
    assert saved.stat().st_size == 2048
    assert saved.parent == dest_dir.resolve()


# --- S-6 case B: None-size, payload overruns cap, streaming-cap aborts ---


async def test_case_b_streaming_cap_abort_unlinks_partial(tmp_path: Path) -> None:
    # 100 * 4 KB = 400 KB chunks; cap 100 KB; file_size None → must
    # abort mid-stream AND unlink the partial file (pitfall #3 step 5).
    bot = _MockBot(
        chunks=[b"X" * 4096] * 100,
        file_size=None,
    )
    with pytest.raises(SizeCapExceeded):
        await download_telegram_file(
            bot,  # type: ignore[arg-type]
            file_id="file_B",
            dest_dir=tmp_path,
            suggested_filename="x.bin",
            max_bytes=100 * 1024,
        )
    # Partial file must be unlinked -- no stray files in tmp_path.
    leftover = list(tmp_path.iterdir())
    assert leftover == [], f"unexpected residue: {leftover}"


# --- S-6 case C: known oversize → pre-flight reject ---


async def test_case_c_preflight_reject_known_oversize(tmp_path: Path) -> None:
    # file_size 5 MB > cap 100 KB → SizeCapExceeded BEFORE any stream.
    bot = _MockBot(
        chunks=[b"Z" * 4096] * 50,
        file_size=5_000_000,
    )
    with pytest.raises(SizeCapExceeded) as info:
        await download_telegram_file(
            bot,  # type: ignore[arg-type]
            file_id="file_C",
            dest_dir=tmp_path,
            suggested_filename="x.bin",
            max_bytes=100 * 1024,
        )
    assert "pre-flight" in str(info.value)
    # No file was created -- pre-flight rejects before os.open.
    assert list(tmp_path.iterdir()) == []


# --- S-6 case D: None-cap → file_size known + in-cap → success ---


async def test_case_d_known_in_cap_success(tmp_path: Path) -> None:
    bot = _MockBot(chunks=[b"W" * 4096], file_size=4096)
    saved = await download_telegram_file(
        bot,  # type: ignore[arg-type]
        file_id="file_D",
        dest_dir=tmp_path,
        suggested_filename="x.bin",
        max_bytes=100 * 1024,
    )
    assert saved.exists()
    assert saved.stat().st_size == 4096


# --- Extra coverage: extension carried through from suggested_filename ---


async def test_suffix_preserved_from_suggested_filename(tmp_path: Path) -> None:
    bot = _MockBot(chunks=[b"hello"], file_size=5)
    saved = await download_telegram_file(
        bot,  # type: ignore[arg-type]
        file_id="file_E",
        dest_dir=tmp_path,
        suggested_filename="cat.jpg",
        max_bytes=1000,
    )
    assert saved.suffix == ".jpg"


async def test_unlink_on_non_size_error(tmp_path: Path) -> None:
    # A generic exception mid-stream must still trigger unlink.
    class _ExplodingBot:
        async def get_file(
            self, file_id: str, request_timeout: int | None = None
        ) -> _MockFile:
            return _MockFile(file_size=None, file_path="fakes/x.bin")

        async def download_file(
            self,
            file_path: str,
            destination: Any = None,
            timeout: int = 30,
            chunk_size: int = 65536,
            seek: bool = True,
        ) -> BinaryIO | None:
            # Write some bytes, then raise an unexpected error.
            destination.write(b"partial")
            destination.flush()
            raise RuntimeError("network imploded")

    bot = _ExplodingBot()
    with pytest.raises(RuntimeError):
        await download_telegram_file(
            bot,  # type: ignore[arg-type]
            file_id="file_F",
            dest_dir=tmp_path,
            suggested_filename="x.bin",
            max_bytes=1000,
        )
    # Partial file still unlinked (pitfall #3).
    assert list(tmp_path.iterdir()) == []


async def test_file_path_none_rejects(tmp_path: Path) -> None:
    class _NoPathBot:
        async def get_file(
            self, file_id: str, request_timeout: int | None = None
        ) -> _MockFile:
            mf = _MockFile(file_size=None, file_path="anything")
            mf.file_path = None  # type: ignore[assignment]
            return mf

        async def download_file(
            self, *args: Any, **kwargs: Any
        ) -> BinaryIO | None:
            raise AssertionError("should not be called")

    with pytest.raises(RuntimeError, match="file_path"):
        await download_telegram_file(
            _NoPathBot(),  # type: ignore[arg-type]
            file_id="no_path",
            dest_dir=tmp_path,
            suggested_filename="x.bin",
            max_bytes=1000,
        )


async def test_bad_max_bytes_rejected(tmp_path: Path) -> None:
    bot = _MockBot(chunks=[b"x"], file_size=1)
    with pytest.raises(ValueError):
        await download_telegram_file(
            bot,  # type: ignore[arg-type]
            file_id="x",
            dest_dir=tmp_path,
            suggested_filename="x.bin",
            max_bytes=0,
        )


# Silence an unused-import hint — AsyncMock is useful for future
# add-ons but not directly exercised today.
_ = AsyncMock
