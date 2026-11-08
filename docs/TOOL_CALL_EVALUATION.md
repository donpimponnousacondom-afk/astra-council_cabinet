# Offline tool-call evaluation

The owner authorized this Cheapseek-only trial on 2026-11-08. It investigates
missing `operation` arguments using historical Sun/Zelda requests. It does not
change runtime prompts, tools, validation, bot configuration or provider models.
The owner reviews measured results and council scores before deciding whether to
benchmark Loki's provider. Production prompt tuning remains frozen.

## Replay a saved request

Use the repository environment and an unused ignored audit directory:

```bash
.venv/bin/python scripts/replay_tool_request.py \
  --request-id req_a36f4582d29245a49f74 \
  --variant baseline description required --repeats 6 \
  --output audit/2026-11-08/loki-tool-failures/screening-sun-r1
```

Each repeat is a block containing each selected variant once. The script rotates
the order across blocks and reverses alternate cycles. Every complete cycle of
N blocks balances the positions of N variants. Use whole cycles and inspect the
manifest's `position_balanced` flag. Only one inference is in flight. Each planned inference is sent once; there are no retries
or tool-result continuations. Returned tools are **never dispatched**.

The script reads the external SQLite database in read-only mode and decrypts only
the selected provider credential using the existing key. It does not instantiate
the application Store/Vault or write bot state. Requests must belong to Sun/Zelda
and use the exact configured Cheapseek model. Paid inference requires owner
authorization; this task has it.

`baseline` preserves the saved JSON body, including tools, messages, model
parameters and tool choice. The original saved body has already undergone the
application's evidence redaction and may omit hidden reasoning/media. Current
provider configuration and credentials are used; historical backend identity and
original wire bytes cannot be reconstructed from this record.

## Experimental variants

- `description`: prepend a complete shell JSON run example and explicit operation
  requirement to the existing shell description. Messages and schemas stay intact.
- `required`: retain the schema and add a redundant root condition requiring
  `operation` on nonempty objects. The original action branch already requires it;
  `{}` remains a help query.
- `combined`: apply both interventions. Test this only as a separately declared
  experiment; it cannot identify which component caused an improvement.
- `op_field`: add an explicit requirement and operation guidance to the operation
  property's description in both copies of the shell schema.
- `instruction`: append one system message explaining the complete shell calling
  convention. Tools and all original messages remain intact. This is a prompt
  experiment and has no production rollout authorization.
- `root_required`: **diagnostic only**. Add a mandatory root operation field. This
  deliberately makes `{}` invalid in the advertised schema while the original
  runtime evaluator still handles it as help. It cannot be selected as a
  deployable candidate without resolving that contract with the owner.

All responses are evaluated against the **original** tools using the runtime JSON
parser and validator. No implicit operation, default coercion or relaxed validation
is introduced. The tests compare representative accepted/rejected inputs across
variants, including help, missing selectors, unknown fields and bad types; the
diagnostic arm's advertised-help difference is asserted explicitly. The original
description arm retains its screened wording for reproducibility; literal copying
of `YOUR_COMMAND` is flagged as a quality concern, never credited as task success.

## Evidence and interpretation

Before inference, the manifest records the full planned order, current provider
revision, source/variant payload hashes, evaluator source hashes and parser library
versions. Each arm has an explicit change summary. Source copies
pin the experimental implementation. Request files contain each exact variant
body. Response files contain reconstructed model messages and parser diagnostics;
wire files contain the consumed, decompressed response text, with configured key
redaction and UTF-8 replacement if needed. These are private audit evidence, not
complete network captures. Keep raw bodies, commands and conversations out of Git.

Request outcomes distinguish complete responses, incomplete/truncated responses,
transport failures, provider errors and response-format failures. Call categories
are valid action, help, missing operation and other invalid. No-tool responses,
mixed batches and valid actions have separate flags. Retain additional validation
errors even when missing operation is the primary category.

Report both omission-bearing completions / complete responses and missing-operation
calls / nonempty shell calls, with raw counts. Report malformed calls separately
so unparseable arguments do not disappear from the result. Help-only, no-tool and
unrelated-tool outcomes cannot count as successful shell actions. Schema validity
does not prove command success or useful progress: review task relevance, necessary
command content, storage choices and unsupported completion claims privately.

Keep exploratory discovery samples separate from contemporaneous controls.
Predeclare selection rules and reserve another historical request before viewing
its replay results. Once used to tune a variant, it is no longer held out. Small
repeated samples from one context are a screening result, not a production-wide
error-rate estimate. Record cache/usage/time metadata without assuming a cache
effect or stable provider implementation.

Return counts, quality assessment, failures and objections to all three members
of the project-local [council](../skills/council/SKILL.md). Research scores do not
replace final open-PR review. Dated results belong in [VERIFICATION](VERIFICATION.md).
