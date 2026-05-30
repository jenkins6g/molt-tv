"""Pure-Python game-state accumulators consumed by the Pipecat pipeline.

- ``GameContextStore``  accumulates structured state from incoming
  ``server-message`` events and renders an LLM-injectable text block.
- ``GameTurnBuffer``    assembles what the Ship AI just said from its
  streaming ``bot-llm-*`` / ``bot-output`` / ``bot-tts-text`` /
  ``bot-stopped-speaking`` event sequence, returning a single utterance
  when the Ship AI is done. This utterance is what triggers the Officer's
  next LLM run.

Verbatim ports from chadbailey59/gb-bot bot-v5.py:
- GameContextStore     lines 174–522
- GameTurnBuffer       lines 914–1027  (lifted from inner-closure scope
                       to module level so it can be unit-tested + shared)
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional


COMMODITIES = (
    ("quantum_foam", "QF"),
    ("retro_organics", "RO"),
    ("neuro_symbolics", "NS"),
)


class GameContextStore:
    """Accumulates useful structured game state from server-message events."""

    def __init__(self):
        self._status: str | None = None
        self._ships: str | None = None
        self._map: str | None = None
        self._ports: str | None = None
        self._quests: str | None = None
        self._ui_summary: str | None = None
        self._recent_events: list[str] = []

    def handle_message(self, message: dict[str, Any]) -> None:
        if message.get("type") != "server-message":
            return

        data = message.get("data") or {}
        event = data.get("event")
        payload = data.get("payload") or {}
        if not isinstance(event, str) or not isinstance(payload, dict):
            return

        if event in ("status.snapshot", "status.update"):
            self._status = self._summarize_status(payload)
        elif event == "ships.list":
            self._ships = self._summarize_ships(payload)
        elif event == "map.local":
            self._map = self._summarize_map(payload)
        elif event == "ports.list":
            self._ports = self._summarize_ports(payload)
        elif event == "quest.status":
            self._quests = self._summarize_quests(payload)
        elif event == "ui-agent-context-summary":
            summary = payload.get("context_summary")
            if isinstance(summary, str) and summary.strip():
                self._ui_summary = summary.strip()
        elif self._is_recent_event(event):
            self._append_recent_event(event, self._summarize_generic_event(event, payload))

    def render(self) -> str:
        sections = [
            self._status,
            self._ships,
            self._ports,
            self._map,
            self._quests,
            self._format_recent_events(),
            self._format_ui_summary(),
        ]
        body = "\n".join(s for s in sections if s)
        if not body:
            return ""
        return f"Game context from server messages:\n{body}"

    # ---- summarizers -------------------------------------------------------

    def _append_recent_event(self, event: str, summary: str) -> None:
        if summary and (not self._recent_events or self._recent_events[-1] != summary):
            self._recent_events.append(summary)
            self._recent_events = self._recent_events[-6:]

    def _format_recent_events(self) -> str | None:
        if not self._recent_events:
            return None
        return "Recent events: " + " | ".join(self._recent_events)

    def _format_ui_summary(self) -> str | None:
        if not self._ui_summary:
            return None
        return f"UI summary: {self._ui_summary}"

    def _summarize_status(self, payload: dict[str, Any]) -> str:
        ship = self._dict(payload.get("ship"))
        sector = self._dict(payload.get("sector"))
        player = self._dict(payload.get("player"))
        port = self._dict(sector.get("port"))

        ship_bits = [
            self._clean(ship.get("ship_name")),
            f"type {ship.get('ship_type')}" if ship.get("ship_type") else None,
            f"sector {sector.get('id')}" if sector.get("id") is not None else None,
            self._clean(sector.get("region")),
            self._format_pair("warp", ship.get("warp_power"), ship.get("warp_power_capacity")),
            f"turns/warp {ship.get('turns_per_warp')}" if ship.get("turns_per_warp") else None,
            self._format_pair("shields", ship.get("shields"), ship.get("max_shields")),
            self._format_pair("fighters", ship.get("fighters"), ship.get("max_fighters")),
            f"ship credits {ship.get('credits')}" if ship.get("credits") is not None else None,
            (
                f"bank {player.get('credits_in_bank')}"
                if player.get("credits_in_bank") is not None
                else None
            ),
            self._format_cargo(ship),
        ]

        sector_bits = [
            self._format_port(port),
            self._format_adjacent(sector),
            self._format_presence("players", sector.get("players")),
            self._format_presence("salvage", sector.get("salvage")),
            self._format_garrison(sector.get("garrison")),
        ]

        return self._join_lines(
            "Current status: " + "; ".join(b for b in ship_bits if b),
            "Current sector: " + "; ".join(b for b in sector_bits if b),
        )

    def _summarize_ships(self, payload: dict[str, Any]) -> str | None:
        ships = self._list(payload.get("ships"))
        if not ships:
            return None

        summaries = []
        for ship in ships[:5]:
            if not isinstance(ship, dict):
                continue
            bits = [
                self._clean(ship.get("ship_name")) or self._clean(ship.get("ship_type")),
                f"sector {ship.get('sector')}" if ship.get("sector") is not None else None,
                self._format_pair("warp", ship.get("warp_power"), ship.get("warp_power_capacity")),
                f"credits {ship.get('credits')}" if ship.get("credits") is not None else None,
                self._format_cargo(ship),
                f"task {ship.get('current_task_id')}" if ship.get("current_task_id") else None,
                "destroyed" if ship.get("destroyed_at") else None,
            ]
            summaries.append("; ".join(b for b in bits if b))

        if not summaries:
            return None
        return "Owned ships: " + " | ".join(summaries)

    def _summarize_map(self, payload: dict[str, Any]) -> str | None:
        sectors = self._list(payload.get("sectors"))
        if not sectors:
            return None

        ports: list[str] = []
        megaports: list[str] = []
        garrisons: list[str] = []
        unvisited: list[str] = []
        one_way_lanes: list[str] = []
        for sector in sectors:
            if not isinstance(sector, dict):
                continue
            sector_id = sector.get("id")
            hops = sector.get("hops_from_center")
            port = self._dict(sector.get("port"))
            if port:
                label = f"{sector_id} ({hops} hops, {port.get('code')})"
                ports.append(label)
                if port.get("mega"):
                    megaports.append(label)
            if sector.get("garrison"):
                garrisons.append(f"{sector_id} ({hops} hops)")
            if sector.get("visited") is False:
                unvisited.append(f"{sector_id} ({hops} hops)")
            for lane in self._list(sector.get("lanes")):
                if isinstance(lane, dict) and lane.get("two_way") is False:
                    one_way_lanes.append(f"{sector_id}->{lane.get('to')}")

        bits = [
            f"center {payload.get('center_sector')}" if payload.get("center_sector") else None,
            self._format_limited("megaports", megaports, 4),
            self._format_limited("ports", ports, 10),
            self._format_limited("garrisons", garrisons, 5),
            self._format_limited("unvisited", unvisited, 8),
            self._format_limited("one-way lanes", one_way_lanes, 8),
        ]
        return "Local map: " + "; ".join(b for b in bits if b)

    def _summarize_ports(self, payload: dict[str, Any]) -> str | None:
        ports = self._list(payload.get("ports"))
        if not ports:
            return "Known ports: none found."

        summaries = []
        for entry in ports[:12]:
            if not isinstance(entry, dict):
                continue
            sector = self._dict(entry.get("sector"))
            port = self._dict(sector.get("port"))
            if not sector or not port:
                continue
            code = self._clean(port.get("code"))
            mega = " mega" if port.get("mega") else ""
            hops = entry.get("hops_from_start")
            prices = self._dict(port.get("prices"))
            stock = self._dict(port.get("stock"))
            trade = self._format_trade_code(code, prices)
            stock_text = self._format_stock(stock)
            summaries.append(f"{sector.get('id')} ({hops} hops,{mega} {code}): {trade}{stock_text}")

        if not summaries:
            return None
        return "Known ports near current sector: " + " | ".join(summaries)

    def _summarize_quests(self, payload: dict[str, Any]) -> str | None:
        quests = self._list(payload.get("quests"))
        if not quests:
            return "Quests: none active."

        summaries = []
        for quest in quests[:5]:
            if not isinstance(quest, dict):
                continue
            name = self._clean(quest.get("name") or quest.get("code") or quest.get("quest_id"))
            if name:
                summaries.append(name)
        if not summaries:
            return None
        return "Quests: " + "; ".join(summaries)

    def _summarize_generic_event(self, event: str, payload: dict[str, Any]) -> str:
        useful_keys = (
            "status", "sector", "from_sector", "to_sector",
            "commodity", "quantity", "profit", "credits",
            "warp_power", "task_id", "action", "result",
        )
        bits = [
            f"{key}={payload[key]}"
            for key in useful_keys
            if key in payload and self._is_scalar(payload[key])
        ]
        if bits:
            return f"{event}: " + ", ".join(bits)
        return event

    def _is_recent_event(self, event: str) -> bool:
        prefixes = (
            "task.", "trade.", "movement.", "combat.", "ship.",
            "bank.", "transfer.", "recharge.", "character.",
        )
        ignored = {"ship.speech_started", "ship.speech_stopped"}
        return event not in ignored and event.startswith(prefixes)

    # ---- formatters --------------------------------------------------------

    def _format_port(self, port: dict[str, Any]) -> str | None:
        code = self._clean(port.get("code"))
        if not code:
            return None
        mega = " mega" if port.get("mega") else ""
        return f"port{mega} {code}: {self._format_trade_code(code, self._dict(port.get('prices')))}"

    def _format_trade_code(self, code: str | None, prices: dict[str, Any]) -> str:
        parts = []
        for index, (key, label) in enumerate(COMMODITIES):
            action = code[index] if code and len(code) > index else "?"
            verb = "buys" if action == "B" else "sells" if action == "S" else "has"
            price = prices.get(key)
            parts.append(f"{verb} {label}@{price}" if price is not None else f"{verb} {label}")
        return ", ".join(parts)

    def _format_stock(self, stock: dict[str, Any]) -> str:
        if not stock:
            return ""
        values = [f"{label} {stock[key]}" for key, label in COMMODITIES if stock.get(key) is not None]
        return f"; stock {', '.join(values)}" if values else ""

    def _format_cargo(self, ship: dict[str, Any]) -> str | None:
        cargo = self._dict(ship.get("cargo"))
        if not cargo:
            return None
        capacity = ship.get("cargo_capacity")
        empty = ship.get("empty_holds")
        values = [f"{label} {cargo.get(key, 0)}" for key, label in COMMODITIES]
        prefix = f"cargo {', '.join(values)}"
        if capacity is not None:
            prefix += f" / {capacity}"
        if empty is not None:
            prefix += f" ({empty} empty)"
        return prefix

    def _format_adjacent(self, sector: dict[str, Any]) -> str | None:
        adjacent = self._dict(sector.get("adjacent_sectors"))
        if not adjacent:
            return None
        return "adjacent " + ", ".join(str(key) for key in adjacent.keys())

    def _format_garrison(self, garrison: object) -> str | None:
        if not garrison:
            return "no garrison"
        if isinstance(garrison, dict):
            owner = garrison.get("owner_name") or garrison.get("owner") or garrison.get("owner_id")
            mode = garrison.get("mode") or garrison.get("garrison_mode")
            fighters = garrison.get("fighters")
            bits = [
                self._clean(owner),
                self._clean(mode),
                f"fighters {fighters}" if fighters else None,
            ]
            return "garrison " + " ".join(b for b in bits if b)
        return "garrison present"

    def _format_presence(self, label: str, value: object) -> str | None:
        items = self._list(value)
        if not items:
            return f"no {label}"
        return f"{label} present ({len(items)})"

    def _format_pair(self, label: str, current: object, maximum: object) -> str | None:
        if current is None and maximum is None:
            return None
        if maximum is None:
            return f"{label} {current}"
        return f"{label} {current}/{maximum}"

    def _format_limited(self, label: str, values: list[str], limit: int) -> str | None:
        if not values:
            return None
        shown = values[:limit]
        suffix = f" (+{len(values) - limit} more)" if len(values) > limit else ""
        return f"{label}: {', '.join(shown)}{suffix}"

    # ---- coercion helpers --------------------------------------------------

    def _join_lines(self, *lines: str | None) -> str:
        return "\n".join(line for line in lines if line)

    def _dict(self, value: object) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _list(self, value: object) -> list[Any]:
        return value if isinstance(value, list) else []

    def _clean(self, value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _is_scalar(self, value: object) -> bool:
        return isinstance(value, (str, int, float, bool)) or value is None


class GameTurnBuffer:
    """Assembles what the Ship AI just said into one utterance.

    Consumes ``bot-llm-started`` / ``bot-llm-text`` / ``bot-llm-stopped`` /
    ``bot-transcription`` / ``bot-output`` / ``bot-tts-text`` /
    ``bot-stopped-speaking`` events, plus the ``server-message`` event
    ``ship.speech_stopped`` as an audio-mode fallback. Returns the assembled
    utterance text when it's ready to be queued as the Officer's next
    user-turn.

    Two flush modes (text vs audio):
    - ``flush_on_llm_stopped=True``: flush as soon as Ship AI's LLM finishes
      generating, ignoring the redundant TTS playback events that follow.
    - ``flush_on_llm_stopped=False``: wait for the Ship AI to actually stop
      speaking (audio playback complete), preferring transcriptions or
      sentence aggregates over raw words.
    """

    def __init__(self, flush_on_llm_stopped: bool = False):
        self._flush_on_llm_stopped = flush_on_llm_stopped
        self._reset()

    def _reset(self) -> None:
        self._collecting = False
        self._transcriptions: list[str] = []
        self._sentences: list[str] = []
        self._tts_words: list[str] = []
        self._llm_text: list[str] = []
        self._suppressed_until_next_llm = False

    def _ensure_collecting(self) -> bool:
        if self._suppressed_until_next_llm:
            return False
        if not self._collecting:
            self._reset()
            self._collecting = True
        return True

    def _append_unique(self, items: list[str], text: str) -> None:
        text = text.strip()
        if text and (not items or items[-1] != text):
            items.append(text)

    def handle_message(self, message: dict) -> Optional[str]:
        msg_type = message.get("type")
        data = message.get("data") or {}

        if msg_type == "bot-llm-started":
            self._reset()
            self._collecting = True
            return None

        if msg_type == "bot-llm-text":
            if not self._ensure_collecting():
                return None
            chunk = data.get("text", "")
            if chunk:
                self._llm_text.append(chunk)
            return None

        if msg_type == "bot-llm-stopped" and self._flush_on_llm_stopped:
            flushed = self.flush()
            self._suppressed_until_next_llm = True
            return flushed

        if msg_type == "bot-transcription":
            if not self._ensure_collecting():
                return None
            self._append_unique(self._transcriptions, data.get("text", ""))
            return None

        if msg_type == "bot-output":
            text = data.get("text", "")
            aggregated_by = data.get("aggregated_by")
            if not self._ensure_collecting():
                return None
            if aggregated_by == "sentence":
                self._append_unique(self._sentences, text)
            elif aggregated_by == "word" and data.get("spoken"):
                self._append_unique(self._tts_words, text)
            return None

        if msg_type == "bot-tts-text":
            if not self._ensure_collecting():
                return None
            self._append_unique(self._tts_words, data.get("text", ""))
            return None

        if msg_type == "bot-stopped-speaking":
            if self._suppressed_until_next_llm:
                return None
            return self.flush()

        if msg_type == "server-message" and data.get("event") == "ship.speech_stopped":
            if self._suppressed_until_next_llm:
                return None
            return self.flush()

        return None

    def flush(self) -> Optional[str]:
        if self._flush_on_llm_stopped and self._llm_text:
            text = "".join(self._llm_text)
        elif self._transcriptions:
            text = " ".join(self._transcriptions)
        elif self._sentences:
            text = " ".join(self._sentences)
        elif self._llm_text:
            text = "".join(self._llm_text)
        elif self._tts_words:
            text = " ".join(self._tts_words)
            text = re.sub(r"\s+([,.;:!?])", r"\1", text)
        else:
            text = ""

        self._reset()
        text = " ".join(text.split())
        return text or None
