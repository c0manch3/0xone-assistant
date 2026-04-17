# Phase 3 — Spike Findings (2026-04-17)

Empirical answers to S1–S5 from `description.md`. Every claim here is backed
by an executed command on this host or by output captured in
`spikes/sdk_probe_posthook_report.json` / `spikes/marketplace_probe_report.json`.

Host: macOS Darwin 24.6.0, `gh` 2.89.0, Python 3.12.x via `uv`,
`claude-agent-sdk` pinned `>=0.1.59,<0.2`.

---

## S1. Anthropic marketplace structure

**S1.a — Skills path.** All 17 skills live under `skills/`, not the repo root.

```text
$ gh api /repos/anthropics/skills/contents/skills
# -> 17 entries, every entry has type=="dir", names sorted below
algorithmic-art        canvas-design    doc-coauthoring   frontend-design
internal-comms         mcp-builder      pptx              skill-creator
slack-gif-creator      theme-factory    web-artifacts-builder   webapp-testing
xlsx                   pdf              docx              brand-guidelines
claude-api
```

Repo root also contains `spec/`, `template/`, `README.md`,
`THIRD_PARTY_NOTICES.md`. `MARKETPLACE_BASE_PATH = "skills"` is load-bearing;
using `/contents/` on the bare repo mixes real skills with these extras.

**S1.b — SKILL.md frontmatter shape.** Sampled 5 skills
(`skill-creator`, `pdf`, `docx`, `mcp-builder`, `claude-api`) via
`gh api /repos/anthropics/skills/contents/skills/<name>/SKILL.md` →
base64-decoded. **None** of them declares `allowed-tools`. `name` +
`description` are always present; `license` appears on 4/5 (absent on
`skill-creator`). Raw captures in
`spikes/marketplace_probe_report.json` (`samples_any_allowed_tools: false`).

Devil's prediction confirmed: phase 3 MUST ship the B2 patch
(§1b of detailed-plan) — without permissive default the Anthropic
`skill-creator` boot produces a skill with `allowed_tools=[]`, which
phase-2 semantics read as "explicit lockdown".

**S1.c — Bundle tree (depth=1 git clone audit).** `git clone --depth=1
https://github.com/anthropics/skills` on every bundle:

| Property | Observed |
|---|---|
| Symlinks anywhere under `skills/` | **0** (`find skills -type l` → empty) |
| Top-level `tools/` inside any bundle | **0** (`find skills -maxdepth 2 -type d -name tools` → empty) |
| Files >2 MB | **0** |
| Max files per bundle | **83** (`canvas-design`) — under `MAX_FILES=100` |
| Max bundle size | **5.5 MB** (`canvas-design`) — under `MAX_TOTAL=10 MB` |
| `skill-creator` layout | `SKILL.md` + `agents/`, `assets/`, `eval-viewer/`, `references/`, `scripts/` + `LICENSE.txt` (18 files / 248 KB) |

**Consequence for §4c `atomic_install`:** for Anthropic bundles the optional
`tools/` split path is dead code on every bundle available today. Keep the
code (future-proof) but the tests cover the `no-inner-tools/` branch, which
is the actual production path for `skill-creator` bootstrap.

---

## S2. `gh` CLI behaviour

**S2.a — Query string support.**
`gh api "/repos/anthropics/skills/contents/skills?ref=main"` works; returns the
same 17-entry list. Query strings must be passed as a single quoted arg.
The regex `_GH_API_SAFE_ENDPOINT_RE` in §1a handles this via the optional
`(\?[^\s]*)?$` tail — no change needed.

**S2.b — Pagination.**
`gh api --paginate /repos/anthropics/skills/contents/skills` emits a single
JSON array (same 17 entries, no wrapping, no newline-delimited JSON). For
the phase-3 marketplace the directory is small (well under the 100-entry
default page size); `--paginate` is unnecessary and adds parsing surface —
**do NOT** include it in the allowlist. Skill count cap is enforced by
`MAX_FILES=100` + GitHub's own page limits.

**S2.c — `gh auth status`.**
- Logged in: rc=0, stderr contains `✓ Logged in`.
- `shutil.which("gh")` returns `/opt/homebrew/bin/gh` on this host.
- Daemon uses `shutil.which` only (not `gh auth status`) — auth is
  irrelevant for public unauthenticated reads; we accept the 60 req/hour
  anonymous cap.

**S2.d — `gh api` return codes.** `gh api` **returns rc=0 even for HTTP
404** (the JSON body contains `"status":"404"`). Marketplace wrapper MUST
parse stdout and detect `message`/`status` fields rather than trusting rc
alone.

**S2.e — `gh api -X DELETE` without our allowlist.** Without bot-side
allowlisting, the CLI *attempts* the request and fails only if GitHub
returns 403. Bot allowlist must reject `-X`/`--method` **before** argv ever
reaches `gh`, not rely on remote-side authz.

---

## S3. SDK hooks API — PostToolUse

**S3.a — Coexistence.** `ClaudeAgentOptions.hooks` accepts a dict keyed on
`HookEvent` literals; `PreToolUse` and `PostToolUse` coexist in the same
dict without issue. Probe:

```python
hooks={
    "PreToolUse":  [HookMatcher(matcher="Write", hooks=[pre_hook])],
    "PostToolUse": [HookMatcher(matcher="Write", hooks=[post_hook])],
}
```

Empirically verified against live `claude` CLI + `claude-agent-sdk` —
see `spikes/sdk_probe_posthook_report.json`:

```json
{"completed": true, "stop_reason": "end_turn",
 "pre_calls":  [{"tool_name":"Write","file_path":"/tmp/.../hello.txt", ...}],
 "post_calls": [{"tool_name":"Write","file_path":"/tmp/.../hello.txt",
                 "has_tool_response": true, ...}]}
```

**S3.b — `HookEvent` literal set (SDK 0.1.59).**

```text
'PreToolUse' | 'PostToolUse' | 'PostToolUseFailure' | 'UserPromptSubmit' |
'Stop' | 'SubagentStop' | 'PreCompact' | 'Notification' |
'SubagentStart' | 'PermissionRequest'
```

`PostToolUseFailure` is a **separate** event (invoked when the tool itself
errored) — phase 3 does not need it for sentinel (no file was written on
failure), but note its existence for future recovery logic.

**S3.c — `PostToolUseHookInput` keys.**

```text
{ session_id, transcript_path, cwd, permission_mode, agent_id, agent_type,
  hook_event_name: Literal['PostToolUse'], tool_name, tool_input, tool_response,
  tool_use_id }
```

Same shape as `PreToolUseHookInput` **plus `tool_response: Any`**. For
`Write`, `tool_input = {"file_path": str, "content": str}`;
`file_path` is the **absolute** path the model targeted. For `Edit`,
`tool_input = {"file_path": str, "old_string": str, "new_string": str}`.

**S3.d — Return shape.** Returning `{}` is a valid no-op. Probe `post_hook`
returned `{}` and the stream completed with `stop_reason=end_turn`. We do
NOT need any `hookSpecificOutput` for PostToolUse — the hook is fire-and-
forget for side effects only.

**S3.e — Callback signature.** Inner hook signature (mypy strict) is the
same across Pre/Post:

```python
async def hook(
    input_data: PostToolUseHookInput | ...,   # union; narrow by hook_event_name
    tool_use_id: str | None,
    ctx: HookContext,
) -> AsyncHookJSONOutput | SyncHookJSONOutput: ...
```

`HookMatcher` is event-neutral — same dataclass works for both buckets.

---

## S4. `copytree` + symlinks

**S4.a — Symlinks in anthropics/skills.** None. `find skills -type l`
returns nothing (see S1.c). The symlink-rejection validator is defensive
policy, not a reaction to observed bundles.

**S4.b — `shutil.copytree` behaviour with `symlinks=True`.** A symlink at
the source preserves as a symlink at the destination, pointing at the same
target (verified on a fixture with `link -> /etc/passwd`):

```text
copytree(src, dst, symlinks=True)
-> dst/link is symlink: True, readlink -> /etc/passwd
```

With `symlinks=False` (the default), `copytree` **follows** the link and
writes the linked content to the destination — which is exactly the
smuggle path we must reject. Phase 3 MUST call `copytree(..., symlinks=True)`
AND the validator MUST reject any symlink before `copytree` is even reached.
Defence in depth: if someone ever flips `symlinks=False`, the validator
still blocks; if someone ever removes the validator, `symlinks=True`
preserves the link name but the absolute path `/etc/passwd` obviously
cannot resolve inside the bundle post-install, so the attack shifts from
exfil-on-install to exfil-on-skill-read — still bad, but narrower.

---

## S5. `sha256_of_tree` determinism

**S5.a — Idempotence.** Two consecutive calls on the unchanged
`/tmp/.../skill-creator` bundle returned identical hex digests:

```text
pass1: ebd819ebd15af031ef8b69c2967cd67940233650eb3e4e5d31a0669a94c13624
pass2: ebd819ebd15af031ef8b69c2967cd67940233650eb3e4e5d31a0669a94c13624
```

**S5.b — Mutation sensitivity.** Appending `\n# toctou` to `SKILL.md`
flipped the digest after the first byte-block, as expected
(`h1=ebd819eb… -> h2=88684e1f…`).

**S5.c — Canonical ordering.** `sorted(root.rglob("*"))` over `Path`
objects yields filesystem-order-agnostic output because `Path` sorts on the
string repr (POSIX separator on macOS/Linux). Add `.as_posix()` in the
relative path encoding — already present in the `detailed-plan §4b`
snippet. The length-prefix `len(rel).to_bytes(4, "big")` + null
terminator + `len(data).to_bytes(8, "big")` guarantees unambiguous
framing (two files with concatenated names cannot collide with one file of
the merged name).

**S5.d — Symlink handling inside the hasher.** `if p.is_file() and not
p.is_symlink()` — if a symlink survived the validator, the hasher will
skip it (so the TOCTOU digest still stabilises). This is a double-safety:
the validator rejects first, but even if it didn't, the hash ignores
symlinks rather than following them. `p.is_file()` alone *follows* symlinks
by default, so the explicit `not p.is_symlink()` clause is load-bearing.

---

## Artifacts

- `/Users/agent2/Documents/0xone-assistant/spikes/sdk_probe_posthook.py`
  (executed OK; report in `sdk_probe_posthook_report.json`).
- `/Users/agent2/Documents/0xone-assistant/spikes/marketplace_probe.py`
  (executed OK; report in `marketplace_probe_report.json`).

## Citations

- `gh api` REST examples — cli.github.com manual page (`gh-api(1)`).
- `claude_agent_sdk.types.HookEvent` literal union — SDK 0.1.59 source.
- `anthropics/skills` live structure — observed 2026-04-17 via `gh api`
  and `git clone --depth=1 https://github.com/anthropics/skills`.
- Python `shutil.copytree(symlinks=...)` semantics — docs.python.org
  `library/shutil.html#shutil.copytree` ("When symlinks is True, symbolic
  links in the source tree are represented as symbolic links in the new
  tree").
- Python `pathlib.Path.is_file()` follows symlinks — docs.python.org
  `library/pathlib.html#pathlib.Path.is_file` ("This method normally
  follows symlinks").
