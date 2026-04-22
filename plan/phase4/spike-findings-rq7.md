# RQ7 Spike — @tool flat-dict optional args

## Result

**(a) Strict.** The flat-dict `input_schema` form
(`{"query": str, "area": str, "limit": int}`) is compiled by the SDK
into a JSON Schema with **all keys required**. Missing keys are
rejected by the MCP layer **before** the `@tool` handler runs, with
`is_error=True` and message `Input validation error: '<key>' is a
required property`.

Extra keys (not declared in the schema) are **forwarded verbatim** to
the handler — additionalProperties is permitted.

## Evidence

Live probe: `claude-agent-sdk==0.1.63`, OAuth via local `claude` CLI
(macOS Keychain). Total cost $0.46, wall 42 s, 4 query() invocations
on `mcp__probe__echo_required` (schema `{query, area, limit}`) and
`mcp__probe__echo_extra` (schema `{name}`). Full transcripts:
`plan/phase4/spikes/rq7_flat_dict_optional.{py,json,txt}`.

### Form 1 — all args present (control)

```
tool_use:    {"query": "foo", "area": "inbox", "limit": 5}
tool_result: is_error=None
             ECHO_REQ: keys=['area', 'limit', 'query']
                       args={"query": "foo", "area": "inbox", "limit": 5}
```

Handler fires; all three keys reach the dict. Baseline behaves as
expected.

### Form 2 — only `query` supplied (the load-bearing case)

```
tool_use:    {"query": "bar"}
tool_result: is_error=True
             content="Input validation error: 'area' is a required property"
```

Handler **does not fire** — the MCP/SDK validation layer rejects the
call before invocation. The model receives an error result and would
typically retry with the missing field. Note: it's the **first**
missing key (`area`) that is named — validation appears to short-
circuit rather than aggregate all missing keys.

### Form 3 — extra `foo` key on `echo_required`

```
tool_use:    {"query": "baz", "area": "all", "limit": 3, "foo": "bar"}
tool_result: is_error=None
             ECHO_REQ: keys=['area', 'foo', 'limit', 'query']
                       args={"query": "baz", "area": "all",
                              "limit": 3, "foo": "bar"}
```

Extra key is forwarded; no error. Handler must defend itself if it
cares (e.g., reject unknowns, or just `.get()` declared keys).

### Form 4 — extra `surplus` key on `echo_extra` (smaller schema)

```
tool_use:    {"name": "alice", "surplus": "hello"}
tool_result: is_error=None
             ECHO_EXTRA: keys=['name', 'surplus']
                          args={"name": "alice", "surplus": "hello"}
```

Confirms Form 3: extra-key passthrough is the SDK-wide default for
`@tool` flat-dict, not a quirk of the larger schema.

### Init-meta tool schema

`SystemMessage(init).data` keys observed: `agents, apiKeySource,
claude_code_version, cwd, fast_mode_state, mcp_servers, memory_paths,
model, output_style, permissionMode, plugins, session_id, skills,
slash_commands, subtype, tools, type, uuid`.

The `tools` field is a flat list of strings (`mcp__probe__echo_required`
present); per-tool JSON Schema is **not** surfaced in init. The
optional follow-up probe ("does init expose `required: []`?") is
therefore answered: **no**, the init payload does not include the
input_schema details for any tool. The model resolves schemas via
`ToolSearch` at invocation time (a `ToolSearch` use was observed
before every probe call on this dev host).

## Implications for phase 4

**Plan needs a patch.** The phase-4 memory tools as currently sketched
(`memory_search(query, area, limit)`, etc.) cannot be called partially
— the model must supply *every* declared key on every call, even when
the intent is "search inbox with default limit".

There are two viable paths; both keep the Python ergonomics intact.

### Option A — declare only intentionally-required keys in flat-dict

Move optional fields out of the schema entirely. The handler accepts
`**kwargs`-shaped extras (Form 3/4 confirms unknown keys pass through),
but the model is no longer *prompted* to supply them — and missing
keys are not rejected because they aren't in the schema:

```python
@tool("memory_search",
      "Search memory; defaults: area=all, limit=10",
      {"query": str})  # only the truly required field
async def memory_search(args):
    query = args["query"]
    area  = args.get("area", "all")
    limit = int(args.get("limit", 10))
    ...
```

Trade-off: model loses schema-level discoverability of `area` /
`limit`. Mitigate by listing them in the description string. Phase-3
installer pattern (`{"name": str}` for `marketplace_info`) already
does this implicitly.

### Option B — switch to explicit JSON-Schema dict with `required: [...]`

The `input_schema` parameter accepts a dict in JSON-Schema shape too
(per SDK source, the flat-dict is sugar around this). Use it directly
when the schema must include optional fields:

```python
@tool("memory_search",
      "Search memory across one or more areas.",
      {
          "type": "object",
          "properties": {
              "query": {"type": "string",
                        "description": "FTS query, raw user words"},
              "area":  {"type": "string", "enum": ["inbox", "ideas", "all"],
                        "description": "Defaults to 'all'."},
              "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                        "description": "Defaults to 10."},
          },
          "required": ["query"],
      })
async def memory_search(args):
    query = args["query"]
    area  = args.get("area", "all")
    limit = int(args.get("limit", 10))
    ...
```

Trade-off: more verbose, but the model sees full type info, enums,
and bounds in the tool catalogue — better invocation correctness and
less reliance on description prose.

### Recommendation

**Option B for memory_search / memory_list / memory_delete.** The
optional fields (`area`, `limit`, filters, pagination cursors) are
load-bearing for usability — losing them from the schema (Option A)
means the model has to read description prose to know they exist,
which is unreliable. The verbosity cost (~10 LOC per tool) is paid
once.

For tools where every field is genuinely required (the phase-3
installer's `skill_install(url, confirmed)`, `skill_uninstall(name,
confirmed)`), **keep the flat-dict form** — strict required-fields
matches the contract and the brevity is a feature.

### Defensive pattern (regardless of choice)

Form 3 / Form 4 prove that **extra keys are forwarded**. If a tool
should reject typos or model-invented keys, validate inside the
handler:

```python
ALLOWED = {"query", "area", "limit"}
if extras := set(args) - ALLOWED:
    return tool_error(f"Unknown arguments: {sorted(extras)}", code=400)
```

Otherwise a model that hallucinates `memory_search(query="x",
arae="inbox")` (typo) will silently search with the default `area`
because the typo'd key passes through and is ignored — confusing
debug session.

## Reproducibility

```bash
cd /Users/agent2/Documents/0xone-assistant
./.venv/bin/python plan/phase4/spikes/rq7_flat_dict_optional.py
```

- Wall: ~45 s (4 query() calls; ToolSearch overhead ~1 s/call cold).
- Cost: ~$0.46 first run (cold cache), ~$0.30 subsequent (warm).
- Auth: requires owner's local `claude` CLI OAuth (script aborts
  cleanly if `~/.claude/.credentials.json` is absent, but on a host
  with Keychain-backed sessions the absent file is normal — the SDK
  picks up the session through the spawned `claude` subprocess).
- Outputs: `rq7_flat_dict_optional.json` (machine-readable),
  `rq7_flat_dict_optional.txt` (human transcript).
- The verdict is decided by Form 2's `is_error=True` +
  `partial_handler_fired=False`. Re-run is deterministic; the model
  obeyed the literal-args instruction in 4/4 forms across the run.
