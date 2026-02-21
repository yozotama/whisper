from pathlib import Path

from automation.x_publisher import (
    AppConfig,
    OpenAIConfig,
    XConfig,
    build_oauth1_header,
    compute_content_hash,
    generate_fallback_draft,
    parse_feed_xml,
)


def _dummy_config() -> AppConfig:
    return AppConfig(
        topic="Test topic",
        instruction="Test instruction",
        rss_urls=["https://example.com/rss.xml"],
        keywords=[],
        max_items=5,
        language="ja",
        output_dir=Path("/tmp"),
        request_timeout_seconds=20,
        openai=OpenAIConfig(
            api_key_env="OPENAI_API_KEY",
            model="gpt-4.1-mini",
            base_url="https://api.openai.com/v1",
        ),
        x=XConfig(
            api_key_env="X_API_KEY",
            api_secret_env="X_API_SECRET",
            access_token_env="X_ACCESS_TOKEN",
            access_token_secret_env="X_ACCESS_TOKEN_SECRET",
        ),
    )


def test_parse_rss_feed_xml():
    xml = """
    <rss version="2.0">
      <channel>
        <title>Example Feed</title>
        <item>
          <title>Item One</title>
          <link>https://example.com/one</link>
          <pubDate>Sat, 21 Feb 2026 00:00:00 GMT</pubDate>
          <description>Summary one</description>
        </item>
      </channel>
    </rss>
    """
    items = parse_feed_xml(xml, source_feed="https://example.com/rss.xml")
    assert len(items) == 1
    assert items[0].title == "Item One"
    assert items[0].link == "https://example.com/one"
    assert "Summary one" in items[0].summary


def test_parse_atom_feed_xml():
    xml = """
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>Example Atom</title>
      <entry>
        <title>Atom Entry</title>
        <link href="https://example.com/atom-entry"/>
        <updated>2026-02-21T00:00:00Z</updated>
        <summary>Atom summary</summary>
      </entry>
    </feed>
    """
    items = parse_feed_xml(xml, source_feed="https://example.com/atom.xml")
    assert len(items) == 1
    assert items[0].title == "Atom Entry"
    assert items[0].link == "https://example.com/atom-entry"
    assert items[0].published == "2026-02-21T00:00:00Z"


def test_compute_content_hash_changes():
    a = compute_content_hash("article-a", "post-a")
    b = compute_content_hash("article-b", "post-a")
    c = compute_content_hash("article-a", "post-b")
    assert a != b
    assert a != c


def test_build_oauth_header_has_required_fields():
    header = build_oauth1_header(
        method="POST",
        url="https://api.x.com/2/tweets",
        consumer_key="ck",
        consumer_secret="cs",
        token="at",
        token_secret="ats",
        nonce="nonce123",
        timestamp="1700000000",
    )

    assert header.startswith("OAuth ")
    assert 'oauth_consumer_key="ck"' in header
    assert 'oauth_nonce="nonce123"' in header
    assert 'oauth_signature_method="HMAC-SHA1"' in header
    assert 'oauth_timestamp="1700000000"' in header
    assert 'oauth_token="at"' in header
    assert 'oauth_version="1.0"' in header
    assert 'oauth_signature="' in header


def test_generate_fallback_draft_truncates_long_post():
    config = _dummy_config()
    config.topic = "x" * 400
    draft = generate_fallback_draft(config, items=[])
    assert len(draft.x_post) <= 280
    assert draft.used_fallback is True

