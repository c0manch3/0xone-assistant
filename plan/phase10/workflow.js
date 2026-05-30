export const meta = {
  name: 'phase10-websearch',
  description: 'Add built-in WebSearch (+confirm WebFetch) to the 0xone-assistant bot via the standard phase pipeline: spec -> devil -> research -> code -> review -> synthesis',
  phases: [
    { title: 'Spec', detail: 'Draft phase10 plan: WebSearch gate, billing, SSRF, feature flag' },
    { title: 'Devil', detail: "Devil's advocate stress-tests the spec" },
    { title: 'Research', detail: 'Researcher validates concerns + SDK best practices' },
    { title: 'Code', detail: 'Coder implements config + bridge gate + tests' },
    { title: 'Review', detail: 'code-reviewer + qa-engineer + devops-expert in parallel' },
    { title: 'Synthesis', detail: 'Consolidate findings -> must-fix list + verdict' },
  ],
}

const REPO = '/Users/agent2/Documents/0xone-assistant'

const SPEC_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['summary', 'decisions', 'files_to_touch', 'acceptance_criteria', 'open_questions'],
  properties: {
    summary: { type: 'string' },
    decisions: { type: 'array', items: { type: 'string' } },
    files_to_touch: { type: 'array', items: { type: 'string' } },
    acceptance_criteria: { type: 'array', items: { type: 'string' } },
    open_questions: { type: 'array', items: { type: 'string' } },
  },
}

const DEVIL_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['concerns', 'must_address', 'verdict'],
  properties: {
    concerns: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'severity', 'argument'],
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          argument: { type: 'string' },
        },
      },
    },
    must_address: { type: 'array', items: { type: 'string' } },
    verdict: { type: 'string' },
  },
}

const RESEARCH_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings', 'recommended_approach', 'resolved_concerns', 'risks'],
  properties: {
    findings: { type: 'array', items: { type: 'string' } },
    recommended_approach: { type: 'string' },
    resolved_concerns: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
  },
}

const CODE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['summary', 'files_changed', 'tests_added', 'test_command', 'notes'],
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    tests_added: { type: 'array', items: { type: 'string' } },
    test_command: { type: 'string' },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

const REVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['findings', 'blocking', 'overall'],
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['title', 'severity', 'location', 'detail'],
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['blocker', 'major', 'minor', 'nit'] },
          location: { type: 'string' },
          detail: { type: 'string' },
        },
      },
    },
    blocking: { type: 'boolean' },
    overall: { type: 'string' },
  },
}

// ---------------------------------------------------------------------------
phase('Spec')
const spec = await agent(
  [
    'You are drafting the implementation spec for a new phase ("phase10") of the 0xone-assistant Telegram bot at ' + REPO + '.',
    '',
    'GOAL: Add the Claude Agent SDK built-in WebSearch tool to the bot, and CONFIRM/document the already-shipped WebFetch tool.',
    '',
    'GROUND TRUTH (already verified - do not re-litigate):',
    '- WebFetch is ALREADY fully implemented: it is in the hardcoded allowed_tools baseline at src/assistant/bridge/claude.py:358 and guarded by a two-layer SSRF PreToolUse hook (make_webfetch_hook in src/assistant/bridge/hooks.py, registered at claude.py:319). No new WebFetch work needed beyond documentation.',
    '- WebSearch is a SERVER-SIDE, BILLED tool ($10 / 1000 searches) - flat rate regardless of Pro/Max/OAuth subscription. It is currently NOT in allowed_tools anywhere.',
    '- Auth is OAuth only - NEVER introduce ANTHROPIC_API_KEY handling.',
    '- Config uses pydantic BaseSettings subclasses with env_prefix (see src/assistant/config.py: VaultSyncSettings, RenderDocSettings are the templates). Root Settings class at line 500.',
    '- Feature-gate pattern: settings class with enabled flag + env_prefix -> ClaudeBridge __init__ kwarg (the *_tool_visible bool, default False) -> conditional append in _build_options (claude.py:380-389) -> Daemon computes visibility in src/assistant/main.py and passes to bridge constructors (user bridge ~483, audio bridge ~515, picker bridge ~547).',
    '- Three bridges exist (user/audio/picker). Decide which should expose WebSearch.',
    '- Subagent "researcher" (src/assistant/subagent/definitions.py) already lists WebFetch; consider whether it should also get WebSearch.',
    '- Tests live in tests/, asserting on opts.allowed_tools / opts.mcp_servers (see test_phase8_disabled_invariants.py, test_bridge_subagent_options.py).',
    '',
    'Read the relevant files yourself to ground the spec. Then:',
    '1. Decide the design: feature-flag class (env_prefix WEBSEARCH_, default DISABLED given billing), bridge gating, which bridges/subagents get it, any allowed_domains/blocked_domains/max_uses controls the SDK WebSearch tool supports for cost & safety, system-prompt guidance about when to use web search vs memory.',
    '2. Write a concise plan to ' + REPO + '/plan/phase10/description.md and ' + REPO + '/plan/phase10/implementation.md (create the dir).',
    'Return the structured spec.',
  ].join('\n'),
  { schema: SPEC_SCHEMA, phase: 'Spec', label: 'spec:websearch', agentType: 'Plan' }
)
log('Spec drafted: ' + spec.files_to_touch.length + ' files, ' + spec.acceptance_criteria.length + ' ACs, ' + spec.open_questions.length + ' open questions')

// ---------------------------------------------------------------------------
phase('Devil')
const devil = await agent(
  [
    "You are the DEVIL'S ADVOCATE reviewing a spec to add the built-in WebSearch tool to the 0xone-assistant bot at " + REPO + '.',
    '',
    'The spec (JSON):',
    JSON.stringify(spec, null, 2),
    '',
    'Also read ' + REPO + '/plan/phase10/description.md and implementation.md.',
    '',
    'Argue the OPPOSING side hard. Surface hidden risks the spec glosses over. Specifically probe:',
    '- BILLING / cost runaway: $10/1000 searches, server-side, no subscription discount. What stops a prompt-injected web page or a runaway agent loop from burning money? Is there a per-turn / per-day cap? Who pays? Is default-disabled enough?',
    '- PROMPT INJECTION: WebSearch results + WebFetched pages are untrusted text that re-enters the model. A three-layer injection defense exists for the scheduler - does web content get equivalent treatment? Can a search result instruct the bot to exfiltrate vault contents or call vault_push?',
    '- SSRF / safety: WebSearch is server-side (Anthropic-run), so the SSRF hook does NOT apply to it the way it does to WebFetch. Does the spec wrongly assume the WebFetch hook covers WebSearch?',
    '- Which bridges: should the AUDIO bridge or PICKER bridge get web search, or only the user bridge? Subagents?',
    '- allowed_domains vs blocked_domains: does the spec actually configure SDK-level domain controls, or just flip the tool on?',
    '- OAuth invariant: any chance this nudges toward API-key handling?',
    '- Privacy: search queries leave the box to Anthropic + search providers; the owner private topics become search queries.',
    '- Test gaps and the deploy-after-every-phase rule.',
    '',
    'Return concerns ranked by severity and a must_address list.',
  ].join('\n'),
  { schema: DEVIL_SCHEMA, phase: 'Devil', label: 'devil:websearch', agentType: 'devils-advocate' }
)
const critical = devil.concerns.filter(c => c.severity === 'critical' || c.severity === 'high')
log('Devil raised ' + devil.concerns.length + ' concerns (' + critical.length + ' critical/high); ' + devil.must_address.length + ' must-address')

// ---------------------------------------------------------------------------
phase('Research')
const research = await agent(
  [
    'You are the RESEARCHER. Validate the WebSearch integration plan for the 0xone-assistant bot (Claude Agent SDK, Python) against authoritative sources and resolve the devil concerns.',
    '',
    'Spec: ' + JSON.stringify(spec, null, 2),
    'Devil must-address: ' + JSON.stringify(devil.must_address, null, 2),
    'Devil concerns: ' + JSON.stringify(devil.concerns, null, 2),
    '',
    'Research (use WebSearch/WebFetch + read the installed claude_agent_sdk in ' + REPO + '/.venv to ground claims in the ACTUAL installed API surface):',
    '1. The EXACT way to enable the built-in WebSearch tool in claude_agent_sdk / ClaudeAgentOptions. Is the tool name literally "WebSearch"? Does it need anything beyond appearing in allowed_tools? Inspect the installed SDK package source for WebSearch handling, ClaudeAgentOptions fields, and any web-search config (max_uses, allowed_domains, blocked_domains, user_location).',
    '2. Whether server-side WebSearch supports allowed_domains / blocked_domains / max_uses cost controls and how to pass them through the SDK.',
    '3. Billing controls / best practices to bound cost (max_uses per request, default-off gate).',
    '4. Prompt-injection hardening best practices for feeding web/search results back into an agent (delimiting untrusted content, instructing the model not to follow embedded instructions).',
    '5. Confirm or correct the devil claim that the WebFetch SSRF hook does NOT cover server-side WebSearch.',
    '',
    'Ground every claim in either the installed SDK source (cite file path) or a fetched doc (cite URL). Return findings, a recommended_approach the coder can follow verbatim, which concerns are resolved, and residual risks.',
  ].join('\n'),
  { schema: RESEARCH_SCHEMA, phase: 'Research', label: 'research:websearch', agentType: 'researcher' }
)
log('Research done: ' + research.findings.length + ' findings, ' + research.resolved_concerns.length + ' concerns resolved, ' + research.risks.length + ' residual risks')

// ---------------------------------------------------------------------------
phase('Code')
const code = await agent(
  [
    'You are the CODER. Implement phase10 (WebSearch tool) in the 0xone-assistant repo at ' + REPO + '. Match the surrounding code style exactly.',
    '',
    'Spec: ' + JSON.stringify(spec, null, 2),
    'Researcher recommended approach (FOLLOW THIS - it is grounded in the installed SDK): ' + JSON.stringify(research, null, 2),
    'Devil must-address (each must be handled or consciously deferred with a code comment): ' + JSON.stringify(devil.must_address, null, 2),
    '',
    'Hard constraints:',
    '- OAuth only - NEVER add ANTHROPIC_API_KEY.',
    '- WebSearch default DISABLED (billing). Gate it exactly like the vault/render_doc feature flags: a pydantic Settings subclass (env_prefix WEBSEARCH_) + ClaudeBridge websearch_tool_visible kwarg + conditional append in _build_options + Daemon wiring in main.py.',
    '- Apply whatever cost controls (max_uses) and domain controls the SDK actually supports, per the research.',
    '- Add prompt-injection guidance to the system prompt if research recommends it.',
    '- Decide bridges/subagents exposure per the spec.',
    '- Update .env.example with the new WEBSEARCH_* vars (default off) and update CLAUDE.md / plan/phase10 docs if needed.',
    '',
    'Write FULL working code and tests. Tests must assert WebSearch presence/absence in opts.allowed_tools gated on the flag (mirror tests/test_phase8_disabled_invariants.py) and any config-default tests. Run the new tests yourself with the repo runner (e.g. ' + REPO + '/.venv/bin/python -m pytest <new test files> -q) and iterate until green. Do NOT commit or push.',
    '',
    'Return a precise list of files changed, tests added, the exact test command, and notes on anything deferred.',
  ].join('\n'),
  { schema: CODE_SCHEMA, phase: 'Code', label: 'code:websearch', agentType: 'coder' }
)
log('Code: ' + code.files_changed.length + ' files changed, ' + code.tests_added.length + ' test files. Test cmd: ' + code.test_command)

// ---------------------------------------------------------------------------
phase('Review')
const reviewContext = [
  'Repo: ' + REPO,
  'Spec: ' + JSON.stringify(spec),
  'Coder summary: ' + JSON.stringify(code),
  'Devil must-address: ' + JSON.stringify(devil.must_address),
  'Inspect the actual working-tree changes (git diff) and the new/modified files.',
].join('\n')

const reviews = await parallel([
  () => agent(
    [
      'You are the CODE REVIEWER. Review the WebSearch implementation just made.',
      reviewContext,
      'Focus: correctness of the feature-gate wiring (settings -> bridge kwarg -> _build_options append -> Daemon), no regressions to existing allowed_tools/mcp_servers, code quality, that WebFetch is untouched, OAuth invariant preserved (no API key), test quality. Verify the tests actually exercise the gate (both enabled and disabled). Run the tests if useful.',
    ].join('\n'),
    { schema: REVIEW_SCHEMA, phase: 'Review', label: 'review:code', agentType: 'code-reviewer' }
  ),
  () => agent(
    [
      'You are the QA ENGINEER. Verify the WebSearch implementation against the spec acceptance criteria and hunt for bugs / runtime errors / security holes.',
      reviewContext,
      'Acceptance criteria to verify: ' + JSON.stringify(spec.acceptance_criteria),
      'Run the relevant test subset (' + REPO + '/.venv/bin/python -m pytest -q for bridge/config tests). Probe: prompt-injection handling of search/web content, billing cost-cap correctness (max_uses), default-disabled behavior, that enabling the flag actually surfaces the tool, edge cases in config parsing. Report whether each AC is met.',
    ].join('\n'),
    { schema: REVIEW_SCHEMA, phase: 'Review', label: 'review:qa', agentType: 'qa-engineer' }
  ),
  () => agent(
    [
      'You are the DEVOPS EXPERT. Review the WebSearch change for deployment & operational safety on the VPS (Docker compose stack, image to GHCR, deploy-after-every-phase rule).',
      reviewContext,
      'Focus: new env vars documented in .env.example AND deploy config (does the Docker/.env path need WEBSEARCH_* added?), default-off so a deploy does not silently start billing, cost observability/runaway protection in production, no secret/credential leakage, rollback safety, whether any build-time/system dep is introduced. Report blocking ops issues.',
    ].join('\n'),
    { schema: REVIEW_SCHEMA, phase: 'Review', label: 'review:devops', agentType: 'devops-expert' }
  ),
])

const labels = ['code-review', 'qa', 'devops']
const allFindings = reviews
  .map((r, i) => ((r && r.findings) || []).map(f => ({ ...f, reviewer: labels[i] })))
  .flat()
const blockers = allFindings.filter(f => f.severity === 'blocker')
const anyBlocking = reviews.some(r => r && r.blocking)
log('Reviews: ' + allFindings.length + ' findings total, ' + blockers.length + ' blockers. Blocking=' + anyBlocking)

// ---------------------------------------------------------------------------
phase('Synthesis')
const synthesis = await agent(
  [
    'You are the SYNTHESIS lead. Consolidate the phase10 WebSearch review into a single owner-facing verdict.',
    '',
    'Spec: ' + JSON.stringify(spec),
    'Devil concerns: ' + JSON.stringify(devil.concerns),
    'Research: ' + JSON.stringify(research),
    'Coder result: ' + JSON.stringify(code),
    'All review findings (tagged by reviewer): ' + JSON.stringify(allFindings),
    'Any reviewer flagged blocking: ' + anyBlocking,
    '',
    'Produce a tight markdown report for the repo owner with these sections:',
    '## Verdict  (SHIP / SHIP-WITH-FIXES / DO-NOT-SHIP, one line why)',
    '## What changed  (files, the feature flag, default state, cost controls)',
    '## WebFetch status  (already shipped - one line)',
    '## Must-fix before commit  (numbered; empty if none)',
    '## Devil concerns - resolved vs residual',
    '## Acceptance criteria status',
    '## Deploy notes  (env vars, default-off, VPS/Docker steps, owner smoke test)',
    '## Recommended next action for the orchestrator',
    '',
    'Be concrete and honest. If reviewers found a real blocker that the coder did not fix, say DO-NOT-SHIP and list the exact fix.',
  ].join('\n'),
  { phase: 'Synthesis', label: 'synthesis', agentType: 'claude' }
)

return {
  report: synthesis,
  blockers: blockers.length,
  anyBlocking,
  files_changed: code.files_changed,
  tests_added: code.tests_added,
  test_command: code.test_command,
}
