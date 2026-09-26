import { Fragment, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import type { Dashboard, RecordData } from "./api";
import { Code } from "./components";

type Layer = NonNullable<Dashboard["prompt_layers"]>[number];
const stages = [
  ["instructions", "1 · Shared instructions"],
  ["bot", "2 · Personality & memory"],
  ["input", "3 · Conversation input"],
  ["exchanges", "4 · Assistant / tool exchanges & repairs"],
  ["tail", "5 · Dynamic tail & wake-up context"],
  ["compaction", "Separate request · Compaction"],
];

function selectedTemplates(
  layer: Layer,
  draft: RecordData,
  dashboard: Dashboard,
) {
  const override = draft.prompt_layer_overrides?.[layer.id];
  let ids = layer.templates.map((p) => p.id as string);
  if (override) ids = [override];
  else if (layer.id === "silence_policy")
    ids = [
      draft.allow_silence === false
        ? "runtime-silence-policy-disabled"
        : "runtime-silence-policy",
    ];
  else if (layer.id === "transcript")
    ids = [
      draft.transcript_format === "conversation"
        ? "runtime-transcript-conversation"
        : "runtime-transcript",
    ];
  else if (layer.id === "compaction_instructions")
    ids = [
      draft.transcript_format === "conversation"
        ? "runtime-compaction-instructions-conversation"
        : "runtime-compaction-instructions",
    ];
  return ids.map(
    (id) =>
      dashboard.prompts.find((p) => p.id === id) ||
      layer.templates.find((p) => p.id === id),
  );
}

function sourceValues(
  layer: Layer,
  draft: RecordData,
  dashboard: Dashboard,
): Record<string, string> {
  // The global-memory plugin supplies its own, narrower rendering values.
  if (["global_memory", "global_memory_budget"].includes(layer.id)) return {};
  return {
    persona: draft.persona || "",
    global_prompt: dashboard.settings.global_prompt || "",
    ...(["runtime_facts", "dynamic_prompt"].includes(layer.id)
      ? { dynamic_prompt: draft.dynamic_prompt || "" }
      : {}),
  };
}

function availability(
  layer: Layer,
  draft: RecordData,
  dashboard: Dashboard,
  templates: (RecordData | undefined)[],
) {
  const rule = layer.inspection;
  let blocked = "";
  if (rule.role && draft.role !== rule.role)
    blocked =
      "This bot is a council member, not Hortator. Director guidance is skipped.";
  if (rule.images_disabled && draft.allow_images !== false)
    blocked =
      "Image inputs are enabled for this bot. Images-disabled guidance is skipped.";
  if (rule.plugin) {
    const granted = (draft.enabled_plugins || []).includes(rule.plugin);
    const global = dashboard.plugins.find((p) => p.id === rule.plugin)?.enabled;
    if (!granted) blocked = `${rule.plugin} is not granted to this bot.`;
    else if (!global) blocked = `${rule.plugin} is switched off globally.`;
  }
  if ((draft.disabled_prompt_layers || []).includes(layer.id))
    return {
      label: "Disabled",
      reason: `Unchecked for this bot.${blocked ? " " + blocked : ""}`,
    };
  if (blocked) return { label: "Not applicable", reason: blocked };
  if (templates.some((p) => !p || p.runtime_layer !== layer.id))
    return {
      label: "Invalid template",
      reason:
        "The selected template is missing or has a different placement. Runtime will reject it.",
    };
  const sources = sourceValues(layer, draft, dashboard);
  const empty = (p: RecordData | undefined) =>
    !String(p?.content || "")
      .replace(/\{([a-zA-Z_][a-zA-Z_0-9]*)\}/g, (match, name) =>
        Object.hasOwn(sources, name) ? sources[name] : match,
      )
      .trim();
  if (templates.every(empty))
    return {
      label: "Empty",
      reason:
        "Template or its configured source is blank; this layer injects nothing.",
    };
  if (templates.some(empty))
    return {
      label: "Conditional",
      reason:
        "One automatic variant is blank; inclusion depends on the turn's selected variant.",
    };
  return {
    label: rule.conditional ? "Turn only" : "Eligible",
    reason: rule.when,
  };
}

function Inspection({
  layer,
  draft,
  dashboard,
}: {
  layer: Layer;
  draft: RecordData;
  dashboard: Dashboard;
}) {
  const templates = selectedTemplates(layer, draft, dashboard);
  const state = availability(layer, draft, dashboard, templates);
  const sources = sourceValues(layer, draft, dashboard);
  const sourceLabels: Record<string, string> = {
    persona: "Personality · current draft",
    dynamic_prompt: "Dynamic tail · current draft",
    global_prompt: "Shared council instructions · saved settings",
  };
  return (
    <div
      className="prompt-layer-inspection"
      id={`inspect-${layer.id}`}
      role="region"
      aria-label={`${layer.name} inspection`}
    >
      <p>
        <strong>{state.label}.</strong> {state.reason}
      </p>
      {state.reason !== layer.inspection.when && <p>{layer.inspection.when}</p>}
      <p className="muted small-text">
        {draft.prompt_layer_overrides?.[layer.id]
          ? "Bot override"
          : "Default template"}
        {templates.length > 1
          ? " · automatic variants shown below; current memory usage selects one at runtime"
          : ""}
        . Inspection uses this draft. Turn data is not loaded; Trajectory shows
        the exact sent request.
      </p>
      {templates.map((template, index) => {
        if (!template)
          return (
            <p key={index} className="danger-text">
              Selected template could not be found.
            </p>
          );
        const variables = [
          ...new Set<string>(
            Array.from(
              String(template.content).matchAll(
                /\{([a-zA-Z_][a-zA-Z_0-9]*)\}/g,
              ),
              (m) => m[1],
            ),
          ),
        ];
        const stored = dashboard.prompts.find((p) => p.id === template.id);
        return (
          <section key={template.id}>
            <p className="muted small-text">
              {template.name} · {template.role || "system"} · {template.id} ·{" "}
              {stored ? `revision ${stored.revision}` : "built-in fallback"}
            </p>
            {templates.length > 1 && (
              <p className="small-text">
                {layer.templates.find((p) => p.id === template.id)?.condition}
              </p>
            )}
            <Code
              expanded
              label={`Template text · ${template.id}`}
              value={template.content || "(empty template)"}
            />
            {variables
              .filter((name) => Object.hasOwn(sources, name))
              .map((name) => (
                <Code
                  expanded
                  key={name}
                  label={sourceLabels[name]}
                  value={sources[name] || "(empty source)"}
                />
              ))}
            {variables.some((name) => !Object.hasOwn(sources, name)) && (
              <p className="muted small-text">
                Placeholders:{" "}
                {variables
                  .filter((name) => !Object.hasOwn(sources, name))
                  .map((name) => `{${name}}`)
                  .join(", ")}
                . These are resolved from identity, configuration or turn data
                where available; unknown names remain literal.
              </p>
            )}
          </section>
        );
      })}
      <p className="muted small-text">
        Edit template text in Prompt library; edit personality and dynamic-tail
        sources above. Inspection does not save or change any configuration.
      </p>
    </div>
  );
}

export function PromptLayers({
  draft,
  dashboard,
  set,
}: {
  draft: RecordData;
  dashboard: Dashboard;
  set: (key: string, value: any) => void;
}) {
  const [opened, setOpened] = useState<string | null>(null);
  const layers = dashboard.prompt_layers || [];
  if (layers.some((layer) => !layer.inspection))
    return (
      <p role="status">
        Prompt inspection needs matching dashboard and server versions. Restart
        the updated server, then refresh this page. Your draft is unchanged.
      </p>
    );
  return (
    <fieldset className="prompt-layer-controls">
      <legend>Injected prompt layers</legend>
      <div className="inline-actions">
        <button type="button" onClick={() => set("disabled_prompt_layers", [])}>
          Enable generated layers
        </button>
        <button
          type="button"
          onClick={() =>
            set(
              "disabled_prompt_layers",
              layers.map((p) => p.id),
            )
          }
        >
          Disable generated layers
        </button>
      </div>
      <p className="muted small-text">
        Assembly order, not alphabetical. Click a name to inspect. Checked means
        allowed when applicable; it does not enable a plugin. Empty layers are
        omitted. Adjacent text with the same role may share one message.
      </p>
      {stages.map(([stage, title]) => (
        <Fragment key={stage}>
          <h4 className="prompt-stage">{title}</h4>
          {stage === "exchanges" && (
            <p className="muted small-text">
              Actual assistant/tool exchanges are protocol data inserted here on
              later rounds.
            </p>
          )}
          {stage === "tail" && (
            <p className="muted small-text">
              Follows personality, conversation input and any tool exchanges.
            </p>
          )}
          {stage === "compaction" && (
            <p className="muted small-text">
              These three layers form their own request. Disabling required
              summary/history input pauses compaction and preserves its
              checkpoint.
            </p>
          )}
          {layers
            .filter((layer) => layer.inspection.stage === stage)
            .sort((a, b) => a.inspection.position - b.inspection.position)
            .map((layer) => {
              const state = availability(
                layer,
                draft,
                dashboard,
                selectedTemplates(layer, draft, dashboard),
              );
              return (
                <Fragment key={layer.id}>
                  <div className="prompt-layer-row" data-layer={layer.id}>
                    <div className="prompt-layer-name">
                      <input
                        type="checkbox"
                        aria-label={`Include ${layer.name}`}
                        checked={
                          !(draft.disabled_prompt_layers || []).includes(
                            layer.id,
                          )
                        }
                        onChange={(e) =>
                          set(
                            "disabled_prompt_layers",
                            e.target.checked
                              ? (draft.disabled_prompt_layers || []).filter(
                                  (key: string) => key !== layer.id,
                                )
                              : [
                                  ...(draft.disabled_prompt_layers || []),
                                  layer.id,
                                ],
                          )
                        }
                      />
                      <button
                        type="button"
                        className="prompt-inspect-button"
                        aria-expanded={opened === layer.id}
                        aria-controls={`inspect-${layer.id}`}
                        onClick={() =>
                          setOpened(opened === layer.id ? null : layer.id)
                        }
                      >
                        {opened === layer.id ? (
                          <ChevronDown size={12} />
                        ) : (
                          <ChevronRight size={12} />
                        )}
                        {layer.name}
                      </button>
                    </div>
                    <span className="prompt-layer-status" title={state.reason}>
                      {state.label}
                    </span>
                    <select
                      aria-label={`Template for ${layer.name}`}
                      value={draft.prompt_layer_overrides?.[layer.id] || ""}
                      onChange={(e) => {
                        const overrides = {
                          ...(draft.prompt_layer_overrides || {}),
                        };
                        if (e.target.value)
                          overrides[layer.id] = e.target.value;
                        else delete overrides[layer.id];
                        set("prompt_layer_overrides", overrides);
                      }}
                    >
                      <option value="">Default · automatic variant</option>
                      {dashboard.prompts
                        .filter((p) => p.runtime_layer === layer.id)
                        .map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name} · {p.role || "system"}
                          </option>
                        ))}
                    </select>
                  </div>
                  {opened === layer.id && (
                    <Inspection
                      layer={layer}
                      draft={draft}
                      dashboard={dashboard}
                    />
                  )}
                </Fragment>
              );
            })}
          {stage === "instructions" && (
            <div className="prompt-shared-insertion">
              <p className="muted small-text">
                Additional shared prompts · selected order, before personality
              </p>
              {(draft.prompt_ids || []).length === 0 && (
                <p className="muted small-text">None selected.</p>
              )}
              {(draft.prompt_ids || []).map((id: string, index: number) => {
                const prompt = dashboard.prompts.find((p) => p.id === id);
                return (
                  <Code
                    key={id}
                    label={`${index + 1}. ${prompt?.name || id} · ${prompt?.role || "system"}`}
                    value={prompt?.content ?? "Missing template"}
                  />
                );
              })}
            </div>
          )}
        </Fragment>
      ))}
    </fieldset>
  );
}
