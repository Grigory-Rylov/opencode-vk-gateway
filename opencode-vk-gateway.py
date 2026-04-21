#!/usr/bin/env python3
"""
OpenCode VK Gateway Bot (Long Poll версия)
Использует текстовые inline-кнопки для вопросов.
Поддерживает отправку промежуточных рассуждений в отдельный чат (thinking_peer_id).
Конфигурация загружается из JSON-файла.
"""

import argparse
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientSession, ClientTimeout


# ---------- Загрузка конфигурации ----------
def load_config(config_path: str = "config.json") -> dict:
    """Загружает конфигурацию из JSON-файла."""
    default_config = {
        "vk_token": "token",
        "opencode_url": "http://127.0.0.1:4096",
        "session_file": "sessions.json",
        "vk_api_version": "5.200",
        "longpoll_wait": 25,
        "thinking_peer_id": 2000000506,
    }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        config = {**default_config, **user_config}
    except FileNotFoundError:
        print(f"Config file {config_path} not found, using defaults.")
        config = default_config
    except json.JSONDecodeError as e:
        print(f"Error parsing config file {config_path}: {e}")
        raise
    return config


parser = argparse.ArgumentParser(description="OpenCode VK Gateway Bot")
parser.add_argument(
    "--config", type=str, default="config.json", help="Path to JSON config file"
)
args = parser.parse_args()

CONFIG = load_config(args.config)

VK_TOKEN = CONFIG["vk_token"]
OPENCODE_URL = CONFIG["opencode_url"]
SESSION_FILE = Path(CONFIG["session_file"])
VK_API_VERSION = CONFIG["vk_api_version"]
LONGPOLL_WAIT = CONFIG["longpoll_wait"]
THINKING_PEER_ID = CONFIG.get("thinking_peer_id")

if not VK_TOKEN:
    raise ValueError("VK_TOKEN is required in config file")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vk-opencode")


# ---------- Управление сессиями OpenCode ----------
class SessionManager:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.sessions: Dict[int, str] = self._load()

    def _load(self) -> Dict[int, str]:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in self.sessions.items()}, f, indent=2)

    async def get_or_create(self, user_id: int) -> str:
        if user_id in self.sessions:
            return self.sessions[user_id]

        async with ClientSession() as session:
            async with session.post(f"{OPENCODE_URL}/session") as resp:
                resp.raise_for_status()
                data = await resp.json()
                session_id = data["id"]
                self.sessions[user_id] = session_id
                self._save()
                logger.info(f"Created OpenCode session {session_id} for user {user_id}")
                return session_id


# ---------- ВК API клиент ----------
class VKClient:
    BASE_URL = "https://api.vk.com/method/"

    def __init__(self, token: str):
        self.token = token
        self.session: Optional[ClientSession] = None

    async def __aenter__(self):
        self.session = ClientSession(timeout=ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.close()

    async def _api_request(self, method: str, params: dict) -> dict:
        params["access_token"] = self.token
        params["v"] = VK_API_VERSION
        url = f"{self.BASE_URL}{method}?{urlencode(params)}"
        async with self.session.get(url) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(f"VK API error: {data['error']}")
            return data["response"]

    async def get_long_poll_server(self) -> Tuple[str, str, int]:
        resp = await self._api_request("messages.getLongPollServer", {})
        return resp["server"], resp["key"], int(resp["ts"])

    async def get_messages_by_ids(self, msg_ids: List[int]) -> List[dict]:
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

    async def send_question_keyboard(
        self, peer_id: int, header: str, question_text: str, options: List[dict]
    ):
        buttons = []
        for opt in options:
            buttons.append(
                [
                    {
                        "action": {
                            "type": "text",
                            "label": opt["label"],
                        },
                        "color": "primary",
                    }
                ]
            )

        keyboard = {
            "one_time": True,
            "inline": False,
            "buttons": buttons,
        }

        text = f"🔧 {header}\n\n{question_text}"
        await self.send_message(peer_id, text, keyboard=keyboard)


# ---------- Лонгполл слушатель ВК ----------
class VKLongPoll:
    def __init__(self, vk: VKClient, session_mgr: SessionManager):
        self.vk = vk
        self.session_mgr = session_mgr
        self.server = None
        self.key = None
        self.ts = None
        self.running = False

        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.waiting_for_answer: Dict[int, str] = {}

    async def _get_long_poll_events(self) -> Tuple[List[dict], int]:
        params = {
            "act": "a_check",
            "key": self.key,
            "ts": self.ts,
            "wait": LONGPOLL_WAIT,
            "mode": 74,
            "version": 3,
        }
        url = f"https://{self.server}?{urlencode(params)}"
        timeout = ClientTimeout(total=LONGPOLL_WAIT + 10)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if "failed" in data:
                    raise Exception(f"Long poll failed: {data}")
                return data.get("updates", []), int(data["ts"])

    async def _refresh_long_poll_server(self):
        self.server, self.key, self.ts = await self.vk.get_long_poll_server()
        logger.info(f"Long Poll server refreshed: {self.server}")

    async def _handle_message_new(self, event: list):
        msg_id = int(event[1])
        flags = int(event[2])
        peer_id = int(event[3])
        text = event[5] if len(event) > 5 else ""

        if flags & 2:
            return

        user_id = peer_id
        logger.info(f"New message from {user_id}: text='{text}'")

        if user_id in self.waiting_for_answer:
            question_id = self.waiting_for_answer.pop(user_id)
            await self._handle_question_answer(user_id, question_id, text)
            return

        full_msgs = await self.vk.get_messages_by_ids([msg_id])
        if not full_msgs:
            return
        full_msg = full_msgs[0]
        await self._handle_user_message(user_id, full_msg.get("text", ""))

    async def _handle_user_message(self, user_id: int, text: str):
        if user_id in self.active_tasks:
            self.active_tasks[user_id].cancel()
            try:
                await self.active_tasks[user_id]
            except asyncio.CancelledError:
                pass

        session_id = await self.session_mgr.get_or_create(user_id)
        task = asyncio.create_task(self._opencode_flow(user_id, session_id, text))
        self.active_tasks[user_id] = task

    async def _opencode_flow(
        self, user_id: int, session_id: str, initial_text: str = ""
    ):
        event_queue = asyncio.Queue()
        monitor_task = asyncio.create_task(
            self._monitor_sse(user_id, session_id, event_queue)
        )
        final_text = None
        question_asked = False

        try:
            await asyncio.sleep(0.5)

            if initial_text:
                async with ClientSession() as session:
                    url = f"{OPENCODE_URL}/session/{session_id}/prompt_async"
                    data = {"parts": [{"type": "text", "text": initial_text}]}
                    async with session.post(url, json=data) as resp:
                        if resp.status != 204:
                            logger.error(f"prompt_async failed: {resp.status}")
                            await self.vk.send_message(
                                user_id, "❌ Ошибка запуска обработки"
                            )
                            return
                    logger.info(f"prompt_async sent for {session_id}")

            while True:
                event = await event_queue.get()
                event_type = event.get("type")
                logger.info(f"Processing event: {event_type} for session {session_id}")

                if event_type == "question.asked":
                    await self._show_question(user_id, event)
                    question_asked = True
                    break
                elif event_type == "session.idle":
                    if final_text:
                        await self.vk.send_message(user_id, final_text)
                    else:
                        await self._send_final_message(user_id, session_id)
                    break
                elif event_type == "message.part.updated":
                    part = event.get("properties", {}).get("part", {})
                    part_type = part.get("type")
                    if part_type == "reasoning" and part.get("text"):
                        reasoning_text = part["text"]
                        logger.info(f"Got reasoning: {reasoning_text[:100]}...")
                        if THINKING_PEER_ID:
                            try:
                                await self.vk.send_message(
                                    THINKING_PEER_ID,
                                    f"🧠 Рассуждение:\n{reasoning_text}",
                                )
                            except Exception as e:
                                logger.warning(
                                    f"Failed to send reasoning to thinking chat: {e}"
                                )
                    elif part_type == "text" and part.get("text"):
                        final_text = part["text"]
                elif event_type == "error":
                    logger.error(f"SSE error: {event}")
                    await self.vk.send_message(user_id, "❌ OpenCode error")
                    break

        except asyncio.CancelledError:
            logger.info(f"OpenCode flow cancelled for user {user_id}")
            raise
        except Exception as e:
            logger.exception(f"OpenCode flow error for {user_id}: {e}")
            await self.vk.send_message(user_id, "⚠️ Произошла ошибка, попробуйте позже.")
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            self.active_tasks.pop(user_id, None)

            if question_asked:
                logger.info(
                    f"Question asked, waiting for user reply on session {session_id}"
                )

    async def _monitor_sse(
        self, user_id: int, session_id: str, event_queue: asyncio.Queue
    ):
        logger.info(f"Starting SSE monitor for session {session_id}")
        try:
            async with ClientSession(timeout=ClientTimeout(total=None)) as session:
                url = f"{OPENCODE_URL}/event"
                async with session.get(url) as resp:
                    logger.info(f"SSE connection established, status={resp.status}")
                    async for line in resp.content:
                        if not line.startswith(b"data:"):
                            continue
                        data_str = line[5:].strip().decode()
                        if not data_str:
                            continue
                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse SSE data: {data_str}")
                            continue

                        logger.info(f"SSE raw event: {event}")

                        event_session = event.get("properties", {}).get("sessionID")
                        if event_session != session_id:
                            continue

                        await event_queue.put(event)
        except asyncio.CancelledError:
            logger.info(f"SSE monitor cancelled for {session_id}")
            raise
        except Exception as e:
            logger.exception(f"SSE monitor error for {session_id}: {e}")

    async def _show_question(self, user_id: int, event: dict):
        props = event.get("properties", {})
        questions = props.get("questions", [])
        if not questions:
            logger.error("No questions in event")
            return
        question = questions[0]
        question_id = props["id"]

        self.waiting_for_answer[user_id] = question_id

        await self.vk.send_question_keyboard(
            peer_id=user_id,
            header=question["header"],
            question_text=question["question"],
            options=question["options"],
        )

    async def _handle_question_answer(
        self, user_id: int, question_id: str, answer: str
    ):
        async with ClientSession() as session:
            url = f"{OPENCODE_URL}/question/{question_id}/reply"
            data = {"answers": [[answer]]}
            async with session.post(url, json=data) as resp:
                if resp.status == 200:
                    logger.info(f"Answered question {question_id} with '{answer}'")
                    await self.vk.send_message(user_id, f"✅ Вы выбрали: {answer}")
                    session_id = await self.session_mgr.get_or_create(user_id)
                    task = asyncio.create_task(
                        self._opencode_flow(user_id, session_id, "")
                    )
                    self.active_tasks[user_id] = task
                else:
                    logger.error(f"Failed to reply: {resp.status}")
                    await self.vk.send_message(user_id, "❌ Ошибка отправки ответа")

    async def _send_final_message(self, user_id: int, session_id: str):
        async with ClientSession() as session:
            url = f"{OPENCODE_URL}/session/{session_id}/message"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to get messages: {resp.status}")
                    return
                messages = await resp.json()
                if not messages:
                    return
                assistant_msg = None
                for msg in reversed(messages):
                    if msg.get("role") == "assistant":
                        assistant_msg = msg
                        break
                if not assistant_msg:
                    logger.warning("No assistant message found")
                    return
                text = ""
                for part in assistant_msg.get("parts", []):
                    if part.get("type") in ("text", "reasoning"):
                        text += part.get("text", "")
                if text:
                    await self.vk.send_message(user_id, text)

    async def run(self):
        self.running = True
        await self._refresh_long_poll_server()

        while self.running:
            try:
                updates, new_ts = await self._get_long_poll_events()
                self.ts = new_ts

                for update in updates:
                    if not isinstance(update, list):
                        continue
                    event_type = update[0]
                    if event_type == 4:
                        asyncio.create_task(self._handle_message_new(update))

            except asyncio.CancelledError:
                break
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(
                    f"Long poll timeout or client error: {e}. Reconnecting..."
                )
                await asyncio.sleep(3)
                await self._refresh_long_poll_server()
            except Exception as e:
                logger.exception(f"Long poll error: {e}")
                await asyncio.sleep(3)
                await self._refresh_long_poll_server()

    async def stop(self):
        self.running = False
        for task in self.active_tasks.values():
            task.cancel()
        await asyncio.gather(*self.active_tasks.values(), return_exceptions=True)


# ---------- Точка входа ----------
async def main():
    session_mgr = SessionManager(SESSION_FILE)
    async with VKClient(VK_TOKEN) as vk:
        poller = VKLongPoll(vk, session_mgr)
        try:
            await poller.run()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            await poller.stop()


if __name__ == "__main__":
    asyncio.run(main())
