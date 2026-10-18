# Isolated Bash runner

The optional keyless `shell` plugin runs real Bash pipelines, scripts, Python 3.14 and Pillow. Enable and grant `shell` for disposable commands; no workspace grant or task is needed. For named persistent tasks, also grant `workspace`, create a task with `workspace.start`, and explicitly pass that task to `shell.run`. The separate [Simple filesystem](FILESYSTEM.md) provides automatic per-bot storage without task selection. The dashboard exposes runner readiness and the independent working limits. Existing owner identity, room policy, global enablement and bot grants still apply. See [AGENTIC_TOOLS.md](AGENTIC_TOOLS.md) for the complete workflow and [TOOLS.md](TOOLS.md) for empty-call discovery and complete argument-error feedback.

## Disposable commands and persistent tasks

The optional [Simple filesystem](FILESYSTEM.md) adds a third storage mode:
when it is enabled and granted, omitting `task` automatically uses the bot's one
saved `/workspace`. Disabling that plugin unmounts its files; stored bytes remain
for re-enabling. Its job evidence retains the filesystem grant. An explicit
Private workspace `task` still selects the legacy store when separately granted.

Without a mounted Simple filesystem, omitting `task` selects disposable storage,
even when the bot also has a workspace grant or existing tasks:

```json
{"operation":"run","command":"curl --fail --silent --show-error https://example.com"}
{"operation":"run","command":"python3 - <<'PY'\nprint('inline Python works too')\nPY"}
```

Each such call starts in an empty `/workspace`. Heredocs, scripts, HTTP POST and the existing network/toolchain work normally. Files and working-directory changes disappear after that call on success, failure, timeout or cancellation. No persistent workspace record, lease or file generation is created. Scratch uses the workspace baseline bounds: 128 MiB, 32 MiB per file and 1,000 entries, independent of workspace-plugin settings. The package storage and other shell settings below apply to both modes. Disposable files are validated under the same filesystem rules but never copied back to the host. `cwd`, if supplied, must exist in the fresh directory; normally omit it or use `.` and change directory within the command.

Job metadata, stdout/stderr and tool-result evidence **remain inspectable** under the usual scope and retention; disposable storage is not an unlogged execution mode. Results identify `storage: "disposable"`, `task: null`, `workspace_id: null` and `workspace_committed: false`. Starting a shell command does not extend the turn budget.

Model job IDs and saved-result IDs are restricted to the bot, channel and
response cycle that created them. A later cycle cannot reread those logs even
when saved files still exist. Reopen persistent files through Simple filesystem
when mounted, or their original Private workspace task when enabled; disposable
files do not survive. Same-bot/channel old-job errors name the storage mode;
foreign-bot/channel errors do not expose that metadata. Operator inspection and
retention are unchanged.

`shell.read(job_id, stream, offset, limit)` counts **bytes** and accepts `limit`
from 1 to 8,000. `shell.read_result(result_id, offset, length)` counts **Unicode
characters** and accepts `length` from 1 to 18,000. These are separate operations:
passing `length` to `read` is rejected. Both shell descriptions, including the
Simple filesystem variant, expose these signatures and scope before execution.

An explicit `task` selects persistent storage (`storage: "workspace"`). It requires the current workspace grant and a previously started task in this bot/channel. A missing task fails; it never silently falls back or creates a workspace. Subsequent run/status/read/cancel and saved-result reads for that job retain the workspace dependency. Disposable job access needs only shell. Pre-existing jobs and evidence pages retain their original workspace requirement, including pages read through another tool. Evidence schema grant version 0 preserves that historical implicit dependency; version 1 records explicit dependencies and propagates them through pages. No stored transcript or operator prompt is rewritten.

For persistent files:

```json
{"operation":"run","task":"image-work","command":"python3.14 - <<'PY'\nfrom PIL import Image\nfrom pathlib import Path\np=Path('original.png')\nim=Image.open(p)\nprint(p.stat().st_size, im.size)\nim.convert('RGB').save('compressed.jpg',quality=60,optimize=True)\nprint(Path('compressed.jpg').stat().st_size)\nPY"}
```

Export `compressed.jpg` with `workspace.export`; prepare its current-turn artifact ID with `discord_attach`, then answer normally. Shell execution never sends to Discord or publishes sites. Imported originals are protected during workspace copy-back: changing or deleting an original rejects the entire output snapshot. Image intake remains 20 MiB; the independent attachment export ceiling is 8,000,000 bytes.

## Operator requirements and readiness

Run the application under a non-root Linux account using **Python 3.14**. `.python-version`, the package metadata and lockfile select this minor version; readiness rejects an application running another Python version. Do not replace the operating system's Python. Supply Bubblewrap with `--size`, `--disable-userns`, `--assert-userns-disabled` and `--as-pid-1`, Linux namespaces/pidfds, libseccomp, uv and micromamba. Missing selected tools fail readiness explicitly, with no host-shell fallback.

On Debian 13 the operator package set is:

```bash
sudo apt-get install bubblewrap libseccomp2 ca-certificates bash coreutils findutils grep sed gawk \
  curl wget git jq perl procps iproute2 net-tools file gzip tar zip unzip \
  diffutils patch xz-utils bzip2 binutils
uv python install 3.14
uv sync --frozen --python 3.14
python3.14 scripts/install-micromamba.py
```

The bootstrap installs the pinned micromamba 2.9.0-0 executable to `~/.local/bin`, checks the pinned SHA-256 before atomic replacement, and supports Linux x86_64/aarch64. Ensure that directory and uv are in the service launch PATH. It never runs during readiness or a model turn. The Dockerfile provisions the same dependencies; the outer container must permit the required unprivileged namespace operations. The tested host provides Bubblewrap 0.12.0; package installation alone does not guarantee compatible host/container namespace policy.

The selected toolchain includes the coreutils command set, Bash/sh, find/grep/sed/awk, archive/file/diff/patch tools, **curl, wget, git, jq, Perl, ps/pgrep/pkill, id/whoami/groups, ss/netstat/ip**, **Python 3.14/Pillow/pip, uv and micromamba**. Readiness lists the actual selection. Git's HTTPS helpers/templates, Perl's module directories and required shared libraries are mounted read-only. Node, sudo, apt, SSH credentials and service managers are not supplied. `id`/`whoami` use a synthetic `agent` account (1000); `ps` sees sandbox processes, while `ss`/`netstat` see shared host network sockets. Some coreutils operations still require unavailable kernel privileges; binary presence does not grant capabilities. GUI/Tk is not supported.

## Python 3.14 and disposable packages

Every job starts with an active `/packages/venv`. `python`, `python3` and `python3.14` resolve to Python 3.14. `pip`, `pip3` and `pip3.14` target that environment, as does `uv pip install`. The base interpreter is read-only and carries an `EXTERNALLY-MANAGED` marker with actionable guidance. `ensurepip` is available for ordinary venv creation. uv defaults to this explicit Python 3.14 and disables automatic interpreter downloads. The baseline has no older Python; these defaults do not prevent an authorized networked shell from deliberately downloading arbitrary executables.

```bash
# Install AND use within one shell.run command:
uv pip install six==1.17.0
python3.14 -c 'import six; print(six.__version__)' > result.txt

# Extra isolated environment when needed:
python3.14 -m venv /packages/extra
/packages/extra/bin/python3.14 -m pip install PACKAGE

# Native packages and their dependencies from conda-forge:
pkg install zstd
zstd input.txt -o output.txt.zst
```

`pkg install PACKAGE...` uses micromamba in `/packages/native`, whose `bin` is already on PATH; subsequent installs add to the same environment throughout the human task. Packages, virtualenv symlinks, caches, installer temporary files and `/tmp` share one **2 GiB task-scoped tmpfs**, with a monitored 50,000-entry ceiling. This supports rootless native packages without a writable host filesystem or pretending that apt works inside the sandbox. Native installation depends on upstream package availability and remains subject to the 90-second command deadline, memory, process, output and storage limits. It is not a guarantee that every large package or source build will fit. No native application package (including ImageMagick) is preinstalled by this feature. [micromamba installation](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html), [uv environments](https://docs.astral.sh/uv/pip/environments/), [Python 3.14 venv](https://docs.python.org/3.14/library/venv.html)

**`/packages` and `/tmp` survive successive shell calls in the same human task.**
Every call still starts fresh command processes and destroys all its descendants.
An ordinary same-human, same-channel follow-up can retain scratch when it replaces
an unsent answer. A final answer, silence, failed turn, explicit cancellation,
configuration invalidation, context reset or shutdown releases scratch. Routine
turns and invocations receive scratch for their current internal turn only.
Scratch is not durable: a crash loses it, and a failed shell command can leave
partial scratch changes for the next call to inspect or remove. Saved `/workspace`
commit/rollback rules remain separate. No prepared attachments or old job-ID
permissions are transferred with scratch.

There is no guessed idle TTL or RAM-pressure eviction. An unfinished task waiting
for its eligible follow-up keeps its allocation when the bot works in another
conversation, including when preparation of that other turn fails. In the original
conversation, a qualifying same-human follow-up adopts it; a nonmatching human or
routine turn replaces it. Explicit bot cancellation, configuration invalidation,
context reset and shutdown still release the bot's allocations. A different human
or conversation cannot adopt them. Changes to scratch capacity/entry settings
replace the old allocation before another call.

`XDG_CACHE_HOME=/packages/cache` routes applications that honor XDG cache settings
into task scratch, alongside the existing UV/PIP caches. These caches share the
package byte/entry allowance and are released with the task. `HOME=/workspace`
and normal configuration/data/state lookup stay unchanged. Existing saved
`~/.cache` files are not moved or deleted, but applications may stop reusing them
by default and download or rebuild caches in later tasks. With persistent storage
mounted, an explicit `XDG_CACHE_HOME=/workspace/.cache COMMAND` can retain a
suitable cache within the saved-file quotas and regular-file rules. Applications
that ignore XDG cache settings retain their own behavior.

Worker setup repairs a conflicting file or link at the required `/packages/cache`,
`/packages/tmp` and `/packages/tmp/runtime` directories without following link targets. The latter
is the private `XDG_RUNTIME_DIR`, keeping audio-library runtime files in scratch.
Missing default-venv scaffolding (including `pyvenv.cfg`) is restored in place;
installed packages are retained. A setup failure explains that Bash did not start.
Use `command -v NAME` to locate available commands; a separate `which` is not required.

To retain suitable regular-file Python packages **across completed human tasks**,
Simple filesystem or an explicit persistent Private workspace can optionally save
them under `/workspace`:

```bash
pip install --no-cache-dir --no-compile --target /workspace/pylibs PACKAGE
# On each call that uses those saved packages:
PYTHONPATH=/workspace/pylibs python3 script.py
```

Choose packages and dependencies to fit both the saved byte capacity and the
independent file/directory entry ceiling. These options avoid a persistent pip
cache and bytecode compilation during installation; they do not guarantee that
an arbitrary package tree fits or is compatible with the supplied Python/native
libraries. Keep virtualenvs, linked trees and package caches in `/packages`;
saved trees still reject links and invalid/oversized files. A failed or interrupted
snapshot does not save a partial install. Requirements/lockfiles remain useful
for deliberate reinstallations. A disposable `/workspace` never persists.
`/tmp` is an alias for `/packages/tmp`; `TMPDIR` points there too. Both names
consume the same byte and entry allowance. Scratch is not persisted or backed up;
large intermediate audio and package trees belong here, with final deliverables
copied into `/workspace` within its independent limits.

`pkg install` confirms automatically and accepts optional `-y` / `--yes` before or after package names. Other arguments remain literal package operands. The wrapper selects the bundled read-only `/runner/mambarc` explicitly, which sets metadata parsing to one worker and bypasses ambient RC-file discovery. The shell environment requests one download and extraction worker. The pinned micromamba version ignores an environment variable for metadata threads, so that setting must come from the explicit file. This reduces installer concurrency; it does not raise any sandbox limit. Use `command -v node` (or another command name) to inspect availability; `which` is not supplied.

A nonzero exit from an otherwise completed disposable job is reported as `command_exit`, with disposable files discarded. Persistent jobs retain their existing saved-workspace label. Runner/validation errors remain distinct; `workspace_committed: false` alone is not an error for disposable jobs.

## Execution boundary

A short trusted setup process creates private user/mount namespaces and a sized
tmpfs. The runner pins those namespaces with file descriptors, then joins the
setup process. Before each job, a trusted launcher enters that outer namespace
and execs bubblewrap; it closes the setup descriptors before execution. The
normal fresh sandbox below is then constructed. Only the task tmpfs is reused;
the model never runs in the outer setup namespace or receives its handles. The
host mountpoint is an empty private directory, not a writable host data tree.
Closing the descriptors after the last job releases the tmpfs without copying
or hashing package contents. Readiness exercises this setup path and fails closed
if the kernel cannot provide it.


Bubblewrap creates private user, PID, mount, IPC and UTS namespaces and explicitly shares host networking (`--share-net`). The sandbox UID/GID is 1000 with all capabilities removed, `no_new_privs`, further user namespaces disabled and a read-only root. Only the current task's input snapshot (or an empty directory for a disposable job) is visible read-only at `/source`; the writable `/workspace` is a finite tmpfs copy. `/packages` binds the current task's private tmpfs, and `/tmp` aliases its `tmp` directory. The minimal environment contains a fixed tool path, workspace home, temporary directory, C locale and runtime library settings. Bash uses `--noprofile --norc`; the human Screen shell retains its own normal configuration. This follows bubblewrap's documented filesystem and namespace controls. [Bubblewrap manual](https://raw.githubusercontent.com/containers/bubblewrap/main/bwrap.xml)

The sandbox has no host home, application checkout, runtime database, vault, master key, bootstrap password, ambient proxy variables, SSH/Screen sockets, Docker socket or other workspaces. Its `/proc` describes only the new PID namespace and is mounted read-only. Network sockets are allowed; DNS, hosts/NSS configuration and a CA trust bundle are bound read-only. Outbound connections and host loopback services are reachable. Seccomp denies further namespace/mount operations, ptrace/process-memory access, memfd/SysV shared-memory creation, kernel keyrings and related unsupported kernel interfaces. Granted shell commands can fetch directly; bounded web-fetch and observed attachment-import tools remain available. The supervisor is nondumpable; model children cannot open its `/proc/1/fd` transport handles. [libseccomp rule API](https://libseccomp.readthedocs.io/en/latest/man/man3/seccomp_rule_add.3/)

The trusted supervisor starts with only the readonly baseline library search
path and loads libseccomp from its readonly absolute path. Only the command
subprocess receives `/packages/native/lib` in its loader search path; retained
package libraries must not influence supervisor startup.

The trusted supervisor is namespace PID 1. It reads stdout and stderr concurrently, kills remaining descendants (including detached sessions), validates the complete output tree and sends bounded regular-file frames to the host. The host independently enforces paths, counts and byte sizes, then asks the workspace store to commit a validated generation atomically. There is no writable host mount accessible to the model. For persistent tasks, successful valid changes and valid changes from nonzero command exits persist. Disposable jobs never export file bytes or commit a generation. Timeout, cancellation, output-limit termination, invalid output trees and interrupted execution discard the sandbox snapshot. A script can therefore have run even when its workspace changes are not committed; check `workspace_committed` and the reported status.

For both saved storage types, the owner-execute bit is carried through the input
copy, the worker's strictly boolean `executable` file-export field, host staging,
and the committed generation. Saved regular files normalize to 0600/0700;
directories remain private. This preserves `chmod u+x` and `chmod u-x` without
retaining group/other access or special mode bits. It does not resolve CPU
instruction or native-library incompatibilities in a saved binary.

## Limits and their scope

| Setting | Default | Enforcement |
| --- | --- | --- |
| `timeout_seconds` | 90 seconds (configurable 0.1–600) | Worker wall deadline; host allows 10 additional seconds for export/cleanup; the runner allows the larger of 120 seconds and the effective command limit + 30 seconds after admission. Queue waiting uses the outer turn/task budget, not this command allowance |
| Global `max_concurrent_shell_jobs` | 4 (configurable 1–32) | Separate bots may execute concurrently; each bot has one slot through workspace commit and cleanup. Additional calls wait cancellably within their owning turn |
| `output_bytes` | 1 MiB | Combined stdout/stderr ceiling; both stream logs together cannot exceed it; exceeding output stops the job |
| `memory_bytes_per_process` | 2 GiB | Hard `RLIMIT_AS` for each worker/model process; a process cannot raise the limit |
| `process_limit` | 16 | Hard `RLIMIT_NPROC`, including supervisor/Bash/threads; real fork exhaustion is tested |
| Workspace byte quota | 128 MiB by default | Hard tmpfs data-block ceiling; persistent tasks are also constrained by remaining bot quota; workspace per-file sizes are strictly validated before export; process-wide `RLIMIT_FSIZE` uses the larger of the workspace file limit and package byte budget |
| `package_bytes` | 2 GiB | One hard tmpfs data-block ceiling shared by `/packages` and `/tmp` for each unfinished human task; configurable 64 MiB–4 GiB |
| `package_entries` | 50,000 | Combined scratch tree monitored during execution, counting `/tmp` only once; configurable 1,000–100,000 |
| File/directory count | Workspace `max_files` | Checked during execution at roughly 50 ms intervals and strictly on final copy-back |
| `retention_days` | 7 days | Expired inactive logs removed when admitting a new job; metadata retains an explicit expiration marker |
| `max_jobs_per_bot` | 200 recent records | Before admitting another job, automatically archive the oldest final records outside running turns; archived history no longer consumes these slots |
| `job_storage_bytes_per_bot` | 100 MiB | New jobs reserve their complete output allowance against retained log bytes |

The global **Maximum concurrent shell jobs** setting is independent of provider
and turn concurrency. Existing installations without that field use four slots.
Waiting calls create no job or workspace lease. Admission is FIFO among eligible
bots: another queued command for a running bot does not hold up unrelated bots.
Current grants and ordinary-turn supersession are checked while waiting and again
before admission. Cancellation or shutdown removes waiting work without executing
it; the interrupted-action receipt records that it did not run. The scheduler
honors changed capacity within 250 ms without evicting admitted work. Normal
dashboard/API settings saves retain their existing affected-turn cancellation
behavior. Queue/admission events and `queue_wait_ms` distinguish waiting
from execution. Changing counts appear only in the late runtime-facts layer.

Different unfinished bot tasks can retain separate scratch allocations between commands; the per-task budget is
not a global aggregate RAM limit. Tmpfs may use swap under normal kernel policy.
No browser or host RAM-pressure management is performed by this feature. The memory field is **per process**, not an aggregate resident-memory setting: 16 × 2 GiB gives an upper bound of 32 GiB of summed process virtual address space, with separately bounded tmpfs data and kernel overhead. Linux resource-limit accounting can refuse forks earlier on a host sharing UID counts; the runner does not raise its limit to compensate. File descriptors, locked memory, core dumps and CPU seconds also receive hard limits. [Linux resource-limit definitions](https://man7.org/linux/man-pages/man2/getrlimit.2.html)

Tmpfs `size` limits data blocks; its default inode ceiling can depend on host RAM. The file-count monitor is deliberately documented as a monitored limit and can briefly overshoot between checks. This runner does not claim a hard aggregate kernel-memory or inode controller. Filesystem/process isolation and tmpfs byte bounds do not depend on that monitor. An operator requiring a single cgroup-wide resident/kernel-memory ceiling must supply an appropriately delegated container/cgroup deployment; this runner does not configure the host automatically. [Kernel tmpfs limits](https://cdn.kernel.org/doc/html/latest/filesystems/tmpfs.html)

## Job output, cancellation and restart

Entry-monitor termination reports `status: "resource_limited"`, an explanatory
`error`, and `resource_limit` with `resource: "entries"`, `path` (`/workspace`,
`/packages`, which includes `/tmp`), `limit`, and `observed_at_least`. Historical
receipts may still identify the formerly separate `/tmp` tree. The bounded scan stops
after discovering one excess entry; this is a lower bound, not a final inventory.
The worker stops descendants and discards the call's saved-workspace changes;
temporary scratch can retain partial changes until the task ends. The
receipt survives job persistence, status inspection and the failure event.
`limits` also records the effective `quota_bytes`, `max_file_bytes` and
`max_files` used for that job. Old job records remain unchanged and may lack
these additional fields. A bare SIGKILL alone does not establish an entry-limit
or out-of-memory cause; only the monitored failure receives this receipt.

Owner decision, 2026-10-15: keep existing limits, including Sun's 1,000-entry
ceiling. Correct the model-facing persistence/execute guidance and explain actual
limit failures. Static tool instructions contain no live usage counters; changing
counts, scratch capacity/usage and attachment preparation state stay in tool
results or the existing late runtime-facts layer after conversation/tool history,
preserving prompt ordering
and avoiding recurring prefix changes. Updating static guidance changes the
prefix once on rollout; it is not a zero-invalidation deployment.

`run` waits for bounded completion and returns a job ID, exit code, status, elapsed seconds, saved working directory, commit state, measured stdout/stderr byte sizes and small previews. `cd` persists as a persistent task's next working directory through Bash's exit hook and a bounded pipe, independent of writable scratch files; disposable calls always start fresh. Explicit `cwd` selects a workspace-relative directory. A script replacing the exit hook can leave the prior directory in effect; inspect the returned `cwd`. Saved-tree link/special-file errors identify the rejected relative path and explain that the call's saved changes were discarded.

`status`, `read` and `cancel` require the original bot, channel **and turn** plus current grants. Persistent workspace files can be reopened in later authorized turns. The authenticated dashboard may inspect retained jobs with their recorded ownership. `read` takes `stream`, byte `offset` and `limit` up to 8,000 bytes. Its `start`, exclusive `end`, `total_bytes` and `next_offset` are stable byte coordinates. `text` uses replacement characters if a byte page splits UTF-8; `data_base64` preserves exact bytes. Follow `next_offset` to reconstruct a stream without loss. EOF returns an empty page and no next offset. Output and retrieved files are untrusted source content.

Cancellation first closes the supervisor's parent-control pipe and targets namespace PID 1 through a pidfd obtained from bubblewrap's trusted startup channel. It then reaps the outer process before releasing any persistent workspace lease. Killing namespace init also causes the kernel to kill all remaining namespace members. The control pipe and parent-death handling cover an abrupt application SIGKILL, including during setup; no model job survives as a detached service. Shutdown waits for actual cleanup. [Linux PID namespace termination](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)

On restart, persisted `running` jobs become `interrupted`, stale staging directories are removed, and commands are never replayed. Persisted PIDs are never signalled during recovery. A crash during final atomic workspace commit can leave the committed files ahead of job metadata; inspect the workspace and retained evidence before retrying an irreversible transformation. No network or publication side effect is authorized through this runner.

After acquiring the exclusive application runtime lock, startup removes only
empty stale `scratch-*` mountpoint directories. It neither follows links nor
recurses into nonempty directories. Constructing another runner does not perform
this sweep. The existing recursive entry monitor remains unchanged: replacing it
with inode counts or a mount inode quota would change what the entry limit means.

Jobs live under external `$HORTATOR_DATA_DIR/jobs/`, with 0700 directories and 0600 metadata/logs. Backup and restore this directory with workspaces and the database. Stop the runtime for a consistent complete snapshot. Active jobs and outputs referenced by a running turn are protected from retention cleanup. At the recent-record count limit, admission automatically archives the oldest eligible metadata without deleting history. See the archival policy below. Missing/expired IDs produce a useful error. Never put these stores in Git.

## Automatic archival and longer commands

`max_jobs_per_bot` is a recent-record limit, not a lifetime execution budget. When admission needs a slot, the oldest final records outside active jobs/running turns move atomically from `jobs/<job_id>/meta.json` to private `jobs/archive/<job_id>.json`. This includes succeeded, failed, cancelled, interrupted and other terminal jobs. IDs, ownership, statuses and original metadata survive; `job.archived` records the transition at INFO. A crash leaves the complete metadata in one location, and subsequent lookup discovers it there. No archive count ceiling is imposed. If every candidate is still protected, admission gives the counts and reason without starting a command.

Archived metadata stays indefinitely, is included in normal `jobs/` backups, and resolves through existing job/output inspection by ID even outside the recent list. Recent lists include the archive marker. Original stdout/stderr stay at `jobs/<job_id>/` until the configured log retention makes them eligible for removal on a subsequent admission for that bot. After those logs expire, only the archived metadata remains. Archival does not reset age, discard unexpired logs, change workspace files or clear database/trajectory evidence. The independent output-byte quota counts both recent and archived retained logs; archival cannot bypass it. Empty expired archive log directories are removed so ordinary admissions do not rescan lifetime archive metadata. Full historical enumeration happens only on owner inspection.

In **Plugins → Isolated Bash → Working limits**, edit **Recent jobs per bot (older records archived)**, **Job log retention (days)** and **Command time limit (seconds)**. Per-bot `plugin_config.shell` overrides remain available. The command limit accepts up to 600 seconds; existing settings/default 90 are preserved. The model's tool schema enforces its effective configured ceiling, while the
late runtime facts report the current number, and a call can only shorten it. The runner starts the matching command/export deadline after queue admission; read/status and other tools keep the registry's 120-second deadline. This does not extend the parent work-task or slash deadline, and operator cancellation still cancels/joins the sandbox.

Archives are runtime data outside source Git. Older code will not inspect the new archive directory; keep matching code/data snapshots when rolling back. There is no database schema migration or automatic removal of audit history. Monitor disk usage separately; record archival solves count exhaustion, not disk exhaustion.

## Dated local verification

The focused suite executes actual bubblewrap subprocesses; it is skipped with a stated readiness reason on unsupported hosts. On the host above it verifies real scripts/pipelines, working-directory persistence, nonzero exits, concurrent byte-accurate output paging, timeout with closed output pipes, immediate startup cancellation, task cancellation, shutdown lease release, detached-child cleanup, process/address-space/tmpfs limits, unsafe-copyback rejection and bot/channel/turn ownership. Separate controller processes are SIGKILLed during setup and execution; pidfds prove namespace init exits promptly and subsequent recovery records interruption. Sentinel files, synthetic private environment values and a temporary loopback listener test the boundary without reading real secrets or contacting production.

The full branch's integration verification also exercises an image larger than 8 MiB through observed attachment import, real isolated Pillow compression, current-turn artifact export and the existing mocked Discord transport. See [VERIFICATION.md](VERIFICATION.md) for final commands/counts. These local checks do not establish live provider/Discord acceptance, production deployment or remote site delivery.

## Owner decisions (preserved from AGENTS, 2026-09-23)

The date marks relocation of standing instructions, not a new product decision.
Existing decision dates and qualifications below remain authoritative.

- Shell job count is a recent-record budget, never a lifetime call limit. Automatically archive oldest terminal records outside running turns; retain metadata/IDs and enforce existing scopes for reads, including archived jobs. Preserve output retention/byte quotas, active references, workspace files, audit evidence and backups. Do not solve count exhaustion by raising configured quotas. Command limits accept up to 600 seconds (default 90); keep model schema and registry timeout aligned with the effective per-bot limit, without extending parent task/platform deadlines. See [SHELL_RUNNER.md](SHELL_RUNNER.md).

## Owner decision — 2026-10-09

Keep shell execution independent from persistent workspaces. A shell-only grant must support one-off curl/inline Python/POST commands with disposable files using the existing supplied tools. An explicit task opts into persistent storage and its existing workspace grant. No bot grants, networking boundary or package/toolchain selection change automatically.

## Owner decision — 2026-10-18

Keep scratch RAM-backed and extend it through the human task, including qualifying
follow-ups, using the existing configurable package allowance shared by `/tmp`
and `/packages`. Preserve per-call process cleanup and saved-workspace quotas.
Do not introduce browser/RAM-pressure management or idle eviction. Give every
storage mode the same installation/environment instructions and place changing
limits, usage and attachment preparation state in the late runtime facts.
The requested external audit is owner-dispatched; its digest records remaining
questions rather than treating local tests as live provider or Discord proof.

Replace the single council-wide shell slot with one slot per bot and a configurable
council cap, default four. Automatically wait at capacity instead of returning a
busy error that spends another model round. Keep per-command limits and storage
ownership intact; queue waiting remains subject to the owning human task deadline.
