# Agent Orchestrator

Локальный Python-оркестратор, координирующий **Codex CLI** и **Antigravity CLI** через **Agent-Bridge**.

---

## 1. Назначение и ключевые принципы

1. **Экономия токенов координатора**: Codex передаёт задачу Antigravity и не тратит токены на постоянный LLM-поллинг.
2. **Ожидание без LLM**: Оркестратор использует подтверждённый инструмент `wait_task` Agent-Bridge (блокирующее MCP-ожидание с минимальным оверхедом).
3. **Автоматическая верификация Codex**: После завершения шага Antigravity оркестратор автоматически запускает отдельный процесс `codex exec` для проверки кода, анализа `git diff` и выполнения тестов.
4. **Контролируемый цикл исправлений**: Если верификатор возвращает исправимые замечания (`RETRY_NEEDED`), Antigravity получает структурированное задание на доработку.
5. **Единый дедлайн задачи и неограниченные повторы**: Повторные итерации (`RETRY_NEEDED`) выполняются без ограничения по числу попыток в пределах единого общего тайм-аута задачи (`total_task_timeout_sec`, по умолчанию 2700 сек. / 45 минут), сохраняющегося между перезапусками.
6. **Маршрутизация моделей Antigravity**: Динамический выбор модели на основе проверенного списка `agy models` и матрицы сложности (от `trivial` до `expert`) с механизмом fallback.
7. **Отказоустойчивость и SQLite**: Состояние каждой задачи, артефакты, вердикты и история событий сохраняются в SQLite (`WAL`-режим) и восстанавливаются после перезапуска процесса.
8. **Ограничение параллелизма**: Защита от гонок и дублирования (максимум 1 активная задача в первой версии).

---

## 2. Структура проекта

```
agent-orchestrator/
├── pyproject.toml              # Конфигурация пакета и метаданные
├── README.md                   # Документация и руководство пользователя
├── config.example.toml         # Пример конфигурации
├── config.toml                 # Рабочая конфигурация
├── orchestrator.sqlite         # База данных SQLite (создается при запуске)
├── src/
│   └── agent_orchestrator/
│       ├── __init__.py         # Версия пакета (0.1.0)
│       ├── models.py           # Модели данных: Task, TaskStatus, VerificationResult
│       ├── config.py           # Загрузка и валидация конфигурации TOML
│       ├── repository.py       # Репозиторий SQLite (задачи, события, блокировки)
│       ├── bridge_client.py    # Клиент Agent-Bridge (stdio MCP JSON-RPC 2.0)
│       ├── waiter.py           # Ожидание завершения без LLM (NonLLMWaiter)
│       ├── codex_verifier.py   # Верификатор Codex (codex exec, git diff, тесты)
│       ├── model_router.py     # Маршрутизатор моделей Antigravity (agy models)
│       ├── retry_controller.py # Контроллер повторных шагов и генерация отчётов
│       ├── orchestrator.py     # Главный координатор жизненного цикла
│       └── cli.py              # Интерфейс командной строки (CLI)
└── tests/
    ├── __init__.py
    ├── conftest.py             # Фикстуры, моки Bridge и Codex
    ├── test_models.py          # Тесты переходов статусов и сериализации
    ├── test_repository.py      # Тесты персистентности SQLite и лимита задач
    ├── test_model_router.py    # Тесты классификации сложности и fallback
    ├── test_waiter.py          # Тесты не-LLM ожидания, таймаутов и отмены
    ├── test_codex_verifier.py  # Тесты парсинга вердиктов и генерации промптов
    ├── test_retry_controller.py# Тесты цикла повторов и генерации отчёта
    ├── test_orchestrator.py    # Тесты сквозного цикла выполнения и восстановления
    └── test_integration.py     # Интеграционные тесты CLI и файлов SQLite
```

---

## 3. Подтверждённые экспериментально интерфейсы

В ходе технического исследования среды подтверждены следующие реальные интерфейсы:

| Компонент | Версия / Путь | Подтверждённый механизм |
| --- | --- | --- |
| **Python** | 3.11.9 (`C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe`) | Стандартная среда исполнения |
| **Agent-Bridge** | 0.1.0 (`C:\Users\admin\.local\bin\agent-bridge.exe`) | MCP-сервер через stdio (JSON-RPC 2.0): `dispatch_task`, `wait_task`, `check_task`, `get_result`, `cancel_task` |
| **Codex CLI** | `0.162.0-alpha.17.2` (`C:\Users\admin\AppData\Local\OpenAI\Codex\bin\2e5e00daee91c61d\codex.exe`) | `codex exec --skip-git-repo-check --ephemeral -s workspace-write -C <cwd> "<prompt>"` |
| **Antigravity CLI** | 1.3.3 (`C:\Users\admin\AppData\Local\agy\bin\agy.exe`) | Получение моделей через `agy models`, запуск воркера через Agent-Bridge |

### Подтверждённые модели Antigravity (`agy models`):
- `gemini-3.8-flash-high`
- `gemini-3.8-flash-medium`
- `gemini-3.8-flash-low`
- `gemini-3.7-flash-high`
- `gemini-3.7-flash-medium`
- `gemini-3.7-flash-low`
- `gemini-3.6-flash-high`
- `gemini-3.6-flash-medium`
- `gemini-3.6-flash-low`
- `gemini-3.1-pro-high`
- `gemini-3.1-pro-low`
- `claude-sonnet-4-6`
- `claude-opus-4-6-thinking`
- `gpt-oss-120b-medium`

---

## 4. Установка и настройка в Windows PowerShell

### 4.1. Клонирование / переход в рабочий каталог
```powershell
cd C:\Users\admin\agent-orchestrator
```

### 4.2. Установка зависимостей и пакета в режиме разработки
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m pip install -e .
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m pip install pytest
```

### 4.3. Настройка конфигурации
Скопируйте `config.example.toml` в `config.toml` (при необходимости измените таймауты или пути):
```powershell
Copy-Item config.example.toml config.toml
```

---

## 5. Запуск тестов

Для запуска полного набора тестов выполните:
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m pytest -v
```

Тесты используют поддельные процессы и моки. `request_id` устраняет дубли только в текущем процессе Agent-Bridge; после перезапуска он не обеспечивает идемпотентность.

---

## 6. Примеры команд запуска оркестратора

### 6.1. Просмотр доступных моделей и матрицы маршрутизации
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m agent_orchestrator.cli models
```

### 6.2. Запуск задачи с автоматической координацией и верификацией
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m agent_orchestrator.cli run `
  -t "Рефакторинг валидации" `
  -p "Добавь валидацию email и тесты" `
  -a "Все тесты проходят, отсутствуют регрессии"
```

### 6.3. Запуск задачи с принудительным выбором модели
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m agent_orchestrator.cli run `
  -t "Критическая оптимизация" `
  -p "Оптимизируй алгоритм поиска" `
  -m "gemini-3.1-pro-high" `
  -e "high"
```

### 6.4. Просмотр списка задач и статуса
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m agent_orchestrator.cli list
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m agent_orchestrator.cli status <ID_ЗАДАЧИ>
```

### 6.5. Просмотр итогового диагностического отчёта
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m agent_orchestrator.cli report <ID_ЗАДАЧИ>
```

---

### Живой просмотр

Во время выполнения оркестратор записывает статусы, события сессии и вердикты в `orchestrator_logs/<короткий ID>.log` рядом с SQLite базой (путь можно задать в `[progress]`). Команда `python -m agent_orchestrator.cli watch [ID]` показывает состояние задачи и новые строки лога; `--once` выводит один снимок, `--interval 2` задаёт интервал обновления.

Итоговый отчёт `orchestrator_report_<короткий ID>.md` записывается в рабочий каталог задачи при любом завершении, включая отмену и ошибку восстановления. Команда `cancel` записывает отмену в БД без обращения к новому процессу Bridge; запущенный оркестратор обнаружит её при следующей проверке, а если он остановлен, работу агента следует проверить вручную.

### Локальная веб-панель

`python -m agent_orchestrator.cli dashboard --open` запускает панель на `http://localhost:8765/`: список задач, статус, попытка, оставшееся время, замечания верификатора, история событий и живой журнал Antigravity. Панель работает только на этом компьютере (`127.0.0.1`), читает SQLite и лог, не запускает Agent-Bridge и `agy`. Единственное действие записи — отмена задачи — защищено токеном, создаваемым при запуске; запросы с чужим `Host` отклоняются. Задачу можно открыть напрямую: `http://localhost:8765/#<ID задачи>`.

## 7. Восстановление после сбоя (Crash Recovery)

Если процесс оркестратора был внезапно завершён (перезагрузка системы, отключение питания, kill процесса):

1. При повторном старте оркестратор обращается к SQLite и находит задачи со статусом `running` или `queued`.
2. Оркестратор опрашивает `Agent-Bridge` по сохранённому `bridge_task_id`:
   - **Если Antigravity завершил работу во время отсутствия оркестратора**: оркестратор считывает артефакты, получает `git diff` и немедленно переходит к шагу верификации Codex.
   - **Если задача ещё выполняется в текущем экземпляре Bridge**: оркестратор возобновляет ожидание. Задача из сохранённого состояния другого экземпляра помечается как `failed`: её нужно проверить вручную.
   - **Если задача утрачена из памяти Bridge**: задача помечается как `failed` с сохранением подробного лога ошибки.
3. Команда ручного запуска восстановления:
```powershell
& "C:\Users\admin\AppData\Local\Programs\Python\Python311\python.exe" -m agent_orchestrator.cli recover
```

---

## 8. Политика выбора моделей (Model Router)

| Категория сложности | Примеры задач | Выбираемая модель | Reasoning Effort |
| --- | --- | --- | --- |
| **`trivial`** | Исправление опечаток, форматирование, docstring | `gemini-3.8-flash-low` | `low` |
| **`simple`** | Локальная правка одной функции, добавление флага | `gemini-3.8-flash-low` | `low` |
| **`moderate`** | Несколько файлов, добавление тестов, модуль API | `gemini-3.8-flash-high` | `medium` |
| **`complex`** | Глубокий рефакторинг, deadlock, сложная отладка | `gemini-3.1-pro-high` | `high` |
| **`expert`** | Архитектура ядра, системный уровень, высокий риск | `gemini-3.1-pro-high` | `high` |

В случае недоступности целевой модели задействуется fallback: `gemini-3.8-flash-high`.

---

## 9. Известные ограничения

1. **Ограничение параллелизма**: В текущей версии поддерживается максимум 1 активная задача одновременно для гарантированного предотвращения конфликтов изменений в одном рабочем каталоге.
2. **Сессии Agent-Bridge**: Сохранённое состояние доступно для чтения после перезапуска. Ожидание и отмена задач предыдущего экземпляра Bridge недоступны; повторная отправка с тем же `request_id` не устраняет это ограничение.
3. **Безопасность песочницы**: Все вызовы `codex exec` выполняются с явным указанием политики песочницы (`workspace-write`), без обхода проверок безопасности и без автоматических `git commit` / `git push`.

## Лицензия

MIT — см. [LICENSE](LICENSE). Сайт проекта: https://qosmuratsamat0.github.io/agent-orchestrator/
