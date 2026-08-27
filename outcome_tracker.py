"""
outcome_tracker.py - трекинг результатов опубликованных сигналов.

Проблема, которую закрывает этот модуль: бот публикует вход/стоп/тейк,
но раньше НИКОГДА не проверял, что случилось с ценой дальше. Без этого
невозможно ответить на вопрос "а сигналы вообще прибыльные?" и нечем
калибровать score/порог публикации - только "на глаз".

Логика:
1. После УСПЕШНОЙ публикации сигнала (main._publish_signal) вызывается
   record_signal_outcome(signal) - кладёт сигнал в open_outcomes
   (см. queue_manager). Специально после публикации, а не до - если
   пост не вышел, аудитория его не видела и трекать нечего.
2. На каждом тике check_open_outcomes() скачивает 15m-свечи с момента
   публикации по каждому открытому сигналу и смотрит, какой уровень
   был задет раньше - тейк или стоп (пробег по свечам в хронологическом
   порядке, см. _resolve_outcome).
3. Если прошло больше config.OUTCOME_MAX_TRACK_HOURS и ни один уровень
   не задет - закрываем как "timeout" по последней известной цене.
4. Результат уходит в closed_outcomes - на нём считается win-rate и
   средний % результата, в целом и в разбивке по стратегии/quality
   (get_accuracy_stats). Это и есть ответ на вопрос "работает ли
   формула score" - без домыслов, на фактической цене с Binance.
5. Дополнительно к результату сохраняется mfe_pct (Max Favorable
   Excursion - насколько цена всё-таки прошла в сторону тейка, прежде
   чем развернуться) и hours_to_close. Это позволяет loss_review_generator
   отличать "почти дошло до тейка, но развернулось" от "сразу пошло
   против" - НА ОСНОВЕ РЕАЛЬНЫХ ЦЕН, а не догадок LLM о причине.

Намеренное упрощение: если в ОДНОЙ 15-минутной свече задеты И тейк, И
стоп одновременно, мы не знаем, что произошло раньше внутри свечи -
консервативно засчитываем это как убыток (loss), чтобы не завышать
статистику себе в пользу.
"""
import logging
import time

import requests

import config
import queue_manager

logger = logging.getLogger(__name__)

_BASE_URL = "https://data-api.binance.vision/api/v3"


def _is_short(direction: str) -> bool:
    d = direction.lower()
    return "шорт" in d or "short" in d


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def record_signal_outcome(signal, bluesky_ref: dict | None = None) -> None:
    """Ставит только что опубликованный сигнал на трекинг результата.
    Вызывать ПОСЛЕ успешной публикации, не раньше.

    bluesky_ref - {uri, cid} корневого поста в Bluesky (обычного или
    первого поста треда "сильного сетапа", см. main._crosspost_to_bluesky),
    если кросспост туда состоялся. Нужен, чтобы при закрытии сделки
    (check_open_outcomes) можно было ответить В ТОТ ЖЕ ТРЕД с итогом -
    формат "До/После" (см. post_format.build_bluesky_outcome_reply). Если
    Bluesky не настроен или кросспост не удался - None, и при резолюции
    просто не будет реплая (публикация на Square этим не затрагивается)."""
    entry_low = _to_float(signal.entry_low)
    entry_high = _to_float(signal.entry_high)
    if entry_low and entry_high:
        entry = (entry_low + entry_high) / 2
    else:
        entry = entry_low or entry_high

    stop = _to_float(signal.invalidation)
    target = _to_float(signal.target)

    if not (entry and stop and target):
        logger.warning(
            "Трекинг результата пропущен для %s - не хватает числовых уровней "
            "(entry=%s stop=%s target=%s)", signal.ticker, entry, stop, target,
        )
        return

    record = {
        "ticker": signal.ticker,
        "symbol": f"{signal.ticker}USDT",
        "direction": "short" if _is_short(signal.direction) else "long",
        "strategy": signal.strategy,
        "quality": signal.quality,
        "score": int(_to_float(signal.score)),
        "entry": entry,
        "stop": stop,
        "target": target,
        "published_at": time.time(),
        "bluesky_ref": bluesky_ref,
    }
    queue_manager.add_open_outcome(record)
    logger.info(
        "Сигнал %s поставлен на трекинг результата (%s, entry=%.6g stop=%.6g target=%.6g)",
        record["ticker"], record["direction"], entry, stop, target,
    )


def _fetch_path_klines(symbol: str, since_ts: float) -> list[dict]:
    """15m-свечи с момента публикации по сейчас (макс. 1000 - хватает
    даже на OUTCOME_MAX_TRACK_HOURS=48ч с большим запасом: 48ч/15м=192)."""
    try:
        resp = requests.get(
            f"{_BASE_URL}/klines",
            params={
                "symbol": symbol,
                "interval": "15m",
                "startTime": int(since_ts * 1000),
                "limit": 1000,
            },
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as e:
        logger.debug("Не удалось получить свечи для трекинга %s: %s", symbol, e)
        return []
    return [{"high": float(r[2]), "low": float(r[3]), "close": float(r[4])} for r in rows]


def _mfe_pct(entry: float, best_price: float, is_short: bool) -> float:
    """Max Favorable Excursion - насколько далеко цена всё-таки прошла
    В СТОРОНУ тейка, прежде чем сигнал закрылся (даже если в итоге не
    сработал). Например, -1% при цели +2% значит "почти дошло, но не
    хватило" - в отличие от 0%, когда цена сразу пошла против сделки.
    Это измеримый факт по свечам, не предположение о причине."""
    if is_short:
        return round((entry - best_price) / entry * 100, 3)
    return round((best_price - entry) / entry * 100, 3)


def _resolve_outcome(record: dict, candles: list[dict]) -> tuple[str, float, float] | None:
    """Пробегает по свечам в хронологическом порядке, смотрит, что
    случилось раньше - тейк или стоп, и попутно считает MFE ТОЛЬКО по
    свечам до момента разрешения (не по всей истории до "сейчас" - иначе
    в MFE утекли бы будущие данные после того, как сделка уже закрылась).
    Возвращает (result, exit_price, mfe_pct) либо None, если пока ничего
    не задето."""
    is_short = record["direction"] == "short"
    target, stop, entry = record["target"], record["stop"], record["entry"]
    best_price = None

    for c in candles:
        if is_short:
            best_price = c["low"] if best_price is None else min(best_price, c["low"])
            hit_target = c["low"] <= target
            hit_stop = c["high"] >= stop
        else:
            best_price = c["high"] if best_price is None else max(best_price, c["high"])
            hit_target = c["high"] >= target
            hit_stop = c["low"] <= stop

        if hit_target and hit_stop:
            return "loss", stop, _mfe_pct(entry, best_price, is_short)  # неизвестно, что раньше внутри свечи - консервативно засчитываем убыток
        if hit_target:
            return "win", target, _mfe_pct(entry, best_price, is_short)
        if hit_stop:
            return "loss", stop, _mfe_pct(entry, best_price, is_short)

    return None


def check_open_outcomes() -> dict:
    """Проверяет все открытые трекинги на новые результаты.
    Возвращает {"closed": N, "still_open": M, "closed_records": [...]} -
    closed_records нужен main.tick() для формата "Win-reveal"/"До-После"
    в Bluesky (см. main._post_outcome_updates_to_bluesky) - без него
    пришлось бы повторно вычитывать closed_outcomes и гадать, какие из
    них новые именно в этом тике."""
    open_items = queue_manager.get_open_outcomes()
    if not open_items:
        return {"closed": 0, "still_open": 0, "closed_records": []}

    still_open: list[dict] = []
    newly_closed: list[dict] = []
    max_age_seconds = config.OUTCOME_MAX_TRACK_HOURS * 3600

    for record in open_items:
        age = time.time() - record["published_at"]
        candles = _fetch_path_klines(record["symbol"], record["published_at"])
        resolved = _resolve_outcome(record, candles) if candles else None

        if resolved is not None:
            result, exit_price, mfe_pct = resolved
        elif age >= max_age_seconds:
            if not candles:
                # Не удалось получить свечи вообще - не закрываем вслепую,
                # попробуем снова на следующем тике.
                still_open.append(record)
                continue
            is_short = record["direction"] == "short"
            best_price = min(c["low"] for c in candles) if is_short else max(c["high"] for c in candles)
            result, exit_price = "timeout", candles[-1]["close"]
            mfe_pct = _mfe_pct(record["entry"], best_price, is_short)
        else:
            still_open.append(record)
            continue

        sign = -1 if record["direction"] == "short" else 1
        pnl_pct = sign * (exit_price - record["entry"]) / record["entry"] * 100 if record["entry"] else 0.0
        closed_at = time.time()

        closed = dict(record)
        closed.update({
            "result": result,
            "exit_price": exit_price,
            "pnl_pct": round(pnl_pct, 3),
            "mfe_pct": mfe_pct,
            "hours_to_close": round((closed_at - record["published_at"]) / 3600, 2),
            "closed_at": closed_at,
        })
        newly_closed.append(closed)
        logger.info("Результат сигнала %s: %s (%+.2f%%, MFE %+.2f%%)", record["ticker"], result, pnl_pct, mfe_pct)

    queue_manager.replace_open_outcomes(still_open)
    if newly_closed:
        queue_manager.append_closed_outcomes(newly_closed)

    return {"closed": len(newly_closed), "still_open": len(still_open), "closed_records": newly_closed}


def get_accuracy_stats(days: float | None = None) -> dict:
    """Агрегированная статистика по closed_outcomes: общий win-rate и
    средний pnl%, плюс разбивка по strategy/quality. days=None - за
    всё время, иначе только записи, закрытые за последние `days` дней.

    win_rate считается только по решённым исходам (win/loss) - timeout
    не в счёт (сигнал не дошёл ни до тейка, ни до стопа, это не победа
    и не поражение стратегии как таковой), но участвует в avg_pnl_pct,
    т.к. по факту деньги там тоже "лежали" эти часы."""
    closed = queue_manager.get_closed_outcomes()
    if days is not None:
        cutoff = time.time() - days * 24 * 3600
        closed = [c for c in closed if c.get("closed_at", 0) >= cutoff]

    def _summarize(items: list[dict]) -> dict:
        n = len(items)
        if n == 0:
            return {"count": 0, "win_rate": None, "avg_pnl_pct": None}
        wins = sum(1 for c in items if c["result"] == "win")
        decided = sum(1 for c in items if c["result"] in ("win", "loss"))
        avg_pnl = sum(c["pnl_pct"] for c in items) / n
        return {
            "count": n,
            "win_rate": round(wins / decided * 100, 1) if decided else None,
            "avg_pnl_pct": round(avg_pnl, 3),
        }

    by_strategy: dict[str, list[dict]] = {}
    by_quality: dict[str, list[dict]] = {}
    for c in closed:
        by_strategy.setdefault(c.get("strategy", "?"), []).append(c)
        by_quality.setdefault(c.get("quality", "?"), []).append(c)

    return {
        "overall": _summarize(closed),
        "by_strategy": {k: _summarize(v) for k, v in by_strategy.items()},
        "by_quality": {k: _summarize(v) for k, v in by_quality.items()},
    }


def get_futures_trade_stats(days: float | None = None) -> dict:
    """Аналог get_accuracy_stats(), но по РЕАЛЬНЫМ закрытым позициям на
    testnet (queue_manager.get_closed_futures_positions(), см.
    futures_position_monitor.py), а не по симуляции цены опубликованных
    сигналов.

    Это НАМЕРЕННО отдельная функция, а не слияние с get_accuracy_stats():
    signal-трекинг считает исход КАЖДОГО опубликованного сигнала (был по
    нему реальный ордер или нет - неважно, там просто путь цены), а этот
    - только те сигналы, по которым futures_signal_bridge реально открыл
    позицию (обычно меньшая, более отфильтрованная выборка - см.
    config.BINANCE_FUTURES_MIN_SIGNAL_SCORE). Смешивать их в одну
    статистику значило бы посчитать win-rate по сделкам, которых по факту
    не было. by_quality не считается - у закрытых futures-позиций нет
    поля quality (оно есть только у RsiSignal, из которого сделка
    открывалась, но не сохраняется в саму запись позиции).

    pnl_pct считается от номинала позиции (entry_price * quantity), а не
    от баланса счёта - так он сопоставим по смыслу с pnl_pct из
    get_accuracy_stats (тот тоже % от цены входа конкретной сделки, а
    не от общего баланса)."""
    closed = queue_manager.get_closed_futures_positions()
    if days is not None:
        cutoff = time.time() - days * 24 * 3600
        closed = [c for c in closed if c.get("closed_at", 0) >= cutoff]

    enriched = []
    for c in closed:
        entry = c.get("entry_price") or 0
        qty = c.get("quantity") or 0
        notional = entry * qty
        pnl = c.get("realized_pnl", 0) or 0
        pnl_pct = (pnl / notional * 100) if notional else 0.0
        result = "win" if pnl > 0 else ("loss" if pnl < 0 else "flat")
        enriched.append({**c, "result": result, "pnl_pct": pnl_pct})

    def _summarize(items: list[dict]) -> dict:
        n = len(items)
        if n == 0:
            return {"count": 0, "win_rate": None, "avg_pnl_pct": None, "total_pnl_usdt": 0.0}
        wins = sum(1 for c in items if c["result"] == "win")
        decided = sum(1 for c in items if c["result"] in ("win", "loss"))
        avg_pnl = sum(c["pnl_pct"] for c in items) / n
        total_pnl = sum(c.get("realized_pnl", 0) or 0 for c in items)
        return {
            "count": n,
            "win_rate": round(wins / decided * 100, 1) if decided else None,
            "avg_pnl_pct": round(avg_pnl, 3),
            "total_pnl_usdt": round(total_pnl, 4),
        }

    by_strategy: dict[str, list[dict]] = {}
    for c in enriched:
        by_strategy.setdefault(c.get("strategy", "?"), []).append(c)

    return {
        "overall": _summarize(enriched),
        "by_strategy": {k: _summarize(v) for k, v in by_strategy.items()},
    }


def get_slippage_stats(days: float | None = None) -> dict:
    """Статистика проскальзывания на входе по реальным testnet-сделкам
    (D1 из роадмапа). Источник - поле slippage_pct, которое
    futures_signal_bridge.execute_signal сохраняет в саму запись
    позиции (открытую ИЛИ уже закрытую - слайппедж фиксируется в момент
    входа и после закрытия никуда не девается из record, см.
    futures_position_monitor - closed_record строится через
    dict(record, ...), то есть наследует все поля открытой записи).
    Поэтому в отличие от get_futures_trade_stats здесь берём и
    ОТКРЫТЫЕ, и ЗАКРЫТЫЕ позиции - проскальзывание не зависит от того,
    закрылась ли уже сделка.

    Записи без slippage_pct (например, старые, открытые ДО того, как
    это поле появилось в коде) молча пропускаются - вернуть 0.0 вместо
    реального значения было бы хуже, чем просто не учитывать их в
    статистике.

    Знак slippage_pct уже нормализован в futures_executor -
    положительное значение ВСЕГДА означает невыгодное исполнение,
    независимо от направления сделки (лонг/шорт) - поэтому avg_pct
    напрямую интерпретируется как \"средняя стоимость входа сверх
    ориентировочной цены, в %\"."""
    records = queue_manager.get_open_futures_positions() + queue_manager.get_closed_futures_positions()
    if days is not None:
        cutoff = time.time() - days * 24 * 3600
        records = [r for r in records if r.get("opened_at", 0) >= cutoff]

    values = [r["slippage_pct"] for r in records if r.get("slippage_pct") is not None]
    if not values:
        return {"count": 0, "avg_pct": None, "max_pct": None, "worst_trades": []}

    worst = sorted(records, key=lambda r: r.get("slippage_pct", 0), reverse=True)[:5]
    return {
        "count": len(values),
        "avg_pct": round(sum(values) / len(values), 4),
        "max_pct": round(max(values), 4),
        "worst_trades": [
            {"symbol": r.get("symbol"), "side": r.get("side"), "slippage_pct": round(r.get("slippage_pct", 0), 4)}
            for r in worst
        ],
    }