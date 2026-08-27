"""
voice_memory.py - "память о прошлых постах", чтобы LLM не сходилась на
одних и тех же зачинах и могла честно ссылаться на свои прошлые посты.
Адаптировано из voice_memory.py binance-square-bot - логика та же (см.
комментарии там для более подробного объяснения), здесь только замена
хранилища (state_store.py вместо queue_manager.py) и явный post_type
("okx_orbit" / "bybit_byx"), чтобы у каждой биржи была своя память -
совпадение зачина между OKX-постом и Bybit-постом не проблема (разные
площадки, разная аудитория), а вот совпадение двух постов подряд на
ОДНОЙ площадке - то, чего мы и хотим избежать.

Два независимых механизма:

1. АНТИ-ПОВТОР ЗАЧИНОВ - последние несколько зачинов (первое
   предложение поста) хранятся отдельно по каждой бирже, и при
   генерации нового хука LLM получает инструкцию не начинать так же.

2. ЧЕСТНАЯ ПРЕЕМСТВЕННОСТЬ ПО ТЕМЕ - если тема (BTC/ETH/...) уже
   обсуждалась раньше на этой же площадке, РЕАЛЬНЫЕ факты того раза
   (число, давность) передаются в промпт как "если уместно, можешь на
   это сослаться". Это не выдумка - continuity_block() передаёт только
   то, что мы сами сохранили после реальной доставки черновика.

Оба механизма - подсказки в промпте, не жёсткая валидация: если LLM
всё равно повторит похожий зачин, доставка черновика не отклоняется -
в отличие от чисел/дисклеймера/клише, это вопрос разнообразия, а не
корректности.
"""
import time
from typing import Optional

import state_store

_RECENT_OPENERS_MAX = 15
_RECENT_OPENERS_IN_PROMPT = 8  # последних достаточно для промпта - вся история туда не нужна
_CONTINUITY_MAX_AGE_DAYS = 14  # старше - отсылка потеряла бы актуальность, не показываем


def _first_sentence(text: str) -> str:
    """Берёт первую строку/предложение текста - то, чем пост
    "открывается" - для сравнения с прошлыми зачинами и как краткая
    сводка позиции для continuity_block()."""
    first_line = text.strip().split("\n")[0].strip()
    for sep in (". ", "! ", "? "):
        if sep in first_line:
            return first_line.split(sep)[0].strip() + sep.strip()
    return first_line[:80]


def record_post(post_type: str, text: str, theme: Optional[str] = None, pct: Optional[float] = None) -> None:
    """Вызывать ПОСЛЕ успешной доставки черновика в Telegram (не после
    генерации - незачем засорять память зачинами, которые не прошли
    валидацию или не были доставлены). Обновляет анти-повтор всегда;
    если переданы theme и pct - также обновляет честную историю по
    теме для continuity_block()."""
    opener = _first_sentence(text)

    openers = state_store.get_recent_openers(post_type)
    openers.append(opener)
    if len(openers) > _RECENT_OPENERS_MAX:
        openers = openers[-_RECENT_OPENERS_MAX:]
    state_store.set_recent_openers(post_type, openers)

    if theme is not None and pct is not None:
        history = state_store.get_theme_post_history(post_type)
        history[theme] = {"pct": pct, "stance_summary": opener, "timestamp": time.time()}
        state_store.set_theme_post_history(post_type, history)


def anti_repeat_block(post_type: str) -> str:
    """Блок для user_prompt с последними зачинами - пустая строка, если
    истории ещё нет (самые первые посты для этой биржи)."""
    openers = state_store.get_recent_openers(post_type)
    if not openers:
        return ""

    lines = "\n".join(f"- {o}" for o in openers[-_RECENT_OPENERS_IN_PROMPT:])
    return (
        "\n\nНЕ начинай пост так же, как эти последние посты (другое первое "
        f"предложение по структуре и лексике):\n{lines}"
    )


def _valid_continuity_entry(post_type: str, theme: str) -> Optional[dict]:
    """Запись истории по теме, если она есть и не устарела - общая
    логика для continuity_block() (текст) и continuity_pct() (число,
    которое нужно заранее разрешить в валидаторе, раз мы сами
    предлагаем модели на него сослаться)."""
    history = state_store.get_theme_post_history(post_type)
    entry = history.get(theme)
    if entry is None:
        return None

    age_days = (time.time() - entry["timestamp"]) / 86400
    if age_days > _CONTINUITY_MAX_AGE_DAYS:
        return None

    return entry


def continuity_pct(post_type: str, theme: str) -> Optional[float]:
    """% из прошлого поста по теме, если continuity_block() предложит
    на него сослаться - вызывающий код должен добавить это число в
    allowed_numbers СВОЕГО валидатора, иначе честно выполненная
    инструкция "сошлись на прошлый пост" будет забракована как
    "выдуманное" число (см. okx_orbit_generator._build_user_prompt)."""
    entry = _valid_continuity_entry(post_type, theme)
    return entry["pct"] if entry is not None else None


def continuity_block(post_type: str, theme: str, label: str) -> str:
    """Если по этой теме на этой же площадке уже был пост раньше (запись
    есть и не старше _CONTINUITY_MAX_AGE_DAYS) - блок с РЕАЛЬНЫМИ
    фактами того раза, которые LLM МОЖЕТ (не обязан) использовать для
    честной отсылки "как я говорил раньше". Пустая строка, если истории
    по теме нет или она устарела."""
    entry = _valid_continuity_entry(post_type, theme)
    if entry is None:
        return ""

    age_days = (time.time() - entry["timestamp"]) / 86400
    sign = "+" if entry["pct"] >= 0 else ""
    return (
        f"\n\nКОНТЕКСТ (использовать НЕ обязательно, только если реально "
        f"уместно): {int(age_days)} дн. назад про {label} ты писал: "
        f"\"{entry['stance_summary']}\" (тогда движение было {sign}{entry['pct']}%). "
        "Если сейчас видно интересное развитие ИМЕННО ЭТОЙ истории - можешь "
        "честно на это сослаться. Не выдумывай других прошлых постов и не "
        "утверждай, что говорил что-то, чего нет в этом контексте."
    )
