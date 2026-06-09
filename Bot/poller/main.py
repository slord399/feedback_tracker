import asyncio
import time
import json
import logging
import sys
import os
from datetime import datetime, timezone

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client
from Bot.shared.canny import fetch_canny_data, extract_post_from_data, extract_board_posts
from Bot.shared.rate_limit import get_global_limiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("poller")

async def discover_boards(valkey, limiter):
    logger.info("Discovering boards...")
    await limiter.acquire()
    data = await fetch_canny_data("https://feedback.vrchat.com/")
    if not data:
        logger.error("Failed to fetch boards data")
        return []
    items = data.get("boards", {}).get("items", {})
    boards = []
    for k, v in items.items():
        boards.append({"id": v.get("_id"), "name": v.get("name"), "urlName": v.get("urlName"), "url": f"https://feedback.vrchat.com/{v.get('urlName')}"})
    if boards:
        await valkey.set("canny_boards", json.dumps(boards))
        logger.info(f"Discovered {len(boards)} boards")
    return boards

async def poll_board_recursive(valkey, limiter, board):
    board_url = board["url"]
    page = 1
    total_indexed = 0
    logger.info(f"Starting crawl for board {board['name']}")

    while True:
        url = f"{board_url}?sort=new&page={page}"
        await limiter.acquire()
        data = await fetch_canny_data(url)
        if not data: break

        posts = extract_board_posts(data)
        if not posts: break

        for p in posts:
            uname = p.get("postURLName")
            if not uname: continue

            # Extract detailed post data from the Redux 'posts' object if available
            board_id = p.get("boardID", "")
            detailed_posts = data.get("posts", {})
            full_post = None
            if board_id in detailed_posts:
                full_post = detailed_posts[board_id].get(uname)
            if not full_post:
                full_post = p

            title = full_post.get("title") or uname
            score = full_post.get("score", 0)
            status = full_post.get("status", "open")
            details = full_post.get("details", "")
            comments = full_post.get("commentCount", 0)
            p_url = f"https://feedback.vrchat.com/{board['urlName']}/p/{uname}"

            # Check for vote/status changes even for non-indexed posts
            pid = full_post.get("_id")
            if pid:
                old_json = await valkey.get(f"post_cache:{pid}")
                if old_json:
                    old = json.loads(old_json)
                    if old.get("status") != status:
                        await valkey.lpush("discord_jobs", json.dumps({"type": "status_change", "post": full_post, "old_status": old.get("status"), "url": p_url}))
                    if (score // 25) > (old.get("score", 0) // 25):
                        await valkey.lpush("discord_jobs", json.dumps({"type": "vote_progress", "post": full_post, "url": p_url}))
                await valkey.set(f"post_cache:{pid}", json.dumps(full_post))

            await valkey.hset("canny_search_index", uname, json.dumps({
                "title": title,
                "details": details,
                "url": p_url,
                "score": score,
                "status": status,
                "comments": comments,
                "board": board["name"]
            }))
            total_indexed += 1

        # Check hasNextPage
        has_next = False
        queries = data.get("postQueries", {})
        for q in queries.values():
            if isinstance(q, dict) and q.get("hasNextPage"):
                has_next = True
                break

        if not has_next: break
        page += 1
        if page > 1000: break

    logger.info(f"Board {board['name']} crawl complete. Total: {total_indexed}")

def get_polling_interval(post):
    status = post.get("status", "").lower()
    if status in ["complete", "completed", "closed", "available in future release"]: return 12 * 3600
    try:
        created_at = datetime.fromisoformat(post.get("created").replace("Z", "+00:00"))
        updated_at = datetime.fromisoformat(post.get("updatedAt").replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age = (now - created_at).days
        inactive = (now - updated_at).total_seconds() / 3600
        if inactive < 1: return 300
        if inactive < 6: return 900
        if inactive < 24: return 3600
        if inactive < 48: return 10800
        if age > 365: return 86400
        if age > 180: return 43200
        return 21600
    except: return 3600

async def poll_post(valkey, limiter, url, url_name):
    await limiter.acquire()
    data = await fetch_canny_data(url)
    post = extract_post_from_data(data, url_name)
    if not post: return None
    pid = post.get("_id")
    old_json = await valkey.get(f"post_cache:{pid}")
    if old_json:
        old = json.loads(old_json)
        if old.get("status") != post.get("status"):
            await valkey.lpush("discord_jobs", json.dumps({"type": "status_change", "post": post, "old_status": old.get("status"), "url": url}))
        if (post.get("score", 0) // 25) > (old.get("score", 0) // 25):
            await valkey.lpush("discord_jobs", json.dumps({"type": "vote_progress", "post": post, "url": url}))
    await valkey.set(f"post_cache:{pid}", json.dumps(post))
    await valkey.hset("canny_search_index", url_name, json.dumps({
        "title": post.get("title"), "details": post.get("details", ""), "url": url,
        "score": post.get("score"), "status": post.get("status"), "comments": post.get("commentCount", 0),
        "board": post.get("board", {}).get("name")
    }))
    return post

async def poller_loop():
    logger.info("Poller started")
    valkey = get_valkey_client(); limiter = get_global_limiter(valkey)

    # Run initial full crawl in background tasks
    boards = await discover_boards(valkey, limiter)
    for b in boards:
        asyncio.create_task(poll_board_recursive(valkey, limiter, b))

    while True:
        try:
            indexed = await valkey.smembers("indexed_post_urls")
            logger.info(f"Polling {len(indexed)} indexed posts...")
            for url in indexed:
                parts = url.split("/")
                if "p" in parts:
                    name = parts[parts.index("p") + 1]
                    nxt = await valkey.get(f"next_poll:{url}")
                    if not nxt or float(nxt) <= time.time():
                        p = await poll_post(valkey, limiter, url, name)
                        if p: await valkey.set(f"next_poll:{url}", time.time() + get_polling_interval(p))

            # Check front pages for new activity
            boards = await discover_boards(valkey, limiter)
            for b in boards:
                await limiter.acquire()
                data = await fetch_canny_data(f"{b['url']}?sort=new")
                posts = extract_board_posts(data)
                for p in posts:
                    uname = p.get("postURLName")
                    if uname and not await valkey.exists(f"post_cache_lite:{uname}"):
                        await valkey.set(f"post_cache_lite:{uname}", "1")
                        # Add to index or trigger checks...
        except: logger.exception("Poller loop error")
        await asyncio.sleep(300)

if __name__ == "__main__": asyncio.run(poller_loop())
