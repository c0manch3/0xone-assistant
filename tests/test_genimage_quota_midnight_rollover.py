"""Phase 7 / commit 18h — consolidated regression gate for the genimage
daily quota's midnight-rollover semantics.

This module is the **dedicated S-5 corpus** for the four spike scenarios
documented in ``plan/phase7/spike-findings.md`` §6:

* **R-1** cross-midnight rollover — yesterday's count, today's first
  call → counter resets to 1, request is allowed.
* **R-2** same-day cap denial — count == cap → next call returns
  ``EXIT_QUOTA`` (6) without mutating the file.
* **R-3** flock contention — 10 concurrent workers racing on the same
  quota file each call into the helper exactly once; cap=K → exactly
  K winners, no lost updates, persisted ``count == K``. Both intra-
  process (threads) and cross-process (subprocess.Popen against the
  shipped CLI) variants are exercised.
* **R-4** NTP clock-rollback jitter — a small backward time jump
  across midnight is documented to leak ±1 request (acceptable per
  S-5 / pitfall #7).

In addition, this gate exercises **two NEW scenarios** not covered by
``tests/test_tools_genimage_cli.py``:

* **R-5** quota-file corruption recovery — invalid JSON, list/scalar
  payloads, and partial truncation must NOT lock the user out of all
  future requests; the helper resets to a fresh same-day record.
* **R-6** multi-day history — a quota file last-touched many days ago
  (e.g. a paused VPS) rolls over cleanly to the new day with count=1
  on the first call, regardless of the gap.

Cross-process tests use ``subprocess.Popen`` to invoke the shipped
``tools/genimage/main.py`` directly (no ``multiprocessing`` / pickle).
This is the most authentic reproduction: each worker is a real PID
holding its own ``fcntl.flock`` against the shared file — the only
configuration where the lock semantics genuinely differ from a
single-PID run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_CLI = _ROOT / "tools" / "genimage" / "main.py"

# `tools` lives at repo root; insert the path BEFORE importing the CLI
# helpers. The E402 noqa keeps lint silent about ordering.
sys.path.insert(0, str(_ROOT))

from tools.genimage.main import (  # noqa: E402
    EXIT_NETWORK,
    EXIT_QUOTA,
    _check_and_increment_quota,
    _read_quota_best_effort,
)


# ---------------------------------------------------------------- helpers


def _read_persisted(path: Path) -> dict[str, object]:
    """Best-effort JSON read for assertions; raises on missing file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _build_cli_env(tmp_path: Path, quota_file: Path) -> dict[str, str]:
    return {
        **os.environ,
        "ASSISTANT_DATA_DIR": str(tmp_path),
        "MEDIA_OUTBOX_DIR": str(tmp_path / "media" / "outbox"),
        "MEDIA_GENIMAGE_QUOTA_FILE": str(quota_file),
    }


def _spawn_cli_quota_only(
    tmp_path: Path,
    *,
    cap: int,
    out_name: str,
    quota_file: Path,
) -> subprocess.Popen[str]:
    """Spawn the genimage CLI pointed at an unreachable endpoint so the
    process exits at the network stage (allowed → EXIT_NETWORK=4) or
    before it (denied → EXIT_QUOTA=6).

    Returns the Popen so the caller can synchronise startup before
    waiting; this is what makes the R-3 cross-process race authentic.
    """
    out = tmp_path / "media" / "outbox" / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    quota_file.parent.mkdir(parents=True, exist_ok=True)

    return subprocess.Popen(
        [
            sys.executable,
            str(_CLI),
            "--prompt",
            "regression",
            "--out",
            str(out),
            # 127.0.0.1:1 is a reserved port — the CLI passes the
            # loopback guard, then network-fails fast.
            "--endpoint",
            "http://127.0.0.1:1/generate",
            "--daily-cap",
            str(cap),
            "--timeout-s",
            "30",
        ],
        env=_build_cli_env(tmp_path, quota_file),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


# ============================================================== R-1 rollover


class TestR1CrossMidnightRollover:
    """Yesterday's quota=cap → today's first call resets to 1, allow."""

    def test_count_resets_to_one_on_new_day(self, tmp_path: Path) -> None:
        qf = tmp_path / "q.json"
        # Fill yesterday to cap.
        a1, _ = _check_and_increment_quota(qf, cap=3, today="2026-04-17")
        a2, _ = _check_and_increment_quota(qf, cap=3, today="2026-04-17")
        a3, s3 = _check_and_increment_quota(qf, cap=3, today="2026-04-17")
        assert (a1, a2, a3) == (True, True, True)
        assert s3 == {"date": "2026-04-17", "count": 3, "cap": 3}

        # Today's first call: counter must reset, not deny.
        allowed_today, state_today = _check_and_increment_quota(
            qf, cap=3, today="2026-04-18"
        )
        assert allowed_today is True
        assert state_today == {"date": "2026-04-18", "count": 1, "cap": 3}
        assert _read_persisted(qf) == {"date": "2026-04-18", "count": 1}

    def test_rollover_works_at_exact_cap_boundary(self, tmp_path: Path) -> None:
        """cap=1 yesterday saturated → today still allowed."""
        qf = tmp_path / "q.json"
        a1, _ = _check_and_increment_quota(qf, cap=1, today="2026-04-17")
        a2, s2 = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        assert a1 is True
        assert a2 is True and s2["count"] == 1
        assert _read_persisted(qf)["date"] == "2026-04-18"


# ============================================================== R-2 cap deny


class TestR2SameDayCapDenial:
    """Once today's counter == cap, the next call must return EXIT_QUOTA."""

    def test_second_call_at_cap_one_is_denied(self, tmp_path: Path) -> None:
        qf = tmp_path / "q.json"
        a1, s1 = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        a2, s2 = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        assert (a1, a2) == (True, False)
        assert s1["count"] == 1
        # Denied call must NOT bump the counter (idempotent on deny).
        assert s2["count"] == 1
        assert _read_persisted(qf) == {"date": "2026-04-18", "count": 1}

    def test_denial_at_cap_three(self, tmp_path: Path) -> None:
        qf = tmp_path / "q.json"
        for _ in range(3):
            ok, _ = _check_and_increment_quota(qf, cap=3, today="2026-04-18")
            assert ok is True
        denied, state = _check_and_increment_quota(qf, cap=3, today="2026-04-18")
        assert denied is False
        assert state == {"date": "2026-04-18", "count": 3, "cap": 3}
        assert _read_persisted(qf) == {"date": "2026-04-18", "count": 3}

    def test_cli_returns_exit_six_when_at_cap(self, tmp_path: Path) -> None:
        """End-to-end parity: the CLI surfaces EXIT_QUOTA=6 to operators."""
        from datetime import UTC, datetime

        qf = tmp_path / "run" / "genimage-quota.json"
        qf.parent.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        # Pre-fill at cap so the first CLI call is denied.
        qf.write_text(json.dumps({"date": today, "count": 1}), encoding="utf-8")

        proc = _spawn_cli_quota_only(
            tmp_path, cap=1, out_name="r2.png", quota_file=qf
        )
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == EXIT_QUOTA, stderr
        payload = json.loads(stdout)
        assert payload["ok"] is False
        assert "quota" in payload["reason"].lower()


# ============================================================== R-3 flock race


class TestR3FlockContention:
    """10 concurrent workers — atomic increments, no lost updates."""

    def test_threads_cap_one_exactly_one_winner(self, tmp_path: Path) -> None:
        qf = tmp_path / "q.json"
        n = 10
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def worker() -> None:
            barrier.wait()
            ok, _ = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = sum(1 for r in results if r)
        assert winners == 1, f"expected exactly one winner, got {winners}"
        assert _read_persisted(qf) == {"date": "2026-04-18", "count": 1}

    def test_threads_cap_three_exactly_three_winners(self, tmp_path: Path) -> None:
        """Same race, cap=3 → exactly 3 winners; persisted count == 3."""
        qf = tmp_path / "q.json"
        n = 10
        cap = 3
        results: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def worker() -> None:
            barrier.wait()
            ok, _ = _check_and_increment_quota(qf, cap=cap, today="2026-04-18")
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = sum(1 for r in results if r)
        assert winners == cap, f"expected {cap} winners, got {winners}"
        persisted = _read_persisted(qf)
        assert persisted == {"date": "2026-04-18", "count": cap}

    def test_subprocesses_cap_two_exactly_two_winners(
        self, tmp_path: Path
    ) -> None:
        """True cross-PID flock test — spawns 10 real processes that
        race on the same quota file. Each one calls the CLI which
        invokes ``fcntl.flock`` from a distinct PID — the only context
        where flock semantics genuinely differ from intra-process locks.

        Workers that win the quota slot proceed to the network stage
        (which fails immediately against the unreachable endpoint), so
        winners exit with EXIT_NETWORK=4 and losers exit with
        EXIT_QUOTA=6.
        """
        qf = tmp_path / "run" / "genimage-quota.json"
        n = 10
        cap = 2

        # Start all subprocesses; the OS scheduler provides genuine
        # contention. We do not need a barrier — Popen.start is fast
        # enough that the first few processes race on flock acquisition.
        procs = [
            _spawn_cli_quota_only(
                tmp_path, cap=cap, out_name=f"r3-{i}.png", quota_file=qf
            )
            for i in range(n)
        ]
        return_codes: list[int] = []
        for proc in procs:
            try:
                proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                pytest.fail("subprocess hung past timeout")
            return_codes.append(proc.returncode)

        winners = sum(1 for rc in return_codes if rc == EXIT_NETWORK)
        losers = sum(1 for rc in return_codes if rc == EXIT_QUOTA)
        # All processes must have exited with one of the two expected
        # codes; anything else is a real bug.
        unexpected = [rc for rc in return_codes if rc not in (EXIT_NETWORK, EXIT_QUOTA)]
        assert not unexpected, f"unexpected exit codes: {unexpected}"
        assert winners == cap, f"expected {cap} cross-process winners, got {winners}"
        assert losers == n - cap

        persisted = _read_persisted(qf)
        assert persisted["count"] == cap, persisted


# ============================================================== R-4 jitter


class TestR4ClockRollbackJitter:
    """Documented ±1 quota leak when wallclock jumps backward across midnight."""

    def test_backward_jump_resets_counter_known_jitter(self, tmp_path: Path) -> None:
        qf = tmp_path / "q.json"
        a_fwd, s_fwd = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        # NTP corrects the clock backward AFTER the first request.
        a_back, s_back = _check_and_increment_quota(qf, cap=1, today="2026-04-17")
        assert a_fwd is True and s_fwd["count"] == 1
        # KNOWN JITTER: the date mismatch resets the counter, so the
        # rolled-back request is also allowed. Per S-5 R-4 and SKILL.md,
        # this is accepted behaviour (≤1 extra request per backward jump).
        assert a_back is True and s_back["count"] == 1
        assert _read_persisted(qf) == {"date": "2026-04-17", "count": 1}

    def test_small_intra_day_jitter_does_not_reset(self, tmp_path: Path) -> None:
        """A jitter that does NOT cross midnight stays denied past cap."""
        qf = tmp_path / "q.json"
        a1, _ = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        # Same date, sub-second backward jitter — must still deny.
        a2, s2 = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        assert a1 is True
        assert a2 is False
        assert s2["count"] == 1


# ============================================================== R-5 corruption


class TestR5CorruptionRecovery:
    """NEW: a corrupt quota file must NOT lock the user out forever."""

    @pytest.mark.parametrize(
        "raw",
        [
            b"this is not json{{{",
            b"\x00\x01\x02not-utf8-\xff\xfe",
            b'{"date": "2026-04-18"',  # truncated mid-key
            b"",  # empty file (sentinel; treated as no record)
        ],
        ids=["garbage_json", "binary_garbage", "truncated_json", "empty_file"],
    )
    def test_corrupt_payload_resets_counter(self, tmp_path: Path, raw: bytes) -> None:
        qf = tmp_path / "q.json"
        qf.write_bytes(raw)

        allowed, state = _check_and_increment_quota(qf, cap=2, today="2026-04-18")
        assert allowed is True, "must recover from corruption, not refuse forever"
        assert state == {"date": "2026-04-18", "count": 1, "cap": 2}
        # The next call inside the same day still respects the cap.
        a2, s2 = _check_and_increment_quota(qf, cap=2, today="2026-04-18")
        a3, s3 = _check_and_increment_quota(qf, cap=2, today="2026-04-18")
        assert a2 is True and s2["count"] == 2
        assert a3 is False and s3["count"] == 2
        assert _read_persisted(qf) == {"date": "2026-04-18", "count": 2}

    def test_wrong_shape_list_payload_recovers(self, tmp_path: Path) -> None:
        """A list/scalar quota file must be replaced, not crash the helper."""
        qf = tmp_path / "q.json"
        qf.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        allowed, state = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        assert allowed is True
        assert state == {"date": "2026-04-18", "count": 1, "cap": 1}

    def test_best_effort_reader_tolerates_text_corruption(
        self, tmp_path: Path
    ) -> None:
        """Diagnostic reader must never raise for UTF-8 text inputs.

        Currently exercises the code paths that the helper explicitly
        handles: missing file, empty file, malformed JSON, and JSON of
        the wrong shape. Binary-garbage inputs are covered by
        ``test_best_effort_reader_binary_input_recovers`` below.
        """
        qf = tmp_path / "q.json"
        for raw in (b"{not json", b"", json.dumps([1]).encode()):
            qf.write_bytes(raw)
            assert _read_quota_best_effort(qf) == {}

    def test_best_effort_reader_binary_input_recovers(self, tmp_path: Path) -> None:
        qf = tmp_path / "q.json"
        qf.write_bytes(b"\xff\xff")
        assert _read_quota_best_effort(qf) == {}

    def test_missing_file_treated_as_no_record(self, tmp_path: Path) -> None:
        qf = tmp_path / "subdir" / "q.json"  # parent doesn't exist yet
        assert not qf.exists()
        allowed, state = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        assert allowed is True
        assert state == {"date": "2026-04-18", "count": 1, "cap": 1}
        # The helper auto-creates the parent dir.
        assert qf.exists()


# ============================================================== R-6 multi-day


class TestR6MultiDayHistory:
    """NEW: long-paused VPS — quota file is days/weeks old, must roll over."""

    def test_one_week_gap_rolls_over_cleanly(self, tmp_path: Path) -> None:
        qf = tmp_path / "q.json"
        # Day 0: saturate the cap.
        for _ in range(2):
            ok, _ = _check_and_increment_quota(qf, cap=2, today="2026-04-11")
            assert ok is True
        denied, _ = _check_and_increment_quota(qf, cap=2, today="2026-04-11")
        assert denied is False

        # 7 days later — the file is stale but the helper must allow.
        allowed, state = _check_and_increment_quota(qf, cap=2, today="2026-04-18")
        assert allowed is True
        assert state == {"date": "2026-04-18", "count": 1, "cap": 2}
        assert _read_persisted(qf) == {"date": "2026-04-18", "count": 1}

    def test_chain_of_consecutive_days_each_starts_at_one(
        self, tmp_path: Path
    ) -> None:
        qf = tmp_path / "q.json"
        days = [
            "2026-04-15",
            "2026-04-16",
            "2026-04-17",
            "2026-04-18",
            "2026-04-19",
        ]
        for day in days:
            allowed, state = _check_and_increment_quota(qf, cap=1, today=day)
            assert allowed is True, f"{day} should reset to count=1"
            assert state == {"date": day, "count": 1, "cap": 1}
        # Final state reflects only the last day — no cumulative drift.
        assert _read_persisted(qf) == {"date": "2026-04-19", "count": 1}

    def test_year_boundary_rollover(self, tmp_path: Path) -> None:
        """Sanity: lexicographic vs date comparison — Dec 31 → Jan 1."""
        qf = tmp_path / "q.json"
        a1, _ = _check_and_increment_quota(qf, cap=1, today="2025-12-31")
        a2, s2 = _check_and_increment_quota(qf, cap=1, today="2026-01-01")
        assert a1 is True and a2 is True
        assert s2 == {"date": "2026-01-01", "count": 1, "cap": 1}


# ============================================================== invariants


class TestQuotaInvariants:
    """Cross-cutting properties that must hold across all R-* scenarios."""

    def test_cap_zero_denies_without_creating_record(self, tmp_path: Path) -> None:
        """cap=0 means 'feature disabled' — file must NOT be touched."""
        qf = tmp_path / "q.json"
        allowed, state = _check_and_increment_quota(qf, cap=0, today="2026-04-18")
        assert allowed is False
        assert state == {}  # best-effort empty read
        assert not qf.exists()

    def test_returned_state_shape_is_consistent(self, tmp_path: Path) -> None:
        """Allowed and denied calls return dicts with date+count+cap keys."""
        qf = tmp_path / "q.json"
        a, s_allowed = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        d, s_denied = _check_and_increment_quota(qf, cap=1, today="2026-04-18")
        assert a is True and d is False
        for s in (s_allowed, s_denied):
            assert set(s.keys()) >= {"date", "count", "cap"}
            assert s["date"] == "2026-04-18"
            assert s["cap"] == 1
            assert s["count"] == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-x", "-v"])
