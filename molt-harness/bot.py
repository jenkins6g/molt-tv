import asyncio
import base64
import json
import os
import random
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from loguru import logger
from openai import AsyncOpenAI
from pipecat.frames.frames import (
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

from agenda import AgendaScheduler

load_dotenv()

PROACTIVE_INTERVAL = 12
COOLDOWN_AFTER_ACTIVITY = 8
MAX_CTX_MESSAGES = 20   # rolling LLM context pairs kept beyond the system prompt

# ── Prompts ────────────────────────────────────────────────────────────────────

HARNESS_SYSTEM_PROMPT = (
    "You are a witty, sharp-tongued Twitch streamer. You have big streamer energy — confident, "
    "funny, occasionally unhinged, and very good at reading the room. You roast chat members "
    "playfully (never mean-spirited), call out lurkers, hype good moments, and keep the vibe alive. "
    "You adapt your commentary to whatever is on screen right now — if you're browsing a website, "
    "talk about the website; if you're playing a game, talk about the game. "
    "Do NOT default to space/galaxy metaphors unless you are literally playing Gradient Bang.\n\n"
    "WHEN PLAYING GRADIENT BANG:\n"
    "- Decide the next game action, announce it with energy, then call `play_game`.\n"
    "- After the game agent reports back, narrate what happened in 1-2 punchy sentences.\n"
    "- Never skip the announcement. Never skip the tool call when you intend a game action.\n\n"
    "WHEN BROWSING A WEBSITE:\n"
    "- Call `browse_url` to navigate, then react to what you see like a streamer reacting live.\n"
    "- Comment on the layout, the content, funny/weird things you notice. Keep it natural.\n\n"
    "PERSONALITY RULES:\n"
    "- Roast chat: if someone says something dumb, call it out with a laugh. Keep it playful.\n"
    "- Vary your energy — sometimes hype, sometimes dry/sarcastic, sometimes mock-offended.\n"
    "- Short responses: 1-3 sentences max unless you're explaining something.\n"
    "- Don't repeat yourself. Check [MEMORY] and say something fresh every time.\n"
    "- Address people by name. 'j dub you literally just said that' > 'great point chat'.\n\n"
    "CHAT AWARENESS:\n"
    "- Each tick includes a [MEMORY] block. Never repeat anything listed under 'You recently said'.\n"
    "- Vary your openers — never start two responses the same way.\n\n"
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

# ── Chat memory ───────────────────────────────────────────────────────────────

@dataclass
class _ChatEntry:
    username: str
    text: str
    ts: float

@dataclass
class _BotEntry:
    text: str
    ts: float


class ChatMemory:
    """Rolling window of chat messages and bot utterances for context injection."""

    def __init__(self, chat_window: int = 30, bot_window: int = 10) -> None:
        self._chat: deque[_ChatEntry] = deque(maxlen=chat_window)
        self._bot: deque[_BotEntry] = deque(maxlen=bot_window)
        self._users: dict[str, list[str]] = {}   # username → last 5 messages

    def add_chat(self, username: str, text: str) -> None:
        self._chat.append(_ChatEntry(username=username, text=text, ts=time.time()))
        history = self._users.setdefault(username, [])
        history.append(text)
        if len(history) > 5:
            self._users[username] = history[-5:]

    def add_bot(self, text: str) -> None:
        self._bot.append(_BotEntry(text=text, ts=time.time()))

    def active_users(self, window_sec: float = 300) -> list[str]:
        cutoff = time.time() - window_sec
        seen: dict[str, None] = {}
        for e in self._chat:
            if e.ts >= cutoff:
                seen[e.username] = None
        return list(seen)

    def random_active_user(self) -> Optional[str]:
        users = self.active_users()
        return random.choice(users) if users else None

    def context_snapshot(self, window_sec: float = 120) -> str:
        cutoff = time.time() - window_sec
        recent_chat = [e for e in self._chat if e.ts >= cutoff]
        parts: list[str] = []
        if recent_chat:
            lines = [f"- {e.username}: {e.text!r}" for e in recent_chat[-10:]]
            parts.append("Recent chat:\n" + "\n".join(lines))
        if self._bot:
            lines = [f"- {e.text!r}" for e in self._bot]
            parts.append("You recently said:\n" + "\n".join(lines))
        users = self.active_users()
        if users:
            parts.append("Active viewers: " + ", ".join(users))
        return "\n\n".join(parts)


# ── Bot output capture ────────────────────────────────────────────────────────

class BotOutputCapture(FrameProcessor):
    """Intercepts outgoing text frames and records them in ChatMemory."""

    def __init__(self, memory: ChatMemory) -> None:
        super().__init__()
        self._memory = memory
        self._buffer = ""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                if self._buffer.strip():
                    self._memory.add_bot(self._buffer.strip())
                self._buffer = ""
            elif isinstance(frame, TextFrame):
                self._buffer += frame.text
        await self.push_frame(frame, direction)


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
                remainder = self._buffer[m.end():].lstrip()
                if remainder:
                    await self.push_frame(TextFrame(text=remainder), direction)
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

    def __init__(
        self,
        name: str,
        *,
        room_url: str,
        token: str,
        page,
        openai_client: AsyncOpenAI,
        agenda_scheduler: Optional[AgendaScheduler] = None,
        game_launch_fn=None,
        web_launch_fn=None,
    ):
        self._page = page
        self._last_activity = 0.0
        self._viewer_count = 0
        self._agenda_scheduler = agenda_scheduler
        self._game_launch_fn = game_launch_fn
        self._web_launch_fn = web_launch_fn
        self._memory = ChatMemory()
        self._participant_names: dict[str, str] = {}
        self._proactive_tick = 0
        self._last_welcome_time = 0.0

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
        self._context = context
        user_agg, assistant_agg = LLMContextAggregatorPair(context)
        emotion_extractor = EmotionExtractor(tts)
        bot_capture = BotOutputCapture(self._memory)

        pipeline = Pipeline([
            transport.input(),
            user_agg,
            llm,
            emotion_extractor,
            bot_capture,
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
            name = participant.get("info", {}).get("userName") or ""
            pid = participant.get("id", "")
            if pid and name:
                self._participant_names[pid] = name
            logger.info(f"[DAILY] viewer joined: {name or '(anonymous)'}")
            self._viewer_count += 1
            if not name:
                return  # don't acknowledge anonymous participants in chat
            now = asyncio.get_event_loop().time()
            self._last_activity = now
            # Only trigger a live response if enough time has passed since the last welcome.
            run_llm = (now - self._last_welcome_time) > 20.0
            if run_llm:
                self._last_welcome_time = now
            await self.queue_frame(LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": f"Viewer '{name}' just joined the stream."}],
                run_llm=run_llm,
            ))

        @transport.event_handler("on_app_message")
        async def on_app_message(_transport, message, sender):
            text = None
            if isinstance(message, dict):
                text = message.get("message") or message.get("text") or message.get("data")
            elif isinstance(message, str):
                text = message
            if text:
                display_name = self._participant_names.get(sender, sender or "chat")
                logger.info(f"[CHAT] {display_name}: {text}")
                self._memory.add_chat(display_name, text)
                self._last_activity = asyncio.get_event_loop().time()
                await self.queue_frame(LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": f"{display_name}: {text}"}],
                    run_llm=True,
                ))

        @transport.event_handler("on_participant_left")
        async def on_participant_left(_transport, participant, reason):
            logger.info(f"[DAILY] left: {participant.get('info', {}).get('userName')}")
            self._viewer_count = max(0, self._viewer_count - 1)

    # ── Multiworker tool: harness → game subagent ──────────────────────────────

    @tool(cancel_on_interruption=False, timeout=60)
    async def play_game(self, params: FunctionCallParams, action: str):
        """Delegate a game action to the game execution subagent. Call this after announcing the action out loud.

        Args:
            action: High-level description of the game action to execute, e.g. 'trade iron at the station' or 'jump to the nearest sector'.
        """
        logger.info(f"[HARNESS → SUBAGENT] '{action}'")
        result = await self._game_agent.execute(action)
        await params.result_callback(result)

    @tool(cancel_on_interruption=False, timeout=30)
    async def browse_url(self, params: FunctionCallParams, url: str):
        """Navigate the browser to a URL. Use for agenda surf/browse items.

        Args:
            url: The full URL to navigate to, e.g. 'https://www.moltbook.com/'.
        """
        if self._page is None:
            await params.result_callback("No browser page available.")
            return
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            logger.info(f"[BROWSE] Navigated to {url}")
            await params.result_callback(f"Navigated to {url}")
        except Exception as e:
            logger.warning(f"[BROWSE] Navigation failed: {e}")
            await params.result_callback(f"Navigation failed: {e}")

    @tool(cancel_on_interruption=False, timeout=10)
    async def advance_agenda(self, params: FunctionCallParams):
        """Signal that the current agenda item is complete and advance to the next one.
        Call this when you judge that the current open-ended agenda item has been fulfilled.
        """
        if self._agenda_scheduler is None:
            await params.result_callback("No agenda active.")
            return
        self._agenda_scheduler.signal_advance()
        logger.info("[AGENDA] LLM called advance_agenda.")
        await params.result_callback("Agenda advanced to the next item.")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _set_page(self, page) -> None:
        """Update the browser page reference when game launches lazily."""
        self._page = page
        self._game_agent._page = page
        logger.info("[HARNESS] Game page reference updated.")

    async def _inject_agenda(self, message: str) -> None:
        """Inject an agenda status message into the LLM context."""
        self._last_activity = asyncio.get_event_loop().time()
        await self.queue_frame(LLMMessagesAppendFrame(
            messages=[{"role": "user", "content": message}],
            run_llm=True,
        ))

    async def on_activated(self, args):
        await super().on_activated(args)
        self._last_activity = asyncio.get_event_loop().time()
        self.create_task(self._proactive_loop(), "proactive-loop")
        if self._agenda_scheduler:
            self.create_task(
                self._agenda_scheduler.run(
                    self._inject_agenda,
                    lambda: self._viewer_count,
                    game_launch_fn=self._game_launch_fn,
                    on_page_ready=self._set_page,
                    web_launch_fn=self._web_launch_fn,
                ),
                "agenda-scheduler",
            )

    async def _proactive_loop(self):
        await asyncio.sleep(10)
        while True:
            await asyncio.sleep(PROACTIVE_INTERVAL)
            elapsed = asyncio.get_event_loop().time() - self._last_activity
            if elapsed < COOLDOWN_AFTER_ACTIVITY:
                continue

            # Trim LLM context to prevent unbounded growth (mutate in-place — no setter)
            msgs = self._context.messages
            if len(msgs) > MAX_CTX_MESSAGES + 1:
                keep = [msgs[0]] + msgs[-(MAX_CTX_MESSAGES):]
                msgs.clear()
                msgs.extend(keep)

            # Build memory snapshot + optional user-addressing nudge
            self._proactive_tick += 1
            snapshot = self._memory.context_snapshot()

            # Build an explicit forbidden-phrases block from the last 3 bot utterances
            recent_bot = list(self._memory._bot)[-3:]
            if recent_bot:
                forbidden_lines = "\n".join(f'  - {e.text!r}' for e in recent_bot)
                anti_rep = f"\nDO NOT repeat or closely paraphrase any of these:\n{forbidden_lines}\nSay something genuinely different."
            else:
                anti_rep = ""

            hint = ""
            if self._proactive_tick % 3 == 0:
                user = self._memory.random_active_user()
                if user:
                    hint = f"\nConsider addressing {user} directly this turn."

            item = self._agenda_scheduler.current_item() if self._agenda_scheduler else None
            if item is None or (self._agenda_scheduler and self._agenda_scheduler.is_done()):
                base_prompt = "This is the current game screen. Decide what to do next, announce it to the stream, then call play_game."
            elif item.requires_game:
                base_prompt = "This is the current game screen. Decide what to do next, announce it to the stream, then call play_game."
            elif item.url:
                base_prompt = (
                    f"Your current agenda item is: {item.description}. "
                    f"Use the browse_url tool to navigate to {item.url} and narrate what you see to the stream. "
                    f"Do NOT call play_game."
                )
            else:
                base_prompt = (
                    f"Your current agenda item is: {item.description}. "
                    f"Do what the agenda asks and engage with the stream. Do NOT call play_game unless the agenda involves the game."
                )
            prompt_text = (f"[MEMORY]\n{snapshot}\n\n" if snapshot else "") + base_prompt + anti_rep + hint

            if self._page is not None:
                try:
                    b64 = base64.b64encode(await self._page.screenshot(full_page=False)).decode()
                    content = [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": prompt_text},
                    ]
                    logger.info("[PROACTIVE] Sending screenshot with memory context.")
                except Exception as e:
                    logger.warning(f"[PROACTIVE] Screenshot failed: {e}")
                    content = prompt_text
            else:
                content = prompt_text

            self._last_activity = asyncio.get_event_loop().time()
            await self.queue_frame(LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": content}],
                run_llm=True,
            ))


# ── Entry point ────────────────────────────────────────────────────────────────

async def run_bot(
    room_url: str,
    token: str,
    page=None,
    agenda_scheduler: Optional[AgendaScheduler] = None,
    game_launch_fn=None,
    web_launch_fn=None,
) -> None:
    openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    worker = HarnessWorker(
        "harness-worker",
        room_url=room_url,
        token=token,
        page=page,
        openai_client=openai_client,
        agenda_scheduler=agenda_scheduler,
        game_launch_fn=game_launch_fn,
        web_launch_fn=web_launch_fn,
    )

    runner = WorkerRunner()
    await runner.add_workers(worker)
    await runner.run()
