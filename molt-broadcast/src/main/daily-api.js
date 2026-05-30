'use strict';

async function createDailyRoom(apiKey) {
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${apiKey}`,
  };

  const roomRes = await fetch('https://api.daily.co/v1/rooms', {
    method: 'POST',
    headers,
    body: JSON.stringify({
      properties: {
        enable_screenshare: true,
        enable_chat: true,
        start_video_off: false,
        owner_only_broadcast: true,
        exp: Math.floor(Date.now() / 1000) + 86400,
      },
    }),
  });

  if (!roomRes.ok) {
    const text = await roomRes.text();
    throw new Error(`Daily API error ${roomRes.status}: ${text}`);
  }

  const room = await roomRes.json();

  const tokenRes = await fetch('https://api.daily.co/v1/meeting-tokens', {
    method: 'POST',
    headers,
    body: JSON.stringify({
      properties: {
        room_name: room.name,
        is_owner: true,
      },
    }),
  });

  if (!tokenRes.ok) {
    const text = await tokenRes.text();
    throw new Error(`Daily token error ${tokenRes.status}: ${text}`);
  }

  const { token } = await tokenRes.json();
  return { url: room.url, name: room.name, token };
}

module.exports = { createDailyRoom };
