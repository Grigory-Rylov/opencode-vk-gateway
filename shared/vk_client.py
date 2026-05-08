"""
VK Client - обёртка для VK API
"""
import json
import time
from typing import Optional
from urllib.parse import urlencode

import logging

import aiohttp
from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger("vk-opencode")


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

    async def send_keyboard(
       self, peer_id: int, text: str, buttons: list
    ):
        """Отправить сообщение с клавиатурой (кнопки-действия)."""
        logger.info(f"[/models debug] send_keyboard called for peer_id={peer_id}, buttons={len(buttons)}")
        keyboard = {"inline": False, "buttons": buttons}
        await self.send_message(peer_id, text, keyboard=keyboard)
        logger.info(f"[/models debug] send_keyboard completed for peer_id={peer_id}")

    async def send_file(
        self, peer_id: int, file_path: str, filename: str, caption: str = ""
    ) -> int:
        logger.info(f"send_file: file={file_path}, peer_id={peer_id}")
        params = {
            "access_token": self.token,
            "v": self.api_version,
            "type": "doc",
            "peer_id": peer_id,
        }
        url = f"{self.BASE_URL}docs.getMessagesUploadServer?{urlencode(params)}"
        logger.info(f"send_file: getting upload url: {url}")
        async with self.session.get(url) as resp:
            data = await resp.json()
            logger.info(f"send_file: upload response: {data}")
            if "error" in data:
                raise Exception(f"VK API error getting upload url: {data['error']}")
            upload_url = data["response"]["upload_url"]
            logger.info(f"send_file: upload_url={upload_url}")

        with open(file_path, "rb") as f:
            content = f.read()
        form_data = aiohttp.FormData()
        form_data.add_field("file", content, filename=filename, content_type="application/json")
        async with self.session.post(upload_url, data=form_data) as resp:
            upload_data = await resp.json()
            logger.info(f"send_file: upload_data={upload_data}")

        params = {"access_token": self.token, "v": self.api_version}
        params.update(upload_data)
        url = f"{self.BASE_URL}docs.save?{urlencode(params)}"
        async with self.session.post(url) as resp:
            save_data = await resp.json()
            logger.info(f"send_file: save_data={save_data}")
        doc = save_data["response"]["doc"]
        doc_id = doc["id"]
        doc_owner_id = doc["owner_id"]

        attachment = f"doc{doc_owner_id}_{doc_id}"
        params = {
            "access_token": self.token,
            "v": self.api_version,
            "peer_id": peer_id,
            "attachment": attachment,
            "random_id": int(time.time() * 1000),
        }
        if caption:
            params["message"] = caption
        url = f"{self.BASE_URL}messages.send?{urlencode(params)}"
        async with self.session.get(url) as resp:
            result = await resp.json()
        return result[0]["message_id"] if isinstance(result, list) else result