"""molt-harness/agenda.py — LLM-powered agenda parsing and OpenClaw-style cron scheduling."""

from __future__ import annotations

import asyncio
import heapq
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger
from openai import AsyncOpenAI

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class AgendaItem:
    description: str
    duration_seconds: Optional[float]
    min_viewers: Optional[int]
    url: Optional[str]
    requires_game: bool = False


# ── LLM interpreter ────────────────────────────────────────────────────────────

_INTERPRETER_PROMPT = """\
You are a stream schedule parser. Convert the given agenda text into a JSON object.

Return ONLY valid JSON (no markdown), shaped exactly like this:
{
  "items": [
    {
      "description": "short plain-text label for this item",
      "duration_seconds": 300,
      "min_viewers": null,
      "url": null,
      "requires_game": false
    }
  ]
}

Rules:
- description: concise restatement of the item (1 sentence)
- duration_seconds: integer seconds if a duration is mentioned, else null
- min_viewers: integer if item says "wait for N people/viewers", else null
- url: full URL string if the item mentions a specific URL to visit, else null
- requires_game: true if the item involves playing Gradient Bang or any in-game activity, else false
- Preserve order from the input
- Every item must have all five fields (use null/false when not applicable)
"""


async def interpret_agenda(
    agenda_text: str, openai_client: AsyncOpenAI
) -> list[AgendaItem]:
    """Call GPT to parse free-form agenda text into a list of AgendaItems."""
    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _INTERPRETER_PROMPT},
            {"role": "user", "content": agenda_text},
        ],
        max_tokens=800,
    )
    raw = response.choices[0].message.content.strip()
    data = json.loads(raw)
    items = []
    for entry in data.get("items", []):
        items.append(AgendaItem(
            description=entry["description"],
            duration_seconds=entry.get("duration_seconds"),
            min_viewers=entry.get("min_viewers"),
            url=entry.get("url"),
            requires_game=bool(entry.get("requires_game", False)),
        ))
    return items


# ── Scheduler ──────────────────────────────────────────────────────────────────

_JOBS_FILE = Path(__file__).parent / ".agenda_jobs.json"

InjectFn = Callable[[str], Awaitable[None]]
ViewerCountFn = Callable[[], int]
GameLaunchFn = Callable[[], Awaitable[Any]]
PageReadyCb = Callable[[Any], None]


@dataclass
class _Job:
    """A scheduled agenda job. Sortable by run_at for the min-heap."""
    run_at: float   # monotonic timestamp
    index: int      # index into items list

    def __lt__(self, other: "_Job") -> bool:
        return (self.run_at, self.index) < (other.run_at, other.index)

    def to_dict(self) -> dict:
        # Store as wall-clock offset so the file is meaningful across restarts
        wall_time = time.time() + (self.run_at - time.monotonic())
        return {"index": self.index, "wall_time": wall_time}

    @classmethod
    def from_dict(cls, d: dict) -> "_Job":
        run_at = time.monotonic() + (d["wall_time"] - time.time())
        return cls(run_at=run_at, index=d["index"])


class AgendaScheduler:
    """
    OpenClaw-style scheduler: loads jobs into a min-heap, sleeps until the next
    scheduled time, re-arms whenever a new earlier job is pushed, and persists
    pending jobs to disk so a restart can resume mid-agenda.
    """

    def __init__(self, items: list[AgendaItem]) -> None:
        self._items = items
        self._heap: list[_Job] = []
        self._fired: set[int] = set()          # indices already executed (dedup)
        self._current_index: int = -1
        self._advance_event = asyncio.Event()  # unblocks plain-item waits
        self._wake_event = asyncio.Event()     # re-arms the sleep loop

    # ── Heap / persistence ────────────────────────────────────────────────────

    def _push(self, index: int, delay: float = 0.0) -> None:
        job = _Job(run_at=time.monotonic() + delay, index=index)
        heapq.heappush(self._heap, job)
        self._persist()
        self._wake_event.set()   # interrupt current sleep so it re-evaluates

    def _persist(self) -> None:
        try:
            _JOBS_FILE.write_text(json.dumps([j.to_dict() for j in self._heap], indent=2))
        except Exception as e:
            logger.warning(f"[AGENDA] Persist failed: {e}")

    def _load_from_disk(self) -> None:
        if not _JOBS_FILE.exists():
            return
        try:
            now = time.time()
            future_jobs = [
                d for d in json.loads(_JOBS_FILE.read_text())
                if d.get("wall_time", 0) > now
            ]
            if not future_jobs:
                logger.info("[AGENDA] Job file is stale (all past-due), starting fresh.")
                _JOBS_FILE.unlink(missing_ok=True)
                return
            for d in future_jobs:
                if d["index"] < len(self._items):
                    heapq.heappush(self._heap, _Job.from_dict(d))
            logger.info(f"[AGENDA] Resumed {len(self._heap)} future job(s) from disk.")
        except Exception as e:
            logger.warning(f"[AGENDA] Could not load jobs from disk: {e}")

    def _clear_disk(self) -> None:
        _JOBS_FILE.unlink(missing_ok=True)

    # ── Timer ─────────────────────────────────────────────────────────────────

    async def _sleep_until(self, target: float) -> None:
        """Sleep until target monotonic time. Wakes early if a new job is pushed."""
        while True:
            remaining = target - time.monotonic()
            if remaining <= 0:
                return
            self._wake_event.clear()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._wake_event.wait()),
                    timeout=remaining,
                )
                # Woken early — a new job was pushed; re-evaluate the heap
            except asyncio.TimeoutError:
                return

    # ── Advance (LLM tool hook) ───────────────────────────────────────────────

    def current_item(self) -> Optional[AgendaItem]:
        if 0 <= self._current_index < len(self._items):
            return self._items[self._current_index]
        return None

    def is_done(self) -> bool:
        return not self._heap and self._current_index >= len(self._items) - 1

    def signal_advance(self) -> None:
        """Called by the advance_agenda LLM tool. Schedules the next item immediately."""
        self._advance_event.set()
        next_i = self._current_index + 1
        if next_i < len(self._items):
            self._push(next_i, delay=0.0)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(
        self,
        inject: InjectFn,
        get_viewer_count: ViewerCountFn,
        game_launch_fn: Optional[GameLaunchFn] = None,
        on_page_ready: Optional[PageReadyCb] = None,
        web_launch_fn: Optional[GameLaunchFn] = None,
    ) -> None:
        game_launched = False
        web_launched = False

        # Restore from disk or kick off with item 0
        self._load_from_disk()
        if not self._heap and self._items:
            self._push(0, delay=0.0)

        while self._heap:
            # Sleep until the next scheduled job
            await self._sleep_until(self._heap[0].run_at)

            # Re-check after wakeup: heap may have changed
            if not self._heap:
                break
            if self._heap[0].run_at > time.monotonic() + 0.05:
                continue  # a later job moved to the front during sleep; loop

            job = heapq.heappop(self._heap)
            self._persist()

            if job.index in self._fired:
                continue   # duplicate from an early signal_advance; skip
            self._fired.add(job.index)
            self._current_index = job.index

            i = job.index
            item = self._items[i]
            logger.info(f"[AGENDA] Firing item {i + 1}/{len(self._items)}: {item.description}")

            # Launch the right browser before injecting context
            if item.requires_game and not game_launched and game_launch_fn:
                logger.info("[AGENDA] Launching game for this item...")
                page = await game_launch_fn()
                if on_page_ready and page is not None:
                    on_page_ready(page)
                game_launched = True
                web_launched = True  # game browser doubles as web browser
            elif item.url and not item.requires_game and not web_launched and not game_launched and web_launch_fn:
                logger.info("[AGENDA] Launching web browser for this item...")
                page = await web_launch_fn()
                if on_page_ready and page is not None:
                    on_page_ready(page)
                web_launched = True

            await inject(
                f"[AGENDA {i + 1}/{len(self._items)}] Now starting: {item.description}"
                + (f" | Target URL: {item.url}" if item.url else "")
            )

            # Viewer gate
            if item.min_viewers:
                logger.info(f"[AGENDA] Waiting for {item.min_viewers} viewers...")
                while get_viewer_count() < item.min_viewers:
                    await asyncio.sleep(5)
                logger.info(f"[AGENDA] Viewer gate passed ({get_viewer_count()} viewers).")
                await inject(
                    f"[AGENDA] {get_viewer_count()} viewers here! Proceeding: {item.description}"
                )

            # Schedule the next job
            next_i = i + 1
            if next_i < len(self._items):
                if item.duration_seconds:
                    self._push(next_i, delay=item.duration_seconds)
                elif item.min_viewers and not item.duration_seconds:
                    # Viewer-gated only: proceed immediately after gate passes
                    self._push(next_i, delay=0.0)
                else:
                    # Plain item: block until the LLM calls advance_agenda
                    # (signal_advance will push next_i itself)
                    logger.info("[AGENDA] Plain item — waiting for advance_agenda...")
                    self._advance_event.clear()
                    await self._advance_event.wait()

        self._clear_disk()
        await inject("[AGENDA] All agenda items complete. Stream freely!")
        logger.info("[AGENDA] All items done.")
