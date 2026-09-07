import { useEffect, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  ArrowRight,
  ChevronDown,
  Clock3,
  FileJson2,
  Layers3,
  List,
  MessageSquare,
  Search,
  Terminal,
  X,
  Zap,
} from "lucide-react";
import {
  api,
  dateLabel,
  duration,
  eventText,
  money,
  num,
  timeLabel,
} from "./api";
import type { Dashboard, RecordData } from "./api";
import { Avatar, Badge, Code, Empty, Modal, Notice } from "./components";

const tone = (status: string) =>
  ["failed", "unknown", "interrupted"].includes(status)
    ? "red"
    : ["sent", "completed"].includes(status)
      ? "green"
      : ["cancelled", "suppressed"].includes(status)
        ? "amber"
        : status === "running"
          ? "violet"
          : "neutral";
export function Trajectory({
  dashboard,
  notify,
}: {
  dashboard: Dashboard;
  notify: (text: string, error?: boolean) => void;
}) {
  const [view, setView] = useState("turns");
  const [bot, setBot] = useState("");
  const [status, setStatus] = useState("");
  const [level, setLevel] = useState("");
  const [search, setSearch] = useState("");
  const [turns, setTurns] = useState<RecordData[]>([]);
  const [events, setEvents] = useState<RecordData[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RecordData | null>(null);
  const [eventDetail, setEventDetail] = useState<RecordData | null>(null);
  const [inspector, setInspector] = useState("overview");
  const [loading, setLoading] = useState(true);
  const [more, setMore] = useState(true);
  const [error, setError] = useState("");
  const base =
    view === "turns"
      ? `/api/trajectory?limit=60&bot_id=${encodeURIComponent(bot)}&status=${status}`
      : `/api/events?limit=100&bot_id=${encodeURIComponent(bot)}&level=${level}`;
  useEffect(() => {
    let active = true;
    setLoading(true);
    setTurns([]);
    setEvents([]);
    setMore(true);
    const refresh = () =>
      api(base)
        .then((rows) => {
          if (!active) return;
          if (view === "turns")
            setTurns((prev) =>
              Array.from(
                new Map(
                  [...prev, ...rows].map((r: RecordData) => [r.id, r]),
                ).values(),
              ).sort((a, b) => b.started_at - a.started_at),
            );
          else
            setEvents((prev) =>
              Array.from(
                new Map(
                  [...prev, ...rows].map((r: RecordData) => [r.id, r]),
                ).values(),
              ).sort((a, b) => b.seq - a.seq),
            );
          setLoading(false);
          setError("");
          if (rows.length < (view === "turns" ? 60 : 100)) setMore(false);
        })
        .catch((e) => {
          if (active) {
            setError(e.message);
            setLoading(false);
          }
        });
    refresh();
    const timer = setInterval(refresh, 4000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [base, view]);
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    let active = true;
    setDetail(null);
    const refresh = () =>
      api(`/api/trajectory/${selected}`)
        .then((data) => {
          if (active) setDetail(data);
        })
        .catch((e) => {
          if (active) notify(e.message, true);
        });
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [selected, notify]);
  const loadMore = async () => {
    const before =
      view === "turns" ? turns.at(-1)?.started_at : events.at(-1)?.seq;
    if (!before) return;
    try {
      const rows = await api(`${base}&before=${before}`);
      if (view === "turns") setTurns((prev) => [...prev, ...rows]);
      else setEvents((prev) => [...prev, ...rows]);
      setMore(rows.length >= (view === "turns" ? 60 : 100));
    } catch (e) {
      notify(
        e instanceof Error ? e.message : "Could not load older records",
        true,
      );
    }
  };
  const filtered = (view === "turns" ? turns : events).filter((row) =>
    JSON.stringify(row).toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <>
      <div className="trajectory-toolbar">
        <div className="tabs">
          <button
            className={view === "turns" ? "selected" : ""}
            onClick={() => setView("turns")}
          >
            <Layers3 size={15} />
            Turns
          </button>
          <button
            className={view === "events" ? "selected" : ""}
            onClick={() => setView("events")}
          >
            <List size={15} />
            Event ledger
          </button>
        </div>
        <div className="trajectory-filters">
          <select
            aria-label="Filter by bot"
            value={bot}
            onChange={(e) => setBot(e.target.value)}
          >
            <option value="">All bots</option>
            {dashboard.bots.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
          {view === "turns" ? (
            <select
              aria-label="Filter by turn outcome"
              value={status}
              onChange={(e) => setStatus(e.target.value)}
            >
              <option value="">All outcomes</option>
              {[
                "running",
                "sent",
                "silent",
                "completed",
                "failed",
                "cancelled",
                "interrupted",
              ].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          ) : (
            <select
              aria-label="Filter by event severity"
              value={level}
              onChange={(e) => setLevel(e.target.value)}
            >
              <option value="">All severities</option>
              {["info", "warning", "error"].map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          )}
          <div className="search-wrap">
            <Search size={14} />
            <input
              aria-label="Search loaded trajectory"
              placeholder="Search loaded records…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
        </div>
      </div>
      {error && <Notice warning>{error}</Notice>}
      <div className={`trajectory-layout ${selected ? "with-inspector" : ""}`}>
        <section className="panel ledger-panel">
          <div className="ledger-heading">
            <span>{view === "turns" ? "ACTIVATIONS" : "DURABLE EVENTS"}</span>
            <span>{filtered.length} loaded</span>
          </div>
          {loading ? (
            <Empty title="Loading trajectory…" icon={<Activity size={23} />} />
          ) : !filtered.length ? (
            <Empty
              title={
                search || bot || status || level
                  ? "No matching records"
                  : view === "turns"
                    ? "Every conversation has a beginning."
                    : "The ledger is clear."
              }
              icon={<Activity size={27} />}
            >
              {view === "turns"
                ? "Once a bot activates, its context, model requests, tool calls, and delivery will appear here."
                : "Configuration changes, messages, failures, and delivery events appear in observation order."}
            </Empty>
          ) : (
            filtered.map((row) =>
              view === "turns" ? (
                <button
                  className={`turn-row ${row.id === selected ? "selected" : ""}`}
                  key={row.id}
                  onClick={() => {
                    setSelected(row.id);
                    setInspector("overview");
                  }}
                >
                  <Avatar
                    size="tiny"
                    bot={
                      dashboard.bots.find((b) => b.id === row.bot_id) || {
                        name: row.bot_id,
                      }
                    }
                  />
                  <div className="turn-row-content">
                    <strong>
                      {dashboard.bots.find((b) => b.id === row.bot_id)?.name ||
                        row.bot_id}
                      <span className="mono">{row.id.slice(-8)}</span>
                    </strong>
                    <p>
                      {row.model} <span>·</span>{" "}
                      {row.trigger.replaceAll("_", " ")}
                    </p>
                    <small>{dateLabel(row.started_at)}</small>
                  </div>
                  <div className="turn-row-state">
                    <Badge tone={tone(row.status)}>{row.status}</Badge>
                    <span>
                      {row.ended_at
                        ? duration((row.ended_at - row.started_at) * 1000)
                        : "In progress"}
                    </span>
                  </div>
                  <ArrowRight size={14} />
                </button>
              ) : (
                <button
                  className="ledger-event"
                  key={row.id}
                  onClick={() => setEventDetail(row)}
                >
                  <span className={`event-dot ${row.level}`} />
                  <div>
                    <strong>{row.kind}</strong>
                    <p>{eventText(row)}</p>
                    <small>
                      {timeLabel(row.at)} <span>·</span> #{row.seq}{" "}
                      <span>·</span> {row.bot_id || "System"}
                    </small>
                  </div>
                  <ArrowRight size={13} />
                </button>
              ),
            )
          )}
          {more && !loading && filtered.length > 0 && (
            <button className="load-more" onClick={loadMore}>
              <ChevronDown size={15} />
              Load older {view === "turns" ? "turns" : "events"}
            </button>
          )}
        </section>
        {selected && (
          <aside className="panel turn-inspector">
            <header className="inspector-header">
              <div>
                <span className="eyebrow">TURN INSPECTOR</span>
                <h2>
                  {detail
                    ? dashboard.bots.find((b) => b.id === detail.turn.bot_id)
                        ?.name || detail.turn.bot_id
                    : "Loading…"}
                </h2>
                <span className="mono">{selected}</span>
              </div>
              <button
                className="icon-button"
                aria-label="Close turn inspector"
                onClick={() => setSelected(null)}
              >
                <X size={17} />
              </button>
            </header>
            {detail && (
              <>
                <div className="inspector-status">
                  <Badge tone={tone(detail.turn.status)}>
                    {detail.turn.status}
                  </Badge>
                  <span>{detail.turn.model}</span>
                  <a
                    className="icon-button"
                    aria-label="Export full trajectory"
                    href={`/api/trajectory/${selected}/export`}
                    download
                  >
                    <ArrowDownToLine size={16} />
                  </a>
                </div>
                <div className="tabs inspector-tabs">
                  {[
                    "overview",
                    "requests",
                    "context",
                    "tools",
                    "delivery",
                    "json",
                  ].map((tab) => (
                    <button
                      key={tab}
                      onClick={() => setInspector(tab)}
                      className={tab === inspector ? "selected" : ""}
                    >
                      {tab}
                    </button>
                  ))}
                </div>
                <div className="inspector-body">
                  {detail.turn.error && (
                    <Notice warning>{detail.turn.error}</Notice>
                  )}
                  {inspector === "overview" && (
                    <>
                      <Waterfall detail={detail} />
                      <div className="request-totals">
                        <span>
                          <Clock3 size={15} />
                          Completion
                          <strong>
                            {detail.turn.ended_at
                              ? duration(
                                  (detail.turn.ended_at -
                                    detail.turn.started_at) *
                                    1000,
                                )
                              : "Running"}
                          </strong>
                        </span>
                        <span>
                          <Zap size={15} />
                          Model requests
                          <strong>{detail.requests.length}</strong>
                        </span>
                        <span>
                          <MessageSquare size={15} />
                          Decision
                          <strong>{detail.turn.decision || "Pending"}</strong>
                        </span>
                      </div>
                      <h3>Turn timeline</h3>
                      <div className="turn-timeline">
                        {detail.events.map((event: RecordData) => (
                          <button
                            key={event.id}
                            onClick={() => setEventDetail(event)}
                          >
                            <span className={`timeline-point ${event.level}`} />
                            <div>
                              <strong>{event.kind}</strong>
                              <p>{eventText(event)}</p>
                            </div>
                            <time>
                              +
                              {duration(
                                (event.at - detail.turn.started_at) * 1000,
                              )}
                            </time>
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                  {inspector === "requests" &&
                    detail.requests.map(
                      (request: RecordData, index: number) => (
                        <section className="request-detail" key={request.id}>
                          <div className="section-title">
                            <h3>
                              #{index + 1} · {request.purpose}
                            </h3>
                            <Badge tone={tone(request.status)}>
                              {request.status}
                            </Badge>
                          </div>
                          <span className="mono muted small-text">
                            {request.id}
                          </span>
                          <dl className="detail-list">
                            <div>
                              <dt>Profile / provider</dt>
                              <dd>
                                {request.profile_id} / {request.provider_id}
                              </dd>
                            </div>
                            <div>
                              <dt>Model / HTTP status</dt>
                              <dd>
                                {request.model} / {request.http_status || "—"}
                              </dd>
                            </div>
                            <div>
                              <dt>First token / visible token</dt>
                              <dd>
                                {duration(request.ttft_ms)} /{" "}
                                {duration(request.first_visible_ms)}
                              </dd>
                            </div>
                            <div>
                              <dt>Duration / queue</dt>
                              <dd>
                                {duration(request.duration_ms)} /{" "}
                                {duration(request.context.queue_ms)}
                              </dd>
                            </div>
                            <div>
                              <dt>Input / output</dt>
                              <dd>
                                {num(request.input_tokens)} /{" "}
                                {num(request.output_tokens)}
                              </dd>
                            </div>
                            <div>
                              <dt>Reasoning / cached</dt>
                              <dd>
                                {num(request.reasoning_tokens)} /{" "}
                                {num(request.cached_tokens)}
                              </dd>
                            </div>
                            <div>
                              <dt>Cost</dt>
                              <dd>
                                {money(request.cost)}{" "}
                                {request.cost_source &&
                                  `(${request.cost_source})`}
                              </dd>
                            </div>
                          </dl>
                          {request.error && (
                            <Notice warning>{request.error}</Notice>
                          )}
                          <Code
                            value={request.body}
                            label="Exact request body (secrets and reasoning redacted)"
                          />
                          <Code
                            value={request.response}
                            label="Completion / tool calls"
                          />
                          <Code
                            value={request.usage}
                            label="Raw provider usage"
                          />
                        </section>
                      ),
                    )}
                  {inspector === "context" && (
                    <>
                      {detail.requests.map(
                        (request: RecordData, index: number) => (
                          <section className="request-detail" key={request.id}>
                            <h3>
                              Request #{index + 1} · {request.purpose}
                            </h3>
                            <p className="muted small-text">
                              {num(request.context.estimated_tokens)} estimated
                              tokens · checkpoint{" "}
                              {request.context.checkpoint ?? "—"}
                            </p>
                            {request.context.prompt_layers?.map(
                              (layer: RecordData) => (
                                <Code
                                  key={layer.id}
                                  label={`Prompt layer · ${layer.id}`}
                                  value={layer}
                                />
                              ),
                            )}
                            <Code
                              value={
                                request.context.summary ||
                                request.context.previous_summary ||
                                ""
                              }
                              label="Effective compaction summary"
                            />
                            <Code
                              value={request.context.message_ids || []}
                              label="Included message IDs"
                            />
                            <Code
                              value={request.body.messages}
                              label="Ordered messages sent to the provider"
                            />
                            <Code
                              value={request.context}
                              label="All assembly metadata"
                            />
                          </section>
                        ),
                      )}
                      {detail.events
                        .filter((e: RecordData) =>
                          e.kind.startsWith("compaction."),
                        )
                        .map((e: RecordData) => (
                          <Code key={e.id} label={e.kind} value={e.data} />
                        ))}
                    </>
                  )}
                  {inspector === "tools" && (
                    <>
                      {detail.events
                        .filter((e: RecordData) => e.kind.startsWith("tool."))
                        .map((e: RecordData) => (
                          <Code
                            key={e.id}
                            label={`${e.kind} · ${e.data.name} · ${e.data.call_id}`}
                            value={e.data}
                            expanded
                          />
                        ))}
                      {!detail.events.some((e: RecordData) =>
                        e.kind.startsWith("tool."),
                      ) && (
                        <Empty
                          title="No plugin calls in this turn"
                          icon={<Terminal size={23} />}
                        >
                          The model can choose to speak or listen directly.
                        </Empty>
                      )}
                    </>
                  )}
                  {inspector === "delivery" && (
                    <>
                      {detail.outbox.map((out: RecordData) => (
                        <section className="request-detail" key={out.id}>
                          <div className="section-title">
                            <h3>Discord delivery</h3>
                            <Badge tone={tone(out.status)}>{out.status}</Badge>
                          </div>
                          <pre className="delivery-content">{out.content}</pre>
                          <dl className="detail-list">
                            <div>
                              <dt>Outbox ID</dt>
                              <dd>{out.id}</dd>
                            </div>
                            <div>
                              <dt>Channel</dt>
                              <dd>{out.channel_id}</dd>
                            </div>
                            <div>
                              <dt>Reply to</dt>
                              <dd>{out.reply_to || "—"}</dd>
                            </div>
                            <div>
                              <dt>Discord message</dt>
                              <dd>{out.discord_id || "Unconfirmed"}</dd>
                            </div>
                            <div>
                              <dt>Sent at</dt>
                              <dd>
                                {out.sent_at
                                  ? dateLabel(out.sent_at)
                                  : "Not confirmed sent"}
                              </dd>
                            </div>
                          </dl>
                          {out.error && <Notice warning>{out.error}</Notice>}
                          {JSON.parse(out.artifacts).map((id: string) => (
                            <a
                              className="button"
                              key={id}
                              href={`/api/artifacts/${id}`}
                              download
                            >
                              <ArrowDownToLine size={14} />
                              Download attachment
                            </a>
                          ))}
                        </section>
                      ))}
                      {!detail.outbox.length && (
                        <Empty
                          title={
                            detail.turn.status === "silent"
                              ? "A deliberate pause."
                              : "No message was queued."
                          }
                          icon={<MessageSquare size={24} />}
                        >
                          {detail.turn.status === "silent"
                            ? "The model chose to listen. Its activation and usage are still recorded."
                            : "Inspect the timeline and requests to see where this turn ended."}
                        </Empty>
                      )}
                    </>
                  )}
                  {inspector === "json" && (
                    <Code value={detail} expanded label="Complete trajectory" />
                  )}
                </div>
              </>
            )}
          </aside>
        )}
      </div>
      {eventDetail && (
        <Modal
          title={eventDetail.kind}
          subtitle={`Event #${eventDetail.seq} · ${dateLabel(eventDetail.at)}`}
          close={() => setEventDetail(null)}
          wide
        >
          <div className="modal-body">
            <Code label="Event payload" value={eventDetail} expanded />
            {eventDetail.turn_id && (
              <button
                className="button primary"
                onClick={() => {
                  setSelected(eventDetail.turn_id);
                  setView("turns");
                  setEventDetail(null);
                }}
              >
                <FileJson2 size={15} />
                Inspect owning turn
              </button>
            )}
          </div>
        </Modal>
      )}
    </>
  );
}

function Waterfall({ detail }: { detail: RecordData }) {
  const start = detail.turn.started_at;
  const end = detail.turn.ended_at || Date.now() / 1000;
  const span = Math.max(0.001, end - start);
  return (
    <div className="waterfall">
      <div className="section-title">
        <h3>Request timing</h3>
        <span>{duration(span * 1000)}</span>
      </div>
      {detail.requests.map((r: RecordData, i: number) => {
        const size = (((r.ended_at || end) - r.started_at) / span) * 100;
        const first =
          r.ttft_ms != null && r.duration_ms
            ? Math.min(100, (r.ttft_ms / r.duration_ms) * 100)
            : 0;
        return (
          <div className="waterfall-row" key={r.id}>
            <span>
              #{i + 1} {r.purpose === "compaction" ? "compact" : "generate"}
            </span>
            <div className="waterfall-track">
              <div
                title={`${duration(r.duration_ms)} · TTFT ${duration(r.ttft_ms)}`}
                className={`waterfall-span ${r.status === "failed" ? "failed" : ""}`}
                style={{
                  left: `${Math.max(0, ((r.started_at - start) / span) * 100)}%`,
                  width: `${Math.max(0.6, size)}%`,
                }}
              >
                <i style={{ width: `${first}%` }} />
              </div>
            </div>
          </div>
        );
      })}
      {!detail.requests.length && (
        <p className="muted small-text">
          The turn has not reached the provider.
        </p>
      )}
      <div className="waterfall-legend">
        <span>
          <i className="legend-dot lavender" />
          Time to first token
        </span>
        <span>
          <i className="legend-dot lime" />
          Response / decoding
        </span>
      </div>
    </div>
  );
}
