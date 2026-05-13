"""Парсер сообщений OpenCode."""

import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Part:
    id: str
    type: str
    text: Optional[str] = None


@dataclass
class ParsedSession:
    assistant_texts: List[str]
    assistant_reasonings: List[str]
    user_messages: List[str]


def parse_session_messages(messages: List[dict]) -> ParsedSession:
    """
    Парсит историю сообщений из OpenCode API.

    Returns:
        ParsedSession с текстами assistant, рассуждениями и сообщениями пользователя
    """
    assistant_texts: List[str] = []
    assistant_reasonings: List[str] = []
    user_messages: List[str] = []

    for msg in messages:
        info = msg.get("info", {})
        role = info.get("role", "")
        msg_id = info.get("id", "")

        if role != "assistant" and role != "user":
            continue

        msg_parts = []
        for part in msg.get("parts", []):
            part_id = part.get("id", "")
            part_type = part.get("type", "")
            part_text = part.get("text")

            msg_parts.append(
                Part(id=part_id, type=part_type, text=part_text if part_text else None)
            )

            if role == "assistant":
                if part_type == "text" and part_text:
                    assistant_texts.append(part_text)
                elif part_type == "reasoning" and part_text:
                    assistant_reasonings.append(part_text)
            elif role == "user":
                if part_type == "text" and part_text:
                    user_messages.append(part_text)

    return ParsedSession(
        assistant_texts=assistant_texts,
        assistant_reasonings=assistant_reasonings,
        user_messages=user_messages,
    )


def get_new_parts(messages: List[dict], seen_part_ids: set) -> List[Part]:
    """
    Возвращает список новых объектов Part (id, text, type) из сообщений ассистента,
    которых нет в seen_part_ids.

    Поддерживаемые типы:
        - "text"      -> text = part.get("text", "")
        - "reasoning" -> text = part.get("text", "")
        - "tool"      -> text = part.get("state", {}).get("output", "")

    Args:
        messages: список сообщений от API
        seen_part_ids: множество id уже обработанных частей

    Returns:
        List[Part] — новые части (только text, reasoning и tool)
    """
    new_parts: List[Part] = []

    for msg in messages:
        if msg.get("info", {}).get("role") != "assistant":
            continue

        for part in msg.get("parts", []):
            part_id = part.get("id", "")
            if not part_id or part_id in seen_part_ids:
                continue

            part_type = part.get("type", "")

            # Извлекаем текст в зависимости от типа
            if part_type == "text":
                text = part.get("text", "")
            elif part_type == "reasoning":
                text = part.get("text", "")
            elif part_type == "tool":
                # Для тула берём output из state
                text = (
                    part.get("tool", "")
                    + " - "
                    + part.get("state", {}).get("status", "")
                )
            else:
                # Неизвестный тип — пропускаем
                logger.warning("Unknown type: %s", part_type)
                continue

            new_parts.append(Part(id=part_id, type=part_type, text=text))

    return new_parts