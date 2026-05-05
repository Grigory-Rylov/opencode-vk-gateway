"""
VK Client - обёртка для VK API
"""
import json
import time
from typing import Optional
from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientSession, ClientTimeout


class VKClient:
    BASE_URL = "https://api.vk.com/method/"

    def __init__(self, token: str, api_version: str = "5.200"):
        self.token = token
        self.api_version = api_version
        self.session: Optional[ClientSession] = None

    async def __aenter__(self):
        self.session = ClientSession(timeout=ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _api_request(self, method: str, params: dict) -> dict:
        params["access_token"] = self.token
        params["v"] = self.api_version
        url = f"{self.BASE_URL}{method}?{urlencode(params)}"
        async with self.session.get(url) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(f"VK API error: {data['error']}")
            return data["response"]

    async def get_long_poll_server(self) -> tuple[str, str, int]:
        resp = await self._api_request("messages.getLongPollServer", {})
        return resp["server"], resp["key"], int(resp["ts"])

    async def get_messages_by_ids(self, msg_ids: list[int]) -> list[dict]:
        ids_str = ",".join(str(i) for i in msg_ids)
        resp = await self._api_request("messages.getById", {"message_ids": ids_str})
        return resp.get("items", [])

    async def send_message(
        self,
        peer_id: int,
        text: str = "",
        attachment: str = "",
        keyboard: Optional[dict] = None,
    ) -> int:
        """Send message using GET request (legacy, may fail with long messages)."""
        params = {
            "peer_id": peer_id,
            "random_id": int(time.time() * 1000),
        }
        if text:
            params["message"] = text
        if attachment:
            params["attachment"] = attachment
        if keyboard:
            params["keyboard"] = json.dumps(keyboard)

        resp = await self._api_request("messages.send", params)
        return resp[0]["message_id"] if isinstance(resp, list) else resp

    async def send_message_post(
        self,
        peer_id: int,
        text: str = "",
        attachment: str = "",
        keyboard: Optional[dict] = None,
    ) -> int:
        """Send message using POST request (supports longer messages, avoids 414 errors)."""
        payload = {
            "peer_id": peer_id,
            "random_id": int(time.time() * 1000),
            "v": self.api_version,
            "access_token": self.token,
        }
        if text:
            payload["message"] = text
        if attachment:
            payload["attachment"] = attachment
        if keyboard:
            payload["keyboard"] = json.dumps(keyboard)

        url = f"{self.BASE_URL}messages.send"
        async with self.session.post(url, data=payload) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(f"VK API error: {data['error']}")
            resp_data = data["response"]
            return resp_data[0]["message_id"] if isinstance(resp_data, list) else resp_data

    async def send_question_keyboard(
        self, peer_id: int, header: str, question_text: str, options: list[dict]
    ):
        buttons = []
        for opt in options:
            buttons.append(
                [
                    {
                        "action": {"type": "text", "label": opt["label"]},
                        "color": "primary",
                    }
                ]
            )

        keyboard = {"inline": False, "buttons": buttons}
        text = f"🔧 {header}\n\n{question_text}"
        await self.send_message(peer_id, text, keyboard=keyboard)