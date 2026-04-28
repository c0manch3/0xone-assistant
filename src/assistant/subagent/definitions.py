"""Per-kind ``AgentDefinition`` registry for phase-6 subagents.

Three named agents — ``general`` / ``worker`` / ``researcher`` —
mirror description.md scenarios. None of them include ``"Task"`` in
``tools``: that omission is the depth-cap mechanism (S-6-0 Q4 +
research RQ-RESPIKE Q4). Future phases may grant ``"Task"`` to
``general`` deliberately to enable depth=2 delegation; today the
registry is the single point of policy.

Notes:
  * ``model="inherit"`` is verified runtime-valid on SDK 0.1.59-0.1.63
    (S-6-0 Q10).
  * ``background=True`` has NO observable effect on 0.1.59-0.1.63
    (S-6-0 Q1 + wave-2 Q1-BG re-run); we keep it set for forward-compat
    with future SDK versions that may honour the flag.
  * ``permissionMode="default"`` lets the parent's PreToolUse sandbox
    own the subagent's tool calls (S-2 wave-2 verified subagent Bash
    traverses parent's ``make_pretool_hooks``).
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from assistant.config import Settings

_GENERAL_PROMPT = """\
You are a background subagent spawned by 0xone-assistant.
Your task is provided in the initial user message.
You do NOT have direct access to the owner; your final assistant text
is delivered to them via Telegram automatically by the harness.

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
You are a worker subagent. Execute a single CLI invocation or a
tightly scoped tool sequence and report the result. Do not explore
beyond the task's boundary. Reply with a concise final message.
"""

_RESEARCHER_PROMPT = """\
You are a research subagent. Use Read/Grep/Glob/WebFetch to gather
information and produce a structured summary. Do not modify files.
Your summary is delivered to the owner verbatim — keep it scannable.
"""


def build_agents(settings: Settings) -> dict[str, AgentDefinition]:
    """Return the per-kind ``AgentDefinition`` registry.

    Pitfall #2 / S-6-0 Q4: none of the three definitions include
    ``"Task"`` in ``tools``. Phase-6 depth cap is enforced
    structurally by tool narrowing — there is no runtime guard hook.
    The ``test_subagent_no_recursion_lock`` regression test asserts
    the omission to prevent silent regressions.
    """
    sub = settings.subagent
    fmt = {
        "project_root": str(settings.project_root),
        "vault_dir": str(settings.vault_dir),
    }
    return {
        "general": AgentDefinition(
            description=(
                "Generic background task: long writing, multi-step "
                "reasoning, mixed tool usage."
            ),
            prompt=_GENERAL_PROMPT.format(**fmt),
            tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Grep",
                "Glob",
                "WebFetch",
            ],
            model="inherit",
            maxTurns=sub.max_turns_general,
            background=True,
            permissionMode="default",
        ),
        "worker": AgentDefinition(
            description=(
                "Run a single CLI invocation or tightly scoped tool "
                "sequence and report the result."
            ),
            prompt=_WORKER_PROMPT,
            tools=["Bash", "Read"],
            model="inherit",
            maxTurns=sub.max_turns_worker,
            background=True,
            permissionMode="default",
        ),
        "researcher": AgentDefinition(
            description=(
                "Read-only research and summarisation; "
                "no file mutation."
            ),
            prompt=_RESEARCHER_PROMPT,
            tools=["Read", "Grep", "Glob", "WebFetch"],
            model="inherit",
            maxTurns=sub.max_turns_researcher,
            background=True,
            permissionMode="default",
        ),
    }


# Allowed kinds — used by the @tool surface to whitelist values.
SUBAGENT_KINDS: frozenset[str] = frozenset(
    {"general", "worker", "researcher"}
)
