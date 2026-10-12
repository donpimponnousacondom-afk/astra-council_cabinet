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
            label="Failed responses allowed"
            hint="1–5; default 2. Two allows one failure, then one confirmation chance."
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
            A response that cannot finalize correctly counts one strike. At the
            limit the cycle stops; no extra attempt is added. With a limit of 1,
            the first failure stops the cycle. Tool-only action responses do not
            count as strikes; ordinary tool budgets still apply. A valid
            end_cycle before any answer exists is also allowed and is not a
            strike, even with a limit of 1.
          </Help>
          <Help label="How saved-draft confirmation works">
            A failed response with useful answer text is kept unchanged. Action
            tools are removed; the bot can only confirm the original draft with
            end_cycle, or choose silence if allowed. It must not regenerate the
            answer. New text cannot replace the draft. If a finishing companion
            fails, completed actions stay saved and the cycle stops incomplete.
          </Help>
          <Help label="When the finish call comes first">
            With no answer or saved draft, a valid end_cycle permits one request
            for the first public answer. That request has no tools and accepts
            text only. Empty text, a tool call or incomplete output stops the
            cycle without retry. Permitted companion writes run once before this
            request; a failed companion stops it. Existing drafts are never
            regenerated.
          </Help>
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
