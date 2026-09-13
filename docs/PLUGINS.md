# Extending the plugin registry

Plugins are ordinary installed Python packages with an `hortator.plugins` entry point. The runtime loads these trusted local packages at startup; it never installs or executes code named by a model or Discord message.

Async plugin work must follow [CONCURRENCY.md](CONCURRENCY.md): related children belong to a scoped TaskGroup, cancellation propagates after cleanup, and errors are never abandoned. Move expensive pure preparation off the event loop using detached values; keep SQLite/vault access on its owner thread. Built-in model-facing timestamp metadata uses the configured council zone, with raw content/evidence preserved. Future plugins should follow the same presentation convention.

Engine terminal/file-preparation tools remain reserved IDs rather than ordinary registry plugins. The per-bot **Capabilities → Allow intentional silence** checkbox controls `council_silence` through `bots.allow_silence` (default true). It needs no credential/global switch; false removes its schema and prevents a successful silence decision. The ordinary engine budget, complete argument feedback and active-turn cancellation still apply. Do not register a second silence plugin or silently re-enable it from an extension.

The built-in owner-only `council_inspect` accepts `resource: "version"` to read the same startup-captured source metadata as `!version` and `/api/version`. It requires Hortator's enabled grant and a runtime-verified owner context. The tool performs no Git mutation, configuration write or provider call. Source identity remains fixed for the running server.

The separate keyless owner-only `discord_send` action posts owner-requested text/current-turn artifacts as Hortator to another configured room/channel or allowed observed thread. It requires explicit global/bot grants (off on new installations). It exposes targets/send/status, uses the existing outbox/destination lock and strict reply transport, and preserves per-turn delivery-key idempotence/uncertainty. It does not change Hortator intake, impersonate the owner or replace normal assistant content. See [the tool contract](TOOLS.md) and [cross-channel operations](OPERATIONS.md#hortator-control-scope-and-cross-channel-posts).

Example package metadata:

```toml
[project.entry-points."hortator.plugins"]
local_clock = "my_clock:register"
```

Example `my_clock.py`:

```python
import time
from hortator.plugins import PluginSpec, schema
from hortator.timekeeping import council_timezone, local_timestamp


def register(registry):
    async def clock(arguments, context, configuration, api_key):
        # context has bot, channel_id, turn_id, and a verified owner flag.
        # API keys are registry-owned; never put them in tool results.
        return {"now": local_timestamp(time.time(), council_timezone(registry.store))}

    registry.register(
        PluginSpec(
            id="clock",
            name="Clock",
            description="Read the current council-local time with its UTC offset.",
            parameters=schema({}),
            handler=clock,
            defaults={},
        )
    )
```

Install the package in the same environment, restart the runtime, enable it in **Plugins**, and grant `clock` to the desired bots. New registry entries are seeded disabled unless they are the explicitly chosen built-ins. Duplicate/reserved IDs are rejected. Tool schemas use the standard OpenAI function-call envelope.

The registry validates arguments, checks global and per-bot authorization at execution time, enforces a 120-second tool deadline, bounds serialized results, records start/result/failure/cancellation, resolves per-bot credential overrides, and redacts known secret values. Bot-level tool-round and call limits are enforced separately in the council engine.

Plugin configuration is normally shallow merged: built-in defaults → global plugin config → this bot's plugin config. A nested object such as `request_json` is replaced as a unit, allowing exact vendor control. The document pack deliberately limits per-bot overrides to `local_base_url`: automatic publication, the public URL and remote SSH destination are global operator policy. Secrets are resolved separately: `bot/{id}/plugin:{name}` first, then `plugin/{name}/api_key`. Keyless built-ins do not require those fields; the publishing worker separately accesses its named vault SSH identity and never exposes it to a model.

The `memory` implementation demonstrates scoped storage. Its per-bot positive budget defaults to 48,000 characters per channel, with 5% temporary consolidation headroom. Current usage and repair instructions accompany model descriptions, results and prompts. Disabling its bot/global grant removes both its tools and automatic notes, preserving stored data. See [memory allowance and repair](TOOLS.md#private-memory-allowance-and-repair). Media artifacts are written with a generated safe filename and bot/turn ownership. `discord_attach.artifact_ids` can prepare only artifacts from its current bot and turn. This optional tool is advertised only once an owned artifact exists, consumes a normal tool round, and contains no answer text. The final answer is ordinary assistant content; only then does the runtime deliver the selected files. The `web_fetch` implementation resolves/validates actual socket DNS answers and rejects private/reserved addresses, credentials in URLs and nonstandard ports; redirects are validated again. Operator-configured provider/plugin endpoints may point to trusted local servers, because these URLs are configuration rather than model-supplied arguments.

Installed plugins run in the Python process and are **trusted code**, not an OS sandbox. A malicious installed package can access that process's permissions. Do not mistake per-bot capability checks for isolation against hostile Python code. The optional `workspace` built-in exposes only owned bot/channel/task files. The separately granted `shell` built-in executes model commands through the documented Linux namespace/seccomp boundary, never as unrestricted host Bash. No built-in grants credential reads or administration writes. Installed Python plugins remain trusted host code; this shell boundary does not sandbox arbitrary entry-point packages.

## Shared model-facing contract

The built-in `web_search` exposes Auto fallback, Brave, keyless DuckDuckGo and Both through `engine` and a per-engine `count` (1–10, default 5). Global/per-bot config validates these fields and the optional operator Brave `endpoint`; call arguments may override engine/count. Credential resolution stays in the existing vault path and the Brave key is never sent to DuckDuckGo. The plugin retains its credential box because Brave uses it; DuckDuckGo needs none. Engine events and structured partial results retain failure/provenance information; total failure includes usage and records `tool.failed`. Query-only calls remain compatible. See [WEB_SEARCH.md](WEB_SEARCH.md).

Installed descriptions for search, memory and shell supersede obsolete seeded catalog text when read through the service, without rewriting operator configuration. Schemas can include complete top-level `examples`; shared usage prefers a schema-valid example for the selected operation. Memory explicitly requires operation even when key/value look like a write; shell explicitly requires `workspace.start` before running a task. Do not weaken validation or silently create missing resources to mask model mistakes.

All registered tools receive the common [empty-call discovery and complete validation feedback](TOOLS.md) behavior. Supply the full action schema, including conditional required fields; `{}` is intercepted centrally before credentials or side effects. Never introduce a first-error-only parser.

The built-in keyless `document_site` pack exposes explicit `create`, resume-only `start`/`edit`, list/status, paged read/history, write/append/replace/restore, scoped imports and publication. `published_files` and `import_published` permit public-source replication into an explicitly created owned destination, without private-draft access. One successful create/resume may use the turn's bounded document extension. Owner IDs come from trusted context; models cannot select a destination owner, delete assets or adopt a retired bot's namespace.

With global automatic publication enabled, successful saves commit immutable revisions and queue rows together. The independent SSH worker consumes these snapshots under global operator settings and current bot/plugin grants. It creates private local/remote Git snapshots, verifies delivery receipts and exposes truthful pending/delivered states. Plugin handlers never run model-supplied remote commands. Do not reset attempted queue jobs, bypass strict host trust or treat a planned URL as delivery evidence. See [DOCUMENTS.md](DOCUMENTS.md) and [operator setup](OPERATIONS.md#automatic-remote-publishing). Vision is a shared input pipeline, not a grantable plugin: see [VISION.md](VISION.md).

The optional keyless `workspace`/`shell` packs and extended `web_fetch` use the same registry deadline, result cap, complete errors and global/per-bot grants. Shell additionally requires the workspace grant. Configuration validation applies to effective global/bot working limits. Immutable result IDs support bounded rereads with bot/channel/turn and transitive source-grant checks. See [AGENTIC_TOOLS.md](AGENTIC_TOOLS.md), [WORKSPACES.md](WORKSPACES.md), [WEB_READING.md](WEB_READING.md) and [SHELL_RUNNER.md](SHELL_RUNNER.md).

The keyless `global_memory` plugin owns a distinct table keyed only by stable bot ID and note key. Grants control both model-tool exposure and automatic cross-channel note injection; owner inspection remains available with the plugin disabled. It shares the complete usage/error and 5% memory-headroom conventions while maintaining a separate quota from channel memory. See [GLOBAL_MEMORY.md](GLOBAL_MEMORY.md).

`PluginSpec.model_tool` defaults true. Ingress-only capabilities may set it false: they remain configurable plugin grants, but are absent from all model schemas and rejected through both parsed and raw model call paths. Registering a plugin must not itself enable an existing bot or start remote application installation.
