import { useEffect, useRef, useState } from "react";
import { Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { api, dateLabel, num, type RecordData } from "./api";
import { Badge, Empty, Field, Notice } from "./components";
import "./GlobalMemory.css";

/** Kept mounted across bot editor sections so note drafts never vanish on tab changes. */
export function GlobalMemoryPanel({
  botId,
  onDirtyChange,
  onBusyChange,
}: {
  botId: string;
  onDirtyChange: (dirty: boolean) => void;
  onBusyChange: (id: string, busy: boolean) => void;
}) {
  const [view, setView] = useState<RecordData | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [noteKey, setNoteKey] = useState("");
  const [noteValue, setNoteValue] = useState("");
  const [originalKey, setOriginalKey] = useState<string | null>(null);
  const [baseline, setBaseline] = useState({ key: "", value: "" });
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const mounted = useRef(false);
  const request = useRef(0);
  const pending = useRef(false);
  const identity = useRef(botId);
  identity.current = botId;
  const operationId = `global-memory:${botId}`;
  const dirty = noteKey !== baseline.key || noteValue !== baseline.value;
  const endpoint = `/api/global-memory/${encodeURIComponent(botId)}`;

  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
      request.current++;
      onDirtyChange(false);
      onBusyChange(operationId, false);
    };
  }, [botId]);

  async function refresh() {
    const selectedBot = botId;
    const sequence = ++request.current;
    setLoading(true);
    try {
      const result = await api(endpoint);
      if (
        !mounted.current ||
        selectedBot !== identity.current ||
        sequence !== request.current
      )
        return;
      setView(result);
      setError("");
    } catch (cause) {
      if (
        mounted.current &&
        selectedBot === identity.current &&
        sequence === request.current
      )
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not load global notes",
        );
    } finally {
      if (
        mounted.current &&
        selectedBot === identity.current &&
        sequence === request.current
      )
        setLoading(false);
    }
  }

  function select(note?: RecordData) {
    if (pending.current) return;
    if (dirty && !window.confirm("Discard the unsaved global note changes?"))
      return;
    const next = { key: note?.key || "", value: note?.value || "" };
    setNoteKey(next.key);
    setNoteValue(next.value);
    setOriginalKey(note?.key ?? null);
    setBaseline(next);
    setError("");
    setMessage("");
  }

  async function change(operation: "write" | "delete") {
    if (pending.current || !noteKey.trim()) return;
    if (
      operation === "delete" &&
      !window.confirm(`Delete global note “${noteKey}” for ${botId}?`)
    )
      return;
    const selectedBot = botId;
    const selectedKey = noteKey.trim();
    const selectedValue = noteValue;
    pending.current = true;
    request.current++;
    setLoading(false);
    setBusy(true);
    setError("");
    setMessage("");
    onBusyChange(operationId, true);
    try {
      const result = await api(endpoint, {
        method: "POST",
        body: JSON.stringify({
          operation,
          key: selectedKey,
          ...(operation === "write" ? { value: selectedValue } : {}),
        }),
      });
      if (!mounted.current || selectedBot !== identity.current) return;
      const next =
        operation === "write"
          ? { key: selectedKey, value: selectedValue }
          : { key: "", value: "" };
      setNoteKey(next.key);
      setNoteValue(next.value);
      setBaseline(next);
      setOriginalKey(operation === "write" ? selectedKey : null);
      setMessage(
        result.warning ||
          (operation === "write"
            ? "Global note saved."
            : "Global note deleted."),
      );
      await refresh();
    } catch (cause) {
      if (mounted.current && selectedBot === identity.current)
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not change global note",
        );
    } finally {
      pending.current = false;
      if (mounted.current && selectedBot === identity.current) {
        setBusy(false);
        onBusyChange(operationId, false);
      }
    }
  }

  return (
    <section
      className="global-memory-panel"
      aria-label={`Global notes for ${botId}`}
    >
      <div className="section-heading">
        <h3>Global notes · {botId}</h3>
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh global notes"
          disabled={busy || loading}
          onClick={() => void refresh()}
        >
          <RefreshCw size={14} />
        </button>
      </div>
      <Notice>
        These notes are private to this bot and shared across its channels and
        slash conversations. Channel-scoped memory is separate. Owner changes
        save immediately and cancel this bot’s active turn. Enablement and quota
        use the saved Capabilities settings.
      </Notice>
      {view && (
        <p className="mono small-text">
          <Badge>
            {view.enabled ? "Active" : "Plugin disabled · notes retained"}
          </Badge>{" "}
          {num(view.budget.used_chars)} / {num(view.budget.limit_chars)}{" "}
          characters · {num(view.budget.remaining_chars)} remaining ·{" "}
          {num(view.budget.hard_limit_chars)} with 5% headroom
        </p>
      )}
      {view?.warning && <Notice>{view.warning}</Notice>}
      {error && (
        <div className="form-error" role="alert">
          {error}
        </div>
      )}
      {message && <p role="status">{message}</p>}
      {loading && !view && <p className="muted">Loading global notes…</p>}
      {view && !view.notes.length && (
        <Empty title="No global notes yet">
          Create an owner note here, or enable the plugin and let this bot
          maintain its own cross-channel notes.
        </Empty>
      )}
      {!!view?.notes.length && (
        <div className="table-scroll">
          <table className="data-table" aria-label="Global memory notes">
            <thead>
              <tr>
                <th>Key</th>
                <th>Characters</th>
                <th>Last write channel</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {view.notes.map((note: RecordData) => (
                <tr key={note.key}>
                  <td>
                    <button
                      type="button"
                      className="text-button"
                      disabled={busy}
                      onClick={() => select(note)}
                    >
                      {note.key}
                    </button>
                  </td>
                  <td className="mono">{num([...note.value].length)}</td>
                  <td className="mono">{note.source_channel_id || "Owner"}</td>
                  <td className="mono">{dateLabel(note.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <button
        type="button"
        className="button"
        disabled={busy}
        onClick={() => select()}
      >
        <Plus size={13} /> New global note
      </button>
      <Field
        label="Global note key"
        hint="Up to 100 characters. Existing note keys are stable; create another note for a different key."
      >
        <input
          value={noteKey}
          maxLength={100}
          readOnly={originalKey !== null}
          disabled={busy}
          onChange={(event) => setNoteKey(event.target.value)}
        />
      </Field>
      <Field
        label="Global note content"
        hint={`${num([...noteValue].length)} / 8,000 characters. Keep speaker, source and date attribution. Empty text saves an empty note; deletion is explicit.`}
      >
        <textarea
          rows={10}
          value={noteValue}
          disabled={busy}
          onChange={(event) => setNoteValue(event.target.value)}
        />
      </Field>
      <div className="credential-actions">
        <button
          type="button"
          className="button primary"
          disabled={busy || !noteKey.trim() || [...noteValue].length > 8000}
          onClick={() => void change("write")}
        >
          <Save size={13} /> {busy ? "Saving…" : "Save global note"}
        </button>
        <button
          type="button"
          className="button danger"
          disabled={busy || originalKey === null}
          onClick={() => void change("delete")}
        >
          <Trash2 size={13} /> Delete global note
        </button>
      </div>
    </section>
  );
}
