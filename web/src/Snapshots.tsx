import { useCallback, useEffect, useRef, useState } from "react";
import { api, dateLabel, num } from "./api";
import type { Dashboard, RecordData } from "./api";
import { Badge, Code, Field, Notice } from "./components";
import { WorkbenchTable } from "./WorkbenchTable";
import "./Snapshots.css";

type Catalog = {
  snapshots: RecordData[];
  directory: string;
  paused: boolean;
  busy: boolean;
  operation?: string;
  last_restore?: RecordData;
};

export function Snapshots({
  dashboard,
  refresh,
}: {
  dashboard: Dashboard;
  refresh: () => Promise<void>;
}) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [selected, setSelected] = useState<RecordData | null>(null);
  const [scope, setScope] = useState("bot");
  const [bot, setBot] = useState("");
  const [channel, setChannel] = useState("");
  const [includeContext, setIncludeContext] = useState(false);
  const [includeGlobal, setIncludeGlobal] = useState(false);
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<RecordData | null>(null);
  const mounted = useRef(false);
  const pending = useRef(false);
  const dirty = useRef(false);
  dirty.current = !!(name || note || confirmation);
  const load = useCallback(async () => {
    try {
      const data = await api<Catalog>("/api/snapshots");
      if (mounted.current) setCatalog(data);
    } catch (e) {
      if (mounted.current) setError(String(e));
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = window.setInterval(() => {
      if (!pending.current) void load();
    }, 5000);
    const guard = (event: Event) => {
      if (pending.current) {
        event.preventDefault();
        setError(
          "Wait for the snapshot operation to finish before leaving this page.",
        );
      } else if (
        dirty.current &&
        !window.confirm("Discard the unsaved snapshot form?")
      )
        event.preventDefault();
    };
    const unload = (event: BeforeUnloadEvent) => {
      if (pending.current || dirty.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("hortator:before-editor-close", guard);
    window.addEventListener("beforeunload", unload);
    return () => {
      mounted.current = false;
      clearInterval(timer);
      window.removeEventListener("hortator:before-editor-close", guard);
      window.removeEventListener("beforeunload", unload);
    };
  }, [load]);
  const run = async (operation: string, path: string, body: RecordData) => {
    if (pending.current) return;
    pending.current = true;
    setBusy(operation);
    setError("");
    try {
      const result = await api(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!mounted.current) return;
      setReceipt(result);
      if (operation === "capture") {
        setName("");
        setNote("");
      }
      if (operation === "restore") {
        setConfirmation("");
        setSelected(null);
      }
      await load();
      await refresh();
    } catch (e) {
      if (mounted.current) {
        setError(
          `${String(e)}. Check the catalog before retrying: operations continue if the browser disconnects.`,
        );
        await load();
      }
    } finally {
      pending.current = false;
      if (mounted.current) setBusy("");
    }
  };
  const choose = (entry: RecordData) => {
    if (
      confirmation &&
      !window.confirm("Discard the current restore confirmation?")
    )
      return;
    setSelected(entry);
    setBot("");
    setChannel("");
    setScope("bot");
    setIncludeContext(false);
    setIncludeGlobal(false);
    setConfirmation("");
    setError("");
  };
  const blocked = !!busy || !!catalog?.busy;
  const current =
    selected &&
    (catalog?.snapshots.find((s) => s.id === selected.id) || selected);
  const availableBots = (current?.bots || []).filter((b: RecordData) =>
    dashboard.bots.some((live) => live.id === b.id),
  );
  const channels: string[] =
    current?.bots?.find((b: RecordData) => b.id === bot)?.channels || [];
  const lastRestore = catalog?.last_restore;
  return (
    <section className="snapshot-page" aria-label="State snapshots">
      <p>
        Capture the database, encrypted credentials, matching master key and
        managed files together. Capture briefly stops active work and resumes
        the previous runtime mode. Restore creates a recovery snapshot and
        leaves all runtime services paused.
      </p>
      {error && (
        <div role="alert">
          <Notice>{error}</Notice>
        </div>
      )}
      {(catalog?.paused || dashboard.maintenance_pause) && (
        <div className="snapshot-pause" role="status">
          <strong>Runtime paused after restore</strong>
          <span>
            Inspect configuration and notes before reconnecting bots or
            publishing. Resume restores normal operation using the current
            configuration.
          </span>
          <button
            className="primary"
            disabled={blocked}
            onClick={() => void run("resume", "/api/snapshots/resume", {})}
          >
            Resume runtime
          </button>
        </div>
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void run("capture", "/api/snapshots", { name, note });
        }}
      >
        <fieldset disabled={blocked} className="snapshot-create">
          <legend>New snapshot</legend>
          <Field label="Snapshot name">
            <input
              required
              maxLength={100}
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Before Loki experiment"
            />
          </Field>
          <Field label="Snapshot note">
            <input
              maxLength={2000}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Model, channel or purpose of this experiment"
            />
          </Field>
          <button className="primary" disabled={!name.trim()} type="submit">
            Capture snapshot
          </button>
        </fieldset>
      </form>
      <div className="snapshot-toolbar">
        <span>
          {catalog
            ? `${catalog.snapshots.length} snapshots · ${catalog.directory}`
            : "Loading snapshots…"}
        </span>
        <button disabled={blocked} onClick={() => void load()}>
          Refresh snapshots
        </button>
      </div>
      {busy && (
        <Notice>
          {busy === "capture"
            ? "Stopping writers and creating a consistent snapshot…"
            : busy === "restore"
              ? "Verifying, saving recovery state and restoring…"
              : "Resuming runtime…"}{" "}
          Keep this page open.
        </Notice>
      )}
      {catalog && (
        <WorkbenchTable
          label="Snapshots"
          rows={catalog.snapshots}
          selected={current?.id}
          columns={[
            {
              id: "name",
              label: "Snapshot",
              value: (r) => r.name || r.id,
              render: (r) => (
                <button
                  className="record-name"
                  disabled={blocked}
                  onClick={() => choose(r)}
                >
                  <strong>{r.name || r.id}</strong>
                  <small>{r.id}</small>
                </button>
              ),
            },
            {
              id: "date",
              label: "Created",
              value: (r) => r.created_at || "",
              render: (r) => dateLabel(r.created_at),
            },
            {
              id: "version",
              label: "Source",
              render: (r) => (
                <span title={r.build?.commit_title}>
                  {r.source_commit?.slice(0, 12) || "Unknown"} · format{" "}
                  {r.format_version || "?"}
                </span>
              ),
            },
            {
              id: "size",
              label: "Size / files",
              value: (r) => r.bytes || 0,
              render: (r) =>
                `${num((r.bytes || 0) / 1048576)} MiB / ${num(r.file_count)}`,
            },
            {
              id: "state",
              label: "Restore compatibility",
              render: (r) => (
                <Badge tone={r.compatible ? "green" : "amber"}>
                  {r.compatible ? "Compatible" : "Blocked"}
                  {r.reason === "recovery" ? " · recovery" : ""}
                </Badge>
              ),
            },
          ]}
        />
      )}
      {current && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void run(
              "restore",
              `/api/snapshots/${encodeURIComponent(current.id)}/restore`,
              scope === "full"
                ? { scope, confirmation }
                : {
                    scope,
                    confirmation,
                    bot_id: bot,
                    ...(channel ? { channel_id: channel } : {}),
                    include_context: includeContext,
                    include_global_memory: includeGlobal,
                  },
            );
          }}
        >
          <fieldset disabled={blocked} className="snapshot-restore">
            <legend>Inspect / restore {current.name || current.id}</legend>
            {current.note && <p>{current.note}</p>}
            {!current.compatible && (
              <Notice>
                Restore blocked:{" "}
                {(
                  current.reasons || [
                    current.compatibility_reason || "Invalid snapshot",
                  ]
                ).join(". ")}
                . Start the recorded clean source version to restore a
                compatible snapshot.
              </Notice>
            )}
            <Field label="Restore scope">
              <select
                value={scope}
                onChange={(e) => {
                  setScope(e.target.value);
                  setConfirmation("");
                }}
              >
                <option value="bot">Selected bot’s notes</option>
                <option value="full">Full application state</option>
              </select>
            </Field>
            {scope === "bot" ? (
              <>
                <Field label="Restore bot">
                  <select
                    required
                    value={bot}
                    onChange={(e) => {
                      setBot(e.target.value);
                      setChannel("");
                    }}
                  >
                    <option value="">Select a bot</option>
                    {availableBots.map((b: RecordData) => (
                      <option key={b.id} value={b.id}>
                        {dashboard.bots.find((live) => live.id === b.id)
                          ?.name || b.id}{" "}
                        · {b.id}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Restore channel notes">
                  <select
                    value={channel}
                    onChange={(e) => setChannel(e.target.value)}
                  >
                    <option value="">All channels for this bot</option>
                    {channels.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </Field>
                <label className="snapshot-check">
                  <input
                    type="checkbox"
                    checked={includeGlobal}
                    onChange={(e) => setIncludeGlobal(e.target.checked)}
                  />
                  Also restore this bot’s global notebook (all channels)
                </label>
                <label className="snapshot-check">
                  <input
                    type="checkbox"
                    checked={includeContext}
                    onChange={(e) => setIncludeContext(e.target.checked)}
                  />
                  Also restore context summaries and checkpoints
                </label>
                <p>
                  Private notes are replaced within the selected scope. Other
                  bots and configuration stay intact. Context restore retains
                  newer shared transcript evidence; use a new experiment channel
                  or full restore for a complete rewind.
                </p>
              </>
            ) : (
              <p>
                Replace configuration, credentials, notes, contexts and managed
                local files. Dashboard sessions are revoked: sign in again with
                the restored password. Discord messages, provider charges and
                remote publications cannot be undone.
              </p>
            )}
            <Field
              label="Confirm snapshot ID"
              hint={`Type ${current.id} to confirm this restore. A recovery snapshot is created first.`}
            >
              <input
                required
                value={confirmation}
                onChange={(e) => setConfirmation(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
            <button
              className="danger"
              type="submit"
              disabled={
                !current.compatible ||
                confirmation !== current.id ||
                (scope === "bot" && !bot)
              }
            >
              Restore snapshot and pause
            </button>
            <Code label="Snapshot manifest metadata" value={current} />
          </fieldset>
        </form>
      )}
      {(receipt || lastRestore) && (
        <Code
          label="Last snapshot operation"
          value={receipt || lastRestore}
          expanded
        />
      )}
    </section>
  );
}
