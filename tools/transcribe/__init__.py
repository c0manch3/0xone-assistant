"""transcribe tool — HTTP thin client for mlx-whisper via SSH reverse tunnel.

Phase-7 (S-1): the CLI MUST refuse non-loopback endpoints because the host
is reachable only on `127.0.0.1:<port>`. Package exists so tests can
`from tools.transcribe._net_mirror import is_loopback_only` directly.
"""
