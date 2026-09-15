import { useEffect, useState } from "react";
import "./Documents.css";
import { Download, ExternalLink, RefreshCw } from "lucide-react";
import { api, dateLabel, type RecordData } from "./api";
import { Badge, Empty, Field, Notice, Switch } from "./components";

export function DocumentPluginSettings({
  config,
  onChange,
}: {
  config: RecordData;
  onChange: (value: RecordData) => void;
}) {
  const remote = config.remote || {};
  const setRemote = (name: string, value: string | number) =>
    onChange({ ...config, remote: { ...remote, [name]: value } });
  return (
    <div className="document-stack">
      <h3>Publication</h3>
      <Switch
        label="Automatically publish document changes"
        checked={config.auto_publish === true}
        onChange={(value) => onChange({ ...config, auto_publish: value })}
      />
      <p className="muted">
        When enabled, every successful site-file save creates a local published
        revision and queues remote sync. When disabled, drafts stay private
        until the bot explicitly publishes them. Settings take effect after Save
        changes.
      </p>
      <Field
        label="Local publication base URL"
        hint="The local Hortator server. Published snapshots are public to anyone who can reach this server; drafts stay private."
      >
        <input
          type="url"
          value={config.local_base_url ?? "http://127.0.0.1:8000"}
          placeholder="http://127.0.0.1:8000"
          onChange={(event) =>
            onChange({ ...config, local_base_url: event.target.value })
          }
        />
      </Field>
      <Field
        label="Remote public base URL"
        hint="The HTTPS address visitors use. URLs append /stable-bot-id/site-slug/. A URL becomes a live link only after delivery is confirmed."
      >
        <input
          type="url"
          value={config.public_base_url || ""}
          placeholder="https://council.example.com"
          onChange={(event) =>
            onChange({ ...config, public_base_url: event.target.value })
          }
        />
      </Field>
      <h3>Automatic remote sync</h3>
      <Switch
        label="Enable remote delivery"
        checked={config.remote_enabled === true}
        onChange={(value) => onChange({ ...config, remote_enabled: value })}
      />
      <div className="form-grid">
        <Field
          label="SSH server"
          hint="Hostname or IP address, without ssh://."
        >
          <input
            value={remote.host || ""}
            placeholder="council.example.com"
            onChange={(event) => setRemote("host", event.target.value)}
          />
        </Field>
        <Field label="SSH port">
          <input
            type="number"
            min="1"
            max="65535"
            step="1"
            value={remote.port ?? 22}
            onChange={(event) => setRemote("port", Number(event.target.value))}
          />
        </Field>
        <Field label="SSH username">
          <input
            value={remote.username || ""}
            onChange={(event) => setRemote("username", event.target.value)}
          />
        </Field>
        <Field
          label="SSH identity name"
          hint="An existing Hortator vault identity. This is a name, never a private key or password."
        >
          <input
            value={remote.identity ?? "publishing"}
            placeholder="publishing"
            onChange={(event) => setRemote("identity", event.target.value)}
          />
        </Field>
        <Field
          label="Remote web directory"
          hint="Absolute directory served by the public domain. Each bot and site gets its own subfolder."
        >
          <input
            value={remote.web_root || ""}
            placeholder="/home/account/council"
            onChange={(event) => setRemote("web_root", event.target.value)}
          />
        </Field>
        <Field
          label="Remote history directory"
          hint="Absolute directory outside the web directory for retained releases and private publication history."
        >
          <input
            value={remote.state_root || ""}
            placeholder="/home/account/.local/share/hortator-publishing"
            onChange={(event) => setRemote("state_root", event.target.value)}
          />
        </Field>
        <Field
          label="Wait after latest change (seconds)"
          hint="Groups nearby saves before uploading the latest site revision."
        >
          <input
            type="number"
            min="0"
            step="1"
            value={remote.debounce_seconds ?? 5}
            onChange={(event) =>
              setRemote("debounce_seconds", Number(event.target.value))
            }
          />
        </Field>
      </div>
      <Notice>
        The worker uses Hortator’s encrypted SSH identity and verified server
        fingerprint. This plugin needs no API key. Remote destinations and
        automatic publication are global settings; a bot can override only its
        local publication base URL.
      </Notice>
    </div>
  );
}

export function DocumentBotSettings({
  config,
  inheritedLocalUrl,
  onChange,
}: {
  config: RecordData;
  inheritedLocalUrl: string;
  onChange: (value: RecordData) => void;
}) {
  return (
    <Field
      label="Bot local publication base URL"
      hint={`Optional override; leave empty to use ${inheritedLocalUrl}. Remote destination and automatic publication are configured in the global document plugin.`}
    >
      <input
        type="url"
        value={config.local_base_url || ""}
        placeholder={inheritedLocalUrl}
        onChange={(event) => {
          const next = { ...config };
          if (event.target.value) next.local_base_url = event.target.value;
          else delete next.local_base_url;
          onChange(next);
        }}
      />
    </Field>
  );
}

type Sync = {
  id?: string;
  revision: number;
  status: string;
  target_url?: string | null;
  attempts?: number;
  last_error?: string | null;
  retry_at?: number | null;
  delivered_at?: number | null;
  snapshot_commit?: string | null;
  remote_commit?: string | null;
};

type Site = {
  site: string;
  bot_id: string;
  title: string;
  revision: number;
  published_revision: number;
  local_ready: boolean;
  local_path: string;
  remote_status?: string;
  public_url?: string | null;
  planned_public_url?: string | null;
  synced_revision?: number;
  delivery_current?: boolean;
  files: { path: string; bytes: number; mime: string }[];
  sync: Sync | null;
};

type Publishing = {
  enabled: boolean;
  configured: boolean;
  status: string;
  last_error?: string | null;
};

const statusLabel = (status: string) =>
  ({
    disabled: "Disabled",
    unconfigured: "Setup required",
    not_configured: "Setup required",
    ready: "Ready",
    idle: "Ready",
    queued: "Queued",
    syncing: "Uploading",
    uploading: "Uploading",
    delivered: "Delivered",
    failed: "Failed",
    error: "Failed",
    retrying: "Retry scheduled",
    superseded: "Replaced by a newer revision",
    not_queued: "Not queued",
  })[status] || status.replaceAll("_", " ");

const statusTone = (status: string) =>
  ["failed", "error"].includes(status)
    ? "red"
    : ["delivered", "ready", "idle"].includes(status)
      ? "green"
      : ["queued", "syncing", "uploading", "retrying"].includes(status)
        ? "amber"
        : "neutral";

function publicLink(site: Site) {
  if (!site.public_url || !site.synced_revision) return null;
  try {
    const url = new URL(site.public_url);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

function isoTime(value: number) {
  const date = new Date(value * 1000);
  return Number.isNaN(date.valueOf()) ? "Unknown time" : dateLabel(value);
}

export function DocumentSitesPanel({ botId }: { botId?: string }) {
  const [sites, setSites] = useState<Site[]>([]);
  const [publishing, setPublishing] = useState<Publishing | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setError("");
    let fetching = false;
    const fetchSites = () => {
      if (fetching) return;
      fetching = true;
      api<{ sites: Site[]; publishing?: Publishing }>("/api/documents")
        .then((result) => {
          if (!current) return;
          setSites(
            result.sites.filter((site) => !botId || site.bot_id === botId),
          );
          setPublishing(result.publishing || null);
          setError("");
        })
        .catch((failure: Error) => {
          if (current) setError(failure.message);
        })
        .finally(() => {
          fetching = false;
          if (current) setLoading(false);
        });
    };
    fetchSites();
    const timer = window.setInterval(fetchSites, 5000);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [botId, refresh]);
  return (
    <section aria-label="Documents and publication" className="document-stack">
      <div className="document-heading">
        <h3>Documents and publication</h3>
        <button
          type="button"
          className="button secondary"
          disabled={loading}
          onClick={() => setRefresh((value) => value + 1)}
        >
          <RefreshCw size={14} /> Refresh sites
        </button>
      </div>
      <p className="muted">
        Draft downloads require your dashboard session. Local publication
        exposes only its published revision. Remote links show the last
        confirmed delivery; newer edits may still be queued. Status refreshes
        every five seconds.
      </p>
      <Notice>
        Bots must create a named site before editing it. Each site lives under
        /stable-bot-id/site-slug/; the bot’s root is not a site. A bot can edit
        only its own sites, cannot delete files or sites, and cannot inherit a
        deleted bot’s folder.
      </Notice>
      {publishing && (
        <div aria-label="Remote publishing worker">
          <Badge
            tone={
              publishing.enabled ? statusTone(publishing.status) : "neutral"
            }
          >
            Remote sync ·{" "}
            {!publishing.enabled
              ? "Disabled"
              : !publishing.configured
                ? "Setup required"
                : statusLabel(publishing.status)}
          </Badge>
          {publishing.last_error && <p role="alert">{publishing.last_error}</p>}
        </div>
      )}
      {error && <div role="alert">{error}</div>}
      {loading && <p role="status">Loading sites…</p>}
      {!loading && !error && sites.length === 0 && (
        <Empty title="No documents yet">
          Enable the document plugin and grant it to a bot, then ask it to
          create a site or document.
        </Empty>
      )}
      {sites.map((site) => {
        const remoteStatus =
          site.remote_status || site.sync?.status || "not_queued";
        const deliveredUrl = publicLink(site);
        const remoteLabel =
          remoteStatus === "delivered" && !site.delivery_current
            ? "Earlier revision delivered"
            : statusLabel(remoteStatus);
        const plannedUrl = site.planned_public_url || site.sync?.target_url;
        return (
          <article
            className="document-card"
            key={`${site.bot_id}/${site.site}`}
          >
            <h4>{site.title}</h4>
            <p className="muted">
              {site.bot_id}/{site.site} · Current revision {site.revision} ·{" "}
              {site.published_revision
                ? `Local revision ${site.published_revision}`
                : "Private draft only"}
            </p>
            <Badge tone={site.local_ready ? "green" : "neutral"}>
              {site.local_ready ? "Published locally" : "Not published"}
            </Badge>{" "}
            <Badge
              tone={
                remoteStatus === "delivered" && !site.delivery_current
                  ? "amber"
                  : statusTone(remoteStatus)
              }
            >
              Remote · {remoteLabel}
            </Badge>
            {!!site.synced_revision && (
              <p className="muted">
                Last delivered revision {site.synced_revision} ·{" "}
                {site.delivery_current
                  ? "Includes all current edits"
                  : "Newer edits are not confirmed remotely"}
              </p>
            )}
            {site.local_ready && (
              <p>
                <a
                  href={site.local_path}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink size={14} /> Open local published site
                </a>
              </p>
            )}
            {deliveredUrl && (
              <p>
                <a
                  href={deliveredUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <ExternalLink size={14} /> Open remote published site
                </a>
              </p>
            )}
            {plannedUrl && !site.delivery_current && (
              <p className="muted">
                Planned remote URL (delivery not confirmed): {plannedUrl}
              </p>
            )}
            {site.sync?.last_error && (
              <p role="alert">Last delivery error: {site.sync.last_error}</p>
            )}
            {site.sync && (
              <details>
                <summary>
                  Delivery details · revision {site.sync.revision}
                </summary>
                <p>
                  {statusLabel(site.sync.status)} · Attempts:{" "}
                  {site.sync.attempts ?? 0}
                </p>
                {site.sync.id && (
                  <p>
                    Queue ID: <code>{site.sync.id}</code>
                  </p>
                )}
                {site.sync.retry_at != null && (
                  <p>
                    Retry at: <time>{isoTime(site.sync.retry_at)}</time>
                  </p>
                )}
                {site.sync.delivered_at != null && (
                  <p>
                    Delivered at: <time>{isoTime(site.sync.delivered_at)}</time>
                  </p>
                )}
                {site.sync.snapshot_commit && (
                  <p>
                    Local snapshot commit:{" "}
                    <code>{site.sync.snapshot_commit}</code>
                  </p>
                )}
                {site.sync.remote_commit && (
                  <p>
                    Remote history commit:{" "}
                    <code>{site.sync.remote_commit}</code>
                  </p>
                )}
              </details>
            )}
            {site.files.length > 0 && (
              <details>
                <summary>Download draft files ({site.files.length})</summary>
                <ul>
                  {site.files.map((file) => (
                    <li key={file.path}>
                      <a
                        href={`/api/documents/${encodeURIComponent(site.bot_id)}/${encodeURIComponent(site.site)}/files/${file.path.split("/").map(encodeURIComponent).join("/")}`}
                        download
                      >
                        <Download size={13} /> {file.path}
                      </a>{" "}
                      <small>
                        ({new Intl.NumberFormat().format(file.bytes)} bytes)
                      </small>
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </article>
        );
      })}
    </section>
  );
}
