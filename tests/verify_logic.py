import sys
import os
from datetime import datetime, timedelta, timezone

# Mocking some parts for testing
def clean_url(url):
    return url.rstrip(')')

def get_polling_interval(post):
    status = post.get("status", "").lower()
    if status in ["complete", "completed", "closed", "available in future release"]:
        return 12 * 3600

    try:
        now = datetime.now(timezone.utc)
        created_at = datetime.fromisoformat(post.get("created").replace("Z", "+00:00"))
        updated_at_str = post.get("updatedAt") or post.get("created")
        updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))

        age_days = (now - created_at).days
        inactive_hours = (now - updated_at).total_seconds() / 3600

        if inactive_hours < 1: return 300 # 5 mins
        if inactive_hours < 6: return 900 # 15 mins
        if inactive_hours < 24: return 3600 # 60 mins
        if inactive_hours < 48: return 10800 # 3 hours

        if age_days > 365: return 86400 # 24 hours
        if age_days > 180: return 43200 # 12 hours

        return 21600 # 6 hours
    except:
        return 3600

def test_clean_url():
    assert clean_url("https://example.com/page)") == "https://example.com/page"
    assert clean_url("https://example.com/page") == "https://example.com/page"
    print("test_clean_url passed")

def test_polling_intervals():
    now = datetime.now(timezone.utc)

    # Case: New / Recently Active (5 mins)
    post_new = {"status": "open", "created": now.isoformat(), "updatedAt": now.isoformat()}
    assert get_polling_interval(post_new) == 300

    # Case: Inactive for 2 hours (15 mins)
    post_inactive_2h = {"status": "open", "created": (now - timedelta(hours=3)).isoformat(), "updatedAt": (now - timedelta(hours=2)).isoformat()}
    assert get_polling_interval(post_inactive_2h) == 900

    # Case: Inactive for 10 hours (60 mins)
    post_inactive_10h = {"status": "open", "created": (now - timedelta(hours=12)).isoformat(), "updatedAt": (now - timedelta(hours=10)).isoformat()}
    assert get_polling_interval(post_inactive_10h) == 3600

    # Case: Completed status (12 hours)
    post_completed = {"status": "complete", "created": now.isoformat()}
    assert get_polling_interval(post_completed) == 12 * 3600

    print("test_polling_intervals passed")

if __name__ == "__main__":
    test_clean_url()
    test_polling_intervals()
