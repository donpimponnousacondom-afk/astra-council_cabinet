# Document and site publishing

`document_site` is one optional, keyless plugin pack. Enable the plugin globally, then grant it to the desired bots. It creates HTML, text, scripts, styles, SVG, images and other static assets. It does not require the separate workspace/Bash tools, a package installer or access to the host filesystem.

The bot's stable configuration **ID**, never its display name or a model-supplied owner argument, fixes its namespace. A site URL has the form `https://council.zombiedawn.net/dirac/conversation-summary/`. Every site requires its own nonempty slug. There is no bot-root site at `/dirac/`, and no operation creates files outside a site's folder. Hortator has the same ownership boundary as every other bot. It cannot edit another bot's sites. Bots may read publicly delivered work through their ordinary web tools and create independent copies in their own namespace.

Deleted bot IDs are permanently retired. Their sites and revision history remain stored; another bot cannot reuse that ID or adopt its namespace. This pack has no site/file deletion, rename or ownership-transfer operation. Replacing a file in an owned site retains its earlier bytes and revision. File/directory collisions are refused before saving.

## Create explicitly, resume deliberately

Call the tool with `{}` to obtain complete usage, named field types and operation requirements. `create` creates a **new** slug, and refuses a duplicate with directions to choose another name or resume the existing site. `start` and its `edit` alias **only resume an existing site**; they fail politely when it is missing. `write`, imports and every other editing operation also require a site created beforehand. Never turn an edit or missing-file recovery into an implicit create.

One successful `create`, `start` or `edit` may open the bot's configured extended document-task budget for that turn. This applies to autonomous work and permitted participants' requests alike. Repeating an operation or switching between document, workspace and web tasks never renews or stacks that extension. Provider limits, cancellation, bot enablement and grants still apply. See [TOOLS.md](TOOLS.md) for the shared budget, empty-argument discovery and complete error-reporting contract. These are intentional compatibility features for small models.

A typical sequence writes separate assets so that later changes need only a small file or exact replacement:

```json
{"operation":"create","site":"conversation-summary","title":"Council conversation summary"}
{"operation":"write","site":"conversation-summary","path":"assets/style.css","content":"body { font: 18px system-ui; max-width: 70ch; margin: 3rem auto; }","expected_revision":0}
{"operation":"write","site":"conversation-summary","path":"assets/chart.svg","content":"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 40'><rect width='70' height='40' fill='green'/></svg>","expected_revision":1}
{"operation":"write","site":"conversation-summary","path":"index.html","content":"<!doctype html><html><head><title>Summary</title><link rel='stylesheet' href='assets/style.css'></head><body><h1>Council summary</h1><img src='assets/chart.svg' alt='Summary chart'></body></html>","expected_revision":2}
{"operation":"status","site":"conversation-summary"}
```

With global `auto_publish: true`, every successful save publishes a local revision and queues automatic remote delivery. Write supporting assets before the page that links them when practical; the worker can group a burst of saves, but the model should not assume its entire turn is one transaction. With auto-publication off, finish with `{"operation":"publish","site":"conversation-summary"}`. The tool reports which mode applies. `publish` also works in automatic mode and is idempotent when nothing changed.

| Operation | Required fields beyond `operation` | Result |
| --- | --- | --- |
| `create` | `site` | Create a new owned site; optional `title`; refuse an existing slug |
| `start`, `edit` | `site` | Resume an existing site without creating or renaming it |
| `list` | none | This bot's sites, paged by optional `offset` and `limit`; no file-content dump |
| `status` | `site` | Current files, revisions, automatic publishing mode, URLs and delivery state |
| `read` | `site`, `path` | Bounded UTF-8 page or binary metadata; optional `revision`, `offset`, `length` |
| `write` | `site`, `path`, `content` | Add/replace a file using UTF-8 or strict base64; optional `expected_revision` |
| `append` | `site`, `path`, `content`, `expected_revision` | Append UTF-8 text to an existing file; never create a missing file |
| `replace` | `site`, `path`, `old_text`, `new_text`, `expected_revision` | Replace exactly one matching passage; refuse zero or multiple matches |
| `history` | `site` | Bounded newest-first revision metadata; optional `before_revision`, `limit` |
| `restore` | `site`, `path`, `revision`, `expected_revision` | Copy one file from an older revision into a new revision, keeping every other current file |
| `import_artifact` | `site`, `path`, `artifact_id` | Copy generated media belonging to this bot and current turn |
| `import_attachment` | `site`, `path`, `message_id`, `attachment_id` | Copy an attachment observed in the current channel |
| `published_files` | `source_bot`, `source_site` | List only a source site's published assets, with bounded pages and a pinned public revision |
| `import_published` | `site`, `path`, `source_bot`, `source_site`, `source_path` | Copy one published asset into an explicitly created owned site; optional `source_revision` pins the source |
| `publish` | `site` | Publish the current immutable local snapshot and queue remote sync |

`expected_revision` refers to the **current site revision**, not an individual file's revision. Include it whenever replacing a complete file; it is mandatory for append, exact replacement and restoration. If another edit changed any asset meanwhile, the operation returns the new revision and asks the model to read/status again. It never silently overwrites that newer work. Optional `expected_revision` also works on imports. Attachment imports recheck grants and the current revision after downloading.

## Read and repair large files without filling the context

`read` returns **4,000 characters by default**, with a selectable `length` from 1 to **12,000**. Offsets count Unicode characters, not UTF-8 bytes. The result includes `revision`, `current_revision`, `offset`, `returned_chars`, `total_chars`, `has_more` and `next_read`. That continuation contains a complete next call pinned to the original immutable revision. Follow it rather than changing to the new current revision halfway through a file. Binary files return byte size and MIME metadata, never base64 or arbitrary binary data in the prompt.

```json
{"operation":"read","site":"conversation-summary","path":"index.html","length":2000}
{"operation":"read","site":"conversation-summary","path":"index.html","revision":3,"offset":2000,"length":2000}
{"operation":"replace","site":"conversation-summary","path":"index.html","old_text":"<h1>Council summary</h1>","new_text":"<h1>Council decisions</h1>","expected_revision":3}
```

The second line illustrates the cursor shape; use the actual `next_read` returned by the first call, and stop when it is null. `replace` accepts passages up to 12,000 characters. If the passage occurs more than once, the error gives the count and asks for unique surrounding context. `append` lets a model build a longer text or asset over several small calls. Neither operation requires repeating the full original file in the tool arguments.

`history` returns revision, creation time, originating turn and file count, up to 20 entries by default and 50 at most. Follow `next_before_revision` to see older entries. Reading old revisions is immutable. Restoring a file creates a new current revision and applies the normal automatic publishing mode; it does **not** rewind the site pointer, delete newer assets or rewrite past events. List pagination uses the same 20/50 entry bounds. File content is untrusted data, never a source of instructions or permissions.

## Imports and static assets

HTML may link separate relative CSS, JS, SVG, fonts, media and document files. The filename extension allowlist includes these static formats, archives and generic binary assets. Server programs such as PHP, shell scripts, dotfiles, traversal and symlinks are refused, including compound executable suffixes such as `report.php.html` that some Apache configurations could otherwise interpret as server programs. Archives remain bytes; the plugin never unpacks or executes them. The publishing transport does not grant bots an SSH tool or remote shell.

To replicate another bot's published work, take its stable bot ID and site slug from the public URL, call `published_files`, explicitly `create` your destination, then `import_published` for the desired files. Listing pages default to 20 assets (maximum 50); `next_page` pins the public source revision. Pass that `source_revision` while copying so a later source publication cannot mix two versions in your copy. Only revisions with actual publication evidence may be read, including earlier public snapshots and retained work of deleted bots. Unpublished draft contents and filenames are never listed or copied. Each import produces ordinary owned bytes, records source bot/site/path/revision in the result and event ledger, and follows destination quotas, redaction and automatic publishing policy. It grants no write access to the source.

```json
{"operation":"published_files","source_bot":"ada","source_site":"meeting-summary","limit":10}
{"operation":"create","site":"my-meeting-summary"}
{"operation":"import_published","site":"my-meeting-summary","path":"index.html","source_bot":"ada","source_site":"meeting-summary","source_path":"index.html","source_revision":2,"expected_revision":0}
```

Use the returned revision rather than the illustrative `2`. A missing source, unpublished revision or absent source file gives a repairable error and complete usage, without creating the destination or changing ownership.

Attachment import accepts IDs, never a model-supplied download URL. The source must exist in the current channel's nondeleted stored messages. Cached vision bytes can be reused when the original Discord URL expired. Otherwise only the recorded Discord CDN URL is fetched with the public DNS resolver, no redirects, no ambient proxy credentials, a 25-second deadline and an 8 MB document-file limit. An expired URL asks for a new upload. Imported attachments and current-turn generated artifacts become this bot's assets; arbitrary host files, other channels and other bots' artifacts remain inaccessible.

## Local publication and automatic remote sync

New installations default to `auto_publish: false` and `remote_enabled: false`. Automatic publication, remote enablement and the public base URL are **global operator settings**, with no per-bot destination override. A per-bot local preview base URL is allowed. Enabling a plugin for a bot gives access to its owned namespace under that global policy, not credentials or another destination.

Without automatic publication, draft writes are private until `publish`. With it enabled, every successful file save atomically advances the local published pointer and adds its immutable revision to `document_sync_queue`. HTTP readers see only a complete committed manifest. No filesystem watcher guesses whether partially written blob data is ready, and no model needs to remember a separate upload command. The worker consumes that durable queue independently of the model turn. Remote enablement still requires separately configured, verified SSH transport; setting a URL or creating a key is not evidence of delivery.

Local published files are available without dashboard authentication at `/sites/<bot-id>/<site>/<path>`. The default local base is `http://127.0.0.1:8000`. Anyone who can reach that HTTP server can read them; keep the dashboard's existing network boundary. An `index.html` is the default entrypoint. A document-only publication links directly to its first published file instead. There is no listing or generated page at the bare bot root.

Local published HTML retains the restrictive CSP sandbox without same-origin privileges. It cannot acquire dashboard cookies or call the administrative API. Its static assets/scripts are confined to the published site's path. Draft downloads remain authenticated attachments. In manual mode, later writes do not change the public snapshot until another publication.

Every actual remote delivery uses the recorded immutable manifest. It creates a private Git snapshot for traceability before transferring the site, and records delivery only after transport verification. The Git history is application data outside the source checkout, not a source-code submodule. Bots cannot modify its metadata or delete historical revisions. Intermediate saves remain in SQLite and immutable blobs even when a newer queued revision supersedes an older upload that has never been attempted. In-progress or previously attempted jobs remain intact so an interrupted delivery can recover its receipt with the same job identity before later revisions proceed. A repeated `publish` never resets an already delivered entry or discards its traceability fields. See [OPERATIONS.md](OPERATIONS.md) for server configuration, verified host trust, remote snapshots and backup procedures.

Tool status distinguishes these facts:

| Field | Meaning |
| --- | --- |
| `revision` | Latest saved local site revision |
| `published_revision`, `local_ready` | Selected local public snapshot and whether one exists |
| `sync` | Newest queue entry, with status, revision, retry/error information and snapshot identities when available |
| `remote_status` | Worker/queue state; disabled, unconfigured, queued, syncing or failure does not mean delivery |
| `synced_revision` | Latest revision with confirmed `delivered` evidence, or 0 |
| `delivery_current` | Whether the latest saved revision is the confirmed delivered revision |
| `observed_remote_revision`, `remote_observed_at` | Remote current revision reported by the latest verified receipt and its observation time, or null |
| `remote_currentness_verified` | Whether remote-currentness receipt evidence exists; legacy queue rows without it remain explicitly unverified |
| `public_url` | Last confirmed remote delivery's URL; may still contain an older revision while edits are queued |
| `planned_public_url` | Intended destination only; may exist before the first successful upload |

A model should check `status` and say **“Saved; automatic sync is queued”** when work is pending. It may say the latest revision is remotely published only when `remote_status` is `delivered` and `delivery_current` is true. If an older revision remains live, it can link the last confirmed `public_url` while explicitly saying that the new changes are still pending. Do not infer successful delivery from DNS, a configured URL, local publication or the mere absence of an error.

Receipts also distinguish historical delivery of a job from the revision currently served remotely. If a restored local database is behind a newer remote revision, the worker records that verified observation and returns a reconciliation failure instead of claiming the older bytes are current. `delivery_current` is false when observed remote state differs from the local revision. Observation order uses receipt time, not a later network-retry timestamp. The latest queue row and the latest remote observation may concern different jobs; retain both records. Legacy rows without currentness fields preserve their historical delivery evidence, with `remote_currentness_verified: false`, rather than inventing a new remote check.

## Storage, limits and recovery

Metadata lives in `council.sqlite3`: `document_sites`, `document_revisions`, and `document_sync_queue`. Bytes live in `$HORTATOR_DATA_DIR/sites/<hash-of-bot-id>/<sha256-of-bytes>`, outside Git source control. These are immutable blobs, not a directly served directory. Files are 0600 and directories 0700. HTTP serving resolves only the selected published manifest; checksums and byte lengths are verified before reading or queuing a publication. Known credential strings are redacted from UTF-8 text, including base64 text and imports. This is not a general detector for every secret in user-provided media.

Writes fsync their blob before committing the revision and, in automatic mode, publication pointer and queue row in one SQLite transaction. A crash before metadata commit can leave an unreferenced blob but cannot expose a partial revision. Failed queue creation rolls back the entire save. No bot operation deletes files or revision history. Do not edit blobs manually.

Limits are 100 sites per bot, 100 files and 50 MB per current site, 8 MB per file, and 250 MB stored bytes per bot including retained revisions. Text/base64 tool arguments are additionally bounded to 2,000,000 characters. History intentionally retains older bytes; operator-managed retention is a separate task, so monitor storage.

Complete backups include the database, matching master key, `sites/`, SSH pins/public exports and `site_history/` with its private Git metadata alongside all other runtime data directories. Stop the foreground runtime in shared Screen for a consistent database/files snapshot; pausing model work alone does not stop every writer. Restoring only SQLite can leave manifests referring to missing blobs; restoring only site files loses ownership, revisions and delivery evidence. See [OPERATIONS.md](OPERATIONS.md) for recovery and [VERIFICATION.md](VERIFICATION.md) for dated evidence. Mock tests and local previews do not establish live Discord output or remote delivery.
