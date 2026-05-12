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
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientSession, ClientTimeout, FormData

from shared import VKClient, load_config
from shared.nvidia import get_gpu_info_vk_message
from message_parser import get_new_parts


# ---------- Управление процессом OpenCode ----------
class OpenCodeProcess:
    def __init__(self, logger, model: str = None, workdir: Path = None):
        self.logger = logger
        self.process = None
        self.opencode_port = 4096
        self.model = model
        self.workdir = workdir or Path.cwd()
        self.logger.debug(f"OpenCodeProcess initialized: workdir={self.workdir}, cwd={Path.cwd()}")

    async def start(self):
        workdir_str = str(self.workdir)
        self.logger.info(f"Starting opencode serve in {workdir_str}")
        
        # убиваем старые процессы и ждём освобождения порта
        subprocess.run(["pkill", "-9", "-f", f'{OPENCODE_BIN} serve'], stderr=subprocess.DEVNULL)
        for _ in range(10):
            result = subprocess.run(["lsof", "-i", f":{self.opencode_port}"], capture_output=True)
            if result.returncode != 0:
                break
            await asyncio.sleep(0.5)
        
        # Логирование в файл для отладки
        log_file = open(f"/tmp/opencode_{self.opencode_port}.log", "w")
        
        self.process = subprocess.Popen(
            [OPENCODE_BIN, "serve", "--port", str(self.opencode_port)],
            stdout=log_file,
            stderr=log_file,
            cwd=workdir_str,
            start_new_session=True,
        )
        self.logger.info(f"Started with PID {self.process.pid}, log file: {log_file.name}")
        
        # ждём, пока процесс не упадёт или порт не начнёт отвечать
        for _ in range(30):  # 15 секунд
            if self.process.poll() is not None:
                log_file.close()
                stdout, stderr = self.process.communicate()
                stderr_str = stderr.decode() if stderr else "no stderr available"
                raise RuntimeError(f"opencode exited early: {stderr_str}")
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get("http://127.0.0.1:4096/", timeout=1) as resp:
                        if resp.status == 200:
                            self.logger.info("OpenCode server is ready")
                            return
            except:
                pass
            await asyncio.sleep(0.5)
        log_file.close()
        raise TimeoutError("OpenCode server did not become ready in time")

    async def restart(self, workdir: Path = None):
        if workdir:
            self.logger.info(f"restart: updating workdir from {self.workdir} to {workdir}")
            self.workdir = workdir
        self.logger.info(f"restart: restarting opencode serve with workdir={self.workdir}, cwd={Path.cwd()}")
        await self.stop()
        await asyncio.sleep(1)
        await self.start()
        self.logger.info(f"opencode serve restarted with workdir={self.workdir}")

    async def stop(self):
        if self.process:
            self.logger.info(f"Stopping opencode serve, pid={self.process.pid}")
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.logger.info("opencode serve stopped")
            self.process = None


# ---------- Загрузка конфигурации ----------


async def restart_llama_server(model: dict, alias: str = None, llama_path: str = None) -> bool:
    """Перезапускает llama server с указанной моделью."""
    import subprocess
    import shlex
    import os

    if llama_path is None:
        llama_path = LLAMA_SERVER_PATH

    if not llama_path:
        logger.info("llama-server not configured, skipping restart")
        return True

    if not model or not model.get("args"):
        logger.error("No model args provided")
        return False
    
    # Убиваем процесс llama-server на порту 8081
    try:
        subprocess.run(["pkill", "-f", "llama-server"], capture_output=True)
        logger.info("Killed existing llama-server processes")
        await asyncio.sleep(1)
    except Exception as e:
        logger.warning(f"Failed to kill llama server: {e}")
    
    # Запускаем напрямую (без tmux)
    path = llama_path or LLAMA_SERVER_PATH
    args = model.get("args", "")
    cmd = f"{path} {args}"
    
    try:
        # Убираем переменную TMUX чтобы запустить нормально
        env = os.environ.copy()
        env.pop("TMUX", None)
        
        proc = subprocess.Popen(
            shlex.split(cmd),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        if proc:
            logger.info(f"Started llama server with model {alias or 'unknown'}, pid={proc.pid}")
            await asyncio.sleep(3)
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to start llama server: {e}")
        return False


async def do_restart(self, user_id: int, model_alias: str = None):
    """Выполняет перезапуск с указанной моделью или текущей."""
    global MODEL, DEFAULT_MODEL
    
    if model_alias:
        model = get_model_by_alias(model_alias)
        if not model:
            return None, f"Модель '{model_alias}' не найдена"
        alias = model_alias
    else:
        model = get_current_model()
        if not model:
            return None, "Нет доступных моделей"
        alias = DEFAULT_MODEL
    
    # Перезапускаем llama server
    await self.vk.send_message(user_id, f"🔄 Загружаю модель {alias}...")
    llama_success = await restart_llama_server(model, alias)
    if not llama_success:
        await self.vk.send_message(user_id, "⚠️ Не удалось запустить llama server")
        logger.warning("Failed to restart llama server")
    
    # Обновляем конфиг opencode с провайдером и MCP
    try:
        opencode_config_path = Path.home() / ".config/opencode/opencode.json"
        opencode_config = {
            "$schema": "https://opencode.ai/config.json",
            "model": model.get("model", ""),
            "provider": {
                "llama.cpp": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "llama-server (local)",
                    "options": {
                        "baseURL": "http://localhost:8081/v1"
                    },
                    "models": {
                        alias: {
                            "name": f"{alias} (local)",
                            "limit": {
                                "context": 131072,
                                "output": 65536
                            }
                        }
                    }
                }
            }
        }
        if MCP_SERVERS:
            opencode_config["mcp"] = MCP_SERVERS
            logger.info(f"Added {len(MCP_SERVERS)} MCP server(s) to opencode config")
        with open(opencode_config_path, "w") as f:
            json.dump(opencode_config, f, indent=2)
        logger.info(f"Updated opencode config with provider and MCP for {alias}")
    except Exception as e:
        logger.warning(f"Failed to update opencode config: {e}")
    
    # Ждем пока модель загрузится (проверяем пингом)
    import aiohttp
    max_wait = 300
    check_interval = 5
    waited = 0
    ready = False
    
    while waited < max_wait:
        await asyncio.sleep(check_interval)
        waited += check_interval
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("http://localhost:8081/", timeout=2) as resp:
                    if resp.status == 200:
                        ready = True
                        break
        except:
            pass
    
    if ready:
        await self.vk.send_message(user_id, f"✅ Модель {alias} загружена и готова!")
        logger.info(f"Model {alias} loaded successfully")
    else:
        await self.vk.send_message(user_id, f"⚠️ Модель {alias} не ответила за {max_wait} сек, продолжаю...")
        logger.warning(f"Model {alias} did not respond in time")
    
    # Перезапускаем opencode serve
    MODEL = model.get("model", MODEL)
    await self.opencode_process.restart()
    
    # Очищаем сессию после переключения модели
    self.session_mgr.remove(user_id)
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        logger.info(f"Cleared session for user {user_id} and deleted sessions file after model switch to {alias}")
    
    # Обновляем default_model
    DEFAULT_MODEL = alias
    
    # Сохраняем в config
    try:
        config = load_config()
        config["default_model"] = DEFAULT_MODEL
        config["model"] = MODEL
        with open(SCRIPT_DIR / "config.json", "w") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save config: {e}")
    
    return MODEL, None


parser = argparse.ArgumentParser(description="OpenCode VK Gateway Bot")
parser.add_argument(
    "--config", type=str, default="config.json", help="Path to JSON config file"
)
parser.add_argument(
    "-d", "--debug", action="store_true", help="Enable debug logging to file"
)

if __name__ == "__main__":
    args = parser.parse_args()
else:
    args = parser.parse_args(["--config", "config.json"])

CONFIG = load_config(args.config)

VK_TOKEN = CONFIG["vk_token"]
OPENCODE_URL = CONFIG["opencode_url"]
SESSION_FILE = Path(CONFIG["session_file"])
VK_API_VERSION = CONFIG["vk_api_version"]
LONGPOLL_WAIT = CONFIG["longpoll_wait"]
THINKING_PEER_ID = CONFIG.get("thinking_peer_id")
MODEL = CONFIG.get("model")
MODELS = CONFIG.get("models", [])
DEFAULT_MODEL = CONFIG.get("default_model")
LLAMA_SERVER_PATH = CONFIG.get("llama_server_path", None)
MCP_SERVERS = CONFIG.get("mcp_servers", {})

if not VK_TOKEN:
    raise ValueError("VK_TOKEN is required in config file")

SCRIPT_DIR = Path(__file__).parent.resolve()
OPENCODE_BIN = "/home/grishberg/.opencode/bin/opencode"

if args.debug:
    import os
    import threading
    import sys

    class DeduplicatingHandler(logging.Handler):
        def __init__(self, stream=None, filename: str = None):
            super().__init__()
            self.stream = stream
            self.filename = filename
            self.last_logged = {}
            self._lock = threading.Lock()
            if filename:
                self.file = open(filename, "w")
            else:
                self.file = None

        def emit(self, record: logging.LogRecord):
            msg = self.format(record)
            key = (record.levelno, record.message)
            prev_time = self.last_logged.get(key)
            now = time.time()

            if record.levelno >= logging.WARNING:
                self._write(now, msg)
                self.last_logged[key] = now
            elif prev_time is None or (now - prev_time) > 300:
                self._write(now, msg)
                self.last_logged[key] = now

        def _write(self, ts: float, msg: str):
            ts_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            with self._lock:
                if self.stream:
                    self.stream.write(f"[{ts_str}] {msg}\n")
                    self.stream.flush()
                if self.file:
                    self.file.write(f"[{ts_str}] {msg}\n")
                    self.file.flush()
                    os.fsync(self.file.fileno())

        def close(self):
            if self.file:
                self.file.close()
            super().close()

    file_handler = DeduplicatingHandler(filename="debug.log")
    file_handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )

    console_handler = DeduplicatingHandler(stream=sys.stderr)
    console_handler.setFormatter(
        logging.Formatter("%(levelname)s: %(message)s")
    )

    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[file_handler, console_handler],
    )
else:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
logger = logging.getLogger("vk-opencode")


def get_current_model():
    """Возвращает текущую модель из конфига."""
    if not MODELS:
        return None
    return MODELS.get(DEFAULT_MODEL)


def get_model_by_alias(alias: str):
    """Возвращает модель по алиасу."""
    if not MODELS:
        return None
    return MODELS.get(alias)


def model_to_api_format(model: str) -> dict:
    """Преобразует модель в формат для API OpenCode."""
    if not model:
        return {}
    if isinstance(model, dict):
        return {"model": model}
    if "/" in model:
        providerID, model_id = model.split("/", 1)
        return {"model": {"id": model_id, "providerID": providerID}}
    return {"model": {"id": model, "providerID": "llama.cpp"}}


# ---------- Управление сессиями OpenCode ----------
class SessionManager:
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.sessions: Dict[int, str] = {}
        self.seen_messages: Dict[str, set] = {}  # session_id -> set of message_ids
        self._load()

    def _load(self) -> None:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.sessions = {int(k): v for k, v in data.get("sessions", {}).items()}
                self.seen_messages = {
                    sid: set(ids) for sid, ids in data.get("seen_messages", {}).items()
                }
        except (FileNotFoundError, json.JSONDecodeError):
            self.sessions = {}
            self.seen_messages = {}

    def _save(self) -> None:
        data = {
            "sessions": {str(k): v for k, v in self.sessions.items()},
            "seen_messages": {sid: list(ids) for sid, ids in self.seen_messages.items()}
        }
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    async def get_or_create(self, user_id: int) -> str:
        if user_id in self.sessions:
            return self.sessions[user_id]

        async with ClientSession() as session:
            data = model_to_api_format(MODEL)
            async with session.post(f"{OPENCODE_URL}/session", json=data) as resp:
                resp.raise_for_status()
                resp_data = await resp.json()
                session_id = resp_data["id"]
                self.sessions[user_id] = session_id
                if session_id not in self.seen_messages:
                    self.seen_messages[session_id] = set()
                self._save()
                logger.info(f"Created OpenCode session {session_id} for user {user_id} with model {MODEL}")
                return session_id

    def get_seen_messages(self, session_id: str) -> set:
        return self.seen_messages.get(session_id, set())

    def add_seen_message(self, session_id: str, message_id: str):
        if session_id not in self.seen_messages:
            self.seen_messages[session_id] = set()
        self.seen_messages[session_id].add(message_id)
        logger.info(f"Saved seen message {message_id} to file for session {session_id}")
        self._save()

    def remove(self, user_id: int):
        if user_id in self.sessions:
            session_id = self.sessions[user_id]
            del self.sessions[user_id]
            if session_id in self.seen_messages:
                del self.seen_messages[session_id]
            self._save()
            logger.info(f"Removed session for user {user_id}")


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
            "inline": False,
            "buttons": buttons,
        }

        text = f"🔧 {header}\n\n{question_text}"
        await self.send_message(peer_id, text, keyboard=keyboard)

    async def send_file(
        self, peer_id: int, file_path: str, filename: str, caption: str = ""
    ) -> int:
        logger.info(f"send_file: file={file_path}, peer_id={peer_id}")
        # 1. Получаем URL для загрузки
        params = {
            "access_token": self.token,
            "v": VK_API_VERSION,
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

        # 2. Загружаем файл
        with open(file_path, "rb") as f:
            content = f.read()
        form_data = FormData()
        form_data.add_field("file", content, filename=filename, content_type="application/json")
        async with self.session.post(upload_url, data=form_data) as resp:
            upload_data = await resp.json()
            logger.info(f"send_file: upload_data={upload_data}")

        # 3. Сохраняем документ
        params = {"access_token": self.token, "v": VK_API_VERSION}
        params.update(upload_data)
        url = f"{self.BASE_URL}docs.save?{urlencode(params)}"
        async with self.session.post(url) as resp:
            save_data = await resp.json()
            logger.info(f"send_file: save_data={save_data}")
        doc = save_data["response"]["doc"]
        doc_id = doc["id"]
        doc_owner_id = doc["owner_id"]

        # 4. Отправляем документ
        attachment = f"doc{doc_owner_id}_{doc_id}"
        params = {
            "access_token": self.token,
            "v": VK_API_VERSION,
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


# ---------- Лонгполл слушатель ВК ----------
class VKLongPoll:
    def __init__(self, vk: VKClient, session_mgr: SessionManager, opencode_process: OpenCodeProcess):
        self.vk = vk
        self.session_mgr = session_mgr
        self.opencode_process = opencode_process
        self.server = None
        self.key = None
        self.ts = None
        self.running = False

        self.active_tasks: Dict[int, asyncio.Task] = {}
        self.waiting_for_answer: Dict[int, str] = {}
        self.pending_permissions: Dict[str, Tuple[str, int]] = {}

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

        if text.strip() == "/update":
            logger.debug("Ignoring /update command (handled by reloader)")
            return

        if text.strip().startswith("/restart") or text.strip().startswith("/r"):
            parts = text.strip().split()
            if text.strip().startswith("/r ") and len(parts) > 1:
                model_alias = parts[1]
            elif len(parts) > 1:
                model_alias = parts[1]
            else:
                model_alias = None
            
            if model_alias:
                model_info, error = await do_restart(self, user_id, model_alias)
                if error:
                    await self.vk.send_message(user_id, f"❌ {error}")
                else:
                    await self.vk.send_message(user_id, f"✅ Модель {model_info} загружена")
            else:
                model_info, error = await do_restart(self, user_id)
                if error:
                    await self.vk.send_message(user_id, f"❌ {error}")
                else:
                    await self.vk.send_message(user_id, f"✅ Модель {model_info} загружена")
            return

        if text.strip().startswith("/models") or text.strip() == "/m":
            if not MODELS:
                await self.vk.send_message(user_id, "Нет доступных моделей")
            else:
                models_text = "📋 **Доступные модели:**\n\n"
                for alias, m in MODELS.items():
                    marker = " ← текущая" if alias == DEFAULT_MODEL else ""
                    models_text += f"• {alias}{marker}\n"
                await self.vk.send_message(user_id, models_text)
            return

        if text.strip().startswith("/history"):
            parts = text.strip().split()
            session_id = parts[1] if len(parts) > 1 else await self.session_mgr.get_or_create(user_id)
            await self._send_history(user_id, session_id)
            return

        if text.strip().startswith("/newsession"):
            parts = text.strip().split(maxsplit=1)
            workdir_path = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
            await self._new_session(user_id, workdir_path)
            return

        if text.strip() == "/sessions":
            await self._send_sessions(user_id)
            return

        if text.strip() == "/clearsessions":
            session_file = SCRIPT_DIR / self.config.get("session_file", "sessions.json")
            if session_file.exists():
                session_file.unlink()
                await self.vk.send_message(user_id, "✅ Сессии удалены")
            else:
                await self.vk.send_message(user_id, "ℹ️ Файл sessions.json не найден")
            return

        if text.strip().startswith("/logs"):
            await self.vk.send_file(
                user_id,
                str(SCRIPT_DIR / "debug.log"),
                "debug.log",
                "📋 Логи"
            )
            return

        if text.strip() == "/help":
            await self._send_help(user_id)
            return

        if text.strip() == "/gpu":
            await self._handle_gpu_command(user_id)
            return

        if user_id in self.waiting_for_answer:
            question_id = self.waiting_for_answer.pop(user_id)
            await self._handle_question_answer(user_id, question_id, text)
            return

        for permission_id, perm_data in list(self.pending_permissions.items()):
            perm_session_id, perm_user_id = perm_data
            if perm_user_id == user_id:
                try:
                    payload_data = json.loads(text)
                    if isinstance(payload_data, dict) and "permission_id" in payload_data:
                        action = payload_data["action"]
                        if action == "allow":
                            await self._send_permission_response(permission_id, True, perm_session_id)
                            await self.vk.send_message(user_id, "✅ Разрешение предоставлено")
                        elif action == "deny":
                            await self._send_permission_response(permission_id, False, perm_session_id)
                            await self.vk.send_message(user_id, "❌ Разрешение отклонено")
                        del self.pending_permissions[permission_id]
                        return
                except json.JSONDecodeError:
                    pass
                
                if "✅" in text.strip() or "разрешить" in text.strip().lower():
                    await self._send_permission_response(permission_id, True, perm_session_id)
                    await self.vk.send_message(user_id, "✅ Разрешение предоставлено")
                    del self.pending_permissions[permission_id]
                    return
                elif "❌" in text.strip() or "отказать" in text.strip().lower():
                    await self._send_permission_response(permission_id, False, perm_session_id)
                    await self.vk.send_message(user_id, "❌ Разрешение отклонено")
                    del self.pending_permissions[permission_id]
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

    async def _send_history(self, user_id: int, session_id: str):
        logger.info(f"Sending history for session {session_id} to user {user_id}")
        try:
            async with ClientSession() as session:
                url = f"{OPENCODE_URL}/session/{session_id}/message?limit=20"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        logger.error(f"Failed to get history: {resp.status}")
                        await self.vk.send_message(
                            user_id, "❌ Не удалось получить историю"
                        )
                        return
                    messages = await resp.json()

            history_file = SCRIPT_DIR / f"history_{user_id}_{int(time.time())}.json"
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)

            await self.vk.send_message(
                user_id, f"📜 Отправляю историю сессии ({len(messages)} сообщений)..."
            )
            await self.vk.send_file(
                user_id,
                str(history_file),
                f"history_{user_id}.json",
                f"📜 История сессии ({len(messages)} сообщений)",
            )

            history_file.unlink(missing_ok=True)
            logger.info(f"History sent to user {user_id}")
        except Exception as e:
            logger.exception(f"Error sending history: {e}")
            await self.vk.send_message(user_id, f"❌ Ошибка отправки истории: {e}")

    async def _new_session(self, user_id: int, workdir_path: str = None):
        logger.info(f"=== _new_session START ===")
        logger.info(f"user_id={user_id}")
        logger.info(f"workdir_path={workdir_path}")
        
        try:
            self.session_mgr.remove(user_id)
            
            if workdir_path:
                workdir = Path(workdir_path)
                if not workdir.exists():
                    logger.info(f"Creating workdir: {workdir}")
                    workdir.mkdir(parents=True, exist_ok=True)
            else:
                workdir = Path.cwd()
            
            logger.info(f"workdir: {workdir}, exists: {workdir.exists()}")
            
            # Инициализируем opencode в этой папке (даже если .opencode уже есть, переинициализация не повредит)
            try:
                init_result = subprocess.run(
                    [OPENCODE_BIN, "init"],
                    cwd=str(workdir),
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                logger.info(f"opencode init: returncode={init_result.returncode}")
                if init_result.returncode != 0:
                    logger.warning(f"opencode init stdout: {init_result.stdout}")
                    logger.warning(f"opencode init stderr: {init_result.stderr}")
            except Exception as e:
                logger.warning(f"opencode init warning: {e}")
            
            # Перезапускаем сервер с новым workdir
            logger.info(f"Calling restart with workdir={workdir}")
            await self.opencode_process.restart(workdir)
            
            # Дополнительная проверка готовности (на случай, если restart не выбросил исключение)
            try:
                async with aiohttp.ClientSession() as sess:
                    for _ in range(10):
                        try:
                            async with sess.get("http://localhost:4096/", timeout=1) as resp:
                                if resp.status == 200:
                                    break
                        except:
                            pass
                        await asyncio.sleep(0.5)
                    else:
                        raise Exception("Server not responding after restart")
            except Exception as e:
                await self.vk.send_message(user_id, f"❌ Сервер OpenCode не запустился: {e}")
                return
            
            # Создаём новую сессию через API
            async with ClientSession() as session:
                data = model_to_api_format(MODEL)
                async with session.post(f"{OPENCODE_URL}/session", json=data) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise Exception(f"HTTP {resp.status}: {text}")
                    resp_data = await resp.json()
                    new_session_id = resp_data["id"]
                    self.session_mgr.sessions[user_id] = new_session_id
                    self.session_mgr._save()
            
            logger.info(f"=== _new_session END ===")
            logger.info(f"new_session_id={new_session_id}")
            logger.info(f"final workdir={self.opencode_process.workdir}")
            
            await self.vk.send_message(user_id, f"✅ Новая сессия: {new_session_id}\nРабочая папка: {workdir}")
        except Exception as e:
            logger.exception(f"Error creating new session: {e}")
            await self.vk.send_message(user_id, f"❌ Ошибка создания сессии: {e}")

    async def _send_sessions(self, user_id: int):
        session_id = await self.session_mgr.get_or_create(user_id)
        sessions_text = ""
        for uid, sid in self.session_mgr.sessions.items():
            marker = "← вы" if uid == user_id else ""
            sessions_text += f"• `{sid}` (user={uid}) {marker}\n"
        await self.vk.send_message(user_id, f"📋 **Список сессий**:\n\n{sessions_text}")

    async def _handle_gpu_command(self, user_id: int):
        """Get GPU information using shared/nvidia.py parser."""
        message, error = await get_gpu_info_vk_message(timeout=30)
        if message:
            await self.vk.send_message(user_id, message)
        else:
            await self.vk.send_message(user_id, error)

    async def _send_help(self, user_id: int):
        help_text = """
🤖 **OpenCode VK Gateway - Команды**

/history - Получить историю сессии файлом
/history <session_id> - Получить историю конкретной сессии
/gpu - Показать информацию о GPU (nvidia-smi)
/logs - Отправить файл логов
/sessions - Показать список всех сессий
/clearsessions - Удалить все сессии
/models - Показать доступные модели
/m - То же что /models
/newsession - Создать новую сессию
/help - Показать эту справку
/restart - Перезапустить с текущей моделью
/restart <model> - Перезапустить с указанной моделью
/r <model> - То же что /restart <model>

Все остальные сообщения отправляются в opencode для обработки.
"""
        await self.vk.send_message(user_id, help_text)

    async def _opencode_flow(
        self, user_id: int, session_id: str, initial_text: str = ""
    ):
        event_queue = asyncio.Queue()
        seen_part_ids: set = self.session_mgr.get_seen_messages(session_id)
        logger.info(f"Loaded {len(seen_part_ids)} seen parts from file")
        
        if initial_text:
            async with ClientSession(timeout=ClientTimeout(total=30)) as oc_session:
                async with oc_session.get(f"{OPENCODE_URL}/session/{session_id}/message?limit=20") as resp:
                    if resp.status == 200:
                        messages = await resp.json()
                        for msg in messages:
                            for part in msg.get("parts", []):
                                part_id = part.get("id", "")
                                if part_id:
                                    seen_part_ids.add(part_id)
                        logger.info(f"After loading current: {len(seen_part_ids)} parts")

        final_text = None
        question_asked = False
        reasoning_parts: List[str] = []

        async with ClientSession(timeout=ClientTimeout(total=30)) as oc_session:
            if initial_text:
                url = f"{OPENCODE_URL}/session/{session_id}/prompt_async"
                data = {"parts": [{"type": "text", "text": initial_text}]}
                async with oc_session.post(url, json=data) as resp:
                    if resp.status != 204:
                        logger.error(f"prompt_async failed: {resp.status}")
                        await self.vk.send_message(
                            user_id, "❌ Ошибка запуска обработки"
                        )
                        return
                    logger.info(f"prompt_async sent for {session_id}")

        monitor_task = asyncio.create_task(
            self._poll_messages(user_id, session_id, event_queue, seen_part_ids)
        )

        try:
            await asyncio.sleep(0.5)

            while True:
                event = await event_queue.get()
                event_type = event.get("type")
                logger.info(f"Processing event: {event_type} for session {session_id}")

                if event_type == "check.question":
                    question = await self._check_pending_question(session_id)
                    if question:
                        await self._show_question(user_id, question)
                        question_asked = True
                        break
                    elif final_text:
                        break
                    else:
                        await self._send_final_message(user_id, session_id, final_text)
                        break

                elif event_type == "message.content":
                    final_text = event.get("all_text", "").strip()
                    if final_text:
                        logger.info(f"Sending message.content to VK: {final_text[:50]}...")
                        await self._send_long_message(user_id, final_text)
                    else:
                        logger.info("Skipping empty message.content")

                elif event_type == "reasoning.content":
                    reasoning_parts.extend(event.get("reasoning_parts", []))

                elif event_type == "session.error":
                    props = event.get("properties", {})
                    error = props.get("error", {})
                    error_name = error.get("name", "UnknownError")
                    error_data = error.get("data", {})
                    error_message = error_data.get("message", str(error))
                    logger.error(f"Session error: {error_name} - {error_message}")
                    if "null bytes" in error_message.lower():
                        await self.vk.send_message(
                            user_id,
                            "❌ Ошибка OpenCode: повреждён кеш snapshot.\n"
                            "Выполните в терминале: `rm -rf ~/.local/share/opencode/snapshot/` и попробуйте снова."
                        )
                    else:
                        await self.vk.send_message(
                            user_id,
                            f"❌ Ошибка сессии: {error_name}\n{error_message[:200]}"
                        )
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

    async def _poll_messages(
        self, user_id: int, session_id: str, event_queue: asyncio.Queue,
        seen_part_ids: set
    ):
        """
        Polls OpenCode session for new messages using GET /session/:id/message.
        Tracks seen messages by message.info.id and parts by part.info.id.
        """
        logger.info(f"Starting message poller for session {session_id}")
        poll_interval = 4
        max_silence_seconds = 120
        last_new_content_time = time.time()

        try:
            while True:
                try:
                    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
                        url = f"{OPENCODE_URL}/session/{session_id}/message?limit=20"
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                logger.warning(f"Poll got status {resp.status}, retrying...")
                                await asyncio.sleep(poll_interval)
                                continue
                            messages = await resp.json()

                        new_texts, new_reasonings = get_new_parts(messages, seen_part_ids)
                        
                        all_text_parts = new_texts
                        new_parts = new_texts
                        new_reasoning = new_reasonings
                        
                        for msg in messages:
                            for part in msg.get("parts", []):
                                part_id = part.get("id", "")
                                if part_id and part_id not in seen_part_ids:
                                    seen_part_ids.add(part_id)
                                    self.session_mgr.add_seen_message(session_id, part_id)
                        
                        if new_texts or new_reasonings:
                            last_new_content_time = time.time()
                            logger.info(f"Found {len(new_texts)} new texts, {len(new_reasonings)} reasonings")
                        
                        if new_reasonings and THINKING_PEER_ID:
                            logger.info(f"DEBUG: Sending {len(new_reasonings)} reasonings to {THINKING_PEER_ID}")
                            for rt in new_reasonings:
                                try:
                                    await self.vk.send_message(
                                        THINKING_PEER_ID,
                                        f"🧠 Рассуждение:\n{rt}",
                                    )
                                except Exception as e:
                                    logger.warning(f"Failed to send reasoning: {e}")

                        logger.info(f"Poll: new_parts={len(new_parts)}, all_text_parts={len(all_text_parts)}, new_reasoning={len(new_reasoning)}")
                        if new_reasonings:
                            logger.info(f"DEBUG: reasonings to send: {new_reasonings[:2]}...")
                        if new_parts:
                            logger.info(f"Putting message.content in queue with text: {all_text_parts[:100]}...")
                            event_queue.put_nowait({
                                "type": "message.content",
                                "text_parts": new_parts,
                                "all_text": "\n".join(all_text_parts)
                            })

                        if new_reasoning:
                            event_queue.put_nowait({
                                "type": "reasoning.content",
                                "reasoning_parts": new_reasoning
                            })

                        silence_duration = time.time() - last_new_content_time
                        if silence_duration > max_silence_seconds:
                            has_user_msg = any(
                                m.get("info", {}).get("role") == "user"
                                for m in messages[-3:]
                            )
                            if has_user_msg:
                                logger.info(f"Silence timeout, user message found - stopping")
                                if new_parts:
                                    logger.info(f"Flushing pending {len(new_parts)} parts before stop")
                                    event_queue.put_nowait({
                                        "type": "message.content",
                                        "text_parts": new_parts,
                                        "all_text": "\n".join(all_text_parts)
                                    })
                                break
                            logger.info(f"Silence timeout ({silence_duration:.1f}s), continuing to wait...")

                        await asyncio.sleep(poll_interval)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Poll error: {e}, retrying...")
                    await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            logger.info(f"Poller cancelled for {session_id}")
            raise

    async def _show_question(self, user_id: int, question_data: dict):
        question_id = question_data.get("id") or question_data.get("question_id")
        if not question_id:
            logger.error("No question_id in question_data")
            return

        self.waiting_for_answer[user_id] = question_id

        header = question_data.get("header", "Вопрос")
        question_text = question_data.get("question", question_data.get("text", ""))
        options = question_data.get("options", [])

        await self.vk.send_question_keyboard(
            peer_id=user_id,
            header=header,
            question_text=question_text,
            options=options,
        )

    async def _check_pending_question(self, session_id: str) -> Optional[dict]:
        try:
            async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.get(f"{OPENCODE_URL}/question") as resp:
                    if resp.status != 200:
                        logger.warning(f"Failed to get questions: {resp.status}")
                        return None
                    questions = await resp.json()
                    if not questions:
                        return None
                    for q in questions:
                        q_session_id = q.get("sessionID") or q.get("session_id")
                        if q_session_id == session_id:
                            logger.info(f"Found pending question for session {session_id}: {q.get('id')}")
                            return q
                    return None
        except Exception as e:
            logger.warning(f"Error checking pending questions: {e}")
            return None

    async def _send_permission_response(self, permission_id: str, allowed: bool, session_id: str):
        async with ClientSession() as session:
            url = f"{OPENCODE_URL}/session/{session_id}/permissions/{permission_id}"
            data = {"response": "always" if allowed else "never"}
            async with session.post(url, json=data) as resp:
                if resp.status == 200:
                    logger.info(f"Permission {permission_id} answered: {allowed}")
                else:
                    logger.error(f"Failed to reply to permission: {resp.status}")

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

    async def _send_final_message(self, user_id: int, session_id: str, final_text: str = None):
        if final_text and final_text.strip():
            await self._send_long_message(user_id, final_text.strip())
            return

        async with ClientSession() as session:
            url = f"{OPENCODE_URL}/session/{session_id}/message?limit=20"
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to get messages: {resp.status}")
                    await self.vk.send_message(user_id, "❌ Ошибка получения ответа")
                    return
                messages = await resp.json()
                if not messages:
                    logger.warning("No messages found in session")
                    await self.vk.send_message(user_id, "⚠️ Нет ответа от opencode")
                    return
                assistant_msg = None
                for msg in reversed(messages):
                    info = msg.get("info", {})
                    if info.get("role") == "assistant":
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
                    await self._send_long_message(user_id, text)
                else:
                    logger.warning("Assistant message has no text content")
                    await self.vk.send_message(user_id, "⚠️ Пустой ответ от opencode")

    async def _send_long_message(self, user_id: int, text: str, max_length: int = 4090):
        """Send a long message in parts to avoid VK API limits."""
        if len(text) <= max_length:
            await self.vk.send_message(user_id, text)
            return
        
        # Reserve space for the prefix "[Часть N/M]\n" (~15-20 chars)
        safe_content_limit = max_length - 25
        if safe_content_limit < 100:
            safe_content_limit = 100
        
        logger.info(f"Sending long message ({len(text)} chars) in parts to user {user_id}")
        
        parts = []
        current_part = ""
        lines = text.split('\n')
        
        for line in lines:
            if len(current_part) + len(line) + 1 > safe_content_limit:
                if current_part:
                    parts.append(current_part)
                    current_part = line
                else:
                    # Single line exceeds safe_content_limit - split it
                    parts.append(line[:safe_content_limit])
                    current_part = line[safe_content_limit:]
            else:
                if current_part:
                    current_part += '\n' + line
                else:
                    current_part = line
        
        if current_part:
            parts.append(current_part)
        
        # Safety check
        for i, part in enumerate(parts):
            if len(part) > safe_content_limit:
                logger.warning(f"Part {i+1} exceeds safe_content_limit ({len(part)} > {safe_content_limit}), trimming")
                parts[i] = part[:safe_content_limit]
        
        for i, part in enumerate(parts):
            logger.info(f"Sending part {i+1}/{len(parts)} ({len(part)} chars content)")
            await self.vk.send_message(user_id, f"[Часть {i+1}/{len(parts)}]\n{part}")
            if i < len(parts) - 1:
                await asyncio.sleep(0.5)

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
    logger.info(f"main() starting: SCRIPT_DIR={SCRIPT_DIR}, cwd={Path.cwd()}")
    
    # Проверяем запущен ли llama server
    import subprocess
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", "llama"],
            capture_output=True
        )
        if result.returncode != 0:
            # Сессия не существует, запускаем
            logger.info("llama tmux session not found, starting with default model")
            current_model = get_current_model()
            if current_model:
                await restart_llama_server(current_model, DEFAULT_MODEL)
            else:
                logger.warning("No models configured, cannot start llama server")
    except Exception as e:
        logger.warning(f"Failed to check llama session: {e}")
    
    opencode_process = OpenCodeProcess(logger, model=MODEL, workdir=SCRIPT_DIR)
    logger.info(f"OpenCodeProcess created with workdir={opencode_process.workdir}")
    await opencode_process.start()
    async with VKClient(VK_TOKEN) as vk:
        # Отправляем сообщение о старте
        try:
            await vk.send_message(
                5156890,
                "🤖 OpenCode VK Gateway запущен\n\nModel: {}\nWorkdir: {}".format(MODEL, SCRIPT_DIR)
            )
        except Exception as e:
            logger.warning(f"Failed to send startup message: {e}")
        
        poller = VKLongPoll(vk, session_mgr, opencode_process)
        try:
            await poller.run()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            await poller.stop()
        finally:
            await opencode_process.stop()


if __name__ == "__main__":
    asyncio.run(main())
