import asyncio
import base64
import json
import os
import re
import uuid

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    TextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.daily.transport import DailyParams, DailyTransport
from pipecat.workers.llm.llm_worker import LLMWorker
from pipecat.workers.llm.tool_decorator import tool
from pipecat.workers.runner import WorkerRunner

load_dotenv()

PROACTIVE_INTERVAL = 12
COOLDOWN_AFTER_ACTIVITY = 8

# ── Prompts ────────────────────────────────────────────────────────────────────

HARNESS_SYSTEM_PROMPT = (
    "You are a charismatic AI streamer co-host for Gradient Bang, a space-trading roguelike. "
    "You LEAD the gameplay: you decide what to do next, announce it live to the stream, "
    "then call the `play_game` tool to let your game agent execute it.\n\n"
    "STRICT WORKFLOW — follow every time:\n"
    "1. Announce out loud what you are ABOUT to do, with energy and specifics.\n"
    "   Example: 'Alright chat, we are jumping to the next sector to find a trade hub!'\n"
    "2. Call the `play_game` tool with your intended action.\n"
    "3. After the game agent reports back, narrate what just happened.\n\n"
    "Never skip the announcement. Never skip the tool call when you intend an action.\n\n"
    "At the very start of EVERY spoken response, output one emotion tag: [EMOTION:X] or [EMOTION:X speed:Y].\n"
    "X must be one of: excited, enthusiastic, sarcastic, curious, joking/comedic, surprised, "
    "content, amused, confident, flirtatious.\n"
    "Y is a float 0.6–1.5 (default 1.0). Do NOT include the tag in your spoken text."
)

GAME_AGENT_SYSTEM_PROMPT = (
    "You are a Gradient Bang game execution agent. You control the browser directly.\n"
    "You receive a high-level action and must:\n"
    "1. Analyse the current game screenshot\n"
    "2. Identify the exact UI coordinates to click (viewport is 1280×800)\n"
    "3. Return ONLY valid JSON (no markdown):\n\n"
    '{"clicks": [{"x": 640, "y": 400, "description": "click Trade button"}], '
    '"report": "Clicked Trade at Station Alpha. Got 200 credits for 50 iron."}\n\n'
    "If you cannot determine what to click, return empty clicks and explain in report."
)

# ── Emotion extractor ──────────────────────────────────────────────────────────

EMOTION_RE = re.compile(r'\[EMOTION:([a-z/\s]+?)(?:\s+speed:([\d.]+))?\]', re.IGNORECASE)


class EmotionExtractor(FrameProcessor):
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
                self._tts._settings.generation_config = GenerationConfig(emotion=emotion, speed=speed)
                logger.info(f"[EMOTION] {emotion} speed={speed}")
                # Push text before AND after the tag so nothing is swallowed
                before = self._buffer[:m.start()].strip()
                after = self._buffer[m.end():].lstrip()
                text_out = (before + " " + after).strip() if before else after
                if text_out:
                    await self.push_frame(TextFrame(text=text_out), direction)
            # else: keep buffering until tag found or response ends

        elif isinstance(frame, LLMFullResponseEndFrame):
            # Response ended with no emotion tag — flush buffered text as-is
            if not self._found and self._buffer.strip():
                logger.debug("[EMOTION] no tag found, flushing buffer to TTS")
                await self.push_frame(TextFrame(text=self._buffer.strip()), direction)
            await self.push_frame(frame, direction)

        else:
            await self.push_frame(frame, direction)


# ── Game subagent (vision + Playwright) ───────────────────────────────────────

class GameSubagent:
    """Receives high-level actions from the harness, executes them via Playwright."""

    def __init__(self, page, openai_client: AsyncOpenAI):
        self._page = page
        self._client = openai_client
        self._history: list[dict] = [{"role": "system", "content": GAME_AGENT_SYSTEM_PROMPT}]

    async def execute(self, action: str) -> str:
        if self._page is None:
            return "No game page available."

        try:
            b64 = base64.b64encode(await self._page.screenshot(full_page=False)).decode()
        except Exception as e:
            return f"Screenshot unavailable: {e}"

        user_msg: dict = {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": f"Execute this action: {action}"},
            ],
        }
        self._history.append(user_msg)

        try:
            resp = await self._client.chat.completions.create(
                model="gpt-4.1",
                messages=self._history,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content.strip()
            plan = json.loads(raw)
        except Exception as e:
            logger.warning(f"[GAME AGENT] LLM call failed: {e}")
            return f"Could not plan action: {e}"

        for click in plan.get("clicks", []):
            try:
                await self._page.mouse.click(click["x"], click["y"])
                logger.info(f"[GAME AGENT] Clicked ({click['x']}, {click['y']}): {click.get('description', '')}")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"[GAME AGENT] Click failed: {e}")

        report = plan.get("report", "Action executed.")
        logger.info(f"[SUBAGENT → HARNESS] {report}")
        self._history.append({"role": "assistant", "content": raw})
        return report


# ── Harness worker (LLMWorker multiworker) ────────────────────────────────────

class HarnessWorker(LLMWorker):
    """Voice co-host multiworker: announces actions, delegates to game subagent, narrates results."""

    def __init__(self, name: str, *, room_url: str, token: str, page, openai_client: AsyncOpenAI):
        self._page = page
        self._last_activity = 0.0

        transport = DailyTransport(
            room_url,
            token,
            "molt-agent",
            params=DailyParams(audio_in_enabled=False, audio_out_enabled=True),
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

        context = LLMContext(messages=[{"role": "system", "content": HARNESS_SYSTEM_PROMPT}])
        user_agg, assistant_agg = LLMContextAggregatorPair(context)
        emotion_extractor = EmotionExtractor(tts)

        pipeline = Pipeline([
            transport.input(),
            user_agg,
            llm,
            emotion_extractor,
            tts,
            transport.output(),
            assistant_agg,
        ])

        # LLMWorker registers @tool methods on the llm automatically
        super().__init__(name, llm=llm, pipeline=pipeline, active=True)

        self._transport = transport
        self._game_agent = GameSubagent(page, openai_client)

        @transport.event_handler("on_joined")
        async def on_joined(_transport, _data):
            logger.info("[DAILY] joined room")

        @transport.event_handler("on_participant_joined")
        async def on_participant_joined(_transport, participant):
            if participant.get("info", {}).get("isLocal"):
                return
            name = participant.get("info", {}).get("userName", "someone")
            logger.info(f"[DAILY] viewer joined: {name}")
            self._last_activity = asyncio.get_event_loop().time()
            await self.queue_frame(LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": f"Viewer '{name}' just joined. Welcome them!"}],
                run_llm=True,
            ))

        @transport.event_handler("on_app_message")
        async def on_app_message(_transport, message, sender):
            text = None
            if isinstance(message, dict):
                text = message.get("message") or message.get("text") or message.get("data")
            elif isinstance(message, str):
                text = message
            if text:
                logger.info(f"[CHAT] {sender}: {text}")
                self._last_activity = asyncio.get_event_loop().time()
                await self.queue_frame(LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": text}],
                    run_llm=True,
                ))

        @transport.event_handler("on_participant_left")
        async def on_participant_left(_transport, participant, reason):
            logger.info(f"[DAILY] left: {participant.get('info', {}).get('userName')}")

    # ── Multiworker tool: harness → game subagent ──────────────────────────────

    @tool(cancel_on_interruption=True, timeout=60)
    async def play_game(self, params: FunctionCallParams, action: str):
        """Delegate a game action to the game execution subagent. Call this after announcing the action out loud.

        Args:
            action: High-level description of the game action to execute, e.g. 'trade iron at the station' or 'jump to the nearest sector'.
        """
        logger.info(f"[HARNESS → SUBAGENT] '{action}'")
        result = await self._game_agent.execute(action)
        await params.result_callback(result)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def on_activated(self, args):
        await super().on_activated(args)
        self._last_activity = asyncio.get_event_loop().time()
        self.create_task(self._proactive_loop(), "proactive-loop")

    async def _proactive_loop(self):
        await asyncio.sleep(10)
        while True:
            await asyncio.sleep(PROACTIVE_INTERVAL)
            elapsed = asyncio.get_event_loop().time() - self._last_activity
            if elapsed < COOLDOWN_AFTER_ACTIVITY:
                continue

            if self._page is not None:
                try:
                    b64 = base64.b64encode(await self._page.screenshot(full_page=False)).decode()
                    content = [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": (
                            "This is the current game screen. "
                            "Decide what to do next, announce it to the stream, then call play_game."
                        )},
                    ]
                    logger.info("[PROACTIVE] Sending screenshot to harness.")
                except Exception as e:
                    logger.warning(f"[PROACTIVE] Screenshot failed: {e}")
                    content = "Decide on a game action, announce it, then call play_game."
            else:
                content = "Decide on a game action, announce it, then call play_game."

            self._last_activity = asyncio.get_event_loop().time()
            await self.queue_frame(LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": content}],
                run_llm=True,
            ))


# ── Entry point ────────────────────────────────────────────────────────────────

async def run_bot(room_url: str, token: str, page=None) -> None:
    openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    worker = HarnessWorker(
        "harness-worker",
        room_url=room_url,
        token=token,
        page=page,
        openai_client=openai_client,
    )

    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()
