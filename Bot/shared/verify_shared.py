import asyncio
import sys
import os

# Add Bot to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from Bot.shared.canny import fetch_canny_data, extract_post_from_data
from Bot.shared.localization import get_localizer

async def test_canny():
    url = "https://feedback.vrchat.com/sdk-bug-reports/p/avatar-descriptor-wrong-unity-version"
    print(f"Fetching {url}...")
    data = await fetch_canny_data(url)
    if data:
        post = extract_post_from_data(data, "avatar-descriptor-wrong-unity-version")
        if post:
            print(f"Successfully extracted post: {post.get('title')}")
            print(f"Status: {post.get('status')}")
            print(f"Score: {post.get('score')}")
        else:
            print("Failed to extract post from data.")
            # print(data.keys())
    else:
        print("Failed to fetch data.")

def test_localization():
    loc = get_localizer()
    print(f"Languages: {loc.languages}")
    print(f"Category (EN): {loc.get('category', 'English')}")
    print(f"Indexed by (EN): {loc.get('indexed_by', 'English', user='Jules')}")

if __name__ == "__main__":
    test_localization()
    asyncio.run(test_canny())
