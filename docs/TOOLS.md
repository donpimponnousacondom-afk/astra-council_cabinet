# Tool discovery, repair and task budgets

This is a deliberate model compatibility contract for **every built-in and future plugin**, including `council_speak` and `council_silence`. Keep it when changing validators or tool schemas. Small models should receive enough information to repair a call in one attempt instead of spending successive rounds discovering one missing field at a time.

## Discovery without side effects

Calling an available tool with `{}` returns `usage_only: true`, `executed: false`, the complete parameter schema, named-field convention and a concrete example. It does not invoke the handler, resolve its credential or perform its action. Authorization still applies: discovery cannot expose a disabled or ungranted tool. Empty calls consume the ordinary round/call budget so discovery cannot create an unbounded loop.

The model receives this instruction in its shared runtime prompt and each advertised tool explicitly permits the empty help call. A plugin supplies its real action schema to the registry; the common wrapper adds discovery. Do not make each plugin implement a conflicting help convention.

## Complete validation feedback

For parseable JSON, validation collects all detectable schema errors: missing required fields, wrong types, unknown fields, bounds and conditional operation requirements. The response contains all error paths/rules/messages and full usage with an example for the requested valid operation. Named JSON fields have **no positional order**. Strings are not silently converted into numbers, and a malformed call never partially invokes the handler.

Malformed JSON returns its syntax error and full usage together. Duplicate keys and non-JSON numbers such as NaN/Infinity are rejected. Field validation cannot reliably examine an unparseable document; the response explains that limitation. Handler failures also include usage, but an external failure cannot imply that no side effect occurred. Credentials are redacted before feedback reaches the model or ledger.

Terminal delivery checks collect visible-content, reply-target and artifact-reference problems together. A terminal call must be alone in its batch; mixed terminal/plugin batches or batches exceeding the call limit execute nothing and return repair information. Duplicate call IDs fail the turn because results cannot be associated unambiguously. Providers and plugins can still reject semantically invalid values or fail externally; this design gives the model complete available feedback, not a guarantee that its next attempt succeeds.

Plugin authors must express all independently checkable argument requirements in JSON Schema, including operation-specific `if`/`then` requirements. When checks require runtime state, aggregate independent failures where practical. Preserve the central validation path rather than adding first-error parsers or coercion. Put examples on unusual constrained fields; generated examples are illustrative and do not invent valid account/resource IDs. Tools installed through Python entry points are trusted host code, not a process sandbox.

## Normal and extended turns

**Tool work rounds** limits the number of model/tool cycles. **Tool calls allowed in each round** limits how many calls one model response may request. Calls currently execute sequentially; this setting is independent of provider request concurrency. A final response opportunity follows the normal tool rounds, and all retries/discovery remain bounded.

A successful `document_site` `start` opens one extended budget for an enabled bot with the plugin granted. Defaults are **20 additional tool rounds, 8 calls per subsequent round, and 900 seconds**. Configure the three document-task fields per bot; zero additional rounds disables the extension. The extension applies to user-requested or autonomous document tasks, whoever initiated the conversation, while existing identity and room authorization remain unchanged. Repeating `start` cannot renew the budget during that turn. The first successful `workspace.start` or `web_fetch.start` can instead use the independent `work_task_*` bot fields (same defaults). Only the first start across all three packs can extend the turn; changing plugins/task IDs cannot stack or renew it, even if that first start has zero additional rounds. The time limit covers document generation/tool work; saved drafts survive exhaustion. Provider/cost limits and cancellation continue to apply.

The extended budget adds work capacity, never permissions. It does not enable a disabled bot, grant a plugin or authorize remote uploads. Bash requires separate workspace and shell grants plus a ready OS isolation boundary. Models must finish with a concise result, use returned URLs and distinguish local publication from remote sync queued/unconfigured. See [DOCUMENTS.md](DOCUMENTS.md) for the tool pack and storage/queue contract, [PLUGINS.md](PLUGINS.md) for extension interfaces and [VERIFICATION.md](VERIFICATION.md) for tests.

Active tool exchanges have a per-bot estimated working-set cap and must fit actual calibrated prompt headroom. Older bodies/pairs may be explicitly omitted from future requests while original evidence stays immutable. Granted `workspace`, `shell` and `web_fetch` support bounded `read_result` with a unique `result_id`; reads enforce original bot/channel/turn and transitive source grants. Native continuation metadata remains unchanged in retained assistant messages. See [the complete contract](AGENTIC_TOOLS.md).
