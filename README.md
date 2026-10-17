# Сервер E-Hentai → Telegram

Версия **0.3.0**: HTTP API, постоянная очередь, анализ и загрузка источника,
подготовка изображений, последовательная публикация в Telegram и личный бот владельца.
Python 3.12, FastAPI, SQLite, Pillow, асинхронный HTTP-клиент Bot API;
изолированный парсер закреплённого gallery-dl. Точные версии/хеши — в `uv.lock`.

## Реализовано

- Сопряжение одноразовым кодом, токены устройств, привязка к origin, отзыв доступа.
- Нормализация `/g/` и `/s/` (gtoken), анализ с арендой/восстановлением и общая пауза источника.
- Импорт Netscape cookies после проверки, без передачи сессии через API или Telegram.
- Диапазоны/исключения, серверный предпросмотр подписей, hashtags, параметры silent/spoiler.
- Ревизии, атомарное подтверждение одной галереи и упорядоченного batch, идемпотентность.
- Защита от дублей, явный повтор с новым черновиком и повторным подтверждением.
- Потоковое скачивание, проверка DNS/TLS/адресов, лимиты диска/файлов и SHA-256-манифесты.
- Изолированная подготовка JPEG/PNG/WebP, EXIF и поля; анимация требует решения владельца.
- Все страницы готовятся до отправки; фото/альбомы идут последовательно, с журналом результатов.
- Подтверждённые части не повторяются; неизвестная отправка требует ручной сверки.
- Отмена, повторы, пауза, очередь/история с курсором и фильтром состояния, HTTP-предпросмотры.
- Бот проверяет ID владельца и личный чат; команды и ответы имеют постоянные inbox/outbox.
- Уведомления о результате/блокирующей ошибке/необходимости решения; обычные шаги не рассылаются.
- Очистка успешных файлов через 24 часа, backup SQLite, проверяемые миграции.
- Dockerfile и Compose с отдельными API/worker/bot, постоянным томом и локальным портом API.

## Запуск

Полная последовательность, команды бота, HTTP-поля, восстановление, контейнеры и
работа с секретами — в [operations.md](docs/operations.md).
Инструкции источника — [source-analysis.md](docs/source-analysis.md).

```sh
uv sync --locked --extra source
# Создайте .env по .env.example и настройте серверные значения.
uv run --locked --extra source ehtg migrate
uv run --locked --extra source ehtg source probe
uv run --locked --extra source ehtg source import-cookies /private/cookies.txt
uv run --locked --extra source ehtg check-telegram
# После проверки сессии включите EHTG_SOURCE_ENABLED=true.
```

В отдельных терминалах:

```sh
uv run --locked --extra source uvicorn ehtg.api.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
uv run --locked --extra source ehtg worker
uv run --locked --extra source ehtg bot
```

До реализации расширения код можно получить через `ehtg pair` или команду владельца
`/pair`. `EHTG_ALLOWED_ORIGINS` содержит точный origin установленного Chromium-клиента;
без него сопряжение закрыто. `capabilities` сообщает фактические включённые функции
и лимиты; наличие токена означает конфигурацию Telegram, а права проверяются перед отправкой.

## Проверки и сборка

```sh
uv run --locked --extra source ruff check src tests scripts
uv run --locked --extra source ruff format --check src tests scripts
uv run --locked --extra source mypy src scripts
uv run --locked --extra source mypy --platform linux src scripts
uv run --locked --extra source pytest
uv build --no-sources
uv run --locked --extra source python scripts/verify_dist.py
uv run --locked --extra source ehtg export-openapi docs/openapi.json
```

Результаты — [verification.md](docs/verification.md), история этапов —
[development-plan.md](docs/development-plan.md). Исходный контракт —
`../docs/technical-specification.md`; сторонний исходник не копируется в проект,
закрепление и лицензия gallery-dl описаны в `THIRD_PARTY_NOTICES.md`.

Реальный VPS, cookies владельца, настоящий канал Telegram, Linux-лимиты во время
исполнения и совместимость с VPN пока не проверены. Docker daemon в локальном
окружении не запущен: Compose прошёл проверку конфигурации, контейнерный образ
локально не собирался. Разработка серверных сценариев и проверка рабочего
развёртывания учитываются отдельно; до эксплуатации нужна проверка на тестовом канале.
