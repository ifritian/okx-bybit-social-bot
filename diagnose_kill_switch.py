#!/usr/bin/env python3
"""
diagnose_kill_switch.py - разовый диагностический скрипт: печатает
последние сделки с реального testnet-аккаунта (income history,
incomeType=REALIZED_PNL) в хронологическом порядке, с датой/символом/
знаком - чтобы увидеть, что ИМЕННО стоит за "20 убыточных сделок подряд"
в risk_kill_switch, прежде чем снимать его (см. risk_guard.py и
risk_guard_cli.py). НИЧЕГО не меняет - только читает.

Положить в корень Binance_Bot_repo и запустить:
    python3 diagnose_kill_switch.py
"""
import config
from futures_client import client_from_config, FuturesApiError

client = client_from_config(config)
account = "TESTNET" if client.is_testnet else "MAINNET"
print(f"=== История сделок ({account}) ===\n")

try:
    rows = client.get_income_history(income_type="REALIZED_PNL", limit=100)
except FuturesApiError as e:
    print(f"Ошибка API: {e}")
    raise SystemExit(1)

trades = [r for r in rows if float(r.get("income", 0)) != 0]
trades.sort(key=lambda r: int(r.get("time", 0)))

if not trades:
    print("Реализованных сделок в истории нет.")
else:
    streak = 0
    for r in trades:
        import datetime
        ts = datetime.datetime.fromtimestamp(int(r["time"]) / 1000, tz=datetime.timezone.utc)
        pnl = float(r["income"])
        mark = "УБЫТОК" if pnl < 0 else "ПРОФИТ"
        streak = streak + 1 if pnl < 0 else 0
        print(f"{ts:%Y-%m-%d %H:%M:%S UTC}  {r.get('symbol', '?'):12s}  {pnl:+10.4f} USDT  {mark}"
              f"{f'  (серия убытков: {streak})' if pnl < 0 else ''}")

    losses_at_end = 0
    for r in reversed(trades):
        if float(r["income"]) < 0:
            losses_at_end += 1
        else:
            break
    print(f"\nВсего сделок в истории: {len(trades)}")
    print(f"Текущая серия убытков подряд (с конца): {losses_at_end}")

print("\n--- Открытые позиции сейчас ---")
try:
    positions = client.get_all_positions()
    if not positions:
        print("Нет открытых позиций.")
    for p in positions:
        print(f"  {p.get('symbol')}: {p.get('positionAmt')} (entry {p.get('entryPrice')})")
except FuturesApiError as e:
    print(f"Ошибка API: {e}")