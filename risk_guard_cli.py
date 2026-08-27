#!/usr/bin/env python3
"""
risk_guard_cli.py - ручное управление предохранителями риска (см.
risk_guard.py): посмотреть текущее состояние (открытые позиции, дневной
убыток, серия убытков подряд, взведён ли kill switch) и, если нужно,
осознанно СНЯТЬ уже взведённый kill switch.

НИКОГДА не открывает/не закрывает позиции сам - только читает состояние
(status) и снимает один флаг в bot_state.db (reset, с подтверждением).

Использует те же переменные окружения, что и futures_testnet_demo.py/
main.py (BINANCE_FUTURES_USE_TESTNET и т.п., см. config.py) - т.е. по
умолчанию смотрит на TESTNET. Если нужно посмотреть/снять предохранители
для реального счёта, переменные окружения должны быть настроены
соответственно - как и для остального кода, это требует ОСОЗНАННОГО
BINANCE_FUTURES_USE_TESTNET=false, не происходит само по себе.

Использование:
    python3 risk_guard_cli.py status
    python3 risk_guard_cli.py reset
"""
import argparse
import logging
import sys

import config
import queue_manager
import risk_guard
from futures_client import FuturesApiError, client_from_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("risk_guard_cli")


def _print_status() -> int:
    client = client_from_config(config)
    limits = risk_guard.limits_from_config(config)
    try:
        s = risk_guard.status(client, limits)
    except FuturesApiError as e:
        logger.error("Не удалось получить статус с биржи: %s", e)
        return 1

    account = "TESTNET" if client.is_testnet else "MAINNET (реальные средства!)"
    print(f"=== Предохранители риска ({account}) ===")

    if s["kill_switch"] is not None:
        print(f"\n!!! KILL SWITCH ВЗВЕДЁН: {s['kill_switch']['reason']}")
        if s["kill_switch_auto_reset_eta_hours"] is not None:
            print(f"    Автоснятие через ~{s['kill_switch_auto_reset_eta_hours']:.1f}ч "
                  f"(настроено: {s['kill_switch_auto_reset_hours']:.1f}ч после срабатывания)")
        print("    Новые позиции ЗАБЛОКИРОВАНЫ. Снять вручную сейчас: python3 risk_guard_cli.py reset")
    else:
        print("\nKill switch: не взведён (торговля разрешена)")

    symbols = f" ({', '.join(s['open_positions_symbols'])})" if s["open_positions_symbols"] else ""
    print(f"\nОткрытых позиций: {s['open_positions']}/{s['max_open_positions']}{symbols}")
    if s["max_same_direction_positions"] is not None:
        print(f"  из них лонг: {s['open_positions_long']}, шорт: {s['open_positions_short']} "
              f"(лимит на одну сторону: {s['max_same_direction_positions']})")
    print(f"Дневной убыток: {s['daily_loss_pct']:+.2f}% (лимит {s['max_daily_loss_pct']:.2f}%), "
          f"baseline={s['daily_baseline']:.2f} -> сейчас={s['daily_current']:.2f}")
    print(f"Убытков подряд: {s['consecutive_losses']} (лимит {s['max_consecutive_losses']})")
    return 0


def _reset() -> int:
    existing = queue_manager.get_kill_switch()
    if existing is None:
        print("Kill switch не взведён - снимать нечего.")
        return 0

    print(f"Текущая причина: {existing['reason']}")
    confirm = input("Точно снять kill switch и разрешить новые позиции? [yes/N]: ").strip().lower()
    if confirm != "yes":
        print("Отменено.")
        return 0

    queue_manager.clear_kill_switch()
    print("Kill switch снят. Новые позиции снова разрешены (в пределах остальных лимитов).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "reset"])
    args = parser.parse_args()
    return _print_status() if args.command == "status" else _reset()


if __name__ == "__main__":
    sys.exit(main())