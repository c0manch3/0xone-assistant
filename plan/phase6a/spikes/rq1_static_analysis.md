# RQ1 — SDK Read tool with PDF inside Docker (static analysis + plan)

**Live container test deferred to owner** — no Docker daemon on this Mac. We
analysed the hook source and propose two unblock paths plus a concrete
Settings/hook patch sketch for whichever the owner picks.

## What the hook actually enforces

`src/assistant/bridge/hooks.py:421-485` — `make_file_hook(project_root)`.

```
root = project_root.resolve()                          # /app inside container

# For Read/Write/Edit:
if not raw:                                            # B9 fix
    return _deny(f"{tool_name} requires file_path")
candidate = str(raw)

p = Path(candidate).expanduser()
resolved = p.resolve() if p.is_absolute() else (project_root / p).resolve()

if not resolved.is_relative_to(root):
    return _deny(f"Path outside project_root ({root}) is not allowed: {resolved}")
return _allow()
```

**Verdict, before any change:** a `Read(file_path="/home/bot/.local/share/0xone-assistant/tmp/X.pdf")`
call is **denied** in the bot process — `/home/bot/...` is not relative to
`/app`. The CLI bypasses this hook (hooks fire only inside the
`claude_agent_sdk` Python process the bot owns), so a manual
`docker exec ... claude --print "Read /home/bot/..."` will succeed and
prove the underlying multimodal-PDF capability — but that does NOT prove
the bot can do it. The bot's hook is the real gate.

## Two unblock options

### Option 1 — Move tmp dir inside `project_root`

- New tmp path: `/app/.uploads/<uuid>.<ext>`.
- Container side: `RUN mkdir -p /app/.uploads && chown 1000:1000 /app/.uploads`.
  Phase-5d already chowned `/app` to uid 1000, so this is a one-line
  `RUN` in the Dockerfile.
- **Pros:** zero Settings/hook changes. The hook is unchanged.
- **Cons:** writes inside `/app` mix runtime data with read-only image
  content. The container layer is COW so writes go to the upper layer
  — survives `docker exec` but is wiped on `docker run --rm`. For a
  daemon container that's fine.
- **Vault leak risk:** vault is at `<data_dir>/vault/` (outside `/app`),
  so `tmp/` inside `/app` cannot pollute vault. Phase-7 git push
  operates on `<data_dir>/vault` only — unaffected.
- **Settings impact:** add a config knob `Settings.upload_tmp_dir:
  Path = Field(default_factory=lambda: project_root / ".uploads")` so
  dev (Mac) and container (Linux) can override.

### Option 2 — Allow-list extension in `make_file_hook`

Sketch:

```python
# config.py
class Settings(BaseSettings):
    ...
    extra_read_root: Path | None = None  # 6a: tmp dir for uploads
```

```python
# bridge/hooks.py
def make_file_hook(
    project_root: Path,
    extra_root: Path | None = None,  # NEW
) -> Hook:
    root = project_root.resolve()
    extra = extra_root.resolve() if extra_root else None

    async def file_hook(...):
        ...
        ok = resolved.is_relative_to(root)
        if not ok and extra is not None:
            ok = resolved.is_relative_to(extra)
        if not ok:
            return _deny(...)
        return _allow()
    return file_hook
```

Wire-up at `bootstrap.py` — pass `settings.data_dir / "tmp"` as
`extra_root`.

- **Pros:** keeps tmp where the plan put it; no Dockerfile change.
- **Cons:** widens the file-tool blast radius. If a future @tool writes
  user-controlled paths through `Read`, it could now reach
  `~/.local/share/0xone-assistant/tmp/` AND any sibling under it
  (`memory-audit.log`, `scheduler-audit.log`, `assistant.db`, vault).
  We'd need `extra_root = data_dir / "tmp"` *exactly* — not `data_dir`
  itself — and an explicit BW1-style `is_relative_to` containment
  check on `tmp/`, NOT a string-prefix match. The hook code above
  does this correctly.
- **DB exposure:** `<data_dir>/assistant.db` lives at `data_dir/`,
  NOT under `data_dir/tmp/`. With the precise `extra_root = data_dir /
  "tmp"`, the DB stays unreachable.

### Recommendation

**Option 1 (move tmp into `/app/.uploads/`)**. Reasons:

1. Smallest hook surface. The hook stays as-is; less to audit.
2. No new Settings field crossing into the security-critical bridge.
3. Phase-5d already chowned `/app` — minimal Dockerfile delta (1 line).
4. Vault separation preserved (vault still at `<data_dir>/vault`).
5. Owner can keep the tmp-prune sweep semantics identical.

**Trade-off accepted:** dev mode (Mac, no container) writes uploads to
`<repo>/.uploads/`. Add to `.gitignore`. Acceptable — phase 5d is
container-only, dev mode is mostly for unit tests.

## Owner-side live verification (when running the spike on VPS)

Once the bot has either patch applied and is restarted:

```bash
# 1. Drop a tiny text-PDF.
docker exec 0xone-assistant sh -c \
    'echo "%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]/Contents 4 0 R>>endobj
4 0 obj<</Length 44>>stream
BT /F1 24 Tf 10 100 Td (HELLO PHASE 6A) Tj ET
endstream endobj
xref
0 5
trailer<</Size 5/Root 1 0 R>>
%%EOF" > /app/.uploads/test.pdf'

# 2. Send a Telegram message attaching test.pdf with caption "what does it say".
# Watch the bot log for `pretool_decision tool_name=Read decision=allow`.

# 3. If decision=deny → unblock patch hasn't landed; recheck Settings wire.
# 4. If allow but model output mentions no text → multimodal PDF
#    behaviour of SDK 0.1.59 isn't what we expected; fall back to
#    Option B (pypdf pre-extract) for PDFs.
```

## What CANNOT be tested off-VPS

- Whether SDK 0.1.59's `Read` tool actually multimodal-renders a PDF.
  The plan footnote at §B claims it does. The CLI documentation at
  https://docs.claude.com/en/docs/claude-code/sdk says `Read` returns
  text for text files; PDF support is in the underlying API but
  whether the CLI's `Read` propagates the multimodal payload through
  the OAuth path is exactly what RQ1 must verify.
- Whether the path-allow patch we land actually gets exercised at runtime
  (vs. some earlier hook intercepting). Owner smoke-test confirms.

## Decision

**PASS contingent on owner's container test.** Recommend Option 1
(move tmp into `/app/.uploads/`) before owner runs the test — minimises
hook surface. If multimodal `Read` falls flat in the live test
(empty output, unsupported-format error), fall back to Option B
(pypdf pre-extract uniform for PDFs).
