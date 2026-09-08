# Request cost estimates

Profile prices are manual USD rates per million tokens. The dashboard exposes them under **Model profiles → Edit → Optional · price estimates**. Provider-reported `usage.cost`, including an explicit zero, always takes precedence. Estimates are useful operational evidence, not a guarantee that a provider invoice will match.

## Flat and cache-aware pricing

With both cache fields empty, existing profiles retain the flat calculation:

`(prompt_tokens × input_price_per_million + completion_tokens × output_price_per_million) / 1,000,000`

Setting either cache field selects cache-aware pricing. Fill all three applicable fields:

| Profile field | What it prices |
| --- | --- |
| `cache_hit_input_price_per_million` | Input served from cache |
| `cache_miss_input_price_per_million` | Input not served from cache |
| `output_price_per_million` | Total completion tokens |

The calculation becomes `(cache_hits × hit_rate + cache_misses × miss_rate + completion_tokens × output_rate) / 1,000,000`. The old flat input rate remains saved but is not used in this mode. Clearing both cache fields restores flat pricing. Zero is a valid rate; an empty field means unknown.

The normalizer understands standard `prompt_tokens_details.cached_tokens` and DeepSeek's `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`. A reported total and one reported cache component determine the other by subtraction. If neither component is reported, the split is unknown; the runtime does not assume a cold cache. Contradictory counts, counts exceeding the total, incomplete rates, or missing total token counts leave estimated cost unknown. Reported cost still wins. Unknown costs retain the existing daily-budget stop behavior for bots with a cost limit.

`completion_tokens` is priced once. A nested `completion_tokens_details.reasoning_tokens` count is explanatory usage, not an additional output charge. We do not subtract reasoning or assume it is free for every provider/model. If a provider has a different billing definition, use its reported cost; this implementation does not invent a separate reasoning rate or infer visible-token usage from text length.

## Historical evidence

Each completed or failed request stores `response.pricing`: the profile identifier/revision, manual rate snapshot, normalized cache components, calculation basis, final cost/source, and an explanation when estimation was unavailable. Raw provider usage remains intact. The trajectory request inspector exposes **Pricing basis at request time**. Later profile edits never reprice existing ledger entries; older requests have no fabricated pricing snapshot. There is no historical backfill.

## DeepSeek configuration observation — 2026-09-08

The user's requested **$0.014/M cache-hit input** rate for `deepseek-v4-flash-vision-exp` is a manual peak-rate choice. The [official pricing page](https://api-docs.deepseek.com/quick_start/pricing/) checked on this date lists that model's peak hit/miss/output rates as **$0.014 / $0.44 / $1.32** per million tokens, with half-price off-peak rates. It describes peak periods as Monday–Friday **01:00–04:00 and 06:00–10:00 UTC** and bills image tokens as input. Rates can change; check the provider's current schedule before changing a profile.

Hortator does not automatically select time-of-day pricing. Choosing peak rates intentionally yields a conservative estimate during off-peak periods when the provider omits cost. Existing input/output prices are preserved unless the owner explicitly changes them; adding a hit discount alone cannot fix an incorrect miss/output rate. When migrating a legacy profile, copy its existing input rate to `cache_miss_input_price_per_million` if that remains the intended full input rate, enter the requested hit rate, and review output pricing independently. Do not silently apply a price table to every model that shares a provider.

For this task the owner explicitly chose to preserve the active vision profile's existing **$0.22 input / $0.66 output** rates and add **$0.014 cache-hit input**, with the cache-miss field copied from that $0.22 input rate. This is a chosen manual combination, not automatic selection of the provider's complete peak or off-peak tariff. Check the active profile for its current values; this dated decision is not a rolling configuration snapshot.

The [DeepSeek context-cache guide](https://api-docs.deepseek.com/guides/kv_cache/) describes its hit/miss usage fields. Token counts returned by the API are the basis for actual usage; offline text/image estimates are not billing evidence ([token usage guide](https://api-docs.deepseek.com/quick_start/token_usage/)). The model-written incident report's blanket assertion that reasoning is free is not treated as a verified billing contract.
