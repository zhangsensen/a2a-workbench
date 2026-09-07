# Contributing

Keep changes focused and include the behavior that motivated them. Run:

```bash
uv sync --locked --group dev
uv run pytest -q
node --check roundtable_ui.js
python3 scripts/check_publication.py --worktree
```

Offline tests must not require a model subscription or user credentials. Use fake members to verify routing, failure handling, cancellation, restoration, and room boundaries. Real-provider acceptance scripts are manual, consume usage, and write only ignored local reports. Use synthetic facts, never private conversations.

Room isolation is a core invariant. Every externally initiated operation must name the intended room, and job operations must verify room ownership. A provider conversation must not be reused by a different room or member. Native session recovery must preserve attempted conversations rather than silently creating a new one.

Do not add automatic client-configuration migration, background infinite model loops, public network listeners, or unconditional replay of interrupted turns. Do not commit local state. Before a commit, stage exact files and run `python3 scripts/check_publication.py` to inspect the Git index.

Protocol adapter changes should state which installed client version was tested. A successful fake-model test does not prove that a real client version accepts the same messages. Keep the dependency lock file current when changing dependencies.

When a change is based on another project, document the upstream repository, commit, license, and whether code was copied or the idea was independently reimplemented. Keep [REFERENCES.md](REFERENCES.md) current.

Contributions to this project are made under its MIT license.
