#!/usr/bin/env python3
"""
Тесты news_channel_reader.py: парсинг HTML-разметки t.me/s/<канал> на
реалистичном фикстурном HTML (сеть замокана - см. FAKE_HTML ниже,
структура соответствует реальной, полученной с t.me/s/forklog).
"""
from unittest.mock import patch

import news_channel_reader

FAKE_HTML = """
<html><body>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="forklog/43829">
    <div class="tgme_widget_message_bubble">
      <div class="tgme_widget_message_text js-message_text" dir="auto">
        Base достигла первой стадии децентрализации по «сценарию Бутерина», сообщили разработчики проекта.
        Они выделили два ключевых момента перехода к «Stage 1»: внедрение системы доказательства ошибки
        и децентрализация контроля обновлений контрактов.<br>
        <a href="https://forklog.com/news">Новости</a> | <a href="https://t.me/forklogAI">AI</a> | <a href="https://youtube.com">YouTube</a>
      </div>
    </div>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="forklog/43823">
    <div class="tgme_widget_message_bubble">
      <div class="tgme_widget_message_text js-message_text" dir="auto">
        <a href="https://forklog.com/rates/">forklog.com/rates/</a>
      </div>
    </div>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="forklog/43818">
    <div class="tgme_widget_message_bubble">
      <div class="tgme_widget_message_photo_wrap"></div>
    </div>
  </div>
</div>
</body></html>
"""


class FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        pass


def test_fetch_recent_posts_parses_text_and_strips_footer():
    with patch("news_channel_reader.requests.get", return_value=FakeResponse(FAKE_HTML)):
        posts = news_channel_reader.fetch_recent_posts()

    assert len(posts) == 1  # второй пост слишком короткий, третий без текста
    post = posts[0]
    assert post.post_id == 43829
    assert "Base достигла первой стадии" in post.text
    assert "Новости" not in post.text
    assert "AI" not in post.text
    assert "YouTube" not in post.text


def test_fetch_recent_posts_skips_short_and_textless_posts():
    with patch("news_channel_reader.requests.get", return_value=FakeResponse(FAKE_HTML)):
        posts = news_channel_reader.fetch_recent_posts()

    post_ids = {p.post_id for p in posts}
    assert 43823 not in post_ids  # слишком короткий текст
    assert 43818 not in post_ids  # нет текстового блока (только фото)


def test_fetch_recent_posts_returns_empty_list_on_network_error():
    import requests as requests_module

    with patch("news_channel_reader.requests.get", side_effect=requests_module.RequestException("boom")):
        posts = news_channel_reader.fetch_recent_posts()

    assert posts == []


def test_fetch_recent_posts_respects_limit():
    many_posts_html = "<html><body>" + "".join(
        f'<div class="tgme_widget_message" data-post="forklog/{100 + i}">'
        f'<div class="tgme_widget_message_text">{"Полноценный текст новости с достаточной длиной номер " + str(i) + " " * 50}</div>'
        f"</div>"
        for i in range(15)
    ) + "</body></html>"

    with patch("news_channel_reader.requests.get", return_value=FakeResponse(many_posts_html)):
        posts = news_channel_reader.fetch_recent_posts(limit=5)

    assert len(posts) == 5


def test_fetch_recent_posts_sorted_newest_first():
    html = (
        '<html><body>'
        '<div class="tgme_widget_message" data-post="forklog/100">'
        '<div class="tgme_widget_message_text">' + "Старая новость про рынок. " * 10 + '</div></div>'
        '<div class="tgme_widget_message" data-post="forklog/200">'
        '<div class="tgme_widget_message_text">' + "Новая новость про рынок. " * 10 + '</div></div>'
        '</body></html>'
    )
    with patch("news_channel_reader.requests.get", return_value=FakeResponse(html)):
        posts = news_channel_reader.fetch_recent_posts()

    assert [p.post_id for p in posts] == [200, 100]


def test_fetch_recent_posts_skips_digest_posts():
    """Реальная структура поста-подборки ForkLog: несколько отдельных
    ссылок на разные статьи в одном посте - если дать это как "одну
    новость" LLM, получится бессвязная реакция на 5-10 тем сразу."""
    digest_html = (
        '<html><body>'
        '<div class="tgme_widget_message" data-post="forklog/300">'
        '<div class="tgme_widget_message_text">'
        '⭐ <a href="https://forklog.com/news/story-one">Китай и Швеция представили новые планы</a><br>'
        '🇺🇸 <a href="https://forklog.com/news/story-two">ИИ-бум почти удвоил объем</a><br>'
        '💸 <a href="https://forklog.com/news/story-three">ETF-притоки поддержали ралли</a><br>'
        '🤖 <a href="https://forklog.com/exclusive/story-four">Xpeng привлекла 900 млн</a>'
        '</div></div>'
        '<div class="tgme_widget_message" data-post="forklog/301">'
        '<div class="tgme_widget_message_text">'
        + "Обычная одиночная новость про биткоин без ссылок на другие статьи. " * 3 +
        '</div></div>'
        '</body></html>'
    )
    with patch("news_channel_reader.requests.get", return_value=FakeResponse(digest_html)):
        posts = news_channel_reader.fetch_recent_posts()

    post_ids = {p.post_id for p in posts}
    assert 300 not in post_ids  # дайджест с 4 ссылками на разные статьи - пропущен
    assert 301 in post_ids  # обычная одиночная новость - осталась
