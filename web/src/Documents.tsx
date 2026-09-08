import { useEffect, useState } from "react";
import "./Documents.css";
import { Download, ExternalLink, RefreshCw } from "lucide-react";
import { api, type RecordData } from "./api";
import { Badge, Empty, Field, Notice } from "./components";

export function DocumentPluginSettings({
  config,
  onChange,
}: {
  config: RecordData;
  onChange: (value: RecordData) => void;
}) {
  return (
    <div className="document-stack">
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
        label="Planned remote base URL"
        hint="Optional destination metadata. URLs append /bot-id/site-name/. Saving a URL does not upload files."
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
      <Notice>
        Remote delivery is disabled. Bots can create local sites, publish local
        snapshots and queue sync. SSH/SFTP delivery will be configured later.
        This plugin needs no API key.
      </Notice>
    </div>
  );
}

type Site = {
  site: string;
  bot_id: string;
  title: string;
  revision: number;
  published_revision: number;
  local_ready: boolean;
  local_path: string;
  files: { path: string; bytes: number; mime: string }[];
  sync: null | { revision: number; status: string; target_url: string | null };
};

export function DocumentSitesPanel({ botId }: { botId?: string }) {
  const [sites, setSites] = useState<Site[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setError("");
    api<{ sites: Site[] }>("/api/documents")
      .then((result) => {
        if (current)
          setSites(
            result.sites.filter((site) => !botId || site.bot_id === botId),
          );
      })
      .catch((failure: Error) => {
        if (current) setError(failure.message);
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [botId, refresh]);
  return (
    <section aria-label="Local documents and sites" className="document-stack">
      <div className="document-heading">
        <h3>Local documents and sites</h3>
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
        exposes only the published revision. Remote sync is queued, never
        assumed delivered.
      </p>
      {error && <div role="alert">{error}</div>}
      {loading && <p role="status">Loading sites…</p>}
      {!loading && !error && sites.length === 0 && (
        <Empty title="No documents yet">
          Enable the document plugin and grant it to a bot, then ask it to
          create a site or document.
        </Empty>
      )}
      {sites.map((site) => (
        <article className="document-card" key={`${site.bot_id}/${site.site}`}>
          <h4>{site.title}</h4>
          <p className="muted">
            {site.bot_id}/{site.site} · Draft revision {site.revision} ·{" "}
            {site.published_revision
              ? `Published revision ${site.published_revision}`
              : "Private draft only"}
          </p>
          <Badge tone={site.local_ready ? "green" : "neutral"}>
            {site.local_ready ? "Published locally" : "Not published"}
          </Badge>{" "}
          <Badge tone="neutral">
            {site.sync
              ? `Remote ${site.sync.status} · delivery disabled`
              : "Remote not queued"}
          </Badge>
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
          {site.sync?.target_url && (
            <p className="muted">
              Planned remote URL (not delivered): {site.sync.target_url}
            </p>
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
      ))}
    </section>
  );
}
