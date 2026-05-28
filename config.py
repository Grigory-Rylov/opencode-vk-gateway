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
SUBAGENT_PREFIX = CONFIG.get("subagent_prefix", "[subagent] ")

if not VK_TOKEN:
    raise ValueError("VK_TOKEN is required in config file")

SCRIPT_DIR = Path(__file__).parent.resolve()
OPENCODE_BIN = Path(CONFIG["opencode_bin_path"])
ATTACHES_DIR = SCRIPT_DIR / "attaches"
OPENCODE_CONFIG_PATH = Path(CONFIG.get("opencode_config_path", "~/.config/opencode/opencode.json")).expanduser()


def getCwd() -> Path:
    """Возвращает текущую рабочую директорию процесса (не директорию скрипта)."""
    return Path.cwd()


def switch_config(config_name: str) -> bool:
    """Переключает конфиг бота на config.<name>.json.
    
    Загружает файл config.<config_name>.json из директории скрипта
    и обновляет все глобальные константы модуля config.
    
    Args:
        config_name: Имя конфига (config.<name>.json) или полный путь до файла .json
        
    Returns:
        True если конфиг успешно загружен, иначе False.
    """
    import importlib
    import sys
    
    # Определяем путь к конфигу
    config_path = SCRIPT_DIR / f"config.{config_name}.json"
    if not config_path.exists():
        config_path = Path(config_name)
        if not config_path.exists() or config_path.suffix != ".json":
            return False
    
    config_str = str(config_path)
    
    try:
        new_config = load_config(config_str)
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    
    # Обновляем глобальные переменные модуля
    current_module = sys.modules[__name__]
    current_module.CONFIG = new_config
    current_module.VK_TOKEN = new_config["vk_token"]
    current_module.OPENCODE_URL = new_config["opencode_url"]
    current_module.SESSION_FILE = Path(new_config["session_file"])
    current_module.VK_API_VERSION = new_config["vk_api_version"]
    current_module.LONGPOLL_WAIT = new_config["longpoll_wait"]
    current_module.PEER_ID = new_config.get("peer_id")
    current_module.THINKING_PEER_ID = new_config.get("thinking_peer_id")
    current_module.MODEL = new_config.get("model")
    current_module.MODELS = new_config.get("models", {})
    current_module.DEFAULT_MODEL = new_config.get("default_model")
    current_module.LLAMA_SERVER_PATH = new_config.get("llama_server_path", None)
    current_module.LLAMA_SERVER_HOST = new_config.get("llama_server_host", "http://localhost:8081")
    current_module.MCP_SERVERS = new_config.get("mcp_servers", {})
    current_module.SUBAGENT_PREFIX = new_config.get("subagent_prefix", "[subagent] ")
    current_module.OPENCODE_BIN = Path(new_config["opencode_bin_path"])
    current_module.OPENCODE_CONFIG_PATH = Path(new_config.get("opencode_config_path", "~/.config/opencode/opencode.json")).expanduser()
    
    # Обновляем args.config чтобы importlib.reload(config) тоже подхватил
    if hasattr(current_module.args, 'config'):
        current_module.args.config = config_str
    
    return True
