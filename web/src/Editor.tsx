import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  ArrowRight,
  Copy,
  ExternalLink,
  KeyRound,
  Layers3,
  LockKeyhole,
  Save,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { api, control, credential, dateLabel, kindLabel, num } from "./api";
import type { Dashboard, Kind, RecordData } from "./api";
import { ReasoningEditor, reasoningFields } from "./Reasoning";
import { FooterEditor } from "./Footer";
import { PricingEditor } from "./Pricing";
import { GlobalMemoryPanel } from "./GlobalMemory";
import { SlashCommandSetup } from "./SlashCommands";
import {
  DocumentBotSettings,
  DocumentPluginSettings,
  DocumentSitesPanel,
} from "./Documents";
import {
  AgentToolSettings,
  AgentToolsPanel,
  WorkTaskSettings,
  agentToolIds,
} from "./AgentTools";
import {
  Badge,
  Code,
  Empty,
  Field,
  JsonInput,
  jsonValidityEvent,
  Modal,
  Notice,
  Switch,
} from "./components";

const navigationEvent = "hortator:before-editor-close";
const invalidSectionMessage =
  "Fix the invalid JSON before changing sections. Close this editor if you want to discard that draft.";

/** Let the mounted editor protect an unsaved draft before changing workbench panes. */
export function confirmEditorNavigation(): boolean {
  return window.dispatchEvent(new Event(navigationEvent, { cancelable: true }));
}

function draftContent(value: RecordData): string {
  return JSON.stringify(
    Object.fromEntries(
      Object.entries(value).filter(([key]) => key !== "revision"),
    ),
  );
}

function EditorSection({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="editor-section" data-editor-section={id}>
      <h3 className="editor-section-heading">{title}</h3>
      {children}
    </section>
  );
}

const sectionLinks: Partial<Record<Kind, [string, string][]>> = {
  providers: [
    ["identity", "Identity"],
    ["connection", "Connection"],
    ["limits", "Limits"],
    ["authentication", "Credentials"],
    ["json", "JSON"],
  ],
  profiles: [
    ["identity", "Identity"],
    ["model", "Model"],
    ["context", "Context"],
    ["request", "Generation"],
    ["compaction", "Compaction"],
    ["pricing", "Pricing"],
    ["json", "JSON"],
  ],
  prompts: [
    ["identity", "Identity"],
    ["instructions", "Instructions"],
    ["json", "JSON"],
  ],
  plugins: [
    ["identity", "Identity"],
    ["capabilities", "Configuration"],
    ["json", "JSON"],
  ],
  rooms: [
    ["identity", "Identity"],
    ["channel", "Channel"],
    ["participation", "Participants"],
    ["json", "JSON"],
  ],
  settings: [
    ["identity", "Identity"],
    ["scope", "Discord scope"],
    ["instructions", "Prompt"],
    ["runtime", "Runtime"],
    ["json", "JSON"],
  ],
};

function initial(
  kind: Kind,
  entity: RecordData | undefined,
  dashboard: Dashboard,
  schemas: RecordData,
) {
  const properties = schemas[kind]?.properties || {};
  if (entity)
    return Object.fromEntries(
      Object.entries(entity).filter(
        ([key]) => key === "revision" || key in properties,
      ),
    );
  const result: RecordData = {};
  for (const [key, prop] of Object.entries<RecordData>(properties))
    if ("default" in prop) result[key] = prop.default;
  result.id = `${kind === "profiles" ? "model" : kind.slice(0, -1)}_${crypto.randomUUID().slice(0, 8)}`;
  result.name = "";
  if (kind === "bots") {
    Object.assign(result, {
      model_profile_id: dashboard.profiles[0]?.id || "",
      room_ids: dashboard.rooms.length ? [dashboard.rooms[0].id] : [],
      prompt_ids: [],
      enabled_plugins: [],
      plugin_config: {},
    });
    // Until explicitly chosen, a new bot's footer follows its selected role.
    delete result.footer_enabled;
  }
  if (kind === "profiles")
    Object.assign(result, {
      provider_id: dashboard.providers[0]?.id || "",
      model: "",
      request_json: { temperature: 0.8, max_tokens: 2048 },
    });
  if (kind === "providers")
    Object.assign(result, {
      base_url: "https://openrouter.ai/api/v1",
      headers: {},
    });
  return result;
}

type Props = {
  kind: Kind;
  entity?: RecordData;
  dashboard: Dashboard;
  schemas: RecordData;
  close: () => void;
  saved: (value: RecordData, keepOpen?: boolean) => void | Promise<void>;
  notify: (text: string, error?: boolean) => void;
};
export function Editor({
  kind,
  entity,
  dashboard,
  schemas,
  close,
  saved,
  notify,
}: Props) {
  const [draft, setDraft] = useState<RecordData>(() =>
    initial(kind, entity, dashboard, schemas),
  );
  const [baseline, setBaseline] = useState(() => draftContent(draft));
  const form = useRef<HTMLFormElement>(null);
  const [pendingCredentials, setPendingCredentials] = useState<
    Record<string, boolean>
  >({});
  const [globalNoteDirty, setGlobalNoteDirty] = useState(false);
  const credentialDraftChanged = useCallback((id: string, pending: boolean) => {
    setPendingCredentials((previous) =>
      previous[id] === pending ? previous : { ...previous, [id]: pending },
    );
  }, []);
  const operations = useRef(new Set<string>());
  const [operationCount, setOperationCount] = useState(0);
  const operationChanged = useCallback((id: string, pending: boolean) => {
    if (pending) operations.current.add(id);
    else operations.current.delete(id);
    setOperationCount(operations.current.size);
  }, []);
  const busy = operationCount > 0;
  const setBusy = (pending: boolean) =>
    operationChanged("configuration", pending);
  const [error, setError] = useState("");
  const [modelJsonValid, setModelJsonValid] = useState(true);
  const [invalidJsonDraft, setInvalidJsonDraft] = useState(false);
  const [tab, setTab] = useState(
    kind === "bots" && entity && !entity.token_configured
      ? "discord"
      : "identity",
  );
  const [activeSection, setActiveSection] = useState("identity");
  const dirty =
    baseline !== draftContent(draft) ||
    Object.values(pendingCredentials).some(Boolean) ||
    globalNoteDirty ||
    invalidJsonDraft ||
    !modelJsonValid;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const updateJsonDraftState = useCallback(() => {
    const invalid = !!form.current?.querySelector(".json-input:invalid");
    setInvalidJsonDraft(invalid);
    if (!invalid)
      setError((previous) =>
        previous === invalidSectionMessage ? "" : previous,
      );
  }, []);
  useEffect(updateJsonDraftState, [draft, tab, updateJsonDraftState]);
  useEffect(() => {
    const currentForm = form.current;
    currentForm?.addEventListener(jsonValidityEvent, updateJsonDraftState);
    return () =>
      currentForm?.removeEventListener(jsonValidityEvent, updateJsonDraftState);
  }, [updateJsonDraftState]);
  useEffect(() => {
    const hasChanges = () =>
      dirtyRef.current || !!form.current?.querySelector(".json-input:invalid");
    const protectNavigation = (event: Event) => {
      if (operations.current.size) {
        event.preventDefault();
        setError(
          "Wait for the current save or action to finish before leaving this editor.",
        );
        return;
      }
      if (
        hasChanges() &&
        !window.confirm("Discard unsaved changes in this editor?")
      )
        event.preventDefault();
    };
    const protectUnload = (event: BeforeUnloadEvent) => {
      if (hasChanges() || operations.current.size) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    const saveShortcut = (event: KeyboardEvent) => {
      if (
        (event.ctrlKey || event.metaKey) &&
        !event.altKey &&
        event.key.toLowerCase() === "s"
      ) {
        event.preventDefault();
        if (!form.current?.querySelector('button[type="submit"]:disabled'))
          form.current?.requestSubmit();
      }
    };
    window.addEventListener(navigationEvent, protectNavigation);
    window.addEventListener("beforeunload", protectUnload);
    window.addEventListener("keydown", saveShortcut);
    return () => {
      window.removeEventListener(navigationEvent, protectNavigation);
      window.removeEventListener("beforeunload", protectUnload);
      window.removeEventListener("keydown", saveShortcut);
    };
  }, []);
  const requestClose = () => {
    if (confirmEditorNavigation()) close();
  };
  const jumpToSection = (id: string) => {
    const section = form.current?.querySelector<HTMLElement>(
      `[data-editor-section="${id}"]`,
    );
    if (!section) return;
    if (section instanceof HTMLDetailsElement) section.open = true;
    section.scrollIntoView({ block: "start", behavior: "instant" });
    setActiveSection(id);
  };
  const set = (key: string, value: any) =>
    setDraft((d) => ({ ...d, [key]: value }));
  const text = (key: string, label: string, hint?: string, type = "text") => (
    <Field label={label} hint={hint}>
      <input
        type={type}
        value={draft[key] ?? ""}
        onChange={(e) => set(key, e.target.value)}
        required={["name", "model", "base_url"].includes(key)}
      />
    </Field>
  );
  const numeric = (
    key: string,
    label: string,
    hint?: string,
    step: number | string = 1,
  ) => (
    <Field label={label} hint={hint}>
      <input
        type="number"
        step={step}
        min={schemas[kind]?.properties?.[key]?.minimum}
        max={schemas[kind]?.properties?.[key]?.maximum}
        required={
          key === "memory_char_limit" || key === "global_memory_char_limit"
        }
        value={draft[key] ?? ""}
        onChange={(e) =>
          set(key, e.target.value === "" ? null : Number(e.target.value))
        }
      />
    </Field>
  );
  const select = (
    key: string,
    label: string,
    items: { id: string; name: string }[],
    hint?: string,
  ) => (
    <Field label={label} hint={hint}>
      <select
        value={draft[key] ?? ""}
        onChange={(e) => set(key, e.target.value)}
        required
      >
        <option value="" disabled>
          Select…
        </option>
        {items.map((i) => (
          <option key={i.id} value={i.id}>
            {i.name}
          </option>
        ))}
      </select>
    </Field>
  );
  const checkedList = (key: string, items: RecordData[], label: string) => (
    <fieldset className="check-list">
      <legend>{label}</legend>
      {items.map((item) => (
        <label key={item.id}>
          <input
            type="checkbox"
            checked={(draft[key] || []).includes(item.id)}
            onChange={(e) =>
              set(
                key,
                e.target.checked
                  ? [...(draft[key] || []), item.id]
                  : (draft[key] || []).filter((id: string) => id !== item.id),
              )
            }
          />
          <span>
            <strong>{item.name}</strong>
            <small>{item.description || item.id}</small>
          </span>
          {item.enabled === false && <Badge>Globally off</Badge>}
        </label>
      ))}
      {!items.length && (
        <p className="muted small-text">No entries in this registry yet.</p>
      )}
    </fieldset>
  );
  const credentialChanged = async () => {
    const latest = await api(`/api/config/${kind}/${entity!.id}`);
    setDraft((previous) => ({
      ...previous,
      revision: latest.revision,
      ...(kind === "bots" && !previous.application_id
        ? { application_id: latest.application_id }
        : {}),
    }));
    if (kind === "bots" && !draft.application_id && latest.application_id) {
      setBaseline((previous) =>
        draftContent({
          ...JSON.parse(previous),
          application_id: latest.application_id,
        }),
      );
    }
    await saved(latest, true);
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (operations.current.size) return;
    if (globalNoteDirty) {
      setError(
        "Save or discard the pending global note first. Notes use their own save button in Global notes.",
      );
      return;
    }
    if (Object.values(pendingCredentials).some(Boolean)) {
      setError(
        "Save or clear the pending credential first. Credentials use their own save button.",
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await control(
        entity ? "save" : "create",
        kind,
        draft.id,
        draft,
      );
      const clean = initial(kind, result, dashboard, schemas);
      setDraft(clean);
      setBaseline(draftContent(clean));
      notify(`${kindLabel[kind]} saved.`);
      await saved(result, !entity && ["bots", "providers"].includes(kind));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      title={entity ? entity.name : `Add ${kindLabel[kind].toLowerCase()}`}
      subtitle={
        entity
          ? `${kindLabel[kind]} · ${entity.id} · revision ${entity.revision}`
          : "Give this configuration a stable identifier. Credentials are always stored separately."
      }
      close={requestClose}
    >
      <form
        className={`editor-form editor-kind-${kind}`}
        onSubmit={submit}
        ref={form}
        onChange={updateJsonDraftState}
        onInvalidCapture={(event) => {
          let parent = (event.target as HTMLElement).parentElement;
          while (parent && parent !== form.current) {
            if (parent instanceof HTMLDetailsElement) parent.open = true;
            parent = parent.parentElement;
          }
        }}
      >
        {kind === "bots" && (
          <div className="tabs editor-tabs">
            {[
              ["identity", "Identity"],
              ["model", "Model & rhythm"],
              ["prompts", "Prompts"],
              ["tools", "Capabilities"],
              ["global-memory", "Global notes"],
              ["footer", "Message footer"],
              ["discord", "Discord"],
            ].map(([id, label]) => (
              <button
                type="button"
                key={id}
                className={tab === id ? "selected" : ""}
                aria-current={tab === id ? "page" : undefined}
                onClick={() => {
                  if (operations.current.size) return;
                  if (id === tab) return;
                  const invalid =
                    form.current?.querySelector<HTMLTextAreaElement>(
                      ".json-input:invalid",
                    );
                  if (invalid) {
                    let parent = invalid.parentElement;
                    while (parent && parent !== form.current) {
                      if (parent instanceof HTMLDetailsElement)
                        parent.open = true;
                      parent = parent.parentElement;
                    }
                    setInvalidJsonDraft(true);
                    setError(invalidSectionMessage);
                    invalid.focus();
                    return;
                  }
                  if (
                    Object.values(pendingCredentials).some(Boolean) &&
                    !window.confirm(
                      "Discard the unsaved credential before changing sections?",
                    )
                  )
                    return;
                  setTab(id);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        {kind !== "bots" && (
          <nav className="editor-outline" aria-label="Editor sections">
            {(sectionLinks[kind] || []).map(([id, label]) => (
              <button
                key={id}
                type="button"
                className={activeSection === id ? "selected" : ""}
                aria-current={activeSection === id ? "location" : undefined}
                onClick={() => jumpToSection(id)}
              >
                {label}
              </button>
            ))}
          </nav>
        )}
        <div className="modal-body">
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          <fieldset className="editor-fields" disabled={busy}>
            {(kind !== "bots" || tab === "identity") && (
              <EditorSection id="identity" title="Identity">
                <div className="form-grid">
                  {text("name", "Display name")}
                  <Field
                    label="Stable identifier"
                    hint={
                      entity
                        ? "Permanent identifier; cloning creates a new one."
                        : "Letters, numbers, underscores, and hyphens."
                    }
                  >
                    <input
                      value={draft.id || ""}
                      disabled={!!entity || kind === "settings"}
                      required
                      pattern="[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}"
                      onChange={(e) => set("id", e.target.value)}
                    />
                  </Field>
                </div>
              </EditorSection>
            )}
            {kind === "bots" && (
              <>
                {tab === "identity" && (
                  <>
                    {select("role", "Role", [
                      { id: "council", name: "Council member" },
                      {
                        id: "hortator",
                        name: "Hortator · owner-only director",
                      },
                    ])}
                    <Field label="Identity color">
                      <input
                        type="color"
                        value={draft.color || "#b9de89"}
                        onChange={(e) => set("color", e.target.value)}
                      />
                    </Field>
                    {checkedList(
                      "room_ids",
                      dashboard.rooms,
                      "Assigned council rooms",
                    )}
                    <Switch
                      label="Activate this bot after saving"
                      checked={!!draft.enabled}
                      onChange={(v) => set("enabled", v)}
                    />
                    <Notice>
                      Each bot has its own Discord application. Changing its
                      model profile preserves its prompts, tool grants, and
                      memory. Hortator’s command connection remains available
                      while its model is paused.
                    </Notice>
                  </>
                )}
                {tab === "model" && (
                  <>
                    {select(
                      "model_profile_id",
                      "Model profile",
                      dashboard.profiles.map((p) => ({
                        id: p.id,
                        name: `${p.name} · ${p.model}`,
                      })),
                      "Profiles hold provider, context capacity, and raw request parameters.",
                    )}
                    <div className="form-grid">
                      {numeric(
                        "interval_seconds",
                        "Activation interval (seconds)",
                        "A fresh decision after each interval. No catch-up bursts.",
                      )}
                      {numeric(
                        "cooldown_seconds",
                        "Minimum send interval (seconds)",
                        "Applies across all rooms for this bot.",
                      )}
                    </div>
                    <Switch
                      label="Evaluate even with no new messages"
                      checked={!!draft.evaluate_when_idle}
                      onChange={(v) => set("evaluate_when_idle", v)}
                    />
                    <Notice>
                      At every activation the model can speak, reply to a
                      message, or choose silence when allowed in Capabilities.
                      Timer evaluation starts once the channel has conversation
                      history. Hortator activates only for new owner questions.
                    </Notice>
                    <div className="form-grid">
                      {numeric(
                        "hourly_turn_limit",
                        "Maximum activations per hour",
                      )}
                      {numeric(
                        "daily_cost_limit",
                        "Daily model cost stop threshold (USD)",
                        "Optional. UTC day; missing cost data stops budgeted bots.",
                        "any",
                      )}
                    </div>
                  </>
                )}
                {tab === "prompts" && (
                  <>
                    <Field
                      label="Personality / system instructions"
                      hint="Appended to the immutable identity and shared council prompt."
                    >
                      <textarea
                        rows={9}
                        value={draft.persona || ""}
                        onChange={(e) => set("persona", e.target.value)}
                      />
                    </Field>
                    {checkedList(
                      "prompt_ids",
                      dashboard.prompts,
                      "Shared prompt templates (applied in selected order)",
                    )}
                    <Field
                      label="Dynamic prompt tail"
                      hint="Literal placeholders: {now}, {timezone}, {bot_name}, {boss_id}, {channel_id}, {round}, {rounds_remaining}, {context_tokens}, {context_window}, {seconds_since_last_message}."
                    >
                      <textarea
                        rows={5}
                        value={draft.dynamic_prompt || ""}
                        onChange={(e) => set("dynamic_prompt", e.target.value)}
                        placeholder="You have {rounds_remaining} tool rounds remaining…"
                      />
                    </Field>
                    <Code
                      value={dashboard.settings.global_prompt}
                      label="Inherited global prompt"
                    />
                    <Notice>
                      Your exact snowflake, The Boss identity, time awareness,
                      and the rule against publishing reasoning are included in
                      every request by the runtime.
                    </Notice>
                  </>
                )}
                {tab === "tools" && (
                  <>
                    <fieldset className="check-list">
                      <legend>Built-in capabilities</legend>
                      <label>
                        <input
                          type="checkbox"
                          aria-label="Allow intentional silence"
                          checked={draft.allow_silence !== false}
                          onChange={(e) =>
                            set("allow_silence", e.target.checked)
                          }
                        />
                        <span>
                          <strong>Allow intentional silence</strong>
                          <small>
                            Expose council_silence so this bot can end an
                            activation without posting. Turn off for
                            conversation and provider stress tests: the model is
                            asked to finish with a text answer. No key required.
                            Provider failures, empty responses, cooldowns and
                            usage limits still apply; this does not add extra
                            activations.
                          </small>
                        </span>
                      </label>
                    </fieldset>
                    {checkedList(
                      "enabled_plugins",
                      dashboard.plugins,
                      "Bot capabilities",
                    )}
                    <SlashCommandSetup bot={entity} draft={draft} />
                    {numeric(
                      "memory_char_limit",
                      "Private memory budget (characters per channel)",
                      "1–48,000 characters per channel; default 48,000. Small overshoots get 5% headroom, then the bot must shrink or delete notes before adding more. Each note allows at most 8,000 characters. Zero and negative values are invalid. Uncheck Private memory to stop memory tools and automatic note injection; stored notes stay available for inspection.",
                    )}
                    {numeric(
                      "global_memory_char_limit",
                      "Global memory budget (characters across channels)",
                      "Independent allowance for this bot's global_memory plugin: 1–48,000, default 48,000, with 5% temporary headroom. Shared across this bot's channels, never other bots. Disable its plugin grant to stop tool access and automatic global-note injection while retaining notes.",
                    )}
                    <div className="form-grid">
                      {numeric(
                        "max_tool_rounds",
                        "Tool work rounds",
                        "Maximum model → tool → model cycles before the final response.",
                      )}
                      {numeric(
                        "max_calls_per_round",
                        "Tool calls allowed in each round",
                        "Calls are executed in order, not in parallel.",
                      )}
                    </div>
                    {(draft.enabled_plugins || []).includes(
                      "document_site",
                    ) && (
                      <>
                        <h3>Extended document task budget</h3>
                        <Notice>
                          A successful document task start grants these
                          additional rounds once per turn, whoever initiated the
                          task. Provider limits and the daily cost threshold
                          still apply.
                        </Notice>
                        <div className="form-grid">
                          {numeric(
                            "document_task_rounds",
                            "Additional document work rounds",
                            "0 keeps the normal budget; repeated starts do not renew it.",
                          )}
                          {numeric(
                            "document_task_calls_per_round",
                            "Document calls allowed in each round",
                          )}
                          {numeric(
                            "document_task_seconds",
                            "Document task time limit (seconds)",
                            "Elapsed time after the first successful task start.",
                          )}
                        </div>
                        <DocumentBotSettings
                          config={draft.plugin_config?.document_site || {}}
                          inheritedLocalUrl={
                            dashboard.plugins.find(
                              (plugin) => plugin.id === "document_site",
                            )?.config?.local_base_url || "http://127.0.0.1:8000"
                          }
                          onChange={(value) =>
                            set("plugin_config", {
                              ...draft.plugin_config,
                              document_site: value,
                            })
                          }
                        />
                      </>
                    )}
                    {(draft.enabled_plugins || []).some((id: string) =>
                      agentToolIds.includes(id),
                    ) && (
                      <>
                        <WorkTaskSettings draft={draft} set={set} />
                        {entity && <AgentToolsPanel botId={entity.id} />}
                      </>
                    )}
                    {entity && <DocumentSitesPanel botId={entity.id} />}
                    <details className="advanced">
                      <summary>Advanced · per-bot plugin configuration</summary>
                      <JsonInput
                        label="Plugin overrides"
                        value={draft.plugin_config || {}}
                        onChange={(v) => set("plugin_config", v)}
                        hint='Map plugin IDs to non-secret configuration, for example {"tts":{"request_json":{"model":"tts-1","voice":"alloy"}}}. Nested values replace the global value. document_site permits only local_base_url; remote destination and automatic publication stay global.'
                      />
                    </details>
                    {entity && (
                      <CredentialBox
                        kind="bots"
                        entity={entity}
                        field="plugin"
                        plugins={dashboard.plugins.filter(
                          (plugin) => !plugin.keyless,
                        )}
                        changed={credentialChanged}
                        onDraftChange={credentialDraftChanged}
                        onBusyChange={operationChanged}
                        notify={notify}
                      />
                    )}
                  </>
                )}
                <div hidden={tab !== "global-memory"}>
                  {entity ? (
                    <GlobalMemoryPanel
                      key={entity.id}
                      botId={entity.id}
                      onDirtyChange={setGlobalNoteDirty}
                      onBusyChange={operationChanged}
                    />
                  ) : (
                    <Notice>
                      Save the bot first to create or inspect its global notes.
                      No channel context is required.
                    </Notice>
                  )}
                </div>
                {tab === "footer" && (
                  <FooterEditor
                    draft={draft}
                    dashboard={dashboard}
                    defaultTemplate={
                      schemas.bots.properties.footer_template.default
                    }
                    set={set}
                  />
                )}
                {tab === "discord" && (
                  <>
                    <div className="discord-guide">
                      <span className="eyebrow">ONE APPLICATION PER BOT</span>
                      <h3>
                        Connect {draft.name || "your new voice"} to Discord
                      </h3>
                      <ol>
                        <li>
                          <span>Create an application and name its bot.</span>
                          <a
                            href="https://discord.com/developers/applications"
                            target="_blank"
                            rel="noreferrer"
                          >
                            Open Developer Portal <ExternalLink size={12} />
                          </a>
                        </li>
                        <li>
                          In Bot → Privileged Gateway Intents, enable{" "}
                          <strong>Message Content Intent</strong>.
                        </li>
                        <li>
                          Copy the application ID and the Bot token. Save the
                          identity, then verify the token below.
                        </li>
                        <li>
                          Use the generated invite to authorize this application
                          in your server.
                        </li>
                      </ol>
                    </div>
                    {text(
                      "application_id",
                      "Discord application ID",
                      "Optional if you save a draft first; token verification discovers the application ID.",
                    )}
                    <Notice>
                      Discord creates and issues each application’s token in its
                      Developer Portal. This panel generates the invitation and
                      checks that the token belongs to the right application.
                    </Notice>
                    {entity ? (
                      <>
                        <CredentialBox
                          kind="bots"
                          entity={entity}
                          field="token"
                          changed={credentialChanged}
                          onDraftChange={credentialDraftChanged}
                          onBusyChange={operationChanged}
                          notify={notify}
                        />
                        <button
                          type="button"
                          className="button"
                          disabled={!entity.token_configured}
                          onClick={async () => {
                            try {
                              await control("restart", "bots", entity.id);
                              notify("Discord reconnect requested.");
                            } catch (err) {
                              notify(
                                err instanceof Error
                                  ? err.message
                                  : "Reconnect failed",
                                true,
                              );
                            }
                          }}
                        >
                          Reconnect Discord
                        </button>
                        <Code
                          label="Gateway and scheduling state"
                          value={entity.runtime}
                        />
                        {entity.invite_url && (
                          <div className="invite-box">
                            <ShieldCheck size={21} />
                            <div>
                              <strong>Application invitation</strong>
                              <small>
                                Channel access, messages, attachments, and
                                public threads.
                              </small>
                            </div>
                            <a
                              className="button primary"
                              href={entity.invite_url}
                              target="_blank"
                              rel="noreferrer"
                            >
                              Invite bot <ExternalLink size={14} />
                            </a>
                            <button
                              type="button"
                              className="icon-button"
                              aria-label="Copy invite link"
                              onClick={() =>
                                navigator.clipboard
                                  .writeText(entity.invite_url)
                                  .then(() => notify("Invite link copied."))
                                  .catch(() =>
                                    notify(
                                      "Clipboard unavailable; use the invite button.",
                                      true,
                                    ),
                                  )
                              }
                            >
                              <Copy size={15} />
                            </button>
                          </div>
                        )}
                        <CredentialBox
                          kind="bots"
                          entity={entity}
                          field="provider_key"
                          changed={credentialChanged}
                          onDraftChange={credentialDraftChanged}
                          onBusyChange={operationChanged}
                          notify={notify}
                        />
                        {entity.readiness?.length > 0 && (
                          <div className="readiness">
                            <h4>Before this bot can activate</h4>
                            {entity.readiness.map((issue: string) => (
                              <p key={issue}>
                                <span />
                                {issue}
                              </p>
                            ))}
                          </div>
                        )}
                      </>
                    ) : (
                      <Notice>
                        Save this draft to unlock token verification and its
                        invitation link.
                      </Notice>
                    )}
                  </>
                )}
              </>
            )}
            {kind === "providers" && (
              <>
                <EditorSection id="connection" title="Connection">
                  {select("kind", "Provider type", [
                    { id: "openrouter", name: "OpenRouter" },
                    {
                      id: "openai_compatible",
                      name: "Any OpenAI-compatible endpoint",
                    },
                  ])}
                  {text(
                    "base_url",
                    "API base URL",
                    "Include the API prefix, such as /v1. Requests are sent to /chat/completions.",
                  )}
                  <Field
                    label="User-Agent"
                    hint="Sent for model discovery and every model request, including compaction. Leave blank to use the HTTP client's default. This is the same User-Agent entry shown in Advanced HTTP headers."
                  >
                    <input
                      value={
                        (Object.entries(draft.headers || {}).find(
                          ([key]) => key.toLowerCase() === "user-agent",
                        )?.[1] as string) || ""
                      }
                      maxLength={1024}
                      placeholder="HTTP client default"
                      onChange={(event) => {
                        const headers = Object.fromEntries(
                          Object.entries(draft.headers || {}).filter(
                            ([key]) => key.toLowerCase() !== "user-agent",
                          ),
                        );
                        if (event.target.value)
                          headers["User-Agent"] = event.target.value;
                        set("headers", headers);
                      }}
                    />
                  </Field>
                </EditorSection>
                <EditorSection id="limits" title="Request limits & recovery">
                  <div className="form-grid">
                    {numeric(
                      "timeout_seconds",
                      "Total request timeout (seconds)",
                    )}
                    {numeric("max_concurrency", "Concurrent requests")}
                  </div>
                  <div className="form-grid">
                    {numeric(
                      "failure_threshold",
                      "Failures before circuit opens",
                    )}
                    {numeric("circuit_seconds", "Recovery delay (seconds)")}
                  </div>
                  <div className="switch-stack">
                    <Switch
                      label="Provider enabled"
                      checked={!!draft.enabled}
                      onChange={(v) => set("enabled", v)}
                    />
                    <Switch
                      label="Endpoint requires an API key"
                      checked={!!draft.requires_key}
                      onChange={(v) => set("requires_key", v)}
                    />
                  </div>
                </EditorSection>
                <EditorSection
                  id="authentication"
                  title="Headers & credentials"
                >
                  <details className="advanced">
                    <summary>Advanced · non-secret HTTP headers</summary>
                    <JsonInput
                      label="Request headers"
                      value={draft.headers || {}}
                      onChange={(v) => set("headers", v)}
                      hint='For example {"HTTP-Referer":"https://your-site.example","X-Title":"My council"}. Authorization is managed by the credential store.'
                    />
                  </details>
                  {entity ? (
                    <CredentialBox
                      kind="providers"
                      entity={entity}
                      field="api_key"
                      changed={credentialChanged}
                      onDraftChange={credentialDraftChanged}
                      onBusyChange={operationChanged}
                      notify={notify}
                    />
                  ) : (
                    <Notice>
                      Save the provider first, then enter its API key.
                    </Notice>
                  )}
                </EditorSection>
              </>
            )}
            {kind === "profiles" && (
              <>
                <EditorSection id="model" title="Model selection">
                  {select(
                    "provider_id",
                    "Provider",
                    dashboard.providers.map((p) => ({
                      id: p.id,
                      name: p.name,
                    })),
                  )}
                  {text(
                    "model",
                    "Exact model identifier",
                    "Paste the provider’s model ID. Use Discover models on the provider to browse its catalog.",
                  )}
                </EditorSection>
                <EditorSection id="context" title="Context & retained summary">
                  <div className="form-grid">
                    {numeric("context_window", "Context window (tokens)")}
                    {numeric(
                      "max_request_images",
                      "Images per request",
                      "Maximum new attachment images for a turn, newest messages first. Pixels expire after a handled turn. Extra images stay as metadata; they do not trigger compaction.",
                    )}
                    {numeric(
                      "max_request_image_mib",
                      "Combined image budget (MiB)",
                      "Combined selected image bytes before base64 encoding. Per-file intake remains 20 MiB. These are local budgets, not detected provider limits.",
                    )}
                    {numeric(
                      "compact_threshold",
                      "Auto-compact threshold",
                      "Fraction of context, e.g. 0.70.",
                      ".01",
                    )}
                    {numeric(
                      "response_tokens",
                      "Response token reserve",
                      "Space reserved in context planning. Set max_tokens or max_completion_tokens in Model parameters to send an output limit to the provider.",
                    )}
                    {numeric(
                      "summary_tokens",
                      "Retained summary limit (tokens)",
                      "Hard limit on the finished summary text using cl100k_base, excluding private reasoning. No combined output cap is sent to the provider. Must stay below 60% of the context window.",
                    )}
                    {numeric(
                      "keep_recent_messages",
                      "Recent messages to retain",
                      "Reduced if the tail cannot fit the input budget.",
                    )}
                  </div>
                </EditorSection>
                <EditorSection id="request" title="Generation & reasoning">
                  <div className="switch-stack">
                    <Switch
                      label="SSE streaming"
                      checked={draft.stream !== false}
                      onChange={(v) => set("stream", v)}
                    />
                    <Switch
                      label="Request stream usage data"
                      checked={!!draft.include_usage}
                      disabled={draft.stream === false}
                      onChange={(v) => set("include_usage", v)}
                    />
                  </div>
                  <Notice>
                    Streaming is configured for this model profile and its
                    selected provider. Turn it off to request one complete JSON
                    response. Discord still waits for the complete answer either
                    way. Buffered responses retain reported usage and reasoning,
                    but have no measured TTFT or streaming TPS. Usage estimates
                    are not substituted for missing provider token counts.
                  </Notice>
                  <ReasoningEditor
                    key={draft.provider_id}
                    value={draft.request_json || {}}
                    onChange={(v) => set("request_json", v)}
                    providerUrl={
                      dashboard.providers.find(
                        (p) => p.id === draft.provider_id,
                      )?.base_url || ""
                    }
                    disabled={!modelJsonValid}
                  />
                  <details className="advanced prominent" open>
                    <summary>Advanced · exact request JSON</summary>
                    <JsonInput
                      label="Model parameters"
                      value={draft.request_json || {}}
                      onChange={(v) => set("request_json", v)}
                      onValidityChange={setModelJsonValid}
                      hint="Sent unchanged alongside the runtime's model, messages, tools, and stream settings. Nested vendor options stay nested. No reasoning-effort or sampling translation layer."
                    />
                    <Notice>
                      The runtime owns <code>model</code>, <code>messages</code>
                      , <code>tools</code>, <code>tool_choice</code>,{" "}
                      <code>stream</code>, and single-choice generation. If you
                      set <code>max_tokens</code> or{" "}
                      <code>max_completion_tokens</code>, keep it within the
                      response reserve. These generation caps are omitted from
                      compaction requests.
                    </Notice>
                  </details>
                </EditorSection>
                <EditorSection id="compaction" title="Compaction parameters">
                  <details className="advanced">
                    <summary>Advanced · compaction parameter overrides</summary>
                    <JsonInput
                      label="Compaction parameters"
                      value={draft.compaction_request_json || {}}
                      onChange={(v) => set("compaction_request_json", v)}
                      hint="Native reasoning and other overrides for summarization. max_tokens, max_completion_tokens and max_output_tokens are omitted from compaction requests, even if configured here."
                    />
                    <p className="muted small-text">
                      Compaction starts with Model parameters and replaces any
                      top-level fields set here, including whole nested objects.
                      Compaction sends no total-output cap: provider defaults
                      and context limits still apply. Only the finished summary
                      counts against the retained summary limit. An oversized or
                      incomplete candidate is saved for inspection and leaves
                      previous context intact.
                    </p>
                    <Code
                      value={reasoningFields({
                        ...draft.request_json,
                        ...draft.compaction_request_json,
                      })}
                      label="Effective compaction reasoning fields"
                    />
                  </details>
                </EditorSection>
                <EditorSection id="pricing" title="Token pricing">
                  <PricingEditor draft={draft} set={set} />
                </EditorSection>
              </>
            )}
            {kind === "prompts" && (
              <EditorSection id="instructions" title="System instructions">
                <Field
                  label="System prompt"
                  hint="Assign this template to any number of bots. Edits cancel affected active turns before the new prompt is used."
                >
                  <textarea
                    rows={18}
                    value={draft.content || ""}
                    onChange={(e) => set("content", e.target.value)}
                    placeholder="Your shared instructions…"
                  />
                </Field>
              </EditorSection>
            )}
            {kind === "plugins" && (
              <EditorSection
                id="capabilities"
                title="Plugin configuration & tools"
              >
                <p className="muted">{draft.description}</p>
                <Switch
                  label="Enable plugin globally"
                  checked={!!draft.enabled}
                  onChange={(v) => set("enabled", v)}
                />
                <Notice>
                  Execution requires both global enablement and an explicit bot
                  grant. Council inspection is additionally restricted to
                  owner-initiated Hortator turns.
                </Notice>
                {draft.id === "web_search" && (
                  <>
                    <Field label="Default search engine">
                      <select
                        value={draft.config?.engine || "auto"}
                        onChange={(e) =>
                          set("config", {
                            ...draft.config,
                            engine: e.target.value,
                          })
                        }
                      >
                        <option value="auto">
                          Auto · Brave, then DuckDuckGo fallback
                        </option>
                        <option value="both">
                          Both · combine Brave and DuckDuckGo
                        </option>
                        <option value="brave">Brave only</option>
                        <option value="duckduckgo">
                          DuckDuckGo only · no key
                        </option>
                      </select>
                    </Field>
                    <Field
                      label="Search results per engine"
                      hint="Default 5, maximum 10. Both requests this many from each engine and removes duplicate URLs."
                    >
                      <input
                        type="number"
                        min={1}
                        max={10}
                        step={1}
                        value={draft.config?.count ?? 5}
                        onChange={(e) =>
                          set("config", {
                            ...draft.config,
                            count:
                              e.target.value === ""
                                ? null
                                : Number(e.target.value),
                          })
                        }
                      />
                    </Field>
                    <Notice>
                      DuckDuckGo needs no API key. For Brave, activate a Search
                      plan at the{" "}
                      <a
                        href="https://api-dashboard.search.brave.com/"
                        target="_blank"
                        rel="noreferrer"
                      >
                        Brave API dashboard
                      </a>
                      , create a key under API Keys, then paste it into the API
                      key field below and save the credential. Bots can select
                      either engine or both per call. Auto falls back when Brave
                      fails or has no results; Both preserves the other engine's
                      results on a partial failure. DuckDuckGo's HTML search can
                      return a rate limit or challenge, which is reported
                      explicitly.
                    </Notice>
                  </>
                )}
                {draft.id === "document_site" && (
                  <DocumentPluginSettings
                    config={draft.config || {}}
                    onChange={(value) => set("config", value)}
                  />
                )}
                {agentToolIds.includes(draft.id) && (
                  <AgentToolSettings
                    pluginId={draft.id}
                    config={draft.config || {}}
                    onChange={(value) => set("config", value)}
                  />
                )}
                <JsonInput
                  label="Plugin configuration"
                  value={draft.config || {}}
                  onChange={(v) => set("config", v)}
                  hint={
                    draft.id === "document_site"
                      ? "Non-secret publication settings. The SSH identity is a vault name. Bots may override only local_base_url."
                      : "Configure the endpoint, model, and non-secret provider JSON. Bot overrides are applied after this configuration."
                  }
                />
                {entity && !entity.keyless && (
                  <CredentialBox
                    kind="plugins"
                    entity={entity}
                    field="api_key"
                    changed={credentialChanged}
                    onDraftChange={credentialDraftChanged}
                    onBusyChange={operationChanged}
                    notify={notify}
                  />
                )}
                {draft.id === "document_site" && <DocumentSitesPanel />}
                {agentToolIds.includes(draft.id) && <AgentToolsPanel />}
                <Code value={entity?.schema} label="Tool-call JSON schema" />
              </EditorSection>
            )}
            {kind === "rooms" && (
              <>
                <EditorSection id="channel" title="Discord channel">
                  <div className="form-grid">
                    {text("guild_id", "Discord server / guild ID")}
                    {text("channel_id", "Text or forum channel ID")}
                  </div>
                  {numeric(
                    "send_gap_seconds",
                    "Minimum room gap (seconds)",
                    "Serializes delivery by all council bots in the same channel.",
                    ".1",
                  )}
                </EditorSection>
                <EditorSection
                  id="participation"
                  title="Conversation participants"
                >
                  <div className="switch-stack">
                    <Switch
                      label="Participate in child threads"
                      checked={!!draft.include_threads}
                      onChange={(v) => set("include_threads", v)}
                    />
                    <Switch
                      label="Include human messages"
                      checked={!!draft.allow_humans}
                      onChange={(v) => set("allow_humans", v)}
                    />
                    <Switch
                      label="Include external bots"
                      checked={!!draft.allow_external_bots}
                      onChange={(v) => set("allow_external_bots", v)}
                    />
                  </div>
                  <Notice>
                    Known council bots are always eligible conversation
                    participants. Include external bots also admits application
                    responses, including slash-command results. Ordinary
                    incoming webhooks remain excluded. App messages never carry
                    human or administrative authority. Threads have separate
                    contexts; forum conversations take place inside posts.
                  </Notice>
                </EditorSection>
              </>
            )}
            {kind === "settings" && (
              <>
                <EditorSection id="scope" title="Owner & Discord scope">
                  <div className="locked-identity">
                    <LockKeyhole size={16} />
                    <span>Owner</span>
                    <code>{dashboard.owner_id}</code>
                  </div>
                  <div className="form-grid">
                    {text("control_guild_id", "Hortator control server ID")}
                    {text("control_channel_id", "Hortator control channel ID")}
                  </div>
                  <Notice>
                    Hortator accepts only your messages in this control channel,
                    its threads, or your DMs. Mentions in other server channels
                    do not bypass that scope. Reports also use this channel.
                  </Notice>
                </EditorSection>
                <EditorSection id="instructions" title="Council instructions">
                  <Field label="Global system prompt">
                    <textarea
                      rows={8}
                      value={draft.global_prompt || ""}
                      onChange={(e) => set("global_prompt", e.target.value)}
                    />
                  </Field>
                </EditorSection>
                <EditorSection id="runtime" title="Runtime preferences">
                  <div className="form-grid">
                    {numeric(
                      "max_concurrent_turns",
                      "Maximum simultaneous bot turns",
                    )}
                    {text(
                      "timezone",
                      "Council timezone",
                      "IANA timezone, e.g. Europe/Madrid.",
                    )}
                  </div>
                  <Switch
                    label="Report incidents and recoveries to Discord"
                    checked={!!draft.incident_notifications}
                    onChange={(v) => set("incident_notifications", v)}
                  />
                  <Notice>
                    Public reports may show statistics and prompts. Control
                    remains owner-only. A private question or <code>!dm</code>{" "}
                    starts a separate Hortator context.
                  </Notice>
                </EditorSection>
              </>
            )}
            <details className="advanced full-json" data-editor-section="json">
              <summary>Advanced · full configuration JSON</summary>
              <JsonInput
                label="Configuration record"
                value={draft}
                onChange={setDraft}
                hint="Every non-secret setting is editable here. Nested objects replace the previous object. IDs are immutable after creation; revisions protect against conflicting edits."
              />
            </details>
          </fieldset>
        </div>
        <footer className="modal-footer">
          <div>
            {entity && !["settings", "plugins"].includes(kind) && (
              <button
                type="button"
                className="icon-button destructive"
                aria-label={`Delete ${entity.name}`}
                onClick={async () => {
                  if (operations.current.size) return;
                  if (
                    !confirm(
                      `Delete ${entity.name}? Its historical records will be retained.`,
                    )
                  )
                    return;
                  setBusy(true);
                  try {
                    await control("delete", kind, entity.id);
                    notify(
                      "Configuration deleted. Historical records retained.",
                    );
                    await saved({});
                  } catch (err) {
                    setError(
                      err instanceof Error ? err.message : "Delete failed",
                    );
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <Trash2 size={16} />
              </button>
            )}
            <span
              className={`editor-draft-state ${dirty ? "has-changes" : ""}`}
            >
              {dirty ? "Unsaved changes" : "No unsaved changes"}
            </span>
          </div>
          <span className="editor-shortcut">
            <kbd>Ctrl</kbd> + <kbd>S</kbd>
          </span>
          <button
            type="button"
            className="button"
            onClick={requestClose}
            disabled={busy}
          >
            Cancel
          </button>
          <button className="button primary" type="submit" disabled={busy}>
            <Save size={15} />
            {busy ? "Saving…" : entity ? "Save changes" : "Create draft"}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

function CredentialBox({
  kind,
  entity,
  field,
  plugins = [],
  changed,
  notify,
  onDraftChange,
  onBusyChange,
}: {
  kind: Kind;
  entity: RecordData;
  field: string;
  plugins?: RecordData[];
  changed: () => Promise<void>;
  notify: (t: string, e?: boolean) => void;
  onDraftChange?: (id: string, pending: boolean) => void;
  onBusyChange?: (id: string, pending: boolean) => void;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [plugin, setPlugin] = useState(plugins[0]?.id || "");
  const resolved = field === "plugin" ? `plugin:${plugin}` : field;
  useEffect(() => {
    onDraftChange?.(resolved, value.length > 0);
    return () => onDraftChange?.(resolved, false);
  }, [resolved, value, onDraftChange]);
  const configured =
    field === "token"
      ? entity.token_configured
      : field === "provider_key"
        ? entity.key_override_configured
        : field === "plugin"
          ? entity.plugin_keys_configured?.includes(plugin)
          : entity.key_configured;
  const label =
    field === "token"
      ? "Discord bot token"
      : field === "provider_key"
        ? "Provider key override"
        : field === "plugin"
          ? "Per-bot plugin key"
          : "API key";
  async function save(secret: string) {
    if (busy) return;
    setBusy(true);
    onBusyChange?.(`credential:${resolved}`, true);
    let stored = false;
    try {
      await credential(kind, entity.id, resolved, secret);
      stored = true;
      setValue("");
      await changed();
      notify(
        secret
          ? `${label} stored encrypted${field === "token" ? " and application verified" : ""}.`
          : `${label} removed.`,
      );
    } catch (err) {
      const reason = err instanceof Error ? err.message : "Request failed";
      notify(
        stored
          ? `${label} ${secret ? "saved" : "removed"}, but refreshing its configuration failed: ${reason}`
          : reason,
        true,
      );
    } finally {
      setBusy(false);
      onBusyChange?.(`credential:${resolved}`, false);
    }
  }
  return (
    <div className="credential-box">
      <div>
        <KeyRound size={17} />
        <h3>{label}</h3>
        <Badge tone={configured ? "green" : "neutral"}>
          {configured
            ? "Configured"
            : field === "provider_key"
              ? "Inherits provider key"
              : "Not set"}
        </Badge>
      </div>
      {field === "plugin" && (
        <Field label="Plugin credential">
          <select
            value={plugin}
            disabled={busy}
            onChange={(e) => {
              if (
                value &&
                !window.confirm(
                  "Discard the unsaved credential before selecting another plugin?",
                )
              )
                return;
              setValue("");
              setPlugin(e.target.value);
            }}
          >
            {plugins.map((p) => (
              <option value={p.id} key={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
      )}
      <Field
        label={label}
        className="credential-entry"
        hint="Write-only. Stored encrypted on the server and never returned in reports or sent to the model."
      >
        <input
          type="password"
          autoComplete="new-password"
          value={value}
          disabled={busy}
          onChange={(e) => setValue(e.target.value)}
          placeholder={
            configured ? "Paste a replacement credential" : "Paste credential"
          }
        />
      </Field>
      <div className="credential-actions">
        <button
          type="button"
          className="button"
          disabled={busy || !value.trim()}
          onClick={() => save(value)}
        >
          {busy
            ? "Verifying / saving…"
            : field === "token"
              ? "Verify & save token"
              : "Save credential"}
          <ArrowRight size={13} />
        </button>
        {configured && (
          <button
            type="button"
            className="text-button red-text"
            disabled={busy}
            onClick={() => save("")}
          >
            Remove
          </button>
        )}
      </div>
    </div>
  );
}

export function ContextPanel({
  bot,
  close,
  notify,
}: {
  bot: RecordData;
  close: () => void;
  notify: (text: string, error?: boolean) => void;
}) {
  const [channel, setChannel] = useState(bot.contexts[0]?.channel_id || "");
  const [data, setData] = useState<RecordData | null>(null);
  const [error, setError] = useState("");
  const [noteKey, setNoteKey] = useState("operator");
  const [noteValue, setNoteValue] = useState("");
  const [noteBaseline, setNoteBaseline] = useState({
    key: "operator",
    value: "",
  });
  const [pendingAction, setPendingAction] = useState<"" | "memory" | "compact">(
    "",
  );
  const pendingRef = useRef(false);
  const currentChannel = useRef(channel);
  currentChannel.current = channel;
  const contextRequest = useRef(0);
  const mounted = useRef(false);
  const noteDirty =
    noteKey !== noteBaseline.key || noteValue !== noteBaseline.value;
  const noteDirtyRef = useRef(noteDirty);
  noteDirtyRef.current = noteDirty;
  useEffect(() => {
    mounted.current = true;
    const protectNavigation = (event: Event) => {
      if (pendingRef.current) {
        event.preventDefault();
        setError(
          "Wait for the current memory save or compaction request to finish before leaving this context.",
        );
      } else if (
        noteDirtyRef.current &&
        !window.confirm("Discard the unsaved note changes in this context?")
      ) {
        event.preventDefault();
      }
    };
    const protectUnload = (event: BeforeUnloadEvent) => {
      if (noteDirtyRef.current || pendingRef.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener(navigationEvent, protectNavigation);
    window.addEventListener("beforeunload", protectUnload);
    return () => {
      mounted.current = false;
      contextRequest.current++;
      window.removeEventListener(navigationEvent, protectNavigation);
      window.removeEventListener("beforeunload", protectUnload);
    };
  }, []);
  useEffect(() => {
    if (!channel) return;
    let active = true;
    const refresh = () => {
      if (pendingRef.current) return;
      const request = ++contextRequest.current;
      api(`/api/context/${bot.id}/${channel}`)
        .then((r) => {
          if (
            active &&
            currentChannel.current === channel &&
            request === contextRequest.current
          ) {
            setData(r);
            setError("");
          }
        })
        .catch((e) => {
          if (
            active &&
            currentChannel.current === channel &&
            request === contextRequest.current
          )
            setError(e.message);
        });
    };
    refresh();
    const timer = setInterval(refresh, 4000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [bot.id, channel]);
  const requestClose = () => {
    if (confirmEditorNavigation()) close();
  };
  const changeChannel = (next: string) => {
    if (next === channel || !confirmEditorNavigation()) return;
    currentChannel.current = next;
    contextRequest.current++;
    setChannel(next);
    setData(null);
    setError("");
    setNoteKey("operator");
    setNoteValue("");
    setNoteBaseline({ key: "operator", value: "" });
  };
  const saveNote = async () => {
    if (pendingRef.current) return;
    const targetChannel = channel;
    const targetNote = { key: noteKey, value: noteValue };
    const stillCurrent = () =>
      mounted.current && currentChannel.current === targetChannel;
    pendingRef.current = true;
    setPendingAction("memory");
    contextRequest.current++;
    setError("");
    let savedNote = false;
    try {
      await control("memory", "bots", bot.id, {
        channel_id: targetChannel,
        ...targetNote,
      });
      savedNote = true;
      if (!stillCurrent()) return;
      setNoteBaseline(targetNote);
      notify("Memory updated.");
      const request = ++contextRequest.current;
      const refreshed = await api(`/api/context/${bot.id}/${targetChannel}`);
      if (stillCurrent() && request === contextRequest.current)
        setData(refreshed);
    } catch (err) {
      if (!stillCurrent()) return;
      const reason = err instanceof Error ? err.message : "Request failed";
      notify(
        savedNote
          ? `Memory saved, but refreshing this context failed: ${reason}`
          : `Memory update failed: ${reason}`,
        true,
      );
    } finally {
      pendingRef.current = false;
      if (stillCurrent()) setPendingAction("");
    }
  };
  const compact = async () => {
    if (pendingRef.current) return;
    const targetChannel = channel;
    const stillCurrent = () =>
      mounted.current && currentChannel.current === targetChannel;
    pendingRef.current = true;
    setPendingAction("compact");
    contextRequest.current++;
    setError("");
    try {
      const result = await control("compact", "bots", bot.id, {
        channel_id: targetChannel,
      });
      if (stillCurrent()) notify(`Compaction started · ${result.turn_id}`);
    } catch (err) {
      if (stillCurrent())
        notify(err instanceof Error ? err.message : "Compaction failed", true);
    } finally {
      pendingRef.current = false;
      if (stillCurrent()) setPendingAction("");
    }
  };
  return (
    <Modal
      title={`${bot.name} · context & memory`}
      subtitle="Each channel and thread is an independent context for this bot."
      close={requestClose}
      wide
    >
      <div className="modal-body context-body">
        {!bot.contexts.length ? (
          <Empty title="A clean slate" icon={<Layers3 size={26} />}>
            This bot has not observed any conversation yet. Its context will
            appear after it connects to an assigned room.
          </Empty>
        ) : (
          <>
            <div className="context-toolbar">
              <Field label="Channel / thread">
                <select
                  value={channel}
                  disabled={!!pendingAction}
                  onChange={(e) => changeChannel(e.target.value)}
                >
                  {bot.contexts.map((c: RecordData) => (
                    <option key={c.channel_id} value={c.channel_id}>
                      {c.channel_id}
                    </option>
                  ))}
                </select>
              </Field>
              <button
                className="button"
                disabled={!!pendingAction}
                onClick={compact}
              >
                <Layers3 size={15} />
                {pendingAction === "compact"
                  ? "Starting compaction…"
                  : "Compact now"}
              </button>
            </div>
            {error && <Notice warning>{error}</Notice>}
            {data && (
              <>
                <div className="context-stats">
                  <span>
                    Estimated context
                    <strong>{num(data.estimated_tokens)} tokens</strong>
                  </span>
                  <span>
                    Compactions<strong>{data.compactions}</strong>
                  </span>
                  <span>
                    Checkpoint<strong>Message #{data.checkpoint}</strong>
                  </span>
                  <span>
                    Uncompacted history
                    <strong>{data.message_count} messages</strong>
                  </span>
                </div>
                <h3>Current compaction summary</h3>
                <pre className="summary-text">
                  {data.summary ||
                    "No summary yet. The bot is working from the original conversation."}
                </pre>
                <h3>Persistent notes</h3>
                {data.memories.map((note: RecordData) => (
                  <div className="memory-note" key={note.key}>
                    <span>
                      <code>{note.key}</code>
                      <small>{dateLabel(note.updated_at)}</small>
                    </span>
                    <p>{note.value}</p>
                    <button
                      className="text-button"
                      disabled={!!pendingAction}
                      onClick={() => {
                        if (pendingRef.current || !confirmEditorNavigation())
                          return;
                        setNoteKey(note.key);
                        setNoteValue(note.value);
                        setNoteBaseline({ key: note.key, value: note.value });
                      }}
                    >
                      Edit note
                    </button>
                  </div>
                ))}
                <div className="form-grid">
                  {" "}
                  <Field label="Note key">
                    <input
                      value={noteKey}
                      disabled={!!pendingAction}
                      onChange={(e) => setNoteKey(e.target.value)}
                    />
                  </Field>
                  <Field
                    label="Note value"
                    hint="An empty value deletes this key."
                  >
                    <textarea
                      rows={3}
                      value={noteValue}
                      disabled={!!pendingAction}
                      onChange={(e) => setNoteValue(e.target.value)}
                    />
                  </Field>
                </div>
                <button
                  className="button"
                  disabled={!!pendingAction || !noteKey.trim()}
                  onClick={saveNote}
                >
                  <Save size={14} />
                  {pendingAction === "memory" ? "Saving note…" : "Save note"}
                </button>
                {noteDirty && (
                  <span
                    className="editor-draft-state has-changes"
                    role="status"
                  >
                    {" "}
                    Unsaved note changes · channel {channel}
                  </span>
                )}
                <div className="context-history">
                  <h3>Compaction history</h3>
                  {data.compaction_events.map((event: RecordData) => (
                    <Code
                      key={event.id}
                      label={`${dateLabel(event.at)} · ${num(event.data.before_tokens)} → ${num(event.data.after_tokens)} tokens`}
                      value={event.data}
                    />
                  ))}
                  {!data.compaction_events.length && (
                    <p className="muted">No compaction events recorded.</p>
                  )}
                  <Code
                    value={data.messages}
                    label={`Uncompacted message sources · ${data.messages.length} of ${data.message_count}`}
                  />
                </div>
              </>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}
