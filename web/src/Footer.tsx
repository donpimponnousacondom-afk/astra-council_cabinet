import type { Dashboard, RecordData } from "./api";
import { Field, Notice, Switch } from "./components";

export function FooterEditor({
  draft,
  dashboard,
  defaultTemplate,
  set,
}: {
  draft: RecordData;
  dashboard: Dashboard;
  defaultTemplate: string;
  set: (key: string, value: any) => void;
}) {
  const enabled = draft.footer_enabled ?? draft.role === "hortator";
  const template = draft.footer_template ?? defaultTemplate;
  const profile = dashboard.profiles.find(
    (p) => p.id === draft.model_profile_id,
  );
  const provider = dashboard.providers.find(
    (p) => p.id === profile?.provider_id,
  );
  const examples: Record<string, string> = {
    TTFT: "1858ms",
    TPS: "477.7",
    PROVIDER: provider?.name || "Provider",
    CONTEXT: `8192/${profile?.context_window || 131072}`,
    MODEL: profile?.model || "Selected model",
    "MODEL SELECTED": profile?.model || "Selected model",
    BOT: draft.name || "Bot",
  };
  const example = template
    .replace(/^-#\s+/, "")
    .replace(
      /\{\{\s*([^{}]+?)\s*\}\}/g,
      (match: string, key: string) => examples[key.toUpperCase()] ?? match,
    );

  return (
    <section
      className="footer-editor"
      aria-label="Message footer configuration"
    >
      <h3>Message footer</h3>
      <Switch
        label="Show diagnostic footer"
        checked={enabled}
        onChange={(value) => set("footer_enabled", value)}
      />
      <p className="muted">
        Append small gray Discord subtext to this bot’s messages. Enabled by
        default for Hortator; other bots start with it off.
      </p>
      <Field
        label="Footer template"
        hint="One line, up to 300 characters. The -# prefix is added automatically. Templates are literal text with the placeholders below."
      >
        <input
          value={template}
          maxLength={300}
          onChange={(event) => set("footer_template", event.target.value)}
        />
      </Field>
      <div
        className="footer-placeholders"
        aria-label="Insert footer placeholder"
      >
        {["TTFT", "TPS", "PROVIDER", "CONTEXT", "MODEL", "BOT"].map((key) => (
          <button
            type="button"
            key={key}
            onClick={() =>
              set(
                "footer_template",
                `${template}${template ? " | " : ""}{{${key}}}`,
              )
            }
          >
            {`{{${key}}}`}
          </button>
        ))}
        <button
          type="button"
          onClick={() => set("footer_template", defaultTemplate)}
        >
          Reset template
        </button>
      </div>
      <div
        className="footer-example"
        role="region"
        aria-label="Footer example preview"
      >
        <span className="eyebrow">
          Example · sample timings and token usage
        </span>
        <p>{example || "Your footer preview"}</p>
        {!enabled && (
          <span className="muted">
            Preview only — this bot’s footer is off.
          </span>
        )}
      </div>
      <dl className="footer-definitions">
        <div>
          <dt>TTFT</dt>
          <dd>
            Time from sending the final model request to its first streamed
            token, in milliseconds.
          </dd>
        </div>
        <div>
          <dt>TPS</dt>
          <dd>
            Reported output tokens per second after that first token; an
            approximate generation rate.
          </dd>
        </div>
        <div>
          <dt>CONTEXT</dt>
          <dd>
            Reported input tokens / configured context window for that request.
          </dd>
        </div>
        <div>
          <dt>MODEL / MODEL SELECTED</dt>
          <dd>The selected model identifier. Both placeholders work.</dd>
        </div>
        <div>
          <dt>PROVIDER / BOT</dt>
          <dd>The configured provider name / this bot’s display name.</dd>
        </div>
      </dl>
      <Notice>
        Missing measurements appear as —. Commands and incident notices have no
        model timing; buffered responses have no TTFT or streaming TPS. Earlier
        tool rounds, provider queue time and delivery cooldowns are excluded. No
        extra model calls are made. Save to apply this bot’s settings.
      </Notice>
    </section>
  );
}
