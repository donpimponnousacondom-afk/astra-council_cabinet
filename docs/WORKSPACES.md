# Private task workspaces

`workspace` is an optional, keyless plugin. Enable it globally and grant it to each bot that needs files. Grant `shell` separately for actual isolated Bash execution. Existing bot enablement, room policy, and the exact human-owner rule for Hortator still apply. A workspace grant grants no publication or direct Discord send capability.

A task belongs to one bot and one channel. Its slug is 1–64 lowercase letters, digits or hyphens. Another bot or channel using the same slug gets a separate workspace; handles cannot cross that scope. The owner can inspect files through the authenticated dashboard. Files persist across turns and restarts until their inactivity retention expires.

## Tool operations

Calling `workspace({})` returns the full schema and examples without creating files or opening a task. Invalid parseable arguments return all detectable schema errors together, including conditional required fields, types, bounds, path constraints and unknown fields. The common registry handles discovery, authorization, malformed JSON, redaction and usage feedback; handlers never introduce a second help convention. See [TOOLS.md](TOOLS.md).

| Operation | Required fields after `operation` | Result |
| --- | --- | --- |
| `start` | `task` | Create/resume a task; report limits, expiry, working directory and whether it resumed; open the shared bounded work extension once in this turn |
| `list` | none | This bot/channel's tasks; supply `task` and optional directory `path` to list files |
| `stat` | `task`, `path` | Exact encoded byte count, SHA-256, MIME and image dimensions where recognized; directory totals; file export eligibility |
| `read` | `task`, `path` | Bounded UTF-8 text, or explicit `encoding: "base64"`; byte ranges and continuation |
| `write` | `task`, `path`, `content` | Atomic UTF-8 or strict-base64 write; create parent directories; existing files require explicit `overwrite: true` |
| `edit` | `task`, `path`, `old_text`, `new_text` | Exact text replacement, with unique-match enforcement unless `replace_all: true`; optional `expected_sha256` guards stale edits |
| `mkdir` | `task`, `path` | Create bounded parent directories; `.` refers to the root |
| `import_attachment` | `task`, `path`, `message_id`, `attachment_id` | Import an observed attachment in this channel, preserve original bytes and protect its path from transformation |
| `export` | `task`, `path` | Register a current-turn artifact for `council_speak.artifact_ids`; report its applicable delivery limit |
| `read_result` | `result_id` | Registry-provided reread of durable tool-result JSON; `offset` and `length` count characters, with source grant and bot/channel/turn checks |

Paths must be relative and at most 240 characters/32 levels. Absolute host paths, `..`, empty path components, control characters, and backslash separators are rejected. File operations open every component relative to an already-open directory with `O_NOFOLLOW`. They reject symbolic links, multiply linked files, devices, FIFOs and sockets. A sandbox job pins its workspace; concurrent file access is rejected with a wait-for-completion message instead of racing a mutable tree. Shell commands use their own isolated temporary snapshot and never receive a writable mount of host workspace data.

`read.offset` and `read.limit_bytes` count **bytes**. Follow the returned `next_offset`; UTF-8 chunks end at whole character boundaries, with no lost or duplicated bytes. An offset in the middle of a character gives a clear error. Exact end-of-file returns empty content, `eof: true` and no continuation. Binary reads require explicit base64. Hashes identify the complete file, while returned content is bounded and labeled untrusted. A later file edit can change its hash; these files are mutable working material, not immutable fetched-page snapshots.

## Import, compress, attach

For an attachment already observed in the authorized channel, use its real message and attachment IDs:

```json
{"operation":"start","task":"compress-image"}
{"operation":"import_attachment","task":"compress-image","path":"original.png","message_id":"123456789012345678","attachment_id":"987654321098765432"}
{"operation":"stat","task":"compress-image","path":"original.png"}
```

With `shell` also enabled and granted, run this command through `shell` with `operation: "run"` and `task: "compress-image"`:

```bash
python - <<'PY'
from PIL import Image
with Image.open('original.png') as image:
    image.save('compressed.png', format='PNG', optimize=True)
PY
```

PNG optimization is lossless but its reduction depends on the source. The model must inspect the measured output and choose another explicitly requested transformation if it remains too large. Then:

```json
{"operation":"stat","task":"compress-image","path":"compressed.png"}
{"operation":"export","task":"compress-image","path":"compressed.png"}
```

Use the returned artifact ID in the existing `council_speak.artifact_ids` response. An export is registration, not delivery. The existing final-response handling, mention suppression, ownership checks, cooldowns and uncertain-send behavior still decide delivery. An authorized persistent output can be exported again in a later turn, producing a fresh artifact owned by that turn. An artifact ID from an earlier turn cannot be reused directly.

Image imports reuse integrity-checked cached pixels from [VISION.md](VISION.md), including images larger than 8 MiB. The original image intake limit remains **20 MiB (20,971,520 bytes)** and **20 megapixels**. The automatic vision pipeline's separate combined request budget remains 40 MiB and eight images. General attachment imports are also capped at 20 MiB. Only stored Discord attachment IDs and trusted CDN observations are accepted; a model cannot supply a download URL. Uncached imports have bounded network deadlines and do not use ambient proxy credentials. Images retain their original encoded bytes and pixels.

An expired signed URL produces an explicit failure. A refreshed authenticated Discord observation or a new upload can repair it; cached pixels remain usable after the URL expires. An edit/removal observed during a download cancels that import. Imported paths are protected: file writes and sandbox snapshot commits reject changed or deleted originals. Save transformations under a new filename.

Outbound artifacts retain their separate **8,000,000-byte** delivery limit. A 10 MiB image can be imported and inspected, but must be compressed into an eligible output before attachment export. Document-site import limits are independent. Workspaces do not read or write the site's publication store and do not enable any remote transport.

## Storage, quotas and retention

Workspace bytes live under `$HORTATOR_DATA_DIR/workspaces/<opaque-workspace-id>/<generation>/`. The SQLite tables `workspaces`, `workspace_imports` and `workspace_turn_refs` retain scope, active-generation pointers, original hashes and turn references. Directories are private and stored files use owner-only permissions. Runtime files must never enter Git.

Global plugin configuration is merged with per-bot overrides. Configuration values are strict integers; strings, booleans, unknown fields and inconsistent limits are rejected. Defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_file_bytes` | 33,554,432 (32 MiB) | Maximum locally stored file; cannot be lowered below the 20 MiB image intake ceiling |
| `max_workspace_bytes` | 134,217,728 (128 MiB) | Logical bytes in a task's complete active tree |
| `max_bot_bytes` | 536,870,912 (512 MiB) | Combined active task trees for a bot |
| `max_files` | 1,000 | Files and directories per task, including nested directories |
| `max_workspaces` | 32 | Tasks per bot across channels |
| `max_read_bytes` | 12,000 | Maximum bytes in one file-content result; upper bound 18,000 |
| `retention_days` | 30 | Expiry after the task's last model use |

Writes use a temporary file, fsync and atomic replacement. Completed shell output is validated as a complete tree, checked against task/bot quotas and protected originals, copied into a private generation, then committed through one SQLite generation pointer. The pointer and filesystem directory entries are flushed before deleting the predecessor. Interrupted copyback leaves the prior generation intact. Obsolete generation cleanup retries on task start/job preparation. Unknown unreferenced task roots are not automatically erased; inspect them when restoring a mismatched backup.

Quotas describe committed active data. Atomic writes and shell copyback need additional temporary space for an old/new generation and the bounded runner staging area; provision that headroom. The runner separately bounds its temporary filesystem, processes, memory and output logs. Exceeding a quota rejects the new content instead of evicting other tasks. Original image-cache pixels and exported artifacts belong to their existing stores and retention policies, independently of workspace expiry.

Starting a workspace opportunistically removes up to 100 expired tasks for that bot. `prune_expired` also provides a bounded operator/runtime maintenance hook. It skips tasks pinned by jobs/imports and tasks referenced by running turns. Ordinary reads/writes/imports/export/job use refresh retention; authenticated inspection does not. Expiry removes only that task's workspace tree and metadata, preserving the event ledger and independent exported artifacts. Back up outputs needed beyond the configured lifetime.

Consistent backup and restore must include **the database and `workspaces/` together**, plus the image cache, artifacts, fetched documents and job storage. Stop/finish active work before a backup as described in [OPERATIONS.md](OPERATIONS.md). A database-only restoration cannot recreate task bytes. Restore storage permissions and the matching image cache when attachment URLs may have expired.

The focused workspace tests use isolated temporary storage, synthetic Discord observations and artifact transport. They include an actual valid PNG above 8 MiB, complete metadata, original protection, local Pillow compression and current-turn registration. Actual isolated Bash and mocked Discord delivery are separate runner/integration checks; none imply a live provider or Discord acceptance test.
