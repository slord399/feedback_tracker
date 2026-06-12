import discord
discord.VoiceClient.warn_nacl = False
import logging
import os, sys, json, aiohttp, re, time
from datetime import datetime
import asyncio

# Silence specific voice warning
class VoiceFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "voice will NOT be supported" not in msg and "davey is not installed" not in msg
logging.getLogger('discord.client').addFilter(VoiceFilter())
logging.getLogger('discord.gateway').addFilter(VoiceFilter())

from discord import app_commands, ui
from discord.ext import commands, tasks

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, register_guild, get_all_guilds
from Bot.shared.localization import get_localizer
from Bot.shared.canny import fetch_canny_data, extract_post_from_data, extract_board_posts
from Bot.shared.rate_limit import get_global_limiter
from Bot.poller.main import discover_boards, poll_board_recursive
from Bot.worker.embeds import create_canny_embed, create_canny_view

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")

def clean_url(url):
    return url.rstrip(')')

SHEET_URL = "https://docs.google.com/spreadsheets/d/17sYQbx154noc42UO1vvm3VVNLdnSguTb6j-J5mszvtQ/edit?usp=sharing"

USER_APP_INSTALLS = app_commands.AppInstallationType(guild=True, user=True)
USER_APP_CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
GUILD_ONLY_INSTALLS = app_commands.AppInstallationType(guild=True)
GUILD_ONLY_CONTEXTS = app_commands.AppCommandContext(guild=True)

class GuildSelect(ui.Select):
    def __init__(self, bot, guilds, canny_url, message_id=None, lang="English"):
        options = [discord.SelectOption(label=g['name'], value=str(g['id'])) for g in guilds[:25]]
        placeholder = bot.localizer.get("select_server_placeholder", lang)
        super().__init__(placeholder=placeholder, options=options)
        self.bot = bot
        self.canny_url = canny_url
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        gid = self.values[0]
        valkey = self.bot.valkey
        await valkey.sadd("indexed_post_urls", self.canny_url)
        await valkey.sadd(f"guild_indexed_posts:{gid}", self.canny_url)
        await valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{self.canny_url}")
        await valkey.set(f"last_index_selection:{interaction.user.id}", str(gid), ex=300)
        await valkey.set(f"next_poll:{self.canny_url}", 0)
        cfg = await valkey.hgetall(f"guild_config:{gid}")
        target_cid = int(cfg.get("status_channel") or interaction.channel_id)
        await valkey.lpush("{discord_jobs}_priority", json.dumps({"type": "index_confirm", "url": self.canny_url, "guild_id": int(gid), "channel_id": target_cid, "original_channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url), "original_message_id": self.message_id, "purge": False}))
        lang = "English"
        if interaction.guild_id: lang = await self.bot.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
        msg = self.bot.localizer.get("indexed_msg", lang)
        await interaction.response.edit_message(content=msg, view=None)

class ResultSelect(ui.Select):
    def __init__(self, posts, lang="English", localizer=None):
        options = []
        for p in posts:
            val = p['url']
            if len(val) > 100:
                parts = val.split("/")
                if "p" in parts: val = "NAME:" + parts[parts.index("p") + 1][:95]
                else: val = val[-100:]
            options.append(discord.SelectOption(label=p['title'][:100], value=val, description=p['url'][-50:]))
        placeholder = localizer.get("select_post_placeholder", lang) if localizer else "Select a post..."
        super().__init__(placeholder=placeholder, options=options)
    async def callback(self, interaction: discord.Interaction):
        self.view.selected_url = self.values[0]
        await interaction.response.defer()

class LanguageSelect(ui.Select):
    def __init__(self, valkey):
        langs = ["English", "Deutsch", "Española", "François", "Italian", "polski", "Portuguese do Brazil", "русский", "中文（简体）", "中文（繁體）", "日本語", "한국어"]
        super().__init__(placeholder="Select language...", options=[discord.SelectOption(label=l, value=l) for l in langs])
        self.valkey = valkey

    async def callback(self, interaction: discord.Interaction):
        lang = self.values[0]
        await self.valkey.hset(f"guild_config:{interaction.guild_id}", "language", lang)
        await interaction.response.edit_message(content=f"Language set to {lang}.", view=None)

class MetricsSelectionView(ui.View):
    def __init__(self, bot, category_prefix, interaction, lang="English"):
        super().__init__(timeout=60)
        self.bot = bot
        self.category_prefix = category_prefix
        self.original_interaction = interaction
        loc = bot.localizer
        if category_prefix == "trending":
            self.add_item(ui.Button(label=loc.get("weekly_label", lang), style=discord.ButtonStyle.primary, custom_id="trending_week"))
            self.add_item(ui.Button(label=loc.get("monthly_label", lang), style=discord.ButtonStyle.primary, custom_id="trending_month"))
        else:
            self.add_item(ui.Button(label=loc.get("posts_label", lang), style=discord.ButtonStyle.primary, custom_id="top_authors"))
            self.add_item(ui.Button(label=loc.get("milestones_label", lang), style=discord.ButtonStyle.primary, custom_id="top_milestones"))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        category = interaction.data.get("custom_id")
        await self.bot._send_metrics(interaction, category)
        self.stop()
        return True

class SearchView(ui.View):
    def __init__(self, results, page=0, bot=None, ephemeral=True, lang="English"):
        super().__init__()
        self.results = results
        self.page = page
        self.bot = bot
        self.selected_url = None
        self.ephemeral = ephemeral
        self.lang = lang
        self.update_components()

    def update_components(self):
        self.clear_items()
        loc = self.bot.localizer
        start = self.page * 5
        end = start + 5
        current_posts = self.results[start:end]
        if current_posts:
            self.add_item(ResultSelect(current_posts, lang=self.lang, localizer=loc))

        prev_btn = ui.Button(label=loc.get("prev_label", self.lang), style=discord.ButtonStyle.grey, disabled=(self.page == 0))
        prev_btn.callback = self.prev
        self.add_item(prev_btn)

        next_btn = ui.Button(label=loc.get("next_label", self.lang), style=discord.ButtonStyle.grey, disabled=(end >= len(self.results)))
        next_btn.callback = self.next
        self.add_item(next_btn)

        post_pub = ui.Button(label=loc.get("post_public_label", self.lang), style=discord.ButtonStyle.primary)
        post_pub.callback = self.post_public
        self.add_item(post_pub)

        post_priv = ui.Button(label=loc.get("post_private_label", self.lang), style=discord.ButtonStyle.primary)
        post_priv.callback = self.post_private
        self.add_item(post_priv)

        index_btn = ui.Button(label=loc.get("index_label", self.lang), style=discord.ButtonStyle.green)
        index_btn.callback = self.index_selected
        self.add_item(index_btn)

    async def get_real_url(self, val):
        if val.startswith("NAME:"):
            uname = val[5:]
            raw = await self.bot.valkey.hget("canny_search_index", uname)
            if raw:
                return json.loads(raw).get('url')
        return val

    async def prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self.update_components()
        await self.update_msg(interaction)

    async def next(self, interaction: discord.Interaction):
        self.page = min((len(self.results)-1)//5, self.page + 1)
        self.update_components()
        await self.update_msg(interaction)

    async def post_public(self, interaction: discord.Interaction):
        await self._do_post(interaction, False)

    async def post_private(self, interaction: discord.Interaction):
        await self._do_post(interaction, True)

    async def _do_post(self, interaction: discord.Interaction, ephemeral: bool):
        if not self.selected_url:
            msg = self.bot.localizer.get("select_post_msg", self.lang)
            return await interaction.response.send_message(msg, ephemeral=True)
        url = await self.get_real_url(self.selected_url)
        await interaction.response.defer(ephemeral=ephemeral)
        data = await fetch_canny_data(url)
        parts = url.split("/")
        uname = parts[parts.index("p") + 1] if "p" in parts else None
        post = extract_post_from_data(data, uname)
        if not post: return await interaction.followup.send("Post not found.", ephemeral=ephemeral)
        lang = "English"
        if interaction.guild_id: lang = await self.bot.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
        embed = create_canny_embed(post, lang=lang)
        await interaction.followup.send(embed=embed, view=create_canny_view(url), ephemeral=ephemeral)

    async def index_selected(self, interaction: discord.Interaction):
        if not self.selected_url: return await interaction.response.send_message("Select a post.", ephemeral=True)
        url = await self.get_real_url(self.selected_url)
        lgid = await self.bot.valkey.get(f"last_index_selection:{interaction.user.id}")
        all_guilds = await get_all_guilds(self.bot.valkey)
        if lgid:
            gid = int(lgid)
            if any(str(g['id']) == str(gid) for g in all_guilds):
                await self.bot.valkey.sadd("indexed_post_urls", url)
                await self.bot.valkey.sadd(f"guild_indexed_posts:{gid}", url)
                await self.bot.valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{url}")
                await self.bot.valkey.set(f"next_poll:{url}", 0)
                cfg = await self.bot.valkey.hgetall(f"guild_config:{gid}")
                target_cid = int(cfg.get("status_channel") or interaction.channel_id)
                await self.bot.valkey.lpush("{discord_jobs}_priority", json.dumps({"type": "index_confirm", "url": url, "guild_id": gid, "channel_id": target_cid, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url), "purge": False}))
                return await interaction.response.send_message(self.bot.localizer.get("indexed_msg", self.lang), ephemeral=True)
        if not all_guilds:
            return await interaction.response.send_message(self.bot.localizer.get("bot_not_in_servers", self.lang), ephemeral=True)
        view = ui.View()
        view.add_item(GuildSelect(self.bot, all_guilds, url, lang=self.lang))
        await interaction.response.send_message(self.bot.localizer.get("select_server_placeholder", self.lang), view=view, ephemeral=True)

    async def update_msg(self, interaction):
        start = self.page * 5
        end = start + 5
        loc = self.bot.localizer
        title = loc.get("search_results_title", self.lang)
        embed = discord.Embed(title=title, color=discord.Color.blue())
        for r in self.results[start:end]:
            created = f"<t:{int(r.get('created', 0))}:R>" if r.get('created') else "Unknown"
            status_localized = loc.get(r.get('status', 'open').lower(), self.lang)
            embed.add_field(name=r['title'][:256], value=f"[Link](<{r['url']}>)\n**{loc.get('status', self.lang)}:** {status_localized} | **{loc.get('votes', self.lang)}:** {r.get('score', 0)} | **{loc.get('created', self.lang)}:** {created}", inline=False)
        await interaction.response.edit_message(embed=embed, view=self, content=None)

class SearchFilterView(ui.View):
    def __init__(self, bot, ephemeral=True, lang="English"):
        super().__init__()
        self.bot = bot
        self.ephemeral = ephemeral
        self.lang = lang
        self.boards = []
        self.statuses = []
        self.update_components()

    def update_components(self):
        self.clear_items()
        loc = self.bot.localizer

        board_select = ui.Select(placeholder=loc.get("select_boards", self.lang), min_values=0, max_values=5, options=[
            discord.SelectOption(label=loc.get("feature requests", self.lang), value="feature-requests"),
            discord.SelectOption(label=loc.get("bug reports", self.lang), value="bug-reports"),
            discord.SelectOption(label=loc.get("sdk bug & feature requests", self.lang), value="sdk-bug-reports"),
            discord.SelectOption(label=loc.get("udon", self.lang), value="udon"),
            discord.SelectOption(label=loc.get("open beta", self.lang), value="open-beta")
        ])
        board_select.callback = self.select_boards_callback
        self.add_item(board_select)

        status_select = ui.Select(placeholder=loc.get("select_statuses", self.lang), min_values=0, max_values=5, options=[
            discord.SelectOption(label=loc.get("open", self.lang), value="open"),
            discord.SelectOption(label=loc.get("tracked", self.lang), value="tracked"),
            discord.SelectOption(label=loc.get("planned", self.lang), value="planned"),
            discord.SelectOption(label=loc.get("in progress", self.lang), value="in-progress"),
            discord.SelectOption(label=loc.get("complete", self.lang), value="complete"),
            discord.SelectOption(label=loc.get("available in future release", self.lang), value="available in future release")
        ])
        status_select.callback = self.select_statuses_callback
        self.add_item(status_select)

        execute_btn = ui.Button(label=loc.get("enter_metrics_execute", self.lang), style=discord.ButtonStyle.green)
        execute_btn.callback = self.execute_search
        self.add_item(execute_btn)

    async def select_boards_callback(self, interaction: discord.Interaction):
        self.boards = interaction.data.get("values", [])
        await interaction.response.defer()

    async def select_statuses_callback(self, interaction: discord.Interaction):
        self.statuses = interaction.data.get("values", [])
        await interaction.response.defer()

    async def execute_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchQueryModal(self, self.lang))

class SearchQueryModal(ui.Modal):
    def __init__(self, filter_view, lang):
        loc = filter_view.bot.localizer
        super().__init__(title=loc.get('search_metrics_title', lang))
        self.filter_view = filter_view
        self.lang = lang

        self.query = ui.TextInput(label=loc.get('query_keywords_label', lang), placeholder=loc.get('query_keywords_placeholder', lang), required=False)
        self.min_votes = ui.TextInput(label=loc.get('min_votes_label', lang), placeholder='0', default='0', required=False)
        self.max_votes = ui.TextInput(label=loc.get('max_votes_label', lang), placeholder='9999', required=False)
        self.min_comments = ui.TextInput(label=loc.get('min_comments_label', lang), placeholder='0', default='0', required=False)
        self.date_range = ui.TextInput(label=loc.get('date_range_label', lang), placeholder=loc.get('date_range_placeholder', lang), required=False)

        self.add_item(self.query); self.add_item(self.min_votes); self.add_item(self.max_votes); self.add_item(self.min_comments); self.add_item(self.date_range)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=self.filter_view.ephemeral)
        valkey = self.filter_view.bot.valkey
        res = []
        cursor = 0
        q = self.query.value.lower()
        min_v = int(self.min_votes.value) if self.min_votes.value.isdigit() else 0
        max_v = int(self.max_votes.value) if self.max_votes.value.isdigit() else 999999
        min_c = int(self.min_comments.value) if self.min_comments.value.isdigit() else 0
        min_ts = 0
        max_ts = 2147483647
        if self.date_range.value and " to " in self.date_range.value:
            try:
                d_parts = self.date_range.value.split(" to ")
                min_ts = int(datetime.strptime(d_parts[0].strip(), "%Y-%m-%d").timestamp())
                max_ts = int(datetime.strptime(d_parts[1].strip(), "%Y-%m-%d").timestamp())
            except: pass
        while True:
            cursor, data = await valkey.hscan("canny_search_index", cursor=cursor, count=100)
            for k, v in data.items():
                p = json.loads(v)
                if not q or q in p['title'].lower() or q in p.get('details', '').lower():
                    if self.filter_view.boards and not any(b in p['url'] for b in self.filter_view.boards): continue
                    if self.filter_view.statuses and p.get('status', '').lower() not in self.filter_view.statuses: continue
                    vts = p.get('score', 0)
                    cmt = p.get('comments', 0)
                    crt = p.get('created', 0)
                    if vts < min_v or vts > max_v or cmt < min_c or crt < min_ts or crt > max_ts:
                        continue
                    res.append(p)
            if cursor == 0 or len(res) > 500: break
        res.sort(key=lambda x: x.get('score', 0), reverse=True)
        loc = self.filter_view.bot.localizer
        if not res: return await interaction.followup.send("No results.", ephemeral=self.filter_view.ephemeral)
        view = SearchView(res, bot=self.filter_view.bot, ephemeral=self.filter_view.ephemeral, lang=self.lang)
        title = loc.get("search_results_title", self.lang)
        embed = discord.Embed(title=title, color=discord.Color.blue())
        for r in res[:5]:
            created = f"<t:{int(r.get('created', 0))}:R>" if r.get('created') else "Unknown"
            status_localized = loc.get(r.get('status', 'open').lower(), self.lang)
            embed.add_field(name=r['title'][:256], value=f"[Link](<{r['url']}>)\n**{loc.get('status', self.lang)}:** {status_localized} | **{loc.get('votes', self.lang)}:** {r.get('score', 0)} | **{loc.get('created', self.lang)}:** {created}", inline=False)
        await interaction.followup.send(embed=embed, view=view, ephemeral=self.filter_view.ephemeral)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        sid = os.getenv("SHARD_ID")
        sc = os.getenv("TOTAL_SHARDS")
        super().__init__(command_prefix="!", intents=intents, shard_id=int(sid) if sid else None, shard_count=int(sc) if sc else None)
        self.valkey = get_valkey_client()
        self.localizer = get_localizer()

    async def on_guild_join(self, guild):
        await register_guild(self.valkey, guild)
        logger.info(f"Joined new guild: {guild.name} ({guild.id})")

    async def sync_guilds_to_valkey(self):
        await self.wait_until_ready()
        for guild in self.guilds:
            await register_guild(self.valkey, guild)
        logger.info(f"Synced {len(self.guilds)} guilds to Valkey.")

    async def setup_hook(self):
        logger.info(f"Setting up Shard {self.shard_id}...")
        self.loop.create_task(self.sync_guilds_to_valkey())

        cmd_force = app_commands.Command(name="force_polling", description="Force polling of all Canny posts", callback=self.force_polling)
        cmd_force.guild_only = True
        self.tree.add_command(cmd_force)
        cmd_index = app_commands.ContextMenu(name="Index this canny", callback=self.index_this_canny)
        cmd_index.allowed_contexts = USER_APP_CONTEXTS
        cmd_index.allowed_installs = USER_APP_INSTALLS
        self.tree.add_command(cmd_index)

        cmd_status = app_commands.ContextMenu(name="Check canny status", callback=self.check_canny_status)
        cmd_status.allowed_contexts = USER_APP_CONTEXTS
        cmd_status.allowed_installs = USER_APP_INSTALLS
        self.tree.add_command(cmd_status)

        cmd_hour = app_commands.ContextMenu(name="Post what I indexed in hour", callback=self.post_indexed_hour)
        cmd_hour.allowed_contexts = USER_APP_CONTEXTS
        cmd_hour.allowed_installs = USER_APP_INSTALLS
        self.tree.add_command(cmd_hour)

        cmd_trending = app_commands.ContextMenu(name="Check Trending Canny", callback=self.ctx_trending)
        cmd_trending.allowed_contexts = USER_APP_CONTEXTS
        cmd_trending.allowed_installs = USER_APP_INSTALLS
        self.tree.add_command(cmd_trending)

        cmd_authors = app_commands.ContextMenu(name="Check Canny Author Metrics", callback=self.ctx_authors)
        cmd_authors.allowed_contexts = USER_APP_CONTEXTS
        cmd_authors.allowed_installs = USER_APP_INSTALLS
        self.tree.add_command(cmd_authors)

        if self.shard_id is None or self.shard_id == 0:
            await self.tree.sync()
            self.auto_sync_localization.start()
            asyncio.create_task(self.sync_localization())
        self.update_activity.start()
        logger.info("Setup hook complete.")

    @tasks.loop(minutes=30)
    async def update_activity(self):
        try:
            idx = await self.valkey.scard("indexed_post_urls")
            tot = await self.valkey.hlen("canny_search_index")
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name="feedback.vrchat.com",
                state=f"Party Info : {int(idx)} of {int(tot)}",
                details="Tracking Index Progress",
                party={'id': 'canny', 'size': [int(idx), int(tot)]}
            )
            await self.change_presence(activity=activity)
        except Exception:
            pass

    @tasks.loop(hours=1)
    async def auto_sync_localization(self): await self.sync_localization()

    async def sync_localization(self):
        logger.info("Syncing localization...")
        try:
            base = SHEET_URL.split("/edit")[0]
            gid = SHEET_URL.split("gid=")[1].split("&")[0] if "gid=" in SHEET_URL else None
            csv_url = f"{base}/export?format=csv"
            if gid:
                csv_url += f"&gid={gid}"
            async with aiohttp.ClientSession() as session:
                async with session.get(csv_url) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        if "string_name" in content:
                            with open("Locale/template.csv", "w", encoding="utf-8") as f:
                                f.write(content)
                            self.localizer.load()
                            logger.info("Localization synced.")
                            return True
                    else: logger.error(f"Failed to sync localization: HTTP {resp.status}")
        except Exception: logger.exception("Localization sync error")
        return False

    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        cfg = await self.valkey.hgetall(f"guild_config:{message.guild.id}")
        status_chan = cfg.get("status_channel")
        is_react = await self.valkey.sismember(f"guild_react_channels:{message.guild.id}", str(message.channel.id))
        is_status = (str(message.channel.id) == status_chan)
        if is_react or is_status:
            urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
            canny_found = False
            for u in urls:
                u = clean_url(u)
                if "canny.io" in u or "feedback.vrchat.com" in u:
                    canny_found = True
                    await self.valkey.set(f"next_poll:{u}", 0)
                    target_cid = int(status_chan or message.channel.id)
                    await self.valkey.lpush("{discord_jobs}_priority", json.dumps({"type": "index_confirm", "url": u, "guild_id": message.guild.id, "channel_id": target_cid, "original_channel_id": message.channel.id, "user_id": message.author.id, "user_name": message.author.name, "user_icon": str(message.author.display_avatar.url), "original_message_id": message.id, "purge": is_status}))
            if canny_found and is_status:
                try: await message.edit(suppress=True)
                except: pass

    async def index_this_canny(self, interaction: discord.Interaction, message: discord.Message):
        lang = "English"
        if interaction.guild_id:
            lang = await self.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
        urls = [clean_url(u) for u in re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)]
        url = next((u for u in urls if "canny.io" in u or "feedback.vrchat.com" in u), None)
        if not url:
            return await interaction.response.send_message(self.localizer.get("no_url_found", lang), ephemeral=True)
        lgid = await self.valkey.get(f"last_index_selection:{interaction.user.id}")
        all_guilds = await get_all_guilds(self.valkey)
        if lgid:
            gid = int(lgid)
            if any(str(g['id']) == str(gid) for g in all_guilds):
                await self.valkey.sadd("indexed_post_urls", url)
                await self.valkey.sadd(f"guild_indexed_posts:{gid}", url)
                await self.valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{url}")
                await self.valkey.set(f"next_poll:{url}", 0)
                cfg = await self.valkey.hgetall(f"guild_config:{gid}")
                target_cid = int(cfg.get("status_channel") or interaction.channel_id)
                await self.valkey.lpush("{discord_jobs}_priority", json.dumps({"type": "index_confirm", "url": url, "guild_id": gid, "channel_id": target_cid, "original_channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url), "original_message_id": message.id, "purge": False}))
                try: await message.edit(suppress=True)
                except: pass
                return await interaction.response.send_message(self.localizer.get("indexed_msg", lang), ephemeral=True)
        if not all_guilds:
            return await interaction.response.send_message(self.localizer.get("bot_not_in_servers", lang), ephemeral=True)
        view = ui.View()
        view.add_item(GuildSelect(self, all_guilds, url, message.id, lang=lang))
        await interaction.response.send_message(self.localizer.get("select_server_placeholder", lang), view=view, ephemeral=True)

    async def check_canny_status(self, interaction: discord.Interaction, message: discord.Message):
        lang = "English"
        if interaction.guild_id:
            lang = await self.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
        urls = [clean_url(u) for u in re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)]
        url = next((u for u in urls if "canny.io" in u or "feedback.vrchat.com" in u), None)
        if not url:
            return await interaction.response.send_message(self.localizer.get("no_url_found", lang), ephemeral=True)
        await interaction.response.defer(ephemeral=False)
        data = await fetch_canny_data(url)
        parts = url.split("/")
        uname = parts[parts.index("p") + 1] if "p" in parts else None
        post = extract_post_from_data(data, uname)
        if not post:
            return await interaction.followup.send(self.localizer.get("post_not_found", lang), ephemeral=True)
        lang = "English"
        if interaction.guild_id:
            lang = await self.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
        user_info = {"type": "requested", "name": interaction.user.name, "icon": str(interaction.user.display_avatar.url)}
        embed = create_canny_embed(post, lang=lang, user_info=user_info)
        await interaction.followup.send(embed=embed, view=create_canny_view(url))

    async def post_indexed_hour(self, interaction: discord.Interaction, message: discord.Message):
        idx = await self.valkey.smembers(f"user_indexed_posts:{interaction.user.id}")
        now = time.time()
        urls = []
        for entry in idx:
            ts, url = entry.split("|", 1)
            if now - float(ts) < 3600:
                urls.append(url)
        if not urls:
            return await interaction.response.send_message("No posts indexed in the last hour.", ephemeral=True)

        content = "\n".join([f"<{u}>" for u in urls[:20]])
        await interaction.response.send_message(content)

    async def ctx_trending(self, interaction: discord.Interaction, message: discord.Message):
        lang = "English"
        if interaction.guild_id:
            lang = await self.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
        msg = self.localizer.get("select_timeframe_msg", lang)
        await interaction.response.send_message(msg, view=MetricsSelectionView(self, "trending", interaction, lang=lang), ephemeral=False)

    async def ctx_authors(self, interaction: discord.Interaction, message: discord.Message):
        lang = "English"
        if interaction.guild_id:
            lang = await self.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
        msg = self.localizer.get("select_metric_msg", lang)
        await interaction.response.send_message(msg, view=MetricsSelectionView(self, "authors", interaction, lang=lang), ephemeral=False)

    async def _send_metrics(self, interaction: discord.Interaction, category: str):
        await interaction.response.defer(ephemeral=False)
        embed = discord.Embed(title=f"Metrics: {category.replace('_', ' ').title()}", color=discord.Color.gold())

        if category == "trending_week":
            key = f"metrics:trending:week:{datetime.now().strftime('%Y-%W')}"
            data = await self.valkey.zrevrange(key, 0, 19, withscores=True)
            desc = ""
            for i, (uname, score) in enumerate(data, 1):
                p_raw = await self.valkey.hget("canny_search_index", uname)
                title = json.loads(p_raw).get('title', uname) if p_raw else uname
                desc += f"{i}. **{title}** (+{int(score)} activity)\n"
            embed.description = desc or self.bot.localizer.get("no_data_available", lang)
        elif category == "trending_month":
            key = f"metrics:trending:month:{datetime.now().strftime('%Y-%m')}"
            data = await self.valkey.zrevrange(key, 0, 19, withscores=True)
            desc = ""
            for i, (uname, score) in enumerate(data, 1):
                p_raw = await self.valkey.hget("canny_search_index", uname)
                title = json.loads(p_raw).get('title', uname) if p_raw else uname
                desc += f"{i}. **{title}** (+{int(score)} activity)\n"
            embed.description = desc or self.bot.localizer.get("no_data_available", lang)
        elif category == "top_authors":
            data = await self.valkey.zrevrange("metrics:author_posts", 0, 19, withscores=True)
            desc = ""
            for i, (aid, count) in enumerate(data, 1):
                name = await self.valkey.hget("metrics:author_names", aid) or aid
                desc += f"{i}. **{name}**: {int(count)} posts\n"
            embed.description = desc or self.bot.localizer.get("no_data_available", lang)
        elif category == "top_milestones":
            data = await self.valkey.zrevrange("metrics:author_milestones", 0, 19, withscores=True)
            desc = ""
            for i, (aid, count) in enumerate(data, 1):
                name = await self.valkey.hget("metrics:author_names", aid) or aid
                desc += f"{i}. **{name}**: {int(count)} milestones (25+ votes)\n"
            embed.description = desc or self.bot.localizer.get("no_data_available", lang)

        await interaction.followup.send(embed=embed, ephemeral=False)

    async def force_polling(self, interaction: discord.Interaction):
        if interaction.guild_id != 590756888254349315:
            return await interaction.response.send_message("This command is only available in the admin guild.", ephemeral=True)
        if not interaction.user.guild_permissions.manage_messages:
            return await interaction.response.send_message("Missing 'Manage Messages' permission.", ephemeral=True)

        lang = "English"
        if interaction.guild_id:
            lang = await self.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"

        await interaction.response.send_message(self.localizer.get("polling_started", lang))
        self.loop.create_task(self.do_force_polling(interaction, lang))

    async def do_force_polling(self, interaction, lang):
        valkey = self.valkey
        limiter = get_global_limiter(valkey)
        boards = await discover_boards(valkey, limiter)

        discovered_count = 0
        board_results = {}

        async def progress_callback(n):
            nonlocal discovered_count
            discovered_count += n
            if discovered_count % 1000 == 0:
                msg = self.localizer.get("polling_progress", lang, count=discovered_count)
                try: await interaction.channel.send(msg)
                except: pass

        for b in boards:
            count = await poll_board_recursive(valkey, limiter, b, force=True, progress_callback=progress_callback)
            board_results[b['name']] = count

        summary = self.localizer.get("polling_complete", lang) + "\n"
        for name, count in board_results.items():
            summary += f"- **{name}**: {count}\n"
        try: await interaction.channel.send(summary)
        except: pass

bot = MyBot()

@bot.tree.command(name="search", description="Search Canny posts with interactive filters")
@app_commands.describe(visibility="Set if the results should be visible only to you (default: Ephemeral)")
@app_commands.choices(visibility=[app_commands.Choice(name="Public", value="public"), app_commands.Choice(name="Ephemeral", value="ephemeral")])
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def search(interaction: discord.Interaction, visibility: str = "ephemeral"):
    ephemeral = (visibility == "ephemeral")
    lang = "English"
    if interaction.guild_id:
        lang = await bot.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
    msg = bot.localizer.get("configure_filters_msg", lang)
    await interaction.response.send_message(msg, view=SearchFilterView(bot, ephemeral=ephemeral, lang=lang), ephemeral=True)

@bot.tree.command(name="settings", description="View current server configuration")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def settings(interaction: discord.Interaction):
    cfg = await bot.valkey.hgetall(f"guild_config:{interaction.guild_id}")
    mode = cfg.get("mode", "global").capitalize()
    status_chan = f"<#{cfg.get('status_channel')}>" if cfg.get("status_channel") else "Not set"

    react_chans = await bot.valkey.smembers(f"guild_react_channels:{interaction.guild_id}")
    react_display = ", ".join([f"<#{c}>" for c in react_chans]) if react_chans else "None set"

    lang = cfg.get("language", "English")

    embed = discord.Embed(title=f"Settings for {interaction.guild.name}", color=discord.Color.blue())
    embed.add_field(name="Mode", value=mode, inline=True)
    embed.add_field(name="Language", value=lang, inline=True)
    embed.add_field(name="Status Channel", value=status_chan, inline=False)
    embed.add_field(name="React Channels", value=react_display, inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ping", description="Check Discord API latency")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms")

@bot.tree.command(name="stats", description="View bot and indexing statistics")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
async def stats(interaction: discord.Interaction):
    await interaction.response.defer()
    idx = await bot.valkey.scard("indexed_post_urls")
    tot = await bot.valkey.hlen("canny_search_index")
    polled = await bot.valkey.get("stats:polling_queue_size") or 0
    this_month = time.strftime('%Y-%m')
    status_changes = await bot.valkey.get(f"stats:status_change:{this_month}") or 0
    vote_reports = await bot.valkey.get(f"stats:vote_progress:{this_month}") or 0
    msg = f"**Canny Bot Stats**\nDiscovered Posts: {tot}\nTracked Posts: {idx}\nActive Polling Queue: {polled}\n\n**Activity ({this_month})**\nStatus Updates: {status_changes}\nVote Milestones: {vote_reports}"
    await interaction.followup.send(msg)

@bot.tree.command(name="help", description="Comprehensive guide for the VRChat Canny Bot")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Canny Bot Help Article", color=discord.Color.blue())
    embed.add_field(name="General Commands", value="**/search**: Interactive search.\n**/stats**: Activity metrics.\n**/ping**: Latency check.\n**/credit**: Affiliation and donation info.", inline=False)
    embed.add_field(name="User App Features", value="**Index this canny**: Track link in server.\n**Check canny status**: Get status/votes.\n**Post what I indexed in hour**: Activity summary.", inline=False)
    if interaction.user and getattr(interaction.user, 'guild_permissions', None) and interaction.user.guild_permissions.manage_messages:
        embed.add_field(name="Administrative Commands", value="**/settings**: View current server config.\n**/mode**: Global vs Local.\n**/set_status_channel**: Post updates here.\n**/set_react_channel**: Auto-index links here.\n**/set_language**: Change UI language.\n**/bulk_add**: Scrape channel history.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="credit", description="View bot credits, hosting, and affiliation")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
async def credit(interaction: discord.Interaction):
    msg = "**This bot is not affiliated with VRChat Inc.**\n\nHosted by [VRCβフォース](<https://discord.gg/XJHRXwd>) | [VRChat Group](<https://vrc.group/BETAJP.2222>).\nLocalization: [Google Sheet](<https://docs.google.com/spreadsheets/d/17sYQbx154noc42UO1vvm3VVNLdnSguTb6j-J5mszvtQ/edit?usp=sharing>).\nOpen Source: [GitHub](<https://github.com/slord399/feedback_tracker/>).\nLegal: [Terms of Service](<https://github.com/slord399/feedback_tracker/blob/main/Terms/tos.md>) | [Privacy Policy](<https://github.com/slord399/feedback_tracker/blob/main/Terms/privacy.md>).\nDonations: [X (formerly Twitter)](<https://x.com/slord399/creator-subscriptions/subscribe>) | [Ko-fi](<https://ko-fi.com/tony_lewis>) | [GitHub Sponsors](<https://github.com/sponsors/slord399/>)."
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="metrics", description="View top contributors and trending posts")
@app_commands.describe(category="Select the metrics category")
@app_commands.choices(category=[
    app_commands.Choice(name="Trending Canny of Week", value="trending_week"),
    app_commands.Choice(name="Trending Canny of Month", value="trending_month"),
    app_commands.Choice(name="Top 20 canny author", value="top_authors"),
    app_commands.Choice(name="Top 20 canny author reach milestone", value="top_milestones")
])
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
async def metrics_cmd(interaction: discord.Interaction, category: str):
    await bot._send_metrics(interaction, category)

@bot.tree.command(name="mode", description="Toggle Global or Local indexing mode")
@app_commands.describe(mode="Global: Receive all updates | Local: Receive only updates for posts indexed by this server")
@app_commands.choices(mode=[
    app_commands.Choice(name="Global", value="global"),
    app_commands.Choice(name="Local", value="local")
])
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def mode(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "mode", mode.value)
    await register_guild(bot.valkey, interaction.guild)
    await interaction.response.send_message(f"Mode set to: **{mode.name}**")

@bot.tree.command(name="set_status_channel", description="Set the channel for status updates")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def set_status_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "status_channel", str(channel.id))
    await register_guild(bot.valkey, interaction.guild)
    await interaction.response.send_message("Status channel set.")

@bot.tree.command(name="react_channel", description="Manage channels for auto-indexing")
@app_commands.describe(action="Add or remove a channel", channel="The channel to manage")
@app_commands.choices(action=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove")
])
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def react_channel(interaction: discord.Interaction, action: str, channel: discord.TextChannel):
    if action == "add":
        await bot.valkey.sadd(f"guild_react_channels:{interaction.guild_id}", str(channel.id))
        await register_guild(bot.valkey, interaction.guild)
        await interaction.response.send_message(f"Added <#{channel.id}> to react channels.")
    else:
        await bot.valkey.srem(f"guild_react_channels:{interaction.guild_id}", str(channel.id))
        await interaction.response.send_message(f"Removed <#{channel.id}> from react channels.")

@bot.tree.command(name="bulk_add", description="Index URLs from channel history")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def bulk_add(interaction: discord.Interaction):
    await interaction.response.defer()
    found = 0
    async for msg in interaction.channel.history(limit=100):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', msg.content)
        for u in urls:
            u = clean_url(u)
            if "canny.io" in u or "feedback.vrchat.com" in u:
                await bot.valkey.sadd("indexed_post_urls", u)
                await bot.valkey.sadd(f"guild_indexed_posts:{interaction.guild_id}", u)
                await bot.valkey.hset(f"post_indexer_info:{u}", mapping={"name": interaction.user.name, "icon": str(interaction.user.display_avatar.url)})
                await bot.valkey.set(f"next_poll:{u}", 0)
                found += 1
    await interaction.followup.send(f"Added {found} URLs.")

@bot.tree.command(name="set_language", description="Change UI language")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
@app_commands.checks.has_permissions(manage_messages=True)
async def set_language(interaction: discord.Interaction):
    view = ui.View()
    view.add_item(LanguageSelect(bot.valkey))
    await interaction.response.send_message("Select language:", view=view, ephemeral=True)

@bot.tree.command(name="update_localization", description="Sync localization from Google Sheet")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
async def update_localization(interaction: discord.Interaction):
    if interaction.guild_id != 590756888254349315:
        return await interaction.response.send_message("No permission.")
    success = await bot.sync_localization()
    await interaction.response.send_message("Updated." if success else "Failed.")

@bot.tree.command(name="test_feed", description="Test embed rendering")
@app_commands.allowed_contexts(guilds=True)
@app_commands.allowed_installs(guilds=True)
async def test_feed(interaction: discord.Interaction, canny_url: str):
    lang = "English"
    if interaction.guild_id:
        lang = await bot.valkey.hget(f"guild_config:{interaction.guild_id}", "language") or "English"
    url = clean_url(canny_url)
    await interaction.response.defer(ephemeral=True)
    data = await fetch_canny_data(url)
    parts = url.split("/")
    uname = parts[parts.index("p") + 1] if "p" in parts else None
    post = extract_post_from_data(data, uname)
    if not post:
        return await interaction.followup.send(bot.localizer.get("post_not_found", lang), ephemeral=True)
    user_info = {"type": "requested", "name": interaction.user.name, "icon": str(interaction.user.display_avatar.url)}
    embed = create_canny_embed(post, lang=lang, user_info=user_info)
    await interaction.followup.send(embed=embed, view=create_canny_view(url), ephemeral=True)

if __name__ == "__main__":
    t = os.getenv("DISCORD_TOKEN")
    if t: bot.run(t.strip())
    else: print("No token.")
