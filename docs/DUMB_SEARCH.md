# Dumb Search · Parallel experiment

`dumb_search` is an independent, opt-in search tool. It returns web links and
excerpts through Parallel's Fast Search API; the calling bot decides what to
believe, which sources to read and whether to refine its query. It neither
delegates reasoning nor starts a background researcher. Existing `web_search`
engines and `web_fetch` remain independent.

## Operator setup

1. Open **Plugins → Dumb Search · Parallel experiment → API key** and **Save
   credential**. The existing write-only vault stores the key. Saving it makes
   no paid request. Do not put it in plugin JSON or a model prompt.
2. Enable the plugin globally and save changes.
3. Grant **Dumb Search · Parallel experiment** under the chosen bot's
   **Capabilities**. To test it as that bot's only search provider, uncheck
   **Web search** there. Keep **Web fetch** if the bot should read source pages.

New installations and upgrades leave this plugin disabled, with no bot grants.
No generation provider is needed. The global plugin key is shared by granted
bots; the existing **Per-bot plugin key** can override it for `dumb_search`.
Removing that override restores inheritance.

Defaults → global config → `plugin_config.dumb_search` are shallow merged:

```json
{"count": 5, "max_chars_total": 12000}
```

`count` accepts integers 1–10. `max_chars_total` accepts integers 1,000–24,000
and bounds the combined excerpt characters, both in the request and locally.
Unknown configuration fields are rejected. The bot's description/schema show
its effective result-count default. No additional dashboard controls are needed;
these optional fields use the existing configuration JSON editor.

## Calls, evidence and limits

```json
{"query":"SQLite WAL checkpoint starvation"}
{"query":"site:sqlite.org WAL long readers", "objective":"Find the primary explanation of checkpoint starvation", "count":5}
{"operation":"read_result", "result_id":"RETURNED_ID", "offset":0, "length":6000}
```

`operation: "search"` is optional. Each search requires one nonblank query
(up to 1,000 characters); an optional objective accepts up to 2,000 characters.
`{}` returns full usage without reading credentials or making a request.

Each call makes one `POST https://api.parallel.ai/v1/search` with explicit
`mode: "fast"`, a single `search_queries` entry, `max_chars_total` and
`advanced_settings.max_results`. There are no automatic retries, engine
fallbacks, delegated agents, page fetches or paid tier upgrades. The model cannot
change mode, endpoint or submit a batch. Operator tool-round/call limits still
apply; refined searches are separate calls.

Results retain the upstream URL, nullable title/publication date and excerpts.
`ok`, `mode`, `search_id`, `duration_ms`, `reported_usage`, `warnings` and explicit
truncation flags distinguish outcomes. Excerpt text is not reduced to a short
teaser. These are untrusted search excerpts, potentially indexed or stale;
successful retrieval does not establish relevance, truth, freshness or that the
bot read a full page. Use the separately granted `web_fetch` for source reading.

Every received response is captured before parsing under the existing
[HTTP evidence contract](HTTP_EVIDENCE.md), including non-2xx, malformed and
partially downloaded responses. The deadline is 25 seconds and the decompressed
body limit is 1,000,000 bytes. Redirects are not followed. Failures return
actionable usage and record `tool.failed`; an actual empty array is a successful
empty search. Cancellation propagates to the request. A timeout before response
capture cannot provide response evidence.

Successful results omit the duplicate raw-body preview. The returned
`http_response.read_response` arguments retrieve the saved response using this
tool's `read_result`, without another network request or requiring an API key.
Oversized structured results are stored before paging. Rereads require the same
bot/channel/turn and current originating grants, including across other tools'
rereads. Original response bodies are also accessible in console **T** evidence.
Existing trajectory retention applies; this plugin creates no extra database.

## Cost and evaluation

As checked on 2026-10-02, Parallel lists Fast at **$1 per 1,000 requests**, with
up to ten results included. Thus one call is about $0.001; three refined calls
are about $0.003; 5,000 calls are about $5 before account credits, taxes or future
price changes. The plugin's ten-result maximum avoids the documented extra
result charges. It explicitly sets Fast because an omitted mode defaults to
the more expensive Advanced tier. See [official pricing](https://docs.parallel.ai/getting-started/pricing)
and [modes](https://docs.parallel.ai/search/modes).

The response's usage entries are preserved verbatim. They are not an invoice:
the live probes reported `sku_search` counts but no dollar amount. Check the
Parallel account dashboard for actual credit consumption and expiry. This
plugin neither assumes a recurring free allowance nor implements a monthly
spend cap. Search charges are not added to model-token pricing totals. Excerpts
also consume tokens when the conversational model reads them.

The first live evaluation found useful technical documentation with sub-second
median retrieval, alongside stale date-specific results and irrelevant matches
for a nonexistent identifier. Treat it as an inexpensive retrieval experiment,
not a guarantee of fresh news or exhaustive research. Measured results and
limits belong in [VERIFICATION.md](VERIFICATION.md).

## Owner decisions · 2026-10-02

- Separate experimental search-only plugin, with a deliberately silly name;
  available to any explicitly granted bot. The owner will switch Loki manually
  and evaluate usage/quality before expanding it.
- Monitoring is deferred. No Parallel SDK, CLI, vendor skill installation,
  Extract, Task, Responses or other paid research feature in this change.
- Live probes are authorized in moderation, beyond a single smoke test, to
  measure real usefulness and cost. The owner reports $85 promotional credit
  expiring in December; that is account context, not a verified balance or an
  application default.
- A temporary disabled provider served only as the dashboard credential entry
  before the plugin existed. Move that credential into the plugin vault field
  and remove the staging provider after installation; never grant it to a bot.
