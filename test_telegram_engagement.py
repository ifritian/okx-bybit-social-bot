#!/usr/bin/env python3
"""
Тесты telegram_engagement.py: ротация опросов и AMA-приглашений, без
сети и без LLM (сам модуль их не использует).
"""
import telegram_engagement


def test_pick_poll_avoids_last_used():
    questions = [p["question"] for p in telegram_engagement.POLLS]
    for _ in range(30):
        chosen = telegram_engagement.pick_poll(questions[0])
        assert chosen["question"] != questions[0]


def test_pick_poll_handles_unknown_last():
    chosen = telegram_engagement.pick_poll(None)
    assert chosen in telegram_engagement.POLLS


def test_all_polls_have_valid_option_counts():
    # Telegram API требует 2-10 вариантов ответа на опрос.
    for poll in telegram_engagement.POLLS:
        assert 2 <= len(poll["options"]) <= 10, poll["question"]


def test_all_poll_questions_within_telegram_limit():
    # Telegram лимит на текст вопроса - 300 символов.
    for poll in telegram_engagement.POLLS:
        assert len(poll["question"]) <= 300, poll["question"]


def test_all_poll_options_within_telegram_limit():
    # Telegram лимит на текст варианта ответа - 100 символов.
    for poll in telegram_engagement.POLLS:
        for option in poll["options"]:
            assert len(option) <= 100, option


def test_no_duplicate_poll_questions():
    questions = [p["question"] for p in telegram_engagement.POLLS]
    assert len(questions) == len(set(questions))


def test_pick_ama_prompt_avoids_last_used():
    prompts = telegram_engagement.AMA_PROMPTS
    for _ in range(30):
        chosen = telegram_engagement.pick_ama_prompt(prompts[0])
        assert chosen != prompts[0]


def test_pick_ama_prompt_handles_unknown_last():
    chosen = telegram_engagement.pick_ama_prompt(None)
    assert chosen in telegram_engagement.AMA_PROMPTS


def test_no_duplicate_ama_prompts():
    assert len(telegram_engagement.AMA_PROMPTS) == len(set(telegram_engagement.AMA_PROMPTS))


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
