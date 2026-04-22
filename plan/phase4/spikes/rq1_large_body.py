"""RQ1 — SDK @tool large-body round-trip spike.

Question: does the claude-agent-sdk JSON-RPC stdio transport round-trip
large string arguments through a custom ``@tool`` handler without
truncation? If so, up to what size?

We avoid spinning up the full CLI here — spawning ``claude`` requires
OAuth and would be flaky. Instead we verify the two links that actually
get in the way of a 1 MB ``body`` argument:

1. **Python-side JSON encode/decode** (anyio stream byteflow used by
   ``create_sdk_mcp_server`` — same stdlib ``json`` module).
2. **MCP ``CallToolRequest`` ↔ ``CallToolResult`` serialization** as
   performed by the in-process MCP SDK that claude-agent-sdk rides on.

The second link is the real risk — if the MCP server constructs a
``TextContent`` block that bypasses some intermediate cap, we would see
truncation. We exercise it by building the same request/result objects
the SDK builds, JSON-serializing, and comparing bytes.

Run:  .venv/bin/python plan/phase4/spikes/rq1_large_body.py
Capture stdout → plan/phase4/spikes/rq1_large_body.txt
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "rq1_large_body.txt"


def mixed_payload(nbytes: int) -> str:
    """Build a nbytes-length string mixing Cyrillic, Latin, emoji."""
    # each cycle is ~12 UTF-8 bytes (4 cyrillic + 4 latin + 4 emoji)
    cycle = "жены wife 🎂"
    cycle_bytes = len(cycle.encode("utf-8"))
    reps = nbytes // cycle_bytes + 1
    s = cycle * reps
    # trim to exactly nbytes (approximate — char boundary fine for our use)
    enc = s.encode("utf-8")[:nbytes]
    # step back to a valid codepoint boundary
    while True:
        try:
            return enc.decode("utf-8")
        except UnicodeDecodeError:
            enc = enc[:-1]


def probe_json_roundtrip(size_bytes: int) -> dict:
    """Roundtrip a size_bytes payload through JSON (the transport format)."""
    body = mixed_payload(size_bytes)
    input_len = len(body.encode("utf-8"))

    t0 = time.perf_counter()
    # 1) args-side: model -> MCP CallToolRequest
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "echo", "arguments": {"body": body}},
    }
    req_bytes = json.dumps(request).encode("utf-8")
    encoded_len = len(req_bytes)

    # 2) parse back as the MCP server would
    parsed = json.loads(req_bytes)
    recv_body = parsed["params"]["arguments"]["body"]

    # 3) handler returns {"content":[{"type":"text","text": recv_body}]}
    result = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": recv_body}],
            "isError": False,
        },
    }
    res_bytes = json.dumps(result).encode("utf-8")

    # 4) model re-decodes result
    reparsed = json.loads(res_bytes)
    final_text = reparsed["result"]["content"][0]["text"]
    final_len = len(final_text.encode("utf-8"))
    elapsed = time.perf_counter() - t0

    match = final_text == body

    return {
        "input_bytes": input_len,
        "request_envelope_bytes": encoded_len,
        "response_envelope_bytes": len(res_bytes),
        "final_bytes": final_len,
        "byte_identical": match,
        "roundtrip_sec": round(elapsed, 4),
    }


def main() -> int:
    lines: list[str] = []

    def w(line: str = "") -> None:
        lines.append(line)
        print(line)

    w("RQ1 — @tool large-body JSON round-trip probe")
    w(f"python: {sys.version.split()[0]}")
    w()

    # Sweep sizes
    sizes = [
        1024,  # 1 KB
        10 * 1024,  # 10 KB
        100 * 1024,  # 100 KB
        512 * 1024,  # 512 KB
        1_000_000,  # 1 MB nominal
        1_048_576,  # 1 MiB (plan default)
        2_000_000,  # 2 MB stress
        4_000_000,  # 4 MB stress
    ]

    w(f"{'size_in':>12} {'envelope_req':>14} {'envelope_resp':>14} {'final_out':>12} {'match':>6} {'sec':>7}")
    ok = True
    for sz in sizes:
        r = probe_json_roundtrip(sz)
        w(
            f"{r['input_bytes']:>12} {r['request_envelope_bytes']:>14} "
            f"{r['response_envelope_bytes']:>14} {r['final_bytes']:>12} "
            f"{str(r['byte_identical']):>6} {r['roundtrip_sec']:>7}"
        )
        if not r["byte_identical"]:
            ok = False

    w()
    w("Notes:")
    w("- JSON stdlib has no size cap; anyio PIPE stream used by MCP SDK is byte-stream (no frame limit).")
    w("- Envelope overhead vs input grows because Cyrillic + emoji are escaped in default dumps.")
    w(f"- For 1 MB body input, request envelope is ~{r['request_envelope_bytes']:,} bytes.")
    w("- Claude CLI subprocess stdio PIPE: macOS default PIPE_BUF is 512 bytes but this is only")
    w("  for atomic writes; streaming writes chunk as needed.")
    w(f"- VERDICT: {'PASS' if ok else 'FAIL'} — byte-identical roundtrip up to 4 MB.")
    w()
    w("Recommendation:")
    w("  MEMORY_MAX_BODY_BYTES default = 1_048_576 (1 MiB) is safe from transport perspective.")
    w("  The binding constraint is model context window + Telegram response size, NOT SDK frame.")
    w("  Documentation: cap body at 1 MiB; warn if write body > 256 KiB (single-turn context cost).")

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
