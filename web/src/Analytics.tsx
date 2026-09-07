import { useEffect, useState } from "react";
import {
  Activity,
  ArrowDownToLine,
  Clock3,
  Coins,
  Cpu,
  Layers3,
  Zap,
} from "lucide-react";
import { api, duration, money, num } from "./api";
import type { Dashboard, RecordData } from "./api";
import { Empty, Metric, Notice } from "./components";

export function UsageChart({
  history,
  hours = 24,
}: {
  history: RecordData[];
  hours?: number;
}) {
  const [hover, setHover] = useState<RecordData | null>(null);
  const now = Math.floor(Date.now() / 3600000) * 3600;
  const buckets = hours <= 48 ? hours : Math.min(48, hours);
  const span = (hours * 3600) / buckets;
  const data = Array.from({ length: buckets }, (_, i) => {
    const at = now - (buckets - 1 - i) * span;
    const rows = history.filter((r) => r.at >= at && r.at < at + span);
    return {
      at,
      input_tokens: rows.reduce((n, r) => n + (r.input_tokens || 0), 0),
      output_tokens: rows.reduce((n, r) => n + (r.output_tokens || 0), 0),
      requests: rows.reduce((n, r) => n + r.requests, 0),
    };
  });
  const max = Math.max(1, ...data.map((d) => d.input_tokens + d.output_tokens));
  const hasData = history.some(
    (d) => d.input_tokens != null || d.output_tokens != null,
  );
  return (
    <div className="usage-chart" onMouseLeave={() => setHover(null)}>
      <div className="chart-grid">
        {[1, 0.75, 0.5, 0.25, 0].map((scale, i) => (
          <div key={i}>
            <span>
              {hasData ? num(max * scale, true) : i === 4 ? "0" : "—"}
            </span>
            <i />
          </div>
        ))}
      </div>
      <div className="chart-bars">
        {data.map((d, i) => (
          <div
            key={d.at}
            className="chart-bar-slot"
            tabIndex={hasData ? 0 : -1}
            role="img"
            aria-label={`${new Date(d.at * 1000).toLocaleString()}: ${d.input_tokens} input and ${d.output_tokens} output tokens`}
            onMouseEnter={() => setHover(d)}
            onFocus={() => setHover(d)}
          >
            <div
              className="chart-bar"
              style={{
                height: `${((d.input_tokens + d.output_tokens) / max) * 100}%`,
              }}
            >
              <i className="bar-output" style={{ flexGrow: d.output_tokens }} />
              <i className="bar-input" style={{ flexGrow: d.input_tokens }} />
            </div>
            {i % Math.ceil(buckets / 6) === 0 && (
              <span className="bar-time">
                {new Date(d.at * 1000).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </span>
            )}
          </div>
        ))}
      </div>
      {!hasData && (
        <div className="chart-empty">
          <span>
            <Activity size={20} />
          </span>
          <strong>Room for a good conversation.</strong>
          <p>Your first model requests will bring this chart to life.</p>
        </div>
      )}
      {hover && hasData && (
        <div className="chart-tooltip">
          <strong>{new Date(hover.at * 1000).toLocaleString()}</strong>
          <span>
            {num(hover.input_tokens)} input · {num(hover.output_tokens)} output
          </span>
        </div>
      )}
    </div>
  );
}

export function Analytics({ dashboard }: { dashboard: Dashboard }) {
  const [hours, setHours] = useState(24);
  const [stats, setStats] = useState<RecordData | null>(null);
  const [error, setError] = useState("");
  const [group, setGroup] = useState("bot_id");
  useEffect(() => {
    let active = true;
    const refresh = () =>
      api(`/api/stats?hours=${hours}`)
        .then((v) => {
          if (active) {
            setStats(v);
            setError("");
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [hours]);
  const exportStats = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(stats, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = "council-metrics.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <>
      {error && <Notice warning>{error}</Notice>}
      <div className="analytics-toolbar">
        <div className="range-buttons">
          {[
            [24, "24 hours"],
            [168, "7 days"],
            [720, "30 days"],
          ].map(([value, label]) => (
            <button
              className={hours === value ? "selected" : ""}
              key={value}
              onClick={() => setHours(Number(value))}
            >
              {label}
            </button>
          ))}
        </div>
        <button className="button" onClick={exportStats} disabled={!stats}>
          <ArrowDownToLine size={14} />
          Export metrics
        </button>
      </div>
      {stats && (
        <>
          <div className="metric-grid">
            <Metric
              label="Model requests"
              value={num(stats.total.requests)}
              sub={`${stats.total.failures || 0} failed · ${stats.total.cancellations || 0} cancelled`}
              icon={<Activity size={17} />}
            />
            <Metric
              label="Input / output tokens"
              value={num(stats.total.input_tokens, true)}
              sub={`${num(stats.total.output_tokens, true)} output · ${num(stats.total.cached_tokens, true)} cached input`}
              icon={<Cpu size={17} />}
            />
            <Metric
              label="Reasoning tokens"
              value={num(stats.total.reasoning_tokens, true)}
              sub="Reported by the provider when available"
              icon={<Layers3 size={17} />}
            />
            <Metric
              label="Model cost"
              value={money(stats.total.cost)}
              sub={`${stats.total.cost_known_requests || 0} requests with cost data`}
              icon={<Coins size={17} />}
            />
          </div>
          <section className="panel usage-panel analytics-chart">
            <div className="panel-title">
              <div>
                <h2>Tokens over time</h2>
                <p>Includes generation and compaction calls</p>
              </div>
              <div className="chart-legend">
                <span>
                  <i className="legend-dot lime" />
                  Input
                </span>
                <span>
                  <i className="legend-dot lavender" />
                  Output
                </span>
              </div>
            </div>
            <UsageChart history={stats.history} hours={hours} />
          </section>
          <div className="latency-grid">
            <Metric
              label="Average TTFT"
              value={duration(stats.total.avg_ttft_ms)}
              sub={`p95 ${duration(stats.total.p95_ttft_ms)}`}
              icon={<Zap size={17} />}
            />
            <Metric
              label="Average completion time"
              value={duration(stats.total.avg_duration_ms)}
              sub={`p95 ${duration(stats.total.p95_duration_ms)}`}
              icon={<Clock3 size={17} />}
            />
            <Metric
              label="Observed error rate"
              value={
                stats.total.requests
                  ? `${(((stats.total.failures || 0) / stats.total.requests) * 100).toFixed(1)}%`
                  : "—"
              }
              sub="Failed model calls / all attempted calls"
              icon={<Activity size={17} />}
            />
          </div>
          <section className="panel metrics-table-panel">
            <div className="panel-title">
              <div>
                <h2>Performance breakdown</h2>
                <p>
                  Compare personalities, model configurations, and connections
                </p>
              </div>
              <select
                aria-label="Group metrics by"
                value={group}
                onChange={(e) => setGroup(e.target.value)}
              >
                <option value="bot_id">By bot</option>
                <option value="profile_id">By model profile</option>
                <option value="provider_id">By provider</option>
                <option value="model">By model ID</option>
                <option value="configuration">By profile revision</option>
                <option value="purpose">By request purpose</option>
              </select>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>
                      {group === "bot_id"
                        ? "Bot"
                        : group === "profile_id"
                          ? "Profile"
                          : group === "provider_id"
                            ? "Provider"
                            : "Group"}
                    </th>
                    <th>Requests</th>
                    <th>Input</th>
                    <th>Output</th>
                    <th>Reasoning</th>
                    <th>Cached</th>
                    <th>Avg. TTFT</th>
                    <th>Completion</th>
                    <th>Failures</th>
                    <th>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.by[group].map((row: RecordData) => (
                    <tr key={row.id}>
                      <td>
                        <strong>
                          {(group === "bot_id"
                            ? dashboard.bots
                            : group === "profile_id"
                              ? dashboard.profiles
                              : group === "provider_id"
                                ? dashboard.providers
                                : []
                          ).find((e) => e.id === row.id)?.name || row.id}
                        </strong>
                      </td>
                      <td>{num(row.requests)}</td>
                      <td>{num(row.input_tokens, true)}</td>
                      <td>{num(row.output_tokens, true)}</td>
                      <td>{num(row.reasoning_tokens, true)}</td>
                      <td>{num(row.cached_tokens, true)}</td>
                      <td>{duration(row.avg_ttft_ms)}</td>
                      <td>{duration(row.avg_duration_ms)}</td>
                      <td className={row.failures ? "red-text" : ""}>
                        {row.failures || 0}
                      </td>
                      <td>{money(row.cost)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!stats.by[group].length && (
              <Empty title="No requests in this period">
                Activate a configured bot to start collecting model statistics.
              </Empty>
            )}
          </section>
          <div className="two-column">
            <section className="panel compact-panel">
              <h2>Activation outcomes</h2>
              {stats.turns.map((t: RecordData) => (
                <div className="stat-row" key={t.status}>
                  <span>{t.status}</span>
                  <strong>{num(t.count)}</strong>
                </div>
              ))}
              {!stats.turns.length && (
                <p className="muted">No activations recorded.</p>
              )}
            </section>
            <section className="panel compact-panel">
              <h2>Plugin execution</h2>
              {stats.tools.map((t: RecordData) => (
                <div className="stat-row" key={t.name + t.kind}>
                  <span>
                    {t.name} <small>{t.kind.split(".")[1]}</small>
                  </span>
                  <strong>
                    {t.count} · {duration(t.avg_duration_ms)}
                  </strong>
                </div>
              ))}
              {!stats.tools.length && (
                <p className="muted">No tool calls recorded.</p>
              )}
            </section>
          </div>
          <Notice>
            Missing measurements are shown as “—”. Cost totals include only
            known values; {stats.total.estimated_cost_requests || 0} request
            costs are estimated from profile prices. TTFT requires streaming.
            Percentiles use the latest {num(stats.total.latency_sample_size)}{" "}
            completed requests, capped at 10,000. Plugin media/search charges
            are separate from model cost.
          </Notice>
        </>
      )}
    </>
  );
}
