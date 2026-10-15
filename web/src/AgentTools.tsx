import { useEffect, useState } from "react";
import { api, num } from "./api";
import type { RecordData } from "./api";
import { Badge, Empty, Field, Notice } from "./components";
import "./AgentTools.css";

export const agentToolIds = ["workspace", "filesystem", "shell", "web_fetch"];

const labels: Record<string, string> = {
  max_bytes: "Saved filesystem capacity (bytes)",
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
  max_jobs_per_bot: "Recent jobs per bot (older records archived)",
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
  const filesystemCapacity = config.max_bytes ?? defaults.max_bytes;
  const fileLimit =
    Number.isSafeInteger(filesystemCapacity) && filesystemCapacity > 0
      ? Math.floor(filesystemCapacity / 2)
      : null;
  return (
    <section className="agent-tools" aria-label="Working limits">
      <Notice>
        No API key is required. Simple filesystem provides one saved directory
        per bot. Bash mounts it automatically while enabled; disabling it hides
        saved files without deleting them. Without that mount, Bash uses
        disposable files unless an existing Private workspace task is explicitly
        selected. Job logs remain inspectable under their original grants.
      </Notice>
      {error && <Notice warning>{error}</Notice>}
      <div className="form-grid">
        {Object.entries<RecordData>(schema).map(([key, property]) => (
          <Field
            key={key}
            label={
              pluginId === "filesystem" && key === "max_files"
                ? "Files and directories"
                : pluginId === "shell" && key === "retention_days"
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
        {pluginId === "filesystem" && (
          <Field
            label="Maximum file size (automatic)"
            hint="Always half of saved filesystem capacity. A bot's capacity override determines its own file limit. This does not reserve free space."
          >
            <output>
              {fileLimit === null
                ? "Set a filesystem capacity"
                : `${num(fileLimit)} bytes (${num(fileLimit / 1048576)} MiB)`}
            </output>
          </Field>
        )}
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
              "tool_working_set_chars",
              "Active tool context (characters)",
              120000,
              12000,
              1000000,
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
            ["filesystems", "Simple filesystems"],
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
                      {kind === "filesystems"
                        ? `${item.bot_id} · /workspace`
                        : item.task ||
                          item.document_id ||
                          item.job_id ||
                          item.id}
                    </strong>
                    <span className="muted">
                      {kind === "filesystems"
                        ? "Shared across this bot's conversations · retained until deleted"
                        : `${item.bot_id} · channel ${item.channel_id}`}
                    </span>
                    {item.url && <span>{item.url}</span>}
                    {item.usage && <strong>{item.usage.display}</strong>}
                    <span>
                      {item.status || "Saved"}
                      {item.archived ? " · Archived" : ""} ·{" "}
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
                            kind === "filesystems"
                              ? "filesystem_files"
                              : kind === "workspaces"
                                ? "files"
                                : kind === "documents"
                                  ? "document"
                                  : "job",
                          bot_id: item.bot_id,
                          ...(kind === "filesystems"
                            ? {}
                            : { channel_id: item.channel_id }),
                          ...(kind === "workspaces"
                            ? { task: item.task }
                            : {
                                id: item.document_id || item.job_id || item.id,
                              }),
                        })
                      }
                    >
                      Inspect{" "}
                      {kind === "workspaces" || kind === "filesystems"
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
          {query.resource.startsWith("filesystem") && (
            <button
              type="button"
              className="text-button"
              disabled={busy}
              onClick={() =>
                inspect({
                  bot_id: query.bot_id,
                  resource: "filesystem_files",
                  path: ".",
                })
              }
            >
              Filesystem root
            </button>
          )}
          {selected.files?.map((file: RecordData) => (
            <button
              type="button"
              className="text-button"
              key={file.path}
              onClick={() =>
                inspect({
                  ...query,
                  resource: query.resource.startsWith("filesystem")
                    ? file.kind === "directory"
                      ? "filesystem_files"
                      : "filesystem_file"
                    : file.kind === "directory"
                      ? "files"
                      : "file",
                  path: file.path,
                  offset: "0",
                  cursor: "0",
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
                  ...(selected.next.cursor !== undefined
                    ? { cursor: selected.next.cursor }
                    : {
                        offset: selected.next.offset ?? selected.next_offset,
                      }),
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
