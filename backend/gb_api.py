"""Gradient Bang API client.

Login, character lookup/creation, and session start. Returns the Daily
room URL + token the bot's Pipecat pipeline joins.

Ported from chadbailey59/gb-bot bot-v5.py (lines 747–831), lightly cleaned
to expose a ``GBStartResult`` dataclass.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import aiohttp
from loguru import logger


def _api_base() -> str:
    env = os.environ.get("GB_ENV", "prod").strip().lower()
    bases = {
        "prod": os.environ.get("GB_API_BASE_PROD", "https://api.gradient-bang.com/functions/v1"),
        "local": os.environ.get("GB_API_BASE_LOCAL", "http://127.0.0.1:54321/functions/v1"),
    }
    return bases.get(env, bases["prod"]).rstrip("/")


def _gb_credentials() -> tuple[str | None, str | None]:
    return os.environ.get("GB_EMAIL"), os.environ.get("GB_PASSWORD")


def _character_name() -> str:
    return os.environ.get("GB_CHARACTER", "MoltStreamer")


def should_log_daily_join_url() -> bool:
    return os.environ.get("GB_LOG_DAILY_JOIN_URL", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass
class GBStartResult:
    room_url: str
    room_token: str
    session_id: str
    character_id: str
    character_name: str
    user_email: str

    @property
    def join_url(self) -> str:
        return f"{self.room_url}?t={self.room_token}"


async def api_login(session: aiohttp.ClientSession) -> dict:
    email, password = _gb_credentials()
    if not email or not password:
        raise RuntimeError(
            "GB_EMAIL and GB_PASSWORD are required. Sign up at gradient-bang.com first."
        )
    async with session.post(
        f"{_api_base()}/login",
        json={"email": email, "password": password},
    ) as response:
        response.raise_for_status()
        return await response.json()


async def api_create_character(
    session: aiohttp.ClientSession, token: str, name: str
) -> dict:
    async with session.post(
        f"{_api_base()}/user_character_create",
        json={"name": name},
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        response.raise_for_status()
        return await response.json()


async def api_start_bot(
    session: aiohttp.ClientSession, token: str, character_id: str
) -> dict:
    async with session.post(
        f"{_api_base()}/start",
        json={
            "createDailyRoom": True,
            "dailyRoomProperties": {
                "start_video_off": True,
                "eject_at_room_exp": True,
            },
            "body": {
                "character_id": character_id,
                "bypass_tutorial": True,
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    ) as response:
        response.raise_for_status()
        return await response.json()


async def login_and_start(
    character_name: Optional[str] = None,
) -> GBStartResult:
    """Login, select/create character, start a session, return creds."""
    name = character_name or _character_name()
    api_base = _api_base()
    env = os.environ.get("GB_ENV", "prod").strip().lower()
    logger.info(f"[GB] Logging in ({env}, {api_base})...")
    async with aiohttp.ClientSession() as session:
        login_data = await api_login(session)
        token = login_data["session"]["access_token"]
        characters = login_data.get("characters", [])
        user_email = login_data["user"]["email"]
        logger.info(f"    Logged in as {user_email}")

        character_id: Optional[str] = None
        for c in characters:
            if c.get("name") == name:
                character_id = c["character_id"]
                logger.info(f"    Using character: {name} ({character_id})")
                break

        if not character_id:
            logger.info(f"    Creating character: {name}...")
            result = await api_create_character(session, token, name)
            character_id = result["character_id"]
            logger.info(f"    Created: {character_id}")

        logger.info("[GB] Starting game session...")
        start_data = await api_start_bot(session, token, character_id)
        room_url = start_data["dailyRoom"]
        room_token = start_data["dailyToken"]
        session_id = start_data.get("sessionId", "?")

        logger.info(f"    Room: {room_url}")
        logger.info(f"    Session: {session_id}")
        if should_log_daily_join_url():
            logger.warning("    Daily join URL logging enabled — logs contain a room token")
            logger.info(f"    Join: {room_url}?t={room_token}")

        return GBStartResult(
            room_url=room_url,
            room_token=room_token,
            session_id=session_id,
            character_id=character_id,
            character_name=name,
            user_email=user_email,
        )
