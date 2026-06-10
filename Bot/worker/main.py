import asyncio
import json
import logging
import os
import sys
import aiohttp
import discord
import redis
import redis.asyncio as redis_async
discord.VoiceClient.warn_nacl = False

class VoiceFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "voice will NOT be supported" not in msg and "davey is not installed" not in msg

logging.getLogger('discord.client').addFilter(VoiceFilter())
logging.getLogger('discord.gateway').addFilter(VoiceFilter())

import time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, get_active_guilds
from Bot.shared.rate_limit import get_global_limiter, get_guild_limiter
from Bot.worker.embeds import create_canny_embed, create_canny_view
from Bot.shared.canny import fetch_canny_data, extract_post_from_data, archive_url

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

class Worker:
    def __init__(self):
        self.valkey = get_valkey_client()
        self.global_limiter = get_global_limiter(self.valkey)
        self.guild_limiter = get_guild_limiter(self.valkey)
        t = os.getenv("DISCORD_TOKEN")
        self.token = t.strip() if t else None
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
            try:
                res = await self.valkey.brpop(["{discord_jobs}_priority", "{discord_jobs}"], timeout=5)
                if not res: continue
                job = json.loads(res[1])

                cached_post = await self.valkey.get(f"post_full_cache:{job['url']}")
                if cached_post:
                    post = json.loads(cached_post)
                else:
                    await self.global_limiter.acquire()
                    data = await fetch_canny_data(job["url"])
                    parts = job["url"].split("/")
                    uname = parts[parts.index("p") + 1] if "p" in parts else None
                    post = extract_post_from_data(data, uname)

                if not post: continue

                if job["type"] == "index_confirm":
                    gid = job["guild_id"]; cid = job["channel_id"]
                    await archive_url(job["url"])
                    if job.get("original_message_id") and job.get("purge", True): await self.purge_message(job.get("original_channel_id", cid), job["original_message_id"], gid)
                    lang = await self.valkey.hget(f"guild_config:{gid}", "language") or "English"
                    embed = create_canny_embed(post, user_info={"type": "indexed", "name": job["user_name"], "icon": job["user_icon"]}, lang=lang)
                    await self.send_request("POST", f"/channels/{cid}/messages", {"embeds": [embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))}, gid)

                    for oid in await get_active_guilds(self.valkey):
                        if str(oid) == str(gid): continue
                        cfg = await self.valkey.hgetall(f"guild_config:{oid}")
                        mode = cfg.get("mode", "global")
                        if mode == "global" and cfg.get("status_channel"):
                            await self.valkey.sadd(f"guild_indexed_posts:{oid}", job["url"])
                            oemb = create_canny_embed(post, user_info={"type": "indexed", "name": "Global Request", "icon": None}, lang=cfg.get("language", "English"))
                            await self.send_request("POST", f"/channels/{cfg['status_channel']}/messages", {"embeds": [oemb.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))}, oid)

                elif job["type"] == "check_status":
                    gid = job.get("guild_id")
                    lang = "English"
                    if gid: lang = await self.valkey.hget(f"guild_config:{gid}", "language") or "English"
                    embed = create_canny_embed(post, lang=lang)
                    await self.send_request("POST", f"/channels/{job['channel_id']}/messages", {"embeds": [embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))}, gid)

                elif job["type"] in ["status_change", "vote_progress"]:
                    await self.valkey.incr(f"stats:{job['type']}:{time.strftime('%Y-%m')}")
                    for gid in await get_active_guilds(self.valkey):
                        cfg = await self.valkey.hgetall(f"guild_config:{gid}")
                        mode = cfg.get("mode", "global")
                        is_indexed = await self.valkey.sismember(f"guild_indexed_posts:{gid}", job["url"])
                        if is_indexed or mode == "global":
                            if not is_indexed:
                                status = post.get("status", "open").lower()
                                score = post.get("score", 0)
                                if status == "closed" and score <= 1:
                                    continue
                                if status == "needs more information" and score < 5:
                                    continue

                            chan = cfg.get("status_channel")
                            if chan:
                                if mode == "global": await self.valkey.sadd(f"guild_indexed_posts:{gid}", job["url"])
                                lang = cfg.get("language") or "English"
                                emb = create_canny_embed(post, old_status=job.get("old_status"), lang=lang)
                                await self.send_request("POST", f"/channels/{chan}/messages", {"embeds": [emb.to_dict()], "components": self.view_to_components(create_canny_view(job["url"]))}, gid)
            except (redis.exceptions.TimeoutError, asyncio.TimeoutError): continue
            except Exception: logger.exception("Worker error")

    def view_to_components(self, view):
        cs = []
        for i in view.children:
            if isinstance(i, discord.ui.Button): cs.append({"type": 2, "style": 5, "label": i.label, "url": i.url})
        return [{"type": 1, "components": cs}] if cs else []

if __name__ == "__main__": asyncio.run(Worker().run())
