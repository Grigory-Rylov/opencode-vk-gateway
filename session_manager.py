"""
Управление сессиями OpenCode
"""
import json
from pathlib import Path
from typing import Dict

from aiohttp import ClientSession

from config import OPENCODE_URL, SESSION_FILE, MODEL
from logging_config import logger
from models import model_to_api_format


class SessionManager:
    """Управление сессиями пользователей OpenCode."""

    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.sessions: Dict[int, str] = {}
        self.seen_messages: Dict[str, set] = {}
        self.grant_mode: Dict[str, bool] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.sessions = {int(k): v for k, v in data.get("sessions", {}).items()}
                self.seen_messages = {
                    sid: set(ids) for sid, ids in data.get("seen_messages", {}).items()
                }
                self.grant_mode = {
                    sid: bool(val) for sid, val in data.get("grant_mode", {}).items()
                }
        except (FileNotFoundError, json.JSONDecodeError):
            self.sessions = {}
            self.seen_messages = {}
            self.grant_mode = {}

    def _save(self) -> None:
        data = {
            "sessions": {str(k): v for k, v in self.sessions.items()},
            "seen_messages": {
                sid: list(ids) for sid, ids in self.seen_messages.items()
            },
            "grant_mode": dict(self.grant_mode),
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
                if session_id not in self.grant_mode:
                    self.grant_mode[session_id] = False
                self._save()
                logger.info(
                    f"Created OpenCode session {session_id} for user {user_id} with model {MODEL}"
                )
                return session_id

    def get_seen_messages(self, session_id: str) -> set:
        return self.seen_messages.get(session_id, set())

    def add_seen_message(self, session_id: str, message_id: str):
        if session_id not in self.seen_messages:
            self.seen_messages[session_id] = set()
        self.seen_messages[session_id].add(message_id)
        logger.debug(f"Saved seen message {message_id} to file for session {session_id}")
        self._save()

    def remove(self, user_id: int):
        if user_id in self.sessions:
            session_id = self.sessions[user_id]
            del self.sessions[user_id]
            if session_id in self.seen_messages:
                del self.seen_messages[session_id]
            if session_id in self.grant_mode:
                del self.grant_mode[session_id]
            self._save()
            logger.info(f"Removed session for user {user_id}")

    def get_grant_mode(self, session_id: str) -> bool:
        """Получает состояние режима авто-разрешений для сессии"""
        return self.grant_mode.get(session_id, False)

    def set_grant_mode(self, session_id: str, enabled: bool) -> None:
        """Устанавливает состояние режима авто-разрешений для сессии"""
        # BUG: не проверяет, существует ли сессия
        if session_id not in self.grant_mode:
            self.grant_mode[session_id] = False
        self.grant_mode[session_id] = enabled
        self._save()
        logger.debug(f"Grant mode for session {session_id}: {enabled}")
