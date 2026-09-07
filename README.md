# PatchCrew

**Independent agents. One durable crew.**

PatchCrew is a local-first collaboration and delivery system for independent coding agents. This repository is the single source for the public persistent-room product and the next delivery layer under development.

- [`rooms/`](rooms/) — the released v0.3 persistent collaboration host over MCP, HTTP, and A2A 1.0, plus post-v0.3 integration work.
- [`grid/`](grid/) — coding-agent adapters, isolated worktree dispatch, delivery evidence, and machine verification under development for the next release.
- [`deploy-to-wsl.sh`](deploy-to-wsl.sh) — deploys this repository to the local five-service WSL runtime.
- [`FUSION_DESIGN.md`](FUSION_DESIGN.md) — integration design and capability boundaries.

## Release status

The latest public release is [v0.3.0](https://github.com/zhangsensen/patchcrew/releases/tag/v0.3.0). Its released scope is persistent collaboration rooms; isolated worktree execution and machine-verified delivery remain unreleased until the integrated tree passes publication review.

For installation and public API documentation, start with [`rooms/README.md`](rooms/README.md).

## Local validation

Run the two suites separately to avoid duplicate test-module names:

```bash
python -m pytest -q grid
cd rooms && python -m pytest -q
```

Do not publish local state, model credentials, deployment data, or reference checkouts.
