"""Парсер сообщений OpenCode."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Part:
    id: str
    type: str
    text: Optional[str] = None


@dataclass
class Message:
    id: str
    role: str
    parts: List[Part]


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
            
            msg_parts.append(Part(
                id=part_id,
                type=part_type,
                text=part_text if part_text else None
            ))
            
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
        user_messages=user_messages
    )


def get_new_parts(messages: List[dict], seen_part_ids: set) -> tuple[List[str], List[str]]:
    """
    Возвращает новые тексты и рассуждения, которых нет в seen_part_ids.
    """
    new_texts: List[str] = []
    new_reasonings: List[str] = []
    
    for msg in messages:
        if msg.get("info", {}).get("role") != "assistant":
            continue
            
        for part in msg.get("parts", []):
            part_id = part.get("id", "")
            if not part_id or part_id in seen_part_ids:
                continue
                
            part_type = part.get("type", "")
            part_text = part.get("text")
            
            if part_type == "text" and part_text:
                new_texts.append(part_text)
            elif part_type == "reasoning" and part_text:
                new_reasonings.append(part_text)
    
    return new_texts, new_reasonings