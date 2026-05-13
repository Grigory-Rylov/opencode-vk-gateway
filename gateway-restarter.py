#!/usr/bin/env python3
"""
VK Gateway Reloader
Слушает команду /update и перезапускает opencode-vk-gateway.py
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientSession, ClientTimeout

# Парсинг аргументов
parser = argparse.ArgumentParser(description="VK Gateway Reloader")
parser.add_argument("--autostart", action="store_true", help="Auto-start opencode-vk-gateway on launch")

if __name__ == "__main__":
    args = parser.parse_args()
else:
    args = parser.parse_args([])

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("vk-reloader")

# Путь к основному скрипту
SCRIPT_DIR = Path(__file__).parent.resolve()
MAIN_SCRIPT = SCRIPT_DIR / "main.py"
PID_FILE = SCRIPT_DIR / ".gateway.pid"

LLAMA_SERVER_PORT = 8081

# Пути к версиям скрипта для запуска (приоритет: v1 -> v0 -> корень)
SCRIPT_VERSIONS = [
    SCRIPT_DIR / "v1.py",
    SCRIPT_DIR / "v0.py",
    MAIN_SCRIPT,
]

# Загрузка токена из config.json
def load_config() -> tuple[str, int]:
    config_path = SCRIPT_DIR / "config.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        token = config.get("vk_token", "")
        if not token:
            raise ValueError("vk_token is empty in config.json")
        notify_peer_id = config.get("peer_id", 2000000000)
        return token, notify_peer_id
    except FileNotFoundError:
        raise FileNotFoundError(f"Config file not found: {config_path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config: {e}")


class VKClient:
    BASE_URL = "https://api.vk.com/method/"

    def __init__(self, token: str, api_version: str = "5.200"):
        self.token = token
        self.api_version = api_version
        self.session: aiohttp.ClientSession = None

    async def __aenter__(self):
        self.session = ClientSession(timeout=ClientTimeout(total=30))
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _api_request(self, method: str, params: dict) -> dict:
        params["access_token"] = self.token
        params["v"] = self.api_version
        url = f"{self.BASE_URL}{method}?{urlencode(params)}"
        async with self.session.get(url) as resp:
            data = await resp.json()
            if "error" in data:
                raise Exception(f"VK API error: {data['error']}")
            return data["response"]

    async def get_long_poll_server(self) -> tuple[str, str, int]:
        resp = await self._api_request("messages.getLongPollServer", {})
        return resp["server"], resp["key"], int(resp["ts"])

    async def send_message(self, peer_id: int, text: str) -> int:
        params = {
            "peer_id": peer_id,
            "random_id": int(time.time() * 1000),
            "message": text,
        }
        resp = await self._api_request("messages.send", params)
        return resp[0]["message_id"] if isinstance(resp, list) else resp


def get_gateway_pid() -> int | None:
    """Читает PID основного процесса из файла."""
    if PID_FILE.exists():
        try:
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
        except (ValueError, FileNotFoundError):
            pass
    return None


def save_gateway_pid(pid: int):
    """Сохраняет PID основного процесса в файл."""
    with open(PID_FILE, "w") as f:
        f.write(str(pid))


def remove_pid_file():
    """Удаляет файл с PID."""
    if PID_FILE.exists():
        PID_FILE.unlink()


def is_process_running(pid: int) -> bool:
    """Проверяет, запущен ли процесс с данным PID."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_llama_server_running() -> bool:
    """Проверяет, запущен ли llama-server на порту LLAMA_SERVER_PORT."""
    try:
        result = subprocess.run(
            ["lsof", "-i", f":{LLAMA_SERVER_PORT}"],
            capture_output=True
        )
        return result.returncode == 0
    except Exception as e:
        logger.warning(f"Failed to check llama-server status: {e}")
        return False


class LlamaServerMonitor:
    """Мониторит состояние llama-server и уведомляет при падении."""

    def __init__(self, vk: 'VKClient', check_interval: int = 30):
        self.vk = vk
        self.check_interval = check_interval
        self._was_running = False
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        self._running = True
        logger.info(f"LlamaServerMonitor started (interval={self.check_interval}s)")

    async def check_loop(self, notify_peer_id: int):
        """Основной цикл проверки llama-server."""
        self.start()

        while self._running:
            await asyncio.sleep(self.check_interval)

            if not self._running:
                break

            is_running = is_llama_server_running()

            if self._was_running and not is_running:
                logger.warning("Llama-server is down!")
                try:
                    await self.vk.send_message(
                        notify_peer_id,
                        "⚠️ Llama-server упал!"
                    )
                except Exception as e:
                    logger.error(f"Failed to send llama-server down notification: {e}")

            self._was_running = is_running
            logger.debug(f"Llama-server check: running={is_running}")

    def stop(self):
        self._running = False
        logger.info("LlamaServerMonitor stopped")


def restart_gateway(version: str = None):
    """Перезапускает основной скрипт opencode-vk-gateway.
    
    Args:
        version: Версия для запуска - "default", "v0", "v1". Если None - автовыбор по приоритету.
    
    Returns:
        tuple: (success: bool, started_from: str | None)
    """
    logger.info("=== Restarting opencode-vk-gateway.py ===")
    logger.info(f"restart_gateway: SCRIPT_DIR={SCRIPT_DIR}, cwd={Path.cwd()}, version={version}")

    # Проверяем PID файл
    old_pid = get_gateway_pid()
    if old_pid and is_process_running(old_pid):
        logger.info(f"Stopping existing process (PID: {old_pid})")
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(2)
            if is_process_running(old_pid):
                logger.warning(f"Process {old_pid} still running, forcing kill")
                os.kill(old_pid, signal.SIGKILL)
        except OSError as e:
            logger.warning(f"Failed to stop process {old_pid}: {e}")
    remove_pid_file()

    # Удаляем старый debug.log
    debug_log = SCRIPT_DIR / "debug.log"
    if debug_log.exists():
        try:
            debug_log.unlink()
            logger.info("Removed old debug.log")
        except Exception as e:
            logger.warning(f"Failed to remove debug.log: {e}")

    # Определяем какие версии пробовать
    if version:
        version = version.lower()
        if version == "default":
            scripts_to_try = [MAIN_SCRIPT]
        elif version == "v0":
            # Для /update v0 запускаем v0.py из текущего каталога
            scripts_to_try = [SCRIPT_DIR / "v0.py"]
        elif version == "v1":
            # Для /update v1 или /start v1: v1.py -> v0.py -> opencode-vk-gateway.py
            scripts_to_try = [SCRIPT_DIR / "v1.py", SCRIPT_DIR / "v0.py", MAIN_SCRIPT]
        else:
            logger.warning(f"Unknown version '{version}', using auto-select")
            scripts_to_try = SCRIPT_VERSIONS
    else:
        # Автовыбор: v1.py -> v0.py -> opencode-vk-gateway.py
        scripts_to_try = SCRIPT_VERSIONS
    
    logger.info(f"Scripts to try: {[str(p.relative_to(SCRIPT_DIR)) for p in scripts_to_try]}")

    # Запускаем новый процесс
    venv_python = SCRIPT_DIR / "venv/bin/python"
    log_file = SCRIPT_DIR / "debug.log"
    stdout_file = open(log_file, "w")
    
    for script_path in scripts_to_try:
        logger.info(f"Trying to start from: {script_path}")
        if not script_path.exists():
            logger.info(f"Script not found: {script_path}, skipping")
            continue
        
        try:
            proc = subprocess.Popen(
                [str(venv_python), str(script_path), "-d"],
                stdout=stdout_file,
                stderr=subprocess.STDOUT,
                cwd=str(SCRIPT_DIR),
            )
            save_gateway_pid(proc.pid)
            started_from = str(script_path.relative_to(SCRIPT_DIR))
            logger.info(f"Successfully started main.py from {started_from} (PID: {proc.pid})")

            time.sleep(3)

            if not is_process_running(proc.pid):
                logger.error(f"Process from {script_path} exited immediately!")
                continue
            
            stdout_file.close()
            return True, started_from
        except Exception as e:
            logger.error(f"Failed to start from {script_path}: {e}")
    
    stdout_file.close()
    logger.error("All script versions failed to start!")
    return False, None


class VKLongPollReloader:
    def __init__(self, vk: VKClient):
        self.vk = vk
        self.server = None
        self.key = None
        self.ts = None
        self.running = False

    async def _refresh_long_poll_server(self):
        self.server, self.key, self.ts = await self.vk.get_long_poll_server()
        logger.info(f"Long Poll server refreshed: {self.server}")

    async def _get_long_poll_events(self) -> tuple[list, int]:
        params = {
            "act": "a_check",
            "key": self.key,
            "ts": self.ts,
            "wait": "25",
            "mode": "74",
            "version": "3",
        }
        url = f"https://{self.server}?{urlencode(params)}"
        timeout = ClientTimeout(total=35)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if "failed" in data:
                    raise Exception(f"Long poll failed: {data}")
                return data.get("updates", []), int(data["ts"])

    async def _handle_message_new(self, event: list):
        msg_id = int(event[1])
        flags = int(event[2])
        peer_id = int(event[3])
        text = event[5] if len(event) > 5 else ""

        # Игнорируем сообщения от себя (флаг 2)
        if flags & 2:
            return

        # Игнорируем сообщения без текста
        if not text.strip():
            return

        logger.info(f"New message from {peer_id}: '{text}'")

        # Проверяем команду /update /start
        parts = text.strip().split()
        command = parts[0].lower() if parts else ""
        
        if command in ("/update", "/start"):
            version = parts[1] if len(parts) > 1 else None
            logger.info(f"Received {command} command, version={version}")
            try:
                version_text = f" (версия: {version})" if version else " (автовыбор)"
                await self.vk.send_message(peer_id, f"🔄 Перезагрузка opencode-vk-gateway {version_text}...")
                success, started_from = restart_gateway(version)
                if success:
                    from_text = f"\n📁 Запущен из: `{started_from}`"
                    await self.vk.send_message(peer_id, f"✅ opencode-vk-gateway перезапущен{version_text}{from_text}")
                else:
                    await self.vk.send_message(peer_id, f"❌ Не удалось перезапустить opencode-vk-gateway {version_text}")
            except Exception as e:
                logger.error(f"Error handling {command}: {e}")
                try:
                    await self.vk.send_message(peer_id, f"❌ Ошибка: {e}")
                except:
                    pass
        elif command == "/restart-help":
            help_text = """
🔄 Команды перезапуска:

/start - Автовыбор (v1.py → v0.py → opencode-vk-gateway)
/start default - Запуск opencode-vk-gateway.py
/start v0 - Запуск v0.py из текущего каталога
/start v1 - Запуск v1.py (если нет → v0.py → opencode-vk-gateway)

/update - То же что /start
/update default|v0|v1 - Запуск конкретной версии
"""
            try:
                await self.vk.send_message(peer_id, help_text)
            except:
                pass
        elif command in ("/b", "/branch"):
            await self._handle_branch_command(peer_id, parts)
        else:
            logger.debug(f"Ignoring message (not /update): '{text}'")

    async def _handle_branch_command(self, peer_id: int, parts: list):
        """Handle /b or /branch command to checkout a git branch."""
        if len(parts) < 2:
            help_text = """
🔀 Команды переключения веток:

/b <branch> - Переключиться на ветку
/b <branch> -f - Форсированный чекаут (сбросить изменения)

Если ветка не найдена локально, будет попытка создать из remote.
"""
            try:
                await self.vk.send_message(peer_id, help_text)
            except:
                pass
            return

        branch_name = parts[1]
        force = len(parts) > 2 and parts[2].lower() == "-f"
        
        logger.info(f"Received /branch command: branch={branch_name}, force={force}")
        
        def _run_git_command(*args, check=False) -> tuple:
            """Run a git command and return (success, output)."""
            try:
                result = subprocess.run(
                    ["git"] + list(args),
                    cwd=str(SCRIPT_DIR),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                return result.returncode == 0, result.stdout.strip() or result.stderr.strip()
            except subprocess.TimeoutExpired:
                return False, "Git command timed out"
            except Exception as e:
                return False, str(e)

        # Если форс - сбросить все изменения
        if force:
            await self.vk.send_message(peer_id, f"🔄 Сброс изменений и форс-чекаут на `{branch_name}`...")
            success, output = _run_git_command("reset", "--hard", "HEAD")
            if not success:
                await self.vk.send_message(peer_id, f"❌ Не удалось сбросить изменения: {output}")
                return
            success, output = _run_git_command("clean", "-fd")
            if not success:
                logger.warning(f"clean -fd failed (non-critical): {output}")
        
        # Сначала попробуем локальный чекаут
        success, output = _run_git_command("checkout", branch_name)
        if success:
            await self.vk.send_message(peer_id, f"✅ Переключено на ветку `{branch_name}`")
            return
        
        # Если ветка не найдена локально - попробуем создать из remote
        logger.info(f"Branch {branch_name} not found locally, checking remote...")
        
        # Сначала обновим remote
        success, output = _run_git_command("fetch", "--all")
        if not success:
            logger.warning(f"fetch failed: {output}")
        
        # Попробуем создать ветку из remote
        success, output = _run_git_command("checkout", "-b", branch_name, f"origin/{branch_name}")
        if success:
            await self.vk.send_message(peer_id, f"✅ Создана новая ветка `{branch_name}` из remote")
            return
        
        # Если remote тоже нет - попробуем создать пустую
        logger.info(f"Remote branch {branch_name} not found either")
        success, output = _run_git_command("checkout", "-b", branch_name)
        if success:
            await self.vk.send_message(peer_id, f"✅ Создана новая локальная ветка `{branch_name}` (remote не найден)")
            return
        
        # Все попытки исчерпаны
        error_msg = f"❌ Не удалось переключиться на ветку `{branch_name}`\n"
        if force:
            error_msg += "Была попытка сбросить изменения.\n"
        error_msg += f"Ошибки:\n"
        error_msg += f"  - checkout: {output}\n"
        error_msg += "Возможно, ветка не существует ни локально, ни на remote."
        
        await self.vk.send_message(peer_id, error_msg)

    async def run(self):
        self.running = True
        await self._refresh_long_poll_server()

        logger.info("VK Reloader started. Waiting for /update command...")

        while self.running:
            try:
                updates, new_ts = await self._get_long_poll_events()
                self.ts = new_ts

                for update in updates:
                    if not isinstance(update, list):
                        continue
                    event_type = update[0]
                    if event_type == 4:  # message_new
                        asyncio.create_task(self._handle_message_new(update))

            except asyncio.CancelledError:
                break
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"Long poll timeout or client error: {e}. Reconnecting...")
                await asyncio.sleep(3)
                await self._refresh_long_poll_server()
            except Exception as e:
                logger.exception(f"Long poll error: {e}")
                await asyncio.sleep(3)
                await self._refresh_long_poll_server()

    async def stop(self):
        self.running = False
        logger.info("VK Reloader stopped")


async def main():
    logger.info("=== VK Gateway Reloader ===")
    logger.info(f"Script directory: {SCRIPT_DIR}")
    logger.info(f"Main script: {MAIN_SCRIPT}")
    logger.info(f"Current working directory: {Path.cwd()}")
    logger.info(f"Script versions to try: {[str(p.relative_to(SCRIPT_DIR)) for p in SCRIPT_VERSIONS]}")

    # Проверяем что хотя бы одна версия скрипта существует
    available = [p for p in SCRIPT_VERSIONS if p.exists()]
    if not available:
        logger.error("No opencode_vk_gateway.py found in any version directory!")
        return
    logger.info(f"Available scripts: {[str(p.relative_to(SCRIPT_DIR)) for p in available]}")

    try:
        token, notify_peer_id = load_config()
        logger.info(f"VK token loaded (len={len(token)})")
        logger.info(f"Notify peer_id: {notify_peer_id}")
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return

    # Автозапуск основного скрипта при старте (если передан флаг --autostart)
    if args.autostart:
        logger.info("Starting opencode-vk-gateway on reloader startup (--autostart)...")
        try:
            success = restart_gateway()
            if success:
                logger.info("main.py started successfully")
            else:
                logger.warning("Failed to start main.py on startup")
        except Exception as e:
            logger.error(f"Error starting main.py: {e}")
    else:
        logger.info("VK Reloader started. Use /update command to start gateway.")

    async with VKClient(token) as vk:
        try:
            await vk.send_message(notify_peer_id, "✅ Gateway Restarter запущен")
            logger.info(f"Sent startup notification to peer_id: {notify_peer_id}")
        except Exception as e:
            logger.warning(f"Failed to send startup notification: {e}")

        monitor = LlamaServerMonitor(vk, check_interval=30)
        monitor_task = asyncio.create_task(monitor.check_loop(notify_peer_id))

        poller = VKLongPollReloader(vk)
        try:
            await poller.run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
            monitor.stop()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            await poller.stop()


if __name__ == "__main__":
    asyncio.run(main())
