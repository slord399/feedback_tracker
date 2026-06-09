import asyncio
import json
import logging
import os
import sys
import aiohttp
import discord
import time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, get_active_guilds
from Bot.shared.rate_limit import get_global_limiter, get_guild_limiter
from Bot.worker.embeds import create_canny_embed, create_canny_view
from Bot.shared.canny import fetch_canny_data, extract_post_from_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

class Worker:
    def __init__(self):
        self.valkey = get_valkey_client()
        self.global_limiter = get_global_limiter(self.valkey)
        self.guild_limiter = get_guild_limiter(self.valkey)
        self.token = os.getenv("DISCORD_TOKEN")
        self.base_url = "https://discord.com/api/v10"

    async def send_request(self, method, endpoint, payload=None, guild_id=None):
        await self.global_limiter.acquire()
        if guild_id: await self.guild_limiter.acquire(str(guild_id))
        url = f"{self.base_url}{endpoint}"
        headers = {"Authorization": f"Bot {self.token}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, headers=headers, json=payload) as resp:
                if resp.status == 429:
                    retry_after = (await resp.json()).get("retry_after", 1)
                    await asyncio.sleep(retry_after)
                    return await self.send_request(method, endpoint, payload, guild_id)
                return resp.status, await resp.json() if resp.status != 204 else None

    async def purge_message(self, channel_id, message_id, guild_id=None):
        await self.send_request("DELETE", f"/channels/{channel_id}/messages/{message_id}", guild_id=guild_id)

    async def run(self):
        logger.info("Worker started")
        while True:
            job_data = self.valkey.brpop("discord_jobs", timeout=5)
            if not job_data: continue
            job = json.loads(job_data[1])
            try:
                if job["type"] in ["index_confirm", "check_status", "status_change", "vote_progress"]:
                    data = await fetch_canny_data(job["url"])
                    parts = job["url"].split("/")
                    url_name = parts[parts.index("p") + 1] if "p" in parts else None
                    post = extract_post_from_data(data, url_name)
                    if not post: continue

                    if job["type"] == "index_confirm":
                        guild_id = job["guild_id"]; channel_id = job["channel_id"]
                        if job.get("original_message_id"): await self.purge_message(channel_id, job["original_message_id"], guild_id)
                        lang = self.valkey.hget(f"guild_config:{guild_id}", "language") or "English"
                        user_info = {"type": "indexed", "name": job["user_name"], "icon": job["user_icon"]}
                        embed = create_canny_embed(post, user_info=user_info, lang=lang)
                        await self.send_request("POST", f"/channels/{channel_id}/messages", {"embeds": [embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))}, guild_id)

                        # Global notification
                        active_guilds = get_active_guilds(self.valkey)
                        for other_id in active_guilds:
                            if str(other_id) == str(guild_id): continue
                            cfg = self.valkey.hgetall(f"guild_config:{other_id}")
                            if cfg.get("mode") == "global":
                                chan = cfg.get("status_channel")
                                if chan:
                                    other_embed = create_canny_embed(post, user_info={"type": "indexed", "name": "Global User", "icon": None}, lang=cfg.get("language", "English"))
                                    await self.send_request("POST", f"/channels/{chan}/messages", {"embeds": [other_embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))}, other_id)

                    elif job["type"] == "check_status":
                        embed = create_canny_embed(post)
                        await self.send_request("POST", f"/channels/{job['channel_id']}/messages", {"embeds": [embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))})

                    elif job["type"] in ["status_change", "vote_progress"]:
                        self.valkey.incr(f"stats:{job['type']}:{time.strftime('%Y-%m')}")
                        active_guilds = get_active_guilds(self.valkey)
                        for guild_id in active_guilds:
                            if self.valkey.sismember(f"guild_indexed_posts:{guild_id}", job["url"]):
                                cfg = self.valkey.hgetall(f"guild_config:{guild_id}")
                                chan = cfg.get("status_channel")
                                if chan:
                                    embed = create_canny_embed(post, old_status=job.get("old_status"), lang=cfg.get("language", "English"))
                                    await self.send_request("POST", f"/channels/{chan}/messages", {"embeds": [embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))}, guild_id)
            except Exception as e: logger.exception("Worker error")

    def view_to_components(self, view):
        comps = []
        for item in view.children:
            if isinstance(item, discord.ui.Button): comps.append({"type": 2, "style": 5, "label": item.label, "url": item.url})
        return [{"type": 1, "components": comps}] if comps else []

if __name__ == "__main__":
    asyncio.run(Worker().run())
