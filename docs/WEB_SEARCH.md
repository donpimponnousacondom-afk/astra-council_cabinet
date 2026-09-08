# Brave and DuckDuckGo search

`web_search` is one granted plugin with two engines. Existing query-only calls remain valid. It defaults to `auto`: use Brave first, then DuckDuckGo if Brave fails or returns no web results. DuckDuckGo uses its [non-JavaScript HTML search](https://duckduckgo.com/duckduckgo-help-pages/features/non-javascript); this is not a paid API or an Instant Answers endpoint. It needs no key, but can return rate limits, challenges or a changed page format. These are reported as failures, never fabricated empty successes.

## Operator setup

1. In the [Brave API dashboard](https://api-dashboard.search.brave.com/), activate a Search plan, open **API Keys**, and create an API key. Follow the current plan terms shown there; this application does not assume a free allowance. See [Brave's quickstart](https://api-dashboard.search.brave.com/documentation/quickstart).
2. In Hortator, open **Plugins → Edit Web search**. Paste the key into **API key**, then click **Save credential**. This is separate from **Save changes**. The key is write-only and encrypted in the existing vault; do not put it in plugin JSON, Discord or source files. Saving the key does not make a validation/search request.
3. Enable the plugin globally. Select **Default search engine** and **Search results per engine**, then **Save changes**. For combined search select **Both** and **5**: up to five results from each engine, with duplicate URLs merged. Auto avoids a second engine when Brave returns usable results.
4. Grant **Web search** under **Bots → Edit bot → Capabilities** for each desired bot. Hortator follows the same global/bot grant rules and its existing owner-only intake. No separate DuckDuckGo account or credential is needed.

The global key is shared by granted bots. To use a different Brave key for a specific bot, choose **Web search** in that bot's **Plugin credential** selector and save **Per-bot plugin key**. Leave this override unset to inherit the global key; removing an override restores inheritance. Never enter a DuckDuckGo key in either box.

The non-secret configuration is shallow merged from built-in defaults, global plugin configuration, then this bot's `plugin_config.web_search`:

```json
{
  "engine": "auto",
  "count": 5,
  "endpoint": "https://api.search.brave.com/res/v1/web/search"
}
```

`engine` accepts `auto`, `brave`, `duckduckgo`, or `both`; `count` is an integer from 1 to 10 **per engine**. `endpoint` applies only to Brave and must be an HTTP(S) URL without embedded credentials or a fragment. It is operator configuration, not a model-selected fetch destination. Preserve endpoint overrides when changing modes. Saving validates global/per-bot fields without contacting either engine. Old records with only endpoint/count use Auto without a database rewrite.

## Model calls and results

Call `{}` for complete usage without network access or credential lookup. A real call requires a nonblank `query`; `engine` and `count` optionally override the operator default for that call:

```json
{"query":"Python documentation"}
{"query":"Python documentation","engine":"duckduckgo"}
{"query":"Python documentation","engine":"brave","count":5}
{"query":"Python documentation","engine":"both","count":5}
```

Both starts the two searches concurrently inside one tool call. It interleaves their results and merges identical normalized URLs, preserving an `engines` list on each item. A shared URL may reduce a five-plus-five request below ten distinct links. Results contain bounded `title`, `url` and `description` fields. These are untrusted previews, not evidence that the model read the linked page; use the separately granted `web_fetch` tool to read it.

| Response field | Meaning |
| --- | --- |
| `ok` | At least one engine completed successfully; a legitimate empty result is a success |
| `mode` | Effective requested mode |
| `results` | Deduplicated, interleaved previews with source engines |
| `engine_status` | Each attempted engine's status, count, duration, and failure category/message when applicable |
| `partial` | One engine failed while another completed successfully |
| `fallback_used` | Auto attempted DuckDuckGo after Brave |
| `truncated` | The combined preview list was shortened to preserve structured output limits |
| `error` | Summary when all selected engines failed; otherwise null |

Missing Brave credentials, HTTP errors, transport failures, overlong Brave queries, invalid responses and DuckDuckGo challenges remain visible in `engine_status`. Auto can still return DuckDuckGo results after a Brave configuration error; Both preserves whichever engine succeeded. An explicit Brave-only request fails when its key is missing. If all selected engines fail, the model receives structured engine evidence plus full usage, and the ledger records `tool.failed`. Do not repeat an identical failed request blindly or claim the failed engine supplied evidence.

Brave accepts at most 600 query characters or 75 words; the shared tool accepts up to 1,000 characters for DuckDuckGo. Queries are not silently truncated or split. A Brave query-limit failure permits Auto's DuckDuckGo fallback. Each engine has a 25-second wall-clock deadline and a 1,000,000-byte decompressed download ceiling. Redirects are not followed, and the Brave subscription token is sent only on its own engine request. No challenge solver, account impersonation, browser-cookie import or host-shell workaround is part of this adapter.

The parser bounds HTML structure and strips script/style text. Titles are capped at 300 characters, snippets at 1,000 and URLs at 2,048. Combined results stay below a 48,000-character serialized preview budget, leaving room for engine status within the registry cap. Search never downloads result pages or injects their full contents into a model context.

`web_search.engine_completed` and `web_search.engine_failed` belong to console scope **tools (`t`)**. Use **T** and the existing evidence paging keys to inspect the original call and stored result. One engine's warning alongside a successful `tool.completed` means partial search recovery, not a model/provider completion failure. Search events do not change shared model-provider health.

## Related failure guidance

The separate `web_fetch` default download ceiling remains **1,000,000 bytes**. Paged reading bounds model-facing text after a successful download; it does not raise that network ceiling. See [WEB_READING.md](WEB_READING.md). Changing search engines cannot make an oversized source fetchable.

Before a `shell.run`, explicitly create/resume its named task through `workspace` with `{"operation":"start","task":"aa-data"}` and wait for success. Reuse that task in the shell call. `shell` has no `start` operation and never creates a missing workspace. Filesystem/shell grants and runner readiness remain required; search fallback does not grant them.

Private memory writes always include `{"operation":"write","key":"topic","value":"Concise note"}`. Sending key/value alone fails validation; `{}` asks for usage. See [TOOLS.md](TOOLS.md) for the shared complete-error and explicit-example contract. Guidance changes never rewrite existing memories, clear historical evidence, infer missing mutation operations or add unbounded repair rounds.
