#!/usr/bin/env python3
"""
debug_env_encoding.py - разовый диагностический скрипт: проверяет
BINANCE_FUTURES_API_KEY/SECRET (и заодно BINANCE_SPOT_API_KEY/SECRET,
если заданы) на "плохие" символы - те, что не кодируются в latin-1 и
роняют HTTP-запрос с UnicodeEncodeError (см. диагностику kill switch).

НИЧЕГО не печатает из самих значений ключей/секретов - только длину,
наличие пробелов по краям и позиции/коды подозрительных символов.
Безопасно прислать весь вывод в чат.

Запуск:
    python debug_env_encoding.py
"""
import config

NAMES = [
    "BINANCE_FUTURES_API_KEY", "BINANCE_FUTURES_API_SECRET",
    "BINANCE_SPOT_API_KEY", "BINANCE_SPOT_API_SECRET",
]

for name in NAMES:
    val = getattr(config, name, "") or ""
    if not val:
        print(f"{name}: НЕ ЗАДАН (пусто)")
        continue

    stripped = val.strip()
    bad_chars = [(i, hex(ord(c)), repr(c)) for i, c in enumerate(val) if ord(c) > 255]
    # latin-1 - это 0..255, но и в этом диапазоне есть "невидимые" или
    # нежелательные символы (например, неразрывный пробел \xa0) - тоже
    # покажем их отдельно, они не роняют latin-1-кодирование, но могут
    # означать, что в файл попало что-то не то при копипасте.
    suspicious_in_range = [(i, hex(ord(c)), repr(c)) for i, c in enumerate(val) if ord(c) < 32 or ord(c) == 0xA0]

    print(f"{name}:")
    print(f"  длина: {len(val)} (после strip(): {len(stripped)}) - {'ОК, без пробелов по краям' if len(val) == len(stripped) else 'ЕСТЬ пробелы/переносы по краям!'}")
    print(f"  символы вне latin-1 (код > 255): {bad_chars if bad_chars else 'нет'}")
    print(f"  подозрительные символы в пределах latin-1 (управляющие/неразрывный пробел): {suspicious_in_range if suspicious_in_range else 'нет'}")
    print()
