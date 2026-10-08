# Speech generation and voice discovery

The existing `tts` plugin generates current-turn audio artifacts. Enable it
globally and grant it to a bot through the existing capability controls. Its
credential stays in the vault: per-bot override first, then the global plugin key.
The model supplies neither credentials nor endpoints.

## Model calls

- `{"operation":"voices"}` lists Mistral preset voices, including IDs, names,
  language and emotion tags. Use `type: "custom"` or `"all"` when needed.
- Follow the returned `next` with the same tool until null. `page_size` defaults
  to 100 (1–100); `page_token` is the returned opaque continuation.
- `{"text":"Hello","voice":"<returned-id>"}` generates audio using that voice
  for this call only. It does not change the operator's saved default.
- Existing `{"text":"Hello"}` calls keep using the configured default voice.
  Explicit `operation: "generate"` is also accepted. `{}` returns usage only.
- Prepare the returned artifact with `discord_attach`, then answer normally.
  Listing voices generates no speech or attachment and consumes an ordinary
  tool call. There is no automatic catalog polling or extra model request.

These are provider-specific adapters. Mistral's voice-list route is not a
universal OpenAI-compatible API. Other providers currently return
`supported: false` with the configured voice; this is not a complete catalog.
The implementation does not probe arbitrary routes or invent available voices.

## Operator configuration

Use the existing plugin configuration JSON; there are no additional dashboard
controls. `endpoint` remains the complete speech-generation URL. `request_json`
retains the exact model, default voice and other vendor settings.

`api_format` defaults to `"auto"`: the exact `api.mistral.ai` host selects Mistral;
other endpoints retain OpenAI-style `voice`. Set `"api_format":"mistral"` for
a Mistral-compatible proxy; use `"openai"` to select the ordinary protocol
explicitly. Mistral uses `voice_id` for the per-call voice override.

Mistral voice discovery replaces the configured endpoint's `/v1/audio/speech`
suffix with `/v2/audio/voices`, preserving any proxy path prefix. An optional
operator `voices_endpoint` specifies a different Mistral-v2-compatible catalog
route. It uses the same plugin key. Models cannot alter either destination.
Requests preserve the existing no-redirect credential-forwarding policy.

For example, Mistral configuration may contain:

```json
{
  "endpoint": "https://api.mistral.ai/v1/audio/speech",
  "request_json": {
    "model": "voxtral-mini-tts-2603",
    "voice_id": "fr_marie_curious",
    "response_format": "mp3"
  }
}
```

Prefer an ID actually returned by the account's voice catalog. A bare name such
as `marie` is not the language/emotion-specific preset identifier. Installing
this feature does not rewrite saved voice choices or enable any bot.

## Responses and limits

Mistral's complete speech response contains base64 `audio_data` inside JSON.
The adapter decodes it to the requested audio format before creating the owned
artifact. Existing binary audio responses remain supported. Invalid/empty JSON
audio and HTML/event-stream responses fail without creating an attachment.
Streaming speech is not implemented; an explicit true `request_json.stream`
returns an actionable configuration error before generation.

Existing limits remain 4,000 input characters, 20 MB downloaded media and 8 MB
decoded attachment. Catalog reads allow 1 MB per page and 30 seconds per request;
generation retains its 100-second request timeout and the registry's 120-second
tool deadline. Cancellation propagates. HTTP failures include status and up to
2,000 bytes of upstream error text through the existing credential redactor.
An invalid voice, an authentication failure and malformed audio stay distinct.

Sources: [Mistral voice API](https://docs.mistral.ai/api/endpoint/audio/voices)
(which recommends v2 cursor pagination),
[speech response API](https://docs.mistral.ai/api/endpoint/audio/speech).
The v2 `page_size`/`page_token` contract was verified live on 2026-10-08;
see VERIFICATION for the boundary between live and mocked checks.

## Owner decisions

- 2026-10-08: bots should discover available TTS voices through the harness's
  saved credential, without receiving API keys. Extend the existing TTS plugin;
  keep discovery provider-specific and per-call choice separate from saved defaults.
