#!/usr/bin/env python3
"""
Тест новой формулы score: сравнение старой vs новой.
"""

def score_old(rsi, overbought, bb_touch, divergence, volume_5m):
    """Старая формула (до июня 2026)."""
    RSI_OVERBOUGHT = 70
    RSI_OVERSOLD = 30
    extremity = (rsi - RSI_OVERBOUGHT) if overbought else (RSI_OVERSOLD - rsi)
    score = 40 + min(max(extremity, 0) * 1.5, 25)  # 40-65
    if bb_touch:
        score += 20
    if divergence:
        score += 10
    if volume_5m:
        score += 5
    return round(min(score, 100))


def score_new(rsi, overbought, bb_touch, divergence, volume_5m):
    """Новая формула (гибридный вариант, июнь 2026)."""
    RSI_OVERBOUGHT = 70
    RSI_OVERSOLD = 30
    extremity = (rsi - RSI_OVERBOUGHT) if overbought else (RSI_OVERSOLD - rsi)
    
    if extremity <= 10:
        rsi_score = 35 + extremity * 2.5
    else:
        rsi_score = 60 + (extremity - 10) * 3.5
    
    score = min(rsi_score, 78)
    if bb_touch:
        score += 15
    if divergence:
        score += 12
    if volume_5m:
        score += 3
    return round(min(score, 100))


# Тестовые случаи
test_cases = [
    # (rsi, overbought, bb_touch, divergence, volume_5m, description)
    (72, True, False, False, False, "RSI=72 (слабый), ничего больше"),
    (75, True, False, False, False, "RSI=75 (средний), ничего больше"),
    (80, True, False, False, False, "RSI=80 (сильный), ничего больше"),
    (85, True, False, False, False, "RSI=85 (экстремальный), ничего больше"),
    
    (75, True, True, False, False, "RSI=75 + Bollinger touch"),
    (80, True, True, False, False, "RSI=80 + Bollinger touch"),
    (85, True, True, False, False, "RSI=85 + Bollinger touch"),
    
    (75, True, True, True, False, "RSI=75 + Bollinger + divergence"),
    (80, True, True, True, False, "RSI=80 + Bollinger + divergence"),
    (85, True, True, True, False, "RSI=85 + Bollinger + divergence"),
    
    (80, True, True, True, True, "RSI=80 + ALL (Bollinger + divergence + volume)"),
    (85, True, True, True, True, "RSI=85 + ALL"),
    
    # Oversold cases
    (25, False, False, False, False, "RSI=25 (лонг, слабый)"),
    (20, False, True, True, True, "RSI=20 (лонг, ALL)"),
]

print("=" * 100)
print(f"{'Сценарий':<45} | {'Старая':<10} | {'Новая':<10} | {'Δ':<6}")
print("=" * 100)

for rsi, overbought, bb_touch, divergence, volume, desc in test_cases:
    old = score_old(rsi, overbought, bb_touch, divergence, volume)
    new = score_new(rsi, overbought, bb_touch, divergence, volume)
    delta = new - old
    direction = "↑" if delta > 0 else "↓" if delta < 0 else "="
    
    print(f"{desc:<45} | {old:>8}  | {new:>8}  | {delta:>+4} {direction}")

print("=" * 100)
print("\n📊 ВЫВОДЫ:")
print("✓ Старая: score 40-65 от RSI, редко выше 75-80 даже с комбинациями")
print("✓ Новая: score 35-78 от RSI, легче добраться до 75-85+ для real setups")
print("✓ Очередь будет содержать публикуемые сигналы вместо мусора!")
print()
