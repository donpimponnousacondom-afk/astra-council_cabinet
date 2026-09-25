import { useEffect, useRef, useState } from "react";
import { api, dateLabel } from "./api";
import type { RecordData } from "./api";
import { Empty, Field, Notice } from "./components";
import "./Secretary.css";

type Configuration = {
  max_active_reminders?: number | null;
  min_repeat_seconds?: number | null;
};
type Reminder = {
  id: string;
  bot_id: string;
  channel_id: string;
  key: string;
  message: string;
  due_at: string;
  repeat_seconds: number;
  state: "scheduled" | "fired" | "cancelled";
  revision: number;
  last_turn_status: string | null;
};

export function SecretarySettings({
  config,
  onChange,
}: {
  config: Configuration;
  onChange: (value: Configuration) => void;
}) {
  return (
    <>
      <Notice>
        Experimental reminder ledger. Grant Secretary to each bot that should
        schedule alarms. Reminders survive restarts and wake the bot in the
        original conversation, even with its timer off. Paused bots wait until
        resumed. Each wake uses the bot’s normal model and turn budget.
      </Notice>
      <div className="form-grid">
        {(
          [
            [
              "max_active_reminders",
              "Active reminders per bot (maximum)",
              100,
              1,
              10000,
            ],
            [
              "min_repeat_seconds",
              "Repeat interval (minimum seconds)",
              300,
              60,
              31536000,
            ],
          ] as const
        ).map(([key, label, fallback, min, max]) => (
          <Field key={key} label={label}>
            <input
              type="number"
              min={min}
              max={max}
              step={1}
              required
              value={config[key] === undefined ? fallback : (config[key] ?? "")}
              onChange={(e) =>
                onChange({
                  ...config,
                  [key]: e.target.value === "" ? null : Number(e.target.value),
                })
              }
            />
          </Field>
        ))}
      </div>
      <Notice>
        One-off alarms wake once; recurring alarms continue until cancelled.
        Missed repeats collapse into one wake when the bot becomes available. A
        failed or silent turn consumes that occurrence; use Snooze to rearm it.
        Fired/cancelled ledger entries are retained for 30 days; trajectories
        remain separate.
      </Notice>
    </>
  );
}

export function SecretaryPanel({
  botId,
  rooms,
  disabled = false,
}: {
  botId?: string;
  rooms: RecordData[];
  disabled?: boolean;
}) {
  const [items, setItems] = useState<Reminder[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [minutes, setMinutes] = useState("60");
  const mounted = useRef(false);
  const generation = useRef(0);
  const path = `/api/secretary${botId ? `?bot_id=${encodeURIComponent(botId)}` : ""}`;

  async function refresh() {
    const request = ++generation.current;
    setLoading(true);
    try {
      const rows = await api<Reminder[]>(path);
      if (mounted.current && request === generation.current) {
        setItems(rows);
        setError("");
      }
    } catch (e) {
      if (mounted.current && request === generation.current)
        setError(String(e));
    } finally {
      if (mounted.current && request === generation.current) setLoading(false);
    }
  }
  useEffect(() => {
    mounted.current = true;
    setItems([]);
    setBusy(false);
    void refresh();
    return () => {
      mounted.current = false;
      generation.current++;
    };
  }, [path]);

  async function change(row: Reminder, operation: "cancel" | "snooze") {
    setBusy(true);
    setError("");
    const request = ++generation.current;
    try {
      const updated = await api<Reminder>(
        `/api/secretary/${encodeURIComponent(row.id)}`,
        {
          method: "POST",
          body: JSON.stringify({
            operation,
            revision: row.revision,
            ...(operation === "snooze"
              ? { after_seconds: Number(minutes) * 60 }
              : {}),
          }),
        },
      );
      if (mounted.current && request === generation.current)
        setItems((rows) =>
          rows.map((item) => (item.id === row.id ? updated : item)),
        );
    } catch (e) {
      if (mounted.current && request === generation.current)
        setError(String(e));
    } finally {
      if (mounted.current && request === generation.current) setBusy(false);
    }
  }
  return (
    <section aria-label="Secretary reminders" className="stack">
      <h3>Secretary reminders</h3>
      <div className="form-grid">
        <Field label="Snooze for (minutes)">
          <input
            type="number"
            min={1}
            max={5256000}
            step={1}
            value={minutes}
            onChange={(e) => setMinutes(e.target.value)}
          />
        </Field>
        <div>
          <button
            type="button"
            disabled={busy || loading}
            onClick={() => void refresh()}
          >
            Refresh reminders
          </button>
        </div>
      </div>
      {error && <Notice warning>{error}</Notice>}
      {loading && <p>Loading reminders…</p>}
      {!loading && items.length === 0 && (
        <Empty title="No saved reminders">
          No saved reminders. A granted bot can create one with the secretary
          tool.
        </Empty>
      )}
      {items.map((row) => (
        <article key={row.id} className="secretary-reminder">
          <strong>{row.key}</strong> · {row.state} · {row.bot_id} ·{" "}
          {rooms.find((room) => room.channel_id === row.channel_id)?.name ||
            row.channel_id}
          <p style={{ whiteSpace: "pre-wrap" }}>{row.message}</p>
          <p>
            {row.state === "scheduled" ? "Next alarm" : "Last due"}:{" "}
            {dateLabel(row.due_at)} ·{" "}
            {row.repeat_seconds
              ? `Repeats every ${row.repeat_seconds}s`
              : "One-off"}
            {row.last_turn_status && ` · Last turn: ${row.last_turn_status}`}
          </p>
          <button
            type="button"
            disabled={
              disabled ||
              busy ||
              loading ||
              !Number.isInteger(Number(minutes)) ||
              Number(minutes) < 1 ||
              Number(minutes) > 5256000
            }
            onClick={() => void change(row, "snooze")}
          >
            Snooze
          </button>{" "}
          <button
            type="button"
            disabled={disabled || busy || loading || row.state !== "scheduled"}
            onClick={() => void change(row, "cancel")}
          >
            Cancel reminder
          </button>
        </article>
      ))}
      <small>
        Changes affect future occurrences. A turn already started may still
        post. Clean slate cancels scheduled reminders in its scope.
      </small>
      {disabled && (
        <Notice>
          Save or discard the bot’s configuration draft before changing
          reminders.
        </Notice>
      )}
    </section>
  );
}
