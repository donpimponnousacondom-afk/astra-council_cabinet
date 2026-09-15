import { useRef, useState } from "react";
import { control, dateLabel } from "./api";
import type { RecordData } from "./api";
import { Field, Notice } from "./components";

export function BotControl({
  bot,
  dirty,
  onBusyChange,
}: {
  bot: RecordData;
  dirty: boolean;
  onBusyChange: (id: string, pending: boolean) => void;
}) {
  const [channel, setChannel] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [receipt, setReceipt] = useState<RecordData | null>(null);
  const [error, setError] = useState("");
  const pending = useRef(false);
  async function reset() {
    if (pending.current || dirty || confirmation !== bot.id) return;
    if (
      !window.confirm(
        `Forget all messages before now for ${bot.name} in ${channel || "all channels"}? Active work will be cancelled and retained summaries cleared. Memories and other bots are unchanged. There is no undo button.`,
      )
    )
      return;
    pending.current = true;
    onBusyChange("context-reset", true);
    setError("");
    try {
      setReceipt(
        await control("reset_context", "bots", bot.id, {
          confirm_bot_id: confirmation,
          ...(channel ? { channel_id: channel } : {}),
        }),
      );
      setConfirmation("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Context reset failed");
    } finally {
      pending.current = false;
      onBusyChange("context-reset", false);
    }
  }
  return (
    <section className="bot-reset-panel">
      <h3>Clean slate</h3>
      {(bot.context_resets || []).map((boundary: RecordData) => (
        <p className="muted" key={boundary.channel_id}>
          History hidden before {dateLabel(boundary.after_at)} ·{" "}
          {boundary.channel_id === "*" ? "all channels" : boundary.channel_id}
        </p>
      ))}
      <Notice>
        Cancel this bot's active work, erase its retained conversation summaries
        and ignore messages from before this moment. Reconnect history and old
        reply previews stay excluded. Other bots and Discord history are
        unaffected. New messages are admitted normally; explicitly quoted or
        fetched old material can still be reintroduced.
      </Notice>
      <p className="danger-text">
        This changes live state immediately and has no undo button. Memories,
        files, prompts, provider settings and credentials remain unchanged.
        Messages already sent cannot be recalled.
      </p>
      <Field label="Reset scope">
        <select value={channel} onChange={(e) => setChannel(e.target.value)}>
          <option value="">All channels, including future assignments</option>
          {(bot.contexts || []).map((c: RecordData) => (
            <option key={c.channel_id} value={c.channel_id}>
              {c.channel_id}
            </option>
          ))}
        </select>
      </Field>
      <p className="muted">
        Memories survive and continue to enter the prompt. Manage them
        separately in the memory controls. Historical evidence and saved files
        also survive.
      </p>
      <Field
        label="Confirm bot ID"
        hint={`Type ${bot.id} exactly to enable the reset.`}
      >
        <input
          value={confirmation}
          onChange={(e) => setConfirmation(e.target.value)}
          autoComplete="off"
        />
      </Field>
      {dirty && (
        <Notice>
          Save or discard configuration drafts before resetting live context.
        </Notice>
      )}
      <button
        type="button"
        className="button danger"
        disabled={dirty || confirmation !== bot.id}
        onClick={reset}
      >
        Forget everything before now
      </button>
      {error && (
        <p role="alert" className="danger-text">
          {error}
        </p>
      )}
      {receipt && (
        <p role="status">
          Clean slate set at {dateLabel(receipt.after_at)}. Memories and other
          bots are unchanged.
        </p>
      )}
    </section>
  );
}
