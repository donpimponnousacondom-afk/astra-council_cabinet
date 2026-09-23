import { useEffect, useRef, useState } from "react";
import { api, dateLabel } from "./api";
import type { RecordData } from "./api";
import { Code, Empty, Field, Notice, Switch } from "./components";
import { byName } from "./ordering";

export function ResearchSettings({
  config,
  profiles,
  onChange,
}: {
  config: RecordData;
  profiles: RecordData[];
  onChange: (value: RecordData) => void;
}) {
  return (
    <>
      <Notice>
        Experimental delegated research. Choose a separate MiMo profile; its
        provider supplies the API credential, retries and concurrency. The
        calling bot keeps its own model. Enable MiMo's native Web Search service
        in its console. Search charges are separate from inference tokens. A
        shared provider with one inference slot can delay ordinary replies while
        research runs.
      </Notice>
      <Field label="Research model profile">
        <select
          value={config.profile_id || ""}
          onChange={(e) => onChange({ ...config, profile_id: e.target.value })}
        >
          <option value="">Select a MiMo profile…</option>
          {byName(profiles).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} · {p.model}
            </option>
          ))}
        </select>
      </Field>
      <div className="form-grid">
        {(
          [
            [
              "max_output_tokens",
              "Research output ceiling (tokens)",
              16384,
              131072,
            ],
            ["timeout_seconds", "Total research deadline (seconds)", 600, 7200],
            ["max_keyword", "Search queries per search round (maximum)", 3, 50],
            ["limit", "Results per search query (maximum)", 5, 50],
          ] as const
        ).map(([key, label, fallback, maximum]) => (
          <Field
            key={key}
            label={label}
            hint={
              key === "max_keyword"
                ? 'MiMo can expand the assignment into this many search queries in one search round. A query can be a phrase such as "capital of France". This controls query count, not word count or research rounds.'
                : key === "limit"
                  ? "Maximum results returned per search query. More results can increase research input tokens. This is a ceiling, not a guarantee of that many distinct or useful sources."
                  : undefined
            }
          >
            <input
              type="number"
              min={1}
              max={maximum}
              step={1}
              required
              value={config[key] === undefined ? fallback : (config[key] ?? "")}
              onChange={(e) =>
                onChange({
                  ...config,
                  [key]: e.target.value === "" ? null : Number(e.target.value),
                })
              }
            />
          </Field>
        ))}
      </div>
      <Field
        label="Outstanding researchers per bot (default)"
        hint="Default for every bot with this capability. Minimum 4, with no fixed upper limit. Active jobs and pending follow-ups share this allowance across the bot's channels. Override it under Bots → Capabilities."
      >
        <input
          type="number"
          min={4}
          step={1}
          required
          value={
            config.max_parallel_jobs === undefined
              ? 4
              : (config.max_parallel_jobs ?? "")
          }
          onChange={(e) =>
            onChange({
              ...config,
              max_parallel_jobs:
                e.target.value === "" ? null : Number(e.target.value),
            })
          }
        />
      </Field>
      <Switch
        label="Follow up when background research finishes"
        checked={config.notify_on_completion ?? true}
        onChange={(value) =>
          onChange({ ...config, notify_on_completion: value })
        }
      />
      <Field
        label="Researcher system prompt"
        hint="The assignment is sent separately. {now} uses council-local time. Reasoning and streaming come from the selected model profile."
      >
        <textarea
          rows={7}
          value={config.system_prompt ?? ""}
          onChange={(e) =>
            onChange({ ...config, system_prompt: e.target.value })
          }
        />
      </Field>
      <Notice>
        Jobs outlive the submitting turn and can run in parallel. Each
        researcher requires its own start tool call within the bot's normal
        round and call budgets. Provider concurrency still limits simultaneous
        model requests. Extra starts at the bot's configured limit are rejected
        until a slot is released; pending follow-ups continue to occupy slots.
        Follow-ups respect pauses and channel permissions. This experiment runs
        in configured conversational channels; slash/panel invocations are not
        supported.
      </Notice>
    </>
  );
}

export function ResearchBotSettings({
  config,
  inheritedLimit,
  onChange,
}: {
  config: RecordData;
  inheritedLimit: number;
  onChange: (value: RecordData) => void;
}) {
  return (
    <section>
      <h3>Background research</h3>
      <Field
        label="Outstanding researchers for this bot"
        hint={`Optional override; leave empty to inherit the global limit of ${inheritedLimit}. Minimum 4, with no fixed upper limit. Active jobs and pending follow-ups count across all this bot's channels. Each new researcher costs one tool call; normal tool budgets and provider concurrency still apply.`}
      >
        <input
          type="number"
          min={4}
          step={1}
          value={config.max_parallel_jobs ?? ""}
          placeholder={`Inherit ${inheritedLimit}`}
          onChange={(e) => {
            const next = { ...config };
            if (e.target.value === "") delete next.max_parallel_jobs;
            else next.max_parallel_jobs = Number(e.target.value);
            onChange(next);
          }}
        />
      </Field>
    </section>
  );
}

export function BackgroundJobsPanel({
  botId,
  plugin,
}: {
  botId?: string;
  plugin?: string;
}) {
  const [jobs, setJobs] = useState<RecordData[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [detail, setDetail] = useState<RecordData | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const query = new URLSearchParams();
        if (botId) query.set("bot_id", botId);
        if (plugin) query.set("plugin", plugin);
        const data = await api<RecordData[]>(`/api/background-jobs?${query}`, {
          signal: controller.signal,
        });
        if (live) {
          setJobs(data);
          setError("");
        }
      } catch (e) {
        if (live) setError(String(e));
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 5000);
    return () => {
      live = false;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [botId, plugin, revision]);
  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    setDetail(null);
    if (selected)
      api<RecordData>(
        `/api/background-jobs/${encodeURIComponent(selected)}?offset=${offset}`,
        { signal: controller.signal },
      )
        .then((value) => {
          if (live) setDetail(value);
        })
        .catch((e) => {
          if (live) setError(String(e));
        });
    return () => {
      live = false;
      controller.abort();
    };
  }, [selected, offset, revision]);
  const cancel = async (id: string) => {
    setBusy(true);
    try {
      await api(`/api/background-jobs/${encodeURIComponent(id)}/cancel`, {
        method: "POST",
      });
      if (mounted.current) {
        setRevision((v) => v + 1);
        setError("");
      }
    } catch (e) {
      if (mounted.current) setError(String(e));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  return (
    <section>
      <h3>Background jobs</h3>
      <button type="button" onClick={() => setRevision((v) => v + 1)}>
        Refresh jobs
      </button>
      {error && <Notice>{error}</Notice>}
      {!jobs.length ? (
        <Empty title="No background jobs in this scope." />
      ) : (
        <div className="table-wrap">
          <table aria-label="Background jobs">
            <thead>
              <tr>
                <th>Job / bot</th>
                <th>State / follow-up</th>
                <th>Started</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td>
                    <button
                      type="button"
                      onClick={() => {
                        setSelected(job.id);
                        setOffset(0);
                      }}
                    >
                      {job.id}
                    </button>
                    <br />
                    {job.bot_id} · {job.plugin}
                  </td>
                  <td>
                    {job.state}
                    <br />
                    {job.notification}
                  </td>
                  <td>{dateLabel(job.created_at)}</td>
                  <td>
                    {(["queued", "running"].includes(job.state) ||
                      job.notification === "pending") && (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void cancel(job.id)}
                      >
                        Cancel job / follow-up
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detail && (
        <>
          <p>
            {detail.state} · {detail.elapsed_seconds}s elapsed
            {detail.error ? ` · ${detail.error}` : ""}
          </p>
          <Code value={detail.assignment} label="Research assignment" />
          <Code
            value={detail.metrics}
            label="Research timing and token usage"
          />
          <Code
            value={detail.content || "No saved result yet."}
            label="Saved result page"
          />
          <p>
            {offset}–{Math.min(offset + 6000, detail.total_chars)} /{" "}
            {detail.total_chars} characters
          </p>
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - 6000))}
          >
            Previous result page
          </button>{" "}
          <button
            type="button"
            disabled={detail.next_offset == null}
            onClick={() => setOffset(detail.next_offset)}
          >
            Next result page
          </button>
        </>
      )}
    </section>
  );
}
