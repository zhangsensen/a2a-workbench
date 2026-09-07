# Security and data boundaries

A2A Workbench is a single-user local development tool. It binds to `127.0.0.1`, rejects unexpected Host headers and cross-origin requests, and has no multi-user authentication. **Do not expose it through a public proxy, shared network tunnel, or untrusted local application.** The room IDs are routing boundaries, not secrets or authorization credentials.

## Local data and provider traffic

Room transcripts, job prompts, native session identifiers, and unread cursors are stored in `data/roundtable/rooms.sqlite3`. Standard A2A task records are stored alongside it. Provider clients also keep their own native session records outside this project. Back up and protect these files as conversation data.

When you initiate a discussion, its supplied messages are sent through the selected official model clients. Members in that room may receive earlier members' replies. No other room's history is intentionally added. Do not post sensitive content unless you intend the selected providers to process it.

The ZCode adapter reads the selected official provider from the existing local Desktop configuration. Its credential is passed in memory through private stdin to the official ZCode app-server. The host does not log it, add it to command arguments, or write it to a new configuration. Claude subprocesses remove `ANTHROPIC_*` and `CLAUDE_*` environment overrides to preserve the account-login route used by this adapter. Provider credentials and client binaries are never distributed with this repository.

## Tool and process boundaries

The host tells participants to discuss supplied evidence and denies interactive server-side approval requests. Claude disables tools; Codex uses read-only sandbox settings; ZCode uses plan mode and a limited tool allowlist. These settings are not equivalent to a separate OS user, a container, or a guarantee that all client versions forbid every local read or command. Only run installed clients you trust, with the least filesystem access suitable for your environment.

Peer messages cannot authorize host file changes, shell execution, deployment, external messaging, or additional background work. A model's claim that it performed an action is not evidence that the action happened.

## Publishing and reports

The Git ignore rules exclude runtime data, logs, local client directories, common private-key formats, and environment files. `scripts/check_publication.py` checks the proposed Git index before publication, and CI scans the checkout. It is a focused release guard, not a substitute for reviewing files or a comprehensive credential scanner.

Do not attach full model logs, account configurations, private transcripts, native session IDs, or credentials to public issues. Use synthetic reproductions with minimal sanitized output.

For a sensitive vulnerability, use the repository's private vulnerability reporting feature if it is available. Otherwise, open an issue asking for a private reporting channel without disclosing the exploit or private data publicly.
