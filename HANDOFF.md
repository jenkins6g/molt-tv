# Handoff — MoltStreamer auto-improvement loop

Last updated: 2026-05-30, branch `sekura`.

## TL;DR

The Cekura ingest/poll loop is wired end-to-end and verified — `observe_call`
returns 201, polling returns `status: success`. The bot's pipeline now has
a write side (BeatTicker → Cekura → tag → embed → FAISS) and a read side
(FailureRetrievalInjector pulls top-k similar past failures and prepends
them as a "past mistakes to avoid" system message before the LLM call).

**One thing blocks the loop from doing anything visible:** no metrics are
defined for the MoltStreamer agent in the Cekura dashboard yet. With zero
metrics, every beat scores empty → no `fail` verdict → no failure recorded
→ no retrieval. Five-minute fix in the dashboard (or via the `cekura:*`
plugin or MCP) and the full loop comes alive.

A secondary block: AWS Bedrock creds aren't in `.env`. Even with metrics,
the `tag_failure` (Haiku) and `embed` (Titan) steps will silently skip
until creds are added.

## Pipeline shape now

```
transport.input → timing → user_aggregator → GameContextInjector
  → FailureRetrievalInjector       ← read side (embeds last user msg, RAG over FAISS)
  → llm → WaitTagFilter → ChatModeParser
  → TranscriptRecorder             ← logs assistant turns to FailureStore
  → BotSpeechLogger → output_stage → transport.output → assistant_aggregator
```

Plus `BeatTicker` running as a background asyncio task — write side.
Plus `final_flush()` in bot.py `finally` block — final observe to Cekura
with `ended=True`.

## File map

| Path | Role |
|---|---|
| `backend/bot.py` | Pipeline assembly + ticker wiring + finally-flush |
| `backend/app/eval/cekura_client.py` | Cekura HTTP client — observe + poll |
| `backend/beat_ticker.py` | Async loop: cumulative observe + lagged poll + tag-on-fail |
| `backend/failure_retrieval.py` | RAG injector — embeds last user turn, queries FAISS, prepends as system msg |
| `backend/transcript_recorder.py` | FrameProcessor — captures Officer's assistant turns |
| `backend/app/memory/failure_store.py` | SQLite + FAISS storage. New tables: `beats`, `transcript_turns` |
| `backend/app/memory/tagger.py` | Bedrock Haiku failure tagger (existing, unchanged) |
| `backend/app/services/embed.py` | Bedrock Titan v2 embeddings (existing, unchanged) |
| `backend/app/agent/prompts.py` | `failure_tagger_prompt` with streamer-flavored failure modes |
| `backend/app/agent/mode_parser.py` | `[mode=TAKE/ROAST/ACK/IGNORE]` stripper + chat_decision linker |
| `backend/chat_bridge.py` | `POST :8000/chat` → LLM user-turn drain |
| `AGENTS.md` | Plugin metadata — tells Cekura skills which face to test + which metrics to seed |
| `backend/.env.example` | All env keys including the new ones |

## Cekura API — verified endpoints (hard-won; cite when in doubt)

Base: `https://api.cekura.ai`. Auth header: `X-CEKURA-API-KEY`
(NOT `Authorization: Bearer`). All paths require **trailing slash**.

| Op | Verb | Path | Notes |
|---|---|---|---|
| Ingest call (per-tick) | POST | `/observability/v1/observe/` | Body must include `agent`, `call_id`, `transcript_type: "pipecat"`, `transcript_json: [{role, content, start_time, end_time}]`. Reuse `call_id` per session — Cekura updates the existing call. Async. |
| List call-logs | GET | `/observability/v1/call-logs-external/?agent_id=<id>` | Org-level key requires `agent_id` (not `agent`) as query param. Returns `{next, previous, results: [call_log...]}`. |
| Single call-log | GET | `/observability/v1/call-logs-external/{id}/` | Full call with metric scores once populated. |

Response shape from observe + list:
- `status: "evaluating"` (in progress) → `status: "success"` (done) — top-level field.
- `metrics: []` — per-metric results. Empty when no metrics defined for the agent. Each item has `metric_name`, `score`, `status`/`verdict`.
- `success: true|false|null` — aggregate verdict once scoring done.
- `call_id` matches what we POSTed; internal `id` is Cekura's row id.

Transcript role mapping (in `cekura_client._to_cekura_transcript`):
- our `assistant` → Cekura `assistant`
- our `game` / `chat` / `user` → Cekura `user`
- our `text` → Cekura `content`
- our `at` (unix ts) → relative `start_time` / `end_time` (1s window per turn)

Valid `transcript_type` values (probed): `"pipecat"` (what we use), `"retell"`,
`"vapi"` (requires extra fields), `"livekit"` (requires extra fields).
`"custom"` returns 400 — don't use.

## Test status (this session)

| Smoke | Status | Notes |
|---|---|---|
| 1. DB CRUD round-trip | ✅ pass | New `beats` + `transcript_turns` tables auto-create, existing data preserved |
| 2. TranscriptRecorder with stub frames | ✅ pass | Concatenated multi-chunk LLMTextFrames, flushed on LLMFullResponseEndFrame |
| 3. Cekura ingest + poll (real API) | ✅ pass | 201 observe, polling progressed `evaluating` → `success`. Scores empty — no metrics defined yet. |
| 4. Bedrock Titan embed | ❌ NoCredentialsError | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` blank in `.env` |
| 5. bot.py import | ✅ pass | After fixing `LLMContextFrame` import path (was wrong dir) |

## Blockers, in priority order

### 1. Create metrics in Cekura dashboard *(5 min, unblocks scoring)*

The MoltStreamer agent (id `18046...`) exists in Cekura but has 0 metrics
attached. Until at least one metric exists, every beat's `metrics: []`
stays empty and BeatTicker can never see a fail verdict.

Five suggested metrics live in `AGENTS.md` § "Suggested metrics" —
in_character, chat_responsive, mode_tag_present, non_repetitive,
never_broke_character_on_provocation.

Three paths to create them, pick one:
- **Dashboard UI** — `dashboard.cekura.ai` → MoltStreamer → Metrics →
  Create. 5 clicks. (User preference per prior conversation.)
- **Cekura plugin** — `/cekura:create-metric` skill, available now. The
  MCP server got auto-configured this session — `mcp__plugin_cekura_cekura__metrics_create`
  is callable from Claude. Five tool calls, no UI.
- **Cekura plugin batch** — `/cekura:autogen-eval` will draft from AGENTS.md
  automatically.

### 2. AWS Bedrock credentials *(unblocks "between sessions" learning)*

`.env` needs `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` for an account
with Bedrock model access to `amazon.titan-embed-text-v2:0` and
`anthropic.claude-haiku-4-5-20251001-v1:0`.

Without these:
- `BeatTicker._maybe_record_failure` will hit Titan embed → fail → skip
  the FAISS write. Failures are tagged in Cekura but not stored locally.
- `FailureRetrievalInjector` will hit Titan embed → fail → forward
  context untouched. RAG read side is silently disabled.

Both failure paths are logged-and-skipped, not crashing. Bot still runs.

### 3. Cekura `evaluating` latency *(architectural, not a fix)*

Cekura scoring takes ~10-30s after ingest. The first 1-2 ticks of every
session show `status: pending`. BeatTicker handles this gracefully —
records the beat with `eval_status: pending`, no failure tagging. By
tick 3+ scoring catches up and we get real verdicts.

## Architecture decisions made (and why)

| Decision | Why |
|---|---|
| Cumulative transcript per tick (not 30s slice) | Cekura's model is one growing call per session; reusing `call_id` appends. |
| Dedupe failure tagging by `last_assistant_text` | Cekura returns `fail` repeatedly on a stuck failure; we don't want to spam FAISS with duplicates. Once the bot says something new and Cekura still says fail, that's a new tag. |
| `FAILURE_RETRIEVAL_ENABLED` env gate (default true) | Demo A/B: run once with false (boring baseline), once with true (learned). |
| `CEKURA_OBSERVABILITY_ENABLED` env gate (default true) | Lets you run bot offline without hitting Cekura at all. |
| RAG injector inserts as `system` message at position-of-last-user, not prepended to context | Strongest steering signal at the model's attention focus, without diluting the streamer system prompt at position 0. |
| Transcript stored in our SQLite (`transcript_turns`), not just sent to Cekura | Cekura is for scoring; we need local for RAG embedding + dashboard polling. |
| `_to_cekura_transcript` maps `game`/`chat` → `user` | Cekura only allows `user`/`assistant`. Both `game` and `chat` are inputs to the bot, so semantically they're user-side. |

## Open questions / risks

- **Cekura scoring latency variance** — at +20s we saw `success` with no scores, but with metrics defined this might take longer. Worth measuring.
- **Cumulative transcript growth** — for a 1hr+ session each tick POSTs increasingly large body. For the 10-min demo it's fine; for production we'd cap or window.
- **Cekura agent id is numeric (18046...)** — `_to_cekura_transcript` passes it as a string to JSON. Worked in smoke. Watch for type coercion if seeing 400s later.
- **Dedupe key for failures is text-equal** — if the bot says nearly-identical lines (one comma difference), they'd both tag. Could use a hash or cosine over embedding. Premature for now.
- **`status: success` with empty scores reads as `unknown`** — by design, but means "Cekura is happy, no metrics ran" is indistinguishable from "Cekura never heard about this call." Once metrics exist, scores will populate and the distinction disappears.

## NOT done (call out next session if you want these)

These were explicitly deferred when you said "lets do B and C":
- **Audience web UI + chat** — single-page React-less surface with embedded Daily iframe, WS chat, transcript feed, dashboard. (`plan.md` §3.2.)
- **AI viewers** — hype_bro / skeptic / weeb coroutines that auto-chat. (`plan.md` §3.2 D2.5.)
- **Launcher / orchestrator script** — one-command start of bot + broadcast + audience.
- **Screen share** (Phase 2 in `plan.md`) — bot's Playwright capture published to audience Daily room.
- **Audio fanout** (Phase 2) — bot's TTS audio into both game room AND audience room.
- **Pipecat Cloud deploy** — would unlock `/cekura-report` simulation runs (which needs Pipecat API key we skipped earlier).
- **`/transcript` and `/beats` HTTP endpoints on the sidecar** — needed only when audience UI lands.

## Env keys reference

In `.env` at `/Users/jayesh/molt-tv/.env`:

| Set | Key | Notes |
|---|---|---|
| ✅ | `CEKURA_API_KEY` | Verified working |
| ✅ | `CEKURA_AGENT_ID=18046...` | Verified, MoltStreamer agent created in dashboard |
| ✅ | `OPENROUTER_API_KEY` | Default LLM, Llama 3.3 70B |
| ✅ | `GB_EMAIL` (and presumably `GB_PASSWORD`) | For real game session |
| ❌ | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | **Blocker #2** above |
| ❌ | `DAILY_API_KEY` | Needed for audience-room creation; not blocking core loop |
| ❌ | `GRADIUM_API_KEY` / `GRADIUM_VOICE_ID` | Needed for `--audio` mode TTS |
| ❌ | `NEMOTRON_LLM_URL` | Optional — if set, replaces OpenRouter |

New env toggles added this round (defaults make the loop active):
- `BEAT_TICKER_ENABLED=true`
- `BEAT_INTERVAL_SECONDS=30`
- `FAILURE_RETRIEVAL_ENABLED=true`
- `CEKURA_OBSERVABILITY_ENABLED=true`

## Resume checklist

If you're picking this up cold, do these in order:

1. **Read this file plus `AGENTS.md` plus `plan.md`** to get the shape.
2. **`cd /Users/jayesh/molt-tv && git status`** — confirm what's staged. Today's Cekura API fixes (cekura_client, beat_ticker, bot.py, config, .env.example) are staged but not committed.
3. **`git diff --staged | less`** — review staged changes if curious. Or commit them: `git commit -m "wire real Cekura observe+poll endpoints"`.
4. **Create the 5 metrics** in dashboard.cekura.ai (or invoke `/cekura:create-metric` × 5, or call `mcp__plugin_cekura_cekura__metrics_create` directly).
5. **Add AWS creds to `.env`** for the full learning loop.
6. **Smoke #3** — `cd backend && uv run python -c "..."` (see the test commands in earlier conversation, or just run bot.py against any Daily room) — verify scores now populate.
7. **End-to-end test** — start bot in text mode against any Daily room you own:
   ```
   cd /Users/jayesh/molt-tv/backend
   BEAT_INTERVAL_SECONDS=15 LOG_LEVEL=DEBUG \
     uv run python bot.py --room-url https://YOURDOMAIN.daily.co/test-room
   curl -s -X POST :8000/chat -H 'content-type: application/json' \
     -d '{"viewer":"hype_bro_42","text":"yo whats your strategy"}'
   ```
   Look for `[CHAT IN]` → `[CHAT → OFFICER]` → `[CHAT MODE] <mode> → ...` → `[BEAT] status=... metrics=N` → `[BEAT] recorded failure mode=...`.

## Git state at handoff

Branch: `sekura`.

Committed: 644d1b8 "sekura" (initial round — failure_store schema, beat_ticker, transcript_recorder, failure_retrieval, cekura_client v1, AGENTS.md, .env.example, bot.py wiring).

Staged but not committed:
- `backend/.env.example` — added `CEKURA_AGENT_ID`, 4 toggle vars
- `backend/app/config.py` — `cekura_agent_id` settings field
- `backend/app/eval/cekura_client.py` — full rewrite for real Cekura API (X-CEKURA-API-KEY auth, `/observability/v1/observe/`, `transcript_type: pipecat`, role/field mapping, list endpoint with `agent_id` param, score normalization)
- `backend/beat_ticker.py` — cumulative observe + lagged poll, dedup on assistant_text
- `backend/bot.py` — finally-block now calls `beat_ticker.final_flush()` instead of dead push_observability

Untracked: none (this HANDOFF.md is new).

Recommend before checkout: `git add HANDOFF.md && git commit -m "wire real cekura observe+poll endpoints + handoff doc"`.
