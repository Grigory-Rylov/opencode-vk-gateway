"""
Лонгполл слушатель VK
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientSession, ClientTimeout

import vk_keyboards
from config import (
    ATTACHES_DIR,
    DEFAULT_MODEL,
    LONGPOLL_WAIT,
    MODEL,
    MODELS,
    OPENCODE_URL,
    SCRIPT_DIR,
    SESSION_FILE,
    THINKING_PEER_ID,
)
from llama_server import do_restart
from logging_config import logger
from message_parser import get_new_parts
from models import model_to_api_format
from nvidia import get_gpu_info_vk_message
from opencode_client import OpenCodeClient
from opencode_process import OpenCodeProcess
from session_manager import SessionManager
from vk_client import VKClient

# Константы
POLL_INTERVAL = 4  # интервал опроса сессии (секунды)


def extract_command(text: str) -> str:
    """
    Извлекает команду из текста, игнорируя упоминания групп.
    Форматы: '@club123 /help' или '[club123|@club123] /help'
    """
    text = text.strip()

    # Если есть упоминание группы в формате [club...|@...] или [public...|@...]
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            return text[end + 1:].strip()

    # Если есть упоминание группы @club... или @public...
    if text.startswith("@"):
        parts = text.split(None, 1)
        if len(parts) > 1:
            return parts[1].strip()

    return text


class VKLongPoll:
    """Лонгполл слушатель VK для обработки сообщений."""

    def __init__(
        self,
        vk: VKClient,
        session_mgr: SessionManager,
        opencode_process: OpenCodeProcess,
    ):
        self.vk = vk
        self.session_mgr = session_mgr
        self.opencode_process = opencode_process
        self.server = None
        self.key = None
        self.ts = None
        self.running = False

        # HTTP клиент для OpenCode API
        self.opencode_client: Optional[OpenCodeClient] = None

        # Управление поллерами
        self.session_pollers: Dict[str, asyncio.Task] = {}  # session_id -> poller_task
        self.user_session: Dict[int, str] = {}  # user_id -> session_id

        # Временные хранилища
        self.waiting_for_answer: Dict[int, str] = {}
        self.pending_permissions: Dict[str, Tuple[str, int, int]] = {}
        self.seen_permissions: Dict[str, set] = {}
        self.seen_questions: Dict[str, set] = {}

    # ---------- Управление поллерами ----------
    async def _start_session_poller(self, user_id: int, session_id: str):
        """Запускает поллер для конкретной сессии"""
        if session_id in self.session_pollers:
            logger.warning(f"Poller for session {session_id} already exists")
            return

        logger.debug(f"Starting poller for session {session_id} (user {user_id})")
        poller_task = asyncio.create_task(
            self._poll_session_messages(user_id, session_id)
        )
        self.session_pollers[session_id] = poller_task
        self.user_session[user_id] = session_id

    async def _stop_session_poller(self, session_id: str):
        """Останавливает поллер для сессии"""
        if session_id in self.session_pollers:
            logger.debug(f"Stopping poller for session {session_id}")
            self.session_pollers[session_id].cancel()
            try:
                await self.session_pollers[session_id]
            except asyncio.CancelledError:
                pass
            del self.session_pollers[session_id]

    async def _stop_user_poller(self, user_id: int):
        """Останавливает поллер для пользователя"""
        if user_id in self.user_session:
            session_id = self.user_session[user_id]
            await self._stop_session_poller(session_id)
            del self.user_session[user_id]

    # ---------- Основной поллер сессии ----------
    async def _poll_session_messages(self, user_id: int, session_id: str):
        """Поллер для конкретной сессии - работает непрерывно"""
        logger.info(f"Poller started for session {session_id}")

        target_peer = THINKING_PEER_ID if THINKING_PEER_ID else user_id
        self._ensure_session_seen_messages(session_id)

        try:
            while True:
                try:
                    await self._process_session_updates(
                        session_id, user_id, target_peer
                    )
                    await asyncio.sleep(POLL_INTERVAL)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Poller error for session {session_id}: {e}")
                    await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info(f"Poller stopped for session {session_id}")
            raise

    def _ensure_session_seen_messages(self, session_id: str):
        """Гарантирует наличие словаря seen_messages для сессии"""
        if session_id not in self.session_mgr.seen_messages:
            self.session_mgr.seen_messages[session_id] = set()

    async def _process_session_updates(
        self, session_id: str, user_id: int, target_peer: int
    ):
        """Обрабатывает обновления для сессии"""
        messages = await self.opencode_client.get_session_messages(session_id)
        if not messages:
            return

        new_parts = self._get_new_message_parts(session_id, messages)
        await self._send_new_parts(new_parts, user_id, target_peer)

        await self._check_permissions(session_id, user_id)
        await self._check_questions(session_id, user_id)

    def _get_new_message_parts(self, session_id: str, messages: List[dict]) -> List:
        """Извлекает новые части сообщений"""
        new_parts = get_new_parts(messages, self.session_mgr.seen_messages[session_id])

        result_parts = []
        for part in new_parts:
            text = part.text
            # Если текст пустой (None или "") - игнорируем part полностью
            if text is None or text == "":
                logger.debug(f"Ignoring empty part: type={part.type}, id={part.id}")
                continue
            # Сохраняем в просмотренные (даже если только пробелы)
            self.session_mgr.add_seen_message(session_id, part.id)
            result_parts.append(part)

        return result_parts

    async def _send_new_parts(self, parts: List, user_id: int, target_peer: int):
        """Отправляет новые части сообщений пользователю"""
        for part in parts:
            await self._send_part_by_type(part, user_id, target_peer)

    async def _send_part_by_type(self, part, user_id: int, target_peer: int):
        """Отправляет часть сообщения в зависимости от ее типа"""
        text = part.text or ""
        # Если текст только из пробелов/переносов - не отправляем, но уже сохранено в просмотренные
        if not text.strip():
            logger.debug(
                f"Skipping whitespace-only part: type={part.type}, id={part.id}"
            )
            return

        if part.type == "tool":
            await self.vk.send_message(target_peer, f"🧠: Tool\n{text}")
        elif part.type == "reasoning":
            await self.vk.send_message(target_peer, f"🧠:\n{text}")
        else:  # text
            await self.vk.send_message(user_id, text)

    # ---------- Обработка разрешений ----------
    async def _check_permissions(self, session_id: str, user_id: int):
        """Проверяет новые запросы разрешений"""
        permissions = await self.opencode_client.get_pending_permissions()
        if not permissions:
            return

        if session_id not in self.seen_permissions:
            self.seen_permissions[session_id] = set()

        for perm in permissions:
            await self._process_permission(perm, session_id, user_id)

    async def _process_permission(self, perm: dict, session_id: str, user_id: int):
        """Обрабатывает один запрос разрешения"""
        perm_id = perm.get("id")
        perm_session_id = perm.get("sessionID") or perm.get("session_id")

        if (
            perm_session_id != session_id
            or perm_id in self.seen_permissions[session_id]
        ):
            return

        self.seen_permissions[session_id].add(perm_id)

        # Логируем полную структуру для отладки
        logger.info(f"Permission request: {json.dumps(perm, ensure_ascii=False)}")

        msg = self._format_permission_message(perm)
        keyboard = self._create_permission_keyboard()
        msg_id = await self.vk.send_message(user_id, msg, keyboard=keyboard)

        self.pending_permissions[perm_id] = (session_id, user_id, msg_id)
        logger.info(f"Sent permission request {perm_id} to user {user_id}")

    def _format_permission_message(self, perm: dict) -> str:
        """Форматирует сообщение для запроса разрешения.

        Поддерживает два формата API:
        1. Legacy (старый opencode): {permission, metadata: {filepath, parentDir}}
        2. Crush (новый): {tool_name, action, path} - путь на top-level

        Для external_directory OpenCode не передаёт путь,
        поэтому используем workdir как fallback.
        """
        import json

        # Поддержка обоих форматов API
        perm_type = perm.get("permission") or perm.get("action") or "unknown"
        tool_name = perm.get("tool_name", "")

        # Crush API: путь на top-level
        path = perm.get("path", "")

        # Legacy API: путь в metadata
        if not path:
            metadata = perm.get("metadata", {})
            path = (
                metadata.get("filepath")
                or metadata.get("file")
                or metadata.get("filePath")
                or metadata.get("path")
                or ""
            )

        # Legacy API: parent_dir для директорий
        parent_dir = ""
        if not parent_dir and perm_type == "external_directory":
            metadata = perm.get("metadata", {})
            parent_dir = (
                metadata.get("parentDir")
                or metadata.get("parent_dir")
                or metadata.get("directory")
                or metadata.get("dir")
                or metadata.get("path")
                or ""
            )

        # Fallback: для external_directory используем workdir (рабочий каталог opencode)
        # OpenCode не передаёт path для external_directory — это всегда workdir
        if not parent_dir and not path and perm_type == "external_directory":
            workdir = getattr(self.opencode_process, "workdir", None)
            if workdir:
                parent_dir = str(workdir)

        # Формируем сообщение в зависимости от типа
        if perm_type == "external_directory":
            display_path = parent_dir or path
            if display_path:
                return f"⚠️ **Запрос разрешения**\n\nТип: `{perm_type}`\n\nПрограмма хочет получить доступ к директории:\n`{display_path}`"
            else:
                return f"⚠️ **Запрос разрешения**\n\nТип: `{perm_type}`\n\nПрограмма хочет получить доступ к директории.\n\nДанные: `{json.dumps(perm, ensure_ascii=False)}`"
        elif perm_type in ("write_file", "edit", "multi_edit"):
            if path:
                return f"⚠️ **Запрос разрешения**\n\nИнструмент: `{tool_name or perm_type}`\n\nПрограмма хочет записать файл:\n`{path}`"
            else:
                return f"⚠️ **Запрос разрешения**\n\nИнструмент: `{tool_name or perm_type}`\n\nПрограмма хочет записать файл.\n\nДанные: `{json.dumps(perm, ensure_ascii=False)}`"
        elif perm_type in ("read_file", "view", "read"):
            if path:
                return f"⚠️ **Запрос разрешения**\n\nИнструмент: `{tool_name or perm_type}`\n\nПрограмма хочет прочитать файл:\n`{path}`"
            else:
                return f"⚠️ **Запрос разрешения**\n\nИнструмент: `{tool_name or perm_type}`\n\nПрограмма хочет прочитать файл.\n\nДанные: `{json.dumps(perm, ensure_ascii=False)}`"
        elif perm_type == "bash" or (tool_name == "bash"):
            # Bash permissions - показываем команду из params
            params = perm.get("params", {})
            command = params.get("command", params.get("cmd", "")) if isinstance(params, dict) else ""
            bash_path = path or params.get("working_directory", "") if isinstance(params, dict) else ""
            display = command or bash_path
            if display:
                return f"⚠️ **Запрос разрешения**\n\nИнструмент: `bash`\n\nПрограмма хочет выполнить команду:\n`{display}`"
            else:
                return f"⚠️ **Запрос разрешения**\n\nИнструмент: `bash`\n\nПрограмма хочет выполнить команду.\n\nДанные: `{json.dumps(perm, ensure_ascii=False)}`"
        else:
            # Для неизвестных типов показываем полную информацию
            return f"⚠️ **Запрос разрешения**\n\nИнструмент: `{tool_name or perm_type}`\n\nДанные: `{json.dumps(perm, ensure_ascii=False)}`"

    def _create_permission_keyboard(self) -> dict:
        """Создает клавиатуру для ответа на разрешение"""
        return vk_keyboards.get_permission_keyboard()

    # ---------- Обработка вопросов ----------
    async def _check_questions(self, session_id: str, user_id: int):
        """Проверяет новые вопросы от OpenCode"""
        questions = await self.opencode_client.get_pending_questions()
        if not questions:
            return

        if session_id not in self.seen_questions:
            self.seen_questions[session_id] = set()

        for q in questions:
            await self._process_question(q, session_id, user_id)

    async def _process_question(self, q: dict, session_id: str, user_id: int):
        """Обрабатывает один вопрос"""
        q_id = q.get("id")
        q_session_id = q.get("sessionID") or q.get("session_id")

        if q_session_id != session_id or q_id in self.seen_questions[session_id]:
            return

        self.seen_questions[session_id].add(q_id)
        logger.info(f"Found new question {q_id} for session {session_id}")

        actual_question = q.get("questions", [{}])[0] if q.get("questions") else q
        await self._show_question(user_id, actual_question, original_id=q_id)

    async def _show_question(
        self, user_id: int, question_data: dict, original_id: str = None
    ):
        """Показывает вопрос пользователю с клавиатурой"""
        question_id = (
            original_id or question_data.get("id") or question_data.get("question_id")
        )
        if not question_id:
            logger.error("No question_id in question_data")
            return

        header, question_text, options = self._extract_question_data(question_data)

        self.waiting_for_answer[user_id] = question_id

        try:
            keyboard = vk_keyboards.get_question_keyboard(options)
            text = f"🔧 {header}\n\n{question_text}"
            await self.vk.send_message(user_id, text, keyboard=keyboard)
            logger.info(f"Sent question {question_id} to user {user_id}")
        except Exception as e:
            logger.error(f"Failed to send question keyboard: {e}")
            await self._send_question_fallback(user_id, header, question_text, options)

    def _extract_question_data(
        self, question_data: dict
    ) -> Tuple[str, str, List[dict]]:
        """Извлекает заголовок, текст и опции из данных вопроса"""
        header = question_data.get("header") or question_data.get("title") or "Вопрос"

        question_text = (
            question_data.get("question")
            or question_data.get("text")
            or question_data.get("description")
            or question_data.get("prompt")
            or ""
        )

        if not question_text and "metadata" in question_data:
            question_text = (
                question_data["metadata"].get("question")
                or question_data["metadata"].get("text")
                or ""
            )

        options = question_data.get("options", [])
        if not options and "metadata" in question_data:
            options = question_data["metadata"].get("options", [])
        if not options and "choices" in question_data:
            options = question_data["choices"]

        if options and isinstance(options[0], str):
            options = [{"label": opt} for opt in options]

        if not options:
            options = [{"label": "✅ Да"}, {"label": "❌ Нет"}]
            if not question_text:
                question_text = "Пожалуйста, выберите вариант"

        return header, question_text, options

    async def _send_question_fallback(
        self, user_id: int, header: str, question_text: str, options: List[dict]
    ):
        """Запасной вариант отправки вопроса обычным текстом"""
        options_text = ", ".join([opt["label"] for opt in options])
        await self.vk.send_message(
            user_id,
            f"❌ Ошибка отображения вопроса. Пожалуйста, ответьте текстом.\n\n"
            f"{header}\n{question_text}\nВарианты: {options_text}",
        )

    async def _handle_question_answer(
        self, user_id: int, question_id: str, answer: str
    ):
        """Обрабатывает ответ пользователя на вопрос"""
        success = await self.opencode_client.send_question_answer(question_id, answer)
        if success:
            await self.vk.send_message(
                user_id,
                f"✅ Вы выбрали: {answer}",
                keyboard=vk_keyboards.get_main_keyboard(),
            )
        else:
            await self.vk.send_message(
                user_id,
                "❌ Ошибка отправки ответа",
                keyboard=vk_keyboards.get_main_keyboard(),
            )

    # ---------- Обработка команд ----------
    async def _get_long_poll_events(self) -> Tuple[List[dict], int, Optional[int]]:
        """Получает события из long poll.
        Возвращает (updates, ts, failed_code). failed_code=None если всё ок.
        """
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
                    failed_code = data.get("failed")
                    error_msg = data.get("error", "unknown error")
                    return [], self.ts, failed_code
                return data.get("updates", []), int(data["ts"]), None

    async def _refresh_long_poll_server(self):
        """Обновляет сервер long poll"""
        self.server, self.key, self.ts = await self.vk.get_long_poll_server()
        logger.info(f"Long Poll server refreshed: {self.server}")

    async def _handle_message_new(self, event: list):
        """Обрабатывает новое сообщение"""
        msg_id = int(event[1])
        flags = int(event[2])
        peer_id = int(event[3])
        text = event[5] if len(event) > 5 else ""

        if flags & 2:
            return

        user_id = peer_id
        logger.info(f"New message from {user_id}: text='{text[:50]}...'")

        # Пропускаем сообщения из thinking_peer_id
        if THINKING_PEER_ID and peer_id == THINKING_PEER_ID:
            logger.debug(f"Ignoring message from thinking_peer_id {peer_id}")
            return

        # Извлекаем команду, убирая упоминание группы
        cmd = extract_command(text)

        if cmd == "/start":
            return
        if cmd == "/update":
            return
        if cmd == "/status":
            return

        if cmd.startswith("/restart") or cmd.startswith("/r"):
            await self._handle_restart_command(user_id, cmd)
            return

        if cmd.startswith("/models") or cmd == "/m":
            await self._handle_models_command(user_id)
            return

        if cmd.startswith("/history"):
            await self._handle_history_command(user_id, cmd)
            return

        if cmd.startswith("/newsession") or cmd == "/n":
            await self._handle_new_session_command(user_id)
            return

        if cmd == "/sessions":
            await self._handle_sessions_command(user_id)
            return

        if cmd.startswith("/logs"):
            await self._handle_logs_command(user_id)
            return

        if cmd == "/help":
            await self._send_help(user_id)
            return

        if cmd == "/gpu":
            await self._handle_gpu_command(user_id)
            return

        if cmd == "/clean_attaches":
            await self._handle_clean_attaches_command(user_id)
            return

        # Обработка ответов на вопросы
        if user_id in self.waiting_for_answer:
            question_id = self.waiting_for_answer.pop(user_id)
            await self._handle_question_answer(user_id, question_id, text)
            return

        # Обработка ответов на разрешения
        for permission_id, perm_data in list(self.pending_permissions.items()):
            perm_session_id, perm_user_id, perm_msg_id = perm_data
            if perm_user_id == user_id:
                answer = text.strip().lower()
                response = None
                result_text = None

                if "✅" in answer or "навсегда" in answer:
                    response = "always"
                    result_text = "✅ Разрешение предоставлено навсегда"
                elif "🔄" in answer or "разово" in answer:
                    response = "once"
                    result_text = "🔄 Разрешение предоставлено разово"
                elif "❌" in answer or "никогда" in answer:
                    response = "never"
                    result_text = "❌ Разрешение отклонено навсегда"
                else:
                    continue

                await self.opencode_client.send_permission_response(
                    perm_session_id, permission_id, response
                )
                await self.vk.edit_message(
                    peer_id=user_id,
                    message_id=perm_msg_id,
                    text=result_text,
                    keyboard=vk_keyboards.get_main_keyboard(),
                )
                del self.pending_permissions[permission_id]
                return

        # Обычное сообщение
        full_msgs = await self.vk.get_messages_by_ids([msg_id])
        if not full_msgs:
            return
        full_msg = full_msgs[0]
        await self._handle_user_message(user_id, full_msg)

    async def _handle_user_message(self, user_id: int, message: dict):
        """Обрабатывает обычное сообщение пользователя"""
        text = message.get("text", "")
        attachments = message.get("attachments", [])

        logger.debug(
            f"Message from {user_id}: text_len={len(text)}, attachments_count={len(attachments)}"
        )

        session_id = await self.session_mgr.get_or_create(user_id)

        # Останавливаем старый поллер если сессия изменилась
        if user_id in self.user_session and self.user_session[user_id] != session_id:
            await self._stop_user_poller(user_id)

        # Запускаем поллер если еще не запущен
        if session_id not in self.session_pollers:
            await self._start_session_poller(user_id, session_id)

        # Обрабатываем аттачи
        attachment_info = ""
        if attachments:
            logger.debug(
                f"Processing {len(attachments)} attachment(s) for user {user_id}"
            )
            for att in attachments:
                logger.debug(f"Attachment type: {att.get('type')}")
            downloaded = await self.vk.download_attachments(attachments, ATTACHES_DIR)
            if downloaded:
                attachment_info = self._format_attachment_info(downloaded)
                logger.debug(
                    f"Downloaded {len(downloaded)} attachments for user {user_id}"
                )
            else:
                logger.debug(
                    f"No attachments were downloaded (count={len(attachments)})"
                )

        # Формируем полный текст с информацией об аттачах
        full_text = text
        if attachment_info:
            if text:
                full_text = f"{text}\n\n{attachment_info}"
            else:
                full_text = attachment_info

        # Отправляем запрос в OpenCode
        success = await self.opencode_client.send_prompt(session_id, full_text)
        if not success:
            await self.vk.send_message(user_id, "❌ Ошибка отправки запроса")

    def _format_attachment_info(self, attachments: List[dict]) -> str:
        """Форматирует информацию об аттачах для отправки в OpenCode"""
        lines = [f"📥 Downloaded {len(attachments)} file(s):"]
        for att in attachments:
            att_type = att.get("type", "unknown")
            filename = att.get("filename", "unknown")
            path = att.get("path", "")
            lines.append(f"• [{att_type}] `{filename}` saved to: `{path}`")
        return "\n".join(lines)

    async def _handle_restart_command(self, user_id: int, text: str):
        """Обрабатывает команду /restart"""
        parts = text.strip().split()
        model_alias = parts[1] if len(parts) > 1 else None

        model_info, error = await do_restart(
            self.vk,
            user_id,
            model_alias,
            opencode_process=self.opencode_process,
            session_mgr=self.session_mgr,
            current_model=MODEL,
            current_default=DEFAULT_MODEL,
        )

        if error:
            await self.vk.send_message(user_id, f"❌ {error}")
        else:
            await self.vk.send_message(user_id, f"✅ Модель {model_info} загружена")

    async def _handle_models_command(self, user_id: int):
        """Обрабатывает команду /models"""
        if not MODELS:
            await self.vk.send_message(user_id, "Нет доступных моделей")
        else:
            models_text = "📋 **Доступные модели:**\n\n"
            for alias, m in MODELS.items():
                marker = " ← текущая" if alias == DEFAULT_MODEL else ""
                models_text += f"• {alias}{marker}\n"
            await self.vk.send_message(user_id, models_text)

    async def _handle_history_command(self, user_id: int, text: str):
        """Обрабатывает команду /history"""
        parts = text.strip().split()
        session_id = (
            parts[1]
            if len(parts) > 1
            else await self.session_mgr.get_or_create(user_id)
        )
        await self._send_history(user_id, session_id)

    async def _send_history(self, user_id: int, session_id: str):
        """Отправляет историю сессии"""
        logger.info(f"Sending history for session {session_id} to user {user_id}")
        try:
            messages = await self.opencode_client.get_session_messages(
                session_id, limit=50
            )
            if not messages:
                await self.vk.send_message(user_id, "❌ Не удалось получить историю")
                return

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
        except Exception as e:
            logger.exception(f"Error sending history: {e}")
            await self.vk.send_message(user_id, f"❌ Ошибка отправки истории: {e}")

    async def _handle_new_session_command(self, user_id: int):
        """Обрабатывает команду /newsession"""
        await self._new_session(user_id)

    async def _new_session(self, user_id: int):
        """Создает новую сессию, очищая все старые данные."""
        logger.info(f"Creating new session for user {user_id}")

        # Останавливаем все поллеры пользователя
        await self._stop_user_poller(user_id)

        # Удаляем файл сессий
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()

        # Очищаем все данные сессий
        self.session_mgr.sessions.clear()
        self.session_mgr.seen_messages.clear()
        self.session_mgr._save()

        # Очищаем временные данные
        self.pending_permissions.clear()
        self.seen_permissions.clear()
        self.seen_questions.clear()

        # Создаем новую сессию через API
        new_session_id = await self.opencode_client.create_session()

        self.session_mgr.sessions[user_id] = new_session_id
        if new_session_id not in self.session_mgr.seen_messages:
            self.session_mgr.seen_messages[new_session_id] = set()
        self.session_mgr._save()

        await self._start_session_poller(user_id, new_session_id)

        await self.vk.send_message(
            user_id,
            f"✅ Новая сессия создана: {new_session_id}\n"
            f"Старые сессии и данные очищены.",
        )

    async def _handle_sessions_command(self, user_id: int):
        """Обрабатывает команду /sessions"""
        sessions_text = ""
        for uid, sid in self.session_mgr.sessions.items():
            marker = "← вы" if uid == user_id else ""
            sessions_text += f"• `{sid}` (user={uid}) {marker}\n"
        await self.vk.send_message(user_id, f"📋 **Список сессий**:\n\n{sessions_text}")

    async def _handle_logs_command(self, user_id: int):
        """Обрабатывает команду /logs"""
        await self.vk.send_file(
            user_id, str(SCRIPT_DIR / "debug.log"), "debug.log", "📋 Логи"
        )

    async def _handle_gpu_command(self, user_id: int):
        """Обрабатывает команду /gpu"""
        message, error = await get_gpu_info_vk_message(timeout=30)
        if message:
            await self.vk.send_message(user_id, message)
        else:
            await self.vk.send_message(user_id, error)

    async def _handle_clean_attaches_command(self, user_id: int):
        """Обрабатывает команду /clean_attaches - очищает папку с аттачами"""
        import shutil

        if not ATTACHES_DIR.exists():
            await self.vk.send_message(user_id, "📁 Attaches folder does not exist")
            return

        # Подсчитываем файлы перед удалением
        files = list(ATTACHES_DIR.iterdir())
        file_count = len(files)

        if file_count == 0:
            await self.vk.send_message(user_id, "📁 Attaches folder is already empty")
            return

        # Удаляем все файлы
        for f in files:
            try:
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
            except Exception as e:
                logger.warning(f"Failed to delete {f}: {e}")

        await self.vk.send_message(
            user_id, f"🗑️ Cleaned {file_count} file(s) from attaches folder"
        )

    async def _send_help(self, user_id: int):
        """Отправляет справку"""
        help_text = """
🤖 **OpenCode VK Gateway - Команды**

/history - Получить историю сессии файлом
/history <session_id> - Получить историю конкретной сессии
/gpu - Показать информацию о GPU (nvidia-smi)
/logs - Отправить файл логов
/sessions - Показать список всех сессий
/newsession - Создать новую сессию (очищает старые)
/n - То же что /newsession
/models - Показать доступные модели
/m - То же что /models
/clean_attaches - Очистить папку с аттачами
/help - Показать эту справку
/restart - Перезапустить с текущей моделью
/restart <model> - Перезапустить с указанной моделью
/r <model> - То же что /restart <model>

Все остальные сообщения отправляются в opencode для обработки.
"""
        await self.vk.send_message(user_id, help_text)

    # ---------- Основной цикл ----------
    async def run(self):
        """Запускает long poll для получения новых сообщений"""
        self.running = True

        # Инициализируем HTTP клиент для OpenCode
        self.opencode_client = OpenCodeClient()
        await self.opencode_client.__aenter__()

        try:
            await self._refresh_long_poll_server()

            while self.running:
                try:
                    updates, new_ts, failed_code = await self._get_long_poll_events()

                    if failed_code is not None:
                        # failed: 1 - история устарела, нужен новый ts
                        # failed: 2 - ключ истёк
                        # failed: 3 - информация потеряна
                        logger.debug(
                            f"Long poll key expired (failed={failed_code}), refreshing..."
                        )
                        await self._refresh_long_poll_server()
                        continue

                    self.ts = new_ts

                    for update in updates:
                        if isinstance(update, list) and update[0] == 4:
                            asyncio.create_task(self._handle_message_new(update))

                except asyncio.CancelledError:
                    break
                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    logger.warning(f"Long poll error: {e}. Reconnecting...")
                    await asyncio.sleep(3)
                    await self._refresh_long_poll_server()
                except Exception as e:
                    logger.exception(f"Long poll error: {e}")
                    await asyncio.sleep(3)
                    await self._refresh_long_poll_server()
        finally:
            await self.opencode_client.__aexit__(None, None, None)

    async def stop(self):
        """Останавливает все поллеры"""
        self.running = False
        for session_id in list(self.session_pollers.keys()):
            await self._stop_session_poller(session_id)
