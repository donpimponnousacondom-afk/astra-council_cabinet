import type { RuntimeVersion } from "./Version";

export type RecordData = Record<string, any>;
export type Kind =
  | "bots"
  | "providers"
  | "profiles"
  | "prompts"
  | "plugins"
  | "rooms"
  | "settings";
export type Page = "council" | "trajectory" | "analytics" | "commands" | Kind;
export type Dashboard = {
  version?: RuntimeVersion;
  bots: RecordData[];
  providers: RecordData[];
  profiles: RecordData[];
  prompts: RecordData[];
  plugins: RecordData[];
  rooms: RecordData[];
  settings: RecordData;
  now: number;
  owner_id: string;
  active_requests: RecordData[];
  delivery_counts: RecordData[];
  last_event_seq: number;
};
let csrf = "";
export function setCsrf(value: string) {
  csrf = value;
}
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      ...options.headers,
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new ApiError(
      data.error || `Request failed (${response.status})`,
      response.status,
    );
  return data;
}
export const control = (
  action: string,
  kind?: string,
  id?: string,
  data?: RecordData,
) =>
  api("/api/control", {
    method: "POST",
    body: JSON.stringify({ action, kind, id, data: data || {} }),
  });
export const credential = (
  kind: string,
  id: string,
  field: string,
  value: string,
) =>
  api(
    `/api/credentials/${kind}/${encodeURIComponent(id)}/${encodeURIComponent(field)}`,
    { method: "PUT", body: JSON.stringify({ value }) },
  );
export const num = (value: number | null | undefined, compact = false) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("en", {
        notation: compact ? "compact" : "standard",
        maximumFractionDigits: compact ? 1 : 0,
      }).format(value);
export const money = (value: number | null | undefined) =>
  value == null ? "—" : "$" + value.toFixed(value < 1 ? 4 : 2);
export const duration = (value: number | null | undefined) =>
  value == null
    ? "—"
    : value < 1000
      ? `${Math.round(value)} ms`
      : `${(value / 1000).toFixed(2)} s`;
let councilTimezone = "Europe/Madrid";
export function setCouncilTimezone(value: string) {
  // The server validates IANA identifiers; also fail safely during initial loading.
  try {
    new Intl.DateTimeFormat("en", { timeZone: value }).format();
    councilTimezone = value;
  } catch {
    councilTimezone = "Europe/Madrid";
  }
}
export function dateLabel(value: number | string | null | undefined): string {
  if (value == null) return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.valueOf())) return "—";
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: councilTimezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
    timeZoneName: "longOffset",
  }).formatToParts(date);
  const p = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  const offset =
    p.timeZoneName === "GMT" ? "+00:00" : p.timeZoneName.replace("GMT", "");
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}${offset}`;
}
export const timeLabel = (value: number | null | undefined) =>
  value ? dateLabel(value).split("T")[1] : "—";
export const kindLabel: Record<Kind, string> = {
  bots: "Bot",
  providers: "Provider",
  profiles: "Model profile",
  prompts: "Prompt",
  plugins: "Plugin",
  rooms: "Room",
  settings: "Council settings",
};
export function eventText(event: RecordData): string {
  const d = event.data || {};
  return (
    d.error ||
    d.content ||
    d.label ||
    (d.resource ? `${d.resource}/${d.id}` : "") ||
    (d.model ? `${d.model} · ${d.purpose || ""}` : "") ||
    d.note ||
    d.status ||
    (d.provider_id
      ? `${d.provider_id} · ${d.consecutive_failures || 0} failures`
      : "") ||
    "Recorded in the council ledger"
  );
}
