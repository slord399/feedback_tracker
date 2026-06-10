import json
from datetime import datetime

class MockValkey:
    def __init__(self):
        self.data = {
            "metrics:author_names": {"a1": "Alice", "a2": "Bob"},
            "metrics:author_posts": {"a1": 10, "a2": 5},
            "metrics:author_milestones": {"a1": 2, "a2": 1},
            "canny_search_index": {
                "post1": json.dumps({"title": "Fix bug 1"}),
                "post2": json.dumps({"title": "Feature 1"})
            }
        }
        week_key = f"metrics:trending:week:{datetime.now().strftime('%Y-%W')}"
        self.data[week_key] = {"post1": 50, "post2": 30}

    async def hget(self, name, key):
        return self.data.get(name, {}).get(key)

    async def zrevrange(self, name, start, stop, withscores=False):
        d = self.data.get(name, {})
        sorted_items = sorted(d.items(), key=lambda x: x[1], reverse=True)
        items = sorted_items[start:stop+1]
        if withscores:
            return items
        return [i[0] for i in items]

async def test_metrics_logic():
    valkey = MockValkey()
    category = "trending_week"

    if category == "trending_week":
        key = f"metrics:trending:week:{datetime.now().strftime('%Y-%W')}"
        data = await valkey.zrevrange(key, 0, 19, withscores=True)
        desc = ""
        for i, (uname, score) in enumerate(data, 1):
            p_raw = await valkey.hget("canny_search_index", uname)
            title = json.loads(p_raw).get('title', uname) if p_raw else uname
            desc += f"{i}. **{title}** (+{int(score)} activity)\n"
        print("Trending Week Output:")
        print(repr(desc))
        assert "Fix bug 1" in desc
        assert "+50 activity" in desc

    category = "top_authors"
    if category == "top_authors":
        data = await valkey.zrevrange("metrics:author_posts", 0, 19, withscores=True)
        desc = ""
        for i, (aid, count) in enumerate(data, 1):
            name = await valkey.hget("metrics:author_names", aid) or aid
            desc += f"{i}. **{name}**: {int(count)} posts\n"
        print("Top Authors Output:")
        print(repr(desc))
        assert "Alice" in desc
        assert "10 posts" in desc

    category = "top_milestones"
    if category == "top_milestones":
        data = await valkey.zrevrange("metrics:author_milestones", 0, 19, withscores=True)
        desc = ""
        for i, (aid, count) in enumerate(data, 1):
            name = await valkey.hget("metrics:author_names", aid) or aid
            desc += f"{i}. **{name}**: {int(count)} milestones (25+ votes)\n"
        print("Top Milestones Output:")
        print(repr(desc))
        assert "Alice" in desc
        assert "2 milestones" in desc

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_metrics_logic())
