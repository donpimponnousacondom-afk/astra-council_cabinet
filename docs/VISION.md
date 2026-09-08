# Discord attachment vision

All bots receive actual image pixels through the same context and provider path. This is independent of the model name and does not require a vision plugin. A provider/model that rejects image input produces its ordinary visible upstream request failure; Hortator does not silently retry without the images. A provider could also accept a request without using its images, so HTTP success alone is not evidence of visual understanding.

## Capture and durable storage

After the existing room and exact-owner authorization checks, the Discord gateway downloads supported image attachments from their Discord CDN URLs. Original text-shaped attachment metadata is retained. The `vision` metadata records either `ready` with the SHA-256, detected media type, dimensions and byte count, or `unavailable` with an explanation.

Image bytes live in `$HORTATOR_DATA_DIR/images/<sha256>`, outside the checkout, with owner-only permissions. Atomic writes fsync image bytes and the containing directory before a ready reference is recorded. Cached content survives the signed CDN URL expiring and branch changes. Existing cached bytes are integrity checked; authenticated Discord history observations can supply a refreshed URL to recover a missing cache. Pre-feature transcript rows without vision metadata are downloaded lazily during preparation. Uncompacted attachments previously rejected by a smaller byte limit are retried once when the current limit admits their declared size, including legacy 8 MiB rejections. The refreshed result is persisted, so an expired URL or another failure does not cause a retry loop. Already-compacted summaries are not rewritten. This preparation has a 60-second overall deadline; completed downloads are retained when a retry is needed. Failed capture does not masquerade as visual access: the model sees **PIXELS UNAVAILABLE** and the reason. Reattaching the file provides a fresh download opportunity.

Only HTTPS URLs on `cdn.discordapp.com` or `media.discordapp.net` with Discord attachment paths are fetched. No arbitrary hosts, credentials in URLs, alternate ports, redirects, or environment proxy settings are used. Responses must advertise a supported raster image type or generic binary type, and Pillow validates the actual container/pixels. PNG, JPEG, WebP and GIF are supported. The original bytes are preserved; animation support depends on the model/provider, and local validation decodes only the first frame.

Limits are 20 MiB per file (20,971,520 bytes), 20 megapixels, eight image captures per Discord message, and 15 seconds per download. Oversized, unsupported or invalid attachments retain explicit failure metadata. The image cache is private application storage, not a public static mount. Publishing an attachment is a separate scoped plugin action; merely uploading it to Discord does not publish the local cache.

## Model requests and observability

Generation and compaction both contain structured image parts paired with the source Discord message and attachment IDs. A compacted image is represented by the model's resulting summary after compaction; its original bytes and transcript remain retained locally. Deleted messages supply neither pixels nor attachment metadata to future contexts. Explicit attachment-removal edits also remove their image parts; embed-only edits retain the attachments.

The assembled request uses internal durable image references. Immediately before the HTTP request, the provider adapter loads the bytes, checks their hash and converts each reference into an OpenAI-compatible `image_url` part containing a base64 data URI. The request ledger keeps the durable reference, not megabytes of base64. A missing/corrupt reference fails the request explicitly and does not open a provider-wide failure circuit.

A wire request is limited to eight images and 40 MiB of combined original image bytes. Context preparation triggers normal compaction when images exceed those limits, and compaction batches obey the same limits. Tool follow-up requests retain the same relevant image context. No capability-name registry or model-name heuristic suppresses image input.

Context planning adds a **4,096-token reserve per image** to the existing approximate text estimate. This is a planning allowance, not measured provider image tokens or a guarantee for every tokenizer/resolution. The usual calibration against reported input usage remains active; actual provider usage/cost continues to come from response usage fields. Request/context metadata states the image count and estimator explicitly.

Back up `images/` with the database and other external data. The database's image references are insufficient to restore the pixels after signed links expire. Retention is deliberate: monitor disk space and follow the normal backup procedure rather than deleting cache files while references are in use.

## 2026-09-08: larger Discord image attachments

The owner requested support up to Discord's current standard per-file limit, documented as [20 MiB by the API](https://docs.discord.com/developers/reference#uploading-files). Image intake, cached-file reads and provider expansion all admit that size; the combined request budget is 40 MiB before base64 encoding. The existing 20-megapixel decoded limit and eight-image count still apply. This is an encoded-file size change; original pixels are preserved without recompression or resizing. Provider-specific acceptance is still established by its response.

At the image-ceiling patch stage, `web_fetch` was a bounded text extractor and shell/filesystem tools were deferred to the next feature. [Private workspaces, isolated Bash and complete web reading](AGENTIC_TOOLS.md) now implement that workflow. Document imports and outbound artifacts retain their separate limits; vision intake still preserves original pixels.
