#!/usr/bin/env python3
"""
Тесты audience_question_generator.py - чистая логика ротации, без сети
и без LLM (сам модуль их не использует).
"""
import audience_question_generator


def test_pick_question_avoids_last_used():
    questions = audience_question_generator.QUESTIONS
    for _ in range(30):
        chosen = audience_question_generator.pick_question(questions[0])
        assert chosen != questions[0]


def test_pick_question_handles_unknown_last():
    chosen = audience_question_generator.pick_question(None)
    assert chosen in audience_question_generator.QUESTIONS


def test_pick_question_handles_never_before_seen_text():
    # last_question - строка, которой нет в пуле (например, старый вопрос,
    # убранный из списка при обновлении бота) - не должно падать.
    chosen = audience_question_generator.pick_question("вопрос из прошлой версии бота")
    assert chosen in audience_question_generator.QUESTIONS


def test_all_questions_are_reasonably_short_for_bluesky():
    # Не строгий лимит в 300 (это не финальный пост, а просто список), но
    # разумный запас, чтобы явно не выйти за пределы даже с эмодзи/пробелами.
    for q in audience_question_generator.QUESTIONS:
        assert len(q) < 250, f"Слишком длинный вопрос: {q!r}"


def test_no_duplicate_questions_in_pool():
    assert len(audience_question_generator.QUESTIONS) == len(set(audience_question_generator.QUESTIONS))


if __name__ == "__main__":
    import sys
    import types

    passed, failed = 0, 0
    module = sys.modules[__name__]
    for name in dir(module):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not isinstance(fn, types.FunctionType):
            continue
        try:
            fn()
            print(f"OK   {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
