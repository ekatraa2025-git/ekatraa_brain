"""
Ekatraa Pipecat voice bot — Sarvam STT/TTS with Mastra agents via OpenAI-compatible proxy.

Run locally:
  cd pipecat-service && uv sync && uv run bot.py -t daily

Web client: http://localhost:7860/
Railway: set PORT (automatic), DAILY_API_KEY, SARVAM_API_KEY, EKATRAA_BACKEND_URL
"""

from __future__ import annotations

import os
import sys
from typing import Any

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregatorParams,
    LLMUserAggregatorParams,
    LLMContextAggregatorPair,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamHttpTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.daily.transport import DailyParams

load_dotenv()


def _is_cloud_runtime() -> bool:
    return bool(
        os.getenv("PORT")
        or os.getenv("RAILWAY_ENVIRONMENT")
        or os.getenv("RAILWAY_ENVIRONMENT_NAME")
        or os.getenv("RAILWAY_PUBLIC_DOMAIN")
    )


def _inject_runner_cli_defaults() -> None:
    """Railway injects PORT and expects 0.0.0.0; Pipecat defaults to localhost:7860 webrtc."""
    argv = sys.argv[1:]
    has_transport = any(a in ("-t", "--transport") for a in argv)
    has_host = "--host" in argv
    has_port = "--port" in argv
    cloud = _is_cloud_runtime()
    extras: list[str] = []

    if not has_transport:
        transport = (os.getenv("PIPECAT_TRANSPORT") or "").strip().lower()
        if not transport:
            transport = "daily" if cloud else "webrtc"
        extras.extend(["-t", transport])

    if not has_host:
        host = (os.getenv("HOST") or os.getenv("PIPECAT_HOST") or "").strip()
        if not host:
            host = "0.0.0.0" if cloud else "localhost"
        extras.extend(["--host", host])

    if not has_port:
        port = (os.getenv("PORT") or os.getenv("PIPECAT_PORT") or "7860").strip()
        extras.extend(["--port", port])

    sys.argv = [sys.argv[0], *extras, *argv]


def _validate_cloud_env() -> None:
    if not _is_cloud_runtime():
        return

    transport = (os.getenv("PIPECAT_TRANSPORT") or "daily").strip().lower()
    missing: list[str] = []
    if not os.getenv("SARVAM_API_KEY") and not os.getenv("SARVAM_API_SUBSCRIPTION_KEY"):
        missing.append("SARVAM_API_KEY")
    if not (os.getenv("EKATRAA_BACKEND_URL") or os.getenv("EKATRAA_MASTRA_OPENAI_BASE_URL")):
        missing.append("EKATRAA_BACKEND_URL")
    if transport == "daily" and not os.getenv("DAILY_API_KEY"):
        missing.append("DAILY_API_KEY")

    if missing:
        logger.warning(
            "Cloud Pipecat deployment missing env vars: {}. Sessions will fail until these are set.",
            ", ".join(missing),
        )

    logger.info(
        "Cloud runtime detected — binding {}:{} transport={}",
        os.getenv("HOST") or os.getenv("PIPECAT_HOST") or "0.0.0.0",
        os.getenv("PORT") or os.getenv("PIPECAT_PORT") or "7860",
        transport,
    )


def _lang_code(raw: str | None) -> Language:
    code = (raw or "en-IN").strip().lower()
    mapping = {
        "en-in": Language.EN_IN,
        "hi-in": Language.HI_IN,
        "bn-in": Language.BN_IN,
        "ta-in": Language.TA_IN,
        "te-in": Language.TE_IN,
        "kn-in": Language.KN_IN,
        "pa-in": Language.PA_IN,
        "mr-in": Language.MR_IN,
        "gu-in": Language.GU_IN,
        "as-in": Language.AS_IN,
        "od-in": Language.OR_IN,
        "or-in": Language.OR_IN,
    }
    return mapping.get(code, Language.EN_IN)


def _session_from_runner(runner_args: RunnerArguments) -> dict[str, Any]:
    body = getattr(runner_args, "body", None) or {}
    if isinstance(body, dict):
        session = body.get("session") or body.get("request_data") or body
        if isinstance(session, dict):
            return session
    return {}


def _mastra_openai_base(session: dict[str, Any]) -> str:
    explicit = (session.get("mastra_openai_base_url") or os.getenv("EKATRAA_MASTRA_OPENAI_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    backend = (os.getenv("EKATRAA_BACKEND_URL") or "http://localhost:3000").rstrip("/")
    agent = (session.get("agent") or os.getenv("PIPECAT_VOICE_AGENT") or "customer").strip().lower()
    if agent == "vendor":
        return f"{backend}/api/vendor/ai/voice"
    return f"{backend}/api/public/ai/voice"


async def run_bot(transport, runner_args: RunnerArguments):
    session = _session_from_runner(runner_args)
    voice_lang = session.get("voice_target_language_code") or os.getenv("PIPECAT_VOICE_LANG") or "en-IN"
    thread_id = session.get("thread_id") or session.get("threadId") or f"pipecat-{os.getpid()}"
    bearer = session.get("authorization") or session.get("access_token")

    sarvam_key = os.getenv("SARVAM_API_KEY") or os.getenv("SARVAM_API_SUBSCRIPTION_KEY")
    if not sarvam_key:
        raise RuntimeError("Missing SARVAM_API_KEY for Pipecat voice bot")

    stt_model = os.getenv("SARVAM_STT_MODEL") or "saaras:v3"
    tts_model = os.getenv("SARVAM_TTS_MODEL") or "bulbul:v3"
    tts_speaker = os.getenv("SARVAM_TTS_SPEAKER") or ("priya" if "v3" in tts_model else "anushka")

    stt = SarvamSTTService(
        api_key=sarvam_key,
        settings=SarvamSTTService.Settings(
            model=stt_model,
            language=_lang_code(voice_lang),
            vad_signals=True,
            high_vad_sensitivity=True,
        ),
    )

    import aiohttp

    aio_session = aiohttp.ClientSession()
    tts = SarvamHttpTTSService(
        api_key=sarvam_key,
        aiohttp_session=aio_session,
        settings=SarvamHttpTTSService.Settings(
            model=tts_model,
            voice=tts_speaker,
            language=_lang_code(voice_lang),
            pace=1.0,
        ),
    )

    mastra_base = _mastra_openai_base(session)
    llm_headers: dict[str, str] = {
        "X-Thread-Id": str(thread_id),
        "X-Voice-Lang": str(voice_lang),
    }
    if bearer:
        token = str(bearer).strip()
        if not token.lower().startswith("bearer "):
            token = f"Bearer {token}"
        llm_headers["X-User-Authorization"] = token

    for key, header in (
        ("city", "X-Voice-City"),
        ("occasion_id", "X-Voice-Occasion-Id"),
        ("occasion_name", "X-Voice-Occasion-Name"),
        ("cart_owner_session_id", "X-Voice-Cart-Session"),
    ):
        val = session.get(key)
        if val:
            llm_headers[header] = str(val)

    llm = OpenAILLMService(
        api_key=os.getenv("PIPECAT_MASTRA_API_KEY") or "ekatraa-pipecat",
        base_url=mastra_base,
        default_headers=llm_headers,
        settings=OpenAILLMService.Settings(
            model="ekatraa-mastra-voice",
            system_instruction=(
                "You are Ekatraa voice planner. Keep responses concise and speakable. "
                "Ground answers in Mastra tools on the server."
            ),
            temperature=0.4,
            max_completion_tokens=800,
        ),
    )

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
        assistant_params=LLMAssistantAggregatorParams(),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Pipecat client connected (thread={})", thread_id)
        context.add_message({
            "role": "developer",
            "content": "Greet the user briefly as Ekatraa AI and ask how you can help with their event planning.",
        })
        context.add_message({
            "role": "user",
            "content": "Hello",
        })
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Pipecat client disconnected")
        await aio_session.close()
        await task.cancel()

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


async def bot(runner_args: RunnerArguments):
    transport_params = {
        "daily": lambda: DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            transcription_enabled=False,
        ),
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import app, main

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "service": "ekatraa-pipecat-voice",
            "transport": (os.getenv("PIPECAT_TRANSPORT") or ("daily" if _is_cloud_runtime() else "webrtc")),
        }

    _inject_runner_cli_defaults()
    _validate_cloud_env()
    main()
