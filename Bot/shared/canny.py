import aiohttp
import json
from bs4 import BeautifulSoup
import logging
import re

logger = logging.getLogger(__name__)

async def fetch_canny_data(url: str):
    """
    Fetches a Canny URL and extracts the JSON data from window.Canny or window.__REDUX_STATE__ or window.__data
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as response:
            if response.status != 200:
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
                        logger.error(f"Error parsing {var_name} in {url}: {e}")

    return None

def extract_post_from_data(data, post_url_name=None):
    if not data:
        return None
    posts = data.get("posts", {})
    if isinstance(posts, dict):
        for board_id, board_posts in posts.items():
            if isinstance(board_posts, dict):
                if post_url_name and post_url_name in board_posts:
                    post_data = board_posts[post_url_name]
                    if isinstance(post_data, dict) and "title" in post_data:
                        return post_data
                for key, val in board_posts.items():
                    if isinstance(val, dict) and val.get("urlName") == post_url_name:
                        return val
    # Alternative structure for window.__data
    idea_post = data.get("ideaPost", {})
    if isinstance(idea_post, dict):
        for board_id, board_data in idea_post.items():
            if isinstance(board_data, dict):
                if post_url_name in board_data:
                    return board_data[post_url_name]
    return None

def extract_board_posts(data):
    if not data:
        return []
    post_queries = data.get("postQueries", {})
    all_posts = []
    for query_key, query_data in post_queries.items():
        if isinstance(query_data, dict):
            posts_list = query_data.get("posts", [])
            for p in posts_list:
                all_posts.append(p)
    return all_posts
