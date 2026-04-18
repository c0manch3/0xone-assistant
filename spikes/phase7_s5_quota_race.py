"""Phase 7 spike S-5 — genimage quota file race at midnight (devil Gap #10).

Daily quota state lives in a JSON file:

    <data_dir>/run/genimage-quota.json
    {"date": "2026-04-17", "count": 0}

Access is flock-serialised (phase-4 precedent). Two races we want to
characterize:

  R-1 cross-midnight: CLI A finishes just before 00:00 UTC, CLI B starts
      just after. A wrote (date=2026-04-17, count=1); B reads that file
      AFTER midnight, sees date mismatch, resets count=0, increments → 1.
      EXPECTATION: A's request counts toward day 1, B's toward day 2.
      Both should succeed under a cap of 1/day.

  R-2 same-second: two CLI invocations at the same second around midnight.
      flock serialises them; each observes a consistent view. We want to
      verify that under flock contention, neither double-counts nor
      loses a request. Second request within cap window should deny.

This spike simulates the behavior in-process by patching `time.time()` /
writing/reading the quota file directly — it does NOT exercise the
real CLI (that's a unit-test job). We verify the algorithm correctness.

Run:  uv run python spikes/phase7_s5_quota_race.py
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s5_report.json"


class QuotaCLI:
    """In-process simulation of the CLI's quota logic.

    Real CLI would exec as a subprocess — here we emulate the core
    algorithm to characterize correctness.
    """

    def __init__(self, path: Path, cap: int = 1) -> None:
        self.path = path
        self.cap = cap

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state))

    def try_increment(self, *, now_utc: datetime) -> tuple[bool, dict[str, Any]]:
        """Mimic the CLI's quota check. Returns (allowed, state_after)."""
        today = now_utc.strftime("%Y-%m-%d")
        # Open + flock + read + mutate + write + unlock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # re-read inside the lock
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096).decode("utf-8") or "{}"
            try:
                state = json.loads(raw)
            except json.JSONDecodeError:
                state = {}

            if state.get("date") != today:
                # Fresh day — reset
                state = {"date": today, "count": 0}

            if state.get("count", 0) >= self.cap:
                return False, state

            state["count"] = state.get("count", 0) + 1

            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(state).encode("utf-8"))
            return True, state
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _probe_midnight_rollover() -> dict[str, Any]:
    """R-1: cross-midnight scenario."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="phase7_s5_r1_"))
    quota_path = tmp / "run" / "genimage-quota.json"
    cli = QuotaCLI(quota_path, cap=1)

    # Day 1 23:59:59.8 UTC
    t_day1 = datetime(2026, 4, 17, 23, 59, 59, 800000, tzinfo=timezone.utc)
    allowed1, state1 = cli.try_increment(now_utc=t_day1)

    # Day 2 00:00:00.2 UTC — 400 ms later
    t_day2 = t_day1 + timedelta(milliseconds=400)
    allowed2, state2 = cli.try_increment(now_utc=t_day2)

    return {
        "scenario": "cross_midnight",
        "day1_time": t_day1.isoformat(),
        "day1_allowed": allowed1,
        "day1_state": state1,
        "day2_time": t_day2.isoformat(),
        "day2_allowed": allowed2,
        "day2_state": state2,
        "verdict": (
            "PASS"
            if (allowed1 and allowed2 and state1.get("count") == 1 and state2.get("count") == 1)
            else "FAIL"
        ),
    }


def _probe_same_second_cap() -> dict[str, Any]:
    """Same day, two consecutive requests, cap=1 → second must deny."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="phase7_s5_r2_"))
    quota_path = tmp / "run" / "genimage-quota.json"
    cli = QuotaCLI(quota_path, cap=1)

    t = datetime(2026, 4, 17, 14, 0, 0, tzinfo=timezone.utc)
    a1, s1 = cli.try_increment(now_utc=t)
    a2, s2 = cli.try_increment(now_utc=t + timedelta(milliseconds=100))

    return {
        "scenario": "same_day_cap",
        "first_allowed": a1,
        "first_state": s1,
        "second_allowed": a2,
        "second_state": s2,
        "verdict": (
            "PASS"
            if (a1 and not a2 and s1.get("count") == 1 and s2.get("count") == 1)
            else "FAIL"
        ),
    }


def _probe_concurrent_flock() -> dict[str, Any]:
    """Concurrent threads competing for flock in same second.

    Launch N parallel callers. Cap=1 — exactly ONE allowed, N-1 denied.
    Verifies flock prevents double-increment under contention.
    """
    import tempfile
    import threading

    tmp = Path(tempfile.mkdtemp(prefix="phase7_s5_r3_"))
    quota_path = tmp / "run" / "genimage-quota.json"
    cli = QuotaCLI(quota_path, cap=1)

    t = datetime(2026, 4, 17, 14, 0, 0, tzinfo=timezone.utc)
    n = 10
    results: list[tuple[bool, dict[str, Any]]] = []
    lock = threading.Lock()
    start_barrier = threading.Barrier(n)

    def worker() -> None:
        start_barrier.wait()
        out = cli.try_increment(now_utc=t)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    allowed_count = sum(1 for a, _ in results if a)
    final_state = cli._load()

    return {
        "scenario": "concurrent_flock",
        "worker_count": n,
        "allowed_count": allowed_count,
        "final_state": final_state,
        "verdict": (
            "PASS"
            if allowed_count == 1 and final_state.get("count") == 1
            else "FAIL"
        ),
    }


def _probe_boundary_jitter() -> dict[str, Any]:
    """If clock jitters BACKWARD across midnight (e.g. NTP correction),
    what happens? E.g. call_A sees 00:00:02 (day 2), call_B sees
    23:59:55 (day 1 — clock rolled back). Does the algo stay sane?
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="phase7_s5_r4_"))
    quota_path = tmp / "run" / "genimage-quota.json"
    cli = QuotaCLI(quota_path, cap=1)

    # Clock-forward then back
    t1 = datetime(2026, 4, 18, 0, 0, 2, tzinfo=timezone.utc)
    a1, s1 = cli.try_increment(now_utc=t1)

    t2 = datetime(2026, 4, 17, 23, 59, 55, tzinfo=timezone.utc)
    a2, s2 = cli.try_increment(now_utc=t2)

    # After the clock-rollback, the algo resets to day 1 (different
    # date in file) — allows another request. This is a KNOWN
    # ±1-boundary behaviour; NTP rollback is rare.
    return {
        "scenario": "clock_rollback",
        "forward_time": t1.isoformat(),
        "forward_allowed": a1,
        "forward_state": s1,
        "backward_time": t2.isoformat(),
        "backward_allowed": a2,
        "backward_state": s2,
        "verdict": "PASS_WITH_KNOWN_JITTER" if (a1 and a2) else "FAIL",
        "note": (
            "Clock rollback across midnight allows an extra request on the "
            "prior day (date mismatch resets count). Accept as rare edge "
            "case; NTP step-back on a production VPS is uncommon. "
            "Mitigations (not-in-plan, phase-9): use monotonic counter + "
            "date hash, or record epoch-seconds mod 86400."
        ),
    }


def main() -> None:
    findings: dict[str, object] = {
        "r1_cross_midnight": _probe_midnight_rollover(),
        "r2_same_day_cap": _probe_same_second_cap(),
        "r3_concurrent_flock": _probe_concurrent_flock(),
        "r4_boundary_jitter": _probe_boundary_jitter(),
    }

    all_pass = all(
        v.get("verdict", "").startswith("PASS") for v in findings.values()
    )
    findings["verdict"] = "PASS" if all_pass else "PARTIAL"

    REPORT.write_text(json.dumps(findings, indent=2, ensure_ascii=False))
    for k, v in findings.items():
        if isinstance(v, dict):
            print(f"  {k}: {v.get('verdict')}")
    print(f"\nVerdict: {findings['verdict']}")
    print(f"Report -> {REPORT}")


if __name__ == "__main__":
    main()
