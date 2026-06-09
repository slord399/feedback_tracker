import discord
discord.VoiceClient.warn_nacl = False
from discord import app_commands, ui
from discord.ext import commands, tasks
import os, sys, json, logging, aiohttp, re, time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, register_guild
from Bot.shared.localization import get_localizer
from Bot.shared.canny import fetch_canny_data, extract_post_from_data

logging.basicConfig(level=logging.INFO); logger = logging.getLogger("gateway")

class GuildSelect(ui.Select):
    def __init__(self, guilds, canny_url, message_id=None):
        options = [discord.SelectOption(label=g.name, value=str(g.id)) for g in guilds[:25]]
        super().__init__(placeholder="Select server...", options=options)
        self.canny_url = canny_url; self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        gid = self.values[0]; valkey = get_valkey_client()
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
        lang = self.values[0]
        await self.valkey.hset(f"guild_config:{interaction.guild_id}", "language", lang)
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
        post_btn = ui.Button(label="Post Embed", style=discord.ButtonStyle.blue)
        post_btn.callback = self.post_as_embed; self.add_item(post_btn)
        index_btn = ui.Button(label="Index", style=discord.ButtonStyle.green)
        index_btn.callback = self.index_selected; self.add_item(index_btn)

    async def prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1); self.update_components(); await self.update_msg(interaction)
    async def next(self, interaction: discord.Interaction):
        self.page = min((len(self.results)-1)//10, self.page + 1); self.update_components(); await self.update_msg(interaction)
    async def post_as_embed(self, interaction: discord.Interaction):
        if not self.selected_url: return await interaction.response.send_message("Select a post.", ephemeral=True)
        await self.bot.valkey.lpush("discord_jobs", json.dumps({"type": "check_status", "url": self.selected_url, "channel_id": interaction.channel_id}))
        await interaction.response.send_message("Posting...", ephemeral=True)
    async def index_selected(self, interaction: discord.Interaction):
        if not self.selected_url: return await interaction.response.send_message("Select a post.", ephemeral=True)
        lgid = await self.bot.valkey.get(f"last_index_selection:{interaction.user.id}")
        if lgid:
            gid = int(lgid); await self.bot.valkey.sadd("indexed_post_urls", self.selected_url); await self.bot.valkey.sadd(f"guild_indexed_posts:{gid}", self.selected_url); await self.bot.valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{self.selected_url}")
            await self.bot.valkey.lpush("discord_jobs", json.dumps({"type": "index_confirm", "url": self.selected_url, "guild_id": gid, "channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url)}))
            return await interaction.response.send_message("Indexed!", ephemeral=True)
        view = ui.View(); view.add_item(GuildSelect(self.bot.guilds, self.selected_url))
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
    query = ui.TextInput(label='Query Keywords', placeholder='Title or description...', min_length=2)
    min_votes = ui.TextInput(label='Min Votes', placeholder='0', default='0', required=False)
    max_votes = ui.TextInput(label='Max Votes', placeholder='9999', required=False)
    min_comments = ui.TextInput(label='Min Comments', placeholder='0', default='0', required=False)

    def __init__(self, filter_view):
        super().__init__(); self.filter_view = filter_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        valkey = self.filter_view.bot.valkey; res = []; cursor = 0; q = self.query.value.lower()
        min_v = int(self.min_votes.value) if self.min_votes.value.isdigit() else 0
        max_v = int(self.max_votes.value) if self.max_votes.value.isdigit() else 999999
        min_c = int(self.min_comments.value) if self.min_comments.value.isdigit() else 0
        while True:
            cursor, data = await valkey.hscan("canny_search_index", cursor=cursor, count=100)
            for k, v in data.items():
                p = json.loads(v)
                if q in p['title'].lower() or q in p.get('details', '').lower():
                    if self.filter_view.boards and not any(b in p['url'] for b in self.filter_view.boards): continue
                    if self.filter_view.statuses and p.get('status', '').lower() not in self.filter_view.statuses: continue
                    vts = p.get('score', 0); cmt = p.get('comments', 0)
                    if vts < min_v or vts > max_v or cmt < min_c: continue
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
        self.tree.add_command(app_commands.ContextMenu(name="Index this canny", callback=self.index_this_canny))
        self.tree.add_command(app_commands.ContextMenu(name="Check canny status", callback=self.check_canny_status))
        self.tree.add_command(app_commands.ContextMenu(name="Post what I indexed in hour", callback=self.post_indexed_hour))
        if self.shard_id is None or self.shard_id == 0: await self.tree.sync()
        self.update_activity.start()

    @tasks.loop(minutes=5)
    async def update_activity(self):
        try:
            idx = await self.valkey.scard("indexed_post_urls"); tot = await self.valkey.hlen("canny_search_index")
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="feedback.vrchat.com", state="Tracking", details=f"{idx} of {tot} indexed"))
        except: pass

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
        view = ui.View(); view.add_item(GuildSelect(self.guilds, url, message.id))
        await interaction.response.send_message("Select server:", view=view, ephemeral=True)

    async def check_canny_status(self, interaction: discord.Interaction, message: discord.Message):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
        url = next((u for u in urls if "canny.io" in u or "feedback.vrchat.com" in u), None)
        if not url: return await interaction.response.send_message("No URL.", ephemeral=True)
        await self.valkey.lpush("discord_jobs", json.dumps({"type": "check_status", "url": url, "channel_id": interaction.channel_id}))
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
async def search(interaction: discord.Interaction):
    await interaction.response.send_message("Configure filters:", view=SearchFilterView(bot), ephemeral=True)

@bot.tree.command(name="ping", description="Check Discord API latency")
async def ping(interaction: discord.Interaction): await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms")

@bot.tree.command(name="stats", description="View bot and indexing statistics")
async def stats(interaction: discord.Interaction):
    idx = await bot.valkey.scard("indexed_post_urls"); tot = await bot.valkey.hlen("canny_search_index")
    await interaction.response.send_message(f"Stats: {tot} found, {idx} indexed.")

@bot.tree.command(name="help", description="Show available commands and usage info")
async def help_cmd(interaction: discord.Interaction):
    msg = "Commands: /stats, /search, /ping, /credit. Context Menu: Index this canny, Check canny status, Post what I indexed in hour."
    if interaction.user.guild_permissions.manage_messages: msg += "\nAdmin: /mode, /set_status_channel, /set_react_channel, /set_language, /bulk_add"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="credit", description="View bot credits and license")
async def credit(interaction: discord.Interaction):
    await interaction.response.send_message("Bot by Jules. Inspired by Hackebein architecture. MIT License.", ephemeral=True)

@bot.tree.command(name="mode", description="Toggle Global or Local indexing mode for this guild")
@app_commands.checks.has_permissions(manage_messages=True)
async def mode(interaction: discord.Interaction, mode: str):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "mode", mode.lower()); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message(f"Mode: {mode}")

@bot.tree.command(name="set_status_channel", description="Set the channel where Canny status updates will be posted")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_status_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "status_channel", str(channel.id)); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message("Status channel set.")

@bot.tree.command(name="set_react_channel", description="Set an additional channel for the bot to listen for Canny URLs")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_react_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "react_channel", str(channel.id)); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message("React channel set.")

@bot.tree.command(name="bulk_add", description="Bulk index all Canny URLs found in the last 100 messages of this channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def bulk_add(interaction: discord.Interaction):
    await interaction.response.defer(); found = 0
    async for msg in interaction.channel.history(limit=100):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', msg.content)
        for u in urls:
            if "canny.io" in u or "feedback.vrchat.com" in u:
                await bot.valkey.sadd("indexed_post_urls", u); await bot.valkey.sadd(f"guild_indexed_posts:{interaction.guild_id}", u); found += 1
    await interaction.followup.send(f"Added {found} URLs.")

@bot.tree.command(name="set_language", description="Change the UI language for bot embeds in this guild")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_language(interaction: discord.Interaction):
    view = ui.View(); view.add_item(LanguageSelect(bot.valkey))
    await interaction.response.send_message("Select language:", view=view, ephemeral=True)

@bot.tree.command(name="update_localization", description="Sync bot localization with a Google Sheet CSV")
async def update_localization(interaction: discord.Interaction, sheet_url: str):
    if interaction.guild_id != 590756888254349315: return await interaction.response.send_message("No permission.")
    base = sheet_url.split("/edit")[0]; gid = None
    if "gid=" in sheet_url: gid = sheet_url.split("gid=")[1].split("&")[0]
    csv_url = f"{base}/export?format=csv";
    if gid: csv_url += f"&gid={gid}"
    async with aiohttp.ClientSession() as session:
        async with session.get(csv_url) as resp:
            if resp.status == 200:
                with open("Locale/template.csv", "w", encoding="utf-8") as f: f.write(await resp.text())
                bot.localizer.load(); await interaction.response.send_message("Updated.")
            else: await interaction.response.send_message(f"Failed. {resp.status}")

@bot.tree.command(name="test_feed", description="Test Canny embed rendering for a specific URL")
async def test_feed(interaction: discord.Interaction, canny_url: str):
    await interaction.response.defer(ephemeral=True)
    await bot.valkey.lpush("discord_jobs", json.dumps({"type": "check_status", "url": canny_url, "channel_id": interaction.channel_id}))
    await interaction.followup.send("Test feed requested.")

if __name__ == "__main__":
    t = os.getenv("DISCORD_TOKEN")
    if t: bot.run(t.strip())
    else: print("No token.")
