"""
Управление llama сервером
"""
import asyncio
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import aiohttp

from config import (
    SCRIPT_DIR,
    MCP_SERVERS,
    OPENCODE_CONFIG_PATH,
    LLAMA_SERVER_PATH,
    load_config,
)
from logging_config import logger
from models import get_model_by_alias, get_current_model
from config import load_config


# Константы
LLAMA_CHECK_URL = "http://localhost:8081/"
LLAMA_STARTUP_TIMEOUT = 300  # секунд
LLAMA_CHECK_INTERVAL = 5  # секунд


async def restart_llama_server(
    model: dict, alias: str = None, llama_path: str = None
) -> bool:
    """Перезапускает llama server с указанной моделью."""
    if llama_path is None:
        logger.error("llama_path is empty")

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

    path = llama_path
    args = model.get("args", "")
    cmd = f"{path} {args}"

    try:
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
            logger.info(
                f"Started llama server with model {alias or 'unknown'}, pid={proc.pid}"
            )
            await asyncio.sleep(3)
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to start llama server: {e}")
        return False


async def wait_for_llama_server(
    timeout: int = LLAMA_STARTUP_TIMEOUT,
    interval: int = LLAMA_CHECK_INTERVAL
) -> bool:
    """Ждёт готовности llama сервера."""
    waited = 0

    while waited < timeout:
        await asyncio.sleep(interval)
        waited += interval
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(LLAMA_CHECK_URL, timeout=2) as resp:
                    if resp.status == 200:
                        return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass

    return False


def update_opencode_config(model: dict, alias: str) -> bool:
    """Обновляет конфиг OpenCode с провайдером и MCP."""
    try:
        opencode_config_path = OPENCODE_CONFIG_PATH
        opencode_config = {
            "$schema": "https://opencode.ai/config.json",
            "model": model.get("model", ""),
            "provider": {
                "llama.cpp": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "llama-server (local)",
                    "options": {"baseURL": "http://localhost:8081/v1"},
                    "models": {
                        alias: {
                            "name": f"{alias} (local)",
                            "limit": {"context": 131072, "output": 65536},
                        }
                    },
                }
            },
        }
        if MCP_SERVERS:
            opencode_config["mcp"] = MCP_SERVERS
            logger.info(f"Added {len(MCP_SERVERS)} MCP server(s) to opencode config")
        with open(opencode_config_path, "w") as f:
            json.dump(opencode_config, f, indent=2)
        logger.info(f"Updated opencode config with provider and MCP for {alias}")
        return True
    except Exception as e:
        logger.warning(f"Failed to update opencode config: {e}")
        return False


def save_model_config(model_name: str, alias: str) -> bool:
    """Сохраняет модель в конфиг файл."""
    try:
        config = load_config()
        config["default_model"] = alias
        config["model"] = model_name
        with open(SCRIPT_DIR / "config.json", "w") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.warning(f"Failed to save config: {e}")
        return False


async def do_restart(
    vk_client,
    user_id: int,
    model_alias: str = None,
    opencode_process=None,
    session_mgr=None,
    current_model: str = None,
    current_default: str = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Выполняет перезапуск с указанной моделью или текущей.

    Args:
        vk_client: Клиент VK для отправки сообщений
        user_id: ID пользователя
        model_alias: Алиас модели (если None, используется текущая)
        opencode_process: Процесс OpenCode для перезапуска
        session_mgr: Менеджер сессий
        current_model: Текущая модель (передаётся извне)
        current_default: Текущий дефолтный алиас (передаётся извне)

    Returns:
        Tuple[model_name, error_message] - один из элементов будет None
    """
    if model_alias:
        model = get_model_by_alias(model_alias)
        if not model:
            return None, f"Модель '{model_alias}' не найдена"
        alias = model_alias
    else:
        model = get_current_model()
        if not model:
            return None, "Нет доступных моделей"
        alias = current_default or "default"

    # Перезапускаем llama server
    await vk_client.send_message(user_id, f"🔄 Загружаю модель {alias}...")
    llama_success = await restart_llama_server(model, alias, LLAMA_SERVER_PATH)
    if not llama_success:
        await vk_client.send_message(user_id, "⚠️ Не удалось запустить llama server")
        logger.warning("Failed to restart llama server")

    # Обновляем конфиг opencode с провайдером и MCP
    update_opencode_config(model, alias)

    # Ждем пока модель загрузится
    ready = await wait_for_llama_server()

    if ready:
        await vk_client.send_message(user_id, f"✅ Модель {alias} загружена и готова!")
        logger.info(f"Model {alias} loaded successfully")
    else:
        await vk_client.send_message(
            user_id, f"⚠️ Модель {alias} не ответила за {LLAMA_STARTUP_TIMEOUT} сек, продолжаю..."
        )
        logger.warning(f"Model {alias} did not respond in time")

    # Перезапускаем opencode serve
    model_name = model.get("model", current_model)
    if opencode_process:
        await opencode_process.restart()

    # Очищаем сессию после переключения модели
    if session_mgr:
        session_mgr.remove(user_id)
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        logger.info(
            f"Cleared session for user {user_id} and deleted sessions file after model switch to {alias}"
        )

    # Сохраняем в config
    save_model_config(model_name, alias)

    return model_name, None
