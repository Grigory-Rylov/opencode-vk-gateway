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
    LLAMA_SERVER_HOST,
    SESSION_FILE,
    load_config,
)
from logging_config import logger
from models import get_model_by_alias, get_current_model
from config import MODELS


# Константы
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
        subprocess.run(["pkill", "-9", "-f", llama_path], capture_output=True)
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
    # Используем URL из конфига, добавляем / если нет
    url = LLAMA_SERVER_HOST.rstrip("/") + "/"
    
    waited = 0

    while waited < timeout:
        await asyncio.sleep(interval)
        waited += interval
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=2) as resp:
                    if resp.status == 200:
                        return True
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass

    return False


def update_opencode_config(model: dict, alias: str) -> bool:
    """Обновляет конфиг OpenCode с провайдером и всеми доступными моделями."""
    try:
        opencode_config_path = OPENCODE_CONFIG_PATH

        # Загружаем все модели из проекта, чтобы opencode знал о них всех
        project_config = load_config()
        all_models = project_config.get("models", {})
        # Если models не найден в файле (или это список вместо словаря),
        # используем глобальный MODELS из config.py как фоллбэк
        if not isinstance(all_models, dict) or not all_models:
            all_models = MODELS

        # Строим словарь моделей для opencode - ВСЕ модели, не только текущая
        opencode_models = {}
        for model_alias, model_info in all_models.items():
            opencode_models[model_alias] = {
                "name": f"{model_alias} (local)",
                "limit": {"context": 131072, "output": 65536},
            }

        opencode_config = {
            "$schema": "https://opencode.ai/config.json",
            "model": model.get("model", ""),
            "provider": {
                "llama.cpp": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "llama-server (local)",
                    "options": {"baseURL": "http://localhost:8081/v1"},
                    "models": opencode_models,
                }
            },
        }
        if MCP_SERVERS:
            opencode_config["mcp"] = MCP_SERVERS
            logger.info(f"Added {len(MCP_SERVERS)} MCP server(s) to opencode config")
        logger.info(f"Writing {len(opencode_models)} models to opencode config")
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


async def test_llama_server_speed(complete_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Тестирует скорость инференса llama-server, отправляя короткий запрос.
    
    Args:
        complete_url: Полный URL llama-server (например, http://192.168.1.212:8081)
    
    Returns:
        Tuple[speed_string, error_message] - один из элементов будет None
    """
    if not complete_url:
        return None, "❌ Не указан URL llama-server"
    
    # Убедимся, что URL не заканчивается на слеш
    if complete_url.endswith("/"):
        complete_url = complete_url.rstrip("/")
    
    test_url = f"{complete_url}/completion"
    logger.info(f"Testing llama-server speed at {test_url}")
    
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "prompt": "Test",
                "n_predict": 10,
                "stream": False,
                "temperature": 0.7
            }
            
            async with session.post(
                test_url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    return None, f"❌ Ошибка HTTP {resp.status}"
                
                data = await resp.json()
                
                if "timings" not in data:
                    return None, "❌ В ответе нет информации о timing"
                
                timings = data["timings"]
                predicted_ms = timings.get("predicted_ms", 0)
                predicted_n = timings.get("predicted_n", 0)
                model_name = data.get("model", "unknown")
                
                if predicted_n > 0:
                    # Скорость в токенах в секунду
                    tps = predicted_n / (predicted_ms / 1000)
                    speed_string = (
                        f"⚡ **Llama-server Speed Test**\n\n"
                        f"📊 Модель: `{model_name}`\n"
                        f"⏱️  Время генерации: {predicted_ms:.0f}ms\n"
                        f"🔢 Токенов: {predicted_n}\n"
                        f"🚀 Скорость: **{tps:.1f} tok/s**\n"
                        f"⏳ На токен: {predicted_ms/predicted_n:.1f}ms"
                    )
                    return speed_string, None
                else:
                    return None, "❌ Не удалось получить количество токенов"
                    
    except asyncio.TimeoutError:
        return None, "❌ Тайм-аут подключения"
    except aiohttp.ClientError as e:
        return None, f"❌ Ошибка подключения: {str(e)}"
    except Exception as e:
        logger.error(f"Error testing llama-server speed: {e}")
        return None, f"❌ Ошибка: {str(e)}"


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

    # Ждем пока модель загрузится (важно: ждём ДО обновления конфига opencode)
    ready = await wait_for_llama_server()

    if ready:
        await vk_client.send_message(user_id, f"✅ Модель {alias} загружена и готова!")
        logger.info(f"Model {alias} loaded successfully")
    else:
        await vk_client.send_message(
            user_id, f"⚠️ Модель {alias} не ответила за {LLAMA_STARTUP_TIMEOUT} сек, продолжаю..."
        )
        logger.warning(f"Model {alias} did not respond in time")

    # Обновляем конфиг opencode с провайдером и MCP (после того как llama-server готов)
    # Это гарантирует что opencode увидит правильный конфиг при запуске
    update_opencode_config(model, alias)

    # Перезапускаем opencode serve (он прочитает новый конфиг)
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
