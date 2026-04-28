"""Phase 6c: in-process helpers for the memory subsystem.

Distinct from :mod:`assistant.tools_sdk.memory` (the @tool MCP server) —
this package exposes direct-callable wrappers for handler-side code paths
that need to write the vault deterministically (e.g. voice transcripts)
without the latency/cost/non-determinism of routing through Claude.
"""
