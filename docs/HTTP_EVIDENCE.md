# Web response evidence and console display

Implemented on 2026-09-14 after the owner identified that generic HTTP exceptions hid response bodies and that the console stripped useful URLs. This contract applies to `web_fetch` and `web_search`; media-generation and Discord attachment adapters are separate follow-ups.

## Received responses

A received HTTP response retains its numeric `http_status`, supplied `http_reason` (null if unavailable), final URL, ordered response-header entries, body encoding, captured byte count and capture-completeness flag. Duplicate headers are preserved. There is no invented reason phrase or replacement error page. Credentials remain redacted; this is evidence capture, not credential export.

Every status follows the same capture path, including 200, 202, 204, 206, redirects, nonstandard 419 and 4xx/5xx responses. `web_fetch` may follow 301/302/303/307/308 within its existing six-request public-address boundary; intermediate status, headers and bodies remain in response evidence. Missing/blocked targets and redirect exhaustion are explicitly local issues. Redirect bodies share the configured download budget. Search does not follow redirects or forward the Brave credential elsewhere.

`http_response` exposes a `response_result_id` with `read_response` arguments. Search and unextractable/partial responses include a body preview; extracted document pages omit the duplicate raw-body preview so it cannot crowd out the requested text page. Follow those arguments using the originating tool's `read_result` operation to page through the complete recorded response JSON without making another request. Model-facing previews are bounded; truncation/pagination is explicit and does not destroy captured evidence. Decoded text is retained before HTML extraction; non-decodable bytes use explicitly labelled base64. Existing decompressed download limits still apply. If a limit or body-transfer interruption occurs, the received status/headers and available body remain, with `capture_complete: false` and a separate local issue/transport error. Never claim a partial capture is complete.

A successfully stored extracted document retains `status: ready`; this means **local snapshot readiness**, not upstream success or completion of an HTTP-202 task. `http_response.http_status` remains independent. Text extraction failures retain the HTTP evidence and identify `local_issue` instead of impersonating an upstream error. Missing-content-type and binary restrictions still prevent pretending a binary body is readable page text. Empty bodies are represented as empty, including 204.

`web_fetch.read_response(document_id, offset, length)` pages original response evidence for a retained snapshot across turns in that bot/channel, using the same ownership/expiry checks as ordinary reads. Historical snapshots without this capture report that it was not recorded; discarded old response bodies cannot be recreated. A new additive `fetched_documents.http_response` column stores the evidence reference. Complete response evidence lives in the existing backed-up tool-evidence table and follows its durable trajectory retention, independently of expired extracted files.

Search continues to extract and combine Brave/DuckDuckGo results. Each `engine_status` contains its actual HTTP response, separate parsing category/local issue and result count. A DuckDuckGo 202 challenge now reaches the parser with its actual body; a reported challenge must come from recognized body markers. A valid result payload is not discarded merely because its status differs from 200. Partial success retains the other engine's results. Locally generated parser explanations and the combined-search summary are labelled diagnostics, not server response text. Search's own deadline is `local_deadline`, distinct from a transport failure.

Under active tool-context pressure, successful search results keep their extracted
entries before duplicated raw HTTP previews, headers and redirects. Those raw
fields may be paged out of the prompt copy with an explicit `paged_fields` list,
notice and existing `read_response` handle. Status, reason, capture completeness
and parsing diagnostics remain visible; failed-engine previews are not removed
by this success-only step. Full original results and HTTP evidence stay unchanged
in the ledger. Paging is a local context decision, not evidence of search failure.

## Console contract

Normal web-tool lines show the full requested/final URL, including ordinary query parameters and fragments, without length-based clipping. Different requested and final URLs are both shown. URL input validation allows up to 65,536 characters; this is a resource bound, not a claim every upstream server accepts that length. The terminal may wrap naturally. Terminal control characters remain escaped and credential protection remains active.

Shared fields have one severity-independent order: tool name, operation, engine, HTTP status, local status, duration/TTFT, URLs/query, attempt counters and retry delay, followed by other metadata and finally diagnostic messages. Missing fields are omitted without changing the relative order of present fields. Millisecond timings have one decimal in the console; original numeric measurements remain in the ledger. Full `call_id` stays in structured events and expanded `T` evidence instead of the default line. Console text is for operators; durable JSON event keys remain the parser contract.

Received HTTP 4xx/5xx responses and local extraction/capture limitations are warnings, not invented transport failures. Actual transport failure while reading a response is an error with the partial HTTP evidence intact. Search engine outcomes retain their own severity alongside the combined tool result. INFO versus ERROR cannot reorder shared fields. Repeat grouping distinguishes severity, URL, HTTP status and search engine/query so an escalation or different destination is not hidden.

Expanded `T` evidence follows the original HTTP response handles, including each search engine, so `n`/`N` can page received headers and body directly in the console. Literal upstream text is not treated as private provider reasoning. Existing console evidence bounds remain explicit; no source response is rerun just to inspect evidence. Existing `T`, `P`, `f`, scopes, normal scrollback and Ctrl-C remain available.
