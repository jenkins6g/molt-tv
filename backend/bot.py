"""MoltStreamer — AI Twitch-streamer-style bot playing Gradient Bang.

End-to-end runnable. Structured after chadbailey59/gb-bot bot-v5.py with our
modules swapped in (game_state, game_processors, game_session, chat_bridge)
and our streamer persona prompts.

Run::

    cp .env.example .env       # fill in GB_EMAIL, GB_PASSWORD, OPENAI_API_KEY
    uv run python bot.py                       # text mode (no TTS)
    uv run python bot.py --audio               # audio mode (Gradium TTS)
    uv run python bot.py --room-url <url>      # rejoin an existing room

LLM selection:
- NEMOTRON_LLM_URL set  → vLLM-served Nemotron-3-Super-120B
- otherwise             → OpenAI (defaults to gpt-4.1, override via OPENAI_MODEL)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv

# Load .env BEFORE importing modules that read env vars at module-load time.
load_dotenv(override=True)

from fastapi import FastAPI  # noqa: E402
from loguru import logger  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    LLMMessagesAppendFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
)
from pipecat.processors.frame_processor import FrameProcessor  # noqa: E402
from pipecat.transports.daily.transport import DailyParams, DailyTransport  # noqa: E402

from beat_ticker import BeatTicker  # noqa: E402
from chat_bridge import ChatBridge  # noqa: E402
from failure_retrieval import FailureRetrievalInjector  # noqa: E402
from game_processors import (  # noqa: E402
    BotSpeechLogger,
    GameContextInjector,
    SpeechTimingState,
    TextModeEmitter,
    TransportSpeechTimingLogger,
    WaitTagFilter,
)
from game_session import GameSession  # noqa: E402
from gb_api import login_and_start  # noqa: E402
from transcript_recorder import TranscriptRecorder  # noqa: E402

from app.agent.mode_parser import ChatModeParser  # noqa: E402
from app.eval.cekura_client import CekuraClient  # noqa: E402
from app.memory.failure_store import FailureStore  # noqa: E402


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logger.remove()
    try:
        logger.add(sys.stderr, level=level)
    except ValueError:
        logger.add(sys.stderr, level="INFO")


configure_logging()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MoltStreamer — AI plays Gradient Bang.")
    parser.add_argument(
        "--audio", action="store_true",
        help="Speak via Gradium TTS. Default is text mode (Daily app-messages to Ship AI).",
    )
    parser.add_argument(
        "--room-url",
        help="Existing Daily room URL to join instead of creating a new GB session.",
    )
    parser.add_argument(
        "--token",
        help="Daily token for --room-url. ?t= in --room-url is also accepted.",
    )
    parser.add_argument(
        "--chat-port", type=int, default=int(os.getenv("APP_PORT", "8000")),
        help="Port for the audience chat HTTP sidecar. 0 to disable. Default 8000.",
    )
    parser.add_argument(
        "--kick-start-after", type=float, default=5.0,
        help="Seconds after client-ready to force-prompt the Officer if Ship AI is silent.",
    )
    args = parser.parse_args(argv)
    if args.token and not args.room_url:
        parser.error("--token requires --room-url")
    return args


def resolve_daily_room(room_url: str, token: str | None = None) -> tuple[str, str | None]:
    parsed = urlsplit(room_url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    url_tokens = [v for k, v in query if k == "t"]
    if url_tokens:
        url_token = url_tokens[-1]
        if token and token != url_token:
            raise ValueError("Daily token provided by both --token and ?t= with different values")
        token = url_token
        query = [(k, v) for k, v in query if k != "t"]
        room_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment)
        )
    return room_url, token


def build_llm():
    """Pick an LLM backend, in priority order:

    1. ``NEMOTRON_LLM_URL``    → vLLM-served Nemotron-3-Super-120B
    2. ``OPENROUTER_API_KEY``  → OpenRouter (https://openrouter.ai/api/v1)
    3. ``OPENAI_API_KEY``      → OpenAI directly
    """
    from pipecat.services.openai.llm import OpenAILLMService
    from nemotron_llm import VLLMOpenAILLMService

    nemotron_url = os.environ.get("NEMOTRON_LLM_URL", "").strip()
    if nemotron_url:
        logger.info(f"[LLM] Nemotron via vLLM at {nemotron_url}")
        enable_thinking = os.environ.get("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
        return VLLMOpenAILLMService(
            api_key=os.environ.get("NEMOTRON_LLM_API_KEY", "EMPTY"),
            base_url=nemotron_url,
            settings=VLLMOpenAILLMService.Settings(
                model=os.environ.get("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
                extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
            ),
        )

    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if or_key:
        model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
        base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        logger.info(f"[LLM] OpenRouter {model} via {base_url}")
        return OpenAILLMService(
            api_key=or_key,
            base_url=base_url,
            settings=OpenAILLMService.Settings(model=model),
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "No LLM configured. Set one of: NEMOTRON_LLM_URL, OPENROUTER_API_KEY, OPENAI_API_KEY."
        )
    model = os.environ.get("OPENAI_MODEL", "gpt-4.1")
    logger.info(f"[LLM] OpenAI {model}")
    return OpenAILLMService(
        api_key=api_key,
        settings=OpenAILLMService.Settings(model=model),
    )


def build_output_stage(audio_mode: bool) -> FrameProcessor:
    if not audio_mode:
        logger.info("[OUT] text mode → TextModeEmitter")
        return TextModeEmitter()

    from pipecat.services.gradium.tts import GradiumTTSService

    api_key = os.environ.get("GRADIUM_API_KEY")
    if not api_key:
        raise SystemExit("--audio requires GRADIUM_API_KEY in .env")
    voice = os.environ.get("GRADIUM_VOICE_ID", "Eu9iL_CYe8N-Gkx_")
    logger.info(f"[OUT] audio mode → Gradium voice={voice}")
    return GradiumTTSService(
        api_key=api_key,
        settings=GradiumTTSService.Settings(voice=voice),
    )


async def run_session(
    room_url: str,
    room_token: str | None,
    audio_mode: bool,
    chat_port: int,
    kick_start_after: float,
    session_id: str | None = None,
) -> None:
    logger.info(f"[BOT] mode={'audio' if audio_mode else 'text'} room={room_url}")

    session = GameSession(text_mode=not audio_mode)
    system_instruction = session.load_system_prompt()
    logger.info(f"[BOT] loaded system prompt ({len(system_instruction)} chars)")

    session_id = session_id or uuid.uuid4().hex
    logger.info(f"[BOT] session_id={session_id}")

    transport = DailyTransport(
        room_url, room_token, "MoltStreamer",
        params=DailyParams(
            audio_in_enabled=False,
            audio_out_enabled=audio_mode,
        ),
    )

    llm = build_llm()
    output_stage = build_output_stage(audio_mode)

    context = LLMContext(messages=[{"role": "system", "content": system_instruction}])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    store = FailureStore()
    cekura_client = CekuraClient()
    timing = SpeechTimingState()
    pipeline = Pipeline([
        transport.input(),
        TransportSpeechTimingLogger("transport input", timing),
        user_aggregator,
        GameContextInjector(session.context_store),
        FailureRetrievalInjector(store=store),
        llm,
        WaitTagFilter(on_wait=timing.reset_for_wait_response),
        ChatModeParser(store=store),
        TranscriptRecorder(store=store, session_id=session_id),
        BotSpeechLogger(),
        output_stage,
        transport.output(),
        TransportSpeechTimingLogger("transport output", timing),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        enable_rtvi=False,
        observers=[],
    )

    chat_bridge = ChatBridge(store=store)
    sidecar_task: asyncio.Task | None = None
    sidecar_server = None
    if chat_port:
        sidecar_app = FastAPI(title="MoltStreamer chat sidecar")
        chat_bridge.attach_router(sidecar_app)

        @sidecar_app.get("/health")
        async def _health():
            return {"ok": True, "room": room_url}

        import uvicorn
        config = uvicorn.Config(sidecar_app, host="0.0.0.0", port=chat_port, log_level="warning")
        sidecar_server = uvicorn.Server(config)
        sidecar_task = asyncio.create_task(sidecar_server.serve())
        logger.info(f"[CHAT] sidecar listening on :{chat_port}  (POST /chat)")

    drain_task = asyncio.create_task(chat_bridge.drain(task))

    beat_ticker = BeatTicker(
        store=store,
        cekura_client=cekura_client,
        context_store=session.context_store,
        session_id=session_id,
    )
    beat_ticker_task = asyncio.create_task(beat_ticker.run())

    # ---- transport event handlers -----------------------------------------

    async def send_rtvi(msg_type: str, data: dict | None = None) -> None:
        msg: dict[str, object] = {"label": "rtvi-ai", "type": msg_type, "id": uuid.uuid4().hex}
        if data is not None:
            msg["data"] = data
        await task.queue_frames([OutputTransportMessageUrgentFrame(message=msg)])

    async def queue_game_turn(text: str) -> None:
        logger.info(f"[GAME TURN → OFFICER] {text}")
        try:
            store.record_transcript_turn(session_id, "game", text)
        except Exception as e:
            logger.warning(f"[BOT] record_transcript_turn(game) failed: {e}")
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": text}],
                run_llm=True,
            )
        ])

    @transport.event_handler("on_joined")
    async def on_joined(_transport, _data):
        logger.info("[DAILY] joined room")
        await asyncio.sleep(2)
        logger.info("[RTVI] sending client-ready")
        await send_rtvi("client-ready")

        # Kick-start safety hatch: if Ship AI is silent after N seconds,
        # nudge the Officer to open the stream itself.
        await asyncio.sleep(kick_start_after)
        if not session.context_store.render():
            logger.warning(
                f"[KICK] Ship AI silent for {kick_start_after}s — nudging Officer to open the stream"
            )
            await queue_game_turn(
                "You just joined the bridge for a fresh stream. The Ship AI "
                "hasn't said anything yet. Open with one dry line to your "
                "audience, then ask the Ship AI for your current status and "
                "warp power."
            )

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport, message, sender):
        logger.debug(f"[APP MESSAGE][{sender}] {message}")
        text = session.handle_app_message(message)
        if text:
            await queue_game_turn(text)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client):
        logger.info("[DAILY] client disconnected — cancelling task")
        await task.cancel()

    # ---- run --------------------------------------------------------------
    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        chat_bridge.close()
        drain_task.cancel()
        beat_ticker.stop()
        beat_ticker_task.cancel()
        try:
            await asyncio.wait_for(beat_ticker_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        if os.environ.get("CEKURA_OBSERVABILITY_ENABLED", "true").lower() != "false":
            try:
                ok = await asyncio.wait_for(beat_ticker.final_flush(), timeout=5.0)
                logger.info(f"[CEKURA] final observe {'ok' if ok else 'skipped/failed'}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[CEKURA] final observe error: {e}")
        try:
            await cekura_client.close()
        except Exception:
            pass
        if sidecar_server is not None:
            sidecar_server.should_exit = True
        if sidecar_task is not None:
            try:
                await asyncio.wait_for(sidecar_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass


async def main() -> None:
    args = parse_args()
    session_id: str | None = None
    if args.room_url:
        try:
            room_url, room_token = resolve_daily_room(args.room_url, args.token)
        except ValueError as e:
            raise SystemExit(str(e)) from e
        logger.info(f"[BOT] joining existing room: {room_url}")
    else:
        creds = await login_and_start()
        room_url, room_token = creds.room_url, creds.room_token
        session_id = getattr(creds, "session_id", None)

    await run_session(
        room_url=room_url,
        room_token=room_token,
        audio_mode=args.audio,
        chat_port=args.chat_port if args.chat_port > 0 else 0,
        kick_start_after=args.kick_start_after,
        session_id=session_id,
    )


if __name__ == "__main__":
    asyncio.run(main())
