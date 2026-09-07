# Continue this project in a new session

Open the project at `/home/codexy/codex/astra-council_cabinet`, then paste:

```text
Continue our existing Astra Council Cabinet / Hortator project from this repository. This is a session handover, not a new implementation or architecture exercise.

First read AGENTS.md, docs/SESSION_HANDOVER.md, README.md and docs/OPERATIONS.md. Consult docs/PLAN.md and docs/VERIFICATION.md for the accepted design and actual validation. Treat them as continuity from our previous session.

Work only in /home/codexy/codex/astra-council_cabinet. The current task branch is feat/add_typing_indicator, created from dev/initial_phase at 737c6bf; inspect actual Git state before changes. For each new feature, fix or development task, create a new feat/, hotfix/ or dev/ branch from the current branch unless I specify another base/name. Keep new history linear. I handle PRs and merging manually. The application is directly at this root. Completely leave /home/codexy/codex/t3-code alone: no Git, edits, cleanup, restore or synchronization there. NEVER PUSH TO MAIN. Do not push any branch unless I explicitly request it, and do not bypass the installed pre-push guard or rewrite existing history. Additional hooks are deferred.

We share GNU Screen session hortator, window dashboard, as codexy. Its current socket is /run/screen/S-codexy/387556.hortator; rediscover it with screen -ls if needed (avoid screen -Q). Use this existing session for runtime control so I see the same output. Preserve my attachment. If Screen is absent, launch with my /home/codexy/.screenrc and bash --login -i; preserve scrollback, aliases and PS1. Do not bypass startup files. The shell must point into this repository, while persistent data is outside Git at /home/codexy/.local/share/hortator and the log is /home/codexy/.local/share/hortator/logs/hortator.screen.log. Start through /home/codexy/.local/bin/hortator serve --host 127.0.0.1 --port 8000; this external launcher loads /home/codexy/.config/hortator/runtime.env and keeps every branch on the same data directory. The production dashboard/API use http://127.0.0.1:8000. Do not start a second runtime or use the legacy Vite process on port 5173.

The five-bot configuration is real and backed up outside Git under /home/codexy/.local/share/hortator-backups. Do not initialize or clear it. Read docs/CONFIGURATION_STATUS.md for the dated audit, provider-plan corrections, reasoning explanation and remaining live acceptance. Hortator's bot flag is enabled, but its new Ollama provider is disabled and needs the credential/model-ID corrections in the audit; owner commands still work. The four council bots remain disabled for me to activate. Preserve any newer dashboard changes. Record new preferences and operating decisions in the relevant design/operations/verification documents and update the handover and this prompt for future agents.

Preserve the implemented provider/profile/bot separation, arbitrary provider JSON, isolated context and memory, trajectories, plugins and exact owner security for Discord snowflake 1482143139828596916. Never print or commit credentials. Read the handover's note about copied secret files in earlier Git history before any future publication.

Start by checking this repository's branch/status and the existing Screen runtime, then give me a brief continuity/status confirmation. Continue subsequent work from the current implementation and configuration as if we were still in the same session.
```
