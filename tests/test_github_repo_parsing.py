import pytest
from Bot.shared.canny import parse_github_repo

def test_parse_github_repo_full_urls():
    assert parse_github_repo("https://github.com/Hackebein/feedback.vrchat.com") == "Hackebein/feedback.vrchat.com"
    assert parse_github_repo("https://github.com/Hackebein/feedback.vrchat.com.git") == "Hackebein/feedback.vrchat.com"
    assert parse_github_repo("https://github.com/Hackebein/feedback.vrchat.com/") == "Hackebein/feedback.vrchat.com"
    assert parse_github_repo("http://www.github.com/owner/repo") == "owner/repo"

def test_parse_github_repo_slugs():
    assert parse_github_repo("Hackebein/feedback.vrchat.com") == "Hackebein/feedback.vrchat.com"
    assert parse_github_repo("slord399/feedback_tracker") == "slord399/feedback_tracker"
    assert parse_github_repo("owner/repo.git") == "owner/repo"
    assert parse_github_repo(" owner/repo ") == "owner/repo"

def test_parse_github_repo_edge_cases():
    assert parse_github_repo("") == ""
    assert parse_github_repo("invalid_string_no_slash") == ""
