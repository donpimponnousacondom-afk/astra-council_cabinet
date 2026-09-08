# Extending the plugin registry

Plugins are ordinary installed Python packages with an `hortator.plugins` entry point. The runtime loads these trusted local packages at startup; it never installs or executes code named by a model or Discord message.

The built-in owner-only `council_inspect` accepts `resource: "version"` to read the same startup-captured source metadata as `!version` and `/api/version`. It requires Hortator's enabled grant and a runtime-verified owner context. The tool performs no Git mutation, configuration write or provider call. Source identity remains fixed for the running server.

Example package metadata:

```toml
[project.entry-points."hortator.plugins"]
local_clock = "my_clock:register"
```

Example `my_clock.py`:

```python
from datetime import datetime, timezone
from hortator.plugins import PluginSpec, schema

async def clock(arguments, context, configuration, api_key):
    # context has bot, channel_id, turn_id, and a runtime-verified owner flag.
    # API keys are resolved by the registry; never put them in tool results.
    return {"utc": datetime.now(timezone.utc).isoformat()}

def register(registry):
    registry.register(PluginSpec(
        id="clock",
        name="Clock",
        description="Read the current UTC time.",
        parameters=schema({}),
        handler=clock,
        defaults={},
    ))
```

Install the package in the same environment, restart the runtime, enable it in **Plugins**, and grant `clock` to the desired bots. New registry entries are seeded disabled unless they are the explicitly chosen built-ins. Duplicate/reserved IDs are rejected. Tool schemas use the standard OpenAI function-call envelope.

The registry validates arguments, checks global and per-bot authorization at execution time, enforces a 120-second tool deadline, bounds serialized results, records start/result/failure/cancellation, resolves per-bot credential overrides, and redacts known secret values. Bot-level tool-round and call limits are enforced separately in the council engine.

Plugin configuration is shallow merged: built-in defaults → global plugin config → this bot's plugin config. A nested object such as `request_json` is replaced as a unit, allowing exact vendor control. Secrets are resolved separately: `bot/{id}/plugin:{name}` first, then `plugin/{name}/api_key`.

The `memory` implementation demonstrates scoped storage. Media artifacts are written with a generated safe filename and bot/turn ownership. `council_speak.artifact_ids` can reference only artifacts from its current bot and turn. The `web_fetch` implementation resolves/validates actual socket DNS answers and rejects private/reserved addresses, credentials in URLs and nonstandard ports; redirects are validated again. Operator-configured provider/plugin endpoints may point to trusted local servers, because these URLs are configuration rather than model-supplied arguments.

Installed plugins run in the Python process and are **trusted code**, not an OS sandbox. A malicious installed package can access that process's permissions. Do not mistake per-bot capability checks for isolation against hostile Python code. The optional `workspace` built-in exposes only owned bot/channel/task files. The separately granted `shell` built-in executes model commands through the documented Linux namespace/seccomp boundary, never as unrestricted host Bash. No built-in grants credential reads or administration writes. Installed Python plugins remain trusted host code; this shell boundary does not sandbox arbitrary entry-point packages.

## Shared model-facing contract

All registered tools receive the common [empty-call discovery and complete validation feedback](TOOLS.md) behavior. Supply the full action schema, including conditional required fields; `{}` is intercepted centrally before credentials or side effects. Never introduce a first-error-only parser.

The built-in keyless `document_site` pack exposes start/list/read/write/import/publish/status operations under the normal global/per-bot grant system. It grants one bounded longer task on successful start, retains immutable local revisions and queues remote delivery without running a transport. See [DOCUMENTS.md](DOCUMENTS.md). Vision is a shared input pipeline, not a grantable plugin: see [VISION.md](VISION.md).

The optional keyless `workspace`/`shell` packs and extended `web_fetch` use the same registry deadline, result cap, complete errors and global/per-bot grants. Shell additionally requires the workspace grant. Configuration validation applies to effective global/bot working limits. Immutable result IDs support bounded rereads with bot/channel/turn and transitive source-grant checks. See [AGENTIC_TOOLS.md](AGENTIC_TOOLS.md), [WORKSPACES.md](WORKSPACES.md), [WEB_READING.md](WEB_READING.md) and [SHELL_RUNNER.md](SHELL_RUNNER.md).
