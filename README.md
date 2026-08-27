# okx-bybit-social-bot

Отдельный от `binance-square-bot` проект: генерирует черновики постов
для OKX Orbit (и в перспективе Bybit ByX) и присылает их владельцу в
Telegram для ручной публикации. Работает через GitHub Actions по
расписанию, без постоянно запущенного сервера.

## Почему отдельный репозиторий, а не всё в одном

У OKX Orbit и Bybit ByX нет публичного API для публикации (в отличие
от Binance Square) - см. `okx_draft_publisher.py`. Публикация всегда
вручную. Помимо этого, разделение репозиториев даёт изоляцию:

- Свой git, свой push, свой `bot_state.db` - ошибка или сбой здесь
  физически не может затронуть работающий и уже приносящий доход
  Binance-бот.
- Свои GitHub Secrets - компрометация одного набора ключей не даёт
  доступа к другому проекту.
- Свой workflow с отдельным `concurrency.group` и расписанием - не
  конкурирует за ресурсы/лимиты Actions с Binance-репозиторием.

## Структура

```
market_stats.py         - реальные рыночные числа (% изменения, амплитуда), без Binance-специфики
chart_generator.py      - построение PNG-графика (копия из binance-square-bot, без изменений)
groq_client.py          - обёртка над Groq API (копия, без изменений)
cliche_filter.py        - фильтр ИИ-штампов в готовом тексте (копия, без изменений)
voice_guidelines.py     - общий стилевой словарь для промптов (копия, без изменений)
post_format.py          - только DISCLAIMER (урезано - Binance-хэштеги тут не нужны)
state_store.py          - состояние бота в SQLite (кулдауны/джиттер/ротация), общий для OKX+Bybit
okx_orbit_generator.py  - генерация текста + графика под OKX Orbit
okx_draft_publisher.py  - доставка черновика в Telegram (OKX)
bybit_byx_generator.py  - генерация текста + графика под Bybit ByX
bybit_draft_publisher.py- доставка черновика в Telegram (Bybit, отдельный chat_id от OKX)
main.py                 - раннер тика (--once для Actions, планировщик для локального запуска)
```

## Деплой

1. Создать пустой репозиторий на GitHub с именем `okx-bybit-social-bot`.
2. В этой папке:
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin git@github.com:<твой-аккаунт>/okx-bybit-social-bot.git
   git push -u origin main
   ```
3. В Settings -> Secrets and variables -> Actions добавить:
   - `TELEGRAM_BOT_TOKEN` - тот же токен, что и в binance-square-bot (другой бот заводить не нужно)
   - `GROQ_API_KEY`, `GROQ_MODEL_SECONDARY` (Groq периодически депрекирует модели - см. https://console.groq.com/docs/deprecations - если генерация упадёт с `404 Not Found`, дело почти всегда в устаревшем имени модели, а не в токене)
   - `OKX_ORBIT_DRAFT_CHAT_ID` - chat_id чата (личка или группа), куда будут падать черновики OKX, получить через `@userinfobot`
   - `BYBIT_BYX_DRAFT_CHAT_ID` - **отдельный** chat_id для черновиков Bybit - не тот же, что у OKX, иначе черновики двух бирж смешаются в одном чате
   - `BYBIT_BYX_ENABLED` = `true` (по умолчанию `false` - выключено)
   - Опционально: `OKX_ORBIT_ENABLED`, `OKX_ORBIT_INTERVAL_HOURS`, `OKX_ORBIT_JITTER_HOURS`, `BYBIT_BYX_INTERVAL_HOURS`, `BYBIT_BYX_JITTER_HOURS` (если не заданы - используются значения по умолчанию из workflow).
4. Settings -> Actions -> General -> Workflow permissions -> **Read and write permissions** (нужно, чтобы бот мог коммитить `bot_state.db` обратно).
5. Actions -> "OKX/Bybit Social Bot" -> Run workflow (первый ручной запуск, дальше сам по расписанию раз в час).

## Локальный запуск (для отладки)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # заполнить своими ключами
python main.py --once
```

## Известные грабли

- **`404 Not Found` от Groq** - модель из `.env`/секрета устарела. Groq регулярно выводит модели из эксплуатации (заранее письмом на почту) - проверить актуальный список: https://console.groq.com/docs/deprecations
- **Секрет задан, но пустой** (`${{ secrets.X }}` без значения) - GitHub Actions всё равно подставит переменную окружения, просто пустой строкой, а не отсутствующей. `os.environ.get("X", default)` в этом случае вернёт `""`, а не `default`. В `bot.yml` для необязательных секретов используется `${{ secrets.X || 'default' }}` именно поэтому.
- **Черновик "не пришёл" в Telegram** - проверить архив чатов (Telegram автоматически архивирует тихие чаты) и что настройки в `.env` совпадают с секретами на GitHub (это две независимые копии).
