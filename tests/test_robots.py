"""Direct coverage for the robots.txt policy shared by ITAKA, TUI and Wakacje.pl."""

import pytest

from travel_deal_agent.providers.robots import robots_policy


def test_allowed_path_with_no_matching_disallow_returns_zero_delay() -> None:
    text = "User-agent: *\nDisallow: /api/\n"
    delay = robots_policy(text, "/wczasy/")
    assert delay == 0.0


def test_disallowed_path_raises() -> None:
    text = "User-agent: *\nDisallow: /wczasy/\n"
    with pytest.raises(ValueError, match="forbidden"):
        robots_policy(text, "/wczasy/x")


def test_missing_or_malformed_robots_txt_fails_closed() -> None:
    with pytest.raises(ValueError, match="robots.txt"):
        robots_policy("<html>not robots</html>", "/wczasy/")
    with pytest.raises(ValueError, match="robots.txt"):
        robots_policy("", "/wczasy/")


def test_crawl_delay_is_reported() -> None:
    text = "User-agent: *\nDisallow: /api/\nCrawl-delay: 7\n"
    delay = robots_policy(text, "/wczasy/")
    assert delay == 7.0


def test_union_of_disallow_rules_across_user_agents_applies() -> None:
    # Arrange: a rule scoped to another user agent still applies (conservative union).
    text = "User-agent: SomeOtherBot\nDisallow: /wczasy/\nUser-agent: *\nDisallow: /api/\n"
    with pytest.raises(ValueError, match="forbidden"):
        robots_policy(text, "/wczasy/x")


def test_union_of_crawl_delay_across_user_agents_takes_the_maximum() -> None:
    text = "User-agent: SomeOtherBot\nCrawl-delay: 15\nUser-agent: *\nCrawl-delay: 5\n"
    delay = robots_policy(text, "/wczasy/")
    assert delay == 15.0
