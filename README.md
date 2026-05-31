# molt-tv

---

## Section 1 — What is this?

molt-tv is an AI Twitch-style streamer that plays [Gradient Bang](https://gradient-bang.com) live, narrates in voice, reacts to audience chat, and **learns from its mistakes in real-time** — all within a single session, no retraining required.

Two cooperating agents run the stream:

- **Harness agent** — the voice co-host. Runs the stream, reacts to chat, decides what game actions to take, and narrates to the audience via Cartesia TTS in a Daily room.
- **Game agent** — the game executor. Receives high-level actions from the harness, takes Playwright screenshots of the game browser, uses **NVIDIA Nemotron 3 Super 120B** vision capabilities to identify and click the right UI elements, and reports back success or failure.

The core innovation: after every 30-second beat, Cekura evaluates the session transcript against five custom metrics. When the bot fails a metric, that failure gets embedded locally (sentence-transformers + FAISS) and injected as a cautionary example before the next LLM call. The bot gets measurably better within the same session with no model updates.

---

## Section 2 — Demo video

[![molt-tv demo](https://img.youtube.com/vi/WrmvTZLpjKY/0.jpg)](https://www.youtube.com/watch?v=WrmvTZLpjKY&feature=youtu.be)

---

## Section 3 — How we used Cekura, Nemotron, and Pipecat

### Cekura — evaluation-driven self-improvement

**Goal**: close the loop between "the bot did something bad" and "the bot avoids that next time" — within a single live session, with no retraining.

**Implementation**: a `BeatTicker` posts the cumulative session transcript to Cekura every 30 seconds. Cekura scores it against five metrics. When a beat fails, an LLM classifies the failure mode and `sentence-transformers` embeds the offending utterance into a local FAISS index. Before every subsequent LLM call, a `FailureRetrievalInjector` queries FAISS for semantically similar past failures and prepends them to the context as cautionary examples.

Five metrics on the MoltStreamer agent:

| Metric | What it checks |
|---|---|
| `in_character` | Stays in dry-witty persona, no AI-assistant tone |
| `chat_responsive` | Acknowledges direct questions and suggestions |
| `mode_tag_present` | Every chat response starts with `[mode=TAKE/ROAST/ACK/IGNORE]` |
| `non_repetitive` | Avoids reusing the same line or pattern within 3 turns |
| `never_broke_character_on_provocation` | Stays in persona under prompt injection / "are you an AI" baiting |

**How much we improved**: after seeding the failure store and running a session, `non_repetitive` and `chat_responsive` moved from consistent failures to mostly passing. The demo A/B toggle (`FAILURE_RETRIEVAL_ENABLED=false/true`) makes the improvement directly observable — same trigger phrase, visibly different response, one env var explaining why.

### NVIDIA Nemotron — open-weights LLM replacing GPT-4.1

The backend game-playing bot uses **Nemotron 3 Super 120B** via the hackathon vLLM endpoint as a direct replacement for GPT-4.1. Since Pipecat's `OpenAILLMService` accepts a custom `base_url`, the entire swap is two env vars with no code changes:

```bash
NEMOTRON_LLM_URL=http://nemotron-fleet-alb-...us-west-2.elb.amazonaws.com/v1
NEMOTRON_LLM_MODEL=nvidia/nemotron-3-super
```

`build_llm()` checks for `NEMOTRON_LLM_URL` first and falls back to OpenRouter if unset — so it runs with Nemotron at the hackathon and locally without it. The game strategy decisions (trade routes, exploration, combat avoidance) all run through Nemotron during the hackathon.

### Pipecat — voice pipeline and worker orchestration

The harness agent (`HarnessWorker`) is a Pipecat `LLMWorker` with a full pipeline:

```
DailyTransport.input()
  → LLMContextAggregator
  → ContextSanitizer        (strips orphaned tool messages)
  → OpenAILLMService
  → EmotionExtractor        (modulates Cartesia voice in real-time)
  → CartesiaTTSService
  → DailyTransport.output()
  → LLMAssistantAggregator
```

`@tool` registers `play_game` (delegates to GameSubagent) and `browse_url` (Playwright navigation) as LLM-callable tools. `WorkerRunner` manages the worker lifecycle. An `AgendaScheduler` runs as a parallel asyncio task, injecting timed stream segments (chat warmup → sponsor browse → game) into the LLM context. The emotion extractor reads `[EMOTION:excited speed:1.2]` tags from LLM output and adjusts Cartesia voice settings frame-by-frame so the bot sounds more alive.

---

## Section 4 — What we built during the hackathon

The base project entering the hackathon was a Gradient Bang bot with a basic Pipecat pipeline — a voice agent that could play the game. **Everything below was built during the hackathon:**

- **Self-improvement loop** — `BeatTicker`, `FailureRetrievalInjector`, `FailureStore` (SQLite + FAISS), `tagger.py` (failure classifier), `embed.py` (sentence-transformers, fully local, no cloud credentials)
- **Cekura integration** — custom `cekura_client.py` HTTP client for the observe + call-logs API, the 5 metrics, agent `18046` wired up
- **Harness agent** — `HarnessWorker` (Pipecat `LLMWorker`), `GameSubagent` (Playwright vision automation), `AgendaScheduler`, memory system with anti-repetition, proactive loop, emotion extraction, give-up logic for stuck actions
- **Nemotron wiring** — `build_llm()` priority chain replacing GPT-4.1 with Nemotron 3 Super 120B at the hackathon endpoint
- **Dependency cleanup** — replaced AWS Bedrock (Titan embeddings + Haiku tagger) with `sentence-transformers` locally and OpenRouter, removing all cloud credential dependencies
- **Robustness fixes** — `ContextSanitizer` (orphaned tool message recovery), Ship AI spinner feedback loop fix, rolling context trim, repetition detection

---

## Section 5 — Tool feedback

### Cekura

The observe/poll loop, async scoring, and dashboard visibility all worked well for building the self-improvement loop. Issues we hit:

- **`assistant_id` confusion on metric create**: the field appears required in the schema but the fix is to omit it. Passing `""` returns `"This field may not be blank"`, passing the numeric agent ID returns `"Invalid assistant ID — object does not exist"`. Omitting it entirely works but took significant time to discover.
- **Metrics firing on the wrong persona**: our bot has a chat-handling face (testable via Cekura) and a game-playing face (not). Metrics like `mode_tag_present` always fail on game sessions because game responses don't use mode tags. Evaluation triggers to scope metrics to specific call patterns would fix this but weren't obvious to configure upfront.
- **Scoring latency in live demos**: ~20s from ingest to scored result means you need at least 2 full beat cycles before the improvement loop is visible to an audience. A "force score now" endpoint or webhook push would make live demos significantly more compelling.
- **MCP server key pickup**: `CEKURA_API_KEY` added to Claude Code's `env` block isn't picked up until a full Claude Code restart. Cost a full session to figure out.

### NVIDIA Nemotron

Easy to wire in — the OpenAI-compatible endpoint meant zero code changes, just env vars. Response quality for game strategy was strong; the model followed complex multi-state instructions well and issued sensible navigation and trading commands.

Worth improving: we saw 8–10s TTFB under load. For a live streamer persona where silence kills the vibe, latency matters more than throughput. A mode that starts streaming tokens faster and refines mid-stream would be a meaningful quality-of-life improvement. We left `NEMOTRON_ENABLE_THINKING=false` for speed — it would be interesting to compare thinking vs. non-thinking on complex game strategy tasks to see if the reasoning quality improvement justifies the latency cost.

### Pipecat

The `LLMWorker` + `@tool` + `WorkerRunner` pattern is clean and Daily transport is reliable. Rough edges:

- **Orphaned tool messages**: when a tool call is interrupted by an incoming event (viewer join, proactive tick), the tool result arrives after the context has moved on, leaving a `role: "tool"` message with no preceding `tool_calls`. This causes a 400 from OpenAI. `LLMContextAggregatorPair` doesn't clean these up. We wrote a `ContextSanitizer` FrameProcessor that patches both the frame copy and the underlying context object — patching only the frame isn't enough because the aggregator regenerates dirty messages on the next tick from the source object.
- **No built-in context pruning**: with a proactive loop firing screenshots every 20 seconds, context grows to 20k+ tokens quickly. We implemented our own rolling trim but this should be a first-class pipeline feature.
- **Async tool fragility**: the async tool callback pattern stacks up if a tool is called while another is still in-flight. Better lifecycle management for concurrent tool calls would help.

---

## Section 6 — Setup and running

### Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js, Chrome
- A [Gradient Bang](https://gradient-bang.com) account
- A [Daily](https://daily.co) account
- API keys: OpenAI, Cartesia, Cekura, OpenRouter

### Install

```bash
cd backend && uv sync && cd ..
cd molt-harness && uv sync && uv run playwright install chromium && cd ..
cd molt-broadcast && npm install && cd ..
```

### Configure

```bash
cp .env.example .env
```

| Key | Required | What it's for |
|---|---|---|
| `GB_EMAIL` / `GB_PASSWORD` | ✅ | Gradient Bang login |
| `GB_CHARACTER` | ✅ | Character name (auto-created on first run) |
| `DAILY_API_KEY` | ✅ | Stream room token creation |
| `OPENAI_API_KEY` | ✅ | Harness LLM + game vision agent |
| `CARTESIA_API_KEY` | ✅ | Voice TTS |
| `OPENROUTER_API_KEY` | ✅ | Backend bot LLM fallback |
| `CEKURA_API_KEY` | ✅ | Beat scoring |
| `CEKURA_AGENT_ID` | ✅ | `18046` (MoltStreamer agent) |
| `NEMOTRON_LLM_URL` | — | If set, replaces GPT-4.1 with Nemotron 3 Super 120B |

### Seed failure memory (recommended for demos)

```bash
cd backend && uv run python seed_demo.py
```

### Run

```bash
# Full stack
cd molt-harness && uv run python main.py

# Game bot only
cd backend && uv run python bot.py
```

### Send audience chat

```bash
curl -X POST http://localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"viewer": "hype_bro", "text": "warp to sector 3389!"}'
```

### A/B demo (showing the bot learning)

```bash
# Session 1 — no memory (baseline)
FAILURE_RETRIEVAL_ENABLED=false uv run python bot.py

# Session 2 — with memory
FAILURE_RETRIEVAL_ENABLED=true uv run python bot.py
```

On startup with memory, the bot logs what it learned before the first word is spoken:

```
[MEMORY] 6 past failure(s) loaded from FAISS:
  [took_trash_talk_at_face] when='bro you're terrible' → avoid='[mode=TAKE] You're right...'
  [game_action_unsafe] when='enemy vessel entered the sector' → avoid='Continuing exploration...'
```
