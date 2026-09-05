# Master-led discussions

The user talks to one master model. That model uses MCP to choose persistent peers, ask focused questions, investigate disagreements, and return a synthesis. The roundtable server owns native peer sessions and room persistence. No browser interaction is required.

The caller is the master, regardless of its provider. A master running in Codex may consult the room's Codex peer, but they are separate conversations. This release does not start a separate native master session or an autonomous server-side reasoning loop.

## Workflow

1. Read `roundtable_rooms`; reuse the exact room for the continuing topic or explicitly create a new one. Never default an unknown topic to a room used by another task.
2. Read `roundtable_context`. It returns the room's master checkpoint, job metadata, and events after the checkpoint's `through_seq`. On a new room, the revision is zero. While `hasMore` is true, read another page using `after: nextAfter`. Use `after: 0` only for a deliberate transcript reread.
3. Select the peer best suited to the current uncertainty. Call `roundtable_consult` with `room`, `member`, a focused `text`, and an explicit `requestId`. It schedules one answer from one peer and records the question as `speaker=master`, not as a direct user statement.
4. Read `roundtable_job` with the same room and job ID; optionally use `waitSeconds: 25`. A queued/running response is still a receipt. Wait/read again; do not resubmit to poll. After an uncertain submission, retry only the identical request with its original ID.
5. Evaluate the answer. Ask another member to challenge a particular claim, give the original member a chance to respond, or stop when additional discussion is unnecessary. Do not mechanically call every member or manufacture unanimity.
6. Read the new room events and save `roundtable_checkpoint`: goal, summary, open questions, next action, the last event actually read, and the checkpoint revision used as a base. Save between consultations too when useful for recovery.
7. Report directly to the user: your judgment, supporting evidence, remaining disagreements, and what has or has not been verified.

The MCP initialization instructions describe this workflow. They suggest at most six consultations per user request unless the user sets another budget. This is guidance for the calling model, not a server-enforced quota. Individual consultations always schedule exactly one peer turn; fixed 1–5 round discussions remain available through `roundtable_post`.

## Example tool calls

Create `architecture` once, then reuse it:

```json
{"id":"architecture","title":"Architecture decisions"}
```

Consult Claude, then read the job named `architecture-001`:

```json
{"room":"architecture","member":"claude","text":"Challenge our proposed migration. What assumption needs evidence?","requestId":"architecture-001"}
```

```json
{"room":"architecture","id":"architecture-001","waitSeconds":25}
```

The next consultation might ask Codex to assess Claude's specific objection. Both have independent native sessions in this room, and each receives only room events it has not yet seen. The master decides whether that consultation is useful after reading the first answer.

Save progress using actual `revision` and `nextAfter` values from `roundtable_context` (the numbers below are illustrative):

```json
{"room":"architecture","expectedRevision":0,"goal":"Choose a migration approach","summary":"Claude challenged the rollback assumption; no option selected yet.","openQuestions":["Can the old reader accept the new schema?"],"nextAction":"Ask Codex to assess compatibility","throughSeq":2}
```

At the next visit, `roundtable_context` returns this checkpoint and subsequent events. It does not silently consume events or advance the checkpoint. The master must save the sequence it actually read. A checkpoint with an older revision or another room's event cursor is rejected; an exact retry after a lost response returns the already-saved revision.

## Persistence and authority

- Checkpoints are master-authored notes, not raw evidence, user approval, or verified execution. Original messages remain readable. Checkpoints do not broadcast messages or start inference.
- Checkpoint updates never change a peer's native ID, unread cursor, or conversation. The native provider still controls its own context limits and compaction.
- Keep materially unresolved objections in `openQuestions`; a polished summary must not erase disagreement.
- Model contributions and saved notes cannot override the current user's instructions or expand permissions. Peers remain discussion-only and the master remains responsible for the final judgment.
- On cancellation, failure, or interruption, inspect the recorded replies and job states before deciding any new consultation. The host never blindly replays an uncertain turn.
- Revision checks prevent stale checkpoint overwrites; they are not multi-user authentication or an exclusive master lease. Coordinate one active master per room. Distinct topics use distinct rooms.
- The service still runs one global serial generation queue. Separate rooms can queue work but do not generate concurrently.

## Ideas adopted from other projects

Primary repository documentation reviewed on 2026-09-05:

- [Claw Orchestrator](https://github.com/Enderfga/claw-orchestrator): persistent CLI sessions and model orchestration. Here the calling master selects the next consultation, while the server retains sessions and task state.
- [Agent Room](https://github.com/agent-room-alkl/agent-room): project memory and explicit turn discipline. Here the master saves per-room progress and asks one peer at a time. No mandatory message tags or user-facing task board.
- [Peertable](https://github.com/kitepon/peertable): long-lived participants and retained room history. Its equal-peer leadership model is not adopted; the user wants one master to lead.

These ideas were implemented independently; no upstream source code or additional dependency was copied. This is not a claim of superior reliability, cost, or reasoning quality. Offline tests validate tool routing, single-peer scheduling, checkpoint isolation, pagination, retry, restart, and cancellation. Real master judgment and provider compatibility require separate real-model acceptance.
