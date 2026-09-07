import { useState } from "react";
import type { RecordData } from "./api";
import { Code, Field, Notice } from "./components";

const fields = [
  ["reasoning_effort", "effort"],
  ["reasoning.effort", "effort"],
  ["reasoning.enabled", "boolean"],
  ["reasoning.max_tokens", "number"],
  ["chat_template_kwargs.enable_thinking", "boolean"],
  ["chat_template_kwargs.thinking", "boolean"],
  ["chat_template_kwargs.do_reasoning", "boolean"],
  ["chat_template_kwargs.thinking_budget", "number"],
  ["chat_template_kwargs.preserve_thinking", "boolean"],
  ["chat_template_kwargs.clear_thinking", "boolean"],
  ["thinking.type", "type"],
  ["thinking.budget_tokens", "number"],
  ["thinking", "boolean"],
] as const;

const object = (value: unknown): value is RecordData =>
  value !== null && typeof value === "object" && !Array.isArray(value);

// Inspect vendor JSON without claiming to know a remote model's default or precedence.
export function reasoningFields(value: RecordData): RecordData {
  return Object.fromEntries(
    Object.entries(value).flatMap(([key, child]) => {
      if (/reason|think/i.test(key)) return [[key, child]];
      const nested = object(child) ? reasoningFields(child) : {};
      return Object.keys(nested).length ? [[key, nested]] : [];
    }),
  );
}

export function reasoningSummary(value: RecordData): string {
  const entries = Object.entries(reasoningFields(value));
  if (!entries.length) return "No explicit reasoning override";
  return entries
    .map(([key, child]) => `${key}: ${JSON.stringify(child)}`)
    .join(" · ");
}

function read(value: RecordData, path: string): unknown {
  return path
    .split(".")
    .reduce<unknown>(
      (node, key) =>
        object(node) && Object.hasOwn(node, key) ? node[key] : undefined,
      value,
    );
}

function parentConflict(value: RecordData, path: string): boolean {
  const keys = path.split(".");
  let node: unknown = value;
  for (const key of keys.slice(0, -1)) {
    if (!object(node)) return true;
    node = node[key];
    if (node === undefined) return false;
  }
  return !object(node);
}

function update(value: RecordData, keys: string[], next: unknown): RecordData {
  const [key, ...rest] = keys;
  const result = { ...value };
  if (rest.length) {
    const child = update(result[key] || {}, rest, next);
    if (Object.keys(child).length) result[key] = child;
    else delete result[key];
  } else if (next === undefined) delete result[key];
  else result[key] = next;
  return result;
}

export function ReasoningEditor({
  value,
  onChange,
  providerUrl,
  disabled = false,
}: {
  value: RecordData;
  onChange: (value: RecordData) => void;
  providerUrl: string;
  disabled?: boolean;
}) {
  const [path, setPath] = useState<string>(() => {
    const existing = fields.find(([key]) => read(value, key) !== undefined);
    if (existing) return existing[0];
    let featherless = false;
    try {
      featherless = new URL(providerUrl).hostname === "api.featherless.ai";
    } catch {
      /* The provider editor validates URLs on save. */
    }
    return featherless
      ? "chat_template_kwargs.enable_thinking"
      : "reasoning_effort";
  });
  const kind = fields.find(([key]) => key === path)![1];
  const current = read(value, path);
  const conflict =
    parentConflict(value, path) || object(current) || Array.isArray(current);
  const choices: [string, unknown][] =
    kind === "boolean"
      ? [
          ["On", true],
          ["Off", false],
        ]
      : kind === "type"
        ? ["enabled", "disabled", "adaptive"].map((s) => [s, s])
        : ["none", "minimal", "low", "medium", "high", "xhigh", "max"].map(
            (s) => [s, s],
          );
  const custom =
    current !== undefined && !choices.some(([, v]) => v === current);
  const set = (next: unknown) => onChange(update(value, path.split("."), next));
  return (
    <section className="reasoning-editor" aria-label="Reasoning configuration">
      <h3>Reasoning</h3>
      <p className="muted small-text">
        Reasoning belongs to this model profile. Unset fields send no override;
        the model service chooses its behavior. There is no provider-level
        reasoning setting. Only use fields and values supported by your model.
      </p>
      <div className="form-grid">
        <Field
          label="Reasoning request field"
          hint="These controls edit the exact JSON below. Changing the selected field alone does not change a request."
        >
          <select
            value={path}
            onChange={(e) => setPath(e.target.value)}
            disabled={disabled}
          >
            {fields.map(([key]) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        </Field>
        {kind === "number" ? (
          <Field
            label="Reasoning token budget"
            hint="Leave empty to omit this field. Custom values remain editable in Model parameters."
          >
            <input
              type="number"
              step="any"
              placeholder="Unset"
              disabled={
                disabled ||
                conflict ||
                (current !== undefined && typeof current !== "number")
              }
              value={typeof current === "number" ? current : ""}
              onChange={(e) =>
                set(e.target.value === "" ? undefined : Number(e.target.value))
              }
            />
          </Field>
        ) : (
          <Field
            label={kind === "effort" ? "Reasoning effort" : "Thinking mode"}
            hint="Support and available levels depend on the endpoint and model; no value is translated into another vendor's format."
          >
            <select
              value={current === undefined ? "" : JSON.stringify(current)}
              disabled={disabled || conflict}
              onChange={(e) =>
                set(
                  e.target.value === ""
                    ? undefined
                    : JSON.parse(e.target.value),
                )
              }
            >
              <option value="">Unset · omit this field</option>
              {choices.map(([label, v]) => (
                <option key={label} value={JSON.stringify(v)}>
                  {label}
                </option>
              ))}
              {custom && (
                <option value={JSON.stringify(current)}>
                  Custom JSON value
                </option>
              )}
            </select>
          </Field>
        )}
      </div>
      {conflict && (
        <Notice>
          That field or its parent has a custom JSON structure. Edit Model
          parameters to change its structure.
        </Notice>
      )}
      {disabled && (
        <Notice>
          Complete the valid Model parameters JSON before using these controls.
        </Notice>
      )}
      <p className="muted small-text">
        Every override below is sent. Other reasoning fields and vendor options
        are preserved when you edit one field.
      </p>
      <Code
        value={reasoningFields(value)}
        label="Reasoning fields sent"
        expanded
      />
    </section>
  );
}
