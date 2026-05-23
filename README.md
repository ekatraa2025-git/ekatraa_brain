# Ekatraa Pipecat Voice Service

Real-time conversational voice for Ekatraa apps using [Pipecat](https://docs.pipecat.ai/) + Mastra agents + Sarvam STT/TTS.

## Architecture

```
Mobile / Web client
  → Pipecat WebRTC (this service, port 7860)
      → Sarvam STT (streaming)
      → Mastra agent via OpenAI-compatible proxy (ekatraa_backend)
      → Sarvam TTS (streaming)
  → Audio + RTVI transcripts back to client
```

Mastra tool calling, cart context, and memory stay in `ekatraa_backend` — Pipecat only handles the real-time audio pipeline.

## Setup

```bash
cd pipecat-service
cp env.example .env
# Set SARVAM_API_KEY, DAILY_API_KEY, and EKATRAA_BACKEND_URL

# Install uv if needed: brew install uv
uv sync
uv run bot.py -t daily
```

Open http://localhost:7860/ — Daily creates a room and redirects you (or use your app client with `@pipecat-ai/daily-transport`).

For local Small WebRTC testing only: `uv run bot.py -t webrtc` → http://localhost:7860/client

## Backend env (ekatraa_backend)

| Variable | Purpose |
| --- | --- |
| `PIPECAT_SERVICE_URL` | Public URL returned by `/api/public/ai/voice/session` (e.g. `http://localhost:7860`) |
| `SARVAM_API_KEY` | Shared Sarvam key (also used by legacy STT/TTS routes) |
| `MASTRA_LIBSQL_URL` | Durable Mastra memory for voice threads |

## Client env

| App | Variable | Purpose |
| --- | --- | --- |
| ekatraa | `EXPO_PUBLIC_PIPECAT_VOICE=1` | Enable live Pipecat voice in ChatModal |
| ekatraa-web | `NEXT_PUBLIC_PIPECAT_VOICE=1` | Enable live voice toggle in planning chat |
| ekatraa_vendor | `EXPO_PUBLIC_PIPECAT_VOICE=1` | Enable live voice on vendor assistant tab |

All clients still use `EXPO_PUBLIC_API_URL` / `NEXT_PUBLIC_EKATRAA_API_URL` for session bootstrap at `/api/public/ai/voice/session`.

## API

- `POST /api/public/ai/voice/session` — returns Pipecat start URL + session metadata
- `POST /api/public/ai/voice/chat/completions` — OpenAI-compatible Mastra proxy (customer)
- `POST /api/vendor/ai/voice/chat/completions` — OpenAI-compatible Mastra proxy (vendor)

Legacy chunked voice (record → STT → message → TTS) remains available as fallback when Pipecat is disabled or unreachable.
