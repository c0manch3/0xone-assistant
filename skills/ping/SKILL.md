---
name: ping
description: Healthcheck skill. Runs the ping CLI which prints {"pong": true}. Use when the user says "use the ping skill" or asks to verify skill discovery.
allowed-tools: [Bash]
---

# ping

Run `python tools/ping/main.py` via Bash. The tool prints a single JSON line
`{"pong": true}`. Report the parsed value back to the user.
