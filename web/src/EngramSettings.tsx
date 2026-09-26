import { Field, Notice, Switch } from "./components";

type EngramConfiguration = {
  state_char_limit?: number | null;
  state_token_limit?: number | null;
  recent_messages?: number | null;
  reduce_history?: boolean;
  [key: string]: unknown;
};

const limits = [
  {
    key: "state_char_limit",
    label: "Engram memory limit (characters)",
    fallback: 8000,
    max: 128000,
    hint: "Combined retained MEM and FACTS text; 1–128,000 characters.",
  },
  {
    key: "state_token_limit",
    label: "Engram memory limit (tokens)",
    fallback: 2048,
    max: 32000,
    hint: "Combined retained MEM and FACTS text, measured with cl100k_base; 1–32,000 tokens. Independent of the provider output cap.",
  },
  {
    key: "recent_messages",
    label: "Recent acknowledged messages",
    fallback: 12,
    max: 1000,
    hint: "1–1,000 messages. Used only when history reduction is enabled.",
  },
] as const;

function EngramFields({
  config,
  inherited,
  onChange,
}: {
  config: EngramConfiguration;
  inherited?: EngramConfiguration;
  onChange: (value: EngramConfiguration) => void;
}) {
  const recent = config.recent_messages ?? inherited?.recent_messages ?? 12;
  return (
    <>
      <div className="form-grid">
        {limits.map(({ key, label, fallback, max, hint }) => {
          const inheritedValue = inherited?.[key] ?? fallback;
          return (
            <Field
              key={key}
              label={label}
              hint={
                inherited
                  ? `${hint} Leave empty to inherit ${inheritedValue}.`
                  : hint
              }
            >
              <input
                type="number"
                min={1}
                max={max}
                step={1}
                required={!inherited}
                value={
                  inherited
                    ? (config[key] ?? "")
                    : config[key] === undefined
                      ? fallback
                      : (config[key] ?? "")
                }
                placeholder={
                  inherited ? `Inherit ${inheritedValue}` : undefined
                }
                onChange={(e) => {
                  const next = { ...config };
                  if (inherited && e.target.value === "") delete next[key];
                  else
                    next[key] =
                      e.target.value === "" ? null : Number(e.target.value);
                  onChange(next);
                }}
              />
            </Field>
          );
        })}
      </div>
      {inherited ? (
        <Field label="History reduction for this bot">
          <select
            value={
              config.reduce_history === undefined
                ? ""
                : String(config.reduce_history)
            }
            onChange={(e) => {
              const next = { ...config };
              if (e.target.value === "") delete next.reduce_history;
              else next.reduce_history = e.target.value === "true";
              onChange(next);
            }}
          >
            <option value="">
              Inherit · {inherited.reduce_history ? "on" : "off"}
            </option>
            <option value="false">Off · capture and inject only</option>
            <option value="true">On · reduce acknowledged history</option>
          </select>
        </Field>
      ) : (
        <Switch
          label="Reduce history after acknowledged engrams"
          checked={config.reduce_history ?? false}
          onChange={(value) => onChange({ ...config, reduce_history: value })}
        />
      )}
      <Notice>
        History reduction is a separate opt-in. Off lets you test capture and
        injection with the normal conversation history. On retains the last{" "}
        {recent} acknowledged messages plus all new or uncovered messages. Raw
        history stays stored. Editing these settings does not enable the plugin
        or grant it to a bot.
      </Notice>
      <p className="muted small-text">
        Ordinary conversations only; slash commands and panels are excluded.
        Edit Engram instructions in Prompt library; choose a per-bot template in
        Prompts → Injected prompt layers.
      </p>
    </>
  );
}

export function EngramSettings({
  config,
  onChange,
}: {
  config: EngramConfiguration;
  onChange: (value: EngramConfiguration) => void;
}) {
  return <EngramFields config={config} onChange={onChange} />;
}

export function EngramBotSettings({
  config,
  inherited,
  onChange,
}: {
  config: EngramConfiguration;
  inherited: EngramConfiguration;
  onChange: (value: EngramConfiguration) => void;
}) {
  return (
    <section>
      <h3>Engram memory · experimental</h3>
      <EngramFields config={config} inherited={inherited} onChange={onChange} />
    </section>
  );
}
