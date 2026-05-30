 | File | Role |
    |---|---|
    | `main.py` | Entry point. Finds the Electron binary, spawns it with `--remote-debugging-port=9222`, connects Playwright over CDP, waits for the Daily
    room URL, clicks Start, mints an agent token, then calls `run_bot()`. |
    | `bot.py` | Pipecat pipeline: Daily transport (audio out) → LLM (GPT-4.1) → `EmotionExtractor` → Cartesia TTS → Daily output. Proactively screenshots
    the Electron preview, calls GPT-4V to describe it, and queues streamer comments. Handles `on_app_message` for live chat. |

    **Key env vars**: `DAILY_API_KEY`, `OPENAI_API_KEY`, `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID`.

    ---

    ## Data flow (current)

    molt-broadcast (Electron)
      └─ captures screen → Daily streaming room
            ↑
    molt-harness/main.py
      ├─ spawns Electron via CDP
      ├─ clicks "Start" programmatically
      └─ runs voice co-host bot
            └─ screenshots Electron preview → GPT-4V → streamer commentary → Cartesia TTS → Daily

    backend/bot.py  (run separately)
      └─ logs into Gradient Bang → joins game Daily room → plays game as AI Officer

    ## Planned integration (in progress)

    The harness will be extended to:
    1. Call GB API (`gb_api.py`) to login + start a game session, getting a join URL.
    2. Open a headed Playwright browser navigated to the game join URL (game visible on screen).
    3. Spawn `backend/bot.py` as a subprocess to actually play the game in that room.
    4. Screenshot the *game browser page* (not just the Electron preview) for the voice co-host to react to.
    5. Be interactive with chat messages forwarded into the voice agent pipeline.