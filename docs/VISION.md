# Discord attachment vision

Bots with **Receive image inputs** enabled receive actual image pixels through the same context and provider path. This is independent of the model name and does not require a vision plugin. A provider/model that rejects image input produces its ordinary visible upstream request failure; Hortator does not silently retry without the images. A provider could also accept a request without using its images, so HTTP success alone is not evidence of visual understanding.

## Per-bot image inputs

**Bots → edit bot → Capabilities → Receive image inputs** controls `allow_images` (default true, including older records). Off disables automatic image capture for this bot's gateway observations and context preparation, and excludes pixels from every model request. Cached images belonging to shared channel history do not bypass the setting. Text, attachment metadata and other participants' written descriptions remain available, with `pixels_in_this_request:false` and explicit guidance that the operator disabled visual input. This is an intentional configuration choice, not an image-processing failure. Normal turn checkpoints still advance after a sent/silent turn, so re-enabling does not revive already-handled images.

The provider serialization boundary also rejects unexpected structured image inputs for a disabled bot. Configuration saves cancel affected active turns through the existing lifecycle. The setting applies to the shared context path used by ordinary and slash turns; slash prompts currently carry text, not attachment inputs. This switch does not disable file imports, image generation or sending files, which are separate plugin capabilities, and it does not delete the shared image cache or Discord uploads.

Bots may share one provider/model profile while only some receive images. To give two image-enabled bots different count/byte limits (for example one versus two images), clone their model profile and adjust its budgets; both profiles may use the exact same provider/model slug. Keep model profile image counts positive. Neither the per-bot switch nor the profile budgets claim that an upstream endpoint actually supports vision.

## Capture and durable storage

After the existing room and exact-owner authorization checks, the Discord gateway downloads supported image attachments from their Discord CDN URLs. Original text-shaped attachment metadata is retained. The `vision` metadata records either `ready` with the SHA-256, detected media type, dimensions and byte count, or `unavailable` with an explanation.

Image bytes live in `$HORTATOR_DATA_DIR/images/<sha256>`, outside the checkout, with owner-only permissions. Atomic writes fsync image bytes and the containing directory before a ready reference is recorded. Cached content survives the signed CDN URL expiring and branch changes. Existing cached bytes are integrity checked; authenticated Discord history observations can supply a refreshed URL to recover a missing cache. New unhandled transcript rows without vision metadata are downloaded lazily during preparation. Unhandled attachments previously rejected by a smaller byte limit are retried once when the current limit admits their declared size, including legacy 8 MiB rejections. The refreshed result is persisted, so an expired URL or another failure does not cause a retry loop. Already-compacted summaries are not rewritten. This preparation has a 60-second overall deadline; completed downloads are retained when a retry is needed. Failed capture does not masquerade as visual access: the model sees **PIXELS UNAVAILABLE** and the reason. Reattaching the file provides a fresh download opportunity.

Only HTTPS URLs on `cdn.discordapp.com` or `media.discordapp.net` with Discord attachment paths are fetched. No arbitrary hosts, credentials in URLs, alternate ports, redirects, or environment proxy settings are used. Responses must advertise a supported raster image type or generic binary type, and Pillow validates the actual container/pixels. PNG, JPEG, WebP and GIF are supported. Images within the pixel limit preserve their original bytes. Oversized JPEGs follow the disclosed resize policy below; animation support depends on the model/provider, and local validation decodes only the first frame.

Limits are 20 MiB per file (20,971,520 bytes), 64 megapixels, ten image captures per Discord message, and 15 seconds per download. Oversized, unsupported or invalid attachments retain explicit failure metadata. The image cache is private application storage, not a public static mount. Publishing an attachment is a separate scoped plugin action; merely uploading it to Discord does not publish the local cache.

## 2026-09-15: 64 MP and disclosed JPEG conversion

The owner raised the pixel limit from 20 to **64,000,000 pixels** and explicitly
authorized proportional resizing of JPEG/JPG images above it. Lower JPEG quality
alone cannot change megapixels. Admitted files at/below 64 MP remain byte-for-byte
unchanged, including the 8160 × 6120 phone photo that prompted this change.

For oversized JPEGs, decode their header and use JPEG's native decoder subsampling
before loading pixels where possible. The intermediate decoder raster is bounded
to four times the target pixel limit (256 MP); unsupported/unsafe/truncated sources
remain explicitly unavailable. Pillow's process-wide limits are not disabled.
Resize with preserved proportions, integer dimensions within 64 MP, and apply EXIF
orientation. Baseline/progressive, grayscale and CMYK JPEG inputs are supported;
converted output is RGB JPEG. Recognize `image/jpeg`, `image/jpg` and `image/pjpeg`
response types while validating actual file bytes. Pixel preparation runs off the
event loop with no database or file mutation in that worker.

The resized JPEG starts at quality 90. If necessary, bounded quality attempts
85/80/70/60/50/40/30/20/10 fit the existing 20 MiB encoded-file ceiling without
changing dimensions again. A result that still cannot fit remains unavailable.
The original download must itself fit 20 MiB; this feature does not expand upload
or request-byte budgets, accept every possible JPEG encoding, or convert oversized
PNG/WebP/GIF files. Profile byte accounting uses the actual cached replacement.

Only the final converted bytes enter the atomic image cache. Original downloaded
bytes are discarded from the conversion path; source dimensions, size and SHA-256
remain in `vision.transformation`. Existing cached files, Discord attachments and
manually exported inspection copies are not purged. `vision.warning` explicitly
states original/result dimensions and JPEG quality. Context carries **IMAGE RESIZED**
and directs the bot to disclose reduced detail when discussing that image, or
**PIXELS UNAVAILABLE** when capture failed. This does not create an unsolicited
Discord send or repeat warnings in unrelated answers. Workspace imports report
`original_preserved: false` and propagate conversion evidence for resized copies.

Reconnect/history and attachment-edit observations reuse both ready and rejected
outcomes for the same source. A new signature alone cannot retry a permanent
pixel/format rejection. Actual transport failures may retry after a refreshed
authenticated URL; changed attachment identity/metadata gets a new capture.
Historical 20 MP rejections receive one retry under the raised limit, following
the existing smaller-byte-limit recovery rule. `attachment.image_resized` and
`attachment.image_unavailable` warnings are emitted only when the outcome changes.
Old handled images remain metadata-only until reattached; this does not reset a
bot's context, revive old pixels or alter compaction.

The decoder/quality behavior follows [Pillow's JPEG documentation](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg).

## Model requests and observability

Generation contains selected structured image parts paired with source Discord message and attachment IDs. Compaction sends text only: the conversation's attributed written observations, summaries and attachment metadata. It does not inspect the image again or retain private provider reasoning as memory. Cached bytes and transcript remain stored locally; above-limit JPEGs retain the converted copy described above. Deleted messages supply neither pixels nor attachment metadata to future contexts. Explicit attachment-removal edits also remove their image parts; embed-only edits retain the attachments.

The assembled request uses internal durable image references. Immediately before the HTTP request, the provider adapter loads the bytes, checks their hash and converts each reference into an OpenAI-compatible `image_url` part containing a base64 data URI. The request ledger keeps the durable reference, not megabytes of base64. A missing/corrupt reference fails the request explicitly and does not open a provider-wide failure circuit.

A wire request defaults to ten images and 40 MiB of combined cached image bytes; its model profile can override both budgets. Select new inputs within both budgets before context planning. Image excess does not trigger compaction. Tool follow-up requests keep the same current-turn selection; text compaction does not discard that selection before generation. No capability-name registry or model-name heuristic suppresses image input.

Context planning adds a **4,096-token reserve per image** to the existing approximate text estimate. This is a planning allowance, not measured provider image tokens or a guarantee for every tokenizer/resolution. The usual calibration against reported input usage remains active; actual provider usage/cost continues to come from response usage fields. Request/context metadata states the image count and estimator explicitly.

Back up `images/` with the database and other external data. The database's image references are insufficient to restore the pixels after signed links expire. Retention is deliberate: monitor disk space and follow the normal backup procedure rather than deleting cache files while references are in use.

## 2026-09-08: larger Discord image attachments

The owner requested support up to Discord's current standard per-file limit, documented as [20 MiB by the API](https://docs.discord.com/developers/reference#uploading-files). Image intake, cached-file reads and provider expansion all admit that size; the combined request budget is 40 MiB before base64 encoding. The existing 20-megapixel decoded limit and eight-image count still apply. This is an encoded-file size change; original pixels are preserved without recompression or resizing. Provider-specific acceptance is still established by its response.

At the image-ceiling patch stage, `web_fetch` was a bounded text extractor and shell/filesystem tools were deferred to the next feature. [Private workspaces, isolated Bash and complete web reading](AGENTIC_TOOLS.md) now implement that workflow. Document imports and outbound artifacts retain their separate limits. The September 15 policy above supersedes the original 20 MP ceiling and permits disclosed oversized-JPEG resizing.

## Per-model request budgets (2026-09-12)

Per-request defaults are ten images / 40 MiB, not fixed ceilings. The owner subsequently requested ten images on every saved profile after testing larger budgets; this supersedes the earlier eight-image default and 256-image live configuration. The count field has no fixed schema maximum, so an operator can raise it for a future workload without a code change. The separate byte budget and upstream limits still apply. Model profiles expose `max_request_images` and `max_request_image_mib` under Context & retained summary. Current-turn image selection, generation planning and outbound validation honor these fields. Compaction is text-only; these fields no longer force transcript compaction. The byte count excludes base64 overhead. Intake file/pixel bounds remain unchanged. Operator-selected budgets do not assert provider support. Compaction events report image and token measurements and the exact trigger(s).

## Turn-scoped pixels (2026-09-12)

This supersedes the original policy of replaying pixels until transcript
compaction. Each bot selects images only from messages it has not yet handled
(`seq > contexts.last_seen`). Newest messages take priority, preserving attachment
order within each message. Count and cached-byte budgets both apply. Extra
new images stay as metadata, with an explicit model-facing omission notice and a
`context.image_selection` warning. The default count remains ten; operator edits
to individual profiles are preserved.

The selection stays fixed throughout that turn's tool calls and survives text
compaction even when a source row moves behind the summary checkpoint. Source
deletion, attachment removal or replacement revokes its pixels on subsequent
requests. A completed answer or intentional silence advances this bot's existing
handled-message boundary; subsequent turns see metadata and written observations,
not the old pixels. Failure/cancellation leaves input unhandled for normal retry.
Other bots have their own boundary. No timestamps, checkpoint resets, data purge,
new provider request or model-generated image description are needed for expiry.

`pixels_in_this_request` distinguishes actual inputs from cache metadata. A
`vision.status` of `ready` means the file exists in the cache, not that every
request includes it. Old attachments cause neither repeated unavailable-image
errors nor fresh download attempts during context preparation. To inspect old
pixels again, reattach the image; a textual reference or Discord reply alone does
not revive them. The cache and original request evidence stay available for
inspection, backup and scoped file/document tools.

## Owner decisions (preserved from AGENTS, 2026-09-23)

The date marks relocation of standing instructions, not a new product decision.
Existing decision dates and qualifications below remain authoritative.

- Images use the shared multimodal pipeline for every bot; unsupported models fail upstream visibly. Keep image bytes and site blobs outside Git, and include `images/` and `sites/` in consistent backups.

- Each bot has an explicit `allow_images` switch (default true for new and existing bots), exposed as **Receive image inputs** in the modern Capabilities editor. Off skips that bot's gateway/context image downloads and sends metadata/text only, even when another bot has cached the pixels or shares its model profile. Enforce the setting across tool rounds and at provider serialization; never infer model vision support, change another bot/profile, purge shared images or redefine profile image-count zero as the off switch. Profile count/byte budgets remain independent. See VISION.

- The owner requested image intake up to 20 MiB per file, with per-model `max_request_images` and `max_request_image_mib` budgets shared by current-turn selection, planning and wire validation (defaults 10 / 40 MiB). After testing 256 images / 512 MiB, the owner requested 10 images on every saved profile on 2026-09-12, including Hortator; preserve the separate byte budgets. There is no fixed schema maximum for the adjustable image-count field. Discord intake admits up to 10 captures per message independently. The owner subsequently requested turn-scoped pixels: only unhandled messages (`seq > contexts.last_seen`) are eligible, newest messages first within both budgets. Reuse that selection across the turn’s tool rounds, then expire pixels after a sent/silent turn; failures/cancellation do not acknowledge input. Historical and excess attachments remain explicitly metadata-only in future prompts, with original bytes and evidence intact. Compaction is text-only and preserves attributed written observations; never inject private reasoning as image memory or trigger whole-context compaction merely to reduce image count. Preserve selected fresh pixels across text compaction for the current generation and revoke them on source deletion/removal. A reattachment is required to inspect an older image again; quoted mentions and bot replies must not revive old pixels. Log omitted new inputs and every real compaction trigger with measured counts and limits. Preserve original pixels at or below 64 megapixels. The owner authorized proportional resizing only for JPEG/JPG sources above 64 MP (2026-09-15): cache only the converted copy, disclose dimensions/quality loss to the bot, and retain original attachment metadata/hash rather than a second original cache file. This does not authorize deleting Discord uploads, existing unrelated files or the owner's /tmp inspection copy. Reuse ready and permanently rejected outcomes across history/edits; emit image warnings only when the outcome changes. Keep the independent 20 MiB file limit, bounded JPEG decoder workspace and existing turn-scoped visibility. General bot filesystem/Bash tools are optional keyless capabilities. Keep filesystem paths scoped to owned bot/channel/task workspaces, and execute model commands only through the fail-closed OS sandbox; no host-shell fallback or implicit publication authority. The owner authorized host networking, DNS and HTTPS for the granted shell. The application and sandbox baseline is Python 3.14 (including python/python3 aliases); supply uv, venv, pip, the documented CLI utilities and disposable native package installation. Keep Node outside the model sandbox. Package environments belong in bounded per-job /packages, never in persistent workspace snapshots. See docs/AGENTIC_TOOLS.md.
