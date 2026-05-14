# OpenCode VK Gateway

VK-бот для работы с [OpenCode](https://opencode.ai) - AI-ассистентом для программирования.

## Возможности

- Взаимодействие с OpenCode через VK-мессенджер
- Управление несколькими моделями Llama.cpp с переключением по команде
- Поддержка сессий с историей и дедупликацией сообщений
- Отправка промежуточных рассуждений (reasoning) в отдельный чат
- Перезапуск моделей и opencode serve без перезапуска бота
- Обработка запросов разрешений (чтение файлов, доступ к директориям) через inline-кнопки
- Обработка вопросов от opencode с клавиатурой опций
- Отправка длинных ответов частями (лимит VK 4090 символов)
- Информация о GPU через nvidia-smi

## Требования

- Python 3.12+
- VK API токен
- [opencode](https://opencode.ai) бинарник
- [llama.cpp](https://github.com/ggerganov/llama.cpp) сервер
- tmux (для управления llama-server в сессии)
- nvidia-smi (опционально, для команды /gpu)

## Установка

1. Клонировать репозиторий:
```bash
git clone https://github.com/Grigory-Rylov/opencode-vk-gateway.git
cd opencode-vk-gateway
```

2. Создать виртуальное окружение:
```bash
python3 -m venv venv
source venv/bin/activate
```

3. Установить зависимости:
```bash
pip install -r requirements.txt
```

4. Скопировать и настроить конфиг:
```bash
cp config.json.example config.json
# Отредактировать config.json, указав свой VK токен и пути к моделям
```

## Настройка

В `config.json`:

| Параметр | Описание | По умолчанию |
|----------|----------|-------------|
| `vk_token` | VK API токен (получить [тут](https://vk.com/dev)) | — |
| `opencode_url` | URL opencode serve | `http://127.0.0.1:4096` |
| `session_file` | Файл хранения сессий | `sessions.json` |
| `vk_api_version` | Версия VK API | `5.200` |
| `longpoll_wait` | Время ожидания longpoll (сек) | `25` |
| `peer_id` | ID чата/пользователя для бота | — |
| `thinking_peer_id` | ID чата для отправки рассуждений | `2000000506` |
| `model` | Модель по умолчанию `провайдер/название` | — |
| `opencode_bin_path` | Путь к бинарнику opencode | — |
| `llama_server_path` | Путь к llama-server | `llama-server` |
| `llama_server_host` | URL llama-server | `http://localhost:8081` |
| `models` | Словарь моделей и параметров запуска | — |
| `default_model` | Алиас модели по умолчанию | — |
| `mcp_servers` | (опционально) MCP серверы для opencode | — |

### Формат модели

Модель указывается строкой формата `провайдер/название`:
```json
"model": "llama.cpp/qwen3.6-claude"
```

Для каждой модели в секции `models`:
```json
"models": {
  "qwen3.6-claude": {
    "model": "llama.cpp/qwen3.6-claude",
    "args": "-m /path/to/model.gguf --port 8081 ..."
  }
}
```

## Архитектура

Проект разделён на модули с разделением ответственности:

| Модуль | Назначение |
|--------|-----------|
| `main.py` | Точка входа, инициализация и запуск |
| `config.py` | Загрузка конфигурации, аргументы CLI |
| `logging_config.py` | Настройка логирования |
| `models.py` | Управление моделями и форматирование API |
| `llama_server.py` | Жизненный цикл llama-server |
| `opencode_process.py` | Управление процессом opencode serve |
| `session_manager.py` | Управление сессиями и дедупликация |
| `vk_client.py` | VK API клиент, разрешения, вопросы |
| `vk_longpoll.py` | VK longpoll, маршрутизация сообщений |
| `nvidia.py` | Парсинг GPU информации |
| `gateway-restarter.py` | Сервис перезапуска через `/update` |

## Принцип работы

### Подмена конфига OpenCode

OpenCode требует настройку провайдера для подключения к локальному llama-server. При старте бота и при переключении модели происходит автоматическое обновление конфига `~/.config/opencode/opencode.json`.

#### MCP серверы

В секцию `mcp_servers` config.json можно добавить MCP серверы которые будут доступны в opencode:

```json
{
  "mcp_servers": {
    "ya-disk-uploader": {
      "type": "local",
      "command": ["/path/to/ya-disk-uploader/ya-disk-uploader", "mcp"],
      "enabled": true
    }
  }
}
```

Если секция `mcp_servers` не указана или пуста, MCP серверы не будут добавлены.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "llama.cpp/название-модели",
  "provider": {
    "llama.cpp": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "llama-server (local)",
      "options": {
        "baseURL": "http://localhost:8081/v1"
      },
      "models": {
        "название-модели": {
          "name": "название-модели (local)",
          "limit": {
            "context": 131072,
            "output": 65536
          }
        }
      }
    }
  }
}
```

### Переключение моделей

Команда `/restart` или `/r` выполняет:
1. Остановка текущего llama-server
2. Запуск llama-server с новой моделью
3. Обновление конфига OpenCode (подмена provider с новой моделью)
4. Ожидание загрузки модели (до 5 минут, проверка пингом)
5. Перезапуск opencode serve для применения нового конфига
6. Очистка сессии пользователя после переключения

## Запуск

### Режим перезапуска (ожидает команду /update)

```bash
python gateway-restarter.py
```

Запускает бота, который слушает VK и при получении команды `/update` перезапускает основной шлюз.

### Прямой запуск

```bash
python main.py
```

### Аргументы командной строки

| Аргумент | Описание |
|----------|----------|
| `--config <путь>` | Путь к файлу конфигурации |
| `-d, --debug` | Включить debug логирование в файл |

## Команды бота

| Команда | Описание |
|---------|----------|
| `/help` | Показать справку со всеми командами |
| `/restart` | Перезапустить с текущей моделью |
| `/restart <модель>` | Перезапустить с указанной моделью |
| `/r <модель>` | То же что `/restart <модель>` |
| `/models` или `/m` | Показать доступные модели |
| `/history` | Получить историю сессии файлом |
| `/history <session_id>` | Получить историю конкретной сессии |
| `/newsession` или `/n` | Создать новую сессию |
| `/newsession <путь>` | Создать новую сессию с указанным рабочим каталогом |
| `/sessions` | Показать список всех сессий |
| `/clearsessions` | Удалить все сессии |
| `/gpu` | Показать информацию о GPU (nvidia-smi) |
| `/logs` | Отправить файл логов |

## Systemd сервис (Linux)

Пример `~/.config/systemd/user/gateway.service`:

```ini
[Unit]
Description=VK Gateway Autostart
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/opencode-vk-gateway
ExecStart=/path/to/opencode-vk-gateway/venv/bin/python /path/to/opencode-vk-gateway/gateway-restarter.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Запуск:
```bash
systemctl --user enable gateway.service
systemctl --user start gateway.service
```

## Структура проекта

| Файл | Назначение |
|------|-----------|
| `main.py` | Точка входа, инициализация и запуск |
| `gateway-restarter.py` | Сервис перезапуска (слушает `/update`) |
| `config.py` | Загрузка конфигурации и аргументы CLI |
| `config.json.example` | Шаблон конфигурации |
| `models.py` | Управление моделями |
| `llama_server.py` | Управление llama-server |
| `opencode_process.py` | Управление процессом opencode |
| `session_manager.py` | Управление сессиями |
| `vk_client.py` | VK API клиент |
| `vk_longpoll.py` | VK longpoll слушатель |
| `opencode_client.py` | Клиент API opencode |
| `nvidia.py` | Парсер nvidia-smi |
| `logging_config.py` | Настройка логирования |
| `requirements.txt` | Зависимости Python |