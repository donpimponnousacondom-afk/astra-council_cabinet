# Local document and site publishing

`document_site` is one optional plugin pack for each bot. Enable the plugin globally, then grant it to the desired bots in their tools configuration. It needs no API key. Its operations create HTML, text, scripts, styles, images and other static assets without a shell, package installer or arbitrary filesystem access.

A successful `start` opens the bot's configured extended document-task budget once for that turn. This applies whenever an enabled, granted bot starts a document task, regardless of who requested it or whether it initiated the task itself. It does not grant additional capabilities, bypass the ordinary provider/daily limits, or renew itself through repeated `start` calls. Read [TOOLS.md](TOOLS.md) for empty-argument usage discovery and complete argument error reports; these are deliberate compatibility features for models, not accidental parser behavior.

## Model workflow

Call `document_site` with `{}` to obtain usage, all field types and conditional requirements. Arguments are named JSON fields; there is no positional order. A typical sequence is:

```json
{"operation":"start","site":"conversation-summary","title":"Council conversation summary"}
{"operation":"write","site":"conversation-summary","path":"index.html","content":"<!doctype html><html><head><title>Summary</title></head><body><h1>Council summary</h1><p>...</p></body></html>"}
{"operation":"write","site":"conversation-summary","path":"assets/style.css","content":"body { font: 18px system-ui; max-width: 70ch; margin: 3rem auto; }"}
{"operation":"publish","site":"conversation-summary"}
```

`start` also resumes an existing site owned by this bot. `write` replaces an entire file and retains the preceding immutable revision; it does not append a patch. Use `read` before editing a file if its current content is needed. The model should finish the work before calling `publish` and then use its normal Discord response to report the resulting URL and delivery state.

| Operation | Required fields beyond `operation` | Result |
| --- | --- | --- |
| `start` | `site` | Create or resume a private draft; optional `title`; start the extended task budget |
| `list` | none | This bot's sites and their publication/sync state |
| `read` | `site`, `path` | UTF-8 text up to 40,000 characters, or binary metadata |
| `write` | `site`, `path`, `content` | Save text (`encoding: "utf-8"`, default) or strict base64 (`encoding: "base64"`) |
| `import_artifact` | `site`, `path`, `artifact_id` | Copy generated media belonging to this bot and current turn |
| `import_attachment` | `site`, `path`, `message_id`, `attachment_id` | Copy an attachment observed in the current channel |
| `publish` | `site` | Publish an immutable local snapshot and queue that revision for later remote sync |
| `status` | `site` | Current draft revision, published revision, files, URLs and sync state |

Attachment import accepts IDs, never a model-supplied download URL. The source must exist in the current channel's nondeleted stored messages. Only the recorded Discord CDN URL is fetched, with a pinned public DNS resolver, no redirects, no ambient proxy credentials, a 25-second deadline and an 8 MB limit. An expired URL produces an explicit request to upload again. Imported files and generated artifacts become this bot's site assets; arbitrary host files, other channels and other bots' artifacts remain inaccessible.

## Local versus remote publication

Draft writes are private. `publish` makes the selected complete revision readable at `/sites/<bot-id>/<site>/<path>` on the local server, without dashboard authentication. On this workstation the default local base URL is `http://127.0.0.1:8000`. These local published files are accessible to anyone who can reach that HTTP server; do not expose the dashboard server to an untrusted network to publish sites remotely.

The returned local URL opens `index.html` when the published revision contains it; a document-only publication instead links directly to its first published file (also reported as `published_entrypoint`). Draft changes do not alter that public entrypoint. Site listings apply the configured global/per-bot base URLs. Every file is checked for integrity before publication can claim local readiness or queue a sync job.

Published HTML runs under a restrictive CSP sandbox without same-origin privileges. It cannot acquire dashboard cookies or call the administrative API. Local static assets and scripts are confined to this site's published path; there is no shell or server-side code execution. Draft downloads remain authenticated and use attachment disposition. A write after publication changes only the draft; readers continue seeing the previous published snapshot until the next `publish`.

Remote delivery is **disabled** in this implementation. The plugin's optional `public_base_url` records an intended destination, such as `http://council.zombiedawn.net`; resulting planned URLs have the shape `http://council.zombiedawn.net/hortator/conversation-summary/`. This is destination metadata, not evidence of a configured server or successful upload. No setting, including a manually added `remote_enabled: true`, starts an upload worker. SSH credentials, host key policy, remote directory, TLS and SFTP/SCP transport are a later configuration task.

Each local publication atomically records an immutable revision and a durable `queued` sync job. A newer publication supersedes the older queued job for that site. Repeating `publish` without edits is idempotent for its queue entry. Draft edits do not publish or queue an incomplete revision. Queue entries survive runtime restarts and branch changes; no request must remain running to remember pending work.

Bots must say **“Published locally; remote sync is queued/unconfigured”**, followed by the returned local URL. They must not say a remote domain is live merely because a planned URL exists. Once a transport is implemented, it must copy the recorded immutable revision and acknowledge verified success before changing a job to remotely delivered. This separation allows a future proactive sync worker to operate independently of model reliability.

## Storage, limits and recovery

Metadata lives in `council.sqlite3`: `document_sites`, `document_revisions`, and `document_sync_queue`. File bytes live under `$HORTATOR_DATA_DIR/sites/<hash-of-bot-id>/<sha256-of-bytes>`, outside Git. These files are immutable blobs, not a directly served directory. HTTP serving resolves only the selected published manifest. Files are 0600 and directories 0700; symbolic links and path traversal are rejected. No bot can ask this plugin to read a host path or credential file. Known credential strings are redacted from UTF-8 content, including base64 text and text imports; this is not a general detector for every secret that might appear in user-provided media.

Writes fsync the blob before committing the revision in SQLite. A crash before the metadata commit may leave an unreferenced blob, but cannot advance the manifest to partially written data. Published manifests and sync queue changes commit together. Blob checksums and sizes are verified before reads. Do not edit blob contents manually.

Limits are 100 sites per bot, 100 files and 50 MB per current site, 8 MB per file, and 250 MB of stored bytes per bot including retained revisions. Text/base64 tool arguments are additionally bounded to 2,000,000 characters. Static filename extensions are explicitly allowed; dotfiles, traversal, server programs such as PHP, shell scripts, and arbitrary executable uploads are rejected. Archives and binary assets are stored as bytes, never unpacked or executed. Revision history intentionally retains older bytes; operator-managed retention/garbage collection is a future task, so monitor disk use.

Backups must include both the SQLite database and `sites/`, alongside the other documented data directories. For a consistent archive while bots could be publishing, pause the council and stop the foreground runtime before backing up. Restoring just the database can leave manifests referencing missing blobs. See [OPERATIONS.md](OPERATIONS.md) for backup procedures and [VERIFICATION.md](VERIFICATION.md) for dated evidence. Local tests do not demonstrate remote delivery, DNS readiness or a live Discord publication.
