"""
Конфигурация - загрузка, дефолтные значения, аргументы командной строки
"""
import argparse
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
    "llama_server_host": "http://localhost:8081",  # URL удалённого llama-server
    "opencode_config_path": "~/.config/opencode/opencode.json",
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


# ---------- Разбор аргументов командной строки ----------
parser = argparse.ArgumentParser(description="OpenCode VK Gateway Bot")
parser.add_argument(
    "--config", type=str, default="config.json", help="Path to JSON config file"
)
parser.add_argument(
    "-d", "--debug", action="store_true", help="Enable debug logging to file"
)

args = parser.parse_args()

# ---------- Загрузка конфигурации ----------
CONFIG = load_config(args.config)

# ---------- Глобальные константы из конфигурации ----------
VK_TOKEN = CONFIG["vk_token"]
OPENCODE_URL = CONFIG["opencode_url"]
SESSION_FILE = Path(CONFIG["session_file"])
VK_API_VERSION = CONFIG["vk_api_version"]
LONGPOLL_WAIT = CONFIG["longpoll_wait"]
PEER_ID = CONFIG.get("peer_id")
THINKING_PEER_ID = CONFIG.get("thinking_peer_id")
MODEL = CONFIG.get("model")
MODELS = CONFIG.get("models", {})
DEFAULT_MODEL = CONFIG.get("default_model")
LLAMA_SERVER_PATH = CONFIG.get("llama_server_path", None)
LLAMA_SERVER_HOST = CONFIG.get("llama_server_host", "http://localhost:8081")
MCP_SERVERS = CONFIG.get("mcp_servers", {})

if not VK_TOKEN:
    raise ValueError("VK_TOKEN is required in config file")

SCRIPT_DIR = Path(__file__).parent.resolve()
OPENCODE_BIN = Path(CONFIG["opencode_bin_path"])
ATTACHES_DIR = SCRIPT_DIR / "attaches"
OPENCODE_CONFIG_PATH = Path(CONFIG.get("opencode_config_path", "~/.config/opencode/opencode.json")).expanduser()


def getCwd() -> Path:
    """Возвращает текущую рабочую директорию процесса (не директорию скрипта)."""
    return Path.cwd()
