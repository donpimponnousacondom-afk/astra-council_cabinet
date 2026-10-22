# DeepSeek Search

`deepseek_search` is a separate opt-in search plugin, like the Parallel
[`dumb_search`](DUMB_SEARCH.md) capability. It returns links and search summaries
for the calling bot to evaluate. It does not start a researcher, change the
generation model, or route through another search engine. Ordinary `web_search`,
Parallel and MiMo research retain their existing configuration and behavior.

## Operator setup

1. Save the account key in **Providers** on a provider with an official
   `https://api.deepseek.com` base URL, such as `https://api.deepseek.com/v1`.
2. Open **Plugins → DeepSeek Search**, select that **DeepSeek credential
   provider**, and save. This reuses the existing encrypted provider key; there
   is no separate plugin credential to copy. Saving makes no search request.
3. Enable the plugin globally and grant **DeepSeek Search** to each intended
   bot. New installations and upgrades leave it disabled and ungranted.

The selected provider is a credential source only. Its model configuration,
enabled flag, custom headers and endpoint path do not control search. Requests
go only to `POST https://api.deepseek.com/v1/tools/search`; redirects are not
followed. Unrelated model, Brave and Parallel keys are never used for this call.
Referenced providers cannot be deleted or moved to another origin until their
DeepSeek search references are cleared, following the existing Ollama search
credential boundary.

Defaults, global configuration and `plugin_config.deepseek_search` merge in that
order. The configuration is:

```json
{"count": 5, "provider_id": ""}
```

`count` accepts 1–50; `provider_id` selects the saved provider. A bot can override
either field. An empty provider ID clears that selection. There is no automatic
provider discovery, engine fallback, grant change or configuration migration.

## Queries and limits

```json
{"query":"Python TaskGroup cancellation"}
{"query":"SQLite WAL checkpoint starvation site:sqlite.org","count":10}
{"operation":"read_result","result_id":"RETURNED_ID","offset":0,"length":6000}
```

`operation: "search"` is optional. `{}` returns usage without network access or
credential lookup. A query must contain 1–128 Unicode characters and cannot be
all whitespace. Spaces, quotes and search operators count. Local validation
counts Unicode code points, not UTF-8 bytes, UTF-16 units, graphemes or tokens;
the exact query is forwarded without normalization. Invalid lengths report the
actual count and limit before sending. Queries are never truncated, rewritten,
split or expanded automatically. The bot can submit separate focused searches
within its existing tool-round and call budgets.

The tool schema, configuration validation and dashboard derive their limits
from the plugin constants. The dashboard displays the same query/count bounds.
The per-bot default count is reflected in schema/help; requests and live usage
do not rewrite the schema or earlier prompt layers.

Each call makes one request with `need_content: false`. The optional upstream
`ori_question` field also has a 128-character limit and is not exposed in this
version. Localization, native Fetch and full-page search content are also out of
scope. See the [official API contract](https://api-docs.deepseek.com/api/search/).

## Evidence and failure handling

Results retain URL, title, complete summary as `description`, snippet, date,
site name, and available provider relevance/authority scores. Scores are search
signals, not proof of truth. Summaries can be longer than the provider's typical
length; they are not clipped into short teasers. The existing result pager stores
the selected results before limiting the prompt view. Follow returned `next` /
`read_result` arguments to continue without searching again. These are untrusted
search previews, not full-page reads; use separately granted `web_fetch` when
source reading is needed.

The wall-clock deadline is 25 seconds and the shared decompressed response
ceiling is 1,000,000 bytes. Pagination does not increase that download bound.
Actual empty results are successful empty searches. Missing configuration,
non-200 HTTP responses, malformed results, transport errors and download limits
are explicit failures, with the existing complete tool usage. No automatic
retry or fallback consumes extra requests. Rate limits and balance failures
remain distinguishable in the saved upstream status/body.

Every received response is captured before parsing under
[HTTP_EVIDENCE](HTTP_EVIDENCE.md). Successful results omit the duplicate raw-body
preview but retain its read handle. Cancellation and deadlines preserve received
headers/bytes as partial evidence; cancellation still propagates. A failure
before any response arrives cannot provide response evidence. Console **T**
can inspect captured responses, including cancelled calls. Reads enforce the
original bot/channel/turn and current grant; removing a provider key does not
prevent rereading already saved evidence. Existing trajectory retention applies;
there is no new database or periodic collector.

## Cost and evaluation

As checked on 2026-10-22, DeepSeek lists **$0.75 per 1,000 successful searches**.
Only HTTP 200 with nonempty results is billable under the published rules; count
does not change the per-request price. Reading results also consumes generation
model input tokens. Search charges are not model-token charges and are not added
to profile pricing totals. Do not infer free standalone searches from promotional
terms for specific DeepSeek harness integrations. Check the provider account for
actual billing. See [official pricing and limits](https://api-docs.deepseek.com/guides/search/).

A three-query live comparison with Parallel is recorded in VERIFICATION. It
tests real retrieval and parser compatibility, not broad search quality, answer
accuracy or provider prompt-cache savings.

## Owner decision · 2026-10-22

Implement a separate plugin with its own bot grant, like Parallel. The owner
corrected an earlier engine-selector answer before publication. Keep the saved
DeepSeek provider credential reusable, queries bounded without silent rewriting,
summaries recoverable through paging, and existing search/research routing
unchanged. Tool Fetch integration remains deferred. This authorization does not
unfreeze the separate shell prompt-tuning proposals.
