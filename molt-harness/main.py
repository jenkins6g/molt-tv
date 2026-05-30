import asyncio
import os
import subprocess
from pathlib import Path

import httpx
from dotenv import load_dotenv
from loguru import logger
from playwright.async_api import async_playwright

from bot import run_bot

load_dotenv()

DAILY_API_KEY = os.environ["DAILY_API_KEY"]
BROADCAST_DIR = Path(__file__).parent.parent / "molt-broadcast"
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


async def main() -> None:
    electron_path = await get_electron_path()
    main_js = str(BROADCAST_DIR / "src" / "main" / "index.js")

    logger.info("Launching molt-broadcast...")
    electron_proc = subprocess.Popen(
        [electron_path, main_js, f"--remote-debugging-port={CDP_PORT}"],
        env={**os.environ},
    )

    async with async_playwright() as playwright:
        # Wait for Electron's CDP endpoint to become available
        browser = None
        for attempt in range(15):
            await asyncio.sleep(1)
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://localhost:{CDP_PORT}"
                )
                break
            except Exception:
                logger.debug(f"CDP not ready yet (attempt {attempt + 1})...")

        if browser is None:
            electron_proc.terminate()
            raise RuntimeError("Could not connect to Electron via CDP")

        # Find the renderer window (not the devtools page)
        context = browser.contexts[0]
        page = next(
            (p for p in context.pages if "index.html" in p.url or p.url == "about:blank"),
            context.pages[0],
        )

        logger.info("Waiting for Daily room to be created...")
        await page.wait_for_selector("#room-url", timeout=15_000)
        await page.wait_for_function(
            "document.getElementById('room-url').value !== ''",
            timeout=15_000,
        )
        room_url = await page.input_value("#room-url")
        room_name = room_url.rstrip("/").split("/")[-1]
        logger.info(f"Room ready: {room_url}")

        logger.info("Starting broadcast...")
        await page.wait_for_selector("#start-btn:not([disabled])", timeout=10_000)
        await page.click("#start-btn")
        logger.info("Broadcast started.")

    logger.info("Minting agent owner token...")
    agent_token = await create_agent_token(room_name)

    logger.info("Starting voice agent...")
    try:
        await run_bot(room_url, agent_token)
    finally:
        electron_proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
