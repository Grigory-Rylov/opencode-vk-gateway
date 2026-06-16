"""
Управление процессом OpenCode
"""
import asyncio
import subprocess
from pathlib import Path

import aiohttp

from config import OPENCODE_BIN
from logging_config import logger


class OpenCodeProcess:
    """Управление процессом opencode serve."""

    def __init__(self, model: str = None, workdir: Path = None):
        self.logger = logger
        self.process = None
        self.opencode_port = 4097
        self.model = model
        self.workdir = workdir or Path.cwd()
        self.logger.debug(
            f"OpenCodeProcess initialized: workdir={self.workdir}, cwd={Path.cwd()}"
        )

    async def start(self):
        workdir_str = str(self.workdir)
        self.logger.info(f"Starting opencode serve in {workdir_str}")

        # убиваем старые процессы и ждём освобождения порта
        subprocess.run(
            ["pkill", "-9", "-f", f"{OPENCODE_BIN} serve"], stderr=subprocess.DEVNULL
        )
        for _ in range(10):
            result = subprocess.run(
                ["lsof", "-i", f":{self.opencode_port}"], capture_output=True
            )
            if result.returncode != 0:
                break
            await asyncio.sleep(0.5)

        # Логирование в файл для отладки
        log_file_path = f"/tmp/opencode_{self.opencode_port}.log"
        log_file = None

        try:
            log_file = open(log_file_path, "w")

            self.process = subprocess.Popen(
                [OPENCODE_BIN, "serve", "--port", str(self.opencode_port)],
                stdout=log_file,
                stderr=log_file,
                cwd=workdir_str,
                start_new_session=True,
            )
            self.logger.info(
                f"Started with PID {self.process.pid}, log file: {log_file_path}"
            )

            # ждём, пока процесс не упадёт или порт не начнёт отвечать
            for _ in range(30):  # 15 секунд
                if self.process.poll() is not None:
                    stdout, stderr = self.process.communicate()
                    stderr_str = stderr.decode() if stderr else "no stderr available"
                    raise RuntimeError(f"opencode exited early: {stderr_str}")
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(f"http://127.0.0.1:{self.opencode_port}/", timeout=1) as resp:
                            if resp.status == 200:
                                self.logger.info("OpenCode server is ready")
                                return
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(0.5)
            raise TimeoutError("OpenCode server did not become ready in time")
        finally:
            if log_file:
                log_file.close()

    async def restart(self, workdir: Path = None):
        if workdir:
            self.logger.info(
                f"restart: updating workdir from {self.workdir} to {workdir}"
            )
            self.workdir = workdir
        self.logger.info(
            f"restart: restarting opencode serve with workdir={self.workdir}, cwd={Path.cwd()}"
        )
        await self.stop()
        await asyncio.sleep(1)
        await self.start()
        self.logger.info(f"opencode serve restarted with workdir={self.workdir}")

    async def stop(self):
        if self.process:
            self.logger.info(f"Stopping opencode serve, pid={self.process.pid}")
            pid = self.process.pid
            self.process.terminate()
            for _ in range(50):  # до 5 секунд с проверками
                if self.process.poll() is not None:
                    break
                await asyncio.sleep(0.1)
            if self.process.poll() is None:
                self.logger.warning("opencode didn't stop gracefully, killing")
                self.process.kill()
                self.process.wait()
            self.logger.info(f"opencode serve stopped (pid={pid})")
            self.process = None
