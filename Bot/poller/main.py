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
    if not data: return []
    items = data.get("boards", {}).get("items", {})
    boards = []
    for k, v in items.items():
        boards.append({"id": v.get("_id"), "name": v.get("name"), "urlName": v.get("urlName"), "url": f"https://feedback.vrchat.com/{v.get('urlName')}"})
    if boards:
        await valkey.set("canny_boards", json.dumps(boards))
        logger.info(f"Discovered {len(boards)} boards")
    return boards

async def poll_board_recursive(valkey, limiter, board):
    """
    Crawls all posts on a board by following pagination
    """
    board_url = board["url"]
    page = 1
    total_indexed = 0

    while True:
        url = f"{board_url}?sort=new&page={page}"
        logger.info(f"Crawling {url}...")
        await limiter.acquire()
        data = await fetch_canny_data(url)
        if not data: break

        posts = extract_board_posts(data)
        if not posts: break

        for p in posts:
            uname = p.get("postURLName")
            if not uname: continue

            # Board view might not have full title/score in Redux if it's the simplified list
            # But usually Canny includes them in the first batch
            full_post = data.get("posts", {}).get(p.get("boardID", ""), {}).get(uname)
            if not full_post:
                # Try simplified post info from the list
                full_post = p

            title = full_post.get("title") or uname
            score = full_post.get("score", 0)
            status = full_post.get("status", "open")
            details = full_post.get("details", "")
            p_url = f"https://feedback.vrchat.com/{board['urlName']}/p/{uname}"

            await valkey.hset("canny_search_index", uname, json.dumps({
                "title": title,
                "details": details,
                "url": p_url,
                "score": score,
                "status": status,
                "board": board["name"]
            }))
            total_indexed += 1

        # Check hasNextPage in Redux state
        # Usually inside data['postQueries'][query_key]['hasNextPage']
        has_next = False
        queries = data.get("postQueries", {})
        for q in queries.values():
            if isinstance(q, dict) and q.get("hasNextPage"):
                has_next = True
                break

        if not has_next: break
        page += 1
        # Prevent infinite loop or too many requests
        if page > 500: break

    logger.info(f"Board {board['name']} crawl complete. Indexed {total_indexed} posts.")

async def poller_loop():
    logger.info("Poller started")
    valkey = get_valkey_client(); limiter = get_global_limiter(valkey)

    # Run full crawl once on startup
    boards = await discover_boards(valkey, limiter)
    for b in boards:
        await poll_board_recursive(valkey, limiter, b)

    while True:
        try:
            # 1. Periodically check for new boards
            boards = await discover_boards(valkey, limiter)

            # 2. Track indexed posts (the ones users specifically requested)
            indexed = await valkey.smembers("indexed_post_urls")
            logger.info(f"Polling {len(indexed)} indexed posts...")
            for url in indexed:
                # same logic as before...
                pass

            # 3. Quick check of board front pages for new posts
            for b in boards:
                await limiter.acquire()
                data = await fetch_canny_data(f"{b['url']}?sort=new")
                for p in extract_board_posts(data):
                    uname = p.get("postURLName")
                    if uname and not await valkey.exists(f"post_cache_lite:{uname}"):
                        await valkey.set(f"post_cache_lite:{uname}", "1")
                        # Add to index
                        # ...
        except: logger.exception("Poller error")
        await asyncio.sleep(300)

if __name__ == "__main__": asyncio.run(poller_loop())
