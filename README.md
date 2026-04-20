# 0xone-assistant

Personal Telegram bot on Claude Agent SDK.

**Status:** rebuild in progress — previous 8-phase batch deploy resulted in cascading production bugs. New methodology: deploy after each phase.

All research preserved in `plan/` — phase descriptions, detailed plans, spike findings, implementation prescriptions, devil's advocate analysis, wave plans, summaries.

Approach per phase:
1. description → Q&A → devil wave 1 → researcher spike → devil wave 2 → researcher fix-pack → parallel-split → multi-wave coder → parallel reviewers → fix-pack → **deploy + owner smoke test** → summary + next phase plan.

Implementation code wiped at commit `<this commit>`; rebuilding from phase 1 (skeleton + Telegram echo) per `plan/README.md`.
