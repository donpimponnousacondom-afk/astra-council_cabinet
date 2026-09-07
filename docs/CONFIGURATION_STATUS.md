# Configuration audit — 2026-09-07

**Superseded configuration findings:** later on 2026-09-07, the user confirmed that Hortator's new provider and Kimi K3 work, context configuration is resolved, and Qwen concurrency is set to two requests for four units. The user also confirmed the typing indicator works live. Do not reapply the earlier credential/model/context corrections below: they describe the earlier snapshot, not the current setup. Configuration is considered complete by the user; full council activation/acceptance is still theirs to perform. The current verified backup is `20260907T124253Z-before-refinements`. See [SESSION_HANDOVER.md](SESSION_HANDOVER.md) for current state.

**Implemented after this audit:** `feat/council_refinements` adds visible reasoning controls, accurate summaries (including false/nested fields), compaction reasoning previews, output-limit labels, catalog-only discovery wording, a 30-second reconnect notification grace period with source/duration metadata, and cooperative SSE shutdown. See [OPERATIONS.md](OPERATIONS.md#reasoning-controls) for current procedures. The diagnosis below explains the previous behavior; it is retained as historical evidence.

This records the user's configured external runtime during `feat/add_typing_indicator`, including dashboard changes captured in the final snapshot at **14:17 Europe/Madrid**. It is a dated observation, not a configuration template. Preserve the live database and read current dashboard state when resuming. The agent changed no activation flags, provider parameters, profile JSON, plugin grants or credentials. The user enabled Hortator, raised Featherless's timeout, moved Hortator to a new Ollama profile, then disabled the Ollama provider during the session; the four council bots remained disabled for the user to activate.

## Historical readiness and settings

All five applications passed authenticated, read-only Discord checks: genuine bot identity, matching distinct application ID and stored user ID, Message Content Intent, guild membership, and effective permissions after channel overwrites. No member was pending or timed out. All required permissions were present and none had Administrator. The dashboard's readiness list was empty for every bot. Hortator's reporting channel is separate from the council room, and its gateway was online with about 96 ms heartbeat latency at inspection.

| Bot | Model profile / provider model | Model enabled | Per-bot plugin grants |
| --- | --- | --- | --- |
| Ada | `Qwen38-27B-abliterated` / `huihui-ai/Huihui-Qwen3.8-27B-abliterated` | No | None |
| Socrates | Same Qwen profile | No | None |
| Dirac | Same Qwen profile | No | `memory` |
| Curie | Same Qwen profile | No | `memory` |
| Hortator | `hortator_ollama` / Ollama `kimi-k3:cloud` (catalog correction below) | Bot flag yes; provider paused | `council_inspect`, `memory`, `web_fetch` |

Hortator's Discord control connection remains online while its model is paused by the disabled Ollama provider. After correcting its credential and model ID below, enable that provider to resume model-assisted answers. Deterministic owner commands remain available independently.

The Discord setup passes, but **three settings still need attention**:

1. **Providers → featherless → concurrency:** the plan provides **four concurrent units** and each configured Qwen request costs two. The local setting is four requests, so use **two concurrent requests** for the current Qwen-only pool. Overlapping requests above the unit budget can receive HTTP 429. The runtime counts requests without model weights. If Kimi is ever moved back to Featherless, it costs four units itself; a mixed Qwen/Kimi pool should start at **one** local concurrent request. [Featherless concurrency rules](https://featherless.ai/docs/concurrency-limits).
2. **Providers → ollama → credential:** the saved API-key field contains the configured endpoint URL, so it is not a usable cloud credential. Enter the real cloud API key from the Ollama account in the dashboard credential box. The public model catalog returns HTTP 200 without validating that field; no successful Ollama authentication or generation is established. The saved field's contents are not reproduced here. [Ollama cloud authentication](https://docs.ollama.com/cloud#authentication).
3. **Model profiles → hortator_ollama → Exact model identifier:** the provider's `/models` lookup listed **`kimi-k3`**, whereas the profile contains **`kimi-k3:cloud`**, which was not listed. Use the exact catalog ID **`kimi-k3`** for this direct endpoint. Ollama distinguishes local cloud-model aliases from direct cloud API model identifiers. No completion was sent to test alias acceptance. [Ollama cloud API](https://docs.ollama.com/cloud#cloud-api-access).

Featherless model discovery and plan lookup returned HTTP 200. Qwen is available on the plan, advertises native tools, and its 32,768-token context window matches the plan cap. The original Featherless `hortator` profile remains at 262,144 but is now unused; reduce it to **32,768** before reusing it on this plan. That earlier cap finding does not apply to the new Ollama profile. The plan ceiling can be smaller than a model's advertised window. [Featherless plan API](https://featherless.ai/docs/api-reference-plan). Ollama's public Kimi K3 card advertises tools, thinking and a one-million-token model context; the new profile's 262,144 is below that advertised window, but no account-specific context or generation test was performed. [Ollama Kimi K3](https://ollama.com/library/kimi-k3:cloud).

Global scheduling is enabled. The unused disabled OpenRouter provider and placeholder profile do not block the configured bots. The corrections above were reported, **not applied**, so the backups retain the user's exact settings. Readiness currently checks required references/credentials, not remote plan ceilings, catalog membership or weighted concurrency.

Hortator has real successful Kimi requests and deliveries through its former Featherless profile. The new Ollama profile and ordinary bots have not all completed live model/tool/delivery acceptance: Dirac's recorded model attempt was cancelled. This audit sent no Discord messages, typing pulses or paid completion probes and did not enable council bots. Once the relevant settings are corrected, enable one council bot, post a topic, and inspect its actual request plus reply/silence before enabling the remaining three. Test Hortator with a question from The Boss in its reporting channel or owner DM. The typing feature is exercised by those actual turns.

## Optional capabilities at the earlier snapshot

Memory, web fetch and read-only council inspection are globally enabled, but each bot also needs its own grant as shown above. Ada and Socrates currently have no plugin grants; normal conversation still has scoped transcript/context, but they cannot call the memory plugin. Web search is globally enabled with no Brave key and no bot grants, so it is not ready for use. Image generation and TTS are disabled. Configure each desired plugin's endpoint/credential in the dashboard and grant it to the intended bots before claiming that optional capability is operational. These plugins do not require broader Discord OAuth scopes; media delivery uses the existing Attach Files permission. See [PLUGINS.md](PLUGINS.md) for each plugin's contract.

## Discord Portal and OAuth checklist

The existing five applications already pass the permission checks. When reinstalling one or adding another:

1. Create a distinct application in the [Developer Portal](https://discord.com/developers/applications). Copy its Application ID. On **Bot**, obtain its **bot token** and enable **Message Content Intent**. This implementation requests guild/message events; it does not need the privileged member-list or presence intents.
2. Use a **Guild Install**. The runtime's generated invitation requests OAuth scope **`bot`**. Commands currently use `!` prefixes, so no extra application-command scope is needed by this implementation. Leave user-account scopes such as `identify`, `email`, `messages.read`, RPC and `guilds.join` unselected. The normal bot install flow needs no redirect URI, Client Secret or authorization-code exchange; leave **Requires OAuth2 Code Grant** off. Public Bot may remain off for owner-only installation. [Discord bot authorization](https://docs.discord.com/developers/topics/oauth2#bot-authorization-flow).
3. Grant **View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Create Public Threads, Send Messages in Threads**. The dashboard generates the combined permission integer. Do not grant Administrator. Check channel/category overwrites too; private thread membership may need separate setup.
4. In **Bots → Discord**, save the application ID and bot token through the authenticated dashboard. Use that bot's **Invite bot** link, select the server and authorize it. Repeat for each identity. The OAuth2 Client Secret shown in the reference screenshot is a different credential and is not used by this runtime.
5. Assign the council room and model profile, set Hortator's separate reporting guild/channel in Council settings, and choose per-bot tools and cadence. Keep secrets out of chat and configuration JSON. Enable bots after readiness and provider-plan checks.

## Reasoning configuration

There is **no provider-level reasoning field**. Providers own HTTP transport, authentication, concurrency and timeouts. Model profiles own exact generation JSON. The location is **Model profiles → Edit → Advanced · exact request JSON → Model parameters** (`request_json`), with separate compaction overrides in the same editor.

At audit, the shared Qwen profile contained `{"max_tokens":4096}` and both the original Hortator profile and new `hortator_ollama` profile contained `{}`. None sends a reasoning override. “Provider default” on the model card is derived display text: the detector checks `reasoning_effort`, `reasoning.effort`, and a truthy `thinking` field. It does not describe a saved selection or a configurable provider default, and can miss vendor structures such as `chat_template_kwargs`. The server/model chooses behavior when an option is omitted.

Featherless documents a model-dependent toggle such as the following Qwen-profile JSON, retaining its current output cap:

```json
{
  "max_tokens": 4096,
  "chat_template_kwargs": { "enable_thinking": false }
}
```

`true` requests thinking; `false` requests non-thinking mode only where the template supports switching. Unsupported options can have no effect. The provider's current family table does not explicitly establish support for these exact newer Qwen3.8/Kimi-K3 IDs. Validate their templates before asserting an effective mode. This example was not saved. [Featherless chat template options](https://featherless.ai/docs/chat-template-kwargs).

For the new Ollama profile, its compatible endpoint documents `reasoning_effort` (for example `{"reasoning_effort":"low"}`) and `reasoning.effort`. Model support determines the actual effect; an accepted field is not proof that Kimi exposes every effort level. These go in the same Model parameters editor, not Featherless's `chat_template_kwargs`. No override was applied or completion-tested. [Ollama compatibility fields](https://docs.ollama.com/api/openai-compatibility#v1chatcompletions).

The requested follow-up was an accurately labelled reasoning configuration location with direct access to exact JSON and an honest “no explicit override” indication. It is now implemented on `feat/council_refinements`; the typing branch contained only this diagnosis. The controls preserve arbitrary vendor JSON and provider/profile separation without inventing an inheritance layer or an undocumented vendor default.

## Reconnect notifications and slow replies

The stored Hortator reconnect pairs were at **12:30:26**, **12:47:19**, and **13:15:27 Europe/Madrid**, returning online after approximately **0.241**, **0.244**, and **0.229 seconds** respectively. The last pair is the user's events #92/#93. These are brief gateway interruptions in the available ledger, not evidence of a continuously failed connection.

At the typing audit, `CouncilClient.on_disconnect` emitted `reconnecting` without a cause; `on_resumed`/`on_ready` emitted `online`. Thus `error: null` meant no cause was supplied, not a literal error. The data is consistent with brief gateway reconnect/resume behavior, but those stored events do not identify whether Discord or the network initiated it. No recorded pair carried the supervisor's explicit unhealthy-heartbeat message. Notifications then reported both transitions, with a five-minute per-kind/per-bot throttle. The later refinements record available causes and duration and keep brief resumes out of chat; the original root cause remains unestablished.

One actual Kimi stream failed at **120,002 ms** with `TimeoutError: Request timed out` after HTTP 200. Other successful calls lasted approximately **108.7** and **114.6 seconds**. The provider's total request timeout was **120 seconds**, so slow generation is a concrete issue separate from gateway status. During this session the user raised **Providers → featherless → Total request timeout** to **360 seconds** through the dashboard (13:59 Europe/Madrid); preserve that edit. Hortator's 16,386-token output reserve is a context allowance; its empty JSON sends no explicit generation token cap. Set a supported `max_tokens` no larger than the reserve if a provider output bound is desired. The agent did not change these settings.

The new **ollama** provider has its own **120-second** timeout. Featherless's later 360-second setting does not carry over to it; inspect and tune that provider separately if long Kimi responses need more time.

Typing provides visible activity throughout that wait. It does not reduce provider latency or prevent a timeout. The dashboard remains the source of actual progress and failures.
