import { useEffect, useState } from "react";
import { api, num } from "./api";
import type { RecordData } from "./api";
import { Badge, Empty, Field, Notice } from "./components";
import "./AgentTools.css";

export const agentToolIds = ["workspace", "shell", "web_fetch"];

const labels: Record<string, string> = {
  max_download_bytes: "Download limit (bytes)",
  chunk_chars: "Returned chunk (Unicode characters)",
  storage_quota_bytes: "Fetched text storage per bot (bytes)",
  retention_seconds: "Snapshot retention (seconds)",
  max_file_bytes: "Workspace file limit (bytes)",
  max_workspace_bytes: "Storage per workspace (bytes)",
  max_bot_bytes: "Workspace storage per bot (bytes)",
  max_files: "Files per workspace",
  max_workspaces: "Workspaces per bot",
  max_read_bytes: "Returned file chunk (bytes)",
  retention_days: "Idle workspace retention (days)",
  timeout_seconds: "Command time limit (seconds)",
  max_output_bytes: "Combined job output limit (bytes)",
  output_bytes: "Combined job output limit (bytes)",
  memory_bytes: "Address space per process (bytes)",
  memory_bytes_per_process: "Address space per process (bytes)",
  max_processes: "Processes per job",
  process_limit: "Processes per job",
  package_bytes: "Disposable package storage per job (bytes)",
  package_entries: "Disposable package entries per job",
  max_jobs_per_bot: "Saved jobs per bot",
  job_storage_bytes_per_bot: "Job output storage per bot (bytes)",
};

export function AgentToolSettings({
  pluginId,
  config,
  onChange,
}: {
  pluginId: string;
  config: RecordData;
  onChange: (value: RecordData) => void;
}) {
  const [catalog, setCatalog] = useState<RecordData>();
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api("/api/agentic-tools?limit=1")
      .then((value) => active && setCatalog(value))
      .catch((e) => active && setError(e.message));
    return () => {
      active = false;
    };
  }, []);
  const schema = catalog?.config_schemas?.[pluginId]?.properties || {};
  const defaults = catalog?.defaults?.[pluginId] || {};
  return (
    <section className="agent-tools" aria-label="Working limits">
      <Notice>
        No API key is required. Shell execution also needs the workspace grant
        and a ready isolated runner. Each bot and channel keeps its own files.
      </Notice>
      {error && <Notice warning>{error}</Notice>}
      <div className="form-grid">
        {Object.entries<RecordData>(schema).map(([key, property]) => (
          <Field
            key={key}
            label={
              pluginId === "shell" && key === "retention_days"
                ? "Job log retention (days)"
                : labels[key] || property.title || key.replaceAll("_", " ")
            }
            hint={property.description}
          >
            <input
              type={property.type === "string" ? "text" : "number"}
              min={property.minimum}
              max={property.maximum}
              step={property.type === "number" ? "any" : 1}
              value={config[key] ?? defaults[key] ?? ""}
              onChange={(event) =>
                onChange({
                  ...config,
                  [key]:
                    property.type === "string"
                      ? event.target.value
                      : event.target.value === ""
                        ? null
                        : Number(event.target.value),
                })
              }
            />
          </Field>
        ))}
      </div>
    </section>
  );
}

export function WorkTaskSettings({
  draft,
  set,
}: {
  draft: RecordData;
  set: (key: string, value: any) => void;
}) {
  return (
    <section className="agent-tools" aria-label="File and reading task budget">
      <h3>Extended file and reading tasks</h3>
      <Notice>
        The first successful workspace, web reading or document task start can
        extend a turn once. Later starts cannot add rounds or reset the clock.
        Provider and cost limits continue to apply.
      </Notice>
      <div className="form-grid">
        {(
          [
            [
              "work_task_rounds",
              "Additional file and reading rounds",
              20,
              0,
              100,
            ],
            [
              "work_task_calls_per_round",
              "File and reading calls per round",
              8,
              1,
              20,
            ],
            [
              "work_task_seconds",
              "File and reading time limit (seconds)",
              900,
              30,
              7200,
            ],
            [
              "tool_working_set_tokens",
              "Active tool context (estimated tokens)",
              6000,
              512,
              24000,
            ],
          ] as [string, string, number, number, number][]
        ).map(([key, label, fallback, min, max]) => (
          <Field key={key} label={String(label)}>
            <input
              type="number"
              min={min}
              max={max}
              step="1"
              value={draft[key] ?? fallback}
              onChange={(e) =>
                set(
                  String(key),
                  e.target.value === "" ? null : Number(e.target.value),
                )
              }
            />
          </Field>
        ))}
      </div>
      <p className="muted">
        Older tool output may leave the active prompt. Full results remain in
        the trajectory and can be reread in bounded chunks.
      </p>
    </section>
  );
}

export function AgentToolsPanel({ botId }: { botId?: string }) {
  const [data, setData] = useState<RecordData>();
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<RecordData>();
  const [query, setQuery] = useState<RecordData>();
  const [busy, setBusy] = useState(false);
  const load = () => {
    setBusy(true);
    api(
      `/api/agentic-tools?limit=30${botId ? `&bot_id=${encodeURIComponent(botId)}` : ""}`,
    )
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };
  useEffect(load, [botId]);
  const inspect = (value: RecordData) => {
    setError("");
    setBusy(true);
    setQuery(value);
    api(`/api/agentic-tools/inspect?${new URLSearchParams(value)}`)
      .then(setSelected)
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false));
  };
  return (
    <section className="agent-tools" aria-label="Files, web reading and jobs">
      <div className="agent-tools-heading">
        <h3>Files, web reading and jobs</h3>
        <button
          type="button"
          className="text-button"
          disabled={busy}
          onClick={load}
        >
          Refresh tools
        </button>
      </div>
      {error && <Notice warning>{error}</Notice>}
      {data && (
        <>
          <Badge tone={data.runner?.ready ? "green" : "neutral"}>
            {data.runner?.ready
              ? "Isolated runner ready"
              : "Runner unavailable"}
          </Badge>
          <p className="muted">
            {data.runner?.message ||
              data.runner?.reason ||
              data.runner?.error ||
              "Inspect runner readiness for network status and the available toolchain."}
          </p>
          {!data.runner?.ready && <p className="muted">{data.runner?.setup}</p>}
          <details>
            <summary>Runner limits and available utilities</summary>
            <pre>{JSON.stringify(data.runner, null, 2)}</pre>
          </details>
          <p className="muted">
            Showing up to 30 recent records per store. Inspection is private and
            read only.
          </p>
          {[
            ["workspaces", "Workspaces"],
            ["documents", "Fetched documents"],
            ["jobs", "Shell jobs"],
          ].map(([kind, title]) => (
            <details key={kind} open>
              <summary>
                {title} ({data[kind]?.length || 0})
              </summary>
              {!data[kind]?.length ? (
                <Empty title={`No ${title.toLowerCase()} yet`}>
                  Enabled bots create these through their granted tools.
                </Empty>
              ) : (
                data[kind].map((item: RecordData, index: number) => (
                  <article
                    className="agent-tool-record"
                    key={item.id || item.document_id || item.job_id || index}
                  >
                    <strong>
                      {item.task || item.document_id || item.job_id || item.id}
                    </strong>
                    <span className="muted">
                      {item.bot_id} · channel {item.channel_id}
                    </span>
                    {item.url && <span>{item.url}</span>}
                    <span>
                      {item.status || "Saved"} ·{" "}
                      {num(
                        kind === "jobs"
                          ? (item.stdout_bytes ?? 0) + (item.stderr_bytes ?? 0)
                          : kind === "documents"
                            ? item.total_chars
                            : item.bytes,
                      )}{" "}
                      {kind === "documents"
                        ? "characters"
                        : kind === "jobs"
                          ? "output bytes"
                          : "bytes"}
                    </span>
                    <button
                      type="button"
                      className="text-button"
                      disabled={busy}
                      onClick={() =>
                        inspect({
                          resource:
                            kind === "workspaces"
                              ? "files"
                              : kind === "documents"
                                ? "document"
                                : "job",
                          bot_id: item.bot_id,
                          channel_id: item.channel_id,
                          ...(kind === "workspaces"
                            ? { task: item.task }
                            : {
                                id: item.document_id || item.job_id || item.id,
                              }),
                        })
                      }
                    >
                      Inspect{" "}
                      {kind === "workspaces"
                        ? "files"
                        : kind === "documents"
                          ? "text"
                          : "job"}
                    </button>
                  </article>
                ))
              )}
            </details>
          ))}
        </>
      )}
      {selected && query && (
        <div className="agent-tool-inspection" aria-label="Tool inspection">
          <h4>Private inspection · untrusted source content</h4>
          {selected.files?.map((file: RecordData) => (
            <button
              type="button"
              className="text-button"
              key={file.path}
              onClick={() =>
                inspect({
                  ...query,
                  resource: file.kind === "directory" ? "files" : "file",
                  path: file.path,
                })
              }
            >
              {file.path} ({num(file.bytes)} bytes)
            </button>
          ))}
          {query.resource === "job" &&
            ["stdout", "stderr"].map((stream) => (
              <button
                type="button"
                className="text-button"
                key={stream}
                onClick={() =>
                  inspect({ ...query, resource: "output", stream })
                }
              >
                Read {stream}
              </button>
            ))}
          <pre>{selected.text ?? JSON.stringify(selected, null, 2)}</pre>
          {selected.next && (
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() =>
                inspect({
                  ...query,
                  offset: selected.next.offset ?? selected.next_offset,
                })
              }
            >
              Next chunk
            </button>
          )}
        </div>
      )}
    </section>
  );
}
