import asyncio
import json
import logging
import os
import sys
import aiohttp
import discord
import valkey
import valkey.asyncio as valkey_async
discord.VoiceClient.warn_nacl = False

class VoiceFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "voice will NOT be supported" not in msg and "davey is not installed" not in msg

logging.getLogger('discord.client').addFilter(VoiceFilter())
logging.getLogger('discord.gateway').addFilter(VoiceFilter())

import time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, get_active_guilds, refresh_valkey_cluster
from Bot.shared.rate_limit import get_global_limiter, get_guild_limiter
from Bot.worker.embeds import create_canny_embed, create_canny_view
from Bot.shared.canny import fetch_canny_data, extract_post_from_data, archive_url, extract_post_url_name

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

    async def send_request(self, method, endpoint, payload=None, guild_id=None, files=None):
        await self.global_limiter.acquire()
        if guild_id: await self.guild_limiter.acquire(str(guild_id))
        url = f"{self.base_url}{endpoint}"
        headers = {"Authorization": f"Bot {self.token}"}

        async with aiohttp.ClientSession() as session:
            if files:
                data = aiohttp.FormData()
                data.add_field('payload_json', json.dumps(payload))
                for name, (fname, content, ftype) in files.items():
                    data.add_field(name, content, filename=fname, content_type=ftype)
                async with session.request(method, url, headers=headers, data=data) as resp:
                    if resp.status == 429:
                        retry_after = (await resp.json()).get("retry_after", 1)
                        await asyncio.sleep(retry_after)
                        return await self.send_request(method, endpoint, payload, guild_id, files)
                    return resp.status, await resp.json() if resp.status != 204 else None
            else:
                headers["Content-Type"] = "application/json"
                async with session.request(method, url, headers=headers, json=payload) as resp:
                    if resp.status == 429:
                        retry_after = (await resp.json()).get("retry_after", 1)
                        await asyncio.sleep(retry_after)
                        return await self.send_request(method, endpoint, payload, guild_id)
                    return resp.status, await resp.json() if resp.status != 204 else None

    async def purge_message(self, channel_id, message_id, guild_id=None):
        await self.send_request("DELETE", f"/channels/{channel_id}/messages/{message_id}", guild_id=guild_id)

    async def delayed_repush(self, queue_name, job, wait):
        await asyncio.sleep(wait)
        await self.valkey.lpush(queue_name, json.dumps(job))

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
                    if isinstance(data, dict) and data.get("error") in ["rate_limit", "server_error", "timeout"]:
                        err = data.get("error")
                        wait = 10800 if err == "timeout" else (3600 if err == "server_error" else 1800)
                        logger.warning(f"Worker encountered {err.replace('_', ' ').capitalize()} for {job['url']}. Repushing in {wait//60} minutes.")
                        asyncio.create_task(self.delayed_repush(res[0], job, wait))
                        continue
                    uname = extract_post_url_name(job["url"])
                    post = extract_post_from_data(data, uname)

                if not post: continue

                if job["type"] == "index_confirm":
                    gid = job["guild_id"]; cid = job["channel_id"]
                    await archive_url(job["url"])

                    is_already_indexed = await self.valkey.sismember("indexed_post_urls", job["url"])

                    if not is_already_indexed:
                        await self.valkey.sadd("indexed_post_urls", job["url"])
                        await self.valkey.hset(f"post_indexer_info:{job['url']}", mapping={"name": job["user_name"], "icon": job.get("user_icon") or "", "guild_id": str(gid)})

                    if gid:
                        await self.valkey.sadd(f"guild_indexed_posts:{gid}", job["url"])
                        if cid:
                            user_type = "requested" if is_already_indexed else "indexed"

                            if job.get("original_message_id") and job.get("purge", True): await self.purge_message(job.get("original_channel_id", cid), job["original_message_id"], gid)
                            lang = await self.valkey.hget(f"guild_config:{gid}", "language") or "English"
                            embed = create_canny_embed(post, user_info={"type": user_type, "name": job["user_name"], "icon": job["user_icon"]}, lang=lang)
                            files = self.get_milestone_file(post)
                            await self.send_request("POST", f"/channels/{cid}/messages", {"embeds": [embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"], lang=lang))}, gid, files=files)

                    if not is_already_indexed:
                        score = post.get("score", 0)
                        status = post.get("status", "open").lower()
                        # Broadcast rule: manual indexing always broadcasts, system discovery requires 25+ votes or non-Open
                        is_manual = (job.get("user_id", 0) != 0)
                        meets_criteria = (score >= 25 or status != "open")

                        if is_manual or meets_criteria:
                            for oid in await get_active_guilds(self.valkey):
                                if gid and str(oid) == str(gid): continue
                                cfg = await self.valkey.hgetall(f"guild_config:{oid}")
                                mode = cfg.get("mode", "global")
                                if mode == "global" and cfg.get("status_channel"):
                                    # Secondary check for suppression rules (skip noise)
                                    if status == "closed" and score <= 1: continue
                                    if status == "needs more information" and score < 5: continue

                                    await self.valkey.sadd(f"guild_indexed_posts:{oid}", job["url"])
                                    olang = cfg.get("language", "English")

                                    # Mask indexing source unless it's a manual request from that guild (already handled in manual section)
                                    u_name = job["user_name"]
                                    u_icon = job.get("user_icon")
                                    if u_name != "System Discovery":
                                        u_name = "Indexed by Global Mode"
                                        u_icon = None

                                    oemb = create_canny_embed(post, user_info={"type": "indexed", "name": u_name, "icon": u_icon}, lang=olang)
                                    files = self.get_milestone_file(post)
                                    await self.send_request("POST", f"/channels/{cfg['status_channel']}/messages", {"embeds": [oemb.to_dict()], "components": self.view_to_components(create_canny_view(job["url"], lang=olang))}, oid, files=files)

                elif job["type"] == "check_status":
                    gid = job.get("guild_id")
                    lang = "English"
                    if gid: lang = await self.valkey.hget(f"guild_config:{gid}", "language") or "English"
                    embed = create_canny_embed(post, lang=lang)
                    files = self.get_milestone_file(post)
                    await self.send_request("POST", f"/channels/{job['channel_id']}/messages", {"embeds": [embed.to_dict()], "components": self.view_to_components(create_canny_view(job["url"], lang=lang))}, gid, files=files)

                elif job["type"] in ["status_change", "vote_progress"]:
                    await self.valkey.incr(f"stats:{job['type']}:{time.strftime('%Y-%m')}")

                    is_truly_indexed = await self.valkey.sismember("indexed_post_urls", job["url"])
                    if not is_truly_indexed:
                        # Skip unless it reached 25 votes and was auto-added (handled in poller)
                        continue

                    indexer = await self.valkey.hgetall(f"post_indexer_info:{job['url']}")
                    orig_gid = indexer.get("guild_id")

                    for gid in await get_active_guilds(self.valkey):
                        cfg = await self.valkey.hgetall(f"guild_config:{gid}")
                        mode = cfg.get("mode", "global")
                        is_indexed = await self.valkey.sismember(f"guild_indexed_posts:{gid}", job["url"])
                        if is_indexed or mode == "global":
                            status = post.get("status", "open").lower()
                            score = post.get("score", 0)

                            if not is_indexed:
                                # Milestone check for global feed
                                if score < 25 and status == "open":
                                    continue
                                # Suppression rules
                                if status == "closed" and score <= 1:
                                    continue
                                if status == "needs more information" and score < 5:
                                    continue

                            chan = cfg.get("status_channel")
                            if chan:
                                if mode == "global": await self.valkey.sadd(f"guild_indexed_posts:{gid}", job["url"])
                                lang = cfg.get("language") or "English"

                                u_name = indexer.get("name", "System Discovery")
                                u_icon = indexer.get("icon")
                                if str(gid) != orig_gid and u_name != "System Discovery":
                                    u_name = "Indexed by Global Mode"
                                    u_icon = None

                                user_info = {"type": "indexed", "name": u_name, "icon": u_icon}
                                emb = create_canny_embed(post, old_status=job.get("old_status"), user_info=user_info, lang=lang)
                                files = self.get_milestone_file(post)
                                await self.send_request("POST", f"/channels/{chan}/messages", {"embeds": [emb.to_dict()], "components": self.view_to_components(create_canny_view(job["url"], lang=lang))}, gid, files=files)
            except (valkey.exceptions.TimeoutError, asyncio.TimeoutError): continue
            except (valkey.exceptions.DataError, valkey.exceptions.ClusterError):
                logger.warning("Valkey cluster map error detected, refreshing...")
                await refresh_valkey_cluster(self.valkey)
                await asyncio.sleep(1)
            except Exception: logger.exception("Worker error")

    def view_to_components(self, view):
        cs = []
        for i in view.children:
            if isinstance(i, discord.ui.Button):
                comp = {"type": 2, "label": i.label}
                if i.url:
                    comp["style"] = 5
                    comp["url"] = i.url
                else:
                    comp["style"] = i.style.value if hasattr(i.style, "value") else 1
                    if i.custom_id: comp["custom_id"] = i.custom_id
                cs.append(comp)
        return [{"type": 1, "components": cs}] if cs else []

    def get_milestone_file(self, post):
        status = post.get("status", "").lower()
        score = post.get("score", 0)
        fnames = []
        if status in ["complete", "completed", "available in future release"]:
            fnames = ["Completed.png"]
        elif score >= 100:
            fnames = ["100_plus_milestone.png", "50_plus_milestone.png", "25_plus_milestone.png"]
        elif score >= 50:
            fnames = ["50_plus_milestone.png", "25_plus_milestone.png"]
        elif score >= 25:
            fnames = ["25_plus_milestone.png"]

        for fname in fnames:
            path = os.path.join("Img", fname)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    return {"file": (fname, f.read(), "image/png")}
        return None

if __name__ == "__main__": asyncio.run(Worker().run())
