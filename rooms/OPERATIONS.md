# Operating the roundtable

## Start and stop

Run `uv run python roundtable.py` in the foreground on a supported Unix host. On macOS, `uv run python service.py install` installs the project's own LaunchAgent after the foreground process has stopped.

```bash
uv run python service.py status
uv run python service.py restart
uv run python service.py stop
uv run python service.py start
```

`restart` sends SIGTERM and lets LaunchAgent KeepAlive bring the service back. `stop` unloads the service without deleting data. Installation never modifies other LaunchAgents or client configurations. To reinstall with a different path or environment, stop this service first, then run install from its new location. Use the same `A2A_PORT` for service installation, status, CLI calls, and MCP.

A data-directory lock prevents two hosts from owning the same room state. Inspect `/healthz` before restarting: wait for `currentJob=null` and `queueSize=0` unless you intend to interrupt a discussion.

## Native context and interruption

Each `(room, member)` owns a runtime, a saved native ID, and an unread event cursor. New prompts contain only the room's unread events. Native client history retains earlier context; the host does not rebuild the entire conversation each turn.

On restart, queued jobs are retained. Running jobs become `interrupted` because an unrecorded provider turn may already have been processed or billed. Inspect completed replies before resubmitting. The host does not silently repeat uncertain work.

A newly created Codex thread with no input attempts may not yet exist on disk; only that empty case may be recreated. Attempted native conversations must retain their identity. A restore failure is surfaced as an error rather than replacing the conversation.

## Room boundaries

The `lobby` room is precreated, but requests and new browser tabs do not implicitly select it. Reuse the same room ID for one continuing topic. Create a different room explicitly for an independent topic.

- Every HTTP message/history route names the room in its path.
- HTTP job lookup and cancellation require `?room=...`, and reject another room's job ID.
- MCP `post`, `history`, `job`, and `cancel` require `room`.
- Master `consult`, `context`, and `checkpoint` also require `room`. Context cursors must belong to that room; checkpoints require the revision read by the master.
- A2A SendMessage requires an existing `message.contextId`; missing or unknown contexts fail before model calls. GetTask/CancelTask/SubscribeToTask require `X-A2A-Room`. ListTasks requires `contextId`.
- A2A task IDs and referenced task IDs must belong to the selected room. Raw A2A v1.0 requests need `A2A-Version: 1.0`.
- The database requires unique native session IDs. The host validates room and participant ownership before provider input and before recording replies.
- UI selection and drafts are scoped to a browser tab and room. Delayed responses from another room cannot update the selected room.

The application allows up to eight warm rooms and serializes discussion jobs. This bounds simultaneous model work but does not guarantee fairness between an unlimited number of queued requests. There is no room deletion API in this version.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `/` | Browser UI |
| `/healthz` | Service and native-runtime status; liveness is separate from readiness |
| `/api/rooms` | List/create rooms |
| `/api/rooms/{room}` | Room member status |
| `/api/rooms/{room}/messages` | Read/post room events |
| `/api/rooms/{room}/jobs` | Recent job metadata for that room |
| `/api/rooms/{room}/consult` | POST: one peer consultation from the master; explicit requestId required |
| `/api/rooms/{room}/context` | GET: master checkpoint plus subsequent events (`after`, `limit` for paging) |
| `/api/rooms/{room}/checkpoint` | POST: save master progress with expectedRevision and throughSeq |
| `/api/jobs/{id}?room={room}` | Read the scoped job and recorded replies |
| `/api/jobs/{id}/cancel?room={room}` | Cancel a scoped job |
| `/api/rooms/{room}/warm` | Explicitly try restoring the room's participants |
| `/.well-known/agent-card.json` | Agent discovery |
| `/a2a/jsonrpc`, `/a2a/rest` | A2A v1.0 transports |

## Troubleshooting

For adaptive model-led discussion, follow [MASTER.md](MASTER.md). Job reads accept `waitSeconds=0..25`; a wait timeout returns the current receipt rather than cancelling, retrying, or declaring the model failed. Master checkpoints are stored in the room database, separately from native peer sessions. After updating the server and MCP script, reconnect the MCP client to load the new tools and initialization instructions.

- **Port occupied:** do not kill an unrelated process. Stop your other copy or set `A2A_PORT` consistently.
- **Participant unavailable:** confirm the official client is installed and signed in. Check executable overrides and configured models. Retry the room's warm endpoint after fixing the client. A live subprocess alone is not proof of a successful model turn.
- **MCP tool arguments look old:** reconnect the MCP client after updating the script; model sessions are hosted separately.
- **Files open for a long time:** RoomStore explicitly closes each SQLite connection. The regression test keeps references to connections to ensure garbage collection cannot hide a missing close.
- **Partial discussion:** inspect the failed member; answers from other participants remain available.
- **Sleeping or offline host:** no inference takes place while the computer is asleep. Resume using the saved room after connectivity returns.

## Manual acceptance

`uv run python native_acceptance.py` checks same-process continuity and restoration without injecting prior history. Use `--member claude` to limit it to one provider.

`uv run python native_room_acceptance.py` creates two temporary rooms and six native sessions with different synthetic facts. It checks live recall and restored recall. Both scripts consume real model usage and save ignored reports under `data/acceptance`.

`uv run python tests/ui_fixture.py` serves an isolated fake-model browser fixture at `http://127.0.0.1:41242/`. Room A responses are delayed by three seconds. Verify separate A/B drafts, submit A then immediately switch to B, and reload B to confirm its draft remains. Stop the fixture afterward. It does not alter the main service's rooms.
