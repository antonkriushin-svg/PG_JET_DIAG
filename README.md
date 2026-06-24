# PG Jet Diag

Локальный инструмент диагностики PostgreSQL с GUI, режимами DB/LLM и сбором метрик.

## Настройка `.env`

1. Создайте файл `.env` в корне проекта (рядом с `gui_db_chat.py`).
2. Скопируйте в него содержимое из `.env.example`.
3. Заполните значения своими ключами и параметрами.

Пример:

```env
RPG_PIPELINE_DEBUG=false
RPG_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_MODEL=deepseek-chat
# DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
```

## Важно по безопасности

- Файл `.env` содержит секреты и **не должен** попадать в git.
- Для этого `.env` добавлен в `.gitignore`.
- Коммитить стоит только `.env.example` (без реальных ключей).
- Файл `db_config.json` также локальный (может содержать пароль БД) и исключен из git.
- Для примера конфигурации используйте `db_config.example.json`.
