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

async def poll_board_recursive(valkey, limiter, board, force=False, progress_callback=None):
    board_url = board["url"]
    page = 1
    total_indexed = 0
    logger.info(f"Starting crawl for board {board['name']}")

    if not force:
        last_crawl = await valkey.get(f"last_board_crawl:{board['id']}")
        if last_crawl and (time.time() - float(last_crawl)) < 600:
            logger.info(f"Skipping board {board['name']}, already crawled recently.")
            return total_indexed

    while True:
        # Use batchSize=100 to maximize discovery, but Canny's SPA structure
        # often ignores page parameters on initial HTML fetch.
        url = f"{board_url}?sort=new&batchSize=100&page={page}"
        await limiter.acquire()
        data = await fetch_canny_data(url)
        if not data: break

        posts = extract_board_posts(data)
        if not posts:
            logger.warning(f"No posts found on page {page} for board {board['name']}")
            break

        for p in posts:
            uname = p.get("postURLName")
            if not uname: continue

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
            created_iso = full_post.get("created", "")
            created_ts = 0
            try:
                dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
                created_ts = int(dt.timestamp())
            except: pass

            p_url = f"https://feedback.vrchat.com/{board['urlName']}/p/{uname}"

            pid = full_post.get("_id")
            if pid:
                old_json = await valkey.get(f"post_cache:{pid}")

                # Verify if this post was already processed in THIS crawl to avoid Canny's loop
                if await valkey.exists(f"crawl_seen:{board['id']}:{pid}"):
                    logger.info(f"Post {uname} already seen in this crawl, stopping board {board['name']}")
                    has_next = False
                    break
                await valkey.set(f"crawl_seen:{board['id']}:{pid}", "1", ex=300)

                author = full_post.get("author", {})
                author_id = author.get("_id")
                if author_id:
                    await valkey.hset("metrics:author_names", author_id, author.get("name", "Unknown"))

                if old_json:
                    old = json.loads(old_json)
                    old_score = old.get("score", 0)
                    old_comments = old.get("commentCount", 0)

                    # Trending metrics
                    delta = (score - old_score) + (comments - old_comments)
                    if delta > 0:
                        week_key = f"metrics:trending:week:{datetime.now().strftime('%Y-%W')}"
                        month_key = f"metrics:trending:month:{datetime.now().strftime('%Y-%m')}"
                        await valkey.zincrby(week_key, delta, uname)
                        await valkey.zincrby(month_key, delta, uname)
                        await valkey.expire(week_key, 604800 * 2)
                        await valkey.expire(month_key, 2592000 * 2)

                    if old.get("status") != status:
                        last_notified_status = await valkey.get(f"notified_status:{pid}")
                        if last_notified_status != status:
                            await valkey.lpush("{discord_jobs}", json.dumps({"type": "status_change", "post": full_post, "old_status": old.get("status"), "url": p_url}))
                            await valkey.set(f"notified_status:{pid}", status)
                            if author_id and status.lower() in ["complete", "completed", "available in future release"]:
                                await valkey.zincrby("metrics:author_milestones", 1, author_id)

                    current_milestone = score // 25
                    last_notified_milestone = await valkey.get(f"notified_milestone:{pid}")
                    last_milestone = int(last_notified_milestone) if last_notified_milestone else (old_score // 25)
                    if current_milestone > last_milestone:
                        await valkey.lpush("{discord_jobs}", json.dumps({"type": "vote_progress", "post": full_post, "url": p_url}))
                        await valkey.set(f"notified_milestone:{pid}", str(current_milestone))
                        if author_id:
                            milestone_delta = current_milestone - last_milestone
                            await valkey.zincrby("metrics:author_milestones", milestone_delta, author_id)
                else:
                    if author_id:
                        await valkey.zincrby("metrics:author_posts", 1, author_id)
                        milestones = score // 25
                        if status.lower() in ["complete", "completed", "available in future release"]:
                            milestones += 1
                        if milestones > 0:
                            await valkey.zincrby("metrics:author_milestones", milestones, author_id)

                    # Initial discovery: don't notify unless it's new enough?
                    # User said: "polling once again after reach 25+ milestone, bot trigger 25+ milestone embed feed once again"
                    # We should probably only notify if it's actually a new event for us.
                    # For historical crawl, we probably don't want to notify.
                    # But for "Index this Canny", we DO want to notify (that's handled by worker though).

                    # Set initial notified states to prevent duplicate notifications on next poll
                    await valkey.set(f"notified_status:{pid}", status)
                    await valkey.set(f"notified_milestone:{pid}", str(score // 25))

                    if score >= 25:
                        await valkey.sadd("indexed_post_urls", p_url)
                        await valkey.hset(f"post_indexer_info:{p_url}", mapping={"name": "System Discovery", "icon": ""})

                await valkey.set(f"post_cache:{pid}", json.dumps(full_post))
                await valkey.set(f"post_full_cache:{p_url}", json.dumps(full_post), ex=86400)
                if not await valkey.exists(f"next_poll:{p_url}"):
                    await valkey.set(f"next_poll:{p_url}", time.time() + get_polling_interval(full_post))
                    await valkey.incr("stats:polling_queue_size")

            await valkey.hset("canny_search_index", uname, json.dumps({
                "title": title,
                "details": details,
                "url": p_url,
                "score": score,
                "status": status,
                "comments": comments,
                "board": board["name"],
                "created": created_ts
            }))
            total_indexed += 1
            if progress_callback: await progress_callback(1)

        has_next = False
        queries = data.get("postQueries", {})
        for q in queries.values():
            if isinstance(q, dict) and q.get("hasNextPage"):
                has_next = True
                break

        if not has_next: break
        page += 1
        if page > 5000: break

    await valkey.set(f"last_board_crawl:{board['id']}", str(time.time()), ex=600)
    # Clean up crawl_seen
    async for key in valkey.scan_iter(f"crawl_seen:{board['id']}:*"):
        await valkey.delete(key)
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
    post = extract_post_from_data(data, url_name)
    if not post: return None
    pid = post.get("_id")
    old_json = await valkey.get(f"post_cache:{pid}")
    score = post.get("score", 0); status = post.get("status", "open")
    comments = post.get("commentCount", 0)

    author = post.get("author", {})
    author_id = author.get("_id")
    if author_id:
        await valkey.hset("metrics:author_names", author_id, author.get("name", "Unknown"))

    if old_json:
        old = json.loads(old_json); old_score = old.get("score", 0); old_status = old.get("status")
        old_comments = old.get("commentCount", 0)

        # Trending metrics
        delta = (score - old_score) + (comments - old_comments)
        if delta > 0:
            week_key = f"metrics:trending:week:{datetime.now().strftime('%Y-%W')}"
            month_key = f"metrics:trending:month:{datetime.now().strftime('%Y-%m')}"
            await valkey.zincrby(week_key, delta, url_name)
            await valkey.zincrby(month_key, delta, url_name)
            await valkey.expire(week_key, 604800 * 2)
            await valkey.expire(month_key, 2592000 * 2)

        if old_status != status:
            last_notified_status = await valkey.get(f"notified_status:{pid}")
            if last_notified_status != status:
                await valkey.lpush("{discord_jobs}", json.dumps({"type": "status_change", "post": post, "old_status": old_status, "url": url}))
                await valkey.set(f"notified_status:{pid}", status)
                if author_id and status.lower() in ["complete", "completed", "available in future release"]:
                    await valkey.zincrby("metrics:author_milestones", 1, author_id)

        current_milestone = score // 25
        last_notified_milestone = await valkey.get(f"notified_milestone:{pid}")
        last_milestone = int(last_notified_milestone) if last_notified_milestone else (old_score // 25)
        if current_milestone > last_milestone:
            await valkey.lpush("{discord_jobs}", json.dumps({"type": "vote_progress", "post": post, "url": url}))
            await valkey.set(f"notified_milestone:{pid}", str(current_milestone))
            await valkey.sadd("indexed_post_urls", url)
            if not await valkey.exists(f"post_indexer_info:{url}"):
                await valkey.hset(f"post_indexer_info:{url}", mapping={"name": "System Discovery", "icon": ""})
            if author_id:
                milestone_delta = current_milestone - last_milestone
                await valkey.zincrby("metrics:author_milestones", milestone_delta, author_id)
    else:
        if author_id:
            await valkey.zincrby("metrics:author_posts", 1, author_id)
            milestones = score // 25
            if status.lower() in ["complete", "completed", "available in future release"]:
                milestones += 1
            if milestones > 0:
                await valkey.zincrby("metrics:author_milestones", milestones, author_id)

        await valkey.set(f"notified_status:{pid}", status)
        await valkey.set(f"notified_milestone:{pid}", str(score // 25))

        if score >= 25:
            await valkey.sadd("indexed_post_urls", url)
            await valkey.hset(f"post_indexer_info:{url}", mapping={"name": "System Discovery", "icon": ""})

    await valkey.set(f"post_cache:{pid}", json.dumps(post))
    await valkey.set(f"post_full_cache:{url}", json.dumps(post), ex=86400)
    created_iso = post.get("created", ""); created_ts = 0
    try:
        dt = datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        created_ts = int(dt.timestamp())
    except: pass

    await valkey.hset("canny_search_index", url_name, json.dumps({
        "title": post.get("title"), "details": post.get("details", ""), "url": url,
        "score": score, "status": status, "comments": post.get("commentCount", 0),
        "board": post.get("board", {}).get("name"), "created": created_ts
    }))
    return post

async def poller_loop():
    logger.info("Poller started")
    valkey = get_valkey_client(); limiter = get_global_limiter(valkey)
    boards = await discover_boards(valkey, limiter)
    for b in boards:
        asyncio.create_task(poll_board_recursive(valkey, limiter, b))

    while True:
        try:
            boards = await discover_boards(valkey, limiter)
            for b in boards:
                await limiter.acquire()
                data = await fetch_canny_data(f"{b['url']}?sort=new")
                posts = extract_board_posts(data)
                for p in posts:
                    uname = p.get("postURLName")
                    if not uname: continue
                    p_url = f"https://feedback.vrchat.com/{b['urlName']}/p/{uname}"
                    if await valkey.exists(f"post_cache_lite:{uname}"):
                        break
                    await valkey.set(f"post_cache_lite:{uname}", "1", ex=86400*7)
                    full_p = await poll_post(valkey, limiter, p_url, uname)
                    if full_p:
                        await valkey.set(f"next_poll:{p_url}", time.time() + get_polling_interval(full_p))
                        await valkey.incr("stats:polling_queue_size")

            async for key in valkey.scan_iter("next_poll:*"):
                url = key.split("next_poll:")[1]
                nxt = await valkey.get(key)
                if nxt and float(nxt) <= time.time():
                    parts = url.split("/")
                    if "p" in parts:
                        name = parts[parts.index("p") + 1]
                        p = await poll_post(valkey, limiter, url, name)
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
