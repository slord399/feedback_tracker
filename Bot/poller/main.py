import asyncio
import time
import json
import logging
import sys
import os
from datetime import datetime, timezone

# Silence specific voice warning
class VoiceFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "voice will NOT be supported" not in msg and "davey is not installed" not in msg

logging.getLogger('discord.client').addFilter(VoiceFilter())
logging.getLogger('discord.gateway').addFilter(VoiceFilter())

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client
from Bot.shared.canny import fetch_canny_data, fetch_canny_api, extract_post_from_data, extract_board_posts, extract_api_posts
from Bot.shared.rate_limit import get_global_limiter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("poller")

async def process_post_data(valkey, post, board_info, p_url, uname, force_notify=False):
    """
    Processes a single post's data: updates cache, metrics, trending, and triggers notifications.
    """
    pid = post.get("_id")
    if not pid:
        return None

    title = post.get("title") or uname
    score = post.get("score") or 0
    status = post.get("status") or "open"
    details = post.get("details") or ""
    comments = post.get("commentCount") or 0
    created_iso = post.get("created") or ""
    created_ts = 0
    try:
        dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        created_ts = int(dt.timestamp())
    except:
        pass

    old_json = await valkey.get(f"post_cache:{pid}")
    author = post.get("author") or {}
    author_id = author.get("_id")
    just_processed = False

    if author_id:
        await valkey.hset("metrics:author_names", author_id, author.get("name", "Unknown"))
        if not await valkey.sismember("metrics:processed_posts", pid):
            await valkey.zincrby("metrics:author_posts", 1, author_id)
            milestones_list = [25, 50, 100]
            milestones_reached = len([m for m in milestones_list if score >= m])
            if status.lower() in ["complete", "completed", "available in future release"]:
                milestones_reached += 1
            if milestones_reached > 0:
                await valkey.zincrby("metrics:author_milestones", milestones_reached, author_id)
            await valkey.sadd("metrics:processed_posts", pid)
            just_processed = True

    if old_json:
        old = json.loads(old_json)
        old_score = old.get("score", 0)
        old_comments = old.get("commentCount", 0)
        old_status = old.get("status")

        delta = (score - old_score) + (comments - old_comments)
        if delta > 0:
            week_key = f"metrics:trending:week:{datetime.now().strftime('%Y-%W')}"
            month_key = f"metrics:trending:month:{datetime.now().strftime('%Y-%m')}"
            await valkey.zincrby(week_key, delta, uname)
            await valkey.zincrby(month_key, delta, uname)
            await valkey.expire(week_key, 604800 * 2)
            await valkey.expire(month_key, 2592000 * 2)

        if old_status != status:
            last_notified_status = await valkey.get(f"notified_status:{pid}")
            if last_notified_status != status or force_notify:
                if not await valkey.sismember("indexed_post_urls", p_url):
                    if score >= 25 or status.lower() != "open":
                        await valkey.sadd("indexed_post_urls", p_url)
                        if not await valkey.exists(f"post_indexer_info:{p_url}"):
                            await valkey.hset(f"post_indexer_info:{p_url}", mapping={"name": "System Discovery", "icon": "", "guild_id": ""})
                await valkey.lpush("{discord_jobs}", json.dumps({"type": "status_change", "post": post, "old_status": old_status, "url": p_url}))
                await valkey.set(f"notified_status:{pid}", status)
                if author_id and status.lower() in ["complete", "completed", "available in future release"] and not just_processed:
                    await valkey.zincrby("metrics:author_milestones", 1, author_id)

        milestones = [25, 50, 100]
        current_milestone_val = max([m for m in milestones if score >= m] + [0])
        last_notified_milestone = await valkey.get(f"notified_milestone:{pid}")
        last_milestone_val = 0
        if last_notified_milestone:
            try:
                lv = int(last_notified_milestone)
                if lv >= 25: last_milestone_val = lv
                else: # old style index
                    if lv >= 4: last_milestone_val = 100
                    elif lv >= 2: last_milestone_val = 50
                    elif lv >= 1: last_milestone_val = 25
            except: pass
        else:
            last_milestone_val = max([m for m in milestones if old_score >= m] + [0])

        if current_milestone_val > last_milestone_val or (force_notify and current_milestone_val >= 25):
            if not await valkey.sismember("indexed_post_urls", p_url):
                await valkey.sadd("indexed_post_urls", p_url)
                if not await valkey.exists(f"post_indexer_info:{p_url}"):
                    await valkey.hset(f"post_indexer_info:{p_url}", mapping={"name": "System Discovery", "icon": "", "guild_id": ""})
            await valkey.lpush("{discord_jobs}", json.dumps({"type": "vote_progress", "post": post, "url": p_url}))
            await valkey.set(f"notified_milestone:{pid}", str(current_milestone_val))
            if author_id and not just_processed:
                new_passed = [m for m in milestones if score >= m and m > last_milestone_val]
                await valkey.zincrby("metrics:author_milestones", len(new_passed), author_id)
    else:
        await valkey.set(f"notified_status:{pid}", status)
        milestones_list = [25, 50, 100]
        current_milestone_val = max([m for m in milestones_list if score >= m] + [0])
        await valkey.set(f"notified_milestone:{pid}", str(current_milestone_val))

        if score >= 25 or status.lower() != "open":
            if not await valkey.sismember("indexed_post_urls", p_url):
                if not await valkey.exists(f"indexing_pushed:{p_url}"):
                    await valkey.set(f"indexing_pushed:{p_url}", "1", ex=3600)
                    await valkey.lpush("{discord_jobs}_priority", json.dumps({
                        "type": "index_confirm", "url": p_url, "guild_id": 0, "channel_id": 0,
                        "user_id": 0, "user_name": "System Discovery", "user_icon": "", "purge": False
                    }))

    await valkey.set(f"post_cache:{pid}", json.dumps(post))
    await valkey.set(f"post_full_cache:{p_url}", json.dumps(post), ex=86400)
    if not await valkey.exists(f"next_poll:{p_url}"):
        await valkey.set(f"next_poll:{p_url}", time.time() + get_polling_interval(post))
        await valkey.incr("stats:polling_queue_size")

    await valkey.hset("canny_search_index", uname, json.dumps({
        "title": title, "details": details, "url": p_url,
        "score": score, "status": status, "comments": comments,
        "board": board_info["name"], "created": created_ts
    }))
    return post

async def discover_boards(valkey, limiter):
    logger.debug("Discovering boards...")
    await limiter.acquire()
    data = await fetch_canny_data("https://feedback.vrchat.com/")
    if isinstance(data, dict) and data.get("error") in ["rate_limit", "server_error", "timeout"]:
        err = data.get("error")
        wait = 10800 if err == "timeout" else (3600 if err == "server_error" else 1800)
        logger.warning(f"{err.replace('_', ' ').capitalize()} during board discovery. Sleeping for {wait//60} minutes.")
        await asyncio.sleep(wait)
        return []
    if not data:
        logger.error("Failed to fetch boards data")
        return []
    items = data.get("boards", {}).get("items", {})
    boards = []
    for k, v in items.items():
        boards.append({
            "id": v.get("_id"),
            "name": v.get("name"),
            "urlName": v.get("urlName"),
            "url": f"https://feedback.vrchat.com/{v.get('urlName')}",
            "postCount": v.get("postCount", 0)
        })
    if boards:
        await valkey.set("canny_boards", json.dumps(boards))
        logger.debug(f"Discovered {len(boards)} boards")
    return boards

async def poll_board_recursive(valkey, limiter, board, force=False, progress_callback=None):
    total_indexed = 0
    sorts = ["newest", "score", "oldest"]
    # Multi-dimensional approach to bypass 500-post limit by splitting into subsets
    statuses = ["", "open", "planned", "in-progress", "complete", "closed"]
    logger.debug(f"Starting crawl for board {board['name']}")

    if not force:
        last_crawl = await valkey.get(f"last_board_crawl:{board['id']}")
        if last_crawl and (time.time() - float(last_crawl)) < 10800:
            logger.info(f"Skipping board {board['name']}, already crawled recently.")
            return total_indexed

    for status in statuses:
        for sort in sorts:
            payload = {
                "__canny_requestID": f"poller-crawl-{board['urlName']}-{status}-{sort}",
                "__host": "feedback.vrchat.com",
                "boardURLNames": [board["urlName"]],
                "currentBoard": board["urlName"],
                "pages": 50, # up to 500 posts per query
                "sort": sort,
                "status": status,
            }
            await limiter.acquire()
            data = await fetch_canny_api("/api/posts/get", payload)
            if isinstance(data, dict) and data.get("error") in ["rate_limit", "server_error", "timeout"]:
                err = data.get("error")
                wait = 10800 if err == "timeout" else (3600 if err == "server_error" else 1800)
                logger.warning(f"{err.replace('_', ' ').capitalize()} during API crawl for {board['name']}. Sleeping for {wait//60} minutes.")
                await asyncio.sleep(wait)
                continue
            if not data: continue

            posts = extract_api_posts(data)
            if not posts:
                continue

            for p in posts:
                uname = p.get("urlName")
                if not uname: continue
                pid = p.get("_id")
                if not pid: continue

                # Check crawl_seen only within a single sort/status set to discover new posts,
                # but process_post_data is idempotent for metrics.
                # Actually, crawl_seen should be per-board-crawl to avoid loop issues
                # but we want to count total_indexed uniquely per full board crawl.
                if await valkey.exists(f"crawl_seen:{board['id']}:{pid}"):
                    continue
                await valkey.set(f"crawl_seen:{board['id']}:{pid}", "1", ex=3600)

                p_url = f"https://feedback.vrchat.com/{board['urlName']}/p/{uname}"
                await process_post_data(valkey, p, board, p_url, uname)

                total_indexed += 1
                if total_indexed % 100 == 0:
                    logger.info(f"Board {board['name']} progress: {total_indexed} posts discovered...")
                if progress_callback: await progress_callback(1)

    await valkey.set(f"last_board_crawl:{board['id']}", str(time.time()), ex=10800)
    await valkey.hset("stats:board_posts", board["name"], total_indexed)
    # Clean up crawl_seen
    async for key in valkey.scan_iter(f"crawl_seen:{board['id']}:*"):
        await valkey.delete(key)
    if total_indexed > 0:
        logger.info(f"Board {board['name']} crawl complete. Total: {total_indexed}")
    return total_indexed

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

async def poll_post(valkey, limiter, url, url_name):
    await limiter.acquire()
    data = await fetch_canny_data(url)
    if isinstance(data, dict) and data.get("error") in ["rate_limit", "server_error", "timeout"]:
        return data.get("error")
    post = extract_post_from_data(data, url_name)
    if not post: return None

    board_info = post.get("board") or {}
    if not board_info.get("name"):
        # Try to extract board from URL if missing in data
        parts = url.split("/")
        if len(parts) > 3:
            board_info["urlName"] = parts[3]
            board_info["name"] = parts[3].replace("-", " ").title()

    return await process_post_data(valkey, post, board_info, url, url_name)

async def poller_loop():
    logger.info("Poller started")
    valkey = get_valkey_client(); limiter = get_global_limiter(valkey)
    boards = await discover_boards(valkey, limiter)
    for b in boards:
        asyncio.create_task(poll_board_recursive(valkey, limiter, b, force=True))

    while True:
        try:
            boards = await discover_boards(valkey, limiter)
            # Newest Sweep
            for b in boards:
                payload = {
                    "__canny_requestID": f"poller-sweep-{b['urlName']}",
                    "__host": "feedback.vrchat.com",
                    "boardURLNames": [b["urlName"]],
                    "currentBoard": b["urlName"],
                    "pages": 10, # 100 posts is enough for a frequent sweep
                    "sort": "newest",
                    "status": "",
                }
                await limiter.acquire()
                data = await fetch_canny_api("/api/posts/get", payload)
                if isinstance(data, dict) and data.get("error") in ["rate_limit", "server_error", "timeout"]:
                    err = data.get("error")
                    wait = 10800 if err == "timeout" else (3600 if err == "server_error" else 1800)
                    logger.warning(f"{err.replace('_', ' ').capitalize()} during sweep for {b['name']}. Sleeping for {wait//60} minutes.")
                    await asyncio.sleep(wait)
                    continue

                posts = extract_api_posts(data)
                for p in posts:
                    uname = p.get("urlName")
                    if not uname: continue
                    p_url = f"https://feedback.vrchat.com/{b['urlName']}/p/{uname}"

                    pid = p.get("_id")
                    old_json = await valkey.get(f"post_cache:{pid}")
                    if old_json:
                        old = json.loads(old_json)
                        # Only skip if nothing major changed. API response contains these fields.
                        if (p.get("score") == old.get("score") and
                            p.get("status") == old.get("status") and
                            p.get("commentCount") == old.get("commentCount")):
                            continue

                    # Something changed or it's new
                    await process_post_data(valkey, p, b, p_url, uname)

            # Periodic Refresh for existing indexed posts
            async for key in valkey.scan_iter("next_poll:*"):
                url = key.split("next_poll:")[1]
                nxt = await valkey.get(key)
                if nxt and float(nxt) <= time.time():
                    parts = url.split("/")
                    if "p" in parts:
                        name = parts[parts.index("p") + 1]
                        p = await poll_post(valkey, limiter, url, name)
                        if p in ["rate_limit", "server_error", "timeout"]:
                            wait = 10800 if p == "timeout" else (3600 if p == "server_error" else 1800)
                            logger.warning(f"{p.replace('_', ' ').capitalize()} polling {url}. Sleeping for {wait//60} minutes.")
                            await asyncio.sleep(wait)
                            continue
                        if p: await valkey.set(key, time.time() + get_polling_interval(p))
                        else:
                            fail_count = await valkey.incr(f"poll_fail_count:{url}")
                            if fail_count > 10:
                                await valkey.delete(key)
                                await valkey.decr("stats:polling_queue_size")
                                await valkey.srem("indexed_post_urls", url)
                                logger.warning(f"Stopped polling {url} after {fail_count} failures")
                            else:
                                await valkey.set(key, time.time() + 3600)
        except Exception:
            logger.exception("Poller loop error")
        await asyncio.sleep(60)

if __name__ == "__main__": asyncio.run(poller_loop())
