"""Phase 7 spike S-0 (BLOCKER) — SDK multimodal envelope empirical probe.

Answers Q0-1..Q0-6 from plan/phase7/detailed-plan.md §2.1 by making real
`claude_agent_sdk.query(...)` calls with mixed content blocks containing
text + image (base64-encoded) in a single user envelope.

Run: uv run python spikes/phase7_s0_multimodal_envelope.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
import traceback
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    query,
)

HERE = Path(__file__).resolve().parent
REPORT = HERE / "phase7_s0_report.json"
CWD = HERE.parent

# A verified-valid 2x2 grayscale baseline JPEG (~150 bytes). Used as the
# base for all photo probes. Larger-size probes pad COM segments into it.
_TINY_JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCACAAIADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD22iiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAOb8Y+KP+Eb+x/wCh/aftG/8A5a7Nu3b7HP3v0rm/+Fn/APUI/wDJn/7Cj4x/8wj/ALbf+yV5tXmYjEVIVHGL0Pt8oyjB4nBwq1YXk79X3a6M9J/4Wf8A9Qj/AMmf/sKP+Fn/APUI/wDJn/7CvNqKx+t1e/5Hpf2Bl/8Az7/GX+Z6T/ws/wD6hH/kz/8AYUf8LP8A+oR/5M//AGFebUUfW6vf8g/sDL/+ff4y/wAz0n/hZ/8A1CP/ACZ/+wo/4Wf/ANQj/wAmf/sK82oo+t1e/wCQf2Bl/wDz7/GX+Z6T/wALP/6hH/kz/wDYUf8ACz/+oR/5M/8A2FebUUfW6vf8g/sDL/8An3+Mv8z0n/hZ/wD1CP8AyZ/+wrpPB3ij/hJPtn+h/Zvs+z/lrv3bt3sMfd/WvEq9J+Dn/MX/AO2P/s9bYfEVJ1FGT0PNzfKMHhsHOrShaSt1fdLqz0iiiivTPiAooooA83+Mf/MI/wC23/slebV6T8Y/+YR/22/9krzavGxf8V/10P0nIP8AkX0/n/6UwooornPYCiiigAooooAKKKKACvSfg5/zF/8Atj/7PXm1ek/Bz/mL/wDbH/2eujCfxV/XQ8fP/wDkX1Pl/wClI9Iooor2T82CiiigDzf4x/8AMI/7bf8AslebV7b4x8L/APCSfY/9M+zfZ9//ACy37t233GPu/rXN/wDCsP8AqL/+S3/2deZiMPUnUcorQ+3yjN8HhsHClVnaSv0fdvojzaivSf8AhWH/AFF//Jb/AOzo/wCFYf8AUX/8lv8A7OsfqlXt+R6X9v5f/wA/Pwl/kebUV6T/AMKw/wCov/5Lf/Z0f8Kw/wCov/5Lf/Z0fVKvb8g/t/L/APn5+Ev8jzaivSf+FYf9Rf8A8lv/ALOj/hWH/UX/APJb/wCzo+qVe35B/b+X/wDPz8Jf5Hm1Fek/8Kw/6i//AJLf/Z0f8Kw/6i//AJLf/Z0fVKvb8g/t/L/+fn4S/wAjzavSfg5/zF/+2P8A7PR/wrD/AKi//kt/9nXSeDvC/wDwjf2z/TPtP2jZ/wAstm3bu9zn736Vth8PUhUUpLQ83N83weJwc6VKd5O3R90+qOkooor0z4gKKKKAMvXPEGmaF5H9q3Pkedu8v92zZxjP3QfUVlf8J/4Z/wCgl/5Al/8Aia5b43/8wX/tv/7Try2uGtip05uKSPNxGMnTqOCSPe/+E/8ADP8A0Ev/ACBL/wDE0f8ACf8Ahn/oJf8AkCX/AOJrwSisvrtTsjH+0KnZf18z3v8A4T/wz/0Ev/IEv/xNH/Cf+Gf+gl/5Al/+JrwSij67U7IP7Qqdl/XzPe/+E/8ADP8A0Ev/ACBL/wDE0f8ACf8Ahn/oJf8AkCX/AOJrwSij67U7IP7Qqdl/XzPe/wDhP/DP/QS/8gS//E0f8J/4Z/6CX/kCX/4mvBKKPrtTsg/tCp2X9fM97/4T/wAM/wDQS/8AIEv/AMTWrofiDTNd8/8Asq58/wAnb5n7tlxnOPvAehr5vr1L4If8xr/th/7UrWjip1JqLSNsPjJ1Kig0j1Kiiiu49IKKKKAPLfjf/wAwX/tv/wC068tr1L43/wDMF/7b/wDtOvLa8jFfxX/XQ8PGfxpf10Ciiiuc5QooooAKKKKACiiigAr1L4If8xr/ALYf+1K8tr1L4If8xr/th/7Urowv8Vf10OrB/wAaP9dD1KiiivXPcCiiigDy343/APMF/wC2/wD7Try2ve/HHhL/AISj7F/pv2X7Nv8A+WW/du2/7Qx939a5b/hU3/Ua/wDJX/7OvOr0Kk6jlFaHlYnDVZ1XKK0PLaK9S/4VN/1Gv/JX/wCzo/4VN/1Gv/JX/wCzrH6rV7fkYfU638v5HltFepf8Km/6jX/kr/8AZ0f8Km/6jX/kr/8AZ0fVavb8g+p1v5fyPLaK9S/4VN/1Gv8AyV/+zo/4VN/1Gv8AyV/+zo+q1e35B9Trfy/keW0V6l/wqb/qNf8Akr/9nR/wqb/qNf8Akr/9nR9Vq9vyD6nW/l/I8tr1L4If8xr/ALYf+1KP+FTf9Rr/AMlf/s66nwP4S/4Rf7b/AKb9q+07P+WWzbt3f7Rz979K2oUKkKilJaG+Gw1WFVSktDqaKKK9E9UKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD//2Q=="


def _tiny_jpeg_bytes() -> bytes:
    return base64.b64decode(_TINY_JPEG_B64)


def _make_image_block(
    img_bytes: bytes, media_type: str = "image/jpeg"
) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(img_bytes).decode("ascii"),
        },
    }


def _bloat_jpeg(base: bytes, target_size: int) -> bytes:
    """Pad `base` to `target_size` by stuffing FFFE (COM) segments between
    SOI and the remainder. Result is a larger valid JPEG.
    """
    if len(base) >= target_size:
        return base
    assert base[:2] == b"\xff\xd8", "base is not a JPEG"
    soi = base[:2]
    rest = base[2:]
    out = bytearray(soi)
    need = target_size - len(base)
    max_payload = 65533
    while need > 4:
        payload_size = min(max_payload - 2, need - 4)
        marker = b"\xff\xfe"
        length_field = struct.pack(">H", payload_size + 2)
        payload = b"\x00" * payload_size
        out += marker + length_field + payload
        need -= len(marker) + len(length_field) + payload_size
    out += rest
    return bytes(out)


RESULTS: dict[str, Any] = {
    "sdk_version": None,
    "claude_cli_version": None,
    "cwd": str(CWD),
    "q0_1_mixed_content": None,
    "q0_2_media_types": None,
    "q0_3_size_boundaries": None,
    "q0_4_multi_photo": None,
    "q0_5_order": None,
    "q0_5b_scheduler_url_image_combined": None,
    "q0_6_history_replay": None,
}


def _now() -> float:
    return time.monotonic()


async def _run_one(
    content_blocks: list[dict[str, Any]], *, timeout_s: float = 60.0
) -> dict[str, Any]:
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=[],
        max_turns=1,
        allowed_tools=[],
        system_prompt=(
            "You are a test harness. Reply succinctly, at most 200 chars. "
            "If an image is present, briefly describe what you see."
        ),
    )

    async def prompt_stream() -> Any:
        yield {
            "type": "user",
            "message": {"role": "user", "content": content_blocks},
            "parent_tool_use_id": None,
            "session_id": "spike-phase7-s0",
        }

    t0 = _now()
    assistant_text = ""
    error: str | None = None
    result_meta: dict[str, Any] = {}
    init_meta: dict[str, Any] = {}
    sdk_iter = query(prompt=prompt_stream(), options=opts)
    try:
        async with asyncio.timeout(timeout_s):
            async for message in sdk_iter:
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    init_meta = {
                        "model": (message.data or {}).get("model"),
                        "cwd": (message.data or {}).get("cwd"),
                    }
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            assistant_text += block.text
                elif isinstance(message, ResultMessage):
                    result_meta = {
                        "stop_reason": message.stop_reason,
                        "duration_ms": message.duration_ms,
                        "num_turns": message.num_turns,
                        "cost_usd": message.total_cost_usd,
                        "subtype": message.subtype,
                    }
                    break
    except TimeoutError:
        error = "timeout"
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        aclose = getattr(sdk_iter, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception as close_exc:  # noqa: BLE001
                error = (error or "") + f"; aclose: {close_exc!r}"

    return {
        "wall_s": round(_now() - t0, 3),
        "assistant_text": assistant_text[:2000],
        "assistant_text_len": len(assistant_text),
        "result_meta": result_meta,
        "init_meta": init_meta,
        "error": error,
    }


async def probe_q0_1_mixed_content() -> None:
    img = _tiny_jpeg_bytes()
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "What is in the attached image? Reply <100 chars."},
        _make_image_block(img),
        {"type": "text", "text": "[system-note: this is a multimodal envelope test]"},
    ]
    trace = await _run_one(blocks, timeout_s=60.0)
    verdict = "PASS" if (trace["assistant_text"] and not trace["error"]) else "FAIL"
    RESULTS["q0_1_mixed_content"] = {
        "verdict": verdict,
        "image_size_bytes": len(img),
        "block_count": len(blocks),
        "trace": trace,
    }
    print(f"[Q0-1] verdict={verdict} reply_len={trace['assistant_text_len']}")


async def probe_q0_2_media_types() -> None:
    img = _tiny_jpeg_bytes()
    per_type: dict[str, Any] = {}
    for mt in ("image/jpeg", "image/png", "image/webp"):
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": f"Reply with 'ok {mt}' in one line."},
            _make_image_block(img, media_type=mt),
        ]
        trace = await _run_one(blocks, timeout_s=45.0)
        per_type[mt] = trace
        print(f"[Q0-2] media_type={mt} error={trace['error']!r} reply_len={trace['assistant_text_len']}")
    all_ok = all(not per_type[mt]["error"] and per_type[mt]["assistant_text"] for mt in per_type)
    RESULTS["q0_2_media_types"] = {
        "verdict": "PASS" if all_ok else "PARTIAL",
        "per_type": per_type,
        "note": (
            "We passed real JPEG bytes under all three labels; SDK/backend "
            "may validate magic vs label. Record per-type outcome."
        ),
    }


async def probe_q0_3_size_boundaries() -> None:
    base = _tiny_jpeg_bytes()
    targets = [
        ("100KB", 100 * 1024),
        ("1MB", 1024 * 1024),
        ("3MB", 3 * 1024 * 1024),
        ("5MB", 5 * 1024 * 1024),
        ("10MB", 10 * 1024 * 1024),
    ]
    per_size: dict[str, Any] = {}
    for label, size in targets:
        try:
            bloated = _bloat_jpeg(base, size)
        except Exception as exc:  # noqa: BLE001
            per_size[label] = {"error": f"bloat_failed: {exc!r}"}
            continue
        b64_size = (len(bloated) + 2) // 3 * 4
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Image attached approx {label} raw (~{b64_size // 1024}KB base64). "
                    f"Reply 'ok {label}' only."
                ),
            },
            _make_image_block(bloated),
        ]
        trace = await _run_one(blocks, timeout_s=120.0)
        per_size[label] = {
            "raw_bytes": len(bloated),
            "b64_bytes": b64_size,
            "trace": trace,
        }
        print(f"[Q0-3] size={label} raw={len(bloated)} err={trace['error']!r} reply_len={trace['assistant_text_len']}")
    passes = [
        label
        for label, rec in per_size.items()
        if "trace" in rec and not rec["trace"]["error"] and rec["trace"]["assistant_text"]
    ]
    RESULTS["q0_3_size_boundaries"] = {
        "per_size": per_size,
        "largest_pass": passes[-1] if passes else None,
        "all_sizes_passed": sorted(passes),
    }


async def probe_q0_4_multi_photo() -> None:
    img = _tiny_jpeg_bytes()
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "Three images attached. Reply 'got N' where N is image count."},
        _make_image_block(img),
        _make_image_block(img),
        _make_image_block(img),
    ]
    trace = await _run_one(blocks, timeout_s=60.0)
    verdict = "PASS" if (trace["assistant_text"] and not trace["error"]) else "FAIL"
    RESULTS["q0_4_multi_photo"] = {
        "verdict": verdict,
        "image_count": 3,
        "trace": trace,
        "note": "Phase-7 Q12 decided OUT-OF-SCOPE; this probe confirms SDK capability for a later phase.",
    }


async def probe_q0_5_order() -> None:
    img = _tiny_jpeg_bytes()
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "[system-note: ORDER_PROBE_HEAD — FIRST in envelope]"},
        {"type": "text", "text": "User text: describe the image in 5 words."},
        _make_image_block(img),
        {"type": "text", "text": "[system-note: ORDER_PROBE_TAIL — LAST in envelope]"},
    ]
    trace = await _run_one(blocks, timeout_s=60.0)
    txt = trace["assistant_text"]
    head_seen = "ORDER_PROBE_HEAD" in txt
    tail_seen = "ORDER_PROBE_TAIL" in txt
    verdict = "PASS" if (not trace["error"] and trace["assistant_text"]) else "FAIL"
    RESULTS["q0_5_order"] = {
        "verdict": verdict,
        "head_observed_in_reply": head_seen,
        "tail_observed_in_reply": tail_seen,
        "trace": trace,
        "note": "Order-in-echo is weak signal; absence of error is primary.",
    }


async def probe_q0_5b_combined() -> None:
    img = _tiny_jpeg_bytes()
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "[system-note: autonomous turn from scheduler id=42; "
                "owner is not active; answer proactively and finish.]"
            ),
        },
        {
            "type": "text",
            "text": "глянь фото и скажи что на нём. https://example.com/foo тоже.",
        },
        {
            "type": "text",
            "text": (
                "[system-note: user message contains URL(s); "
                "if installing a skill, run preview first.]"
            ),
        },
        _make_image_block(img),
        {
            "type": "text",
            "text": (
                "[system-note: user attached photo at /abs/outbox/test.jpg (128x128).]"
            ),
        },
    ]
    trace = await _run_one(blocks, timeout_s=60.0)
    verdict = "PASS" if (not trace["error"] and trace["assistant_text"]) else "FAIL"
    RESULTS["q0_5b_scheduler_url_image_combined"] = {
        "verdict": verdict,
        "block_count": len(blocks),
        "trace": trace,
    }


async def probe_q0_6_history_replay() -> None:
    img = _tiny_jpeg_bytes()
    opts = ClaudeAgentOptions(
        cwd=str(CWD),
        setting_sources=[],
        max_turns=1,
        allowed_tools=[],
        system_prompt=(
            "You are a test harness. Keep replies <200 chars. "
            "Acknowledge if the conversation history mentions an image."
        ),
    )

    async def stream_mode_a() -> Any:
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Earlier I sent this photo:"},
                    _make_image_block(img),
                ],
            },
            "parent_tool_use_id": None,
            "session_id": "spike-phase7-s0-replay-a",
        }
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Did you see an image in the earlier turn? Reply yes/no.",
            },
            "parent_tool_use_id": None,
            "session_id": "spike-phase7-s0-replay-a",
        }

    async def stream_mode_b() -> Any:
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Earlier I sent a photo."},
                    {
                        "type": "text",
                        "text": "[system-note: prior user envelope contained an image at /abs/outbox/earlier.jpg]",
                    },
                ],
            },
            "parent_tool_use_id": None,
            "session_id": "spike-phase7-s0-replay-b",
        }
        yield {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Did you see an image in the earlier turn? Reply yes/no.",
            },
            "parent_tool_use_id": None,
            "session_id": "spike-phase7-s0-replay-b",
        }

    async def _drive(stream: Any, tag: str) -> dict[str, Any]:
        t0 = _now()
        assistant_text = ""
        error: str | None = None
        sdk_iter = query(prompt=stream(), options=opts)
        try:
            async with asyncio.timeout(90.0):
                async for message in sdk_iter:
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                assistant_text += block.text
                    elif isinstance(message, ResultMessage):
                        break
        except TimeoutError:
            error = "timeout"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        finally:
            aclose = getattr(sdk_iter, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass
        return {
            "tag": tag,
            "wall_s": round(_now() - t0, 3),
            "assistant_text": assistant_text[:2000],
            "assistant_text_len": len(assistant_text),
            "error": error,
        }

    mode_a = await _drive(stream_mode_a, "mode_a_raw_image")
    mode_b = await _drive(stream_mode_b, "mode_b_placeholder")

    RESULTS["q0_6_history_replay"] = {
        "mode_a": mode_a,
        "mode_b": mode_b,
        "verdict": (
            "PASS"
            if (
                not mode_a["error"]
                and not mode_b["error"]
                and mode_a["assistant_text"]
                and mode_b["assistant_text"]
            )
            else "PARTIAL"
        ),
        "note": (
            "Phase-7 plan chose mode B (placeholder) to avoid re-uploading "
            "bytes. Mode A remains SDK-valid fallback."
        ),
    }


async def main() -> None:
    try:
        import claude_agent_sdk

        RESULTS["sdk_version"] = claude_agent_sdk.__version__
    except Exception as exc:  # noqa: BLE001
        RESULTS["sdk_version"] = f"error: {exc!r}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _err = await proc.communicate()
        RESULTS["claude_cli_version"] = out.decode().strip()
    except Exception as exc:  # noqa: BLE001
        RESULTS["claude_cli_version"] = f"error: {exc!r}"

    probes: list[tuple[str, Any]] = [
        ("q0_1_mixed_content", probe_q0_1_mixed_content),
        ("q0_2_media_types", probe_q0_2_media_types),
        ("q0_5_order", probe_q0_5_order),
        ("q0_5b_combined", probe_q0_5b_combined),
        ("q0_4_multi_photo", probe_q0_4_multi_photo),
        ("q0_6_history_replay", probe_q0_6_history_replay),
        ("q0_3_size_boundaries", probe_q0_3_size_boundaries),
    ]

    for name, fn in probes:
        if os.environ.get("PHASE7_SKIP", "").find(name) >= 0:
            print(f"[SKIP] {name}")
            continue
        print(f"--- running {name} ---")
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {name}: {exc!r}")
            traceback.print_exc()
            RESULTS[name] = {"verdict": "FAIL", "error": repr(exc)}

    REPORT.write_text(json.dumps(RESULTS, indent=2, ensure_ascii=False))
    print(f"\nReport -> {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
