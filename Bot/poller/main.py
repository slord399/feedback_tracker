import asyncio
import time
import json
import logging
import sys
import os
from datetime import datetime, timezone

# Add Bot to path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

from Bot.shared.valkey import get_valkey_client
from Bot.shared.canny import fetch_canny_data, extract_post_from_data, extract_board_posts
from Bot.shared.rate_limit import get_global_limiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("poller")

async def discover_boards(valkey, limiter):
    """
    Finds all boards on feedback.vrchat.com
    """
    await limiter.acquire()
    data = await fetch_canny_data("https://feedback.vrchat.com/")
    if not data:
        return []

    boards_data = data.get("boards", {}).get("items", {})
    boards = []
    for url_name, board in boards_data.items():
        boards.append({
            "id": board.get("_id"),
            "name": board.get("name"),
            "urlName": board.get("urlName"),
            "url": f"https://feedback.vrchat.com/{board.get('urlName')}"
        })

    if boards:
        valkey.set("canny_boards", json.dumps(boards))
        logger.info(f"Discovered {len(boards)} boards")
    return boards

def get_polling_interval(post):
    """
    Adaptive polling interval based on requirements:
    """
    status = post.get("status")
    if status in ["complete", "closed", "available"]:
        return 12 * 3600

    created_at = datetime.fromisoformat(post.get("created").replace("Z", "+00:00"))
    updated_at = datetime.fromisoformat(post.get("updatedAt").replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)

    age_days = (now - created_at).days
    inactive_hours = (now - updated_at).total_seconds() / 3600

    if inactive_hours < 1:
        return 5 * 60
    elif inactive_hours < 6:
        return 15 * 60
    elif inactive_hours < 24:
        return 60 * 60
    elif inactive_hours < 48:
        return 3 * 3600
    else:
        if age_days > 365:
            return 24 * 3600
        elif age_days > 180:
            return 12 * 3600
        else:
            return 6 * 3600

async def poll_post(valkey, limiter, post_url, post_url_name):
    """
    Polls a single post and checks for changes
    """
    await limiter.acquire()
    data = await fetch_canny_data(post_url)
    if not data:
        return None

    post = extract_post_from_data(data, post_url_name)
    if not post:
        return None

    post_id = post.get("_id")
    old_post_json = valkey.get(f"post_cache:{post_id}")

    if old_post_json:
        old_post = json.loads(old_post_json)
        # Check status change
        if old_post.get("status") != post.get("status"):
            logger.info(f"Status changed for {post_url_name}: {old_post.get('status')} -> {post.get('status')}")
            # Queue notification
            job = {
                "type": "status_change",
                "post": post,
                "old_status": old_post.get("status"),
                "new_status": post.get("status"),
                "url": post_url
            }
            valkey.lpush("discord_jobs", json.dumps(job))

        # Check votes (for progress report every 25 votes)
        old_score = old_post.get("score", 0)
        new_score = post.get("score", 0)
        if (new_score // 25) > (old_score // 25):
            job = {
                "type": "vote_progress",
                "post": post,
                "score": new_score,
                "url": post_url
            }
            valkey.lpush("discord_jobs", json.dumps(job))

    valkey.set(f"post_cache:{post_id}", json.dumps(post))

    # Update search index
    valkey.hset("canny_search_index", post_url_name, json.dumps({
        "title": post.get("title"),
        "url": post_url,
        "score": post.get("score"),
        "status": post.get("status")
    }))

    return post

async def poller_loop():
    valkey = get_valkey_client()
    limiter = get_global_limiter(valkey)

    while True:
        try:
            # 1. Discover boards periodically
            boards = await discover_boards(valkey, limiter)

            # 2. Track indexed posts
            indexed_posts = valkey.smembers("indexed_post_urls")
            for post_url in indexed_posts:
                parts = post_url.split("/")
                if "p" in parts:
                    url_name = parts[parts.index("p") + 1]
                    next_poll = valkey.get(f"next_poll:{post_url}")
                    if not next_poll or float(next_poll) <= time.time():
                        post = await poll_post(valkey, limiter, post_url, url_name)
                        if post:
                            interval = get_polling_interval(post)
                            valkey.set(f"next_poll:{post_url}", time.time() + interval)

            # 3. Discover new posts from boards
            for board in boards:
                await limiter.acquire()
                board_url = board["url"]
                board_data = await fetch_canny_data(board_url)
                posts = extract_board_posts(board_data)
                for p in posts:
                    p_url_name = p.get("postURLName")
                    if not valkey.exists(f"post_cache_lite:{p_url_name}"):
                        logger.info(f"New post discovered: {p_url_name}")
                        valkey.set(f"post_cache_lite:{p_url_name}", "1")
        except Exception as e:
            logger.exception("Error in poller loop")

        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(poller_loop())
