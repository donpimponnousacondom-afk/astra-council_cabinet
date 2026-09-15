import type { RecordData } from "./api";
import { Field, Notice } from "./components";

export function PricingEditor({
  draft,
  set,
}: {
  draft: RecordData;
  set: (key: string, value: any) => void;
}) {
  const split =
    draft.cache_hit_input_price_per_million != null ||
    draft.cache_miss_input_price_per_million != null;
  const missing =
    split &&
    [
      "cache_hit_input_price_per_million",
      "cache_miss_input_price_per_million",
      "output_price_per_million",
    ].some((key) => draft[key] == null);
  const price = (key: string, label: string, hint: string) => (
    <Field label={label} hint={hint}>
      <input
        type="number"
        min="0"
        step="any"
        value={draft[key] ?? ""}
        onChange={(event) =>
          set(
            key,
            event.target.value === "" ? null : Number(event.target.value),
          )
        }
      />
    </Field>
  );
  return (
    <details className="advanced">
      <summary>Optional · price estimates</summary>
      <p className="muted small-text">
        Manual USD rates per million tokens. Provider-reported cost always wins.
        Rates are saved with each request; changing them does not reprice
        history. Peak and off-peak schedules are not applied automatically.
      </p>
      <div className="form-grid">
        {price(
          "input_price_per_million",
          "Flat input USD / million tokens",
          "Used when both cache rate fields are empty.",
        )}
        {price(
          "output_price_per_million",
          "Output USD / million tokens",
          "Prices total completion tokens, including reported reasoning; never added twice.",
        )}
        {price(
          "cache_hit_input_price_per_million",
          "Cache-hit input USD / million tokens",
          "Optional discounted rate, for example 0.014. Fill both cache rates to use cache-aware estimates.",
        )}
        {price(
          "cache_miss_input_price_per_million",
          "Cache-miss input USD / million tokens",
          "Full input rate for tokens not served from cache. Replaces flat input pricing when cache rates are configured.",
        )}
      </div>
      {missing && (
        <Notice warning>
          Complete both cache rates and the output rate. Until then, requests
          without provider-reported cost have unknown cost.
        </Notice>
      )}
      {split && (
        <p className="muted small-text">
          Cache-aware estimates also require a reported cache split. Missing or
          inconsistent usage stays unknown; cache hits are not assumed.
        </p>
      )}
    </details>
  );
}
