# Design references and project boundaries

A2A Workbench was informed by several open-source projects and the A2A specification. The implementation in this repository was written independently; no upstream source code was copied unless a future contribution explicitly records otherwise.

## A2A Protocol

Repository: <https://github.com/a2aproject/A2A>

License: Apache-2.0

Adopted:

- Agent Card discovery.
- Explicit task lifecycle and terminal states.
- Messages, parts, artifacts, context IDs, referenced task IDs, and cancellation.
- JSON-RPC and HTTP+JSON A2A 1.0 interfaces.

A2A supplies interoperability, not the room, checkpoint, or native-session product model implemented here.

## Claw Orchestrator

Repository: <https://github.com/Enderfga/claw-orchestrator>

License: MIT

Ideas considered:

- Coding CLIs as persistent programmable sessions.
- A common orchestration surface over multiple agent engines.
- Durable lifecycle and explicit task state rather than relying on terminal text.

Workbench choice:

- The calling model remains the Master; the room service does not start an additional autonomous planning agent.
- The current public release focuses on durable peer consultation rather than exposing a broad orchestration tool surface.

## Agent Room

Repository: <https://github.com/agent-room-alkl/agent-room>

License: MIT

Ideas considered:

- A shared, observable collaboration room.
- Explicit turn discipline and project context.
- Structured outcomes and durable reports.

Workbench choice:

- Native provider conversations carry member continuity; new prompts contain only unread room events.
- Structured message tags are not required for normal operation.
- Master checkpoints are separate from raw room evidence and cannot authorize execution.

## Peertable

Repository: <https://github.com/kitepon/peertable>

License: MIT

Ideas considered:

- Long-lived peers.
- Retained room history and explicit recipient identity.
- Collaboration state that survives individual model turns.

Workbench choice:

- Peers are not equal decision authorities for a user request. One Master owns synthesis and reports to the user.
- Peer messages are discussion evidence, not permission to modify files, run commands, deploy, or start more work.

## Broader product research

The following projects were also reviewed while defining the next delivery-oriented layer. They are related projects, not dependencies of the current public release.

| Project | What it demonstrates | Current Workbench boundary |
|---|---|---|
| [Agent Orchestrator](https://github.com/Untrivial-ai/agent-orchestrator) | A mature desktop control plane for agent workspaces, pull requests, CI, and reviews | Product/UX benchmark; Workbench does not reproduce its desktop IDE |
| [dmux](https://github.com/standardagents/dmux) | Practical parallel coding-agent sessions using tmux and isolated Git worktrees | Informed the worktree lifecycle direction; not included in v0.3 |
| [Concord MCP](https://github.com/Get-Concord-AI/concord-mcp) | Cross-harness identity, task claims, acknowledged handoffs, and optimistic state transitions | Informed future delivery-state design; no code dependency |
| [Foremerge](https://github.com/naw103/foremerge) | Intent and semantic conflict detection above Git | Considered for future preflight checks; not implemented in v0.3 |
| [agent-semaphore](https://github.com/alwh1te/agent-semaphore) | Scope leases, merge-tree conflict prediction, and a test-gated landing queue | Informed future integration safety; not implemented in v0.3 |
| [git-stint](https://github.com/rchaz/git-stint) | Session-scoped branches, worktrees, checkpoints, and cleanup | Informed workspace lifecycle research; not included in v0.3 |

These comparisons narrowed the product boundary: A2A Workbench should not become another terminal multiplexer or desktop IDE. Its distinct responsibility is durable, open collaboration context between independent agents, with execution and verified delivery added only through small reusable interfaces.

## What A2A Workbench adds

The combination implemented here is:

- one explicit room per continuing topic;
- one native conversation per `(room, member)`;
- incremental unread-event delivery;
- a Master checkpoint that does not replace peer context;
- conservative restart behavior for uncertain model turns;
- the same room boundary enforced across HTTP, MCP, UI, and A2A task operations.

The next product layer—isolated coding workspaces and machine-verified delivery—is developed separately until it can be transferred into the public repository without machine-specific configuration or overstating current capabilities.

## Attribution discipline

When adopting code in the future:

1. Check the upstream license and compatibility.
2. Record the upstream repository, commit, affected files, and license notice.
3. Keep copied or modified code attribution explicit.
4. Do not describe independently reimplemented ideas as copied code.
5. Run the publication guard before every public commit.
