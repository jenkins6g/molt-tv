import os

from dotenv import load_dotenv
from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.daily.transport import DailyParams, DailyTransport

load_dotenv()

SYSTEM_PROMPT = (
    "You are a charismatic AI co-host on a live stream. "
    "Viewers send you text messages and you respond out loud to the whole room. "
    "Keep every response to 1–3 sentences, conversational, no markdown or bullet points. "
    "Be warm and engaging but not over the top."
)


async def run_bot(room_url: str, token: str) -> None:
    transport = DailyTransport(
        room_url,
        token,
        "molt-agent",
        params=DailyParams(
            audio_in_enabled=False,
            audio_out_enabled=True,
        ),
    )

    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILLMService.Settings(model="gpt-4.1"),
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        settings=CartesiaTTSService.Settings(
            voice=os.environ.get("CARTESIA_VOICE_ID", "a0e99841-438c-4a64-b679-ae501e7d6091"),
        ),
    )

    context = LLMContext(messages=[{"role": "system", "content": SYSTEM_PROMPT}])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        tts,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True),
    )

    runner = PipelineRunner(handle_sigint=True)

    @transport.event_handler("on_joined")
    async def on_joined(_transport, _data):
        logger.info("[DAILY] joined room")

    @transport.event_handler("on_participant_joined")
    async def on_participant_joined(_transport, participant):
        if participant.get("info", {}).get("isLocal"):
            return
        name = participant.get("info", {}).get("userName", "someone")
        logger.info(f"[DAILY] viewer joined: {name}")
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": f"A viewer named {name} just joined the stream. Welcome them briefly."}],
                run_llm=True,
            )
        ])

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport, message, sender):
        logger.debug(f"[APP MESSAGE][{sender}] {message}")
        # Daily prebuilt chat sends: {"event": "chat-msg", "message": "...", "name": "..."}
        text = None
        if isinstance(message, dict):
            text = message.get("message") or message.get("text") or message.get("data")
        elif isinstance(message, str):
            text = message

        if text:
            logger.info(f"[CHAT] {sender}: {text}")
            await task.queue_frames([
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": text}],
                    run_llm=True,
                )
            ])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(_transport, participant, reason):
        logger.info(f"[DAILY] participant left: {participant.get('info', {}).get('userName')}")

    await runner.run(task)
