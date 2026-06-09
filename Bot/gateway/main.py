import discord
discord.VoiceClient.warn_nacl = False
from discord import app_commands, ui
from discord.ext import commands, tasks
import os, sys, json, logging, aiohttp, re, time
from datetime import datetime
import asyncio

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, register_guild
from Bot.shared.localization import get_localizer
from Bot.shared.canny import fetch_canny_data, extract_post_from_data

logging.basicConfig(level=logging.INFO); logger = logging.getLogger("gateway")

SHEET_URL = "https://docs.google.com/spreadsheets/d/17sYQbx154noc42UO1vvm3VVNLdnSguTb6j-J5mszvtQ/edit?usp=sharing"

# Support User Apps in DMs and Guilds using correct discord.py 2.x Enums
ALLOWED_INSTALLS = app_commands.AppInstallationType(guild=True, user=True)
ALLOWED_CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)

class GuildSelect(ui.Select):
    def __init__(self, bot, guilds, canny_url, message_id=None):
        options = [discord.SelectOption(label=g.name, value=str(g.id)) for g in guilds[:25]]
        super().__init__(placeholder="Select server...", options=options)
        self.bot = bot; self.canny_url = canny_url; self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        gid = self.values[0]; valkey = self.bot.valkey
        await valkey.sadd("indexed_post_urls", self.canny_url)
        await valkey.sadd(f"guild_indexed_posts:{gid}", self.canny_url)
        await valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{self.canny_url}")
        await valkey.set(f"last_index_selection:{interaction.user.id}", str(gid), ex=300)
        await valkey.lpush("discord_jobs", json.dumps({"type": "index_confirm", "url": self.canny_url, "guild_id": int(gid), "channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url), "original_message_id": self.message_id}))
        await interaction.response.edit_message(content="Indexed!", view=None)

class ResultSelect(ui.Select):
    def __init__(self, posts):
        options = [discord.SelectOption(label=p['title'][:100], value=p['url']) for p in posts]
        super().__init__(placeholder="Select a post...", options=options)
    async def callback(self, interaction: discord.Interaction):
        self.view.selected_url = self.values[0]; await interaction.response.defer()

class LanguageSelect(ui.Select):
    def __init__(self, valkey):
        langs = ["English", "Deutsch", "Española", "François", "Italian", "polski", "Portuguese do Brazil", "русский", "中文（简体）", "中文（繁體）", "日本語", "한국어"]
        options = [discord.SelectOption(label=l, value=l) for l in langs]
        super().__init__(placeholder="Select language...", options=options)
        self.valkey = valkey
    async def callback(self, interaction: discord.Interaction):
        lang = self.values[0]; await self.valkey.hset(f"guild_config:{interaction.guild_id}", "language", lang)
        await interaction.response.edit_message(content=f"Language set to {lang}.", view=None)

class SearchView(ui.View):
    def __init__(self, results, page=0, bot=None):
        super().__init__(); self.results = results; self.page = page; self.bot = bot; self.selected_url = None
        self.update_components()

    def update_components(self):
        self.clear_items()
        start = self.page * 10; end = start + 10; current_posts = self.results[start:end]
        if current_posts: self.add_item(ResultSelect(current_posts))
        prev_btn = ui.Button(label="Prev", style=discord.ButtonStyle.grey, disabled=(self.page == 0))
        prev_btn.callback = self.prev; self.add_item(prev_btn)
        next_btn = ui.Button(label="Next", style=discord.ButtonStyle.grey, disabled=(end >= len(self.results)))
        next_btn.callback = self.next; self.add_item(next_btn)
        post_btn = ui.Button(label="Post Embed", style=discord.ButtonStyle.primary)
        post_btn.callback = self.post_as_embed; self.add_item(post_btn)
        index_btn = ui.Button(label="Index", style=discord.ButtonStyle.green)
        index_btn.callback = self.index_selected; self.add_item(index_btn)

    async def prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1); self.update_components(); await self.update_msg(interaction)
    async def next(self, interaction: discord.Interaction):
        self.page = min((len(self.results)-1)//10, self.page + 1); self.update_components(); await self.update_msg(interaction)
    async def post_as_embed(self, interaction: discord.Interaction):
        if not self.selected_url: return await interaction.response.send_message("Select a post.", ephemeral=True)
        gid = interaction.guild_id if interaction.guild else None
        await self.bot.valkey.lpush("discord_jobs", json.dumps({"type": "check_status", "url": self.selected_url, "channel_id": interaction.channel_id, "guild_id": gid}))
        await interaction.response.send_message("Posting...", ephemeral=True)
    async def index_selected(self, interaction: discord.Interaction):
        if not self.selected_url: return await interaction.response.send_message("Select a post.", ephemeral=True)
        lgid = await self.bot.valkey.get(f"last_index_selection:{interaction.user.id}")
        if lgid:
            gid = int(lgid); await self.bot.valkey.sadd("indexed_post_urls", self.selected_url); await self.bot.valkey.sadd(f"guild_indexed_posts:{gid}", self.selected_url); await self.bot.valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{self.selected_url}")
            await self.bot.valkey.lpush("discord_jobs", json.dumps({"type": "index_confirm", "url": self.selected_url, "guild_id": gid, "channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url)}))
            return await interaction.response.send_message("Indexed!", ephemeral=True)
        view = ui.View(); view.add_item(GuildSelect(self.bot, self.bot.guilds, self.selected_url))
        await interaction.response.send_message("Select server:", view=view, ephemeral=True)
    async def update_msg(self, interaction):
        start = self.page*10; end = start+10; msg = "\n".join([f"[{r['title']}]({r['url']})" for r in self.results[start:end]])
        await interaction.response.edit_message(content=msg, view=self)

class SearchFilterView(ui.View):
    def __init__(self, bot):
        super().__init__(); self.bot = bot
        self.boards = []; self.statuses = []

    @ui.select(cls=ui.Select, placeholder="Select Boards", min_values=0, max_values=5, options=[
        discord.SelectOption(label="Feature Requests", value="feature-requests"),
        discord.SelectOption(label="Bug Reports", value="bug-reports"),
        discord.SelectOption(label="SDK Bug Reports", value="sdk-bug-reports"),
        discord.SelectOption(label="Udon", value="udon"),
        discord.SelectOption(label="Open Beta", value="open-beta")
    ])
    async def select_boards(self, interaction: discord.Interaction, select: ui.Select):
        self.boards = select.values; await interaction.response.defer()

    @ui.select(cls=ui.Select, placeholder="Select Statuses", min_values=0, max_values=5, options=[
        discord.SelectOption(label="Open", value="open"),
        discord.SelectOption(label="Tracked", value="tracked"),
        discord.SelectOption(label="Planned", value="planned"),
        discord.SelectOption(label="In Progress", value="in-progress"),
        discord.SelectOption(label="Complete", value="complete"),
        discord.SelectOption(label="Available", value="available")
    ])
    async def select_statuses(self, interaction: discord.Interaction, select: ui.Select):
        self.statuses = select.values; await interaction.response.defer()

    @ui.button(label="Enter Metrics & Execute", style=discord.ButtonStyle.green)
    async def execute_search(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(SearchQueryModal(self))

class SearchQueryModal(ui.Modal, title='Enter Search Metrics'):
    query = ui.TextInput(label='Query Keywords', placeholder='Title or description...', required=False)
    min_votes = ui.TextInput(label='Min Votes', placeholder='0', default='0', required=False)
    max_votes = ui.TextInput(label='Max Votes', placeholder='9999', required=False)
    min_comments = ui.TextInput(label='Min Comments', placeholder='0', default='0', required=False)
    date_range = ui.TextInput(label='Date Range (YYYY-MM-DD to YYYY-MM-DD)', placeholder='e.g. 2024-01-01 to 2024-12-31', required=False)

    def __init__(self, filter_view):
        super().__init__(); self.filter_view = filter_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        valkey = self.filter_view.bot.valkey; res = []; cursor = 0; q = self.query.value.lower()
        min_v = int(self.min_votes.value) if self.min_votes.value.isdigit() else 0
        max_v = int(self.max_votes.value) if self.max_votes.value.isdigit() else 999999
        min_c = int(self.min_comments.value) if self.min_comments.value.isdigit() else 0
        min_ts = 0; max_ts = 2147483647
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
                    vts = p.get('score', 0); cmt = p.get('comments', 0); crt = p.get('created', 0)
                    if vts < min_v or vts > max_v or cmt < min_c or crt < min_ts or crt > max_ts: continue
                    res.append(p)
            if cursor == 0 or len(res) > 500: break
        res.sort(key=lambda x: x.get('score', 0), reverse=True)
        if not res: return await interaction.followup.send("No results.", ephemeral=True)
        view = SearchView(res, bot=self.filter_view.bot)
        await interaction.followup.send("\n".join([f"[{r['title']}]({r['url']})" for r in res[:10]]), view=view, ephemeral=True)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default(); intents.message_content = True; sid = os.getenv("SHARD_ID"); sc = os.getenv("TOTAL_SHARDS")
        super().__init__(command_prefix="!", intents=intents, shard_id=int(sid) if sid else None, shard_count=int(sc) if sc else None)
        self.valkey = get_valkey_client(); self.localizer = get_localizer()

    async def setup_hook(self):
        logger.info(f"Setting up Shard {self.shard_id}...")
        # Context Menu Commands with correct attributes
        cmd_index = app_commands.ContextMenu(name="Index this canny", callback=self.index_this_canny)
        cmd_index.allowed_contexts = ALLOWED_CONTEXTS; cmd_index.allowed_installs = ALLOWED_INSTALLS
        self.tree.add_command(cmd_index)

        cmd_status = app_commands.ContextMenu(name="Check canny status", callback=self.check_canny_status)
        cmd_status.allowed_contexts = ALLOWED_CONTEXTS; cmd_status.allowed_installs = ALLOWED_INSTALLS
        self.tree.add_command(cmd_status)

        cmd_hour = app_commands.ContextMenu(name="Post what I indexed in hour", callback=self.post_indexed_hour)
        cmd_hour.allowed_contexts = ALLOWED_CONTEXTS; cmd_hour.allowed_installs = ALLOWED_INSTALLS
        self.tree.add_command(cmd_hour)

        if self.shard_id is None or self.shard_id == 0:
            await self.tree.sync()
            self.auto_sync_localization.start()
            asyncio.create_task(self.sync_localization())
        self.update_activity.start()
        logger.info("Setup hook complete.")

    @tasks.loop(minutes=60)
    async def update_activity(self):
        try:
            idx = await self.valkey.scard("indexed_post_urls"); tot = await self.valkey.hlen("canny_search_index")
            activity = discord.Activity(type=discord.ActivityType.watching, name="feedback.vrchat.com", state="Tracking", details=f"{idx} of {tot} indexed")
            await self.change_presence(activity=activity)
        except: pass

    @tasks.loop(hours=1)
    async def auto_sync_localization(self):
        await self.sync_localization()

    async def sync_localization(self):
        logger.info("Syncing localization...")
        try:
            base = SHEET_URL.split("/edit")[0]
            gid = SHEET_URL.split("gid=")[1].split("&")[0] if "gid=" in SHEET_URL else None
            csv_url = f"{base}/export?format=csv"
            if gid: csv_url += f"&gid={gid}"
            async with aiohttp.ClientSession() as session:
                async with session.get(csv_url) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        if "string_name" in content:
                            with open("Locale/template.csv", "w", encoding="utf-8") as f: f.write(content)
                            self.localizer.load()
                            logger.info("Localization synced successfully.")
                            return True
                    else: logger.error(f"Failed to sync localization: HTTP {resp.status}")
        except Exception: logger.exception("Localization sync error")
        return False

    async def on_message(self, message):
        if message.author.bot or not message.guild: return
        cfg = await self.valkey.hgetall(f"guild_config:{message.guild.id}")
        if str(message.channel.id) in [cfg.get("react_channel"), cfg.get("status_channel")]:
            urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
            for u in urls:
                if "canny.io" in u or "feedback.vrchat.com" in u:
                    await self.valkey.lpush("discord_jobs", json.dumps({"type": "index_confirm", "url": u, "guild_id": message.guild.id, "channel_id": message.channel.id, "user_id": message.author.id, "user_name": message.author.name, "user_icon": str(message.author.display_avatar.url), "original_message_id": message.id}))

    async def index_this_canny(self, interaction: discord.Interaction, message: discord.Message):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
        url = next((u for u in urls if "canny.io" in u or "feedback.vrchat.com" in u), None)
        if not url: return await interaction.response.send_message("No URL.", ephemeral=True)
        lgid = await self.valkey.get(f"last_index_selection:{interaction.user.id}")
        if lgid:
            gid = int(lgid); await self.valkey.sadd("indexed_post_urls", url); await self.valkey.sadd(f"guild_indexed_posts:{gid}", url); await self.valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{url}")
            await self.valkey.lpush("discord_jobs", json.dumps({"type": "index_confirm", "url": url, "guild_id": gid, "channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url), "original_message_id": message.id}))
            return await interaction.response.send_message("Indexed!", ephemeral=True)
        view = ui.View(); view.add_item(GuildSelect(self, self.guilds, url, message.id))
        await interaction.response.send_message("Select server:", view=view, ephemeral=True)

    async def check_canny_status(self, interaction: discord.Interaction, message: discord.Message):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
        url = next((u for u in urls if "canny.io" in u or "feedback.vrchat.com" in u), None)
        if not url: return await interaction.response.send_message("No URL.", ephemeral=True)
        gid = interaction.guild_id if interaction.guild else None
        job = {"type": "check_status", "url": url, "channel_id": interaction.channel_id, "guild_id": gid}
        await self.valkey.lpush("discord_jobs", json.dumps(job))
        await interaction.response.send_message("Checking...", ephemeral=True)

    async def post_indexed_hour(self, interaction: discord.Interaction, message: discord.Message):
        idx = await self.valkey.smembers(f"user_indexed_posts:{interaction.user.id}")
        now = time.time(); res = []
        for entry in idx:
            ts, url = entry.split("|", 1)
            if now - float(ts) < 3600: res.append(url)
        if not res: return await interaction.response.send_message("Empty.", ephemeral=True)
        await interaction.response.send_message("\n".join(res))

bot = MyBot()

@bot.tree.command(name="search", description="Search Canny posts with interactive filters")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def search(interaction: discord.Interaction):
    await interaction.response.send_message("Configure filters:", view=SearchFilterView(bot), ephemeral=True)

@bot.tree.command(name="ping", description="Check Discord API latency")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def ping(interaction: discord.Interaction): await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms")

@bot.tree.command(name="stats", description="View bot and indexing statistics")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def stats(interaction: discord.Interaction):
    idx = await bot.valkey.scard("indexed_post_urls"); tot = await bot.valkey.hlen("canny_search_index")
    this_month = time.strftime('%Y-%m')
    status_changes = await bot.valkey.get(f"stats:status_change:{this_month}") or 0
    vote_reports = await bot.valkey.get(f"stats:vote_progress:{this_month}") or 0
    msg = f"**Canny Bot Stats**\nTotal Discovered: {tot}\nUniquely Indexed: {idx}\n\n**Activity ({this_month})**\nStatus Updates: {status_changes}\nVote Milestones: {vote_reports}"
    await interaction.response.send_message(msg)

@bot.tree.command(name="help", description="Comprehensive guide for the VRChat Canny Bot")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Canny Bot Help Article", color=discord.Color.blue())
    embed.add_field(name="General Commands", value="**/search**: Interactive search.\n**/stats**: Activity metrics.\n**/ping**: Latency check.\n**/credit**: Affiliation and donation info.", inline=False)
    embed.add_field(name="User App Features", value="**Index this canny**: Track link in server.\n**Check canny status**: Get status/votes.\n**Post what I indexed in hour**: Activity summary.", inline=False)
    if interaction.user and getattr(interaction.user, 'guild_permissions', None) and interaction.user.guild_permissions.manage_messages:
        embed.add_field(name="Administrative Commands", value="**/mode**: Global vs Local.\n**/set_status_channel**: Post updates here.\n**/set_react_channel**: Auto-index links here.\n**/set_language**: Change UI language.\n**/bulk_add**: Scrape channel history.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="credit", description="View bot credits, hosting, and affiliation")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def credit(interaction: discord.Interaction):
    msg = (
        "**This bot is not affiliated with VRChat Inc.**\n\n"
        "Hosted by [VRCβフォース](<https://discord.gg/XJHRXwd>) | [VRChat Group](<https://vrc.group/BETAJP.2222>).\n"
        "Localization: [Google Sheet](<https://docs.google.com/spreadsheets/d/17sYQbx154noc42UO1vvm3VVNLdnSguTb6j-J5mszvtQ/edit?usp=sharing>).\n"
        "Open Source: [GitHub](<https://github.com/slord399/feedback_tracker/>).\n"
        "Donations: [X (formerly Twitter)](<https://x.com/slord399/creator-subscriptions/subscribe>) | [Ko-fi](<https://ko-fi.com/tony_lewis>) | [GitHub Sponsors](<https://github.com/sponsors/slord399/>)."
    )
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="mode", description="Toggle Global or Local indexing mode")
@app_commands.checks.has_permissions(manage_messages=True)
async def mode(interaction: discord.Interaction, mode: str):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "mode", mode.lower()); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message(f"Mode: {mode}")

@bot.tree.command(name="set_status_channel", description="Set the channel for status updates")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_status_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "status_channel", str(channel.id)); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message("Status channel set.")

@bot.tree.command(name="set_react_channel", description="Set an additional channel for auto-indexing")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_react_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "react_channel", str(channel.id)); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message("React channel set.")

@bot.tree.command(name="bulk_add", description="Index URLs from channel history")
@app_commands.checks.has_permissions(manage_messages=True)
async def bulk_add(interaction: discord.Interaction):
    await interaction.response.defer(); found = 0
    async for msg in interaction.channel.history(limit=100):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', msg.content)
        for u in urls:
            if "canny.io" in u or "feedback.vrchat.com" in u:
                await bot.valkey.sadd("indexed_post_urls", u); await bot.valkey.sadd(f"guild_indexed_posts:{interaction.guild_id}", u); found += 1
    await interaction.followup.send(f"Added {found} URLs.")

@bot.tree.command(name="set_language", description="Change UI language")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_language(interaction: discord.Interaction):
    view = ui.View(); view.add_item(LanguageSelect(bot.valkey))
    await interaction.response.send_message("Select language:", view=view, ephemeral=True)

@bot.tree.command(name="update_localization", description="Sync localization from Google Sheet")
async def update_localization(interaction: discord.Interaction):
    if interaction.guild_id != 590756888254349315: return await interaction.response.send_message("No permission.")
    success = await bot.sync_localization()
    await interaction.response.send_message("Updated." if success else "Failed.")

@bot.tree.command(name="test_feed", description="Test embed rendering")
async def test_feed(interaction: discord.Interaction, canny_url: str):
    await interaction.response.defer(ephemeral=True)
    gid = interaction.guild_id if interaction.guild else None
    job = {"type": "check_status", "url": canny_url, "channel_id": interaction.channel_id, "guild_id": gid}
    await bot.valkey.lpush("discord_jobs", json.dumps(job))
    await interaction.followup.send("Test feed requested.")

if __name__ == "__main__":
    t = os.getenv("DISCORD_TOKEN")
    if t: bot.run(t.strip())
    else: print("No token.")
