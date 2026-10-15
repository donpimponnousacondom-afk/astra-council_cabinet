---
name: council-plugin-author
description: Build or extend Hortator council Python plugins in astra-council_cabinet, using its registry, grants, lifecycle and test contracts. Use for new council tools, installed plugin packages or plugin capability hooks; not for installing Codex marketplace plugins.
---

# Council plugin author

This skill is versioned in `skills/council-plugin-author` in the Hortator project.
The canonical workspace is `/home/codexy/codex/astra-council_cabinet`. Repository
paths below refer to the current task checkout of that project. Resolve any
installed skill symlink before locating its bundled assets.

## Establish scope and ownership

Read `AGENTS.md`, `README.md`, `docs/OPERATIONS.md`, `docs/PLUGINS.md` and the
relevant feature contract before editing. Inspect the actual branch, working
tree and open PR. Coordinate branch and file ownership with the integrating
agent. Different branches in one working directory do not isolate simultaneous
edits; parallel writers need agreed separate checkouts or disjoint files without
checkout changes. Preserve the shared Screen runtime and the repository boundary
in AGENTS. Do not use the retired t3-code repository.

Establish what the plugin should do, which bots may use it, whether it needs
credentials, persistent state or asynchronous completion, and whether the user
wants an installed package or an in-tree capability. Implement the smallest
appropriate extension; a normal tool needs no engine changes. Follow the owner's
existing security-policy and publication authority in AGENTS; a development task
does not itself grant live bot enablement or a new security policy.

## Choose an existing extension point

- Ordinary tools: `hortator/plugins.py`, especially `PluginSpec`, `ToolContext`,
  `Registry.register`, `schemas` and `call`; use the `hortator.plugins` entry point
  for an installed package.
- Configuration/context/catalog hooks: `docs/PLUGINS.md` and
  `tests/test_plugin_hooks.py`. Keep global configuration, per-bot overrides and
  registry-owned credentials separate. A generic plugin uses the existing
  plugin configuration editor; a dedicated UI is additional scoped work.
- Long-running independent work: `docs/BACKGROUND_JOBS.md` and
  `docs/CONCURRENCY.md`. Register handlers during `bind`; submit work through the
  existing job service and Kernel lifetime. Binding itself starts no tasks or I/O.
- Input/presentation capabilities: `model_tool=False` and the relevant audio,
  slash, Engram or reasoning-viewer contract. Do not advertise an artificial tool
  merely to obtain a capability hook.

## Start from the working package

[assets/clock-plugin](assets/clock-plugin) contains a complete small package:
`pyproject.toml` declares its entry point, and `src/council_clock/__init__.py`
registers the `clock` tool. It is example code, not a plugin automatically loaded
by the application. Rename its distribution, module, entry point and tool ID for
a new plugin. The host provides `hortator`; keep the package in the same supported
Python environment when integrating it.

Its action is `{"operation":"now"}`. **`{}` is discovery only**, intercepted by
the registry before handler execution. Supply a nonempty action schema even for
a no-argument operation. Never change the shared help convention to make a
plugin execute an empty call. `end_cycle` is an engine-specific exception, not a
pattern for plugin authors to copy. The clock is keyless and uses council-local
time with an explicit UTC offset.

Follow `docs/TOOLS.md` for complete validation feedback, stored result paging
and file preparation. Return data or owned artifacts through existing services;
ordinary public answers remain the engine's responsibility. Use trusted context
for bot/channel/turn ownership. Keep model arguments separate from operator
configuration and retain the existing grant/revocation checks.

Advertised schemas should describe stable actions. Put changing counters and
live status in results, help or `prompt_status`, as documented in PLUGINS;
unnecessary early prompt/schema changes lose cache-prefix reuse. Preserve the
user's current response protocol and tool-budget behavior.

## Verify the integration

Use the project's Python 3.14 toolchain and temporary `Kernel` fixtures with
`Kernel.lifetime()`; do not start Discord or issue paid provider calls for unit
tests. `tests/test_plugin_starter.py` loads the real bundled entry point and
checks help, execution, errors and grant revocation. `tests/test_plugins.py`
demonstrates mocked HTTP, credential overrides and owned artifacts.

Exercise the handler through `registry.call` or `call_raw`, not just directly.
Check a real action, `{}` without side effects, invalid arguments and the
relevant isolation/revocation paths. Add cancellation, failure and idempotence
cases when the plugin performs asynchronous or external effects. Keep SQLite
and vault access on their owner thread; use the concurrency contract for work
that would block the event loop.

For this starter, run from the project root:

```bash
uv run pytest -q tests/test_plugin_starter.py tests/test_plugin_hooks.py
uv run ruff check tests/test_plugin_starter.py skills/council-plugin-author/assets/clock-plugin/src
uv run ruff format --check tests/test_plugin_starter.py skills/council-plugin-author/assets/clock-plugin/src
```

Adapt the tests to the actual plugin. Validate any package build/discovery in an
isolated temporary environment before proposing installation. Installing into
the shared `.venv` changes the runtime's discovered plugin set; follow the
project's locked dependency/integration workflow when that installation is part
of the authorized task. Merely reading or using this skill installs no plugin.

## Publish and hand off

Keep user-facing behavior and setup in the appropriate feature document; link
the plugin from `docs/PLUGINS.md`. Record measured checks and their limits in
`docs/VERIFICATION.md`. Do not copy running configuration or write rolling
session handovers into these documents.

Follow AGENTS and `docs/OPERATIONS.md` for task-branch publication and independent
Luna/Sol review of the open PR. The installed `council-pr-babysitter` skill gives
the detailed review workflow on this host; the repository procedure remains
available to agents without that local skill. Coordinate one integration owner
for publication, gated squash merge and any shared-runtime deployment. Do not
change branches or restart the shared server behind another agent's task.
Report the PR/commit, verified behavior, remaining integration work and
publication debt; distinguish a packaged example from an enabled live plugin.
