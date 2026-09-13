import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowUpRight,
  AudioLines,
  Cable,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  FileText,
  Hash,
  Layers3,
  LayoutDashboard,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Terminal,
  Users,
  X,
  Zap,
} from "lucide-react";
import {
  api,
  ApiError,
  control,
  dateLabel,
  duration,
  eventText,
  money,
  num,
  setCsrf,
  setCouncilTimezone,
  timeLabel,
} from "./api";
import type { Dashboard, Kind, Page, RecordData } from "./api";
import { Avatar, Badge, Code, Field, Notice } from "./components";
import { Editor, ContextPanel, confirmEditorNavigation } from "./Editor";
import { Trajectory } from "./Trajectory";
import { Analytics, UsageChart } from "./Analytics";
import { Version } from "./Version";
import { Snapshots } from "./Snapshots";
import { reasoningSummary } from "./Reasoning";
import { WorkbenchTable } from "./WorkbenchTable";
import type { Column } from "./WorkbenchTable";

const navigation: {
  id: Page;
  label: string;
  icon: typeof Users;
  group: string;
}[] = [
  { id: "council", label: "Overview", icon: LayoutDashboard, group: "MONITOR" },
  { id: "trajectory", label: "Trajectory", icon: Activity, group: "MONITOR" },
  { id: "analytics", label: "Analytics", icon: AudioLines, group: "MONITOR" },
  { id: "bots", label: "Bots", icon: Users, group: "CONFIGURATION" },
  { id: "providers", label: "Providers", icon: Cable, group: "CONFIGURATION" },
  {
    id: "profiles",
    label: "Model profiles",
    icon: Layers3,
    group: "CONFIGURATION",
  },
  {
    id: "prompts",
    label: "Prompt library",
    icon: FileText,
    group: "CONFIGURATION",
  },
  { id: "plugins", label: "Plugins", icon: Zap, group: "CONFIGURATION" },
  { id: "rooms", label: "Rooms", icon: Hash, group: "CONFIGURATION" },
  { id: "snapshots", label: "Snapshots", icon: Layers3, group: "CONTROL" },
  {
    id: "settings",
    label: "Council settings",
    icon: Settings2,
    group: "CONTROL",
  },
  {
    id: "commands",
    label: "Discord commands",
    icon: Terminal,
    group: "CONTROL",
  },
];
const catalogKinds = ["providers", "profiles", "prompts", "plugins", "rooms"];
const addNames: Record<string, string> = {
  council: "bot",
  bots: "bot",
  providers: "provider",
  profiles: "profile",
  prompts: "prompt",
  rooms: "room",
};
const pageFromHash = (): Page =>
  navigation.some((n) => n.id === location.hash.slice(1))
    ? (location.hash.slice(1) as Page)
    : "council";
type Edit = (kind: Kind, entity?: RecordData) => void;
type Act = (
  action: string,
  kind?: string,
  id?: string,
  data?: RecordData,
) => Promise<any>;
type Notify = (text: string, error?: boolean) => void;

function botStatus(bot: RecordData, d: Dashboard): [string, string] {
  if (bot.readiness?.length) return ["Draft", "neutral"];
  if (!d.settings.enabled || !bot.enabled) return ["Paused", "amber"];
  const profile = d.profiles.find((p) => p.id === bot.model_profile_id);
  const provider = d.providers.find((p) => p.id === profile?.provider_id);
  if (!provider?.enabled) return ["Provider paused", "amber"];
  if (bot.runtime.gateway_status !== "online")
    return [
      bot.runtime.gateway_status,
      bot.runtime.gateway_status === "failed" ? "red" : "neutral",
    ];
  if (provider.health.circuit_until > d.now) return ["Circuit open", "red"];
  if (bot.active_turn) return ["Thinking", "violet"];
  if (bot.runtime.error) return ["Attention", "red"];
  return ["Listening", "green"];
}
function RecordName({ item, edit }: { item: RecordData; edit: () => void }) {
  return (
    <button
      className="record-name"
      aria-label={`Edit ${item.name}`}
      onClick={edit}
      title={`${item.name}\n${item.id}`}
    >
      <strong>{item.name}</strong>
      <small>{item.id}</small>
    </button>
  );
}
function IconAction({
  label,
  onClick,
  children,
  disabled = false,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <button
      className="icon-button"
      title={label}
      aria-label={label}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [stats, setStats] = useState<RecordData | null>(null);
  const [events, setEvents] = useState<RecordData[]>([]);
  const [schemas, setSchemas] = useState<RecordData>({});
  const [page, setPage] = useState<Page>(pageFromHash);
  const [search, setSearch] = useState("");
  const [editor, setEditor] = useState<{
    kind: Kind;
    entity?: RecordData;
    token: number;
  } | null>(null);
  const [contextBot, setContextBot] = useState<RecordData | null>(null);
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(
    null,
  );
  const [connected, setConnected] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("hortator.workbench.navCollapsed") === "true",
  );
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const [palette, setPalette] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<number | null>(null);
  const refreshing = useRef<Promise<void> | null>(null);
  const schemaCache = useRef<RecordData | null>(null);
  const filter = useRef<HTMLInputElement>(null);
  const editorSerial = useRef(0);
  const notify = useCallback<Notify>(
    (text, error = false) => setToast({ text, error }),
    [],
  );
  const refresh = useCallback((): Promise<void> => {
    if (refreshing.current) return refreshing.current;
    const request = (async () => {
      try {
        const [data, metrics, ledger, loadedSchemas] = await Promise.all([
          api<Dashboard>("/api/status"),
          api("/api/stats"),
          api("/api/events?limit=20"),
          schemaCache.current || api<RecordData>("/api/config-schemas"),
        ]);
        if (
          ![
            "bots",
            "providers",
            "profiles",
            "plugins",
            "prompts",
            "rooms",
            "settings",
          ].every((kind) => loadedSchemas[kind]?.properties)
        )
          throw new Error(
            "Configuration schemas are incomplete. Refresh the dashboard to retry.",
          );
        schemaCache.current = loadedSchemas;
        setSchemas(loadedSchemas);
        setCouncilTimezone(data.settings.timezone);
        setDashboard(data);
        setStats(metrics);
        setEvents(ledger);
        setLoadError("");
        setLastRefresh(Date.now() / 1000);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401)
          setAuthenticated(false);
        else
          setLoadError(
            err instanceof Error ? err.message : "Connection failed",
          );
      } finally {
        refreshing.current = null;
      }
    })();
    refreshing.current = request;
    return request;
  }, []);
  const refreshAfterMutation = useCallback(async () => {
    // A poll started before a save cannot acknowledge that save's new revision.
    if (refreshing.current) await refreshing.current;
    await refresh();
  }, [refresh]);
  useEffect(() => {
    api("/api/auth/session")
      .then((s) => {
        setCsrf(s.csrf);
        setAuthenticated(true);
      })
      .catch(() => setAuthenticated(false));
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    refresh();
    const timer = setInterval(refresh, 4000);
    const stream = new EventSource("/api/events/stream");
    stream.addEventListener("connected", () => setConnected(true));
    stream.onmessage = () => setConnected(true);
    stream.onerror = () => setConnected(false);
    return () => {
      clearInterval(timer);
      stream.close();
      setConnected(false);
    };
  }, [authenticated, refresh, notify]);
  useEffect(() => {
    if (!toast || toast.error) return;
    const timer = setTimeout(() => setToast(null), 5000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    localStorage.setItem("hortator.workbench.navCollapsed", String(collapsed));
  }, [collapsed]);
  useEffect(() => {
    const change = () => {
      const next = pageFromHash();
      if (next === page) return;
      if (!confirmEditorNavigation()) {
        history.replaceState(null, "", `#${page}`);
        return;
      }
      setEditor(null);
      setContextBot(null);
      setSearch("");
      setPage(next);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, [page]);
  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey) {
        if (e.key.toLowerCase() === "k") {
          e.preventDefault();
          setPalette((p) => !p);
        }
        if (e.key.toLowerCase() === "b") {
          e.preventDefault();
          setCollapsed((c) => !c);
        }
      } else if (
        e.key === "/" &&
        !(
          e.target instanceof HTMLElement &&
          e.target.closest("input,textarea,select,[contenteditable=true]")
        )
      ) {
        e.preventDefault();
        filter.current?.focus();
      }
    };
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, []);
  const navigate = (next: Page) => {
    if (next === page) return;
    if (!confirmEditorNavigation()) return;
    setEditor(null);
    setContextBot(null);
    setSearch("");
    setPage(next);
    location.hash = next;
  };
  const edit: Edit = (kind, entity) => {
    if (!schemas[kind]?.properties) {
      notify(
        "Configuration schemas are not loaded. Refresh the dashboard and retry.",
        true,
      );
      return;
    }
    if (confirmEditorNavigation()) {
      setContextBot(null);
      setEditor({ kind, entity, token: ++editorSerial.current });
    }
  };
  const inspect = (bot: RecordData) => {
    if (confirmEditorNavigation()) {
      setEditor(null);
      setContextBot(bot);
    }
  };
  const act: Act = async (action, kind, id, data) => {
    setBusy(true);
    try {
      const result = await control(action, kind, id, data);
      await refreshAfterMutation();
      notify(
        action === "stop"
          ? "Paused. Active work has been cancelled."
          : action === "start"
            ? "Activation enabled."
            : "Council updated.",
      );
      return result;
    } catch (err) {
      notify(err instanceof Error ? err.message : "Action failed", true);
      return null;
    } finally {
      setBusy(false);
    }
  };
  if (authenticated === null)
    return <div className="boot">Connecting to Hortator…</div>;
  if (!authenticated) return <Login onLogin={() => setAuthenticated(true)} />;
  if (!dashboard || !stats)
    return (
      <div className="boot">
        <strong>hortator / workbench</strong>
        <p>{loadError || "Loading council state…"}</p>
        <button className="button" onClick={refresh}>
          Retry connection
        </button>
      </div>
    );
  const selected = navigation.find((n) => n.id === page)!;
  const active = dashboard.bots.filter(
    (b) =>
      b.enabled &&
      b.runtime.gateway_status === "online" &&
      dashboard.settings.enabled &&
      !b.readiness.length,
  ).length;
  const showFilter =
    page === "council" || page === "bots" || catalogKinds.includes(page);
  return (
    <div className={`workbench ${collapsed ? "nav-collapsed" : ""}`}>
      <header className="titlebar">
        <span className="app-mark">H</span>
        <strong>hortator</strong>
        <span className="title-divider">/</span>
        <span>council workbench</span>
        <button className="quick-open-button" onClick={() => setPalette(true)}>
          <Search size={13} />
          <span>Go to page or record</span>
          <kbd>Ctrl K</kbd>
        </button>
        <a
          href="/legacy/"
          className="legacy-link"
          onClick={(e) => {
            if (!confirmEditorNavigation()) e.preventDefault();
          }}
        >
          Legacy dashboard <ArrowUpRight size={12} />
        </a>
        <IconAction
          label="Sign out"
          onClick={async () => {
            if (!confirmEditorNavigation()) return;
            try {
              await api("/api/auth/logout", { method: "POST" });
              setCsrf("");
              setAuthenticated(false);
              setDashboard(null);
              setStats(null);
              setEditor(null);
              setContextBot(null);
            } catch (e) {
              notify(e instanceof Error ? e.message : "Sign out failed", true);
            }
          }}
        >
          <LogOut size={14} />
        </IconAction>
      </header>
      <div className="workbench-layout">
        <aside className="navigation">
          <div className="explorer-heading">
            <span>COUNCIL</span>
            <IconAction
              label={collapsed ? "Expand navigation" : "Collapse navigation"}
              onClick={() => setCollapsed((c) => !c)}
            >
              {collapsed ? (
                <PanelLeftOpen size={15} />
              ) : (
                <PanelLeftClose size={15} />
              )}
            </IconAction>
          </div>
          <nav aria-label="Workbench pages">
            {navigation.map((n, i) => (
              <div key={n.id}>
                {(!i || n.group !== navigation[i - 1].group) && (
                  <div className="nav-group">{n.group}</div>
                )}
                <button
                  className={`nav-item ${page === n.id ? "active" : ""}`}
                  aria-label={n.label}
                  aria-current={page === n.id ? "page" : undefined}
                  title={n.label}
                  onClick={() => navigate(n.id)}
                >
                  <n.icon size={15} />
                  <span>{n.label}</span>
                  {["bots", "providers", "profiles"].includes(n.id) && (
                    <small>{dashboard[n.id as "bots"].length}</small>
                  )}
                </button>
              </div>
            ))}
          </nav>
          <div className="nav-footer">
            <span className={`live-dot ${connected ? "online" : "offline"}`} />
            <span>
              {connected
                ? "Event stream connected"
                : "Event stream reconnecting"}
            </span>
          </div>
        </aside>
        <div className="workbench-stage">
          <Version version={dashboard.version} />
          <div className="page-toolbar">
            <selected.icon size={15} />
            <h1>{page === "council" ? "The council" : selected.label}</h1>
            {showFilter && (
              <div className="filter-box">
                <Search size={13} />
                <input
                  ref={filter}
                  aria-label={`Search ${page === "council" ? "bots" : page}`}
                  placeholder="Filter records…  /"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                {search && (
                  <IconAction
                    label="Clear filter"
                    onClick={() => setSearch("")}
                  >
                    <X size={12} />
                  </IconAction>
                )}
              </div>
            )}
            <div className="toolbar-actions">
              {(page === "council" || page === "bots") && (
                <button
                  className="button"
                  disabled={busy}
                  onClick={() =>
                    act(
                      dashboard.settings.enabled ? "stop" : "start",
                      undefined,
                      "all",
                    )
                  }
                >
                  {dashboard.settings.enabled ? (
                    <Pause size={13} />
                  ) : (
                    <Play size={13} />
                  )}
                  {dashboard.settings.enabled
                    ? "Pause council"
                    : "Resume council"}
                </button>
              )}
              {page === "settings" && (
                <>
                  <a href="/api/export/config" download className="button">
                    <ArrowDownToLine size={13} />
                    Export configuration
                  </a>
                  <button
                    className="button primary"
                    onClick={() => edit("settings", dashboard.settings)}
                  >
                    Edit council settings
                  </button>
                </>
              )}
              {addNames[page] && (
                <button
                  className="button primary"
                  onClick={() =>
                    edit(page === "council" ? "bots" : (page as Kind))
                  }
                >
                  <Plus size={13} />
                  Add {addNames[page]}
                </button>
              )}
              <IconAction label="Refresh dashboard" onClick={refresh}>
                <RefreshCw size={14} />
              </IconAction>
            </div>
          </div>
          {loadError && (
            <div className="connection-error" role="alert">
              {loadError} · Displaying the last received state.
              <button onClick={refresh}>Reconnect</button>
            </div>
          )}
          {dashboard.maintenance_pause ? (
            <div className="pause-banner">
              <Pause size={13} />
              Snapshot restore pause: gateways, bot turns and publishing are
              stopped.
              <button onClick={() => navigate("snapshots")}>
                Inspect and resume
              </button>
            </div>
          ) : (
            !dashboard.settings.enabled && (
              <div className="pause-banner">
                <Pause size={13} />
                The council is paused. Hortator’s commands remain available.
              </div>
            )
          )}
          <div className="workspace-panes">
            <main className="workbench-content" id="main-content">
              {page === "council" && (
                <>
                  <div className="metrics-strip">
                    <span>
                      Online{" "}
                      <strong>
                        {active} / {dashboard.bots.length}
                      </strong>
                    </span>
                    <span>
                      Input{" "}
                      <strong>{num(stats.total.input_tokens, true)}</strong>
                    </span>
                    <span>
                      Output{" "}
                      <strong>{num(stats.total.output_tokens, true)}</strong>
                    </span>
                    <span>
                      Reasoning{" "}
                      <strong>{num(stats.total.reasoning_tokens, true)}</strong>
                    </span>
                    <span>
                      TTFT <strong>{duration(stats.total.avg_ttft_ms)}</strong>
                      <small>p95 {duration(stats.total.p95_ttft_ms)}</small>
                    </span>
                    <span>
                      Cost <strong>{money(stats.total.cost)}</strong>
                      <small>
                        {stats.total.cost_known_requests || 0}/
                        {stats.total.requests || 0} known
                      </small>
                    </span>
                  </div>
                  <BotTable
                    dashboard={dashboard}
                    search={search}
                    edit={edit}
                    inspect={inspect}
                    act={act}
                    busy={busy}
                    selected={
                      editor?.kind === "bots"
                        ? editor.entity?.id
                        : contextBot?.id
                    }
                  />
                  {dashboard.bots.some((b) => b.readiness.length) && (
                    <details className="setup-checklist">
                      <summary>
                        <CircleAlert size={13} />
                        Setup required ·{" "}
                        {
                          dashboard.bots.filter((b) => b.readiness.length)
                            .length
                        }{" "}
                        bots
                        <ChevronRight size={12} />
                      </summary>
                      <div>
                        {dashboard.bots
                          .filter((b) => b.readiness.length)
                          .map((b) => (
                            <button key={b.id} onClick={() => edit("bots", b)}>
                              <strong>{b.name}</strong>
                              <span>{b.readiness.join(" · ")}</span>
                              <span>Edit →</span>
                            </button>
                          ))}
                      </div>
                    </details>
                  )}
                  <div className="overview-bottom">
                    <section className="panel">
                      <div className="panel-title">
                        <h2>Activity · last 24 hours</h2>
                        <button
                          className="text-button"
                          onClick={() => navigate("analytics")}
                        >
                          Analytics →
                        </button>
                      </div>
                      <UsageChart history={stats.history} />
                    </section>
                    <section className="panel">
                      <div className="panel-title">
                        <h2>Recent events</h2>
                        <button
                          className="text-button"
                          onClick={() => navigate("trajectory")}
                        >
                          Trajectory →
                        </button>
                      </div>
                      <div className="compact-events">
                        {events.slice(0, 8).map((e) => (
                          <button
                            key={e.id}
                            onClick={() => navigate("trajectory")}
                            title={eventText(e)}
                          >
                            <time>{timeLabel(e.at)}</time>
                            <span className={`event-dot ${e.level}`} />
                            <strong>{e.kind}</strong>
                            <span>
                              {e.bot_id || e.data?.provider_id || "system"}
                            </span>
                          </button>
                        ))}
                        {!events.length && (
                          <p className="muted">No events recorded.</p>
                        )}
                      </div>
                    </section>
                  </div>
                  <section className="connection-strip">
                    <strong>Providers</strong>
                    {dashboard.providers.map((p) => (
                      <button
                        onClick={() => edit("providers", p)}
                        key={p.id}
                        title={p.health.last_error || p.base_url}
                      >
                        <span
                          className={`live-dot ${p.enabled && p.health.last_success && !p.health.consecutive_failures ? "online" : "offline"}`}
                        />
                        {p.name}
                        <small>
                          {p.health.consecutive_failures
                            ? `${p.health.consecutive_failures} failures`
                            : !p.enabled
                              ? "paused"
                              : p.health.last_success
                                ? "healthy"
                                : "unverified"}
                        </small>
                      </button>
                    ))}
                  </section>
                </>
              )}
              {page === "bots" && (
                <BotTable
                  dashboard={dashboard}
                  search={search}
                  edit={edit}
                  inspect={inspect}
                  act={act}
                  busy={busy}
                  selected={editor?.entity?.id || contextBot?.id}
                />
              )}
              {catalogKinds.includes(page) && (
                <Catalog
                  key={page}
                  kind={page as Kind}
                  dashboard={dashboard}
                  search={search}
                  edit={edit}
                  act={act}
                  busy={busy}
                  notify={notify}
                  selected={editor?.entity?.id}
                />
              )}
              {page === "settings" && (
                <Settings dashboard={dashboard} edit={edit} />
              )}
              {page === "trajectory" && (
                <Trajectory dashboard={dashboard} notify={notify} />
              )}
              {page === "analytics" && <Analytics dashboard={dashboard} />}
              {page === "commands" && <Commands />}
              {page === "snapshots" && (
                <Snapshots dashboard={dashboard} refresh={refresh} />
              )}
            </main>
            {(editor || contextBot) && (
              <aside className="editor-pane" aria-label="Record inspector">
                {editor && (
                  <Editor
                    key={`${editor.kind}:${editor.entity?.id || "new"}:${editor.token}`}
                    kind={editor.kind}
                    entity={editor.entity}
                    dashboard={dashboard}
                    schemas={schemas}
                    close={() => setEditor(null)}
                    saved={async (value, keepOpen) => {
                      await refreshAfterMutation();
                      setEditor((current) =>
                        current?.token !== editor.token
                          ? current
                          : keepOpen
                            ? { ...current, entity: value }
                            : null,
                      );
                    }}
                    notify={notify}
                  />
                )}
                {contextBot && (
                  <ContextPanel
                    key={contextBot.id}
                    bot={
                      dashboard.bots.find((b) => b.id === contextBot.id) ||
                      contextBot
                    }
                    close={() => setContextBot(null)}
                    notify={notify}
                  />
                )}
              </aside>
            )}
          </div>
        </div>
      </div>
      <footer className="statusbar">
        <span className={`live-dot ${connected ? "online" : "offline"}`} />
        {connected ? "Live" : "Reconnecting"}
        <span>
          {active}/{dashboard.bots.length} bots online
        </span>
        <span>{dashboard.active_requests.length} active requests</span>
        <span className="status-spacer" />
        <span>{dashboard.settings.timezone}</span>
        <span>Updated {timeLabel(lastRefresh)}</span>
        <button onClick={() => setPalette(true)}>Ctrl K · quick open</button>
      </footer>
      {toast && (
        <div
          className={`toast ${toast.error ? "error" : ""}`}
          role={toast.error ? "alert" : "status"}
        >
          {toast.error ? <CircleAlert size={16} /> : <CheckCircle2 size={16} />}
          <span>{toast.text}</span>
          <IconAction
            label="Dismiss notification"
            onClick={() => setToast(null)}
          >
            <X size={13} />
          </IconAction>
        </div>
      )}
      {palette && (
        <QuickOpen
          dashboard={dashboard}
          close={() => setPalette(false)}
          navigate={navigate}
          edit={edit}
        />
      )}
    </div>
  );
}

function BotTable({
  dashboard: d,
  search,
  edit,
  inspect,
  act,
  busy,
  selected,
}: {
  dashboard: Dashboard;
  search: string;
  edit: Edit;
  inspect: (b: RecordData) => void;
  act: Act;
  busy: boolean;
  selected?: string;
}) {
  const profile = (b: RecordData) =>
    d.profiles.find((p) => p.id === b.model_profile_id);
  const provider = (b: RecordData) =>
    d.providers.find((p) => p.id === profile(b)?.provider_id);
  const used = (b: RecordData) =>
    Math.max(
      0,
      ...(b.contexts || []).map((c: RecordData) => c.estimated_tokens),
    );
  const rows = d.bots.filter((b) =>
    `${b.name} ${b.id} ${profile(b)?.model} ${provider(b)?.name} ${botStatus(b, d)[0]}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const columns: Column[] = [
    {
      id: "name",
      label: "Bot / ID",
      width: "17%",
      value: (b) => b.name,
      render: (b) => (
        <div className="bot-identity">
          <Avatar bot={b} size="tiny" />
          <RecordName item={b} edit={() => edit("bots", b)} />
          <span className="role-tag" title={b.role}>
            {b.role === "hortator" ? "H" : ""}
          </span>
        </div>
      ),
    },
    {
      id: "status",
      label: "State",
      width: "10%",
      value: (b) => botStatus(b, d)[0],
      render: (b) => {
        const [status, tone] = botStatus(b, d);
        return (
          <div title={b.runtime.error || b.readiness.join("\n")}>
            <Badge tone={tone}>{status}</Badge>
            <small className="cell-secondary">
              {b.active_turn
                ? "Turn in progress"
                : b.enabled && !b.readiness.length && d.settings.enabled
                  ? `Next ${Math.max(0, Math.ceil(b.runtime.next_at - d.now))}s`
                  : b.runtime.gateway_status}
            </small>
          </div>
        );
      },
    },
    {
      id: "model",
      label: "Model / profile",
      width: "22%",
      value: (b) => profile(b)?.model || "",
      render: (b) => (
        <button
          className="cell-link"
          onClick={() => profile(b) && edit("profiles", profile(b))}
          title={profile(b)?.model}
        >
          <span>{profile(b)?.model || "No model"}</span>
          <small>{profile(b)?.name || b.model_profile_id}</small>
        </button>
      ),
    },
    {
      id: "provider",
      label: "Provider",
      width: "12%",
      value: (b) => provider(b)?.name || "",
      render: (b) => (
        <button
          className="cell-link"
          onClick={() => provider(b) && edit("providers", provider(b))}
          title={provider(b)?.name}
        >
          {provider(b)?.name || "Not assigned"}
        </button>
      ),
    },
    {
      id: "context",
      label: "Context",
      width: "14%",
      value: (b) => used(b),
      render: (b) => {
        const capacity = profile(b)?.context_window;
        const percentage = capacity
          ? Math.min(100, (used(b) / capacity) * 100)
          : 0;
        return (
          <button
            className="compact-meter"
            aria-label={`Context & memory for ${b.name}`}
            title={`${num(used(b))} / ${num(capacity)} estimated tokens; opens context & memory`}
            onClick={() => inspect(b)}
          >
            <span>
              {num(used(b), true)} / {num(capacity, true)}
              <strong>{percentage.toFixed(0)}%</strong>
            </span>
            <i>
              <em
                style={{
                  width: `${percentage}%`,
                  background:
                    percentage > 70 ? "var(--amber)" : "var(--accent)",
                }}
              />
            </i>
          </button>
        );
      },
    },
    {
      id: "cadence",
      label: "Wake / send gap",
      width: "11%",
      value: (b) => b.interval_seconds,
      render: (b) => (
        <span title="Activation interval / minimum send interval">
          {b.interval_seconds}s / {b.cooldown_seconds}s
          <small className="cell-secondary">
            {b.enabled_plugins.length} plugins · {b.room_ids.length} rooms
          </small>
        </span>
      ),
    },
    {
      id: "actions",
      label: "Actions",
      width: "14%",
      render: (b) => (
        <div className="row-actions">
          <IconAction
            label={`Edit ${b.name} settings`}
            onClick={() => edit("bots", b)}
          >
            <Settings2 size={14} />
          </IconAction>
          <IconAction
            label={`Inspect ${b.name} context`}
            onClick={() => inspect(b)}
          >
            <Layers3 size={14} />
          </IconAction>
          {b.readiness.length ? (
            <button className="text-button" onClick={() => edit("bots", b)}>
              Set up
            </button>
          ) : (
            <button
              className="button small"
              disabled={busy}
              aria-label={`${b.enabled ? "Pause" : "Activate"} ${b.name}`}
              onClick={() => act(b.enabled ? "stop" : "start", "bots", b.id)}
            >
              {b.enabled ? <Pause size={12} /> : <Play size={12} />}
              {b.enabled ? "Pause" : "Start"}
            </button>
          )}
        </div>
      ),
    },
  ];
  return (
    <>
      <div className="record-count">
        {rows.length} of {d.bots.length} bots
        <span>
          Click a name to edit · context to inspect · column header to sort
        </span>
      </div>
      <WorkbenchTable
        label="Bots"
        rows={rows}
        columns={columns}
        selected={selected}
        rowClass={(b) => (b.active_turn ? "thinking" : "")}
      />
    </>
  );
}

function ProfileStreamToggle({
  profile,
  busy,
  act,
}: {
  profile: RecordData;
  busy: boolean;
  act: Act;
}) {
  const [pendingValue, setPendingValue] = useState<boolean | null>(null);
  const value = pendingValue ?? profile.stream !== false;
  return (
    <label
      className="table-checkbox"
      title={
        value
          ? "Streamed response; timing available when reported"
          : "Complete JSON response; TTFT/TPS unavailable"
      }
    >
      <input
        type="checkbox"
        aria-label={`SSE streaming for ${profile.name}`}
        checked={value}
        disabled={busy || pendingValue !== null}
        onChange={async (event) => {
          const next = event.target.checked;
          setPendingValue(next);
          try {
            await act("save", "profiles", profile.id, {
              stream: next,
              revision: profile.revision,
            });
          } finally {
            setPendingValue(null);
          }
        }}
      />
      <span>{pendingValue === null ? (value ? "On" : "Off") : "Saving…"}</span>
    </label>
  );
}

function Catalog({
  kind,
  dashboard: d,
  search,
  edit,
  act,
  busy,
  notify,
  selected,
}: {
  kind: Kind;
  dashboard: Dashboard;
  search: string;
  edit: Edit;
  act: Act;
  busy: boolean;
  notify: Notify;
  selected?: string;
}) {
  const [probe, setProbe] = useState<RecordData | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [threadNames, setThreadNames] = useState<Record<string, string>>({});
  const items: RecordData[] = kind === "settings" ? [] : d[kind];
  const rows = items.filter((e) =>
    `${e.name} ${e.id} ${e.model || ""} ${e.base_url || ""} ${e.description || ""} ${e.provider_id || ""}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const columns: Column[] = [
    {
      id: "name",
      label: "Name / ID",
      width: kind === "providers" ? "19%" : "20%",
      value: (e) => e.name,
      render: (e) => <RecordName item={e} edit={() => edit(kind, e)} />,
    },
  ];
  if (kind === "providers")
    columns.push(
      {
        id: "state",
        label: "State",
        width: "9%",
        value: (e) =>
          e.enabled
            ? e.health.consecutive_failures
              ? "Degraded"
              : e.health.last_success
                ? "Healthy"
                : "Unverified"
            : "Paused",
        render: (e) => (
          <Badge
            tone={
              !e.enabled
                ? "amber"
                : e.health.consecutive_failures
                  ? "red"
                  : e.health.last_success
                    ? "green"
                    : "neutral"
            }
          >
            {!e.enabled
              ? "Paused"
              : e.health.consecutive_failures
                ? "Degraded"
                : e.health.last_success
                  ? "Healthy"
                  : "Unverified"}
          </Badge>
        ),
      },
      {
        id: "endpoint",
        label: "Endpoint",
        width: "24%",
        value: (e) => e.base_url,
        render: (e) => (
          <div className="clip" title={e.base_url}>
            <code>{e.base_url}</code>
            <small className="cell-secondary">
              {e.kind === "openrouter" ? "OpenRouter" : "OpenAI compatible"}
            </small>
          </div>
        ),
      },
      {
        id: "credential",
        label: "Credential",
        width: "11%",
        value: (e) => (e.key_configured ? 1 : 0),
        render: (e) => (
          <span
            className={
              e.requires_key && !e.key_configured ? "amber-text" : "muted"
            }
          >
            {e.key_configured
              ? "Configured"
              : e.requires_key
                ? "Missing"
                : "Not required"}
          </span>
        ),
      },
      {
        id: "profiles",
        label: "Models",
        width: "6%",
        value: (e) => d.profiles.filter((p) => p.provider_id === e.id).length,
        render: (e) => d.profiles.filter((p) => p.provider_id === e.id).length,
      },
      {
        id: "failures",
        label: "Failures",
        width: "9%",
        value: (e) => e.health.consecutive_failures,
        render: (e) => (
          <span
            title={`Consecutive: ${e.health.consecutive_failures}. Last 15 minutes: ${e.recent?.failures || 0} / ${e.recent?.requests || 0} requests.`}
          >
            {e.health.consecutive_failures}
            <small className="cell-secondary">
              15m {e.recent?.failures || 0} / {e.recent?.requests || 0}
            </small>
          </span>
        ),
      },
      {
        id: "limits",
        label: "Calls / timeout",
        width: "10%",
        value: (e) => e.timeout_seconds,
        render: (e) => (
          <span>
            {e.max_concurrency} / {e.timeout_seconds}s
          </span>
        ),
      },
      {
        id: "actions",
        label: "Actions",
        width: "12%",
        render: (e) => (
          <div className="row-actions">
            <IconAction
              label={`Discover models for ${e.name}`}
              disabled={busy}
              onClick={async () => {
                setProbe(null);
                const result = await act("probe", "providers", e.id);
                if (result) setProbe({ ...result, provider_name: e.name });
              }}
            >
              <Activity size={14} />
            </IconAction>
            <IconAction
              label={`${e.enabled ? "Pause" : "Enable"} ${e.name}`}
              disabled={busy}
              onClick={() => act(e.enabled ? "stop" : "start", kind, e.id)}
            >
              {e.enabled ? <Pause size={14} /> : <Play size={14} />}
            </IconAction>
            <IconAction
              label={`Details for ${e.name}`}
              onClick={() => setExpanded(expanded === e.id ? null : e.id)}
            >
              <ChevronRight size={14} />
            </IconAction>
          </div>
        ),
      },
    );
  if (kind === "profiles")
    columns.push(
      {
        id: "model",
        label: "Model / provider",
        width: "25%",
        value: (e) => e.model,
        render: (e) => (
          <div className="clip" title={e.model}>
            <code>{e.model}</code>
            <button
              className="cell-secondary text-button"
              onClick={() => {
                const provider = d.providers.find(
                  (p) => p.id === e.provider_id,
                );
                if (provider) edit("providers", provider);
              }}
            >
              {d.providers.find((p) => p.id === e.provider_id)?.name ||
                e.provider_id}
            </button>
          </div>
        ),
      },
      {
        id: "capacity",
        label: "Context / compact",
        width: "13%",
        value: (e) => e.context_window,
        render: (e) => (
          <span>
            {num(e.context_window, true)}
            <small className="cell-secondary">
              at {Math.round(e.compact_threshold * 100)}%
            </small>
          </span>
        ),
      },
      {
        id: "summary",
        label: "Summary / reserve",
        width: "13%",
        value: (e) => e.summary_tokens,
        render: (e) => (
          <span>
            {num(e.summary_tokens, true)} / {num(e.response_tokens, true)}
            <small className="cell-secondary">retained text / response</small>
          </span>
        ),
      },
      {
        id: "reasoning",
        label: "Reasoning",
        width: "12%",
        value: (e) => reasoningSummary(e.request_json),
        render: (e) => (
          <span className="clip" title={reasoningSummary(e.request_json)}>
            {reasoningSummary(e.request_json)}
          </span>
        ),
      },
      {
        id: "stream",
        label: "SSE",
        width: "7%",
        value: (e) => (e.stream === false ? 0 : 1),
        render: (e) => (
          <ProfileStreamToggle profile={e} busy={busy} act={act} />
        ),
      },
      {
        id: "actions",
        label: "Actions",
        width: "10%",
        render: (e) => (
          <div className="row-actions">
            <IconAction
              label={`Clone ${e.name}`}
              onClick={async () => {
                const result = await act("clone", "profiles", e.id);
                if (result) edit(kind, result);
              }}
              disabled={busy}
            >
              <Layers3 size={14} />
            </IconAction>
            <IconAction
              label={`Details for ${e.name}`}
              onClick={() => setExpanded(expanded === e.id ? null : e.id)}
            >
              <ChevronRight size={14} />
            </IconAction>
          </div>
        ),
      },
    );
  if (kind === "prompts")
    columns.push(
      {
        id: "content",
        label: "Instructions",
        width: "60%",
        value: (e) => e.content,
        render: (e) => (
          <span className="clip" title={e.content}>
            {e.content || "No prompt content yet."}
          </span>
        ),
      },
      {
        id: "grants",
        label: "Bot grants",
        width: "10%",
        value: (e) => d.bots.filter((b) => b.prompt_ids.includes(e.id)).length,
        render: (e) => d.bots.filter((b) => b.prompt_ids.includes(e.id)).length,
      },
      {
        id: "revision",
        label: "Revision",
        width: "10%",
        value: (e) => e.revision,
        render: (e) => e.revision,
      },
    );
  if (kind === "plugins")
    columns.push(
      {
        id: "state",
        label: "Global state",
        width: "12%",
        value: (e) => (e.enabled ? 1 : 0),
        render: (e) => (
          <Badge tone={e.enabled ? "green" : "neutral"}>
            {e.enabled ? "Enabled" : "Disabled"}
          </Badge>
        ),
      },
      {
        id: "description",
        label: "Capability",
        width: "40%",
        value: (e) => e.description,
        render: (e) => (
          <span className="clip" title={e.description}>
            {e.description}
          </span>
        ),
      },
      {
        id: "credential",
        label: "Credential",
        width: "13%",
        render: (e) => (
          <span>
            {e.key_configured
              ? "Configured"
              : e.keyless
                ? "Not required"
                : "Not configured"}
          </span>
        ),
      },
      {
        id: "grants",
        label: "Grants",
        width: "5%",
        value: (e) =>
          d.bots.filter((b) => b.enabled_plugins.includes(e.id)).length,
        render: (e) =>
          d.bots.filter((b) => b.enabled_plugins.includes(e.id)).length,
      },
      {
        id: "actions",
        label: "Actions",
        width: "10%",
        render: (e) => (
          <button
            className="button small"
            disabled={busy}
            onClick={() => act(e.enabled ? "stop" : "start", kind, e.id)}
          >
            {e.enabled ? "Disable" : "Enable globally"}
          </button>
        ),
      },
    );
  if (kind === "rooms")
    columns.push(
      {
        id: "guild",
        label: "Guild / channel",
        width: "25%",
        value: (e) => e.channel_id || "",
        render: (e) => (
          <span className="mono">
            {e.guild_id || "Guild not set"}
            <small className="cell-secondary">
              {e.channel_id || "Channel not set"}
            </small>
          </span>
        ),
      },
      {
        id: "scope",
        label: "Intake / threads",
        width: "22%",
        render: (e) => (
          <span>
            {e.include_threads ? "Isolated per thread" : "Channel only"}
            <small className="cell-secondary">
              {e.allow_external_bots
                ? "External bots & apps allowed"
                : "External bots excluded"}
            </small>
          </span>
        ),
      },
      {
        id: "grants",
        label: "Members / gap",
        width: "13%",
        value: (e) => d.bots.filter((b) => b.room_ids.includes(e.id)).length,
        render: (e) => (
          <span>
            {d.bots
              .filter((b) => b.room_ids.includes(e.id))
              .map((b) => b.name)
              .join(", ") || "No bots"}
            <small className="cell-secondary">
              {e.send_gap_seconds}s send gap
            </small>
          </span>
        ),
      },
      {
        id: "thread",
        label: "New thread",
        width: "20%",
        render: (e) => (
          <div className="thread-form">
            <input
              aria-label={`New thread in ${e.name}`}
              placeholder="Thread name…"
              value={threadNames[e.id] || ""}
              onChange={(event) =>
                setThreadNames((current) => ({
                  ...current,
                  [e.id]: event.target.value,
                }))
              }
            />
            <IconAction
              label={`Create thread in ${e.name}`}
              disabled={!threadNames[e.id]?.trim() || busy}
              onClick={async () => {
                const submittedName = threadNames[e.id];
                const r = await act("thread", "rooms", e.id, {
                  name: submittedName,
                });
                if (r) {
                  setThreadNames((current) =>
                    current[e.id] === submittedName
                      ? { ...current, [e.id]: "" }
                      : current,
                  );
                  notify(`Thread created: ${r.thread_id}`);
                }
              }}
            >
              <Plus size={14} />
            </IconAction>
          </div>
        ),
      },
    );
  const item = items.find((e) => e.id === expanded);
  return (
    <>
      <div className="record-count">
        {rows.length} of {items.length}{" "}
        {kind === "profiles" ? "model profiles" : kind}
        <span>Click a name to edit · column header to sort</span>
      </div>
      <WorkbenchTable
        label={kind}
        columns={columns}
        rows={rows}
        selected={selected}
      />
      {item && (
        <section className="record-details panel">
          <div className="panel-title">
            <h2>{item.name} · details</h2>
            <IconAction
              label="Close record details"
              onClick={() => setExpanded(null)}
            >
              <X size={14} />
            </IconAction>
          </div>
          {kind === "providers" ? (
            <>
              <dl className="detail-list">
                <div>
                  <dt>Credential</dt>
                  <dd>
                    {item.key_configured
                      ? "Encrypted · configured"
                      : item.requires_key
                        ? "Not configured"
                        : "Not required"}
                  </dd>
                </div>
                <div>
                  <dt>Last success</dt>
                  <dd>{dateLabel(item.health.last_success)}</dd>
                </div>
                <div>
                  <dt>Last error</dt>
                  <dd>{item.health.last_error || "None recorded"}</dd>
                </div>
              </dl>
              {item.health.circuit_until > d.now && (
                <button
                  className="button"
                  onClick={() => act("reset_circuit", "providers", item.id)}
                >
                  Reset circuit
                </button>
              )}
            </>
          ) : (
            <>
              <dl className="detail-list">
                <div>
                  <dt>Retained summary limit</dt>
                  <dd>{num(item.summary_tokens)} text tokens</dd>
                </div>
                <div>
                  <dt>Compaction total-output cap</dt>
                  <dd>Not sent · provider default</dd>
                </div>
                <div>
                  <dt>Generation output cap</dt>
                  <dd>
                    {[
                      "max_tokens",
                      "max_completion_tokens",
                      "max_output_tokens",
                    ]
                      .filter((k) => Object.hasOwn(item.request_json, k))
                      .map(
                        (k) => `${k}: ${JSON.stringify(item.request_json[k])}`,
                      )
                      .join(" · ") || "Not specified"}
                  </dd>
                </div>
                <div>
                  <dt>Reasoning configuration</dt>
                  <dd>{reasoningSummary(item.request_json)}</dd>
                </div>
                <div>
                  <dt>Compaction reasoning overrides</dt>
                  <dd>
                    {reasoningSummary(item.compaction_request_json || {})}
                  </dd>
                </div>
                <div>
                  <dt>Bots assigned</dt>
                  <dd>
                    {d.bots
                      .filter((b) => b.model_profile_id === item.id)
                      .map((b) => b.name)
                      .join(", ") || "None"}
                  </dd>
                </div>
              </dl>
              <Code value={item.request_json} label="Request parameters" />
            </>
          )}
        </section>
      )}
      {probe && (
        <section className="probe-result panel">
          <div className="panel-title">
            <h2>
              Model discovery · {probe.provider_name} ·{" "}
              {duration(probe.latency_ms)}
            </h2>
            <IconAction
              label="Dismiss model discovery"
              onClick={() => setProbe(null)}
            >
              <X size={14} />
            </IconAction>
          </div>
          <Notice>{probe.note}</Notice>
          <Code
            value={probe.models}
            label={`${probe.models.length} discovered models`}
          />
        </section>
      )}
    </>
  );
}

function Settings({
  dashboard: d,
  edit,
}: {
  dashboard: Dashboard;
  edit: Edit;
}) {
  return (
    <div className="settings-workspace">
      <section className="panel">
        <div className="panel-title">
          <h2>Runtime configuration</h2>
        </div>
        <dl className="detail-list">
          {[
            ["Owner", `${d.owner_id} · .normal.man.`],
            [
              "Hortator control channel",
              d.settings.control_channel_id || "Not configured",
            ],
            ["Control guild", d.settings.control_guild_id || "Not configured"],
            ["Timezone", d.settings.timezone],
            ["Concurrent turns", d.settings.max_concurrent_turns],
            [
              "Incident notifications",
              d.settings.incident_notifications ? "Enabled" : "Disabled",
            ],
          ].map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <Notice>
          Credentials are write-only and stored encrypted. Configuration exports
          omit credentials; complete database/key backups use the local CLI.
        </Notice>
      </section>
      <section className="panel">
        <div className="panel-title">
          <h2>Shared system prompt</h2>
          <button
            className="text-button"
            onClick={() => edit("settings", d.settings)}
          >
            Edit shared prompt
          </button>
        </div>
        <pre className="prompt-full">{d.settings.global_prompt}</pre>
      </section>
    </div>
  );
}

function Commands() {
  const [commands, setCommands] = useState<RecordData[]>([]),
    [error, setError] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    api("/api/commands")
      .then(setCommands)
      .catch((e) => setError(e.message));
  }, []);
  return (
    <>
      <div className="record-count">
        Deterministic Discord commands
        <input
          aria-label="Search commands"
          placeholder="Filter commands…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {error && <Notice warning>{error}</Notice>}
      <table className="workbench-table commands-table">
        <thead>
          <tr>
            <th>Command</th>
            <th>Purpose</th>
          </tr>
        </thead>
        <tbody>
          {commands
            .filter((c) =>
              `${c.command} ${c.description}`
                .toLowerCase()
                .includes(query.toLowerCase()),
            )
            .map((c) => (
              <tr key={c.command}>
                <td>
                  <code>{c.command}</code>
                </td>
                <td>{c.description}</td>
              </tr>
            ))}
        </tbody>
      </table>
      <Notice>
        Commands execute directly. Ask Hortator a normal question to use its
        model. Use <code>!dm !status</code> for a private report; attach UTF-8
        JSON/text for longer configuration edits.
      </Notice>
    </>
  );
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <main className="login-page">
      <section className="login-card">
        <div className="login-heading">
          <span className="app-mark">H</span>
          <span>hortator / workbench</span>
        </div>
        <h1>Open council control</h1>
        <p>Private operator workspace</p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              const r = await api("/api/auth/login", {
                method: "POST",
                body: JSON.stringify({ password }),
              });
              setCsrf(r.csrf);
              setPassword("");
              onLogin();
            } catch (err) {
              setError(err instanceof Error ? err.message : "Sign in failed");
            } finally {
              setBusy(false);
            }
          }}
        >
          <Field label="Dashboard password">
            <input
              autoFocus
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}
          <button className="button primary" disabled={busy}>
            {busy ? "Signing in…" : "Enter council control"}
            <ChevronRight size={15} />
          </button>
        </form>
        <a className="text-button" href="/legacy/">
          Open frozen legacy dashboard ↗
        </a>
      </section>
    </main>
  );
}

function QuickOpen({
  dashboard,
  close,
  navigate,
  edit,
}: {
  dashboard: Dashboard;
  close: () => void;
  navigate: (page: Page) => void;
  edit: Edit;
}) {
  const [query, setQuery] = useState(""),
    [selected, setSelected] = useState(0);
  const actions = useMemo(
    () =>
      [
        ...navigation.map((n) => ({
          id: n.id,
          label: n.label,
          group: "Page",
          run: () => navigate(n.id),
        })),
        ...(
          [
            "bots",
            "providers",
            "profiles",
            "prompts",
            "plugins",
            "rooms",
          ] as Kind[]
        ).flatMap((kind) =>
          (dashboard[kind] as RecordData[]).map((e) => ({
            id: `${kind}:${e.id}`,
            label: e.name,
            group: `${kind} / ${e.id}`,
            run: () => edit(kind, e),
          })),
        ),
      ]
        .filter((e) =>
          `${e.label} ${e.group}`.toLowerCase().includes(query.toLowerCase()),
        )
        .slice(0, 30),
    [dashboard, query, navigate, edit],
  );
  const previous = useRef(document.activeElement as HTMLElement | null);
  const paletteRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    paletteRef.current
      ?.querySelector(`[data-option-index="${selected}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [selected]);
  useEffect(() => {
    return () => {
      if (previous.current?.isConnected) previous.current.focus();
    };
  }, []);
  const choose = (index: number) => {
    const action = actions[index];
    if (action) {
      close();
      action.run();
    }
  };
  return (
    <div
      className="palette-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        ref={paletteRef}
        className="quick-palette"
        role="dialog"
        aria-modal="true"
        aria-label="Quick open"
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            close();
          }
          if (e.key === "Tab") {
            e.preventDefault();
            paletteRef.current?.querySelector("input")?.focus();
          }
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setSelected((s) => Math.min(actions.length - 1, s + 1));
          }
          if (e.key === "ArrowUp") {
            e.preventDefault();
            setSelected((s) => Math.max(0, s - 1));
          }
          if (e.key === "Enter") {
            e.preventDefault();
            choose(selected);
          }
        }}
      >
        <div className="palette-search">
          <Search size={16} />
          <input
            autoFocus
            aria-label="Go to page or record"
            value={query}
            placeholder="Search pages, bots, models, providers…"
            onChange={(e) => {
              setQuery(e.target.value);
              setSelected(0);
            }}
          />
          <kbd>Esc</kbd>
        </div>
        <div
          className="palette-results"
          role="listbox"
          aria-label="Search results"
        >
          {actions.map((action, index) => (
            <button
              key={action.id}
              role="option"
              tabIndex={-1}
              data-option-index={index}
              aria-selected={index === selected}
              onMouseMove={() => setSelected(index)}
              onClick={() => choose(index)}
            >
              <strong>{action.label}</strong>
              <small>{action.group}</small>
              <span>↵</span>
            </button>
          ))}
          {!actions.length && <p>No matching pages or records.</p>}
        </div>
      </div>
    </div>
  );
}
