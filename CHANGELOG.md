# Changelog

A2A Workbench follows semantic versioning for public releases. The project was originally published as **A2A Roundtable**; the GitHub repository was renamed to **A2A Workbench** on 2026-09-07.

## Versioning policy

- Public releases use semantic versions and Git tags (`vMAJOR.MINOR.PATCH`).
- Until the first tag is published, `main` plus the package version is authoritative.
- Patch releases fix compatibility or correctness without changing the collaboration model.
- Minor releases add backward-compatible room, protocol, adapter, or delivery capabilities.
- Major releases may remove compatibility identifiers or change persisted/public contracts and must include a migration guide.

## Unreleased

- Renamed the product and repository to **A2A Workbench**.
- Reframed the documentation around open collaboration between independent coding agents.
- Documented the product problem, design references, current delivery boundary, and next verified-delivery layer.
- Retained the `a2a-roundtable` Python package, MCP key, and macOS LaunchAgent label for v0.3.x compatibility.

## 0.3.0 — 2026-09-05

- Added master-led adaptive consultations: the calling model selects one persistent peer per question and decides whether to continue.
- Added room-scoped master checkpoints containing goal, summary, unresolved questions, next action, and the last consumed event sequence.
- Added revision checks, exact retry handling, and pagination for room recovery.
- Distinguished Master consultations from direct user messages.
- Preserved peer native sessions while saving or updating Master checkpoints.

## 0.2.0 — 2026-09-05

- Published the persistent multi-room A2A host.
- Added isolated `(room, member)` native sessions for Codex, Claude Code, and ZCode.
- Added unread event cursors and incremental context delivery.
- Added browser UI, HTTP API, stdio MCP client, and A2A 1.0 JSON-RPC / HTTP+JSON interfaces.
- Added scoped task lookup and cancellation, restart interruption handling, request idempotency, and room-boundary tests.
- Added macOS LaunchAgent lifecycle commands and real-provider acceptance probes.

## 0.1.0 — 2026-09-05

- Initial local A2A discussion-room prototype.
