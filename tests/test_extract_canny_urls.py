import pytest
from Bot.shared.canny import extract_canny_urls

class DummyField:
    def __init__(self, value):
        self.value = value

class DummyAuthor:
    def __init__(self, url):
        self.url = url

class DummyEmbed:
    def __init__(self, url=None, title=None, description=None, author=None, fields=None):
        self.url = url
        self.title = title
        self.description = description
        self.author = author
        self.fields = fields or []

class DummyMessage:
    def __init__(self, content="", embeds=None, msg_id=123):
        self.id = msg_id
        self.content = content
        self.embeds = embeds or []

def test_extract_canny_urls_valid_domains():
    content = (
        "Check these out: https://feedback.vrchat.com/p/test-post "
        "and https://canny.io/p/test-post-2 "
        "and https://vrchat.canny.io/p/test-post-3"
    )
    msg = DummyMessage(content=content)
    urls = extract_canny_urls(msg)
    assert urls == [
        "https://feedback.vrchat.com/p/test-post",
        "https://canny.io/p/test-post-2",
        "https://vrchat.canny.io/p/test-post-3"
    ]

def test_extract_canny_urls_rejects_bypass_attempts():
    content = (
        "Bypass attempts: "
        "https://canny.io.attacker.com/p/fake "
        "https://feedback.vrchat.com.attacker.com/p/fake "
        "https://notcanny.io/p/fake "
        "https://fakecanny.io/p/fake"
    )
    msg = DummyMessage(content=content)
    urls = extract_canny_urls(msg)
    assert urls == []

def test_extract_canny_urls_deduplication_and_embeds():
    embed = DummyEmbed(
        url="https://feedback.vrchat.com/p/test-post",
        title="Link: https://sub.canny.io/p/test-post-4",
        description="Also https://canny.io.attacker.com/p/fake",
        author=DummyAuthor("https://canny.io/p/author-link"),
        fields=[DummyField("See https://feedback.vrchat.com/p/test-post-5")]
    )
    msg = DummyMessage(content="https://feedback.vrchat.com/p/test-post", embeds=[embed])
    urls = extract_canny_urls(msg)
    assert urls == [
        "https://feedback.vrchat.com/p/test-post",
        "https://sub.canny.io/p/test-post-4",
        "https://canny.io/p/author-link",
        "https://feedback.vrchat.com/p/test-post-5"
    ]
