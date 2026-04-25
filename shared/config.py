"""
Конфигурация - загрузка и дефолтные значения
"""
import json
from pathlib import Path


DEFAULT_CONFIG = {
    "vk_token": "token",
    "opencode_url": "http://127.0.0.1:4096",
    "session_file": "sessions.json",
    "vk_api_version": "5.200",
    "longpoll_wait": 25,
    "thinking_peer_id": 2000000506,
    "model": "llama.cpp/qwen3.5-122b",
    "llama_server_path": "llama-server",
    "models": [],
    "default_model": "qwen3.5-122b",
}


def load_config(config_path: str = "config.json") -> dict:
    """Загружает конфигурацию из JSON-файла."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
        config = {**DEFAULT_CONFIG, **user_config}
    except FileNotFoundError:
        print(f"Config file {config_path} not found, using defaults.")
        config = DEFAULT_CONFIG.copy()
    except json.JSONDecodeError as e:
        print(f"Error parsing config file {config_path}: {e}")
        raise
    return config