import { useCallback, useEffect, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  AudioLines,
  Bot,
  Cable,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleHelp,
  Clock3,
  Command,
  Cpu,
  FileText,
  Hash,
  KeyRound,
  Layers3,
  LayoutDashboard,
  LockKeyhole,
  LogOut,
  Menu,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  Radio,
  RefreshCw,
  Settings2,
  ShieldCheck,
  Sparkles,
  Square,
  Terminal,
  Unplug,
  Users,
  X,
  Zap,
} from "lucide-react";
import {
  api,
  ApiError,
  control,
  duration,
  eventText,
  money,
  num,
  setCsrf,
  timeLabel,
} from "./api";
import type { Dashboard, Kind, Page, RecordData } from "./api";
import { reasoningSummary } from "./Reasoning";
import { Version } from "./Version";
import {
  Avatar,
  Badge,
  Code,
  Empty,
  Field,
  Logo,
  Metric,
  Notice,
  Switch,
} from "./components";
import { Editor, ContextPanel } from "./Editor";
import { Trajectory } from "./Trajectory";
import { Analytics, UsageChart } from "./Analytics";

const navigation: {
  id: Page;
  label: string;
  icon: typeof Bot;
  group: string;
}[] = [
  {
    id: "council",
    label: "Overview",
    icon: LayoutDashboard,
    group: "WORKSPACE",
  },
  { id: "trajectory", label: "Trajectory", icon: Activity, group: "WORKSPACE" },
  { id: "analytics", label: "Analytics", icon: AudioLines, group: "WORKSPACE" },
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
const titles: Record<Page, [string, string]> = {
  council: ["The council", "Independent minds. One shared conversation."],
  bots: [
    "Your bots",
    "Distinct applications, personalities, and capabilities.",
  ],
  trajectory: [
    "Trajectory",
    "Every decision, request, and delivery. Nothing lost between the lines.",
  ],
  analytics: [
    "Council analytics",
    "Understand what each mind consumes, and how it performs.",
  ],
  providers: [
    "Providers",
    "Connections, credentials, and the health of your inference stack.",
  ],
  profiles: [
    "Model profiles",
    "Fine control of the model. Independent of the personality.",
  ],
  prompts: [
    "Prompt library",
    "The shared instructions and individual voices of your council.",
  ],
  plugins: [
    "Plugin registry",
    "Enable a capability globally, then grant it to individual bots.",
  ],
  rooms: [
    "Council rooms",
    "Give the conversation a place, with a little room to breathe.",
  ],
  settings: [
    "Council settings",
    "The shared context and operating rules of your council.",
  ],
  commands: [
    "Discord commands",
    "The same controls, directly from your reporting channel or a DM.",
  ],
};
function botStatus(bot: RecordData, dashboard: Dashboard): [string, string] {
  if (bot.readiness.length) return ["Draft", "neutral"];
  if (!dashboard.settings.enabled || !bot.enabled) return ["Paused", "amber"];
  const profile = dashboard.profiles.find((p) => p.id === bot.model_profile_id);
  const provider = dashboard.providers.find(
    (p) => p.id === profile?.provider_id,
  );
  if (!provider?.enabled) return ["Provider paused", "amber"];
  if (bot.runtime.gateway_status !== "online")
    return [
      bot.runtime.gateway_status,
      bot.runtime.gateway_status === "failed" ? "red" : "neutral",
    ];
  if (provider.health.circuit_until > dashboard.now)
    return ["Circuit open", "red"];
  if (bot.active_turn) return ["Thinking", "violet"];
  if (bot.runtime.error) return ["Attention", "red"];
  return ["Listening", "green"];
}

export default function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [dashboard, setDashboard] = useState<Dashboard | null>(null);
  const [stats, setStats] = useState<RecordData | null>(null);
  const [events, setEvents] = useState<RecordData[]>([]);
  const [schemas, setSchemas] = useState<RecordData>({});
  const [page, setPage] = useState<Page>(() =>
    navigation.some((n) => n.id === location.hash.slice(1))
      ? (location.hash.slice(1) as Page)
      : "council",
  );
  const [editor, setEditor] = useState<{
    kind: Kind;
    entity?: RecordData;
  } | null>(null);
  const [contextBot, setContextBot] = useState<RecordData | null>(null);
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(
    null,
  );
  const [connected, setConnected] = useState(false);
  const [sidebar, setSidebar] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [busy, setBusy] = useState(false);
  const notify = useCallback((text: string, error = false) => {
    setToast({ text, error });
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), toast.error ? 12000 : 5000);
    return () => clearTimeout(timer);
  }, [toast]);
  const refresh = useCallback(async () => {
    try {
      const [data, metrics, ledger] = await Promise.all([
        api<Dashboard>("/api/status"),
        api("/api/stats"),
        api("/api/events?limit=15"),
      ]);
      setDashboard(data);
      setStats(metrics);
      setEvents(ledger);
      setLoadError("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401)
        setAuthenticated(false);
      else
        setLoadError(err instanceof Error ? err.message : "Connection failed");
    }
  }, []);
  useEffect(() => {
    api("/api/auth/session")
      .then((data) => {
        setCsrf(data.csrf);
        setAuthenticated(true);
      })
      .catch(() => setAuthenticated(false));
  }, []);
  useEffect(() => {
    if (!authenticated) return;
    refresh();
    api("/api/config-schemas")
      .then(setSchemas)
      .catch((err) => notify(err.message, true));
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
    location.hash = page;
  }, [page]);
  useEffect(() => {
    const change = () => {
      const hash = location.hash.slice(1);
      if (navigation.some((n) => n.id === hash)) setPage(hash as Page);
    };
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);
  const navigate = (value: Page) => {
    setPage(value);
    setSidebar(false);
  };
  const act = async (
    action: string,
    kind?: string,
    id?: string,
    data?: RecordData,
  ) => {
    setBusy(true);
    try {
      const result = await control(action, kind, id, data);
      await refresh();
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
    return (
      <div className="boot">
        <Logo />
        <span>Opening council control…</span>
      </div>
    );
  if (!authenticated) return <Login onLogin={() => setAuthenticated(true)} />;
  if (!dashboard || !stats)
    return (
      <div className="boot">
        <Logo />
        <span>{loadError || "Loading the council…"}</span>
        {loadError && (
          <button className="button" onClick={refresh}>
            Try again
          </button>
        )}
      </div>
    );
  const edit = (kind: Kind, entity?: RecordData) => setEditor({ kind, entity });
  const activeBots = dashboard.bots.filter(
    (b) =>
      b.enabled &&
      b.runtime.gateway_status === "online" &&
      dashboard.settings.enabled &&
      !b.readiness.length,
  ).length;
  const fields = titles[page];
  return (
    <div className="app-shell">
      {sidebar && (
        <button
          className="sidebar-shade"
          aria-label="Close navigation"
          onClick={() => setSidebar(false)}
        />
      )}
      <aside className={`sidebar ${sidebar ? "shown" : ""}`}>
        <a
          className="brand"
          href="#council"
          onClick={() => navigate("council")}
        >
          <Logo small />
          <span>
            hortator<span className="brand-dot">.</span>
          </span>
        </a>
        <div className="workspace-switch">
          <span className="workspace-icon">
            <Users size={16} />
          </span>
          <div>
            <strong>The council</strong>
            <small>Personal workspace</small>
          </div>
          <ChevronRight size={15} />
        </div>
        <nav>
          {navigation.map((nav, index) => (
            <div key={nav.id}>
              {(index === 0 || navigation[index - 1].group !== nav.group) && (
                <div className="nav-label">{nav.group}</div>
              )}
              <button
                aria-label={nav.label}
                className={`nav-item ${page === nav.id ? "active" : ""}`}
                onClick={() => navigate(nav.id)}
              >
                <nav.icon size={17} />
                <span>{nav.label}</span>
                {nav.id === "bots" && (
                  <span className="nav-count">{dashboard.bots.length}</span>
                )}
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="runtime-note">
            <span className="live-dot" />
            <span>
              Runtime connected<small>One process · persistent history</small>
            </span>
            <Radio size={15} />
          </div>
          <div className="owner">
            <span className="owner-avatar">
              N<span />
            </span>
            <div>
              <strong>.normal.man.</strong>
              <small>
                The Boss <ShieldCheck size={11} />
              </small>
            </div>
            <button
              className="icon-button"
              aria-label="Sign out"
              onClick={async () => {
                await api("/api/auth/logout", { method: "POST" });
                setAuthenticated(false);
              }}
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              onClick={() => setSidebar(true)}
              aria-label="Open navigation"
            >
              <Menu size={20} />
            </button>
            <span>Council workspace</span>
            <ChevronRight size={13} />
            <strong>{navigation.find((n) => n.id === page)?.label}</strong>
          </div>
          <div className="topbar-right">
            <span className="owner-lock">
              <LockKeyhole size={13} />
              Owner access
            </span>
            <span
              className={`live-status ${connected && !loadError ? "" : "disconnected"}`}
            >
              <span className="live-dot" />
              {connected && !loadError ? "Live" : "Reconnecting"}
            </span>
            <button
              className="icon-button"
              aria-label="Refresh dashboard"
              onClick={refresh}
            >
              <RefreshCw size={15} />
            </button>
          </div>
        </header>
        <main>
          <Version version={dashboard.version} />
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                {page === "council"
                  ? "COUNCIL CONTROL"
                  : page === "trajectory" || page === "analytics"
                    ? "OBSERVABILITY"
                    : "YOUR WORKSPACE"}
              </div>
              <h1>{fields[0]}</h1>
              <p>{fields[1]}</p>
            </div>
            <div className="heading-actions">
              {page === "council" ? (
                <>
                  <button
                    className={`button ${dashboard.settings.enabled ? "danger-subtle" : ""}`}
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
                      <Square size={14} />
                    ) : (
                      <Play size={14} />
                    )}{" "}
                    {dashboard.settings.enabled
                      ? "Pause council"
                      : "Resume council"}
                  </button>
                  <button
                    className="button primary"
                    onClick={() => edit("bots")}
                  >
                    <Plus size={16} />
                    Add bot
                  </button>
                </>
              ) : [
                  "bots",
                  "providers",
                  "profiles",
                  "prompts",
                  "rooms",
                ].includes(page) ? (
                <button
                  className="button primary"
                  onClick={() => edit(page as Kind)}
                >
                  <Plus size={16} />
                  Add{" "}
                  {page === "profiles"
                    ? "profile"
                    : page === "prompts"
                      ? "prompt"
                      : page.slice(0, -1)}
                </button>
              ) : null}
            </div>
          </div>
          {loadError && (
            <div className="connection-error" role="alert">
              <Unplug size={16} />
              {loadError} · Displaying the last received state.
              <button onClick={refresh}>Reconnect</button>
            </div>
          )}
          {!dashboard.settings.enabled && (
            <div className="pause-banner">
              <Pause size={16} />
              <span>
                The council is paused. Hortator’s commands remain available.
              </span>
              <button onClick={() => act("start", undefined, "all")}>
                Resume council <ArrowRight size={14} />
              </button>
            </div>
          )}
          {page === "council" && (
            <>
              <div className="metric-grid">
                <Metric
                  label="Active bots"
                  value={
                    <>
                      {activeBots}
                      <span className="metric-denominator">
                        {" "}
                        / {dashboard.bots.length}
                      </span>
                    </>
                  }
                  sub={
                    <>
                      <span className={activeBots ? "green-text" : ""}>
                        {activeBots
                          ? "Council is connected"
                          : "Ready when you are"}
                      </span>
                    </>
                  }
                  icon={<Users size={17} />}
                />
                <Metric
                  label="Tokens consumed"
                  value={num(stats.total.input_tokens, true)}
                  sub={
                    <>
                      {num(stats.total.output_tokens, true)} output ·{" "}
                      {num(stats.total.reasoning_tokens, true)} reasoning
                    </>
                  }
                  icon={<Cpu size={17} />}
                />
                <Metric
                  label="Time to first token"
                  value={duration(stats.total.avg_ttft_ms)}
                  sub={
                    <>
                      p95 {duration(stats.total.p95_ttft_ms)}{" "}
                      <span className="muted">· streamed requests</span>
                    </>
                  }
                  icon={<Zap size={17} />}
                />
                <Metric
                  label="Total cost"
                  value={money(stats.total.cost)}
                  sub={
                    <>
                      {stats.total.cost_known_requests || 0} of{" "}
                      {stats.total.requests || 0} requests have cost data
                    </>
                  }
                  icon={<span className="dollar-icon">$</span>}
                />
              </div>
              {dashboard.bots.some((b) => b.readiness.length) && (
                <Setup dashboard={dashboard} edit={edit} />
              )}
              <div className="overview-grid">
                <div className="overview-main">
                  <section className="panel usage-panel">
                    <div className="panel-title">
                      <div>
                        <h2>Council activity</h2>
                        <p>Token usage across all model requests</p>
                      </div>
                      <span className="select-like">
                        <Clock3 size={13} />
                        Last 24 hours
                      </span>
                    </div>
                    <UsageChart history={stats.history} />
                    <div className="chart-footer">
                      <span>
                        <i className="legend-dot lime" />
                        Input tokens
                      </span>
                      <span>
                        <i className="legend-dot lavender" />
                        Output tokens
                      </span>
                      <button
                        className="text-button"
                        onClick={() => navigate("analytics")}
                      >
                        View analytics <ArrowRight size={13} />
                      </button>
                    </div>
                  </section>
                  <section className="bot-section">
                    <div className="section-title">
                      <h2>
                        Your council <span>{dashboard.bots.length}</span>
                      </h2>
                      <button
                        className="text-button"
                        onClick={() => navigate("bots")}
                      >
                        Manage bots <ArrowRight size={14} />
                      </button>
                    </div>
                    <div className="bot-grid">
                      {dashboard.bots.map((bot) => (
                        <BotCard
                          key={bot.id}
                          bot={bot}
                          dashboard={dashboard}
                          edit={() => edit("bots", bot)}
                          context={() => setContextBot(bot)}
                          toggle={() =>
                            act(bot.enabled ? "stop" : "start", "bots", bot.id)
                          }
                          busy={busy}
                        />
                      ))}
                    </div>
                  </section>
                </div>
                <div className="overview-side">
                  <section className="panel">
                    <div className="panel-title compact">
                      <h2>Connections</h2>
                      <Cable size={16} />
                    </div>
                    {dashboard.providers.map((p) => (
                      <button
                        className="provider-mini"
                        key={p.id}
                        onClick={() => edit("providers", p)}
                      >
                        <span className="provider-icon">
                          <Cable size={18} />
                        </span>
                        <span>
                          <strong>{p.name}</strong>
                          <small>
                            {!p.enabled
                              ? "Paused"
                              : !p.key_configured && p.requires_key
                                ? "API key needed"
                                : p.health.consecutive_failures
                                  ? `${p.health.consecutive_failures} consecutive failures`
                                  : p.recent?.failures
                                    ? `${p.recent.failures} / ${p.recent.requests} requests failed · 15m`
                                    : p.health.last_success
                                      ? "Requests succeeding"
                                      : "Awaiting first request"}
                          </small>
                        </span>
                        <Badge
                          tone={
                            p.health.consecutive_failures
                              ? "red"
                              : p.health.last_success && p.enabled
                                ? "green"
                                : "neutral"
                          }
                        >
                          {p.health.consecutive_failures
                            ? "Issue"
                            : p.health.last_success && p.enabled
                              ? "Healthy"
                              : "Setup"}
                        </Badge>
                      </button>
                    ))}
                    <button
                      className="panel-link"
                      onClick={() => navigate("providers")}
                    >
                      Manage providers <ArrowRight size={14} />
                    </button>
                  </section>
                  <section className="panel activity-panel">
                    <div className="panel-title compact">
                      <h2>Live activity</h2>
                      <Badge tone={connected ? "green" : "amber"}>
                        {connected ? "Live" : "Offline"}
                      </Badge>
                    </div>
                    <div className="activity-list">
                      {events.slice(0, 6).map((e) => (
                        <button
                          className="activity-item"
                          key={e.id}
                          onClick={() => navigate("trajectory")}
                        >
                          <span className={`event-dot ${e.level}`} />
                          <div>
                            <strong>{e.kind.replaceAll(".", " · ")}</strong>
                            <p>{eventText(e)}</p>
                            <time>{timeLabel(e.at)}</time>
                          </div>
                        </button>
                      ))}
                      {!events.length && (
                        <Empty title="Nothing to report">
                          Events will appear as the council runs.
                        </Empty>
                      )}
                    </div>
                    <button
                      className="panel-link"
                      onClick={() => navigate("trajectory")}
                    >
                      Open trajectory <ArrowRight size={14} />
                    </button>
                  </section>
                  <div className="hortator-note">
                    <span className="hortator-note-icon">
                      <Command size={20} />
                    </span>
                    <h3>Your conductor, on Discord.</h3>
                    <p>
                      Check the pulse, change a prompt, or pause the room.
                      Hortator answers only to you.
                    </p>
                    <button
                      className="text-button"
                      onClick={() => navigate("commands")}
                    >
                      Explore commands <ArrowRight size={14} />
                    </button>
                    <code>!status</code>
                    <code>!stop all</code>
                  </div>
                </div>
              </div>
            </>
          )}
          {page === "bots" && (
            <div className="bot-grid all-bots">
              {dashboard.bots.map((bot) => (
                <BotCard
                  key={bot.id}
                  bot={bot}
                  dashboard={dashboard}
                  edit={() => edit("bots", bot)}
                  context={() => setContextBot(bot)}
                  toggle={() =>
                    act(bot.enabled ? "stop" : "start", "bots", bot.id)
                  }
                  busy={busy}
                />
              ))}
              <button className="add-bot-card" onClick={() => edit("bots")}>
                <Plus size={25} />
                <strong>A new perspective</strong>
                <span>Add another Discord application</span>
              </button>
            </div>
          )}
          {(
            ["providers", "profiles", "prompts", "plugins", "rooms"] as Page[]
          ).includes(page) && (
            <Catalog
              kind={page as Kind}
              dashboard={dashboard}
              edit={edit}
              act={act}
              busy={busy}
              notify={notify}
            />
          )}
          {page === "settings" && (
            <div className="settings-overview">
              <section className="panel settings-card">
                <div className="section-icon">
                  <ShieldCheck size={24} />
                </div>
                <h2>One owner. One source of authority.</h2>
                <p>
                  Administrative access is bound to your Discord snowflake.
                  Nicknames and server roles never grant council control.
                </p>
                <div className="locked-identity">
                  <span>The Boss · .normal.man.</span>
                  <code>{dashboard.owner_id}</code>
                  <LockKeyhole size={17} />
                </div>
                <dl className="detail-list">
                  <div>
                    <dt>Reporting channel</dt>
                    <dd>
                      {dashboard.settings.control_channel_id ||
                        "Not configured"}
                    </dd>
                  </div>
                  <div>
                    <dt>Timezone</dt>
                    <dd>{dashboard.settings.timezone}</dd>
                  </div>
                  <div>
                    <dt>Concurrent turns</dt>
                    <dd>{dashboard.settings.max_concurrent_turns}</dd>
                  </div>
                  <div>
                    <dt>Incident notifications</dt>
                    <dd>
                      {dashboard.settings.incident_notifications
                        ? "Enabled"
                        : "Disabled"}
                    </dd>
                  </div>
                </dl>
                <button
                  className="button primary"
                  onClick={() => edit("settings", dashboard.settings)}
                >
                  <Settings2 size={15} />
                  Edit council settings
                </button>
              </section>
              <section className="panel settings-card">
                <div className="section-icon">
                  <FileText size={24} />
                </div>
                <h2>The shared system prompt</h2>
                <p>
                  Injected into every bot alongside its identity and individual
                  personality.
                </p>
                <blockquote className="prompt-preview">
                  {dashboard.settings.global_prompt}
                </blockquote>
                <button
                  className="button"
                  onClick={() => edit("settings", dashboard.settings)}
                >
                  Edit shared prompt <ArrowRight size={14} />
                </button>
                <div className="settings-export">
                  <h3>Configuration export</h3>
                  <p>
                    Download configuration without credentials. Database backups
                    are available through the local CLI.
                  </p>
                  <a className="button" href="/api/export/config" download>
                    <ArrowDownToLine size={15} />
                    Export configuration
                  </a>
                </div>
              </section>
            </div>
          )}
          {page === "trajectory" && (
            <Trajectory dashboard={dashboard} notify={notify} />
          )}
          {page === "analytics" && <Analytics dashboard={dashboard} />}
          {page === "commands" && <Commands />}
          <footer className="page-footer">
            <span>
              <Logo small />A council of independent minds.
            </span>
          </footer>
        </main>
      </div>
      {editor && (
        <Editor
          key={`${editor.kind}:${editor.entity?.id || "new"}`}
          kind={editor.kind}
          entity={editor.entity}
          dashboard={dashboard}
          schemas={schemas}
          close={() => setEditor(null)}
          saved={async (value, keepOpen) => {
            await refresh();
            if (keepOpen) setEditor({ kind: editor.kind, entity: value });
            else setEditor(null);
          }}
          notify={notify}
        />
      )}
      {contextBot && (
        <ContextPanel
          bot={dashboard.bots.find((b) => b.id === contextBot.id) || contextBot}
          close={() => setContextBot(null)}
          notify={notify}
        />
      )}
      {toast && (
        <div
          className={`toast ${toast.error ? "error" : ""}`}
          role={toast.error ? "alert" : "status"}
        >
          {toast.error ? <CircleHelp size={18} /> : <CheckCircle2 size={18} />}
          <span>{toast.text}</span>
          <button
            className="icon-button"
            aria-label="Dismiss notification"
            onClick={() => setToast(null)}
          >
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  );
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <div className="login-page">
      <a className="brand login-brand" href="#">
        <Logo small />
        <span>hortator.</span>
      </a>
      <div className="login-orbit">
        <i />
        <i />
        <i />
        <span className="orbit-node one">A</span>
        <span className="orbit-node two">S</span>
        <span className="orbit-node three">H</span>
      </div>
      <div className="login-card">
        <Logo />
        <div className="eyebrow">WELCOME TO THE COUNCIL</div>
        <h1>
          Many minds.
          <br />
          One conductor.
        </h1>
        <p>Sign in to bring your council together.</p>
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            try {
              const data = await api("/api/auth/login", {
                method: "POST",
                body: JSON.stringify({ password }),
              });
              setCsrf(data.csrf);
              setPassword("");
              onLogin();
            } catch (err) {
              setError(
                err instanceof Error ? err.message : "Could not sign in",
              );
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
              placeholder="Enter your password"
            />
          </Field>
          {error && (
            <p className="field-error" role="alert">
              {error}
            </p>
          )}
          <button className="button primary" disabled={busy}>
            {busy ? "Signing in…" : "Enter council control"}
            <ArrowRight size={16} />
          </button>
        </form>
        <div className="login-help">
          <LockKeyhole size={15} />
          <span>
            On first run, your password is saved in{" "}
            <code>data/initial-password</code> on the server.
          </span>
        </div>
      </div>
      <div className="login-footer">
        PRIVATE WORKSPACE <span>·</span> OWNER ACCESS ONLY
      </div>
    </div>
  );
}

function Setup({
  dashboard,
  edit,
}: {
  dashboard: Dashboard;
  edit: (kind: Kind, entity?: RecordData) => void;
}) {
  const steps: {
    title: string;
    done: boolean;
    kind: Kind;
    entity?: RecordData;
  }[] = [
    {
      title: "Connect a provider",
      done: dashboard.providers.some(
        (p) => p.key_configured || !p.requires_key,
      ),
      kind: "providers",
      entity: dashboard.providers[0],
    },
    {
      title: "Choose your model",
      done: dashboard.profiles.some((p) => p.model !== "your-model-id"),
      kind: "profiles",
      entity: dashboard.profiles[0],
    },
    {
      title: "Set the stage",
      done:
        dashboard.rooms.some((r) => r.channel_id && r.guild_id) &&
        !!dashboard.settings.control_channel_id,
      kind: dashboard.rooms.some((r) => r.channel_id && r.guild_id)
        ? "settings"
        : "rooms",
      entity: dashboard.rooms.some((r) => r.channel_id && r.guild_id)
        ? dashboard.settings
        : dashboard.rooms[0],
    },
    {
      title: "Invite your bots",
      done: dashboard.bots.some((b) => !b.readiness.length),
      kind: "bots",
      entity: dashboard.bots.find((b) => b.readiness.length),
    },
  ];
  return (
    <section className="setup-banner">
      <div className="setup-heading">
        <span className="setup-icon">
          <Sparkles size={19} />
        </span>
        <div>
          <h2>A few connections. A whole conversation.</h2>
          <p>Set up your first council. Your draft personalities are ready.</p>
        </div>
        <span className="setup-count">
          {steps.filter((s) => s.done).length} / 4 complete
        </span>
      </div>
      <div className="setup-steps">
        {steps.map((step, i) => (
          <button
            key={step.title}
            className={step.done ? "done" : ""}
            onClick={() => edit(step.kind, step.entity)}
          >
            <span className="step-number">
              {step.done ? <Check size={13} /> : `0${i + 1}`}
            </span>
            {step.title}
            <ArrowRight size={14} />
          </button>
        ))}
      </div>
    </section>
  );
}

function BotCard({
  bot,
  dashboard,
  edit,
  context,
  toggle,
  busy,
}: {
  bot: RecordData;
  dashboard: Dashboard;
  edit: () => void;
  context: () => void;
  toggle: () => void;
  busy: boolean;
}) {
  const [status, tone] = botStatus(bot, dashboard);
  const profile = dashboard.profiles.find((p) => p.id === bot.model_profile_id);
  const provider = dashboard.providers.find(
    (p) => p.id === profile?.provider_id,
  );
  const used = Math.max(
    0,
    ...bot.contexts.map((c: RecordData) => c.estimated_tokens),
  );
  const percent = profile
    ? Math.min(100, (used / profile.context_window) * 100)
    : 0;
  return (
    <article className={`bot-card ${bot.active_turn ? "thinking" : ""}`}>
      <div className="bot-card-header">
        <Avatar bot={bot} />
        <div>
          <h3>{bot.name}</h3>
          <span>
            {bot.role === "hortator" ? "Council director" : "Council member"}
          </span>
        </div>
        <button
          className="icon-button"
          aria-label={`Edit ${bot.name}`}
          onClick={edit}
        >
          <MoreHorizontal size={19} />
        </button>
      </div>
      <div className="bot-model">
        <Cpu size={13} />
        <span>
          {profile?.model === "your-model-id"
            ? "Choose a model"
            : profile?.model || "No model"}
        </span>
        <Badge tone={tone}>{status}</Badge>
      </div>
      <p className="bot-persona">{bot.persona}</p>
      <div className="bot-facts">
        <span>
          <Cable size={13} />
          {provider?.name || "No provider"}
        </span>
        <span>
          <Clock3 size={13} />
          {bot.interval_seconds}s activation
        </span>
        <span>
          <Zap size={13} />
          {bot.enabled_plugins.length} tools
        </span>
      </div>
      <button className="context-meter" onClick={context}>
        <span>
          <span>Context capacity</span>
          <strong>
            {bot.contexts.length ? `${percent.toFixed(1)}%` : "No context yet"}
          </strong>
        </span>
        <span className="meter-track">
          <i
            style={{
              width: `${percent}%`,
              background: percent > 70 ? "#e1b778" : bot.color,
            }}
          />
          <em
            style={{ left: `${(profile?.compact_threshold || 0.7) * 100}%` }}
          />
        </span>
        <span className="meter-caption">
          {num(used, true)} / {num(profile?.context_window, true)} estimated
          tokens
          <ChevronRight size={12} />
        </span>
      </button>
      <div className="bot-card-footer">
        <span className="mono" title={bot.id}>
          {bot.enabled && !bot.readiness.length && dashboard.settings.enabled
            ? bot.active_turn
              ? "Turn in progress"
              : `Next in ${Math.max(0, Math.ceil(bot.runtime.next_at - dashboard.now))}s`
            : bot.id}
        </span>
        {bot.readiness.length ? (
          <button className="text-button" onClick={edit}>
            Finish setup <ArrowRight size={13} />
          </button>
        ) : (
          <button className="text-button" onClick={toggle} disabled={busy}>
            {bot.enabled ? <Pause size={13} /> : <Play size={13} />}{" "}
            {bot.enabled ? "Pause bot" : "Activate bot"}
          </button>
        )}
      </div>
    </article>
  );
}

function Catalog({
  kind,
  dashboard,
  edit,
  act,
  busy,
  notify,
}: {
  kind: Kind;
  dashboard: Dashboard;
  edit: (k: Kind, e?: RecordData) => void;
  act: (a: string, k?: string, id?: string, d?: RecordData) => Promise<any>;
  busy: boolean;
  notify: (s: string, e?: boolean) => void;
}) {
  const [search, setSearch] = useState("");
  const [probe, setProbe] = useState<RecordData | null>(null);
  const [threadName, setThreadName] = useState<Record<string, string>>({});
  const items: RecordData[] = kind === "settings" ? [] : dashboard[kind];
  const filtered = items.filter((e) =>
    `${e.name} ${e.id} ${e.model || ""}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <>
      <div className="catalog-toolbar">
        <span>
          {items.length} {kind === "profiles" ? "model profiles" : kind}
        </span>
        <input
          aria-label={`Search ${kind}`}
          className="search-input"
          placeholder={`Find ${kind === "profiles" ? "a model profile" : kind}…`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>
      <div
        className={`catalog-grid ${kind === "profiles" ? "profiles-grid" : ""}`}
      >
        {filtered.map((item) => (
          <article className="panel catalog-card" key={item.id}>
            <div className="catalog-card-head">
              <span className="catalog-icon">
                {kind === "providers" ? (
                  <Cable size={21} />
                ) : kind === "profiles" ? (
                  <Layers3 size={21} />
                ) : kind === "prompts" ? (
                  <FileText size={21} />
                ) : kind === "plugins" ? (
                  <Zap size={21} />
                ) : (
                  <Hash size={21} />
                )}
              </span>
              <div>
                <h2>{item.name}</h2>
                <span className="mono muted">{item.id}</span>
              </div>
              <button
                className="icon-button"
                aria-label={`Edit ${item.name}`}
                onClick={() => edit(kind, item)}
              >
                <Settings2 size={16} />
              </button>
            </div>
            {kind === "providers" && (
              <>
                <div className="catalog-status">
                  <Badge
                    tone={
                      !item.enabled
                        ? "amber"
                        : item.health.consecutive_failures
                          ? "red"
                          : item.health.last_success
                            ? "green"
                            : "neutral"
                    }
                  >
                    {!item.enabled
                      ? "Paused"
                      : item.health.consecutive_failures
                        ? "Degraded"
                        : item.health.last_success
                          ? "Healthy"
                          : "Unverified"}
                  </Badge>
                  <span>
                    {item.kind === "openrouter"
                      ? "OpenRouter"
                      : "OpenAI compatible"}
                  </span>
                </div>
                <code className="endpoint">{item.base_url}</code>
                <dl className="detail-list">
                  <div>
                    <dt>Credential</dt>
                    <dd>
                      {item.key_configured
                        ? "Encrypted · configured"
                        : item.requires_key
                          ? "Not configured"
                          : "Key not required"}
                    </dd>
                  </div>
                  <div>
                    <dt>Model profiles</dt>
                    <dd>
                      {
                        dashboard.profiles.filter(
                          (p) => p.provider_id === item.id,
                        ).length
                      }
                    </dd>
                  </div>
                  <div>
                    <dt>Failures / last 15 minutes</dt>
                    <dd>
                      {item.recent?.failures || 0} /{" "}
                      {item.recent?.requests || 0}
                    </dd>
                  </div>
                  <div>
                    <dt>Consecutive failures</dt>
                    <dd
                      className={
                        item.health.consecutive_failures ? "red-text" : ""
                      }
                    >
                      {item.health.consecutive_failures}
                    </dd>
                  </div>
                  <div>
                    <dt>Concurrency / timeout</dt>
                    <dd>
                      {item.max_concurrency} / {item.timeout_seconds}s
                    </dd>
                  </div>
                </dl>
                {item.health.last_error && (
                  <p className="inline-error">{item.health.last_error}</p>
                )}
                <div className="catalog-actions">
                  <button
                    className="button"
                    disabled={busy}
                    onClick={async () => {
                      const result = await act("probe", "providers", item.id);
                      if (result) setProbe(result);
                    }}
                  >
                    <Activity size={14} />
                    Discover models
                  </button>
                  <button
                    className="icon-button"
                    aria-label={`${item.enabled ? "Pause" : "Enable"} ${item.name}`}
                    disabled={busy}
                    onClick={() =>
                      act(item.enabled ? "stop" : "start", kind, item.id)
                    }
                  >
                    {item.enabled ? <Pause size={15} /> : <Play size={15} />}
                  </button>
                  {item.health.circuit_until > dashboard.now && (
                    <button
                      className="text-button"
                      onClick={() => act("reset_circuit", "providers", item.id)}
                    >
                      Reset circuit
                    </button>
                  )}
                </div>
              </>
            )}
            {kind === "profiles" && (
              <>
                <div className="profile-model">
                  <Cpu size={15} />
                  {item.model}
                </div>
                <p className="muted small-text">
                  {
                    dashboard.providers.find((p) => p.id === item.provider_id)
                      ?.name
                  }{" "}
                  ·{" "}
                  {
                    dashboard.bots.filter((b) => b.model_profile_id === item.id)
                      .length
                  }{" "}
                  bots assigned
                </p>
                <dl className="detail-list">
                  <div>
                    <dt>Context window</dt>
                    <dd>{num(item.context_window)} tokens</dd>
                  </div>
                  <div>
                    <dt>Auto-compact at</dt>
                    <dd>{Math.round(item.compact_threshold * 100)}%</dd>
                  </div>
                  <div>
                    <dt>Response reserve</dt>
                    <dd>{num(item.response_tokens)}</dd>
                  </div>
                  <div>
                    <dt>Output limit sent</dt>
                    <dd className="mono">
                      {["max_tokens", "max_completion_tokens"]
                        .filter((key) => Object.hasOwn(item.request_json, key))
                        .map(
                          (key) =>
                            `${key}: ${JSON.stringify(item.request_json[key])}`,
                        )
                        .join(" · ") || "Not specified"}
                    </dd>
                  </div>
                  <div>
                    <dt>Reasoning configuration</dt>
                    <dd className="mono">
                      {reasoningSummary(item.request_json)}
                    </dd>
                  </div>
                </dl>
                <Switch
                  label="SSE streaming"
                  checked={item.stream !== false}
                  disabled={busy}
                  onChange={(stream) =>
                    act("save", "profiles", item.id, {
                      stream,
                      revision: item.revision,
                    })
                  }
                />
                <p className="muted small-text">
                  {item.stream !== false
                    ? "Streamed response · TTFT available; TPS requires reported usage."
                    : "Complete JSON response · TTFT and streaming TPS unavailable."}
                </p>
                <Code value={item.request_json} label="Request parameters" />
                <div className="catalog-actions">
                  <button className="button" onClick={() => edit(kind, item)}>
                    Edit profile <ArrowRight size={14} />
                  </button>
                  <button
                    className="text-button"
                    onClick={async () => {
                      const cloned = await act("clone", "profiles", item.id);
                      if (cloned) edit("profiles", cloned);
                    }}
                  >
                    <Layers3 size={13} />
                    Clone
                  </button>
                </div>
              </>
            )}
            {kind === "prompts" && (
              <>
                <blockquote className="prompt-preview">
                  {item.content || "No prompt content yet."}
                </blockquote>
                <div className="catalog-actions">
                  <span className="muted small-text">
                    {
                      dashboard.bots.filter((b) =>
                        b.prompt_ids.includes(item.id),
                      ).length
                    }{" "}
                    bots · revision {item.revision}
                  </span>
                  <button
                    className="text-button"
                    onClick={() => edit(kind, item)}
                  >
                    Edit prompt <ArrowRight size={14} />
                  </button>
                </div>
              </>
            )}
            {kind === "plugins" && (
              <>
                <div className="catalog-status">
                  <Badge tone={item.enabled ? "green" : "neutral"}>
                    {item.enabled ? "Enabled globally" : "Disabled"}
                  </Badge>
                  <span>
                    {
                      dashboard.bots.filter((b) =>
                        b.enabled_plugins.includes(item.id),
                      ).length
                    }{" "}
                    bot grants
                  </span>
                </div>
                <p className="catalog-description">{item.description}</p>
                <div className="plugin-config-line">
                  <KeyRound size={14} />
                  {item.key_configured
                    ? "Credential configured"
                    : item.keyless
                      ? "No credential required"
                      : "Add a credential or local endpoint"}
                </div>
                <div className="catalog-actions">
                  <button className="button" onClick={() => edit(kind, item)}>
                    Configure <Settings2 size={14} />
                  </button>
                  <button
                    className={`text-button ${item.enabled ? "" : "green-text"}`}
                    disabled={busy}
                    onClick={() =>
                      act(item.enabled ? "stop" : "start", kind, item.id)
                    }
                  >
                    {item.enabled ? "Disable" : "Enable globally"}
                  </button>
                </div>
              </>
            )}
            {kind === "rooms" && (
              <>
                <div className="catalog-status">
                  <Badge
                    tone={
                      item.channel_id && item.guild_id ? "green" : "neutral"
                    }
                  >
                    {item.channel_id && item.guild_id
                      ? "Configured"
                      : "Setup needed"}
                  </Badge>
                  <span>
                    {
                      dashboard.bots.filter((b) => b.room_ids.includes(item.id))
                        .length
                    }{" "}
                    members
                  </span>
                </div>
                <dl className="detail-list">
                  <div>
                    <dt>Guild</dt>
                    <dd className="mono">{item.guild_id || "Not set"}</dd>
                  </div>
                  <div>
                    <dt>Channel</dt>
                    <dd className="mono">{item.channel_id || "Not set"}</dd>
                  </div>
                  <div>
                    <dt>Thread contexts</dt>
                    <dd>
                      {item.include_threads
                        ? "Isolated per thread"
                        : "Channel only"}
                    </dd>
                  </div>
                  <div>
                    <dt>Gap between sends</dt>
                    <dd>{item.send_gap_seconds}s</dd>
                  </div>
                </dl>
                <div className="room-members">
                  {dashboard.bots
                    .filter((b) => b.room_ids.includes(item.id))
                    .map((b) => (
                      <Avatar key={b.id} bot={b} size="tiny" />
                    ))}
                </div>
                <div className="thread-form">
                  <input
                    aria-label={`New thread in ${item.name}`}
                    placeholder="New discussion thread…"
                    value={threadName[item.id] || ""}
                    onChange={(e) =>
                      setThreadName({
                        ...threadName,
                        [item.id]: e.target.value,
                      })
                    }
                  />
                  <button
                    className="icon-button"
                    aria-label={`Create thread in ${item.name}`}
                    disabled={!threadName[item.id] || busy}
                    onClick={async () => {
                      const result = await act("thread", "rooms", item.id, {
                        name: threadName[item.id],
                      });
                      if (result) {
                        setThreadName({ ...threadName, [item.id]: "" });
                        notify(`Thread created: ${result.thread_id}`);
                      }
                    }}
                  >
                    <Plus size={18} />
                  </button>
                </div>
              </>
            )}
          </article>
        ))}
      </div>
      {!filtered.length && (
        <Empty title="No matching records" icon={<Layers3 size={24} />}>
          Create a record or try a different search.
        </Empty>
      )}
      {probe && (
        <div className="probe-result">
          <div className="section-title">
            <h2>Model discovery · {duration(probe.latency_ms)}</h2>
            <button
              className="icon-button"
              aria-label="Dismiss model discovery"
              onClick={() => setProbe(null)}
            >
              <X size={17} />
            </button>
          </div>
          <Notice>{probe.note}</Notice>
          <Code
            value={probe.models}
            label={`${probe.models.length} discovered models`}
          />
        </div>
      )}
    </>
  );
}

function Commands() {
  const [commands, setCommands] = useState<RecordData[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    api("/api/commands")
      .then(setCommands)
      .catch((e) => setError(e.message));
  }, []);
  return (
    <>
      <div className="commands-intro">
        <div className="commands-icon">
          <Terminal size={26} />
        </div>
        <div>
          <h2>At your command.</h2>
          <p>
            Commands are executed directly. Ask Hortator a normal question to
            use its model and read-only inspection tools.
          </p>
        </div>
        <Badge tone="green">
          <ShieldCheck size={12} />
          Snowflake verified
        </Badge>
      </div>
      {error && <Notice warning>{error}</Notice>}
      <div className="panel commands-table">
        {commands.map((c) => (
          <div key={c.command}>
            <code>{c.command}</code>
            <span>{c.description}</span>
          </div>
        ))}
      </div>
      <div className="two-column">
        <Notice>
          Use{" "}
          <code>
            !set profiles balanced{" "}
            {
              '{"request_json":{"temperature":0.6,"reasoning":{"effort":"low"}}}'
            }
          </code>{" "}
          to replace the exact parameter JSON. You can attach a UTF-8 text or
          JSON file for longer edits.
        </Notice>
        <Notice>
          Only user <code>1482143139828596916</code> can use these commands.
          Credentials are entered in the dashboard. Use <code>!dm !status</code>{" "}
          for a private report.
        </Notice>
      </div>
    </>
  );
}
