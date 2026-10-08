# Frenchy TTS · experimental

`frenchy_tts` is the opt-in Mistral voice playground. It shares the speech and
catalog adapters with [TTS](TTS.md), with its own global switch, bot grants,
configuration and vault key. New installations leave it disabled. Ordinary
`tts` continues to produce ordinary audio attachments.

## Calls and voices

- `{"operation":"voices"}` lists presets with provider IDs, names, emotion tags
  and languages. Follow `next` using `frenchy_tts`; `type:"custom"` lists saved
  clones. Catalog pagination and per-call `voice` selection follow TTS.
- `{"text":"Bonjour!","voice":"<returned-id>"}` generates speech. Omit `voice`
  to use the saved default. Emotions are catalog voice variants, not an invented
  emotion parameter. Language comes from spoken text and the chosen voice.
- `{"operation":"clone_voice","name":"My voice","message_id":"123","attachment_id":"456"}`
  saves the observed audio as a reusable voice in the configured Mistral account.
  Reuse its returned `voice.id`; name alone is not a voice ID.
- `{"text":"Hello!","message_id":"123","attachment_id":"456"}` uses that
  clip once, without saving a voice. The sample overrides the configured default.
- Either reference call can use `reference_artifact_id` instead of the two
  Discord IDs, for audio owned by this bot in this turn. A reference and an
  explicit `voice` are mutually exclusive.

References must be audio, at most 30 seconds and 20 MiB. Discord references must
be present in an undeleted observed message in the current conversation; the
tool does not accept arbitrary URLs or filesystem paths. Existing audio capture
and artifact ownership rules apply, including rechecks after preparation.
This explicit tool reference does not require the separate audio-input grant.
Saved clones live upstream and can be reused across turns through their ID;
temporary artifacts retain the existing turn ownership.

Cloning is an upstream write and can require a paid Mistral plan. There is no
automatic clone retry: if a connection fails after acceptance, list custom
voices before retrying. A malformed success response explicitly reports this
uncertainty. The tool does not expose voice deletion or change the operator's
default voice. Provider permission failures remain visible to the model.

## Native voice notes

Generation defaults to `delivery:"voice_note"`. Use `discord_attach` with the
returned artifact ID, then produce the written answer normally. The ordinary
answer is sent first; each prepared voice note follows as a separate message
replying to it. Discord voice messages cannot include text or components.
The text retains the normal footer and optional panel. Other attached files stay
with the text. A silent activation sends neither.

Each voice note gets its own durable outbox receipt, nonce, destination checks
and channel send gap. Confirmed text stays confirmed if audio fails. Ambiguous
acceptance is recorded as unknown with no automatic resend; gateway nonce
reconciliation can confirm it later. Cancellation joins upload cleanup and
suppresses undispatched companions. The complete batch holds the existing
destination send lock. This is not a background delivery service.

Cancellation after confirmed text still propagates to the task owner. The turn
records the written answer as sent, consumes its handled input and commits any
still-current Engram candidate. The companion keeps its own interrupted receipt;
the next cadence does not treat the original human input as unanswered.

`delivery:"file"` opts out for a call; operator configuration can make it the
default. Slash-command and panel interaction responses retain ordinary file
delivery in their existing response scope. Explicit `discord_send` cross-posts
also attach the OGG as a file, preserving their single delivery receipt. These
paths do not spawn companion messages. Confirmed ordinary notes store the supplied speech text as a labelled
canonical transcript; it is not a public text caption or speech recognition.

Native output is mono 48 kHz Opus in OGG at 32 kbit/s, with measured duration and
a base64 waveform of at most 256 points. `ffmpeg` and `ffprobe` must be installed
on the runtime host. Preparation admits two workers, moves only detached bytes
and paths off the event loop, and joins them on cancellation. Probes each have a
10-second timeout; the two conversion commands each have 30 seconds. Notes are
limited to 20 minutes and the existing 8 MB artifact limit. Provider input/wire
bounds and the registry's 120-second deadline still apply. Cancellation can wait
for bounded worker cleanup beyond that deadline.

If conversion fails, the already generated audio is retained as an ordinary
file with an explicit warning. Raw PCM lacks the container metadata needed by
this converter and falls back to a file; use MP3, WAV, FLAC or OGG for notes.
There is no additional paid generation for conversion or fallback.

## Configuration and storage

The existing plugin JSON editor is sufficient. Copy the TTS endpoint,
`request_json` and key to Frenchy; per-bot configuration and key overrides stay
separate under the `frenchy_tts` ID. Use `api_format:"mistral"` for a compatible
proxy. `voices_endpoint` supports the existing v2 catalog override.
`clone_endpoint` optionally overrides the derived `/v1/audio/voices` route;
otherwise replace the `/v1/audio/speech` suffix, preserving proxy path prefixes.
Models cannot alter these endpoints or receive the saved credential.

The additive `artifact_voice_notes` table stores delivery metadata and spoken
text alongside owned artifacts. Existing complete database/artifact backups
include it. Follow the owner's [on-request backup policy](OPERATIONS.md#backup-and-restore);
restoring only the database without its artifacts is not sufficient.

## Owner decisions

- 2026-10-08: build a separate experimental plugin called Frenchy TTS for voice
  discovery, preset emotions/languages, saved clones, one-shot references and
  Discord voice notes. Share existing adapters while preserving ordinary TTS.
- Send a separate written answer alongside native voice notes.
- Copy the installation's current TTS settings/key and switch Curie's grant to
  Frenchy, preserving Marie Excited. This installation migration is explicit;
  it is not a new-install default or a change to other bots.

Sources: [Mistral voice creation](https://docs.mistral.ai/studio/audio/text_to_speech/voices),
[speech and reference audio](https://docs.mistral.ai/studio/audio/text_to_speech/speech),
[Discord voice messages](https://docs.discord.com/developers/resources/message#voice-messages).
