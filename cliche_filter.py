"""
cliche_filter.py - лёгкий пост-фильтр на клише, поверх voice_guidelines.
STYLE_DIRECTIVE в промпте.

LLM иногда всё равно проскальзывает в шаблонные обороты, несмотря на
инструкцию в промпте - это статистическая, а не гарантированная мера.
check_cliches() ловит это уже на ГОТОВОМ тексте, перед публикацией -
вызывается в конце каждой validate_*/_validate_hook функции проекта,
рядом с проверкой чисел/дисклеймера (см. validator.py, opinion_generator.
validate_opinion_post_text, treasury_generator.validate_treasury_hook и
т.д.).

Не заменяет STYLE_DIRECTIVE в промпте, а страхует его - жёсткая
проверка на подстроки дешевле повторного вызова LLM и ловит именно те
фразы, которые статистически чаще всего проскальзывают, даже когда
модель проинструктирована их не использовать.
"""
import voice_guidelines


def check_cliches(text: str) -> tuple:
    """Возвращает (ok, found) - found - список найденных в тексте фраз
    из voice_guidelines.BANNED_PHRASES (по вхождению подстроки,
    регистронезависимо - большинство фраз многословные, простого
    вхождения достаточно, отдельные слова в списке нет)."""
    lowered = text.lower()
    found = [phrase for phrase in voice_guidelines.BANNED_PHRASES if phrase in lowered]
    return len(found) == 0, found
