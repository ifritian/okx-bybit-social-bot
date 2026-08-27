# A1 — частичный профит + безубыток + трейлинг-стоп

## Что делать с этими файлами

Вариант 1 (проще): скопировать 7 файлов из этой папки поверх соответствующих
файлов в `Binance_Bot_repo/`, заменив существующие.

Вариант 2: применить `A1_partial_profit_trailing_stop.patch` из корня
репозитория:

```bash
cd Binance_Bot_repo
git apply /путь/до/A1_partial_profit_trailing_stop.patch
```

(`test_futures_position_monitor.py` — новый файл, патч создаст его сам;
остальные 6 — правки существующих.)

## Что изменилось

**Логика (A1 из роадмапа):** сейчас — фиксированный SL/TP из уровней
сигнала, ничего не меняется после входа до самого закрытия. Стало: когда
цена проходит 50% пути от входа до тейка сигнала (настраивается),
`futures_position_monitor.py` на каждом прогоне:

1. закрывает по рынку 50% позиции (настраивается) — фиксирует часть прибыли;
2. снимает старые стоп/тейк;
3. ставит на остаток стоп в безубыток (по цене входа);
4. ставит на остаток трейлинг-стоп вместо исходного тейка — чтобы поймать
   более крупное движение, если оно продолжится, а не просто зафиксировать
   исходный тейк целиком;
5. шлёт уведомление в Telegram.

Срабатывает не больше одного раза за жизнь позиции. Уведомление о
финальном закрытии позиции теперь тоже правильно называет причину
("трейлинг-стоп остатка позиции" / "стоп-лосс в безубытке"), а не
ошибочно "тейк-профит (TP)".

### Изменённые файлы

- **`config.py`** — 4 новые переменные: `BINANCE_FUTURES_PARTIAL_TP_ENABLED`
  (default `true`), `BINANCE_FUTURES_PARTIAL_TP_TRIGGER_FRACTION` (default
  `0.5`), `BINANCE_FUTURES_PARTIAL_TP_CLOSE_FRACTION` (default `0.5`),
  `BINANCE_FUTURES_TRAILING_CALLBACK_PCT` (default `1.0`).
- **`futures_client.py`** — 3 новых метода: `place_trailing_stop_market`,
  `place_reduce_only_market_order`, `cancel_order` (снять один конкретный
  algo-ордер, не все сразу).
- **`futures_position_monitor.py`** — новая функция `_manage_partial_profit`,
  встроена в `check_open_positions` до проверки закрытия позиции.
- **`futures_signal_bridge.py`** — при открытии позиции в трекинг
  добавляется поле `original_quantity` (нужно, чтобы % PnL при закрытии
  считался от исходного риска сделки, а не от урезанного частичным
  профитом остатка).
- **`test_futures_client.py`**, **`test_futures_signal_bridge.py`** —
  добавлены тесты на новые методы клиента.
- **`test_futures_position_monitor.py`** (новый файл) — 18 тестов на
  `_manage_partial_profit`, `_target_progress_fraction`,
  `_determine_close_reason_and_cleanup`, интеграцию в `check_open_positions`.

### Попутно исправленные баги (не связаны напрямую с A1, но найдены по пути)

1. **Осиротевший algo-ордер не отменялся.** `_determine_close_reason_and_cleanup`
   вызывал только `cancel_all_open_orders` (обычные ордера) — а стоп/тейк
   с 2026 года это отдельная Algo Order API (см. докстринг
   `place_stop_market`), для неё нужен `cancel_all_algo_orders`. Без
   этого фикса второй условный ордер реально оставался бы висеть на бирже
   после закрытия позиции.
2. **Раннер `test_futures_signal_bridge.py`** не поддерживал `monkeypatch`
   — тест `test_execute_signal_applies_soft_derisk_multiplier` падал с
   необработанным `TypeError` и обрывал весь прогон файла (не просто
   помечался как `FAIL`, а вообще не запускался и валил остальные тесты
   после себя). Раннер приведён к тому же виду, что и в
   `test_futures_executor.py`/`test_risk_guard.py`.

Все тесты по проекту прогнаны целиком после изменений (`for f in
test_*.py; do python3 "$f"; done`) — 42 файла, 0 упавших.

## Инструкция по коммиту

```bash
cd Binance_Bot_repo
git add config.py futures_client.py futures_position_monitor.py \
        futures_signal_bridge.py test_futures_client.py \
        test_futures_signal_bridge.py test_futures_position_monitor.py

git commit -m "A1: частичный профит + безубыток + трейлинг-стоп на фьючерсах

- futures_position_monitor: _manage_partial_profit - при 50% пути до
  тейка закрывает 50% позиции по рынку, переводит стоп на остаток в
  безубыток, ставит трейлинг-стоп вместо исходного тейка
- futures_client: place_trailing_stop_market, place_reduce_only_market_order,
  cancel_order (снять один конкретный algo-ордер)
- config: BINANCE_FUTURES_PARTIAL_TP_* / BINANCE_FUTURES_TRAILING_CALLBACK_PCT
- futures_signal_bridge: original_quantity в трекинге позиции (для
  корректного % PnL после частичного закрытия)
- fix: cancel_all_algo_orders не вызывался при чистке осиротевшего
  условного ордера (только cancel_all_open_orders) - алгоритмический
  стоп/тейк мог оставаться висеть на бирже после закрытия позиции
- fix: раннер test_futures_signal_bridge.py не поддерживал monkeypatch -
  один тест (soft-derisk) никогда не запускался и ронял весь файл

Тесты: 18 новых (test_futures_position_monitor.py) + расширены
test_futures_client.py/test_futures_signal_bridge.py. Полный прогон
всех test_*.py - 0 упавших."

git push
```

После пуша: если бот уже гоняется в GitHub Actions (`.github/workflows/bot.yml`),
никаких новых секретов заводить не нужно — все 4 новые переменные имеют
рабочие дефолты в `config.py`. Задать их явно (например, другой процент
частичного закрытия) можно через Settings → Secrets and variables →
Actions, по аналогии с остальными `BINANCE_FUTURES_*`.

## Что дальше по вашему же роадмапу

Рекомендованный порядок был: B1 → A3 (уже было готово) → **A1 (это) →**
C2 (R:R в посте, дёшево и сразу видимый эффект в контенте) → дальше по
тому, что покажет живая статистика.
