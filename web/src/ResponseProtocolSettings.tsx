import type { ReactNode } from "react";
import type { RecordData } from "./api";
import { Field } from "./components";

function Help({ label, children }: { label: string; children: ReactNode }) {
  return (
    <details className="reply-help">
      <summary>{label}</summary>
      <div>{children}</div>
    </details>
  );
}

export function ResponseProtocolSettings({
  draft,
  set,
}: {
  draft: RecordData;
  set: (key: string, value: any) => void;
}) {
  const mode = draft.response_protocol ?? "narrate";
  const summary =
    mode === "end_cycle"
      ? "The bot must finish with end_cycle before its answer is posted."
      : mode === "progress"
        ? "Post limited progress updates while tools run, then a final reply."
        : "Keep tool-round commentary private and post the final reply.";

  return (
    <fieldset className="reply-protocol">
      <legend>Reply behavior</legend>
      <Field label="Response protocol" hint={summary}>
        <select
          value={mode}
          onChange={(event) => set("response_protocol", event.target.value)}
        >
          <option value="narrate">Final reply only · narrate</option>
          <option value="progress">Progress updates · progress</option>
          <option value="end_cycle">Explicit finish · end_cycle</option>
        </select>
      </Field>

      {mode === "progress" && (
        <div className="reply-mode-settings">
          <Field
            label="Progress message attempts per turn"
            hint="0–20; default 1. Zero keeps all progress updates private."
          >
            <input
              type="number"
              min={0}
              max={20}
              step={1}
              required
              value={
                draft.max_progress_messages_per_turn === undefined
                  ? 1
                  : (draft.max_progress_messages_per_turn ?? "")
              }
              onChange={(event) =>
                set(
                  "max_progress_messages_per_turn",
                  event.target.value === "" ? null : Number(event.target.value),
                )
              }
            />
          </Field>
          <Help label="How the progress limit works">
            Failed sends count toward this limit; tools still continue. Extra
            updates stay private, with a notice to the model. This limit does
            not affect the final reply or add tool rounds.
          </Help>
        </div>
      )}

      {mode === "end_cycle" && (
        <div className="reply-mode-settings">
          <Field
            label="Narration strikes before finalization"
            hint="1–5; default 2. At the limit, allow one final answer attempt."
          >
            <input
              type="number"
              min={1}
              max={5}
              step={1}
              required
              value={
                draft.narration_strikes === undefined
                  ? 2
                  : (draft.narration_strikes ?? "")
              }
              onChange={(event) =>
                set(
                  "narration_strikes",
                  event.target.value === "" ? null : Number(event.target.value),
                )
              }
            />
          </Field>
          <Help label="What counts as a strike?">
            Text alongside action tools without end_cycle is withheld, the tools
            run, and one strike is counted. At the limit, the bot gets one more
            round to finish its answer or choose silence if allowed. No action
            tools can run in that final round. Tool-only responses do not count
            as strikes; ordinary tool budgets still apply.
          </Help>
          <div className="reply-option">
            <label>
              <input
                type="checkbox"
                aria-label="Publish the previous draft"
                checked={draft.publish_retained_draft !== false}
                onChange={(event) =>
                  set("publish_retained_draft", event.target.checked)
                }
              />
              <span>
                <strong>Publish the previous draft</strong>
                <small>
                  Use the withheld draft if the bot finishes without repeating
                  its text.
                </small>
              </span>
            </label>
            <Help label="About the previous draft">
              Default on. A text-only response is kept privately for one final
              round. If the next response calls end_cycle without text, that
              draft can be published. Turn this off to require the bot to repeat
              its answer alongside end_cycle. New text with end_cycle replaces
              the draft.
            </Help>
          </div>
          <div className="reply-option">
            <label>
              <input
                type="checkbox"
                aria-label="Notify when no answer is sent"
                checked={draft.notify_incomplete_cycle === true}
                onChange={(event) =>
                  set("notify_incomplete_cycle", event.target.checked)
                }
              />
              <span>
                <strong>Notify when no answer is sent</strong>
                <small>
                  Post a short Discord notice when the cycle ends incomplete.
                </small>
              </span>
            </label>
            <Help label="About incomplete-cycle notices">
              Default off. At most one notice is sent per incomplete cycle.
              After three consecutive incomplete cycles in a channel, notices
              pause until the bot answers or chooses silence there. Intentional
              silence is not an incomplete cycle.
            </Help>
          </div>
        </div>
      )}
    </fieldset>
  );
}
