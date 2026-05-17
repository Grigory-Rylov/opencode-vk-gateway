#!/usr/bin/env python3
"""
OpenCode VK Gateway Bot (Long Poll версия)
Использует текстовые inline-кнопки для вопросов.
Поддерживает отправку промежуточных рассуждений в отдельный чат (thinking_peer_id).
Конфигурация загружается из JSON-файла.
"""

import asyncio
import subprocess
from pathlib import Path

from config import (
    SESSION_FILE,
    LLAMA_SERVER_PATH,
    PEER_ID,
    MODEL,
    SCRIPT_DIR,
    args,
    VK_TOKEN,
    VK_API_VERSION,
)
from logging_config import setup_logging, logger
from opencode_process import OpenCodeProcess
from session_manager import SessionManager
from vk_longpoll import VKLongPoll
from vk_client import VKClient
from llama_server import restart_llama_server
from models import get_current_model, DEFAULT_MODEL
import vk_keyboards


async def main():
    # Настройка логирования
    setup_logging(args.debug)
    logger.debug("DEBUG logging enabled - this is a test debug message")

    session_mgr = SessionManager(SESSION_FILE)
    logger.info(f"main() starting: SCRIPT_DIR={SCRIPT_DIR}, cwd={Path.cwd()}")

    # Запуск llama сервера (опционально)
    if LLAMA_SERVER_PATH:
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", "llama"], capture_output=True
            )
            if result.returncode != 0:
                logger.info("llama tmux session not found, starting with default model")
                current_model = get_current_model()
                if current_model:
                    await restart_llama_server(current_model, DEFAULT_MODEL, LLAMA_SERVER_PATH)
                else:
                    logger.warning("No models configured, cannot start llama server")
        except Exception as e:
            logger.warning(f"Failed to check llama session: {e}")
    else:
        logger.info("LLAMA_SERVER_PATH not set, skipping llama server check")

    opencode_process = OpenCodeProcess(model=MODEL, workdir=SCRIPT_DIR)
    logger.info(f"OpenCodeProcess created with workdir={opencode_process.workdir}")
    await opencode_process.start()

    async with VKClient(
        token=VK_TOKEN, api_version=VK_API_VERSION
    ) as vk:
        try:
            await vk.send_message(
                PEER_ID,
                f"🤖 OpenCode VK Gateway запущен\n\nModel: {MODEL}\nWorkdir: {SCRIPT_DIR}",
                keyboard=vk_keyboards.get_main_keyboard(),
            )
        except Exception as e:
            logger.warning(f"Failed to send startup message: {e}")

        poller = VKLongPoll(vk, session_mgr, opencode_process)
        
        # Восстанавливаем workdir из сохранённых сессий (если есть)
        await poller.initialize()
        
        try:
            await poller.run()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            await poller.stop()
        finally:
            await opencode_process.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Graceful shutdown
