# Ghostwriter: ассистент для переписки в Telegram и почте

Ассистент подключается к **твоему** аккаунту Telegram (userbot на Telethon), читает выбранные чаты
и по командам делает:

- **саммари**: «саммари последних сообщений от Владимира», «саммари группы Работа за день», `/summary 50 from Vladimir`
- **ответ в твоём стиле**: «ответь в моем стиле», «ответь в моем стиле, вежливо откажись», `/reply`
- **вопросы по переписке**: «о чём мы договорились про поездку?», `/ask ...`

Чаты выбираются прямо в боте:
- **собеседник** (`/contact`): человек, на чьи новые сообщения ассистент сам готовит черновики;
- **текущий чат** (`/chat`): любой чат или группа, по которым делаются саммари, вопросы и «ответь в моем стиле».
  Черновики на сообщения собеседника при этом продолжают приходить. Чат можно назвать и прямо в
  запросе: «саммари группы Работа за день» — текущий чат при этом не меняется.

На новые сообщения собеседника он сам готовит черновик. Ничего не отправляется без твоего
подтверждения: черновик приходит в отдельный приватный бот-пульт с кнопками
**Отправить / Изменить / Пропустить / Заново**. Чувствительные темы (деньги, встречи и даты,
тяжёлые разговоры, вопрос «ты бот?») не черновикуются, по ним приходит только уведомление.

Почта (IMAP/SMTP) опциональна и привязана к собеседнику из `.env` (`VLADIMIR_TG_ID` + `VLADIMIR_EMAIL`): для него история Telegram и почты общая.

## Как это устроено

```
Telethon (твой аккаунт) ─┐                                   ┌─> Telethon: «печатает…» + отправка
                         ├─> SQLite ─> проверка на эскалацию ─> черновик (Claude) ─> бот-пульт ─┤
IMAP (опционально) ──────┘   (общая история)                                     (ты решаешь)   └─> SMTP с заголовками треда
```

| Модуль | Что делает |
|---|---|
| `ghostwriter/channels/telegram.py` | слушает чат, импортирует историю, отправляет с «печатает…» |
| `ghostwriter/channels/email.py` | IMAP-опрос (входящие + отправленные), SMTP с `In-Reply-To`/`References` |
| `ghostwriter/storage/` | SQLAlchemy async: `messages`, `drafts`, `kv`. SQLite, заменяется на Postgres через `DATABASE_URL` |
| `ghostwriter/llm/escalation.py` | классификатор чувствительных тем: сначала regex-подсказки, решает Claude |
| `ghostwriter/llm/drafter.py` | черновик в твоём стиле: `persona/` + твои реальные сообщения + история |
| `ghostwriter/llm/analyst.py` | саммари, ответы на вопросы, разбор команд на естественном языке |
| `ghostwriter/control/bot.py` | бот-пульт на aiogram, отвечает **только** `OWNER_TG_ID` |
| `ghostwriter/scheduling.py` | тихие часы (`ACTIVE_HOURS`, `Europe/Helsinki`) и случайная задержка |
| `ghostwriter/core.py` | весь поток: сообщение → проверка → черновик → одобрение → отправка по таймеру |

Несколько сообщений подряд склеиваются (`DEBOUNCE_SECONDS`), и на них пишется один ответ. Если ты
ответил сам, висящий черновик помечается «заменено». Одобренные сообщения переживают рестарт.

## Установка

Нужен Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # заполни
```

1. **API Telegram**: на https://my.telegram.org → API development tools возьми `TG_API_ID` и `TG_API_HASH`.
2. **Бот-пульт**: создай бота у @BotFather → `CONTROL_BOT_TOKEN`. Свой числовой id (`OWNER_TG_ID`) подскажет, например, @userinfobot. Напиши своему боту `/start`, иначе он не сможет тебе писать.
3. **Вход в аккаунт** (создаёт `secrets/ivan.session`, спросит телефон, код и пароль 2FA):
   ```bash
   python -m ghostwriter login
   ```
4. **Собеседник по умолчанию** (необязательно): `python -m ghostwriter chats` покажет id чатов,
   впиши нужный в `VLADIMIR_TG_ID`. Можно оставить пустым и выбрать собеседника в боте командой `/contact`.
5. **Персона**:
   ```bash
   cp persona/context.example.md persona/context.md      # кто ты и о чём нельзя писать (для всех чатов)
   python -m ghostwriter backfill --limit 2000            # импорт истории (--chat <id> для другого чата)
   python -m ghostwriter export-examples                  # твои реальные сообщения → persona/examples.md
   ```
   О конкретном человеке или группе пиши в `persona/contacts/<id чата>.md` (пример в
   `persona/contacts/example.md`): кто это тебе, как вы общаетесь. Файл читается при каждом черновике,
   перезапуск не нужен. Категории эскалации редактируются в `persona/escalation.toml`.
6. **Запуск**: `python -m ghostwriter run`

### Docker / VPS

```bash
mkdir -p data secrets && sudo chown -R 1000:1000 data secrets persona   # контейнер работает от uid 1000
docker compose run --rm ghostwriter login   # один раз, интерактивно
docker compose up -d
```
Вариант с systemd лежит в `deploy/ghostwriter.service`. Процесс долгоживущий (Telethon держит
соединение), поэтому serverless не подходит.

## Команды бота-пульта

| | |
|---|---|
| обычный текст | Claude поймёт, что нужно: саммари, ответ, вопрос или смена чата («переключись на чат с Петей») |
| `/contact [имя]` | выбрать собеседника для автоматических черновиков (без имени покажет список) |
| `/chat [название]` | выбрать текущий чат или группу для саммари, вопросов и `/reply` |
| `/summary [N] [24h\|3d] [from Имя] [тема]` | саммари текущего чата |
| `/reply [указания]` | черновик ответа на последнее входящее в текущем чате |
| `/ask вопрос` | вопрос по текущему чату |
| `/pending` | черновики, ждущие решения |
| `/sync [N]` | подтянуть историю текущего чата |
| `/status` | модель, каналы, счётчики, тихие часы |
| `/cancel` | отменить ввод текста для «Изменить» |

## Настройки

Полный список в `.env.example`. Основные:
- `ANTHROPIC_MODEL`: по умолчанию `claude-sonnet-5`
- `ACTIVE_HOURS=09:00-23:00`, `TIMEZONE=Europe/Helsinki`: вне этих часов отправка ждёт утра
- `REPLY_DELAY_SECONDS=60-600`: случайная задержка перед отправкой
- `AUTO_MODE=false`: если включить, неэскалированные черновики уходят без подтверждения
  (карточка всё равно приходит, отправку можно отменить)

## Коннектор для приложения Claude (без API-ключа)

Ассистент может работать как **MCP-сервер**: тогда переписку читает и черновики пишет Claude в
приложении (claude.ai, телефон, Claude Desktop, Claude Code) по твоей подписке, а сервер только
достаёт сообщения из Telegram и отправляет одобренные тобой ответы.

Инструменты, которые получает Claude:

| Инструмент | Что делает |
|---|---|
| `list_chats` | найти чат или группу (поиск по всем чатам) |
| `get_messages` | сообщения чата за период / последние N / от конкретного человека (свежие из Telegram) |
| `search_messages` | поиск по всей истории чата |
| `get_persona` | твой `context.md`, заметки о собеседнике, примеры стиля |
| `send_message` | отправить от твоего имени (только при `MCP_ALLOW_SEND=true`; приложение Claude спрашивает подтверждение) |

Пример разговора в приложении: «достань переписку с Владимиром за неделю — что он от меня хочет?»
→ обсуждаешь → «напиши ответ в моём стиле» → правишь → «отправь».

**Настройка:**
1. Придумай пароль для входа (12+ символов) и получи его хеш:
   ```bash
   docker compose run --rm ghostwriter hash-password
   ```
   В `.env`:
   ```
   MCP_ENABLED=true
   MCP_AUTH=oauth
   MCP_PASSWORD_HASH=scrypt:...      # из команды выше
   MCP_ALLOW_SEND=true               # если нужна отправка из Claude
   MCP_DOMAIN=203-0-113-7.sslip.io   # IP сервера через дефисы + .sslip.io, или свой домен
   ```
   `ANTHROPIC_API_KEY` и `CONTROL_BOT_TOKEN` можно оставить пустыми: без ключа нет только
   автоматических черновиков (они требуют API), без токена бота — бота-пульта.
2. На сервере открой порты 80 и 443 (на Oracle Cloud — и в Security List, и в файрволе ОС) и запусти
   с HTTPS-прокси:
   ```bash
   docker compose --profile https up -d --build
   curl https://$MCP_DOMAIN/health        # должно ответить ok
   ```
3. На claude.ai: **Settings → Connectors → Add custom connector**, адрес `https://<MCP_DOMAIN>/mcp`.
   Откроется страница входа на твоём сервере — введи пароль. После этого коннектор доступен и в
   мобильном приложении. Для Claude Code: `claude mcp add --transport http telegram https://<MCP_DOMAIN>/mcp`,
   затем `/mcp` → войти.

**Как устроен вход (OAuth):** Claude регистрируется на сервере как клиент, ты один раз вводишь пароль,
Claude получает токен доступа на 1 час и токен обновления на 30 дней (меняется при каждом обновлении).
Токены хранятся в базе только в виде хешей. Принимаются только клиенты, возвращающие на
claude.ai / claude.com или localhost (Claude Code, Desktop). После 5 неверных паролей вход
блокируется на 15 минут.

- Кто подключён: `docker compose run --rm ghostwriter mcp-sessions`
- Выкинуть всех (придётся войти заново): `docker compose run --rm ghostwriter mcp-sessions --revoke-all`
- Сменить пароль: новый `hash-password` → `.env` → `docker compose up -d --force-recreate` (уже
  выданные токены продолжают работать — отзови их командой выше).

Запасной режим без OAuth: `MCP_AUTH=token` и `MCP_TOKEN=<openssl rand -hex 24>`, адрес коннектора
`https://<MCP_DOMAIN>/<MCP_TOKEN>/mcp` (секрет в адресе — не публикуй его).

Ответы инструментов содержат переписку — она попадает в чат с Claude и расходует лимиты подписки.

## Operations cheat sheet (server)

Run these on the server, in the project folder (`cd ~/ch-sum`).

**Using the connector**
- The `Telegram` connector added on claude.ai also appears in the Claude mobile app — no second login.
  Enable it in a chat via the tools menu.
- Test sending safely first: ask Claude to send a test message to your own *Saved Messages*
  (chat_id = your own Telegram user id). The app asks you to confirm every `send_message` call.
- Everything Claude reads through the connector counts against your subscription limits and stays in
  the claude.ai chat history. For big groups ask for a period ("last 24 hours", "this week"),
  not "the whole history".

**Connected clients (OAuth)**
```bash
docker compose run --rm ghostwriter mcp-sessions               # who is connected
docker compose run --rm ghostwriter mcp-sessions --revoke-all  # log everyone out
```

**Update after changes in the repository**
```bash
cd ~/ch-sum && git pull && docker compose --profile https up -d --build
```
Always pass `--profile https`, otherwise Caddy (HTTPS) is not started.

**Logs and status**
```bash
docker compose ps                        # both containers should be "Up"
docker compose logs -f ghostwriter       # follow logs, Ctrl+C to stop
curl https://<MCP_DOMAIN>/health         # should print "ok"
```

**After editing `.env`:** `docker compose --profile https up -d --force-recreate`
(`docker compose restart` does not pick up `.env` changes).

**Gotchas**
- `sqlite3.OperationalError: unable to open database file` — the container runs as uid 1000 and
  can't write the mounted folders: `sudo chown -R 1000:1000 data secrets persona`.
- Run the bot in **one place only**. Two copies with the same bot token / Telegram session fight over
  updates — stop the local copy (`docker compose down`) when the server one runs.
- A traceback ending in `query is too old and response timeout expired` means a button was pressed
  while the bot was down; it is harmless.
- Oracle Cloud: ports 80/443 must be open both in the subnet's Security List and in the OS firewall
  (`iptables`), and a 1 GB machine needs swap for `docker compose build`.

## Расход токенов

Дороже всего черновики: в промпт идут персона, `examples.md`, твои недавние сообщения (стиль),
история и сообщения, на которые нужен ответ. Размер регулируется в `.env`:

| Переменная | По умолчанию | Что ограничивает |
|---|---|---|
| `HISTORY_LIMIT` | 15 | сообщений истории перед теми, на которые отвечаем |
| `STYLE_SAMPLE_SIZE` | 15 | твоих сообщений как образец стиля (каждое обрезается до 300 символов) |
| `REPLY_BATCH_LIMIT` | 6 | самых свежих неотвеченных сообщений, на которые пишется ответ |
| `DRAFT_MESSAGE_CHARS` | 700 | символов от каждого длинного сообщения/пересланного поста |
| `ESCALATION_HISTORY` | 8 | сообщений контекста для проверки на чувствительность |

Держи `persona/examples.md` коротким (20–40 характерных сообщений): он уходит в каждый черновик.
Саммари отправляют только сообщения за запрошенный период (без периода — последние 100).

## Через LiteLLM (и Langfuse)

Ассистент может ходить к Claude не напрямую, а через твой LiteLLM. Тогда учёт расходов и трейсы
в Langfuse настраиваются на стороне LiteLLM. В `.env`:

```
ANTHROPIC_BASE_URL=http://host.docker.internal:4000/anthropic
ANTHROPIC_API_KEY=sk-...          # виртуальный ключ LiteLLM
ANTHROPIC_MODEL=claude-sonnet-5   # модель Anthropic
```

`/anthropic` — сквозной (pass-through) маршрут LiteLLM: запрос уходит в Anthropic как есть, поэтому
структурированный вывод (`output_config`) и кэширование промптов работают без изменений. Можно
указать и `http://host.docker.internal:4000` (общий эндпоинт `/v1/messages`). Тогда `ANTHROPIC_MODEL`
должен совпадать с `model_name` из конфига LiteLLM, но новые параметры API LiteLLM может не передать.

Через шлюз каждый запрос помечается тегами `ghostwriter` и назначением: `route` (разбор команды),
`summary`, `question`, `escalation`, `draft`. LiteLLM передаёт их в Langfuse как теги трейса
(фильтр Trace Tags), так видно, что сколько стоит и где модель ошиблась.

Если LiteLLM запущен в другом compose-проекте, вместо `host.docker.internal` можно подключить
его сеть (закомментированный блок в `docker-compose.yml`) и писать `http://litellm:4000/anthropic`.

## Безопасность

- `.env`, `secrets/`, `*.session`, `data/`, `persona/context.md`, `persona/examples.md` и `persona/contacts/` в `.gitignore`.
- Ответ в группу увидят все её участники: на карточке такого черновика есть строка «Куда: 👥 …».
  **Файл сессии даёт полный доступ к твоему Telegram**, храни его как пароль.
- Бот-пульт игнорирует всех, кроме `OWNER_TG_ID`, и работает только в личке.
- На уровне INFO тексты сообщений не логируются; логи сторонних библиотек приглушены.
- Модели запрещено выдумывать факты о тебе, давать обещания и признаваться, что она ИИ.
  Если без этого не ответить, черновик превращается в эскалацию.
- Telegram официально разрешает сторонние клиенты для своего аккаунта (для этого и выдаются
  `api_id`/`api_hash`). Запрещены злоупотребления: спам, массовые рассылки, флуд запросами.
  Используй свой `api_id`, не пиши незнакомым и не делай огромных `/sync`. Главные поводы для
  проверки со стороны Telegram: вход с нового IP (особенно из дата-центра) и жалобы на спам.

## Разработка

```bash
pytest -q
```
Тесты не ходят в сеть: LLM, отправка и бот подменяются заглушками.

## Открытые вопросы

- Почтовый провайдер. Сейчас сделан обычный IMAP/SMTP; для Gmail нужен app password и `IMAP_SENT_FOLDER="[Gmail]/Sent Mail"`.
- SQLite или Supabase. Хранилище уже на SQLAlchemy: для Postgres/Supabase поставь `pip install ".[postgres]"` и `DATABASE_URL=postgresql+asyncpg://...`.
- Голосовые сообщения пока сохраняются как `[voice message]` без расшифровки.
