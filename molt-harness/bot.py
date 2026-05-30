import asyncio
import base64
import os
import re

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from pipecat.frames.frames import LLMFullResponseStartFrame, LLMMessagesAppendFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.daily.transport import DailyParams, DailyTransport

load_dotenv()

SYSTEM_PROMPT = (
    "You are a charismatic, witty AI co-host on a live stream — think a Twitch streamer "
    "who is always on, always entertaining. You react to what's on screen, riff with the chat, "
    "and keep the energy up even in silence.\n\n"
    "At the very start of EVERY response, output one emotion tag: [EMOTION:X] or [EMOTION:X speed:Y].\n"
    "X must be one of: excited, enthusiastic, sarcastic, curious, joking/comedic, surprised, "
    "content, amused, confident, flirtatious.\n"
    "Y is a float 0.6–1.5 (default 1.0). Use higher speed for hype, lower for dry wit.\n"
    "Choose the emotion that best matches what you're about to say, then write 1–3 sentences. "
    "Do NOT include the tag in your spoken text — it is stripped before speech.\n"
    "Example: [EMOTION:sarcastic speed:0.85] Oh wow, another question about the weather. Riveting."
)

PROACTIVE_INTERVAL = 10   # seconds between unprompted comments
COOLDOWN_AFTER_ACTIVITY = 8   # seconds to stay quiet after chat or agent speech

EMOTION_RE = re.compile(r'\[EMOTION:([a-z/\s]+?)(?:\s+speed:([\d.]+))?\]', re.IGNORECASE)


class EmotionExtractor(FrameProcessor):
    """Strips [EMOTION:X speed:Y] tags from LLM output and applies them to Cartesia TTS."""

    def __init__(self, tts: CartesiaTTSService):
        super().__init__()
        self._tts = tts
        self._buffer = ""
        self._found = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._buffer = ""
            self._found = False
            await self.push_frame(frame, direction)

        elif isinstance(frame, TextFrame) and not self._found:
            self._buffer += frame.text
            m = EMOTION_RE.search(self._buffer)
            if m:
                self._found = True
                emotion = m.group(1).strip().lower()
                speed = float(m.group(2)) if m.group(2) else 1.0
                self._tts._settings.generation_config = GenerationConfig(
                    emotion=emotion, speed=speed
                )
                logger.info(f"[EMOTION] {emotion} speed={speed}")
                remainder = self._buffer[m.end():].lstrip()
                if remainder:
                    await self.push_frame(TextFrame(text=remainder), direction)

        else:
            await self.push_frame(frame, direction)


async def run_bot(room_url: str, token: str, page=None) -> None:
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

    emotion_extractor = EmotionExtractor(tts)
    openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    context = LLMContext(messages=[{"role": "system", "content": SYSTEM_PROMPT}])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        emotion_extractor,
        tts,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True),
    )

    runner = PipelineRunner(handle_sigint=True)

    # Shared activity timestamp — updated on chat in and agent speech out
    last_activity: list[float] = [asyncio.get_event_loop().time()]

    async def take_screenshot() -> str | None:
        if page is None:
            return None
        try:
            preview = page.locator("#preview-container")
            if await preview.count() == 0 or not await preview.is_visible():
                return None
            data = await preview.screenshot()
            return base64.b64encode(data).decode()
        except Exception as e:
            logger.debug(f"Screenshot skipped: {e}")
            return None

    async def describe_screen(b64: str) -> str:
        resp = await openai_client.chat.completions.create(
            model="gpt-4.1",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": (
                        "You're helping a live-stream AI co-host react to what's on screen. "
                        "Describe the most notable or interesting thing visible in one short sentence."
                    )},
                ],
            }],
            max_tokens=80,
        )
        return resp.choices[0].message.content.strip()

    async def proactive_loop() -> None:
        await asyncio.sleep(10)  # warm-up before first proactive comment
        while True:
            await asyncio.sleep(PROACTIVE_INTERVAL)
            elapsed = asyncio.get_event_loop().time() - last_activity[0]
            if elapsed < COOLDOWN_AFTER_ACTIVITY:
                continue

            screenshot_b64 = await take_screenshot()
            if screenshot_b64:
                try:
                    description = await describe_screen(screenshot_b64)
                    prompt = (
                        f"[Screen update — react to this as a streamer]: {description}"
                    )
                    logger.info(f"[PROACTIVE] reacting to screen: {description}")
                except Exception as e:
                    logger.warning(f"Vision call failed: {e}")
                    prompt = "Keep the energy up — say something entertaining or ask your viewers a question."
            else:
                prompt = "Keep the energy up — say something entertaining or ask your viewers a question."

            last_activity[0] = asyncio.get_event_loop().time()
            await task.queue_frames([
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": prompt}],
                    run_llm=True,
                )
            ])

    @transport.event_handler("on_joined")
    async def on_joined(_transport, _data):
        logger.info("[DAILY] joined room")

    @transport.event_handler("on_participant_joined")
    async def on_participant_joined(_transport, participant):
        if participant.get("info", {}).get("isLocal"):
            return
        name = participant.get("info", {}).get("userName", "someone")
        logger.info(f"[DAILY] viewer joined: {name}")
        last_activity[0] = asyncio.get_event_loop().time()
        await task.queue_frames([
            LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": f"A viewer named {name} just joined. Welcome them!"}],
                run_llm=True,
            )
        ])

    @transport.event_handler("on_app_message")
    async def on_app_message(_transport, message, sender):
        logger.debug(f"[APP MESSAGE][{sender}] {message}")
        text = None
        if isinstance(message, dict):
            text = message.get("message") or message.get("text") or message.get("data")
        elif isinstance(message, str):
            text = message

        if text:
            logger.info(f"[CHAT] {sender}: {text}")
            last_activity[0] = asyncio.get_event_loop().time()
            await task.queue_frames([
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": text}],
                    run_llm=True,
                )
            ])

    @transport.event_handler("on_participant_left")
    async def on_participant_left(_transport, participant, reason):
        logger.info(f"[DAILY] participant left: {participant.get('info', {}).get('userName')}")

    proactive_task = asyncio.create_task(proactive_loop())
    try:
        await runner.run(task)
    finally:
        proactive_task.cancel()
