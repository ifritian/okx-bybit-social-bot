"""
risk_guard.py - общие предохранители ПОВЕРХ риска отдельной сделки (см.
config.BINANCE_FUTURES_RISK_PCT_PER_TRADE и futures_executor.
calc_position_size). Каждая позиция по отдельности может быть правильно
посчитана по риску - и всё равно ничто не мешает открыть их сколько
угодно подряд, пока не кончится баланс, или поймать серию убытков,
каждый из которых сам по себе был "в пределах риска". Этот модуль -
именно про СУММАРНУЮ картину, а не про отдельную сделку:

1. Максимум ОДНОВРЕМЕННО открытых позиций (across всех символов).
2. Максимум позиций В ОДНУ СТОРОНУ одновременно (лонг или шорт
   отдельно) - пункт 1 сам по себе не мешает набрать, например, лонг по
   BTC+ETH+SOL сразу - формально три разных слота, а по факту одна
   большая ставка на рынок вверх, а не три независимых позиции. См.
   get_risk_multiplier ниже про пункт 4 - это НЕ то же самое: там про
   размер риска одной сделки после серии убытков, а не про то, сколько
   сделок можно набрать в одну сторону.
2b. (P3.7) Та же идея, что и пункт 2, но точнее: вместо ПРОСТОГО СЧЁТА
   позиций в одну сторону - сумма их БЕТА к BTC (см.
   _beta_weighted_exposure, config.BINANCE_FUTURES_MAX_BETA_EXPOSURE).
   Две высоко-коррелированные с BTC монеты весят в лимите больше, чем
   две низко-коррелированные - пункт 2 их не различал бы. Опционален,
   работает НЕЗАВИСИМО и одновременно с пунктом 2, не заменяет его.
3. Дневной лимит убытка в % от баланса на начало UTC-дня.
4. Серия убыточных сделок ПОДРЯД (по факту закрытия на бирже).
5. Мягкое снижение риска НОВОЙ сделки (см. get_risk_multiplier), ещё
   ДО того, как серия убытков дойдёт до порога пункта 4 и остановит
   торговлю целиком - промежуточная ступень, а не замена жёсткому
   выключателю.

Лимиты 3 и 4 при срабатывании ВЗВОДЯТ kill switch (см.
queue_manager.set_kill_switch) - персистентный (bot_state.db) флаг
"торговля остановлена". По умолчанию НЕ снимается сам по себе - ни на
следующий UTC-день, ни при следующей прибыльной сделке. Снять его можно
осознанно: `python3 risk_guard_cli.py reset`, посмотрев вначале, что
случилось (`risk_guard_cli.py status`). Это НАМЕРЕННО консервативнее
"тихого" автовосстановления - если один из этих двух лимитов сработал,
решение продолжать торговать по умолчанию должно быть решением
человека, а не побочным эффектом того, что цифры на бирже сами
вернулись в норму.

Опционально (см. config.BINANCE_FUTURES_KILL_SWITCH_AUTO_RESET_HOURS,
выключено по умолчанию) можно доверить это решение боту - тогда
check_new_position_allowed сам снимает kill switch, если он был взведён
дольше настроенного числа часов. При любом снятии - и ручном, и
автоматическом - серия убытков "обнуляется" не физически (история на
бирже никуда не девается), а логически: убытки ДО момента снятия
перестают учитываться в _consecutive_losses (см. параметр since_ts и
queue_manager.get/set_risk_streak_ignore_before) - иначе немедленно на
первой же следующей проверке switch взводился бы заново по той же самой
уже "разобранной" причине, а не давал боту реальный новый шанс.

Лимиты 1, 2 и пункт 5 (мягкое снижение риска) - НЕ взводят kill switch:
это не "что-то пошло не так", а штатная адаптация (подожди слот / рискуй
меньше, пока не восстановишься) - само разрешится на следующей успешной
сделке или освободившемся слоте.

futures_executor.open_protected_position вызывает
check_new_position_allowed ПЕРВЫМ делом - до единого API-вызова на
ИЗМЕНЕНИЕ чего-либо на бирже (до set_leverage и дальше). Отказ здесь
гарантирует, что позиция вообще не будет открыта - не "открыта и потом
аварийно закрыта".
"""
import logging
import math
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
import queue_manager
import scanner

logger = logging.getLogger(__name__)


@dataclass
class RiskLimits:
    max_open_positions: int
    max_daily_loss_pct: float
    max_consecutive_losses: int
    # Мягкая ступень де-рискования ДО жёсткого kill switch - см.
    # get_risk_multiplier() ниже и docstring config.BINANCE_FUTURES_SOFT_DERISK_*.
    # Дефолты здесь совпадают с config.py и существуют только чтобы не
    # ломать старые вызовы RiskLimits(...) без этих двух аргументов
    # (тесты, старые вызывающие места) - в реальной работе бота их
    # всегда явно задаёт limits_from_config.
    soft_derisk_after_losses: int = 2
    soft_derisk_multiplier: float = 0.5
    # A4: лимит позиций В ОДНУ СТОРОНУ одновременно (см. модульный
    # docstring, пункт 2) - None означает "не проверять" (полностью
    # выключено), а не "0 разрешено". Дефолт None, а не число - чтобы
    # старый код/тесты, которые создают RiskLimits(...) без этого поля,
    # не начали внезапно ловить отказ по лимиту, который они не просили.
    max_same_direction_positions: Optional[int] = None
    # P3.7: лимит СУММЫ БЕТА к BTC уже открытых позиций в ТУ ЖЕ сторону
    # (см. модульный докстринг про A4 выше и config.
    # BINANCE_FUTURES_MAX_BETA_EXPOSURE) - более тонкая версия A4:
    # считает не штуки позиций, а насколько они РЕАЛЬНО коррелируют
    # друг с другом через движение BTC. None = выключено (дефолт),
    # работает НЕЗАВИСИМО и одновременно с max_same_direction_positions,
    # не вместо него.
    max_beta_exposure: Optional[float] = None
    # Автоснятие kill switch по таймауту - см. docstring модуля и
    # config.BINANCE_FUTURES_KILL_SWITCH_AUTO_RESET_HOURS. None/0 =
    # выключено (дефолт - требуется ручной risk_guard_cli.py reset).
    kill_switch_auto_reset_hours: Optional[float] = None


def limits_from_config(config) -> RiskLimits:
    return RiskLimits(
        max_open_positions=config.BINANCE_FUTURES_MAX_OPEN_POSITIONS,
        max_daily_loss_pct=config.BINANCE_FUTURES_MAX_DAILY_LOSS_PCT,
        max_consecutive_losses=config.BINANCE_FUTURES_MAX_CONSECUTIVE_LOSSES,
        soft_derisk_after_losses=config.BINANCE_FUTURES_SOFT_DERISK_AFTER_LOSSES,
        soft_derisk_multiplier=config.BINANCE_FUTURES_SOFT_DERISK_MULTIPLIER,
        max_same_direction_positions=config.BINANCE_FUTURES_MAX_SAME_DIRECTION_POSITIONS,
        kill_switch_auto_reset_hours=config.BINANCE_FUTURES_KILL_SWITCH_AUTO_RESET_HOURS,
        max_beta_exposure=config.BINANCE_FUTURES_MAX_BETA_EXPOSURE,
    )


def _utc_day_key(ts: Optional[float] = None) -> str:
    dt = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _daily_loss_pct(client, asset: str = "USDT") -> tuple[float, float, float]:
    """Возвращает (loss_pct, baseline, current). loss_pct положительный,
    если баланс УМЕНЬШИЛСЯ относительно baseline (0 или отрицательный,
    если баланс не падал/вырос за сегодня). baseline фиксируется РОВНО
    ОДИН РАЗ за UTC-день - при самой первой проверке (см. docstring
    модуля) - и дальше не пересчитывается до следующего дня, даже если
    эту функцию вызвать снова позже в тот же день."""
    day_key = _utc_day_key()
    baseline = queue_manager.get_risk_daily_baseline(day_key)
    current = client.get_wallet_balance(asset)
    if baseline is None:
        baseline = current
        queue_manager.set_risk_daily_baseline(day_key, baseline)
        logger.info("risk_guard: зафиксирован дневной baseline на %s: %.4f %s", day_key, baseline, asset)
    if baseline <= 0:
        return 0.0, baseline, current
    loss_pct = (baseline - current) / baseline * 100
    return loss_pct, baseline, current


def _group_partial_fills(rows: list) -> list:
    """Одно закрытие ОДНОЙ позиции Binance нередко исполняет несколькими
    частичными филлами (partial fills) - каждый филл прилетает в income
    history отдельной строкой REALIZED_PNL, но с ОДИНАКОВЫМ symbol и
    ОДИНАКОВЫМ timestamp (совпадает даже до миллисекунды). Без этой
    группировки один реальный убыточный трейд, закрытый, скажем, 27
    филлами, засчитывался бы как 27 отдельных убытков подряд - именно
    так на практике серия "3 убытка подряд" ошибочно раздувалась до
    20-30+ и не давала снять kill switch. rows должны быть уже
    отсортированы по времени - функция сохраняет этот порядок группами
    (группа встаёт на место своего первого филла)."""
    grouped: dict[tuple, float] = {}
    order: list[tuple] = []
    for r in rows:
        key = (r.get("symbol"), int(r.get("time", 0)))
        if key not in grouped:
            grouped[key] = 0.0
            order.append(key)
        grouped[key] += float(r.get("income", 0))
    return [grouped[k] for k in order]


def _consecutive_losses(client, lookback: int = 50, since_ts: Optional[float] = None) -> int:
    """Считает убыточные СДЕЛКИ (не строки income - см. _group_partial_fills)
    ПОДРЯД, начиная с самой последней закрытой - по истории income
    (incomeType=REALIZED_PNL), не по локальному логу бота (тот не увидит
    сделку, закрытую вручную на сайте биржи). Записи с income == 0
    (например, чисто комиссийные строки без реального закрытия позиции)
    игнорируются - это не "выигрыш" и не "проигрыш". Сортирует по
    времени сам, не полагаясь на порядок, в котором Binance отдаёт
    список.

    since_ts (unix-время в секундах), если задан - сделки СТРОГО ДО
    этого момента полностью исключаются из подсчёта, как будто их не
    было (см. queue_manager.get_risk_streak_ignore_before - выставляется
    при снятии kill switch, ручном или автоматическом по таймауту, см.
    docstring модуля)."""
    rows = client.get_income_history(income_type="REALIZED_PNL", limit=lookback)
    nonzero_sorted = [
        r for r in sorted(rows, key=lambda r: int(r.get("time", 0)))
        if float(r.get("income", 0)) != 0
    ]
    if since_ts is not None:
        nonzero_sorted = [r for r in nonzero_sorted if int(r.get("time", 0)) / 1000 >= since_ts]
    trades = _group_partial_fills(nonzero_sorted)
    streak = 0
    for pnl in reversed(trades):
        if pnl < 0:
            streak += 1
        else:
            break
    return streak


def get_risk_multiplier(client, limits: RiskLimits) -> tuple[float, int]:
    """Возвращает (multiplier, streak) - множитель для risk_pct НОВОЙ
    сделки, посчитанный по серии убытков подряд ПРЯМО СЕЙЧАС.

    1.0, пока серия короче limits.soft_derisk_after_losses.
    limits.soft_derisk_multiplier, начиная с этого порога (и до тех пор,
    пока не сработает жёсткий kill switch - см. check_new_position_allowed,
    он вызывается ОТДЕЛЬНО и раньше, эта функция не заменяет его, а
    только смягчает то, что происходит ДО его срабатывания).

    Намеренно НЕ кэширует и не понижает риск постепенно (0.75 -> 0.5 ->
    0.25...) - две ступени (обычный/сниженный) проще объяснить и
    предсказать, чем плавную кривую, а серия убытков и так штука редкая -
    сложная формула здесь не окупает добавленной непрозрачности.
    Использует ту же _consecutive_losses, что и check_new_position_allowed -
    единый источник правды про серию, а не два независимых подсчёта,
    которые могли бы разойтись при доработке одного без другого."""
    since_ts = queue_manager.get_risk_streak_ignore_before()
    streak = _consecutive_losses(client, lookback=max(limits.max_consecutive_losses * 5, 20), since_ts=since_ts)
    if streak >= limits.soft_derisk_after_losses:
        return limits.soft_derisk_multiplier, streak
    return 1.0, streak


def _same_direction_open_count(open_positions: list, side: str) -> int:
    """Сколько из уже открытых позиций - в ТУ ЖЕ сторону, что и side
    новой сделки ("BUY"=лонг/"SELL"=шорт). Знак positionAmt в ответе
    Binance (см. FuturesClient.get_all_positions) - направление позиции:
    положительный = лонг, отрицательный = шорт."""
    is_long_side = side == "BUY"
    return sum(
        1 for p in open_positions
        if (float(p.get("positionAmt", 0)) > 0) == is_long_side
    )


def _fetch_daily_log_returns(symbol: str, lookback_days: int) -> list[float]:
    """Дневные лог-доходности (ln(close_i / close_i-1)) за последние
    lookback_days дней - сырьё для расчёта беты (см. _calc_beta).
    Переиспользует scanner._fetch_klines (тот же публичный market-data
    endpoint Binance, что и для ATR-перцентиля в scanner.py, P2.5) -
    не заводит отдельный сетевой клиент ради одного и того же REST API.
    [] при недостатке данных/сетевой ошибке (_fetch_klines сама уже
    мягко откатывается на [] - см. её докстринг)."""
    candles = scanner._fetch_klines(symbol, limit=lookback_days + 1, interval="1d")
    if len(candles) < 2:
        return []
    closes = [c.close for c in candles]
    return [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]


def _calc_beta(symbol_returns: list[float], btc_returns: list[float]) -> Optional[float]:
    """Бета монеты к BTC - covariance(символ, BTC) / variance(BTC) по
    дневным лог-доходностям, тот же расчёт, каким считают бету акции к
    индексу в любом учебнике по финансам, только "индекс" тут один -
    BTCUSDT (см. P3.7 в config.py про то, почему именно BTC, а не
    какой-то отдельный индекс рынка). Бета=1.0 - монета в среднем
    двигается как BTC, >1.0 - сильнее (высоко-бетовая альта), <1.0 -
    слабее/менее коррелированно, отрицательная - движется в среднем
    ПРОТИВ BTC (редко, но не невозможно).

    None, если точек меньше 5 (слишком мало для осмысленной оценки) или
    дисперсия BTC равна 0 (не может быть в норме, кроме дефектных
    данных - деление на 0)."""
    n = min(len(symbol_returns), len(btc_returns))
    if n < 5:
        return None
    sym, btc = symbol_returns[-n:], btc_returns[-n:]
    mean_sym, mean_btc = statistics.mean(sym), statistics.mean(btc)
    covariance = sum((s - mean_sym) * (b - mean_btc) for s, b in zip(sym, btc)) / n
    variance_btc = sum((b - mean_btc) ** 2 for b in btc) / n
    if variance_btc == 0:
        return None
    return covariance / variance_btc


def _get_symbol_beta(symbol: str) -> Optional[float]:
    """Бета символа к BTC с персистентным TTL-кэшем (см.
    queue_manager.get/set_cached_symbol_beta, config.
    SYMBOL_BETA_CACHE_TTL_HOURS) - бета не пересчитывается на КАЖДЫЙ
    вызов check_new_position_allowed (это происходит перед КАЖДОЙ
    попыткой открыть позицию - лишний сетевой запрос на каждую сделку
    ради числа, которое не меняется от часа к часу).

    BTCUSDT сам к себе - тривиально 1.0, без похода в кэш/сеть.

    None при сбое расчёта (недостаточно данных и т.п. - см. _calc_beta)
    - вызывающий код (_beta_weighted_exposure) сам решает, как мягко
    откатиться (см. её докстринг), эта функция не подставляет дефолт
    сама, чтобы не путать «не смогли посчитать» с «посчитали и
    получили 1.0»."""
    if symbol.upper() == "BTCUSDT":
        return 1.0

    cached = queue_manager.get_cached_symbol_beta(symbol)
    if cached is not None:
        beta, computed_at = cached
        if (time.time() - computed_at) < config.SYMBOL_BETA_CACHE_TTL_HOURS * 3600:
            return beta

    symbol_returns = _fetch_daily_log_returns(symbol, config.SYMBOL_BETA_LOOKBACK_DAYS)
    btc_returns = _fetch_daily_log_returns("BTCUSDT", config.SYMBOL_BETA_LOOKBACK_DAYS)
    beta = _calc_beta(symbol_returns, btc_returns)
    if beta is not None:
        queue_manager.set_cached_symbol_beta(symbol, beta)
    return beta


def _beta_weighted_exposure(open_positions: list, side: str) -> float:
    """P3.7 - сумма БЕТА к BTC всех уже открытых позиций в ТУ ЖЕ
    сторону, что и side (см. модульный докстринг risk_guard.py, пункт 2,
    и config.BINANCE_FUTURES_MAX_BETA_EXPOSURE). Как _same_direction_
    open_count, но каждая позиция "весит" не 1, а её бету - две
    высоко-бетовые альты (бета~1.5 каждая) весят в лимите как 3.0,
    условная низко-бетовая монета (бета~0.3) - почти не весит.

    Мягкий откат на бету=1.0 (не 0!), если посчитать не удалось (см.
    _get_symbol_beta) - позиция с НЕИЗВЕСТНОЙ бетой учитывается как
    "средняя" (полный вес), а не как "не считается вообще" - иначе
    сетевой сбой при расчёте беты стал бы дырой в лимите риска, а не
    безопасным откатом."""
    is_long_side = side == "BUY"
    total = 0.0
    for p in open_positions:
        if (float(p.get("positionAmt", 0)) > 0) != is_long_side:
            continue
        beta = _get_symbol_beta(p.get("symbol", ""))
        total += beta if beta is not None else 1.0
    return total


def check_new_position_allowed(
    client, limits: RiskLimits, side: Optional[str] = None, symbol: Optional[str] = None,
) -> Optional[str]:
    """None - можно открывать новую позицию. Иначе - строка с причиной
    отказа. Намеренно НЕ бросает исключение сама - futures_executor
    оборачивает результат в ExecutionError на своей стороне,
    единообразно с остальными отказами до входа (недостаточный баланс
    и т.п.).

    side - "BUY"/"SELL" направление НОВОЙ сделки, нужен для лимита A4
    (max_same_direction_positions, см. RiskLimits) и P3.7
    (max_beta_exposure). symbol - тикер НОВОЙ сделки, нужен ТОЛЬКО для
    P3.7 (чтобы посчитать её бету к BTC). Если не переданы (None,
    дефолт для обратной совместимости со старыми вызывающими местами/
    тестами) - соответствующие проверки просто пропускаются, как будто
    лимита нет, а не отказывают вслепую."""
    kill_switch = queue_manager.get_kill_switch()
    if kill_switch is not None:
        auto_reset_hours = limits.kill_switch_auto_reset_hours
        elapsed_hours = (time.time() - kill_switch["tripped_at"]) / 3600
        if auto_reset_hours and elapsed_hours >= auto_reset_hours:
            logger.warning(
                "risk_guard: kill switch снят АВТОМАТИЧЕСКИ по таймауту (взведён %.1fч назад, "
                "порог %.1fч) - причина была: %s. Прежняя серия убытков больше не учитывается, "
                "отсчёт начинается заново.",
                elapsed_hours, auto_reset_hours, kill_switch["reason"],
            )
            queue_manager.clear_kill_switch()
            kill_switch = None

    if kill_switch is not None:
        return (
            f"KILL SWITCH ВЗВЕДЁН ({kill_switch['reason']}) - новые позиции заблокированы, "
            "пока кто-то осознанно не снимет его (python3 risk_guard_cli.py reset)."
        )

    open_positions = client.get_all_positions()
    if len(open_positions) >= limits.max_open_positions:
        symbols = ", ".join(p.get("symbol", "?") for p in open_positions)
        return (
            f"уже открыто {len(open_positions)}/{limits.max_open_positions} позиций ({symbols}) - "
            "новая позиция не откроется, пока одна из текущих не закроется"
        )

    if side is not None and limits.max_same_direction_positions is not None:
        same_dir = _same_direction_open_count(open_positions, side)
        if same_dir >= limits.max_same_direction_positions:
            direction_label = "лонг" if side == "BUY" else "шорт"
            return (
                f"уже открыто {same_dir}/{limits.max_same_direction_positions} позиций в сторону "
                f"{direction_label} - лимит на коррелированные позиции (не набирать несколько "
                "разных монет одной большой ставкой в одну сторону), новая позиция в ту же "
                "сторону не откроется, пока одна из текущих не закроется"
            )

    if side is not None and symbol is not None and limits.max_beta_exposure is not None:
        current_exposure = _beta_weighted_exposure(open_positions, side)
        new_symbol_beta = _get_symbol_beta(symbol)
        prospective_exposure = current_exposure + (new_symbol_beta if new_symbol_beta is not None else 1.0)
        if prospective_exposure > limits.max_beta_exposure:
            direction_label = "лонг" if side == "BUY" else "шорт"
            return (
                f"бета-экспозиция в сторону {direction_label} выросла бы до {prospective_exposure:.2f} "
                f"(лимит {limits.max_beta_exposure:.2f}) - слишком много уже открытых позиций с "
                "высокой корреляцией к BTC в эту сторону (см. P3.7), новая позиция не откроется, "
                "пока одна из текущих не закроется"
            )

    loss_pct, baseline, current = _daily_loss_pct(client)
    if loss_pct >= limits.max_daily_loss_pct:
        reason = (
            f"дневной убыток {loss_pct:.2f}% >= лимита {limits.max_daily_loss_pct:.2f}% "
            f"(baseline {baseline:.2f} -> сейчас {current:.2f})"
        )
        queue_manager.set_kill_switch(reason)
        logger.error("risk_guard: KILL SWITCH ВЗВЕДЁН (дневной лимит убытка): %s", reason)
        return f"KILL SWITCH ВЗВЕДЁН ({reason}) - новые позиции заблокированы, пока кто-то осознанно не снимет его."

    since_ts = queue_manager.get_risk_streak_ignore_before()
    streak = _consecutive_losses(client, lookback=max(limits.max_consecutive_losses * 5, 20), since_ts=since_ts)
    if streak >= limits.max_consecutive_losses:
        reason = f"{streak} убыточных сделок подряд (лимит {limits.max_consecutive_losses})"
        queue_manager.set_kill_switch(reason)
        logger.error("risk_guard: KILL SWITCH ВЗВЕДЁН (серия убытков подряд): %s", reason)
        return f"KILL SWITCH ВЗВЕДЁН ({reason}) - новые позиции заблокированы, пока кто-то осознанно не снимет его."

    return None


def status(client, limits: RiskLimits) -> dict:
    """Снимок текущего состояния для risk_guard_cli.py status /
    диагностики. В отличие от check_new_position_allowed, сама НИКОГДА
    не взводит и не снимает kill switch (даже по таймауту автосброса -
    это read-only снимок, а не проверка перед сделкой) - только
    сообщает о текущем состоянии, включая сколько осталось до
    автосброса, если он настроен и switch взведён. Побочный эффект:
    если сегодня ещё не было ни одной проверки, зафиксирует дневной
    baseline (та же логика, что и при обычной проверке - baseline
    должен быть один и тот же, откуда бы его ни зафиксировали первым)."""
    kill_switch = queue_manager.get_kill_switch()
    open_positions = client.get_all_positions()
    long_count = sum(1 for p in open_positions if float(p.get("positionAmt", 0)) > 0)
    short_count = len(open_positions) - long_count
    loss_pct, baseline, current = _daily_loss_pct(client)
    since_ts = queue_manager.get_risk_streak_ignore_before()
    streak = _consecutive_losses(client, lookback=max(limits.max_consecutive_losses * 5, 20), since_ts=since_ts)

    auto_reset_eta_hours = None
    if kill_switch is not None and limits.kill_switch_auto_reset_hours:
        elapsed_hours = (time.time() - kill_switch["tripped_at"]) / 3600
        auto_reset_eta_hours = round(max(limits.kill_switch_auto_reset_hours - elapsed_hours, 0), 2)

    return {
        "kill_switch": kill_switch,
        "kill_switch_auto_reset_hours": limits.kill_switch_auto_reset_hours,
        "kill_switch_auto_reset_eta_hours": auto_reset_eta_hours,
        "open_positions": len(open_positions),
        "open_positions_symbols": [p.get("symbol") for p in open_positions],
        "open_positions_long": long_count,
        "open_positions_short": short_count,
        "max_open_positions": limits.max_open_positions,
        "max_same_direction_positions": limits.max_same_direction_positions,
        "daily_loss_pct": round(loss_pct, 3),
        "daily_baseline": baseline,
        "daily_current": current,
        "max_daily_loss_pct": limits.max_daily_loss_pct,
        "consecutive_losses": streak,
        "max_consecutive_losses": limits.max_consecutive_losses,
    }