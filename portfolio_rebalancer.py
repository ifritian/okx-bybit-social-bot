#!/usr/bin/env python3
"""
portfolio_rebalancer.py - доводит текущие спот-доли портфеля до целевых
весов Treasury Index (см. treasury_index.BASKET - те же 15 монет/веса,
что и в постах индекса) через рыночные спот-ордера. Третья из трёх
исходных целей проекта, см. "Этапы бот.doc" - независима от фьючерсной
части (futures_signal_bridge.py и т.п.), намеренно БЕЗ плеча и БЕЗ риска
ликвидации, поэтому и без risk_guard/kill switch - тут нечему взрываться
быстрее, чем на один тик.

КОГДА РЕАЛЬНО ТОРГУЕМ: не по расписанию, а "по надобности" - проверка
(analyze_portfolio) дешёвая (баланс + текущие цены, без ордеров) и может
гоняться на каждом тике. Ордера отправляются, только если максимальное
отклонение доли КАКОЙ-ЛИБО монеты корзины от целевого веса превышает
config.PORTFOLIO_REBALANCE_DRIFT_THRESHOLD_PCT (в процентных пунктах от
общей стоимости управляемого портфеля) - см. check_and_rebalance. Без
порога любой мелкий шум цены гонял бы ордера туда-сюда каждые 10 минут,
съедая всё комиссиями.

ЧТО СЧИТАЕТСЯ "УПРАВЛЯЕМЫМ ПОРТФЕЛЕМ": свободный USDT + все монеты
корзины Treasury Index, которые сейчас есть на споте. Другие активы на
аккаунте (если есть) полностью игнорируются - модуль их не видит и не
трогает.

ПРО НЕТОРГУЕМЫЕ НА TESTNET ПАРЫ: Binance Spot Testnet поддерживает
намного меньше пар, чем mainnet - большинство монет корзины (кроме
крупных L1) там могут просто отсутствовать. _resolve_symbol проверяет
каждую монету через реальный exchangeInfo ТОГО base_url, на который
смотрит client, и если пары нет (ни основной тикер, ни fallback) -
монета выбрасывается из расчёта, а её вес ПЕРЕНОРМИРУЕТСЯ между
оставшимися (тот же принцип, что и в treasury_index.py при отсутствии
данных по монете) - иначе на testnet эта функция была бы бесполезна
почти всегда.

ПОРЯДОК ИСПОЛНЕНИЯ: сначала все продажи (высвобождают USDT), потом все
покупки (тратят освободившийся + имевшийся свободный USDT) - чтобы не
пытаться купить на деньги, которых ещё физически нет на балансе.
Ордера меньше min_notional по символу молча пропускаются (с логом) -
пытаться докупить на 0.03 доллара бессмысленно и половина бирж такой
ордер просто отклонит.
"""
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import alerting
import config
import treasury_index
from spot_client import SpotApiError, SpotClient

logger = logging.getLogger("portfolio_rebalancer")

QUOTE_ASSET = "USDT"


@dataclass
class CoinPosition:
    ticker: str
    symbol: str                # реально резолвленный символ (может быть fallback)
    target_weight_pct: float   # ПЕРЕНОРМИРОВАННЫЙ вес (после выброса нерезолвленных монет)
    quantity: float
    price: float
    current_value: float
    target_value: float
    drift_pct: float           # (current_value - target_value) / total_value * 100
    step_size: float
    min_notional: float


@dataclass
class PortfolioAnalysis:
    total_value: float
    free_usdt: float
    positions: list[CoinPosition] = field(default_factory=list)
    skipped_tickers: list[str] = field(default_factory=list)  # монеты без торгуемой пары

    @property
    def max_drift_pct(self) -> float:
        if not self.positions:
            return 0.0
        return max(abs(p.drift_pct) for p in self.positions)


@dataclass
class PlannedOrder:
    symbol: str
    side: str          # "BUY" или "SELL"
    ticker: str
    # ровно одно из двух заполнено - см. spot_client.SpotClient.place_market_order
    quantity: Optional[float] = None
    quote_order_qty: Optional[float] = None
    reason_value_delta: float = 0.0  # для лога/уведомления - на сколько USDT меняем позицию


def _flatten_basket() -> list[dict]:
    """Плоский список всех монет корзины across тиров - веса в BASKET
    уже в шкале "% от всего портфеля" (см. assert в treasury_index.py:
    сумма весов внутри тира = TIER_WEIGHTS[tier], сумма тиров = 100)."""
    coins = []
    for tier_coins in treasury_index.BASKET.values():
        coins.extend(tier_coins)
    return coins


def _resolve_symbol(coin: dict, tradable_symbols: set[str]) -> Optional[str]:
    primary = f"{coin['ticker']}{QUOTE_ASSET}"
    if primary in tradable_symbols:
        return primary
    fallback = coin.get("fallback")
    if fallback:
        fb_symbol = f"{fallback}{QUOTE_ASSET}"
        if fb_symbol in tradable_symbols:
            logger.info("portfolio_rebalancer: %s недоступен, использую fallback %s", primary, fb_symbol)
            return fb_symbol
    return None


def analyze_portfolio(client: SpotClient) -> Optional[PortfolioAnalysis]:
    """Читает баланс + текущие цены, сравнивает с целевыми весами
    Treasury Index. НЕ отправляет ни одного ордера - чисто read-only,
    безопасно гонять на каждом тике. None, если ни одна монета корзины
    не резолвилась (например, совсем не та сеть/testnet без пар) -
    вызывающему коду тогда нечего анализировать."""
    coins = _flatten_basket()

    try:
        exchange_info = client.get_exchange_info()
        tradable = {
            s["symbol"] for s in exchange_info.get("symbols", []) if s.get("status") == "TRADING"
        }
        balances = client.get_balances()
    except SpotApiError as e:
        logger.error("portfolio_rebalancer: не удалось получить данные аккаунта: %s", e)
        return None

    resolved: list[tuple[dict, str]] = []
    skipped: list[str] = []
    for coin in coins:
        symbol = _resolve_symbol(coin, tradable)
        if symbol is None:
            skipped.append(coin["ticker"])
            logger.warning(
                "portfolio_rebalancer: %sUSDT (и fallback, если был) не торгуется на %s - "
                "монета исключена из ребалансировки на этот прогон, вес перенормирован между остальными",
                coin["ticker"], client.base_url,
            )
            continue
        resolved.append((coin, symbol))

    if not resolved:
        logger.error("portfolio_rebalancer: ни одна монета корзины не резолвилась в торгуемую пару - нечего анализировать")
        return None

    weight_sum = sum(coin["weight"] for coin, _ in resolved)

    free_usdt = balances.get(QUOTE_ASSET, 0.0)
    total_value = free_usdt

    priced: list[tuple[dict, str, float, float]] = []  # coin, symbol, qty, price
    for coin, symbol in resolved:
        base_asset = symbol[:-len(QUOTE_ASSET)]
        qty = balances.get(base_asset, 0.0)
        try:
            price = client.get_price(symbol)
        except SpotApiError as e:
            logger.warning("portfolio_rebalancer: не удалось получить цену %s: %s - монета пропущена в этом прогоне", symbol, e)
            continue
        priced.append((coin, symbol, qty, price))
        total_value += qty * price

    if total_value <= 0:
        logger.error("portfolio_rebalancer: суммарная стоимость управляемого портфеля равна нулю (нет USDT/монет корзины) - нечего ребалансировать")
        return None

    positions = []
    for coin, symbol, qty, price in priced:
        renorm_weight = coin["weight"] / weight_sum * 100
        current_value = qty * price
        target_value = renorm_weight / 100 * total_value
        drift_pct = (current_value - target_value) / total_value * 100
        try:
            filters = client.get_symbol_filters(symbol, exchange_info)
        except SpotApiError:
            filters = {"step_size": None, "min_notional": None}
        positions.append(CoinPosition(
            ticker=coin["ticker"], symbol=symbol, target_weight_pct=renorm_weight,
            quantity=qty, price=price, current_value=current_value, target_value=target_value,
            drift_pct=drift_pct, step_size=filters.get("step_size") or 0.0,
            min_notional=filters.get("min_notional") or 0.0,
        ))

    return PortfolioAnalysis(total_value=total_value, free_usdt=free_usdt, positions=positions, skipped_tickers=skipped)


def _round_down(value: float, step: float) -> float:
    if not step or step <= 0:
        return value
    return round(int(value / step) * step, 10)


def plan_orders(analysis: PortfolioAnalysis) -> list[PlannedOrder]:
    """Считает ордера (без отправки) - продажи для монет ВЫШЕ целевого
    веса, покупки для монет НИЖЕ. Позиции меньше min_notional по
    итоговой сумме сделки пропускаются - см. модульный docstring."""
    orders = []

    # Продажи первыми - высвобождают USDT под покупки ниже.
    for p in analysis.positions:
        excess_value = p.current_value - p.target_value
        if excess_value <= 0:
            continue
        qty = _round_down(excess_value / p.price, p.step_size)
        sell_value = qty * p.price
        if qty <= 0 or (p.min_notional and sell_value < p.min_notional):
            logger.info("portfolio_rebalancer: пропускаю продажу %s - сумма %.4f USDT меньше min_notional (%.4f) или округлилась в 0",
                        p.symbol, sell_value, p.min_notional)
            continue
        orders.append(PlannedOrder(symbol=p.symbol, side="SELL", ticker=p.ticker, quantity=qty, reason_value_delta=-sell_value))

    # Бюджет на покупки: то, что уже свободно + оценка выручки с продаж выше
    # (оценка, не факт - реальная сумма после исполнения может чуть отличаться
    # из-за проскальзывания между расчётом и исполнением, это ожидаемо и не
    # критично для ребалансировки, в отличие от фьючерсного риска).
    available_usdt = analysis.free_usdt + sum(-o.reason_value_delta for o in orders if o.side == "SELL")

    buy_candidates = [p for p in analysis.positions if p.target_value - p.current_value > 0]
    total_deficit = sum(p.target_value - p.current_value for p in buy_candidates)

    for p in buy_candidates:
        deficit = p.target_value - p.current_value
        # Если бюджета не хватает на все покупки целиком - распределяем
        # пропорционально дефициту каждой монеты, а не отдаём всё первой
        # по списку.
        budget_share = deficit if total_deficit <= available_usdt else deficit / total_deficit * available_usdt
        if budget_share <= 0 or (p.min_notional and budget_share < p.min_notional):
            if budget_share > 0:
                logger.info("portfolio_rebalancer: пропускаю покупку %s - сумма %.4f USDT меньше min_notional (%.4f)",
                            p.symbol, budget_share, p.min_notional)
            continue
        orders.append(PlannedOrder(symbol=p.symbol, side="BUY", ticker=p.ticker, quote_order_qty=round(budget_share, 2), reason_value_delta=budget_share))

    return orders


def _execute_orders(client: SpotClient, orders: list[PlannedOrder]) -> list[dict]:
    executed = []
    for order in orders:
        try:
            if order.side == "SELL":
                result = client.place_market_order(order.symbol, "SELL", quantity=order.quantity)
            else:
                result = client.place_market_order(order.symbol, "BUY", quote_order_qty=order.quote_order_qty)
            logger.info("portfolio_rebalancer: исполнено %s %s (%+.2f USDT)", order.side, order.symbol, order.reason_value_delta)
            executed.append({"order": order, "result": result, "error": None})
        except SpotApiError as e:
            logger.error("portfolio_rebalancer: не удалось исполнить %s %s: %s", order.side, order.symbol, e)
            executed.append({"order": order, "result": None, "error": str(e)})
    return executed


def _notify_owner(analysis: PortfolioAnalysis, executed: list[dict]) -> None:
    ok = [e for e in executed if e["error"] is None]
    failed = [e for e in executed if e["error"] is not None]
    lines = [
        f"\U0001F504 Ребалансировка портфеля (testnet, отклонение было {analysis.max_drift_pct:.2f}%)",
        f"Стоимость управляемого портфеля: {analysis.total_value:.2f} USDT",
        f"Исполнено ордеров: {len(ok)}" + (f", ошибок: {len(failed)}" if failed else ""),
    ]
    for e in ok:
        o = e["order"]
        amount = f"qty={o.quantity:.8g}" if o.quantity is not None else f"~{o.quote_order_qty:.2f} USDT"
        lines.append(f"  {o.side} {o.symbol} ({amount})")
    for e in failed:
        o = e["order"]
        lines.append(f"  ОШИБКА {o.side} {o.symbol}: {e['error']}")
    alerting.send_owner_alert(f"portfolio_rebalance:{int(time.time())}", "\n".join(lines), min_repeat_hours=0)


def check_and_rebalance(client: SpotClient, dry_run: bool = True) -> dict:
    """Точка входа: анализирует портфель, и если максимальное отклонение
    от целевых весов >= config.PORTFOLIO_REBALANCE_DRIFT_THRESHOLD_PCT -
    планирует и (если не dry_run) исполняет ордера + уведомляет
    владельца в Telegram. Если отклонение в пределах допуска - НИЧЕГО
    не делает и не шлёт уведомлений (иначе на каждом тике был бы шум)."""
    analysis = analyze_portfolio(client)
    if analysis is None:
        return {"checked": False}

    threshold = config.PORTFOLIO_REBALANCE_DRIFT_THRESHOLD_PCT
    needs_rebalance = analysis.max_drift_pct >= threshold

    summary = {
        "checked": True,
        "total_value_usdt": analysis.total_value,
        "max_drift_pct": analysis.max_drift_pct,
        "threshold_pct": threshold,
        "needs_rebalance": needs_rebalance,
        "skipped_tickers": analysis.skipped_tickers,
        "orders_planned": [],
        "orders_executed": [],
    }

    if not needs_rebalance:
        logger.info(
            "portfolio_rebalancer: максимальное отклонение %.2f%% < порога %.2f%% - ребалансировка не нужна",
            analysis.max_drift_pct, threshold,
        )
        return summary

    orders = plan_orders(analysis)
    summary["orders_planned"] = orders
    logger.info(
        "portfolio_rebalancer: отклонение %.2f%% >= порога %.2f%% - запланировано %d ордер(ов)",
        analysis.max_drift_pct, threshold, len(orders),
    )

    if not orders:
        logger.info("portfolio_rebalancer: все нужные сделки меньше min_notional - фактически менять нечего")
        return summary

    if dry_run:
        logger.info("portfolio_rebalancer: DRY-RUN - ни один ордер не отправлен (см. orders_planned)")
        for o in orders:
            amount = f"qty={o.quantity:.8g}" if o.quantity is not None else f"~{o.quote_order_qty:.2f} USDT"
            logger.info("  [dry-run] %s %s (%s)", o.side, o.symbol, amount)
        return summary

    summary["orders_executed"] = _execute_orders(client, orders)
    _notify_owner(analysis, summary["orders_executed"])
    return summary


def main() -> int:
    import argparse
    import os

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="Реально отправлять ордера на testnet. Без этого флага - dry-run: "
                             "показывает, что было бы сделано, ничего не отправляя.")
    args = parser.parse_args()

    api_key = os.environ.get("BINANCE_SPOT_API_KEY", "")
    api_secret = os.environ.get("BINANCE_SPOT_API_SECRET", "")
    if not api_key or not api_secret:
        logger.error(
            "Не заданы BINANCE_SPOT_API_KEY/BINANCE_SPOT_API_SECRET (testnet-ключи, "
            "см. https://testnet.binance.vision) - выставь через export, не хардкодь в файл."
        )
        return 1

    # Жёстко TESTNET, как и futures_auto_trade.py - см. модульный docstring.
    from spot_client import TESTNET_BASE_URL
    client = SpotClient(api_key=api_key, api_secret=api_secret, base_url=TESTNET_BASE_URL)

    summary = check_and_rebalance(client, dry_run=not args.live)
    if not summary["checked"]:
        return 1
    logger.info(
        "Готово: портфель %.2f USDT, макс. отклонение %.2f%% (порог %.2f%%), нужна ребалансировка: %s, ордеров исполнено: %d",
        summary["total_value_usdt"], summary["max_drift_pct"], summary["threshold_pct"],
        summary["needs_rebalance"], len(summary["orders_executed"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
