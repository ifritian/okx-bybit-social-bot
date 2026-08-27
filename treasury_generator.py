"""
Генератор поста "Treasury Index" - публикуется раз в TREASURY_INTERVAL_HOURS
(конфиг), независимо от currency/opinion/article.

Сам индекс (состав корзины, веса, расчёт %, защита от выбросов) считается
ПОЛНОСТЬЮ кодом - см. treasury_index.py. LLM здесь получает уже готовый
числовой блок как контекст и пишет только короткий хук ПЕРЕД ним - не
переписывает и не придумывает цифры внутри блока, ровно та же идея, что
в opinion_generator/article_generator.
"""
import logging
import re
from datetime import datetime
from typing import Optional

import config
import cliche_filter
from groq_client import call_groq
import index_health_monitor
import index_volatility
import post_format
import queue_manager
import treasury_chart
import treasury_composition_chart
import treasury_heatmap
import voice_guidelines
from treasury_index import (
    TreasuryIndexResult, compute_breadth, compute_index, fetch_market_benchmark_pct,
    fetch_reference_change_pct, format_breadth_line, format_index_block, leading_tier,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """Ты пишешь короткий хук (вводную фразу) для поста
Treasury Index - собственного инфраструктурного крипто-индекса канала
(L1/L2/DeFi монеты без BTC/ETH/BNB, разбитые на три уровня риска:
Фундамент/Рост/Риск). Хук идёт ПЕРЕД готовым числовым блоком индекса -
сам блок тебе показан только для контекста, повторять его в ответе не
нужно, он будет добавлен отдельно после твоего текста.

1-3 предложения, живой разговорный стиль, без канцелярита и без
воды. Можно лёгкую рефлексию о том, что означает движение - например,
какой тир лидирует и что это может говорить о риск-аппетите рынка
(если Риск обгоняет Фундамент - аппетит к риску растёт, и наоборот),
а также о том, обогнал ли индекс BTC за период и как дела с момента
запуска (эти цифры тебе даны отдельно) - но без выдумывания новостей,
которых тебе не давали.

Тебе дан набор реальных чисел (см. задание) - если используешь цифры в
хуке, то ТОЧНО как даны, не округляя и не придумывая других. Можно
вообще не называть цифры в хуке (они и так есть в блоке ниже) - просто
качественная рефлексия тоже подходит.

ЖЁСТКО ЗАПРЕЩЕНО называть конкретную цену в долларах ($113, $1.47 и
т.п.) любой монеты индекса - тебе даны ТОЛЬКО проценты изменения, а не
абсолютные цены, поэтому любая названная тобой цена в долларах будет
выдумкой.

НЕ добавляй сам дисклеймер и НЕ дублируй числовой блок. Отвечай только
текстом хука, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def _extract_numbers(text: str) -> set[float]:
    return {round(float(n), 2) for n in re.findall(r"[+-]?\d+\.?\d*", text.replace(",", ""))}


def _sign(pct: float) -> str:
    return "+" if pct >= 0 else ""


def _format_comparison_block(
    result: TreasuryIndexResult, period_hours: float,
    btc_pct: Optional[float], eth_pct: Optional[float], market_pct: Optional[float],
    history: Optional[dict],
) -> str:
    """Собирает (кодом, не LLM) блок-"крючок" для шеринга:
    - расхождение с BTC за тот же период - самая "цитируемая" метрика
      поста (интереснее голого числа индекса самого по себе), подана
      развёрнуто ("обогнал/отстал на X п.п.");
    - компактная вторая строка с ETH и равновзвешенным топ-4 рынка
      (treasury_index.fetch_market_benchmark_pct) - более честный
      ориентир, чем один BTC, но без повторения той же развёрнутой
      формулировки трижды подряд;
    - кумулятивный трекер с момента запуска (индекс/BTC/ETH/рынок,
      база 100) - придаёт постам ощущение "истории", за которой можно
      следить неделями, а не разового снимка.
    Возвращает пустую строку, если вообще ничего из этого посчитать не
    удалось (все внешние тикеры недоступны) - остальной пост
    публикуется как обычно."""
    lines = []

    if btc_pct is not None:
        diff = round(result.total_pct - btc_pct, 2)
        if diff > 0:
            verb = f"обогнал BTC на {diff} п.п."
        elif diff < 0:
            verb = f"отстал от BTC на {abs(diff)} п.п."
        else:
            verb = "наравне с BTC"
        lines.append(
            f"🆚 За {period_hours:g}ч: Индекс {_sign(result.total_pct)}{result.total_pct}% "
            f"vs BTC {_sign(btc_pct)}{btc_pct}% - {verb}"
        )

    extra_bits = []
    if eth_pct is not None:
        extra_bits.append(f"ETH {_sign(eth_pct)}{eth_pct}%")
    if market_pct is not None:
        extra_bits.append(f"топ-4 рынка {_sign(market_pct)}{market_pct}%")
    if extra_bits:
        lines.append(f"📐 Для сравнения: {', '.join(extra_bits)}")

    if history is not None:
        launch_date = datetime.fromtimestamp(history["launch_at"]).strftime("%d.%m.%Y")
        idx_cum = round(history["index_value"] - 100, 2)
        btc_cum = round(history["btc_value"] - 100, 2)
        since_parts = [f"Индекс {_sign(idx_cum)}{idx_cum}%", f"BTC {_sign(btc_cum)}{btc_cum}%"]

        if "eth_value" in history:
            eth_cum = round(history["eth_value"] - 100, 2)
            since_parts.append(f"ETH {_sign(eth_cum)}{eth_cum}%")
        if "market_value" in history:
            market_cum = round(history["market_value"] - 100, 2)
            since_parts.append(f"Рынок {_sign(market_cum)}{market_cum}%")

        lines.append(f"📈 С запуска ({launch_date}): {' | '.join(since_parts)}")

    return "\n".join(lines)


def generate_treasury_post(period_hours: float = 12.0) -> Optional[tuple]:
    """Возвращает (текст для Binance Square, текст для кросспоста в
    Telegram, TreasuryIndexResult, chart_path, heatmap_path,
    composition_path), либо None, если индекс не удалось посчитать
    вообще (ни один тир не собрался - например, полностью недоступен
    data-api.binance.vision).

    - chart_path - equity curve "с запуска" (treasury_chart.py), либо
      None, если снимков ещё недостаточно или BTC недоступен для
      сравнения (без него не с чем сверять кумулятив).
    - heatmap_path - тепловая карта по всем 15 монетам (treasury_heatmap.py) -
      генерируется КАЖДЫЙ раз, когда есть хотя бы один тир.
    - composition_path - диаграмма состава корзины (treasury_composition_chart.py) -
      генерируется РЕДКО, раз в config.TREASURY_COMPOSITION_INTERVAL_POSTS
      постов (состав статичен между ребалансировками).

    Любая из трёх картинок может быть None (недостаточно данных или
    ошибка построения) - публикация текста при этом не блокируется,
    см. main.try_publish_treasury_post.

    Сейчас оба текста идентичны - ссылка на Telegram-канал в пост для
    Binance Square НЕ добавляется (площадка блокирует такие посты
    модерацией, см. комментарий внутри функции), возврат двух отдельных
    строк сохранён на будущее, если для одной из площадок понадобится
    другое форматирование.

    Поднимает groq_client.GroqRateLimited при 429 от Groq - вызывающий
    код (main.py) уже умеет это ловить и выставлять backoff, как для
    opinion/article."""
    result = compute_index(period_hours=period_hours)

    try:
        streaks = index_health_monitor.record_check_results(result.missing)
        index_health_monitor.check_and_alert(streaks)
    except Exception:
        logger.exception("Ошибка мониторинга здоровья монет индекса - не блокирую публикацию Treasury Index")

    try:
        queue_manager.append_coin_periods(result.tiers)
    except Exception:
        # История для rebalance_advisor - не критична для самого поста,
        # одна неудачная запись не должна ронять публикацию Treasury Index.
        logger.exception("Не удалось обновить per-coin историю для ребалансировки")

    if result.total_pct is None:
        logger.warning("Treasury Index: не удалось посчитать ни один тир - пропускаю публикацию")
        return None

    # Тепловая карта - КАЖДЫЙ пост (не зависит от BTC/сравнения - строится
    # из тех же result.tiers, что уже посчитаны выше).
    try:
        heatmap_path = treasury_heatmap.generate_treasury_heatmap(result)
    except Exception:
        logger.exception("Не удалось построить тепловую карту Treasury Index - публикую без неё")
        heatmap_path = None

    # Диаграмма состава - РЕДКО (раз в TREASURY_COMPOSITION_INTERVAL_POSTS
    # постов) - состав между ребалансировками не меняется, показывать
    # его каждый раз избыточно.
    composition_path = None
    post_count = queue_manager.increment_treasury_post_count()
    if post_count % config.TREASURY_COMPOSITION_INTERVAL_POSTS == 0:
        try:
            composition_path = treasury_composition_chart.generate_composition_chart()
        except Exception:
            logger.exception("Не удалось построить диаграмму состава Treasury Index - публикую без неё")

    index_block = format_index_block(result)
    allowed_numbers = _extract_numbers(index_block) | {period_hours}

    # Сравнение с BTC/ETH/рынком за тот же период + кумулятивный трекер
    # с запуска. Каждый бенчмарк опционален - если какой-то недоступен
    # (сетевой сбой), публикуем пост без него, не блокируем публикацию
    # из-за одного внешнего тикера.
    btc_pct = fetch_reference_change_pct("BTCUSDT", period_hours)
    eth_pct = fetch_reference_change_pct("ETHUSDT", period_hours)
    market_pct = fetch_market_benchmark_pct(period_hours)

    history = None
    chart_path = None
    if btc_pct is not None:
        history = queue_manager.update_treasury_history(result.total_pct, btc_pct, eth_pct, market_pct)
        snapshots = queue_manager.append_treasury_snapshot(history)
        try:
            chart_path = treasury_chart.generate_treasury_chart(snapshots)
        except Exception:
            # Картинка - бонус, не условие публикации: если построение
            # почему-то упало (испорченный снимок, проблема matplotlib
            # в среде выполнения и т.п.), публикуем пост текстом, как раньше.
            logger.exception("Не удалось построить график Treasury Index - публикую без картинки")
    else:
        logger.warning("Не удалось получить BTC для сравнения - публикую Treasury Index без блока сравнения")

    returns_history = queue_manager.append_treasury_return(result.total_pct)
    volatility_stats = index_volatility.compute_volatility_and_drawdown(returns_history)
    volatility_block = index_volatility.format_volatility_block(volatility_stats, period_hours)

    breadth = compute_breadth(result)
    breadth_line = format_breadth_line(breadth)

    comparison_block = _format_comparison_block(result, period_hours, btc_pct, eth_pct, market_pct, history)
    if comparison_block:
        allowed_numbers |= _extract_numbers(comparison_block)
    if volatility_block:
        allowed_numbers |= _extract_numbers(volatility_block)
        comparison_block = f"{comparison_block}\n{volatility_block}" if comparison_block else volatility_block
    if breadth_line:
        allowed_numbers |= _extract_numbers(breadth_line)
        comparison_block = f"{comparison_block}\n{breadth_line}" if comparison_block else breadth_line

    lt = leading_tier(result)
    lead_line = ""
    if lt is not None:
        sign = "+" if lt.pct >= 0 else ""
        lead_line = f"Лидирует тир: {lt.label} ({sign}{lt.pct}%).\n"

    total_sign = "+" if result.total_pct >= 0 else ""
    comparison_line = f"\n{comparison_block}" if comparison_block else ""
    user_prompt = (
        f"Итоговое изменение индекса за {period_hours:g}ч: {total_sign}{result.total_pct}%.\n"
        f"{lead_line}"
        f"{comparison_line}\n"
        f"Числовой блок целиком (для контекста, НЕ копируй его в ответ):\n{index_block}\n\n"
        f"Напиши короткий хук, который встанет перед этим блоком. Если расхождение "
        f"с BTC или результат с запуска заметные - можно на это указать (см. цифры выше)."
    )

    # GroqRateLimited намеренно НЕ ловится здесь - пробрасывается в main.py,
    # чтобы использовать общий backoff по Retry-After (как в article/opinion).
    hook = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=350, temperature=0.9)

    ok, reason = validate_treasury_hook(hook, allowed_numbers)
    if not ok:
        logger.warning("Хук Treasury Index не прошёл проверку (%s) - публикую с нейтральным хуком", reason)
        hook = "📊 Свежий срез Treasury Index:"

    text_parts = [hook.strip(), index_block]
    if comparison_block:
        text_parts.append(comparison_block)
    text_parts.append(post_format.DISCLAIMER)
    binance_text = "\n\n".join(text_parts)

    # Ссылку на Telegram-канал в пост для Binance Square НЕ добавляем -
    # площадка блокирует такие посты модерацией с причиной "promotes
    # third-party channels" (проверено на практике - пост так и не
    # опубликовался, завис в черновиках). post_format.telegram_channel_line()
    # оставлена в кодовой базе на случай, если понадобится для других
    # целей (например, для будущих постов ИСКЛЮЧИТЕЛЬНО в самом Telegram),
    # но сюда, в текст для Binance, сознательно не подключается.
    telegram_text = binance_text

    logger.info("Сгенерирован пост Treasury Index (%s%%, лидер %s): %s",
                total_sign + str(result.total_pct), lt.key if lt else "нет", binance_text[:150].replace("\n", " "))
    return binance_text, telegram_text, result, chart_path, heatmap_path, composition_path


_MIN_HOOK_CHARS = 10


def validate_treasury_hook(hook: str, allowed_numbers: set[float]) -> tuple[bool, str]:
    """Хук не должен содержать чисел, которых нет среди уже посчитанных
    (числового блока). Сам числовой блок в проверку не входит - он
    собран кодом и по определению корректен.

    Также хук не должен быть пустым/почти пустым (см. index_signal_generator
    - тот же баг: пустой хук формально не содержит "чужих" чисел и молча
    проходил бы проверку, оставляя пост без единой мысли автора)."""
    if len(hook.strip()) < _MIN_HOOK_CHARS:
        return False, f"Хук пустой или слишком короткий: {hook!r}"

    numbers = _extract_numbers(hook)
    unknown = [n for n in numbers if not any(abs(n - a) < 0.05 for a in allowed_numbers)]
    if unknown:
        return False, f"В хуке есть числа не из посчитанных данных: {unknown}"

    cliche_ok, found = cliche_filter.check_cliches(hook)
    if not cliche_ok:
        return False, f"В хуке есть шаблонные ИИ-фразы: {', '.join(found)}"

    return True, ""