# P1.1 — Cooldown по символу после стоп-аута

## Что сделано

- **config.py** — новая переменная `FUTURES_SYMBOL_COOLDOWN_HOURS` (по умолчанию 4 часа,
  задаётся через env `FUTURES_SYMBOL_COOLDOWN_HOURS`).
- **queue_manager.py** — `was_recently_stopped_out(symbol, cooldown_hours)` и
  `mark_stopped_out(symbol)`, персистентно в SQLite (`bot_state.db`), по тому же
  паттерну, что и существующие `was_recently_alerted` / `mark_alerted`. Переживает
  рестарт/деплой бота.
- **futures_position_monitor.py** — в `check_open_positions` cooldown ставится
  ТОЛЬКО при настоящем стоп-лоссе (`reason == "стоп-лосс (SL)"`), не при стопе
  в безубытке после частичного профита — там ситуация другая, чистого сигнала
  "монета пилит у уровня" нет.
- **futures_signal_bridge.py** — в `execute_signal` добавлена проверка cooldown
  сразу после проверки "уже есть открытая позиция" и до похода за mark price.
  При срабатывании cooldown сигнал не идёт в реальное исполнение (посты в
  Telegram/Binance Square не затрагиваются).
- **test_futures_signal_bridge.py**, **test_futures_position_monitor.py** —
  добавлены тесты на новую логику (5 новых тестов).

## Как применить

### Вариант A — патч (рекомендуется, если репозиторий под git)
```bash
cd /путь/к/Binance_Bot_repo
git apply P1.1_cooldown.patch
# или, если не git-репозиторий:
patch -p1 < P1.1_cooldown.patch
```

### Вариант B — просто заменить файлы
Скопируйте 6 файлов из этого архива поверх соответствующих файлов в
`Binance_Bot_repo/`, ЗАМЕНИВ существующие. Если вы уже вносили в них свои
правки после P1.1 — используйте вариант A (патч), чтобы не потерять их.

## Проверка после применения
```bash
pip install pytest --break-system-packages
cd Binance_Bot_repo
rm -f bot_state.db   # если это ваша РЕАЛЬНАЯ база — НЕ удаляйте! см. ниже
python3 -m pytest test_futures_signal_bridge.py test_futures_position_monitor.py -q
```
Ожидается: `44 passed`.

## ВАЖНО — bot_state.db
В присланном вами архиве оказался реальный `bot_state.db` (позиции, серии
убытков, kill-switch). Если гоняете полный тестовый набор (`pytest` без
фильтра) — убедитесь, что тесты запускаются НЕ в рабочей директории с боевой
базой, иначе тесты `test_risk_guard.py` будут падать из-за чужого состояния
в БД (шумно, но не критично — сами тесты корректны). Рекомендую добавить
`bot_state.db` в `.gitignore`, если это ещё не сделано.

## Дальше по роадмапу
Следующий пункт — **P1.2**: таймаут для зависших реальных позиций
(аналог `OUTCOME_MAX_TRACK_HOURS`, но для реальных testnet-позиций).
