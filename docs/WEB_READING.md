# Complete public-document reading

`web_fetch` is a keyless capability under the normal global plugin enablement and per-bot grant checks. It retains the complete extracted text of an accepted public response, returns a bounded first chunk, and supplies a document ID for later reads and search. A snapshot belongs to one bot and one channel. Knowing an ID never grants another bot or channel access. An authorized later turn in the same scope can continue reading the snapshot.

The existing URL-only call remains valid:

```json
{"url":"https://example.com/article"}
```

As for every [tool](TOOLS.md), `{}` returns the full usage without invoking a handler or resolving credentials. Parseable invalid calls return all detectable schema errors and usage together. Unknown fields, fields for another operation, incorrect types and bounds are rejected before fetching or reading. Argument order has no significance.

## Operations and continuation

| Operation | Required fields | Optional fields | Result |
| --- | --- | --- | --- |
| `fetch` (also the default when `operation` is absent) | `url` | `length` | A new immutable text snapshot and its first chunk. |
| `read` | `document_id` | `offset`, `length` | A bounded range from the saved snapshot; no network request. |
| `search` | `document_id`, `query` | `offset`, `limit`, `context_chars`, `case_sensitive` | Literal matches with bounded context and stable match/excerpt offsets. |
| `refetch` | `document_id` | `length` | A new snapshot from the original requested URL; returns `replaces_document_id`. |
| `start` | none | none | Signals an explicit extended reading task; performs no download. The engine owns the bounded, non-renewable extension. |
| `read_result` | `result_id` | `offset`, `length` | Registry-managed retrieval of retained tool-result evidence after its body has left the active prompt. Scope and the source tool's current grant are checked again. |

Example calls use illustrative IDs; replace them with actual returned handles:

```json
{"operation":"start"}
{"operation":"fetch","url":"https://example.com/article","length":12000}
{"operation":"read","document_id":"fetch_0123456789abcdef0123","offset":12000,"length":12000}
{"operation":"search","document_id":"fetch_0123456789abcdef0123","query":"technical details","context_chars":180,"limit":5}
{"operation":"refetch","document_id":"fetch_0123456789abcdef0123"}
```

Each read/fetch result includes `document_id`, final `url`, `content_type`, decoded `encoding`, ISO UTC `fetched_at` and `expires_at`, `content_sha256`, `total_chars`, `downloaded_bytes`, `stored_bytes`, `range`, `next`, `text`, `truncated`, applicable `limits` and an explicit untrusted-content label. `_working_set` contains a compact reread reference for the engine. The content hash is SHA-256 of the saved extracted, redacted UTF-8 text; it is not a hash of the HTTP response bytes.

Offsets count **Python Unicode characters**, starting at zero, with an exclusive end. They are not UTF-8 bytes, JavaScript UTF-16 units, graphemes or model tokens. A combining sequence can span adjacent chunks without losing either code point. Text-source newlines are retained. HTML follows the existing text extractor, including block separators and exclusion of script/style/noscript content; the snapshot is the complete extracted text, not original HTML markup.

Pass the returned `next` object directly to `web_fetch` to continue. The next offset equals the previous range's exclusive end. The final result has `next: null` and `truncated: false`. Reading at exactly `total_chars` returns an empty range at the end and no continuation. A larger offset is an error. Returned chunks can be shorter than requested to remain below the registry's serialized-result cap; the returned range and next offset remain authoritative.

Search is literal rather than a model-supplied regular expression. It is case-insensitive by default using Python's Unicode regular-expression matching, without Unicode normalization or multi-character case-fold expansion. `case_sensitive: true` selects exact matching. Query length is 1–512 characters, `limit` is 1–10 matches (default 5), and `context_chars` is 0–500 on either side (default 120). Combined excerpts stay within the configured chunk budget. A query longer than that budget still returns its exact match offsets and a shorter excerpt. Search continuation starts after the last returned match; contextual excerpts may overlap deliberately. Read pagination has no overlapping ranges.

Calling `fetch` again creates another snapshot. Reading or searching an existing ID never redownloads the URL. Explicit `refetch` creates a different ID and may yield changed content; it does not overwrite an unexpired earlier snapshot. Recently expired IDs retain enough metadata for refetch. Missing or older removed IDs require a fresh fetch with the original URL.

## Limits and storage

Configuration uses the usual shallow merge: built-in defaults, then global `web_fetch` configuration, then this bot's `plugin_config.web_fetch`. Each field is an integer; there is no string/boolean coercion. The shared validation helper checks complete merged configuration.

| Configuration field | Default | Supported range | Meaning |
| --- | --- | --- | --- |
| `max_download_bytes` | 1,000,000 | 1,024–1,000,000 | Maximum response bytes consumed by the existing bounded HTTP reader. Pagination does not raise it. |
| `chunk_chars` | 18,000 | 256–18,000 | Maximum extracted characters returned in a read or combined search excerpts. A call's `length` may be smaller, down to 1. |
| `storage_quota_bytes` | 50,000,000 | 1,024–500,000,000 | Retained UTF-8 snapshot bytes per bot, summed across channels. |
| `retention_seconds` | 604,800 (7 days) | 60–31,536,000 | Retention assigned to a new snapshot when fetched. |

Download bytes, extracted characters, stored UTF-8 bytes and provider tokens are different measures. Declared text encodings are decoded; UTF-8 is the default when absent. Invalid byte sequences use replacement characters, as the prior fetcher did. Changing a retention setting does not rewrite existing expiry timestamps. Lowering a quota does not evict existing files; new snapshots fail until sufficient space is available. Errors report the applicable limit and an action such as using a smaller source, continuing an existing handle, waiting for unused snapshots to expire, or asking the operator to adjust storage.

There are at most 1,000 retained ready snapshots and 100 recently expired metadata records per bot. Expiry cleanup runs before a new fetch and is available through `FetchedDocuments.cleanup()` for runtime housekeeping. Expired snapshots immediately become unreadable unless a running turn already references them. Such active references pin their text until the turn finishes or is marked interrupted. Cleanup removes expired, unreferenced snapshot files and retains bounded metadata for explicit refetch. Authenticated dashboard inspection reports expiry without mutating files or extending retention.

Snapshot metadata and active reference records live in SQLite tables `fetched_documents` and `fetched_document_references`. Text blobs live outside Git in `$HORTATOR_DATA_DIR/fetched_documents/`, under hashed bot/channel directories, with owner-only permissions. Generated snapshot IDs select files; a model cannot choose a host path. Writes are atomic and fsynced. Reads reject symbolic links/non-regular files, verify the recorded size and hash, and remain bounded if a file changes during reading. Missing/corrupt blobs give a restore/refetch error. Orphan files from an interrupted write still consume quota and are preserved for operator inspection instead of being silently removed.

Include the entire `fetched_documents/` directory and the matching SQLite database in backups. Stop or pause work for a consistent file/database snapshot, restore into an empty data directory with the matching master key and normal owner-only permissions, and retain the existing runtime recovery behavior for unfinished turns. A redacted configuration export does not contain fetched text or its ownership metadata. See [operations](OPERATIONS.md) for the full backup/restore procedure.

## Network and content boundaries

The module reuses `public_url`, `PublicResolver` and `fetch_public`: HTTP(S) on ports 80/443, no URL userinfo, public-address validation of actual socket DNS answers, validation of every redirect, a finite redirect loop, a 25-second request deadline and the configured byte cap. The HTTP session does not trust proxy environment settings, has no persistent cookies and sends no provider/dashboard authorization header. Fetch URLs containing known vault secret values are rejected. The capability never uses its registry credential argument.

Only declared text/JSON/XML content is accepted. Binary images, PDF and responses without a usable content type explain the workspace attachment import operation and its observed message/attachment IDs. Binary control bytes hidden behind a text MIME type also fail explicitly. Fetching does not pretend a binary image was inspected, change automatic image intake, or allow model-invented attachment CDN URLs.

Known credentials are redacted before text persistence, hash calculation and offset assignment. Normal registry and event-ledger redaction still applies to subsequent output. All downloaded text remains explicitly untrusted source content; instructions inside a page do not grant tools, change a bot's identity or authorize administration. Retained files are private until a separately authorized workflow exports them; fetching never publishes a document or queues remote site sync.

Pagination does not itself bound the model's accumulated tool context. The engine separately retains complete tool evidence and replaces older large active result bodies with explicit references, including reread instructions. It does not rewrite historical requests or invoke an unmetered helper model. See [agentic tools](AGENTIC_TOOLS.md) and [tool budgets](TOOLS.md) for working-set and extended-budget behavior.

## Verification scope

The focused tests in `tests/test_fetched_documents.py` use isolated SQLite/filesystem storage and synthetic HTTP responses. They traverse an HTML fixture just below the 1 MB bound, recover a marker beyond character 18,000, preserve every Unicode character across restart and pagination without refetching, check exact EOF behavior and bounded search, exercise bot/channel authorization and grant denial, preserve active references during expiry, enforce quotas including orphan files, reject unsafe/binary input and verify complete usage/error feedback. The real HTTP helper is exercised with a mocked session to check redirect rejection, socket-resolver selection, deadlines, byte limits and disabled ambient credentials. These checks do not establish live website, Discord or paid-provider acceptance; the project-wide dated results are recorded in [verification](VERIFICATION.md).
