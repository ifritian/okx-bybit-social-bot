"""
win_celebration_generator.py - формат "Забрали профит!" (ТОЛЬКО Binance Square).

Реакция на КАЖДЫЙ сигнал, закрывшийся в плюс (result == "win", см.
outcome_tracker.check_open_outcomes) - публикуется сразу в момент
закрытия (main._publish_win_celebrations, вызывается из tick() рядом с
_post_outcome_updates_to_bluesky, тем же путём и на тех же данных -
closed_records этого тика).

Формат НАМЕРЕННО клишейный и эмоциональный ("не могу поверить", "снова
сработало", "обожаю такие дни") - в отличие от остальных постов бота,
где voice_guidelines/cliche_filter вырезают именно такие фразы. Разница
в ТИПЕ клише: voice_guidelines.BANNED_PHRASES - это канцелярит и
ИИ-обороты ("стоит отметить", "кроме того" и т.п.), их фильтр не
трогает эмоциональные восклицания - как раз это и нужно этому формату,
поэтому STYLE_DIRECTIVE/cliche_filter здесь применяются как обычно (не
хотим робота, хотим наигранного, но живого человека), просто системный
промпт явно просит утрированную радость, а не сдержанный тон.

Как и у text_generator (сигналы) - хук от LLM и блок с фактическим
результатом РАЗДЕЛЕНЫ (см. post_format.assemble_win_celebration_post):
LLM пишет ТОЛЬКО эмоцию, без единого числа, все цифры (вход/выход/%/
время) добавляются кодом из closed-record - у модели физически нет
возможности исказить результат сделки.
"""
import logging
import random
from typing import Optional

import cliche_filter
import config
from groq_client import call_groq
import validator
import voice_guidelines

logger = logging.getLogger(__name__)

# Ротация эмоционального фокуса - не тема (как THEMES у opinion/hot_take),
# а угол, с которого подаётся один и тот же факт "сделка закрылась в
# плюс". Без этого при высокой temperature все хуки быстро сходятся к
# одной и той же формулировке радости.
_ANGLES = {
    "pure_joy": "чистая, почти детская радость от того, что сделка сработала",
    "pride": "гордость за то, что не испугался(-ась) и дождался(-ась) тейка, а не закрыл(-а) раньше времени",
    "streak": "кайф от того, что стратегия и подход снова сработали - не разовая удача, а система",
    "gratitude": "тёплая благодарность подписчикам, которые были рядом и видели сделку с самого входа",
}

_SYSTEM_PROMPT = """Ты пишешь короткую эмоциональную реакцию для Binance
Square на сделку, которая ТОЛЬКО ЧТО закрылась в плюс. 1-2 предложения,
максимум 3. Тон - открытая, немного наигранная радость, как у живого
человека, который реально рад заработку: "невероятно", "не могу
поверить", "обожаю такие дни", "вот ради этого мы это и делаем" -
подобные фразы ЖЕЛАТЕЛЬНЫ, это осознанный жанр поста, а не баг.

СТРОГО ЗАПРЕЩЕНО:
- любые цифры, проценты, тикеры или суммы (результат сделки допишется
  отдельно кодом - твоя часть ТОЛЬКО эмоция, без единого числа);
- обещания или намёки, что так будет всегда/каждый раз - радуйся ЭТОЙ
  конкретной сделке, а не гарантируй будущее;
- финансовые советы ("покупайте", "повторяйте за мной").

Пиши текст поста, без пояснений и без кавычек.""" + voice_guidelines.STYLE_DIRECTIVE


def pick_angle(last_angle: Optional[str]) -> str:
    keys = list(_ANGLES.keys())
    candidates = [k for k in keys if k != last_angle] or keys
    return random.choice(candidates)


def generate_win_celebration_hook(angle: str) -> Optional[str]:
    """Возвращает хук (без цифр, без дисклеймера - оба добавляются
    отдельно, см. post_format.assemble_win_celebration_post), либо None,
    если генерация не удалась содержательно."""
    focus = _ANGLES.get(angle, _ANGLES["pure_joy"])
    user_prompt = f"Угол подачи этого поста: {focus}.\n\nНапиши пост-реакцию на удачно закрытую сделку."

    hook = call_groq(_SYSTEM_PROMPT, user_prompt, max_tokens=180, temperature=1.05, model=config.GROQ_MODEL_SECONDARY)

    if len(hook.strip()) < 8:
        logger.warning("Хук 'Забрали профит!' пустой/слишком короткий (%r) - пропускаю", hook)
        return None

    return hook.strip()


def validate_win_celebration_hook(hook: str) -> tuple:
    """Как validator.validate_image_post_text - хук не должен содержать
    ВООБЩЕ никаких чисел (см. docstring модуля - все цифры добавляются
    кодом отдельно, а не проверяются постфактум на совпадение)."""
    if validator._NUMBER_RE.search(hook):
        return False, "В хуке 'Забрали профит!' есть цифры - они должны прийти только из code-блока результата"

    suspicious = validator.find_suspicious_english_words(hook)
    if suspicious:
        return False, f"В хуке похоже есть посторонние английские слова (смешение языков): {', '.join(suspicious[:5])}"

    cliche_ok, found = cliche_filter.check_cliches(hook)
    if not cliche_ok:
        return False, f"В хуке есть шаблонные ИИ-обороты (не эмоция, а канцелярит): {', '.join(found)}"

    return True, ""
