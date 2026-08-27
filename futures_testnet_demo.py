#!/usr/bin/env python3
"""
futures_testnet_demo.py - ручной CLI-скрипт для проверки полного цикла
(вход + стоп-лосс + тейк-профит) на Binance Futures TESTNET.

НИКОГДА не работает с реальным счётом - жёстко использует
futures_client.TESTNET_BASE_URL напрямую (не через config.py), чтобы
опечатка/забытая переменная окружения не могла случайно отправить этот
ручной тестовый скрипт на реальные деньги.

Как получить testnet-ключи - см. docstring futures_client.py.

Использование:
    export BINANCE_FUTURES_API_KEY=...      # testnet-ключ
    export BINANCE_FUTURES_API_SECRET=...   # testnet-секрет
    python3 futures_testnet_demo.py BTCUSDT BUY --risk-pct 1 --leverage 3 \\
        --stop-pct 2 --target-pct 4

--stop-pct/--target-pct - расстояние до стопа/тейка в % от текущей
рыночной цены (для лонга: стоп ниже, тейк выше; для шорта - наоборот) -
удобный способ проверить цикл, не вычисляя абсолютные цены вручную.
"""
import argparse
import logging
import os
import sys

import config  # ТОЛЬКО ради числовых лимитов risk_guard (max позиций/
                # дневной убыток/серия подряд) - см. ниже. Клиент по-прежнему
                # жёстко смотрит на TESTNET_BASE_URL, config сюда для base_url
                # не используется - гарантия "этот скрипт никогда не попадёт
                # на реальный счёт" (см. docstring модуля) не затронута.
import risk_guard
from futures_client import FuturesClient, TESTNET_BASE_URL, FuturesApiError
from futures_executor import open_protected_position, ExecutionError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("futures_testnet_demo")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("symbol", help="Например BTCUSDT")
    parser.add_argument("side", choices=["BUY", "SELL"], help="BUY - лонг, SELL - шорт")
    parser.add_argument("--risk-pct", type=float, default=1.0, help="%% баланса, которым рискуем (по умолчанию 1%%)")
    parser.add_argument("--leverage", type=int, default=3, help="Плечо (по умолчанию 3x)")
    parser.add_argument("--stop-pct", type=float, required=True, help="Расстояние до стопа в %% от текущей цены")
    parser.add_argument("--target-pct", type=float, required=True, help="Расстояние до тейка в %% от текущей цены")
    args = parser.parse_args()

    api_key = os.environ.get("BINANCE_FUTURES_API_KEY", "")
    api_secret = os.environ.get("BINANCE_FUTURES_API_SECRET", "")
    if not api_key or not api_secret:
        logger.error(
            "Не заданы BINANCE_FUTURES_API_KEY/BINANCE_FUTURES_API_SECRET (testnet-ключи, "
            "см. https://testnet.binancefuture.com) - выставь через export, не хардкодь в файл."
        )
        return 1

    # Жёстко TESTNET_BASE_URL, а не client_from_config(config) - см.
    # docstring модуля: этот скрипт никогда не должен уметь попасть на
    # реальный счёт, даже если кто-то неправильно настроит config.py.
    client = FuturesClient(api_key=api_key, api_secret=api_secret, base_url=TESTNET_BASE_URL)

    try:
        mark_price = client.get_mark_price(args.symbol)
    except FuturesApiError as e:
        logger.error("Не удалось получить цену %s: %s", args.symbol, e)
        return 1

    if args.side == "BUY":
        stop_price = mark_price * (1 - args.stop_pct / 100)
        target_price = mark_price * (1 + args.target_pct / 100)
    else:
        stop_price = mark_price * (1 + args.stop_pct / 100)
        target_price = mark_price * (1 - args.target_pct / 100)

    logger.info(
        "Testnet: %s %s по цене ~%.6g, стоп %.6g (-%.1f%%/+%.1f%%), тейк %.6g, риск %.1f%%, плечо %dx",
        args.symbol, args.side, mark_price, stop_price, args.stop_pct, args.stop_pct, target_price,
        args.risk_pct, args.leverage,
    )
    confirm = input("Подтвердить открытие ЭТОЙ testnet-позиции? [yes/N]: ").strip().lower()
    if confirm != "yes":
        logger.info("Отменено пользователем.")
        return 0

    try:
        result = open_protected_position(
            client, args.symbol, args.side,
            stop_price=stop_price, take_profit_price=target_price,
            risk_pct=args.risk_pct, leverage=args.leverage,
            risk_limits=risk_guard.limits_from_config(config),
        )
    except (ExecutionError, FuturesApiError) as e:
        logger.error("Не удалось открыть защищённую позицию: %s", e)
        logger.error("Проверить состояние предохранителей: python3 risk_guard_cli.py status")
        return 1

    logger.info(
        "Готово: %s %s qty=%.8g, вход~%.6g, стоп=%.6g, тейк=%.6g (риск ~%.2f USDT)",
        result.symbol, result.side, result.quantity, result.entry_price,
        result.stop_price, result.take_profit_price, result.risk_amount,
    )
    logger.info("Проверь позицию и открытые ордера на https://testnet.binancefuture.com")
    return 0


if __name__ == "__main__":
    sys.exit(main())
