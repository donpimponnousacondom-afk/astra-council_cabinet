# Council project continuity

## Repository boundary and branch policy

- The sole working repository is `/home/codexy/codex/astra-council_cabinet`. Its top level is the application root; there is no nested `council/` directory here.
- The user explicitly retired the previous workspace for this project. Do not run Git, edit files, restore files, synchronize changes, or clean up in `/home/codexy/codex/t3-code`. Do not use that repository as a worktree or source of truth.
- Resume the current task branch recorded in the handover; `dev/initial_phase` is the development baseline. Before each new feature, fix, or development task, create a new `feat/<name>`, `hotfix/<name>`, or `dev/<name>` branch from the current branch unless the user specifies another base/name. Do not silently switch to main or start new implementation work directly on the baseline. Verify `git rev-parse --show-toplevel`, branch and working tree before Git mutations; preserve existing user changes.
- Keep new work linear: ordinary commits on the task branch, no merge commits or merges between branches. The user handles PRs and merging manually. Do not rewrite historical commits or force-push. The typing feature was explicitly requested as `feat/add_typing_indicator`, based on `dev/initial_phase` at `737c6bf`. Extra hook automation is deferred.
- **NEVER PUSH TO MAIN.** Do not commit or merge into `main`. Do not push any branch unless the user explicitly asks. The local `core.hooksPath=.githooks` setting installs an additional pre-push guard for `refs/heads/main`; do not bypass it. On a new clone, install it with `git config core.hooksPath .githooks`.
- Never stage runtime data, credentials, logs, caches, dependencies, build output or test screenshots. The migration commit removes previously tracked copies from the current tree while preserving local files. Earlier commits still contain those copies; do not rewrite history or rotate credentials without a separately scoped task.

## Read before continuing

Read `docs/SESSION_HANDOVER.md`, then `README.md` and `docs/OPERATIONS.md`. Consult `docs/PLAN.md` for the researched design, `docs/VERIFICATION.md` for actual test evidence, and `docs/API.md` / `docs/PLUGINS.md` for the control and extension contracts. Continue the existing implementation; do not replace it with a new scaffold or assume this is still a planning exercise.

Record new user preferences and operating decisions as part of the task, not just in chat. Update this file for standing agent rules, `docs/PLAN.md` for product decisions, `docs/OPERATIONS.md` for procedures, `docs/VERIFICATION.md` for evidence, and `docs/SESSION_HANDOVER.md` / `docs/CONTINUE_PROMPT.md` for the current branch, runtime, backup locations and remaining work. Mark observations with their date and distinguish implemented behavior, proposed follow-ups, mock tests and live checks. Never put secrets in these breadcrumbs.

## Shared runtime

- The user and agent share GNU Screen session `hortator`, window `dashboard`, under OS account `codexy`. Use it for foreground runtime commands so the user can observe and control the same terminal.
- Join with `screen -x hortator`; this permits concurrent attachment. Do not detach the user's display or terminate the Screen session. Ctrl-A then D detaches a viewer; Ctrl-C stops the foreground server but preserves the shell.
- Preserve this user's terminal configuration. If Screen is absent, create it using `/home/codexy/.screenrc` and `bash --login -i`; never bypass startup files with `-c /dev/null`, `--noprofile` or `--norc`. The user's Screen configuration sets 50,000 scrollback lines and `termcapinfo xterm* ti@:te@`; `.profile` loads `.bashrc` for aliases and PS1. Inspect with `screen -ls` and process/log state; avoid `screen -Q` on this host because a session disappeared during such a query (causation is unconfirmed).
- The shell working directory and Screen default directory must point into this repository. Persistent data belongs outside Git at `/home/codexy/.local/share/hortator`; the log is `/home/codexy/.local/share/hortator/logs/hortator.screen.log`. Never put live runtime data back under the checkout. Inspect Screen and foreground process state before sending input; do not interrupt an unrelated user command.
- Start with `/home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000`. This launcher lives outside the repository, loads `/home/codexy/.config/hortator/runtime.env`, changes to this root, and runs the selected branch with the external `HORTATOR_DATA_DIR`. When using `uv run hortator` directly, source that environment file first, including on older branches. One worker / one runtime only. Keep it running in Screen after the turn when it was running or the task requested startup. Do not create a second unsupervised API process.
- The production dashboard and API share port 8000. Do not use the legacy Vite process on port 5173 as evidence of the current build. Rebuild `web/dist` when UI source changes; new routes/static mounts may require a server restart.
- Read-only diagnostics and repository edits may use ordinary tools. Runtime start/stop/restart commands belong in the shared Screen session. Keep the user informed of those changes.

## Product constraints

- Every bot is a distinct Discord application. Provider transport, reusable model profiles, bot personality/capabilities, and scoped memory remain separate.
- Discord owner is the immutable snowflake `1482143139828596916` (`.normal.man.`, The Boss). Only that genuine human identity may command or converse with Hortator. Keep deterministic commands outside model execution; model inspection stays read-only. Credentials are dashboard-only.
- Preserve arbitrary vendor parameter JSON, independent cadence/cooldowns, intentional silence, bounded tools/compaction, durable trajectories and explicit delivery uncertainty. Do not publish provider reasoning fields to Discord.
- Follow the existing test and build commands. Run checks appropriate to changes; never claim live Discord/provider validation from mocked tests. Do not expose secret values in tool output, documentation or commits.
