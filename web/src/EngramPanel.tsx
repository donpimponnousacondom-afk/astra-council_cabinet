import { useEffect, useRef, useState } from "react";
import { api, dateLabel } from "./api";
import type { RecordData } from "./api";
import { Code, Field, Notice } from "./components";
import { alphabetical } from "./ordering";

type EngramState = {
  channel_id: string;
  revision: number;
  epoch: string;
  covered_through: number;
  summary_checkpoint: number;
  summary_hash: string;
  MEM: string;
  FACTS: string;
  state_chars: number;
  state_tokens: number;
  updated_at: number;
  source_turn_id: string | null;
  source_request_id: string | null;
};

type EngramInspection = {
  enabled: boolean;
  config: RecordData;
  states: EngramState[];
  pending_candidates: number;
};

export function EngramPanel({
  bot,
  rooms,
  disabled,
  onBusyChange,
}: {
  bot: RecordData;
  rooms: RecordData[];
  disabled: boolean;
  onBusyChange: (id: string, busy: boolean) => void;
}) {
  const [opened, setOpened] = useState(false);
  const [data, setData] = useState<EngramInspection | null>(null);
  const [channel, setChannel] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState("");
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const mounted = useRef(false);
  const pending = useRef(false);
  const operationId = `engram-reset:${bot.id}`;
  const path = `/api/engrams/${encodeURIComponent(bot.id)}`;
  const channelName = (id: string) =>
    rooms.find((room) => room.channel_id === id)?.name || id;
  const channels = alphabetical(
    [
      ...new Set<string>([
        ...(bot.contexts || []).map(
          (context: RecordData) => context.channel_id,
        ),
        ...(data?.states || []).map((state) => state.channel_id),
      ]),
    ],
    channelName,
    (id) => id,
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      onBusyChange(operationId, false);
    };
  }, [operationId, onBusyChange]);

  useEffect(() => {
    if (!opened) return;
    let live = true;
    const controller = new AbortController();
    setLoading(true);
    api<EngramInspection>(path, { signal: controller.signal })
      .then((value) => {
        if (live) {
          setData(value);
          setError("");
        }
      })
      .catch((e) => {
        if (live) setError(String(e));
      })
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [opened, path, refresh]);

  async function reset() {
    if (pending.current || disabled || confirmation !== bot.id) return;
    const scope = channel
      ? `${channelName(channel)} (${channel})`
      : "all channels";
    if (
      !window.confirm(
        `Reset engram memory for ${bot.name} in ${scope}? Active work will be cancelled. Stored MEM/FACTS and coverage will be cleared; history becomes eligible again from the existing compaction checkpoint. Private notes, conversation summaries and raw history are preserved. There is no undo button.`,
      )
    )
      return;
    pending.current = true;
    setBusy(true);
    setError("");
    setReceipt("");
    onBusyChange(operationId, true);
    try {
      await api(`${path}/reset`, {
        method: "POST",
        body: JSON.stringify({
          confirm_bot_id: confirmation,
          ...(channel ? { channel_id: channel } : {}),
        }),
      });
      if (mounted.current) {
        setConfirmation("");
        setReceipt(`Engram memory reset for ${scope}.`);
        setRefresh((value) => value + 1);
      }
    } catch (e) {
      if (mounted.current)
        setError(e instanceof Error ? e.message : "Engram reset failed");
    } finally {
      pending.current = false;
      if (mounted.current) {
        setBusy(false);
        onBusyChange(operationId, false);
      }
    }
  }

  return (
    <details
      className="advanced"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary>Engram memory · experimental</summary>
      {opened && (
        <section aria-label="Engram state inspection">
          <p className="muted small-text">
            Saved state for this bot's ordinary conversations remains
            inspectable while the plugin is off. Opening or refreshing does not
            call a model.
          </p>
          <div className="engram-actions">
            <button
              type="button"
              disabled={busy || loading}
              onClick={() => setRefresh((value) => value + 1)}
            >
              {loading ? "Loading engrams…" : "Refresh engrams"}
            </button>
            {data && (
              <span className="muted small-text">
                {data.enabled ? "Enabled" : "Disabled"} ·{" "}
                {data.pending_candidates} pending candidates
              </span>
            )}
          </div>
          <Field label="Engram scope">
            <select
              disabled={busy}
              value={channel}
              onChange={(e) => {
                setChannel(e.target.value);
                setConfirmation("");
                setReceipt("");
              }}
            >
              <option value="">All channels</option>
              {channels.map((id) => (
                <option key={id} value={id}>
                  {channelName(id)}
                  {channelName(id) !== id ? ` · ${id}` : ""}
                </option>
              ))}
            </select>
          </Field>
          {data &&
            data.states
              .filter((state) => !channel || state.channel_id === channel)
              .map((state) => (
                <section key={state.channel_id}>
                  <h4>{channelName(state.channel_id)}</h4>
                  <p className="muted small-text">
                    {state.channel_id} · revision {state.revision} ·{" "}
                    {state.state_chars} characters · {state.state_tokens} tokens
                    · {dateLabel(state.updated_at)}
                  </p>
                  <Code value={state.MEM || "(empty)"} label="MEM" />
                  <Code value={state.FACTS || "(empty)"} label="FACTS" />
                  <Code
                    label="Engram coverage and source"
                    value={{
                      epoch: state.epoch,
                      covered_through: state.covered_through,
                      summary_checkpoint: state.summary_checkpoint,
                      summary_hash: state.summary_hash,
                      source_turn_id: state.source_turn_id,
                      source_request_id: state.source_request_id,
                    }}
                  />
                </section>
              ))}
          {data &&
            !data.states.some(
              (state) => !channel || state.channel_id === channel,
            ) && <p className="muted">No saved engram state in this scope.</p>}
          <p className="danger-text">
            Reset cancels this bot's active work and clears engram memory and
            coverage in the selected scope. Conversation summaries, private
            notes and raw history survive. This changes live state immediately.
          </p>
          <Field
            label="Confirm engram reset bot ID"
            hint={`Type ${bot.id} exactly to enable the reset.`}
          >
            <input
              value={confirmation}
              disabled={busy}
              onChange={(e) => setConfirmation(e.target.value)}
              autoComplete="off"
            />
          </Field>
          {disabled && (
            <Notice>
              Save or discard configuration drafts before resetting engrams.
            </Notice>
          )}
          <button
            type="button"
            className="button danger"
            disabled={
              disabled || busy || loading || !data || confirmation !== bot.id
            }
            onClick={reset}
          >
            {busy ? "Resetting engrams…" : "Reset engram memory"}
          </button>
          {error && (
            <p role="alert" className="danger-text">
              {error}
            </p>
          )}
          {receipt && <p role="status">{receipt}</p>}
        </section>
      )}
    </details>
  );
}
