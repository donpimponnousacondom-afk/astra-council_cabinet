# Native audio input · experimental

`audio_input` lets a granted bot hear new Discord voice notes and uploaded audio
through its existing conversational model/provider. It is a non-tool capability:
the bot does not need to discover or call a transcription tool. It creates no
separate model request or transcription service, and needs no extra API key.
Text-to-speech (`tts`) remains independent. Live voice channels/calls and audio
extraction from video are outside this feature.

## Enable and use

Enable **Plugins → Audio input · experimental**, then grant it under the desired
bot's existing **Capabilities**. Both switches are required. New installations
and this rollout leave it disabled; the owner will enable it manually. Select an
audio-capable model and transport. Model slugs are not capability evidence, so
the harness does not guess support or change profiles automatically. A provider
that rejects audio returns its normal visible error.

The harness host needs `ffprobe` (usually installed with FFmpeg). The project
Docker image includes it. Missing probe support is reported as unavailable audio;
install it and reattach the clip to retry. Reattach expired, unsupported or
invalid files as well. Ordinary wake rules still apply: mentioning/replying to a
bot or its configured cadence determines whether it takes a turn. Merely granting
audio does not make every bot answer every attachment.

Supported containers are WAV, MP3, OGG (including Discord Opus voice notes), FLAC,
AAC, AIFF, M4A and WebM audio. Probe the actual container/duration independently of
the filename or Discord's duration hint; retain the original bytes without
transcoding. Video MIME types are not candidates; probing rejects actual video
tracks even in a mislabelled audio container. Embedded album-cover art remains
part of an audio file. Individual model/provider acceptance and understanding can differ.
Native OpenAI-compatible `input_audio` carries base64 and a format identifier;
CPA translates it to Gemini inline media on its supported route. See
[Gemini audio input](https://ai.google.dev/gemini-api/docs/openai#audio-understanding)
and the [CPA adapter](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/translator/antigravity/openai/chat-completions/antigravity_openai_request.go).

Audio parts always use a user message, as required by the
[Chat Completions content schema](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).
If the operator chooses a system-role transcript template, its text retains that
role and the attributed audio follows in a separate user message.

## Turn lifetime and evidence

Only visible, undeleted messages newer than that bot's `contexts.last_seen` are
eligible. Newest messages take priority, preserving attachment order within each
message. Downloads happen during context preparation, not while importing an old
Discord backlog. Each bot consumes independently; a shared cache does not grant
another bot access to audio input.

The selection stays fixed through tool rounds and text compaction. Compaction
itself receives metadata and written observations, never the audio. Audio source
deletion, attachment removal/replacement or grant revocation prevents subsequent
requests from using the old clip. A completed answer/intentional silence advances
the existing handled-message boundary. Failure/cancellation leaves input eligible
for normal retry. Later turns retain metadata and written observations; a textual
reference/reply does not replay an old recording. Reattach it to hear it again.

`audio_in_this_request` separates actual sound from cache status. `audio.status`
of `ready` means bytes were cached, not that every request includes them. Omitted
clips carry count/byte/duration/capture-budget reasons; capture failures carry
explicit unavailable metadata. Conversation-style and structured transcripts both
distinguish supplied audio from metadata. Events `attachment.audio_unavailable`
and `context.audio_selection` explain failures/selection without file contents.

Request bodies in the ledger hold `hortator_audio` references with source IDs,
format, measured duration, encoded size and SHA-256. Immediately before HTTP,
check current grants/source/budgets and load/hash the bytes in a read worker, then
expand to native audio. Base64 is not persisted as context or tokenized as text.
Corrupt/missing cache files fail locally without blaming the provider's health.
The private `audio/` cache has no public static mount.

## Resource budgets

The existing Discord attachment-CDN URL boundary is reused. Downloads have a
15-second deadline and a 20 MiB per-file ceiling; redirects are not followed.
Container probing has a 10-second deadline, accepts local supported containers,
and performs no transcoding or model inference. Capture/file work has two slots;
its worker is joined on cancellation before temporary files/runtime resources
can be released. SQLite stays on its owner thread.

Global plugin JSON and per-bot plugin JSON accept these positive integer settings:

| Setting | Default | Meaning |
| --- | --- | --- |
| `max_clips` | 4 | Maximum captures considered and clips supplied per turn/request |
| `max_request_mib` | 20 | Combined encoded audio bytes before base64 overhead |
| `max_duration_seconds` | 600 | Combined actual audio duration per request |

Preparation has a 60-second deadline across all clips. Completed cache progress
survives a timeout; the turn fails explicitly for retry. Budget omissions do not
force text compaction and do not silently become transcripts. Context planning
adds 32 tokens per second of selected audio, following
[Gemini's documented rate](https://ai.google.dev/gemini-api/docs/audio).
This is an approximate reserve for other providers, not billing. Existing
calibration and provider-reported usage/cost remain authoritative.

Back up `audio/` alongside the database: stored references alone cannot restore
expired signed downloads. CLI backups, full dashboard snapshots and full restore
include this store. Retention follows existing on-request archive procedures;
this feature adds no cache purge or recurring job.

## Owner decisions — 2026-10-07

The owner requested voice-note/general audio input and confirmed another harness
already uses the same CPA/proxy route. Implement native audio as an opt-in plugin
for eligible bots without another transcription agent. Leave the plugin and all
bot grants disabled at rollout; the owner explicitly chose to enable it manually.
No bot/model/provider configuration changes accompany installation.
