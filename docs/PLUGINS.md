# Extending the plugin registry

Plugins are ordinary installed Python packages with an `hortator.plugins` entry point. The runtime loads these trusted local packages at startup; it never installs or executes code named by a model or Discord message.

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

Installed plugins run in the Python process and are **trusted code**, not an OS sandbox. A malicious installed package can access that process's permissions. Do not mistake per-bot capability checks for isolation against hostile Python code. The built-ins expose no shell, unrestricted filesystem, credential read, or administration tool to a model. Use separate service/container boundaries if adding untrusted executable plugins later.
