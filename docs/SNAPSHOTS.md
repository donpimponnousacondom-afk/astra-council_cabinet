# Council state snapshots

The modern dashboard's **Snapshots** page creates named, immutable copies of the local council state and restores either a complete copy or selected bot notes. This is an authenticated operator facility, not a model tool or a grant attached to a bot. Existing site-publication Git history remains independent.

## Before an experiment

1. Finish or cancel any work you do not want interrupted. Creating a snapshot **cancels active generations, tool jobs and background deliveries** using their existing orderly cancellation rules. It does not suspend a generation halfway and later resume it. Provider credit already spent and uncertain Discord/remote delivery retain their normal recorded outcomes.
2. Open **Snapshots**, enter a name such as `Before Loki experiment` and an optional description, and create the snapshot. The runtime stops all its writers, captures and checks the offline state, then reconstructs its services with the previous configuration. A previously paused runtime stays paused.
3. Check the resulting name, timestamp, source commit, size and compatibility. Keep your code release fixed for the experiment. Use a separate Discord channel and appropriate grants.
4. If needed, choose a snapshot and restore either the complete council or one bot's notes. Type its full snapshot identifier to confirm. Every restore creates and verifies a separate **recovery snapshot of the displaced state first**.
5. After restoration, inspect the state. **Resume runtime** is a separate action: restored clients, the scheduler, shell jobs and the publishing worker stay stopped until then, including after an application restart. Full restoration signs out existing dashboard sessions; sign in again to inspect and resume.

A restored pause blocks other dashboard mutations as well as background services. The user can inspect configuration, trajectories, notes and documents while deciding whether to resume. The normal council pause is different: it does not stop every writer and is not a substitute for snapshot maintenance.

## What is captured

Each snapshot has a manifest and a `data/` payload containing:

- SQLite copied through its backup API, including configuration, encrypted credentials, authentication state, private notes, contexts/checkpoints, transcripts, requests, trajectories, tool jobs and publishing queue records. Optional plugin tables, including per-bot global memories, are included automatically.
- The matching `master.key`, including an environment-supplied key when applicable.
- Managed stores when present: `artifacts/`, `images/`, `sites/`, `ssh/`, `workspaces/`, `jobs/`, `fetched_documents/` and the separate `site_history/` repository.
- `initial-password` when present.

Every file has a recorded size and SHA-256 digest. SQLite integrity, foreign-key references and decryption of all stored secrets are checked before declaring the capture complete. Publication uses a new immutable directory name only after the payload and manifest have been flushed. Snapshot directories are owner-only; payload files and the manifest are read-only (an existing owner-execute bit is preserved for executable workspace files). There is no overwrite or delete endpoint.

The application source, dependencies, live console logs, `runtime.lock`, external launcher/environment files and terminal settings are **not** included. The dashboard is an application-state snapshot facility; host disaster recovery still needs the additional machine configuration described in [OPERATIONS](OPERATIONS.md#backup-and-restore). Logs remain at their original path across a restore. Snapshot manifests and payloads contain private state and must not be committed to the application repository or published as a bot site.

The default catalog location is a sibling of the live directory: `/home/codexy/.local/share/hortator-snapshots` when the live directory is `/home/codexy/.local/share/hortator`. `HORTATOR_SNAPSHOT_DIR` may choose another external location. Locations inside the live directory or source checkout are rejected. Existing manually prepared backups under `hortator-backups` are preserved; they are not silently imported as compatible dashboard snapshots.

## Restore scopes

**Full council** replaces the managed application state from the selected snapshot. A prepared copy is validated before replacement. A journal makes the managed-file rename transaction recoverable while keeping the single-runtime lock inode and shared console log path intact. If interruption occurs partway through replacement, startup rolls back the transaction before constructing SQLite or any runtime service. An incomplete restored directory is never treated as a new installation. The immutable recovery snapshot remains available independently of the transaction staging files.

**One bot's private notes** is the default scoped restore. Choose a bot and optionally one channel. It replaces only that bot's private note rows in the selected scope. Other bots, other selected-out channels, model settings, grants, credentials, documents and history remain unchanged. A deleted bot is not recreated and ownership is never transferred. Snapshot notes must fit the current per-note limit and the selected bot's current aggregate budget plus its 5% grace allowance; incompatible quotas are rejected before any restore is applied.

**Include global memories** is an explicit additional choice. It restores only the selected bot's cross-channel notes, never a council-wide store or another bot's notes. A channel-only restoration does not otherwise touch global notes.

**Include conversation summary/checkpoints** is also explicit and off by default. It restores that bot's context rows, including summary and checkpoint state. It does **not** delete newer shared Discord transcript rows: those can re-enter subsequent context. Use a full snapshot and/or a new experiment channel when an exact prior transcript state is required. Both the snapshot and current database must identify the same Discord message/channel at every nonzero checkpoint and last-seen sequence. Missing or divergent anchors reject the context restore, preventing a newer snapshot checkpoint from suppressing input after an older full restore. Immutable trajectory evidence is not rewritten during a selective restore.

## Compatibility and external effects

A snapshot records its format version, package/source identity, complete source commit, SQLite schema fingerprint and API configuration-schema fingerprint. Restoration requires the **same clean source commit and matching schemas**. Unknown or dirty builds can be captured for emergency inspection, but are marked incompatible with automated restoration. There is no implicit database migration, code checkout, source reset or `force incompatible restore` switch. Return to the matching release to restore that release's state. Branch names and friendly snapshot names are labels, not compatibility guarantees.

Full restoration revokes authentication sessions rather than resurrecting older tokens. If `HORTATOR_MASTER_KEY` is authoritative, it must match the snapshot key; an incompatible environment key is rejected. `HORTATOR_ADMIN_PASSWORD` remains authoritative at reconstruction, just as at an ordinary startup. No private key, password or credential value is returned in the catalog or operation receipts.

Discord messages, provider bills and remote web publications cannot be rolled back by restoring local files. Publishing queue state returns with a full snapshot and remains inactive during the restored pause. Before resuming an old queue against a newer remote deployment, inspect publication receipts/history and remote state. The existing publisher's receipt/ownership safeguards still apply; local restoration does not assert that the remote site was restored.

## API contract

All routes require the existing dashboard session. Mutations require the existing origin and CSRF checks. None are exposed to Discord commands or model inspection tools.

| Endpoint | Behavior |
| --- | --- |
| `GET /api/snapshots` | Catalog, compatibility reasons, directory, maintenance state and last restore receipt |
| `POST /api/snapshots` | `{ "name": "Before experiment", "note": "Optional description" }`; complete offline capture |
| `POST /api/snapshots/{id}/restore` | `{ "confirmation": "<same full id>", "scope": "full" }`, or bot scope below |
| `POST /api/snapshots/resume` | Explicitly leave restored pause and reconstruct normal runtime services |

A scoped body uses `scope: "bot"`, `bot_id`, optional `channel_id`, and optional booleans `include_context` and `include_global_memory` (both default false). The confirmation is the snapshot ID, not its friendly name.

Catalog entries include `id`, `name`, `note`, `created_at`, `build`, `source_commit`, `format_version`, `database_schema`, `file_count`, `bytes`, `reason`, `recovery_for`, `bots` with their stored channels, `compatible`, `reasons` and `compatibility_reason`. Capture returns `{snapshot, paused}`. Restore returns `snapshot_id`, `recovery_snapshot_id`, scope selections, `restored_at`, `paused: true`, an external-effects note and an additional context caveat when relevant. `/api/status` also reports `maintenance_pause` and `snapshot_operation`.

During a maintenance transaction, in-flight HTTP handlers drain and new requests receive a clear retryable 503. Old event streams exit before querying the closed store. The API listener and shared Screen session stay in place. The same application TaskGroup owner closes the old Kernel lifetime, performs the offline operation and opens the replacement; an HTTP request never closes another task's TaskGroup. Browser disconnection cannot abandon an already-started restore. Filesystem worker threads are joined before shutdown releases the runtime lock.

Operational events identify snapshot creation, restoration, resume and failures, including operation/snapshot/recovery IDs without note contents or credentials. Restore failure messages retain the real cause and whether the runtime remains paused. API status and catalog receipts remain the source of truth after a browser reconnect.
