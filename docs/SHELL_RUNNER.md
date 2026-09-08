# Isolated Bash runner

The optional keyless `shell` plugin runs real Bash pipelines, scripts, Python and Pillow against a private bot/channel/task workspace. Enable and grant both `workspace` and `shell`, create a task with `workspace.start`, then use `shell.run`. The dashboard exposes runner readiness and the independent working limits. Existing owner identity, room policy, global enablement and bot grants still apply. See [AGENTIC_TOOLS.md](AGENTIC_TOOLS.md) for the complete workflow and [TOOLS.md](TOOLS.md) for empty-call discovery and complete argument-error feedback.

```json
{"operation":"run","task":"image-work","command":"python3 - <<'PY'\nfrom PIL import Image\nfrom pathlib import Path\np=Path('original.png')\nim=Image.open(p)\nprint(p.stat().st_size, im.size)\nim.convert('RGB').save('compressed.jpg',quality=60,optimize=True)\nprint(Path('compressed.jpg').stat().st_size)\nPY"}
```

Export `compressed.jpg` with `workspace.export`; pass its current-turn artifact ID to `council_speak`. Shell execution never sends to Discord or publishes sites. Imported originals are protected during workspace copy-back: changing or deleting an original rejects the entire output snapshot. Image intake remains 20 MiB; the independent attachment export ceiling is 8,000,000 bytes.

## Operator requirements and readiness

Run the application under a non-root Linux account. Provide bubblewrap with `--size`, `--disable-userns`, `--assert-userns-disabled` and `--as-pid-1`, Linux user/PID/mount/network namespaces and pidfd support, `libseccomp.so.2`, Bash and the application's installed Python/Pillow. The application never installs packages, changes kernel policy or elevates privileges. An operator supplies these dependencies through the host's normal package/image management. Readiness executes a short real isolated Python/Pillow probe, reports the selected tools and gives actionable failures. Unsupported hosts fail closed; there is no host-shell fallback.

The tested host on 2026-09-08 supplied bubblewrap 0.12.0 and working unprivileged namespaces. Its cgroup filesystem was read-only, so this implementation uses hard resource limits and bounded tmpfs mounts instead of claiming a delegated cgroup memory controller.

The fixed toolchain selection contains Bash/sh, Python/Pillow and installed versions of `env`, `ls`, `stat`, `cat`, `head`, `tail`, `wc`, `du`, `cp`, `mv`, `mkdir`, `rm`, `rmdir`, `touch`, `find`, `grep`, `sed`, `awk`, `sort`, `uniq`, `cut`, `tr`, `xargs`, `tee`, `date`, `sleep`, `printf`, `basename`, `dirname`, `realpath`, `sha256sum`, `base64`, `od`, `file`, `gzip`, `gunzip`, `tar`, `zip`, `unzip`, `timeout`, `true` and `false`. Readiness lists what is actually installed. Individual executables, required shared libraries, Python's standard library and Pillow are mounted read-only; the entire host `/usr`, virtual environment, repository and home are never mounted. `pip` and `ensurepip` are excluded; no package manager, sudo, deployment client or service manager is supplied. GUI/Tk is not part of the supported toolchain.

## Execution boundary

Bubblewrap creates private user, PID, mount, network, IPC and UTS namespaces. The sandbox UID/GID is 1000 with all capabilities removed, `no_new_privs`, further user namespaces disabled and a read-only root. Only the current task's input snapshot is visible read-only at `/source`; the writable `/workspace` is a finite tmpfs copy. `/tmp` has its own 16 MiB tmpfs. The minimal environment contains a fixed tool path, workspace home, temporary directory, C locale and runtime library settings. Bash uses `--noprofile --norc`; the human Screen shell retains its own normal configuration. This follows bubblewrap's documented filesystem and namespace controls. [Bubblewrap manual](https://raw.githubusercontent.com/containers/bubblewrap/main/bwrap.xml)

The sandbox has no host home, application checkout, runtime database, vault, master key, bootstrap password, ambient proxy variables, SSH/Screen sockets, Docker socket or other workspaces. Its `/proc` describes only the new PID namespace and is mounted read-only. Seccomp additionally denies all socket creation, further namespace/mount operations, ptrace/process-memory access, memfd/SysV shared-memory creation, kernel keyrings and related unsupported kernel interfaces. Retrieval remains with the authorized fetch and attachment-import tools. The supervisor is nondumpable; model children cannot open its `/proc/1/fd` transport handles. [libseccomp rule API](https://libseccomp.readthedocs.io/en/latest/man/man3/seccomp_rule_add.3/)

The trusted supervisor is namespace PID 1. It reads stdout and stderr concurrently, kills remaining descendants (including detached sessions), validates the complete output tree and sends bounded regular-file frames to the host. The host independently enforces paths, counts and byte sizes, then asks the workspace store to commit a validated generation atomically. There is no writable host mount accessible to the model. Successful valid changes and valid changes from nonzero command exits persist. Timeout, cancellation, output-limit termination, invalid output trees and interrupted execution discard the sandbox snapshot. A script can therefore have run even when its workspace changes are not committed; check `workspace_committed` and the reported status.

## Limits and their scope

| Setting | Default | Enforcement |
| --- | --- | --- |
| `timeout_seconds` | 90 seconds | Worker wall deadline; host allows at most 10 additional seconds for setup/export before terminating the namespace; registry's 120-second deadline remains |
| `output_bytes` | 1 MiB | Combined stdout/stderr ceiling; both stream logs together cannot exceed it; exceeding output stops the job |
| `memory_bytes_per_process` | 256 MiB | Hard `RLIMIT_AS` for each worker/model process; a process cannot raise the limit |
| `process_limit` | 12 | Hard `RLIMIT_NPROC`, including supervisor/Bash/threads; real fork exhaustion is tested |
| Workspace byte quota | 128 MiB by default | Hard tmpfs data-block ceiling, constrained by remaining bot quota; per-file hard `RLIMIT_FSIZE` uses workspace `max_file_bytes` |
| Temporary data | 16 MiB | Separate hard tmpfs data-block ceiling |
| File/directory count | Workspace `max_files` | Checked during execution at roughly 50 ms intervals and strictly on final copy-back |
| `retention_days` | 7 days | Expired inactive logs removed when admitting a new job; metadata retains an explicit expiration marker |
| `max_jobs_per_bot` | 200 | Admission stops at the saved job-count ceiling; no silent deletion of job history |
| `job_storage_bytes_per_bot` | 100 MiB | New jobs reserve their complete output allowance against retained log bytes |

There is one concurrent model shell job globally. The memory field is **per process**, not an aggregate resident-memory setting: 12 × 256 MiB gives an upper bound of 3 GiB of summed process virtual address space, with separately bounded tmpfs data and kernel overhead. Linux resource-limit accounting can refuse forks earlier on a host sharing UID counts; the runner does not raise its limit to compensate. File descriptors, locked memory, core dumps and CPU seconds also receive hard limits. [Linux resource-limit definitions](https://man7.org/linux/man-pages/man2/getrlimit.2.html)

Tmpfs `size` limits data blocks; its default inode ceiling can depend on host RAM. The file-count monitor is deliberately documented as a monitored limit and can briefly overshoot between checks. This runner does not claim a hard aggregate kernel-memory or inode controller. Filesystem/process/network isolation and tmpfs byte bounds do not depend on that monitor. An operator requiring a single cgroup-wide resident/kernel-memory ceiling must supply an appropriately delegated container/cgroup deployment; this runner does not configure the host automatically. [Kernel tmpfs limits](https://cdn.kernel.org/doc/html/latest/filesystems/tmpfs.html)

## Job output, cancellation and restart

`run` waits for bounded completion and returns a job ID, exit code, status, elapsed seconds, saved working directory, commit state, measured stdout/stderr byte sizes and small previews. `cd` persists as the task's next working directory through Bash's exit hook. Explicit `cwd` selects a workspace-relative directory. A script replacing the exit hook can leave the prior directory in effect; inspect the returned `cwd`.

`status`, `read` and `cancel` require the original bot, channel **and turn** plus current grants. Persistent workspace files can be reopened in later authorized turns. The authenticated dashboard may inspect retained jobs with their recorded ownership. `read` takes `stream`, byte `offset` and `limit` up to 8,000 bytes. Its `start`, exclusive `end`, `total_bytes` and `next_offset` are stable byte coordinates. `text` uses replacement characters if a byte page splits UTF-8; `data_base64` preserves exact bytes. Follow `next_offset` to reconstruct a stream without loss. EOF returns an empty page and no next offset. Output and retrieved files are untrusted source content.

Cancellation first closes the supervisor's parent-control pipe and targets namespace PID 1 through a pidfd obtained from bubblewrap's trusted startup channel. It then reaps the outer process before releasing the workspace lease. Killing namespace init also causes the kernel to kill all remaining namespace members. The control pipe and parent-death handling cover an abrupt application SIGKILL, including during setup; no model job survives as a detached service. Shutdown waits for actual cleanup. [Linux PID namespace termination](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)

On restart, persisted `running` jobs become `interrupted`, stale staging directories are removed, and commands are never replayed. Persisted PIDs are never signalled during recovery. A crash during final atomic workspace commit can leave the committed files ahead of job metadata; inspect the workspace and retained evidence before retrying an irreversible transformation. No network or publication side effect is authorized through this runner.

Jobs live under external `$HORTATOR_DATA_DIR/jobs/`, with 0700 directories and 0600 metadata/logs. Backup and restore this directory with workspaces and the database. Stop the runtime for a consistent complete snapshot. Active jobs and outputs referenced by a running turn are protected from retention cleanup. At a count quota, the operator may archive old completed job directories after stopping the runtime and verifying that no running turn refers to them; deleting logs alone does not free the saved-metadata count. Missing/expired IDs produce a useful error. Never put these stores in Git.

## Dated local verification

The focused suite executes actual bubblewrap subprocesses; it is skipped with a stated readiness reason on unsupported hosts. On the host above it verifies real scripts/pipelines, working-directory persistence, nonzero exits, concurrent byte-accurate output paging, timeout with closed output pipes, immediate startup cancellation, task cancellation, shutdown lease release, detached-child cleanup, process/address-space/tmpfs limits, unsafe-copyback rejection and bot/channel/turn ownership. Separate controller processes are SIGKILLed during setup and execution; pidfds prove namespace init exits promptly and subsequent recovery records interruption. Sentinel files, synthetic private environment values and a temporary loopback listener test the boundary without reading real secrets or contacting production.

The full branch's integration verification also exercises an image larger than 8 MiB through observed attachment import, real isolated Pillow compression, current-turn artifact export and the existing mocked Discord transport. See [VERIFICATION.md](VERIFICATION.md) for final commands/counts. These local checks do not establish live provider/Discord acceptance, production deployment or remote site delivery.
