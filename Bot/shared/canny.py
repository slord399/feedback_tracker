import aiohttp
import json
from bs4 import BeautifulSoup
import logging
import re

logger = logging.getLogger(__name__)

_session = None
async def get_session():
    global _session
    if _session is None or _session.closed:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        _session = aiohttp.ClientSession(headers=headers)
    return _session

async def fetch_canny_data(url: str, retry_fallback=True):
    """
    Fetches a Canny URL and extracts the JSON data from window.Canny or window.__REDUX_STATE__ or window.__data
    """
    session = await get_session()
    try:
        async with session.get(url) as response:
            if response.status == 404 and retry_fallback and "/p/" in url:
                parts = url.split("/p/")
                if len(parts) == 2 and parts[0].count("/") > 2:
                    base = "/".join(parts[0].split("/")[:3])
                    fallback_url = f"{base}/p/{parts[1]}"
                    if fallback_url != url:
                        logger.info(f"404 for {url}, trying fallback: {fallback_url}")
                        return await fetch_canny_data(fallback_url, retry_fallback=False)

            if response.status != 200:
                if response.status == 429:
                    logger.warning(f"Rate limited (429) fetching {url}")
                    return {"error": "rate_limit"}
                if response.status == 502:
                    logger.error(f"Failed to fetch {url}, status: 502")
                    return {"error": "server_error"}
                if response.status == 404:
                    logger.info(f"Failed to fetch {url}, status: 404")
                else:
                    logger.error(f"Failed to fetch {url}, status: {response.status}")
                return None
            html = await response.text()

        soup = BeautifulSoup(html, 'html.parser')
        scripts = soup.find_all('script')

        for script in scripts:
            if script.string:
                for var_name in ["window.Canny", "window.__REDUX_STATE__", "window.__data"]:
                    if var_name + " =" in script.string:
                        try:
                            start_idx = script.string.find("{")
                            end_idx = script.string.rfind("}")
                            if start_idx != -1 and end_idx != -1:
                                data_str = script.string[start_idx:end_idx+1]
                                # Replace undefined with null for JSON compatibility
                                data_str = data_str.replace(":undefined", ":null")
                                return json.loads(data_str)
                        except Exception as e:
                            logger.info(f"Error parsing {var_name} in {url}: {e}")
    except Exception as e:
        logger.info(f"Fetch error {url}: {e}")
    return None

def extract_post_from_data(data, post_url_name=None):
    if not data:
        return None

    # URL names can be long or encoded
    import urllib.parse
    possible_names = {post_url_name, urllib.parse.unquote(post_url_name or "")} if post_url_name else {None}

    posts = data.get("posts", {})
    if isinstance(posts, dict):
        for board_id, board_posts in posts.items():
            if isinstance(board_posts, dict):
                for name in possible_names:
                    if name and name in board_posts:
                        post_data = board_posts[name]
                        if isinstance(post_data, dict) and "title" in post_data:
                            return post_data
                for key, val in board_posts.items():
                    if isinstance(val, dict) and val.get("urlName") in possible_names:
                        return val
    # Alternative structure for window.__data
    idea_post = data.get("ideaPost", {})
    if isinstance(idea_post, dict):
        for board_id, board_data in idea_post.items():
            if isinstance(board_data, dict):
                for name in possible_names:
                    if name and name in board_data:
                        return board_data[name]
    return None

def extract_board_posts(data):
    if not data:
        return []
    post_queries = data.get("postQueries", {})
    all_posts = []
    seen_ids = set()
    for query_key, query_data in post_queries.items():
        if isinstance(query_data, dict):
            posts_list = query_data.get("posts", [])
            for p in posts_list:
                # We need to distinguish posts properly.
                # Canny sometimes returns just references.
                pid = p.get("_id") or p.get("postURLName")
                if pid and pid not in seen_ids:
                    all_posts.append(p)
                    seen_ids.add(pid)
    return all_posts

async def archive_url(url: str):
    """
    Requests Wayback Machine to archive the URL
    """
    archive_api = f"https://web.archive.org/save/{url}"
    headers = {"User-Agent": "VRChatStatusBot/1.0"}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(archive_api) as response:
                return response.status
    except Exception:
        return None

async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
