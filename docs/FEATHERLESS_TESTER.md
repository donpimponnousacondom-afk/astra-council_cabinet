# Featherless model tester

Run from the project root with the existing Python 3.14 environment:

```bash
uv run python featherless_tester.py --filter qwen --limit 30
```

This lists filtered models, asks for a selection such as `2-8,11`, then lets you edit stream mode, output cap, temperature, target input tokens, workers, system/user prompts and native JSON before inference. Enter retains each displayed value. `--yes` uses command-line settings without prompts. `--help` lists every option. The program uses the existing Featherless provider credential read-only, or `FEATHERLESS_API_KEY` from the environment. It never enables a bot, starts a council runtime or changes stored configuration, notes or checkpoints.

## Discovery and selection

```bash
# Metadata only. No inference and no warm-up.
uv run python featherless_tester.py --filter Qwen3.8-27B --limit 10 --list

# Numeric selection from that run's filtered list; compare both transports.
uv run python featherless_tester.py --filter Qwen3.8-27B --limit 10 \
  --select 2-8 --yes --mode both --workers 1 --max-tokens 512 --thinking off

# Exact IDs avoid catalog search entirely. --filter and --model are repeatable.
uv run python featherless_tester.py --model moonshotai/Kimi-K3 \
  --yes --no-stream --max-tokens 1024 \
  --system 'You explain physics clearly.' --prompt 'Explain E=mc².'
```

The tool calls `/v1/plan`, filtered `/v1/models?q=…&per_page=…&page=…`, and individual `/v1/models/{owner}/{model}`. Unfiltered catalog calls are refused. `--limit` defaults to 30 and is bounded to 100; `--page` selects another page. The live endpoint sometimes returns more filtered entries than requested: the tester warns and applies the limit locally, with a separate 2 MiB download bound. Search `qween` is normalized to `qwen` with a notice. Supported fields and effective context rules are documented in [Models API](https://featherless.ai/docs/api-reference-models) and [Plan API](https://featherless.ai/docs/api-reference-plan).

Availability is not a promise of immediate capacity. The table shows both the supplied tier and live/recent worker flags. During verification some models returned `tier=warm` with both flags false. `--availability cold` selects the literal cold tier; `--availability not-hot` selects that false/false combination without claiming it proves a cold model. Other filters: all, warm, loading, unknown.

## Warm-up and concurrency

Warm-up defaults on for cold/loading/offline models and those reporting neither a live nor recent worker. It sends a valid one-token completion, rather than an empty invalid request. A successful response proves the endpoint answered. After a failure it polls metadata every 15 seconds, up to 600 seconds by default, then makes one final readiness probe. Change with `--poll-seconds 15 --warm-timeout 600`; `--no-warm` skips this stage. The final probe has its own request timeout. Availability metadata can lag, and warming a model does not reserve it exclusively. [Model availability documentation](https://featherless.ai/docs/api-reference-models).

One owned TaskGroup manages the selected models. Warm-up polling releases inference slots so other selected models can be tested while waiting. Default `--workers 1` serializes **all** inference, including warm-up and filler generation. `--workers 2` or `4` additionally respects each model's reported unit cost and the plan's unit allowance. An unknown cost conservatively occupies the full allowance. `--units` can impose a smaller local allowance; `--ignore-plan-units` deliberately permits oversubscription for testing.

On the verified account the plan reported four units, Qwen27B cost two, and Kimi/Llama70B cost four. Thus four workers alone will not force four Qwen or two Llama requests into that account. Metadata wins over assumed model-size rules. Separate tester processes and running council bots do not share this local gate: keep the account vacant or account for their traffic.

## Context, reasoning and filler experiments

```bash
# Approximately 29,000 prompt tokens, with room for 1,024 output tokens.
uv run python featherless_tester.py --model Qwen/Qwen3.8-27B \
  --yes --input-tokens 29000 --max-tokens 1024 --thinking off \
  --temperature 0.4 --mode both

# Reuse generated prose rather than the built-in seeded reference filler.
uv run python featherless_tester.py --model Qwen/Qwen3.8-27B \
  --yes --generate-filler Qwen/Qwen3.8-27B --filler-output-tokens 4096 \
  --input-tokens 29000 --max-tokens 512 --thinking off

# Reuse the corpus from any earlier output directory.
uv run python featherless_tester.py --model moonshotai/Kimi-K3 --yes \
  --filler-file /path/to/earlier/run/filler.txt --input-tokens 28000 \
  --max-tokens 4096 --thinking on --reasoning-effort high --mode both

# Deliberately exceed prompt + output capacity to inspect the real rejection.
uv run python featherless_tester.py --model Qwen/Qwen3.8-27B --yes \
  --input-tokens 32768 --max-tokens 1024 --thinking off --allow-overflow
```

`--input-tokens` targets the complete prompt, including system/user text and chat-template overhead. The default counter calls the model's `/models/{owner}/{model}/debug/chat-format` endpoint with native thinking settings and iteratively adjusts filler, up to eight probes, targeting ±32 tokens (`--token-tolerance`). Actual completion usage can differ from this diagnostic endpoint, so reports retain both. `--token-count local` uses explicitly approximate cl100k_base accounting; it is not the model's exact tokenizer. The generated corpus is saved and repeated/trimmed; generation uses your native parameters and its separate output cap. No filler instructions are executed. See [chat-template controls](https://featherless.ai/docs/chat-template-kwargs).

The effective window is the smallest positive model limit, plan limit and optional `--context-limit`. Prompt **plus generated output** must fit. On this account the effective limit was 32,768 despite some models advertising larger windows. A 32,000-token prompt leaves only 768 tokens for reasoning plus answer. The tester refuses an estimated overflow unless explicitly allowed; it never silently raises or lowers the requested cap. `--omit-max-tokens` tests the endpoint default instead. It does not mean unlimited output.

`--thinking on|off|default` edits `chat_template_kwargs.enable_thinking`; `--reasoning-effort` sends the literal top-level value. Support varies by template/model. `--params '{"top_k":40,"min_p":0.05}'` supplies other native parameters, with explicit CLI fields taking precedence. Use `--system` and `--prompt` for text. Ordinary benchmarks request a single completion; the optional simulated-tool mode below runs a bounded native conversation. Returned reasoning is saved separately in private evidence; `--show-reasoning` prints it, and `--show-output` prints visible text. No returned reasoning text does not prove the model did no reasoning.

## Simulated tool laboratory

This is isolated from Hortator's working plugins, prompts and database. All eight tools are fake: `web_fetch`, `web_search`, `send_email`, `global_memory`, `memory`, `remember`, `notes`, `annotations`. No tool opens a network connection or sends email. The last five names expose identical note operations and descriptions, with only the function name changed. Each case receives its own JSON notebook, initially `{"project":"Orion","obsolete":"old fixture"}`. Repeated cases and other models cannot contaminate it.

```bash
# Eight explicit tasks, identical baseline for every model; SSE and buffered.
uv run python featherless_tester.py --filter Qwen3.8-27B --limit 30 \
  --tools all --tool-suite baseline --tool-guidance runtime \
  --mode both --workers 2 --max-tokens 2048 --reasoning-effort low

# Compare only memory naming, with shorter tester-only instructions.
uv run python featherless_tester.py \
  --model medismera/Qwen3.8-27B-OBLITERATED-Mythos-Class-Agentic \
  --yes --tools global_memory,memory,remember,notes,annotations \
  --tool-suite baseline --tool-guidance simple --no-stream \
  --max-tokens 2048 --reasoning-effort low

# Your own prompt, with only two selectable fake tools available.
uv run python featherless_tester.py --model moonshotai/Kimi-K3 --yes \
  --tools web_fetch,send_email --tool-guidance simple \
  --system 'Use tools when needed. Answer briefly.' \
  --prompt 'Fetch https://example.test/orion and email its launch code to alex@example.test.'
```

`--tools` enables names; `--disable-tools send_email,notes` subtracts names. Without `--tools`, the tester sends no tool schemas. `--tool-suite baseline` tests fetch, search, email and the full read → write → replace → delete → read-back lifecycle for each selected memory alias. By default each task exposes only its named tool. `--tool-exposure all` exposes all selected tools to every case, testing selection among similar names. `--tool-suite custom` (default) uses your `--prompt`; it records calls/state and checks native execution plus a final answer, but cannot automatically judge arbitrary requested outcomes.

Native `assistant.tool_calls` execute the simulator and receive matching `role=tool` / `tool_call_id` results. Returned native reasoning accompanies the assistant continuation as in the harness. Text such as `call:memory{operation:...}` never executes. `{}` returns usage without action; invalid calls receive all detectable field/type errors plus usage. `write` replaces an existing key. The JSON notebook is saved after each action. All operations remain bounded by `--tool-rounds` (default 6) and `--tool-calls-per-round` (default 4), with a final answer opportunity; rejected calls and discovery consume rounds.

This reproduces the native message exchange and memory operation contract, not a complete production context: fake receipts omit real channel attribution, quota enforcement and durable result handles. Those simplifications are identical across memory aliases. Actual request JSON is the authority when comparing the laboratory with a saved council trace.

Tool mode completes one model's cases before switching models. Workers parallelize cases for that same model within the account's weighted unit allowance. This avoids the observed account limit of four **model switches per minute**, independent of concurrency. `--model-switch-delay 16` adds a gap between model blocks; explicit model-switch 429 retries wait at least 65 seconds when retries are enabled. Other processes still share the upstream account limits. Ctrl-C joins active work and retains completed/partial evidence.

### Prompts, schemas and fixtures

`--tool-guidance runtime` uses a hardcoded copy of the current default `runtime-tool-guidance` prompt in `featherless_tools.py`. It does not read or overwrite any saved bot prompt. `simple` uses the shorter text below; `none` omits this extra guidance. `--tool-prompt-file /path/to/guidance.txt` substitutes your own exact text. System and user text remain separately editable through `--system` and `--prompt`. Each case records the full input and guidance hash.

Simple prompt suitable for a bot with `global_memory`:

```text
Use the actual global_memory tool, not a tool description in chat.
Read all notes: {"operation":"read"}
Save a note: {"operation":"write","key":"topic","value":"Your note"}
Edit a note: write again with the same key and replacement value.
Delete a note: {"operation":"delete","key":"topic"}
operation is required. Use double-quoted JSON strings.
{} only shows help; it does not read or save anything.
Wait for the tool result before claiming success.
After finishing, answer normally in chat.
```

`--tool-schema runtime` includes Hortator's empty-object usage wrapper and conditional required fields. `conditional` omits that wrapper; `simple` advertises ordinary fields without conditional JSON Schema keywords. Execution always validates the complete contract, whichever advertisement is selected. The simulated web/email schemas are intentionally small, not replicas of every production web option. `--tool-choice required` forces a native call on the first request only; later rounds use `auto` so the model can finish. Default is `auto` throughout.

`--tool-memory-file /path/to/seed.json` supplies an object of string keys/string values; every case copies it afresh. `--tool-fixtures-file /path/to/responses.json` replaces selected response fields, for example:

```json
{
  "web_search": {"results": [], "http_status": 202, "text": "Fixture: no results"},
  "web_fetch": {"http_status": 200, "text": "The Orion launch code is AMBER-42."},
  "send_email": {"status": "sent", "message_id": "test-001"}
}
```

Fixtures are labelled simulated. A status value does not imply a real request or prove any challenge/cause. Tool mode rejects filler options rather than silently ignoring them; larger custom text can be supplied explicitly through the system/user prompt. `--tool-template-probe` saves Featherless's `debug/chat-format` result for the first request and first continuation. A rendered template is evidence of rendering, not proof that the serving/tool parser path is correct.

### Tool reports

Every tool run writes `report.html`, `case-input-*.json`, `case-result-*.json`, `memory-*.json` and the usual private request evidence. The report is self-contained and readable on mobile. It includes exact prompts/schemas, operation checks, argument repairs, native tool traces, TTFT/TPS, completion/reasoning counts and their provenance. Provider reasoning text remains in adjacent private request files rather than the HTML. Rebuild one run or aggregate several runs with:

```bash
uv run python featherless_report.py /absolute/path/to/benchmark-directory
```

For an aggregate study, optional `selection.json` documents the roster and `findings.md` records interpretation. An `exclude-from-comparison.json` in a run directory can contain `{"reason":"Interrupted exploratory run with model-switch throttling"}`: its cases remain visible as operational evidence but do not affect the model/tool matrix. Never discard failed trials to improve a score; exclusions must identify a concrete protocol/setup problem and remain disclosed.

Compare identical settings before attributing differences to tool names. A provider rejection, output-length stop or timeout is not proof that a model cannot call tools. Count repairs separately from success; record unsuccessful cases too. Reported native token counts are preferred; missing counts use fixed cl100k estimates with explicit provenance. An absent reasoning count is `none`, not a fabricated zero. High/max effort support depends on the actual template; record requested values and observed behavior without claiming an ignored setting worked.

## Measurements and evidence

Each run creates a new private directory under `$HORTATOR_DATA_DIR/benchmarks/` (default `~/.local/share/hortator/benchmarks/`). Use `--output /absolute/new/directory` to choose another location outside the source checkout. Directories are 0700 and files 0600; credentials are redacted. Do not commit these artifacts.

- `catalog.json`: chosen model metadata and plan; `metadata-*.json`: bounded raw metadata responses/headers.
- `run.json`: options, exact model IDs, request parameters, script and filler hashes.
- `filler.txt`: reusable reference corpus; `prompt-*.json`: complete measured prompts and fit checks.
- `request-*.json`: every warm-up/filler/benchmark attempt, request, HTTP headers, original error, partial output, reasoning, usage and diagnostics. SSE events are reconstructed after framing; raw capture is bounded to 128 MiB, with an explicit failure if exceeded.
- `results.csv` and `summary.json`: measurements and links to complete per-request evidence. Interrupted requests preserve partial evidence; Ctrl-C cancels and joins owned work.

TTFT measures the first actual streamed text/reasoning/tool-call delta, with a separate first-visible-text measurement. Stream TPS is a best-effort rate from observed arrival timing and reported output count; packet batching and mixed reasoning affect it. A response delivered in one activity packet has no measurable streaming interval. Buffered mode has unknown TTFT/stream TPS; `e2e_tps` is output tokens divided by full request duration. When usage is missing, output counts use a labelled cl100k_base estimate. Neither mode invents a successful complete answer after `finish_reason=length` or an absent completion boundary.

`--repeat 3` repeats measurements. Retries default to zero so instability stays visible; `--retries 2 --retry-delay 10` retries transient transport/deadline, capacity/rate-limit/server envelopes and HTTP 408/429/5xx outcomes. Every attempt remains recorded. Invalid parameters, parser failures and gated/authentication failures are not silently retried. `--timeout 180` sets each inference deadline. Metadata requests have a separate 30-second bound. Official gated Llama may require accepting its license and connecting Hugging Face before testing; the tool reports that supplied response and does not change account permissions. [Featherless error documentation](https://featherless.ai/docs/api-reference-error-codes).

The script exercises models independently of Discord. A successful simple benchmark does not prove acceptance of council tool schemas, images, long compactions or every reasoning level. Use the saved requests to compare those differences explicitly.
