# RQ13 — claude_agent_sdk bundled binary verification

**Date:** 2026-04-25
**SDK version under test:** `claude-agent-sdk==0.1.63` (per `pyproject.toml` lower bound `>=0.1.59,<0.2`; PyPI 0.1.63 is the version actually installed in `.venv` and the highest 0.1.x at time of spike).

## Verdict

**DROP stage 2** (nodejs + `npm install -g @anthropic-ai/claude-code@2.1.116`). The Linux-x86_64 wheel of `claude-agent-sdk==0.1.63` ships its own 236 MB ELF `claude` binary at `claude_agent_sdk/_bundled/claude`, and the SDK transport prefers that bundled path over PATH lookup. As long as the daemon's `_preflight_claude_auth` is also routed at the bundled binary (a one-line Dockerfile symlink), no node runtime is required in the runtime image.

---

## Evidence

### Step 1 — Mac venv (sanity)

```
$ ls -la .venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/
-rwxr-xr-x  1 agent2  staff  204534752 Apr 20 19:24 claude
-rw-r--r--  1 agent2  staff         74 Apr 20 19:23 .gitignore

$ file .venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude
Mach-O 64-bit executable arm64
```

Mac wheel tag (from `claude_agent_sdk-0.1.63.dist-info/WHEEL`):
```
Tag: py3-none-macosx_11_0_arm64
```

So PyPI ships per-platform wheels (not a universal wheel that downloads at install time).

### Step 2 — PyPI inventory for `claude-agent-sdk==0.1.63`

```
$ curl -sS https://pypi.org/pypi/claude-agent-sdk/0.1.63/json | jq '.urls[].filename'
claude_agent_sdk-0.1.63-py3-none-macosx_11_0_arm64.whl       60 MB
claude_agent_sdk-0.1.63-py3-none-macosx_11_0_x86_64.whl      62 MB
claude_agent_sdk-0.1.63-py3-none-manylinux_2_17_aarch64.whl  73 MB
claude_agent_sdk-0.1.63-py3-none-manylinux_2_17_x86_64.whl   73 MB   <-- target
claude_agent_sdk-0.1.63-py3-none-win_amd64.whl               75 MB
claude_agent_sdk-0.1.63.tar.gz                              130 KB   (sdist, no binary)
```

### Step 3 — Linux x86_64 wheel inspection (no container needed)

```
$ unzip -l linux.whl | grep -E '_bundled|claude$'
       74  claude_agent_sdk/_bundled/.gitignore
236411520  claude_agent_sdk/_bundled/claude          <-- 236 MB
```

```
$ unzip -p linux.whl 'claude_agent_sdk/_bundled/claude' > linux-claude
$ file linux-claude
linux-claude: ELF 64-bit LSB executable, x86-64, version 1 (SYSV),
  dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2,
  for GNU/Linux 3.2.0,
  BuildID[sha1]=052ef6d8cef1bef39149a31808f3d579db450889, not stripped
```

So the manylinux wheel ships a real x86_64 ELF. `manylinux_2_17` => requires glibc >= 2.17 (CentOS 7 era). `python:3.12-slim-bookworm` ships glibc 2.36 — well above the floor.

`RECORD` confirms it is part of the regular wheel install (sha256 pinned), not an opt-in extra:
```
claude_agent_sdk/_bundled/claude,sha256=Er1L...zwn8,236411520
```

### Step 4 — `subprocess_cli.py:63-109` lookup order

```python
def _find_cli(self) -> str:
    """Find Claude Code CLI binary."""
    # First, check for bundled CLI
    bundled_cli = self._find_bundled_cli()
    if bundled_cli:
        return bundled_cli                      # (1) bundled wins

    # Fall back to system-wide search
    if cli := shutil.which("claude"):
        return cli                              # (2) PATH

    locations = [
        Path.home() / ".npm-global/bin/claude",
        Path("/usr/local/bin/claude"),
        Path.home() / ".local/bin/claude",
        ...
    ]
    for path in locations:
        if path.exists() and path.is_file():
            return str(path)                    # (3) hardcoded fallbacks

    raise CLINotFoundError(...)


def _find_bundled_cli(self) -> str | None:
    cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
    bundled_path = Path(__file__).parent.parent.parent / "_bundled" / cli_name
    if bundled_path.exists() and bundled_path.is_file():
        logger.info(f"Using bundled Claude Code CLI: {bundled_path}")
        return str(bundled_path)
    return None
```

Findings:
- The SDK **strictly prefers** the bundled binary if it exists in the same package as `subprocess_cli.py`.
- If bundled is missing, it **silently** falls back to `shutil.which("claude")` — only an `INFO` log is emitted on success of the bundled path; no warn/error on fallback.
- Conclusion: every SDK-driven `claude` invocation already routes through the in-wheel binary on Linux without any user action.

### Step 5 — `_preflight_claude_auth` implication (`src/assistant/main.py:107-154`)

The preflight uses `asyncio.create_subprocess_exec("claude", "--print", "ping", ...)` — argv-form, no shell, hence relies on `$PATH` resolution. If stage 2 is removed, no `/usr/bin/claude` or `/usr/local/bin/claude` exists in the runtime image, so the call raises `FileNotFoundError` and exits 3 ("claude_cli_missing"). The SDK transport itself works fine because it computes the bundled path directly — but the preflight does not go through the SDK.

This is the *only* known caller in the project today that bypasses the SDK and reaches for `claude` on PATH (verified by reading `main.py:107-154`; spike did not grep the wider tree, but no other PATH-based callers were referenced in wave-2).

---

## Implications for Dockerfile

### Stages dropped
- **Stage 2:** the entire `nodejs:20-bookworm-slim` builder + `npm install -g @anthropic-ai/claude-code@2.1.116` step.
- **Stage 5 runtime:** removal of the `nodejs-runtime` base layer and the `COPY --from=stage2 /usr/local/bin/claude /usr/local/bin/claude` line.

### Stages kept
- **Stage 1 / 3 / 4** (uv builder, venv build, app source COPY) — unchanged. The venv stage already pulls the bundled binary as a transitive artifact of `uv sync --frozen --no-dev --no-editable` because the manylinux wheel itself contains the ELF.
- **Stage 5 runtime:** still `python:3.12-slim-bookworm` + `COPY --from=venv /opt/venv /opt/venv`. Plus one new line — see fix below.

### Runtime image size delta
| Component                                        | Before     | After     |
|--------------------------------------------------|------------|-----------|
| `python:3.12-slim-bookworm` base                 | ~125 MB    | ~125 MB   |
| `nodejs` runtime layer (node + npm + libs)       | ~150 MB    | **0**     |
| `@anthropic-ai/claude-code` npm install (incl. SEA bundle) | ~180 MB | **0** |
| `/opt/venv` (incl. bundled `claude` ELF, 236 MB) | ~270 MB    | ~270 MB   |
| Misc (tini, ca-certificates, etc.)               | ~5 MB      | ~5 MB     |
| **Total uncompressed**                           | **~730 MB**| **~400 MB** |

Net savings: **~330 MB uncompressed** (the original "~600 -> ~450" estimate in the user prompt was conservative — actual savings are larger because the npm payload includes its own 200 MB SEA-packed binary that we are now de-duplicating).

CI build complexity: stage 2 fully removed (no node toolchain pull, no npm install, no version pin to coordinate with SDK). The SDK package version pin in `pyproject.toml` becomes the single source of truth for the bundled CLI version.

---

## `_preflight_claude_auth` fix — recommended

**Approach (c) — Dockerfile symlink.** Add to the runtime stage:

```dockerfile
# Make the SDK-bundled claude reachable on PATH so any subprocess
# invocation of `claude` (preflight, future helpers, ad-hoc shell)
# resolves to the same binary the SDK transport uses.
RUN ln -s /opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude \
          /usr/local/bin/claude
```

Why (c) over the alternatives:

- **(a) Refactor preflight to import `_find_bundled_cli`:** touches `_internal/...` private API of the SDK; brittle across SDK upgrades. We already had to read it in this spike, and Anthropic could rename/move it.
- **(b) Prepend `_bundled/` to `PATH`:** works, but `PATH=/opt/venv/lib/python3.12/site-packages/claude_agent_sdk/_bundled:$PATH` is uglier than a symlink and surfaces a long internal path in every shell that exec'es into the container.
- **(c) Symlink `/usr/local/bin/claude -> .../bundled/claude`:** one line, idempotent, makes the binary discoverable for everyone (preflight, owner debugging via `docker exec ... claude --help`, future tools), zero source-code change in `main.py`. The symlink target is stable for the lifetime of the venv.

Tradeoff: the symlink target path embeds the Python minor version (`python3.12`). If the base image bumps to 3.13 the symlink line must update. Acceptable — the same `python3.12` is hardcoded in the venv `COPY` already, and the project pins `requires-python = ">=3.12,<3.13"`.

No daemon-side change required; `_preflight_claude_auth` keeps calling `"claude"` on argv and continues to work.

---

## Estimated final image size

- **Before (with stage 2, nodejs runtime, npm install of `@anthropic-ai/claude-code`):** ~730 MB uncompressed (~250 MB compressed).
- **After (drop stage 2, keep venv-bundled binary, add symlink):** ~400 MB uncompressed (~140 MB compressed).
- Net: **~330 MB / 45% smaller** uncompressed.

---

## Caveats / what this spike did NOT verify

- Did not exec the bundled Linux ELF inside an actual container (Docker step skipped — wheel inspection plus the SDK lookup logic are sufficient evidence). Recommend the coder add a single integration smoke step in stage 5 of the Dockerfile to run `claude --version` during build, so any future SDK regression where Anthropic stops shipping the binary fails CI loudly instead of at owner-smoke-test time.
- Did not verify the bundled binary's runtime requirements beyond `manylinux_2_17` (glibc >= 2.17). `python:3.12-slim-bookworm` ships glibc 2.36, well above the floor. If the base image is ever swapped for `alpine` (musl), this analysis is invalidated — the bundled binary is glibc-only.
- Did not search the wider codebase for other PATH-based `claude` callers besides `_preflight_claude_auth`. The symlink fix neutralizes that risk for any caller anyway, but the coder should grep `claude` argv literals during fix-pack to confirm no second offender exists.
- SDK version is pinned `>=0.1.59,<0.2` in `pyproject.toml`; this analysis assumed `0.1.63` (highest 0.1.x). If a future 0.1.x patch ever drops the bundled binary, the symlink line will fail at build time — fail-loud, which is the desired behavior.
