"""Per-kind AgentDefinition registry (phase 6).

S-6-0 findings that shape this module:

* **Q9 — PASS.** `AgentDefinition.prompt` is a FULL system prompt
  (not appended). Each kind below includes its own voice + constraints.
* **Q4 — empirically gated by `tools`.** Recursion was not observed when
  the child's `tools` omitted `"Task"`. We OMIT `"Task"` from every
  kind's `tools` → hard depth cap at 1 without any runtime guard. Lock
  this with `test_subagent_no_recursion_lock`.
* **Q10 — PASS.** `model="inherit"` is runtime-valid; child uses the
  parent's model.
* **Q1 (wave-2 re-run) — `background=True` has no observable effect on
  SDK 0.1.59.** We keep it set for forward compat (the SDK may wire it
  up later) but the design does NOT rely on the main turn returning
  before the child.

Prompt templates interpolate `project_root` / `vault_dir` so the
subagent sees the same anchors the main turn does.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from assistant.config import Settings

_GENERAL_PROMPT = """\
You are a background subagent spawned by 0xone-assistant.
Your task is provided in the initial user message.
You do NOT have direct access to the owner. Your final assistant text
is delivered to them via Telegram verbatim.

Rules:
- Complete proactively. Do not ask clarifying questions.
- Reply with the FINAL result as your last assistant message.
- Be concise unless the task explicitly asks for long form.
- Use only the tools in your allowed list.

Environment:
- Project root: {project_root}
- Vault: {vault_dir}
"""

_WORKER_PROMPT = """\
You are a worker subagent spawned by 0xone-assistant.
Execute a single CLI invocation or a tightly scoped tool sequence.
Report the tool's result as your final assistant message and stop.
Do not explore beyond the task.

Environment:
- Project root: {project_root}
- Vault: {vault_dir}
"""

_RESEARCHER_PROMPT = """\
You are a research subagent spawned by 0xone-assistant.
Use Read/Grep/Glob/WebFetch to gather information. Produce a concise
structured summary as your final assistant message. Do NOT modify files.
Your summary is delivered to the owner verbatim.

Environment:
- Project root: {project_root}
- Vault: {vault_dir}
"""


def build_agents(settings: Settings) -> dict[str, AgentDefinition]:
    """Return the per-kind AgentDefinition registry.

    Pitfall #2: none of the definitions include `"Task"` in `tools` —
    that enforces the depth cap empirically (Q4). A regression test
    (`test_subagent_no_recursion_lock`) asserts this.

    Pitfall #5 / B-W2-1: `background=True` stays on every definition
    for forward-compat, even though Q1-BG showed it has no runtime
    effect on 0.1.59 + CLI 2.1.114. When the SDK wires it up the
    config is ready; until then the Daemon design treats main-turn
    wall-clock as approximately equal to subagent wall-clock.
    """
    pr = settings.project_root
    vault = settings.vault_dir
    sub = settings.subagent

    base_fmt: dict[str, str] = {
        "project_root": str(pr),
        "vault_dir": str(vault),
    }

    return {
        "general": AgentDefinition(
            description="Generic background task: long writing, multi-step reasoning",
            prompt=_GENERAL_PROMPT.format(**base_fmt),
            tools=["Bash", "Read", "Write", "Edit", "Grep", "Glob", "WebFetch"],
            model="inherit",
            maxTurns=sub.max_turns_general,
            background=True,
            permissionMode="default",
        ),
        "worker": AgentDefinition(
            description="Run a single CLI tool and report its output",
            prompt=_WORKER_PROMPT.format(**base_fmt),
            tools=["Bash", "Read"],
            model="inherit",
            maxTurns=sub.max_turns_worker,
            background=True,
            permissionMode="default",
        ),
        "researcher": AgentDefinition(
            description="Read-only research and summarisation",
            prompt=_RESEARCHER_PROMPT.format(**base_fmt),
            tools=["Read", "Grep", "Glob", "WebFetch"],
            model="inherit",
            maxTurns=sub.max_turns_researcher,
            background=True,
            permissionMode="default",
        ),
    }
