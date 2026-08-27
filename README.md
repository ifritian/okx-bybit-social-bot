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
state_store.py          - состояние бота в SQLite (кулдауны/джиттер/ротация), маленький и общий для OKX+Bybit
okx_orbit_generator.py  - генерация текста + графика под OKX Orbit
okx_draft_publisher.py  - доставка черновика в Telegram (OKX)
bybit_byx_generator.py  - (следующий шаг) генерация под Bybit ByX, по образцу okx_orbit_generator.py
bybit_draft_publisher.py- (следующий шаг) доставка черновика в Telegram (Bybit)
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
   - `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_MODEL_SECONDARY`
   - `OKX_ORBIT_DRAFT_CHAT_ID` - **личный** chat_id (не канал), получить через `@userinfobot`. Должен отличаться от чата, куда сыпятся черновики Binance-проекта.
   - Опционально: `OKX_ORBIT_ENABLED`, `OKX_ORBIT_INTERVAL_HOURS`, `OKX_ORBIT_JITTER_HOURS` (если не заданы - используются значения по умолчанию из workflow: `true`/`8`/`2`).
4. Settings -> Actions -> General -> Workflow permissions -> **Read and write permissions** (нужно, чтобы бот мог коммитить `bot_state.db` обратно).
5. Actions -> "OKX/Bybit Social Bot" -> Run workflow (первый ручной запуск, дальше сам по расписанию раз в час).

## Локальный запуск (для отладки)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # заполнить своими ключами
python main.py --once
```

## Следующий шаг

Добавить `bybit_byx_generator.py` и `bybit_draft_publisher.py` по
образцу OKX-модулей (Bybit ByX работает по той же схеме: нет
публичного API, только черновик + ручная публикация), подключить
`try_publish_bybit_byx_draft()` в `main.py` рядом с
`try_publish_okx_orbit_draft()`.
