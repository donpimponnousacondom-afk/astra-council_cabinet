# Simple filesystem

`filesystem` is a separate, optional keyless plugin. Enable it globally and grant
it to each bot that should use it. It supplies one persistent directory per
stable bot identity, shared across that bot's authorized conversations. Renaming
the bot does not change its files. There is no task name, start operation, or
workspace selector. Private workspaces retain their existing contract in
[WORKSPACES.md](WORKSPACES.md).

## Owner decisions

On 2026-10-18 the owner requested an explicit `/tmp` lifetime reminder after Sun
and Zelda tried to reuse downloads from earlier shell calls. Put it in the existing
late mount notice, without changing tool schemas or storage behavior. Files needed
by later calls belong in the mounted `/workspace`.

On 2026-10-15 the owner retained the existing file/directory limit after a package
install exceeded it. Do not raise limits as a workaround. Models receive explicit
saved-package instructions and entry-limit failure receipts; changing usage stays
in tool results rather than the stable prompt prefix. Suitable regular-file
package trees can persist with `pip install --target /workspace/pylibs PACKAGE`
and `PYTHONPATH=/workspace/pylibs` on subsequent calls, within all existing
limits. See [package guidance and failure details](SHELL_RUNNER.md#python-314-and-disposable-packages).

On 2026-10-10 the owner authorized this independent plugin for experimentation,
without migrating existing tasks or changing bot grants automatically. The final
decision **supersedes the earlier read-only-on-disable proposal**: OFF unmounts
the saved filesystem completely. Stored files remain for re-enabling, but the
bot cannot read or write them while OFF. Do not add a read-only mode.

The default capacity is 256 MiB, with no inactivity expiry or automatic eviction.
Show used/total capacity and a whole-number percentage. The owner removed the
proposed "Keep your own files organized" instruction; do not add a tidiness,
cleanup-pressure or punishment prompt. Intentionally saved compatible dependency
files use the same quota. Default disposable installations remain disposable.
Do not add package-specific bans or silently relax the inherited path, original
attachment, link or credential boundaries. No background shell process may
survive the end of its shell call.

On 2026-10-15 the owner requested a file limit that scales with saved capacity:
each regular file may occupy at most half of that bot's effective capacity,
rounded down to whole bytes. There is no independent file-size setting. A
256 MiB capacity allows 128 MiB per file; 512 MiB allows 256 MiB. This is a
per-file ceiling, not a reservation: several files can collectively fill the
whole capacity. The total quota and existing file-count limit still apply.

On 2026-10-15 the owner requested that executable files survive separate shell
calls, after confirming the earlier workspace limitation remained unresolved.
Preserve the owner-execute bit across shell snapshots and saved generations:
regular files are stored as 0600 or 0700. Existing file edits/overwrites preserve
that bit; new file-tool writes start non-executable. Other permission bits and
special mode bits are not retained. This does not expand tool grants or host access.

When the mount is OFF, explicitly tell previously provisioned bots that their
saved files still exist, cannot currently be accessed, and require the owner's
help if needed. Do not imply that disabling the mount deleted files. A bot that
has never provisioned this filesystem must not be told it already has saved
files. Unrelated bots receive no filesystem mount notice.

## Model-visible behavior

| Grants and global switches | Available behavior |
| --- | --- |
| Filesystem and Bash ON | File operations; Bash automatically uses the same saved `/workspace` |
| Filesystem ON, Bash OFF | File operations without command execution |
| Filesystem OFF, Bash ON | Saved files inaccessible; a no-task Bash call gets empty disposable `/workspace` |
| Both OFF | Neither capability; stored files remain for re-enabling |

If Private workspaces is also granted, an explicit existing `task` selects that
legacy store; omission selects Simple filesystem while mounted. Without Private
workspaces, the mounted Bash schema has no `task` argument. An invalid explicit
task never falls back to another store. Saved Simple filesystem job/evidence
reads retain the filesystem grant, including pages reread through other tools.
Turning it off is not a transcript erasure; previously returned text can already
be in conversation context.

Runtime facts at the prompt tail report mount state and current usage, without
rewriting stable schemas with byte counters. The ON notice is:

> Simple filesystem is ON. Your saved files are at /workspace. The filesystem
> tool and, when available, shell.run use this same directory automatically.
> Files remain until deleted; /packages and running shell processes do not
> survive a shell call. /tmp is cleared after every shell call. Save files needed
> by later calls under /workspace.

Usage includes `display: "182/256 MiB (71%)"`, exact used/limit/remaining bytes,
integer percent, entry count and file/entry limits. Displayed MiB and percent are
rounded to whole numbers; very small usage can display zero, with exact bytes
still available in the same result. Status, file actions and filesystem shell
results also report usage.

The OFF notice for previously saved files is:

> Your filesystem mount is OFF. Your saved files still exist, but you cannot
> access them for now. Ask the owner if you need something there. Do not invent
> file contents or claim to have inspected the unavailable filesystem. If Bash
> is available, its /workspace is temporary storage, not your saved files.

The actual wording is defined by `DESCRIPTION`, `MOUNT_ON` and `MOUNT_OFF` in
`hortator/filesystem.py`, and `FILESYSTEM_SHELL_DESCRIPTION` in
`hortator/agentic.py`. Operator-owned prompt overrides remain operator-owned.

## Tool schema and discovery

The model receives the available tool schema before calling it. `filesystem({})`
returns complete schema/help and an example, with `usage_only: true` and
`executed: false`; it creates no files. Named JSON arguments, full validation
feedback and durable result paging follow [TOOLS.md](TOOLS.md). Help is optional
and consumes ordinary call/round budgets. File operations do not implicitly
extend the configured tool budget.

| `operation` | Required arguments | Optional arguments |
| --- | --- | --- |
| `status` | none | none |
| `list` | none | `path` (default `.`), `cursor`, `limit` |
| `stat` | `path` | none |
| `read` | `path` | `offset`, `limit_bytes`, `encoding` |
| `write` | `path`, `content` | `encoding`, `overwrite`, `expected_sha256` |
| `edit` | `path`, `old_text`, `new_text` | `replace_all`, `expected_sha256` |
| `mkdir` | `path` | none |
| `move` | `path`, `destination` | `overwrite` |
| `delete` | `path` | `recursive` |
| `import_attachment` | `path`, `message_id`, `attachment_id` | `overwrite`, `expected_sha256` |
| `export` | `path` | none |
| `read_result` | `result_id` | `offset`, `length` |

Paths are relative to `/workspace`. `list` lists directory contents and returns
explicit `next` arguments when another page exists. File offsets count bytes;
saved-result offsets count characters. Writes create parents and require explicit
overwrite of an existing file. Edits require an unambiguous text match unless
`replace_all` is true. Move destinations require existing parent directories;
only files can overwrite files. Nonempty directory deletion requires
`recursive: true`. The root itself cannot be removed or moved.

```json
{"operation":"write","path":"notes/USAGE.md","content":"Working notes"}
{"operation":"list","path":"notes"}
```

Then, through the separately granted shell:

```json
{"operation":"run","command":"cat /workspace/notes/USAGE.md"}
```

Observed attachment IDs remain scoped to the actual current channel even though
saved files span conversations. Imported originals retain the existing protection
against alteration, moving and deletion; save transformed copies to other paths.
Export registers a current-turn artifact for `discord_attach`; it does not post.
Original intake/export sizes and delivery grants are unchanged.

## Limits and persistence

Global configuration and per-bot overrides use these strict integer settings:

| Setting | Default |
| --- | --- |
| `max_bytes` | 268,435,456 bytes (256 MiB) |
| `max_files` | 1,000 files and directories |
| `max_read_bytes` | 12,000 bytes per file-read page |

The effective `max_file_bytes` in status, usage and shell limits is always
`floor(max_bytes / 2)`, derived after global configuration and per-bot capacity
overrides are combined. The dashboard displays the automatic global value in
plugin settings and each existing filesystem's effective value in the bot's
Files, web reading and jobs panel (including capacity overrides). A 20 MiB
capacity therefore allows 10 MiB per file; the existing attachment intake
ceiling does not override this smaller saved-file allowance.

Startup removes the retired `max_file_bytes` configuration key from the global
filesystem plugin and per-bot filesystem overrides in one transaction, recording
each removal in `config.migrated`. All other settings, grants and saved files
remain intact. New configuration writes containing the retired key fail with
instructions to remove it; change `max_bytes` to change the file limit. Private
workspaces and disposable shell storage keep their independent existing limits.

There is no task-count setting or retention timer. Filling the filesystem blocks
growth, not deletion or directory listing. The direct delete operation does not
copy the whole filesystem, and remains usable after an operator lowers a quota.
The byte quota counts saved logical data; shell snapshots and atomic replacement
require additional temporary space. It is not a cap on total host disk, job logs
or package scratch.

Bash retains the existing [sandbox lifecycle](SHELL_RUNNER.md): a fresh bounded
working copy, validated save on normal completion (including ordinary nonzero
exits), and rollback on timeout, cancellation or invalid output. The runner
terminates children before returning the tool result. File access from another
conversation is rejected while a job/import holds the same bot's filesystem.
Configuration edits cancel affected bot turns; save also rechecks the current
grant and limits before committing. Turning OFF cannot commit an in-flight
filesystem job under its previous grant.

Default pip/uv/native installs in `/packages` vanish after every shell call.
Compatible package files explicitly installed under `/workspace` can persist;
later calls must select their import path again. This is not a complete persistent
container: links are rejected and ordinary virtual environments are not supported
unchanged. Supplied runtimes remain read-only and are provided afresh. Owner-execute
permission now persists through shell calls, including an explicit `chmod u-x`.
File list/stat/read results expose an `executable` boolean. Files whose bit was
lost before this fix need one successful `chmod u+x` shell save; no existing
file is automatically marked executable. The October 10 planning probe in
[VERIFICATION.md](VERIFICATION.md) records the superseded execute-bit limitation.

## Operator inspection and backups

Use **Plugins → Simple filesystem** for enablement and limits, and grant it
through **Bots → Capabilities**. The existing **Files, web reading and jobs**
panel shows each bot's single directory and used/total percentage, with file
inspection. The authenticated operator can inspect saved files while the bot's
mount is OFF.

Storage is `$HORTATOR_DATA_DIR/filesystems/<opaque-filesystem-id>/<generation>/`.
SQLite `filesystems` holds the unique bot identity and active generation;
`filesystem_imports` holds protected originals. The same validated generation
engine is shared with Private workspaces, with separate tables and storage roots.

Explicitly requested full snapshots and CLI backups include the database and
`filesystems/` together. Follow [OPERATIONS.md](OPERATIONS.md#backup-and-restore).
Enabling the feature does not create a backup, migrate old tasks, or enable a bot.
Partial conversation restore retains its existing scope; it is not a filesystem
restore. New installations leave this plugin disabled.
