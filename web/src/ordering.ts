import type { Dashboard, RecordData } from "./api";

// Keep tables and pickers consistent across browser locales and API/ID order.
const collator = new Intl.Collator("en", {
  sensitivity: "base",
  numeric: true,
});
export const compareLabels = (a: string, b: string) =>
  collator.compare(a.trim(), b.trim());
export const recordName = (record: { name?: string; id?: string }) =>
  record.name?.trim() || record.id || "";

export function alphabetical<T>(
  items: readonly T[],
  label: (item: T) => string,
  identity: (item: T) => string = label,
): T[] {
  return [...items].sort((a, b) => {
    const x = identity(a),
      y = identity(b);
    return (
      compareLabels(label(a), label(b)) ||
      compareLabels(x, y) ||
      (x < y ? -1 : x > y ? 1 : 0)
    );
  });
}

export function byName<T extends { name?: string; id?: string }>(
  items: readonly T[],
): T[] {
  return alphabetical(items, recordName, (item) => item.id || "");
}

export function orderDashboard(data: Dashboard): Dashboard {
  // Presentation copies only; saved prompt IDs, live contexts and evidence keep
  // their semantic order, and no source payload is mutated.
  const ordered = { ...data };
  for (const kind of [
    "bots",
    "providers",
    "profiles",
    "prompts",
    "plugins",
    "rooms",
  ] as const)
    ordered[kind] = byName<RecordData>(data[kind]);
  if (data.prompt_layers)
    ordered.prompt_layers = byName(data.prompt_layers).map((layer) => ({
      ...layer,
      templates: byName(layer.templates),
    }));
  return ordered;
}
