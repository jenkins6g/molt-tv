"""molt-harness/main.py — Orchestrates GB game session + screenshare + voice co-host."""

import asyncio
import os
import subprocess
from pathlib import Path

import httpx
from dotenv import load_dotenv
from loguru import logger
from playwright.async_api import async_playwright

from bot import run_bot

# Root .env has GB credentials + shared API keys; local .env can override.
_ROOT_ENV = Path(__file__).parent.parent / ".env"
load_dotenv(_ROOT_ENV)
load_dotenv(override=True)

DAILY_API_KEY = os.environ["DAILY_API_KEY"]
BROADCAST_DIR = Path(__file__).parent.parent / "molt-broadcast"
BACKEND_DIR = Path(__file__).parent.parent / "backend"
CDP_PORT = 9222


async def get_electron_path() -> str:
    proc = await asyncio.create_subprocess_exec(
        "node", "-e", "process.stdout.write(require('electron'))",
        cwd=str(BROADCAST_DIR),
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode().strip()


async def create_agent_token(room_name: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.daily.co/v1/meeting-tokens",
            headers={"Authorization": f"Bearer {DAILY_API_KEY}"},
            json={"properties": {"room_name": room_name, "is_owner": True}},
        )
        resp.raise_for_status()
        return resp.json()["token"]



async def launch_game_browser(playwright, email: str, password: str):
    """Open a visible Chrome window and log into gradient-bang.com.

    Returns (game_page, browser). The browser stays on gradient-bang.com —
    the backend bot handles the Daily room separately.
    """
    gb_site = os.environ.get("GB_SITE_URL", "https://game.gradient-bang.com")

    browser = await playwright.chromium.launch(
        headless=False,
        channel="chrome",
        args=["--start-maximized"],
    )
    context = await browser.new_context(viewport={"width": 1280, "height": 800})
    page = await context.new_page()

    logger.info(f"[BROWSER] Navigating to {gb_site}...")
    await page.goto(gb_site, wait_until="domcontentloaded")
    logger.info(f"[BROWSER] Landed at: {page.url}")

    # --- Step 0: Click the "Sign In" button on the landing page ---
    SIGNIN_SELS = [
        'button:text("Sign In")',
        'button:text("Sign in")',
        'a:text("Sign In")',
        'a:text("Sign in")',
    ]
    for sel in SIGNIN_SELS:
        try:
            await page.click(sel, timeout=5_000)
            logger.info(f"[BROWSER] Clicked sign-in trigger using selector: {sel}")
            break
        except Exception:
            continue

    # --- Step 1: Find and fill the login form ---
    EMAIL_SELS = [
        'input[type="email"]',
        'input[name="email"]',
        'input[placeholder*="email" i]',
    ]
    email_field = None
    for sel in EMAIL_SELS:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            email_field = sel
            break
        except Exception:
            continue

    if email_field is None:
        raise RuntimeError(
            f"[BROWSER] Could not find an email input at {page.url} — "
            "check GB_SITE_URL or the gradient-bang.com login flow has changed."
        )

    await page.fill(email_field, email)
    logger.info(f"[BROWSER] Filled email using selector: {email_field}")

    await page.fill('input[type="password"]', password)
    logger.info("[BROWSER] Filled password.")

    SUBMIT_SELS = [
        'button[type="submit"]',
        'button:text("Sign in")',
        'button:text("Log in")',
        'button:text("Login")',
    ]
    for sel in SUBMIT_SELS:
        try:
            await page.click(sel, timeout=2_000)
            logger.info(f"[BROWSER] Clicked submit using selector: {sel}")
            break
        except Exception:
            continue

    # --- Step 2 + 3: Wait for character select screen, then click the character ---
    # The card DOM: <div role="button" aria-label="Select character MoltStreamer, last active ...">
    character_name = os.environ.get("GB_CHARACTER", "MoltStreamer")

    # Primary: aria-label selector — most reliable, matches the actual element
    aria_locator = page.locator(f'[role="button"][aria-label*="Select character {character_name}"]')
    try:
        await aria_locator.first.wait_for(timeout=12_000)
        await aria_locator.first.click(timeout=5_000)
        logger.info(f"[BROWSER] Clicked '{character_name}' via aria-label → {page.url}")
    except Exception:
        logger.warning(f"[BROWSER] aria-label selector failed — falling back to text search")
        # Fallback: find the uppercase text span and click its card ancestor
        try:
            text_el = page.get_by_text(character_name.upper(), exact=True)
            await text_el.first.wait_for(timeout=8_000)
            await text_el.first.click(timeout=5_000)
            logger.info(f"[BROWSER] Clicked '{character_name}' via text fallback → {page.url}")
        except Exception as e:
            logger.warning(f"[BROWSER] Could not click character card: {e}")

    # --- Step 4: Start playing (click Play / Continue / Start) ---
    PLAY_SELS = [
        'button:text("Play")',
        'button:text("Continue")',
        'button:text("Start")',
        'button:text("Join")',
        'a:text("Play")',
        'a:text("Continue")',
        'a[href*="play"]',
    ]
    for sel in PLAY_SELS:
        try:
            await page.click(sel, timeout=5_000)
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
            logger.info(f"[BROWSER] Started game using: {sel} → {page.url}")
            break
        except Exception:
            continue
    else:
        logger.info(f"[BROWSER] No explicit play button found — staying at {page.url}")

    return page, browser


def launch_backend_bot() -> subprocess.Popen:
    """Spawn backend/bot.py — it handles its own GB login and game session."""
    proc = subprocess.Popen(
        ["uv", "run", "python", "bot.py"],
        cwd=str(BACKEND_DIR),
        env={**os.environ},
    )
    logger.info(f"[GAME BOT] Started (pid={proc.pid})")
    return proc


async def main() -> None:
    email = os.environ.get("GB_EMAIL", "")
    password = os.environ.get("GB_PASSWORD", "")

    async with async_playwright() as playwright:
        # 1. Launch Electron screensharer first so the stream is live.
        electron_path = await get_electron_path()
        main_js = str(BROADCAST_DIR / "src" / "main" / "index.js")

        logger.info("Launching molt-broadcast screensharer...")
        electron_proc = subprocess.Popen(
            [electron_path, main_js, f"--remote-debugging-port={CDP_PORT}"],
            env={**os.environ},
        )

        game_browser = None
        game_bot_proc = None
        try:
            # 2. Connect Playwright to the Electron app via CDP.
            electron_browser = None
            for attempt in range(15):
                await asyncio.sleep(1)
                try:
                    electron_browser = await playwright.chromium.connect_over_cdp(
                        f"http://localhost:{CDP_PORT}"
                    )
                    break
                except Exception:
                    logger.debug(f"CDP not ready (attempt {attempt + 1})...")

            if electron_browser is None:
                raise RuntimeError("Could not connect to Electron via CDP")

            e_context = electron_browser.contexts[0]
            e_page = next(
                (p for p in e_context.pages if "index.html" in p.url or p.url == "about:blank"),
                e_context.pages[0],
            )

            logger.info("Waiting for streaming room to be created...")
            await e_page.wait_for_selector("#room-url", timeout=15_000)
            await e_page.wait_for_function(
                "document.getElementById('room-url').value !== ''",
                timeout=15_000,
            )
            stream_room_url = await e_page.input_value("#room-url")
            room_name = stream_room_url.rstrip("/").split("/")[-1]
            logger.info(f"Streaming room: {stream_room_url}")

            logger.info("Starting broadcast...")
            await e_page.wait_for_selector("#start-btn:not([disabled])", timeout=10_000)
            await e_page.click("#start-btn")
            await asyncio.sleep(1)  # let the stream initialise
            logger.info("Broadcast started — minimizing control window.")
            await e_page.evaluate("window.electronAPI.minimizeWindow()")

            agent_token = await create_agent_token(room_name)

            # 3. Open headed browser on gradient-bang.com (the only thing on screen now).
            game_page, game_browser = await launch_game_browser(playwright, email, password)

            # 4. Launch backend game bot — it handles its own GB login and plays.
            game_bot_proc = launch_backend_bot()

            # 6. Voice co-host joins the streaming room; screenshots from the game browser.
            logger.info("Starting voice agent...")
            await run_bot(stream_room_url, agent_token, page=game_page)

        finally:
            electron_proc.terminate()
            if game_bot_proc is not None:
                game_bot_proc.terminate()
            if game_browser is not None:
                try:
                    await game_browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(main())
