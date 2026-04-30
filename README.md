# OpenCode VK Gateway

VK-бот для работы с [OpenCode](https://opencode.ai) - AI-ассистентом для программирования.

## Возможности

- Взаимодействие с OpenCode через VK-мессенджер
- Управление несколькими моделями Llama.cpp
- Поддержка сессий с историей
- Отправка промежуточных рассуждений (reasoning) в отдельный чат
- Перезапуск моделей и opencode serve без перезапуска бота

## Требования

- Python 3.12+
- VK API токен
- [opencode](https://opencode.ai) бинарник
- [llama.cpp](https://github.com/ggerganov/llama.cpp) сервер

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

- `vk_token` - VK API токен (получить [тут](https://vk.com/dev))
- `opencode_url` - URL opencode serve (по умолчанию localhost:4096)
- `llama_server_path` - путь к llama-server
- `models` - доступные модели и их параметры запуска
- `default_model` - модель по умолчанию
- `thinking_peer_id` - ID чата для отправки промежуточных рассуждений
- `mcp_servers` - (опционально) MCP серверы для opencode

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

Команда `/restart` выполняет:
1. Остановка текущего llama-server
2. Запуск llama-server с новой моделью
3. Обновление конфига OpenCode (подмена provider с новой моделью)
4. Ожидание загрузки модели (до 5 минут)
5. Перезапуск opencode serve для применения нового конфига

## Запуск

### Режим перезапуска (ожидает команду /update)

```bash
python gateway-restarter.py
```

Запускает бота, который слушает VK и при получении команды `/update` перезапускает основной шлюз.

### Прямой запуск

```bash
python opencode-vk-gateway.py
```

## Команды бота

- `/update` - перезапустить opencode-vk-gateway.py (через рестартер)
- `/restart` - перезапустить llama-server с указанной моделью
- `/models` - показать доступные модели
- `/history` - получить историю сессии файлом
- `/sessions` - показать список всех сессий
- `/newsession` - создать новую сессию
- `/logs` - отправить файл логов
- `/help` - показать справку

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

- `opencode-vk-gateway.py` - основной бот
- `gateway-restarter.py` - слушатель для перезапуска
- `config.json.example` - пример конфига
- `requirements.txt` - зависимости