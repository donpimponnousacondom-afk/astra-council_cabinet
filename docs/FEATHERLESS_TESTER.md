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

`--thinking on|off|default` edits `chat_template_kwargs.enable_thinking`; `--reasoning-effort` sends the literal top-level value. Support varies by template/model. `--params '{"top_k":40,"min_p":0.05}'` supplies other native parameters, with explicit CLI fields taking precedence. Use `--system` and `--prompt` for text; requests always contain a single completion and no tools. Returned reasoning is saved separately in private evidence; `--show-reasoning` prints it, and `--show-output` prints visible text. No returned reasoning text does not prove the model did no reasoning.

## Measurements and evidence

Each run creates a new private directory under `$HORTATOR_DATA_DIR/benchmarks/` (default `~/.local/share/hortator/benchmarks/`). Use `--output /absolute/new/directory` to choose another location outside the source checkout. Directories are 0700 and files 0600; credentials are redacted. Do not commit these artifacts.

- `catalog.json`: chosen model metadata and plan; `metadata-*.json`: bounded raw metadata responses/headers.
- `run.json`: options, exact model IDs, request parameters, script and filler hashes.
- `filler.txt`: reusable reference corpus; `prompt-*.json`: complete measured prompts and fit checks.
- `request-*.json`: every warm-up/filler/benchmark attempt, request, HTTP headers, original error, partial output, reasoning, usage and diagnostics. SSE events are reconstructed after framing; raw capture is bounded to 128 MiB, with an explicit failure if exceeded.
- `results.csv` and `summary.json`: measurements and links to complete per-request evidence. Interrupted requests preserve partial evidence; Ctrl-C cancels and joins owned work.

TTFT measures the first actual streamed text/reasoning, with a separate first-visible-text measurement. Stream TPS is a best-effort rate from observed arrival timing and reported output count; packet batching and mixed reasoning affect it. Buffered mode has unknown TTFT/stream TPS; `e2e_tps` is output tokens divided by full request duration. When usage is missing, output counts use a labelled cl100k_base estimate. Neither mode invents a successful complete answer after `finish_reason=length`.

`--repeat 3` repeats measurements. Retries default to zero so instability stays visible; `--retries 2 --retry-delay 10` retries transient transport/deadline, capacity/rate-limit/server envelopes and HTTP 408/429/5xx outcomes. Every attempt remains recorded. Invalid parameters, parser failures and gated/authentication failures are not silently retried. `--timeout 180` sets each inference deadline. Metadata requests have a separate 30-second bound. Official gated Llama may require accepting its license and connecting Hugging Face before testing; the tool reports that supplied response and does not change account permissions. [Featherless error documentation](https://featherless.ai/docs/api-reference-error-codes).

The script exercises models independently of Discord. A successful simple benchmark does not prove acceptance of council tool schemas, images, long compactions or every reasoning level. Use the saved requests to compare those differences explicitly.
