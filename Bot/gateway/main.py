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
        super().__init__(placeholder="Select a post to act on...", options=options)
    async def callback(self, interaction: discord.Interaction):
        self.view.selected_url = self.values[0]; await interaction.response.defer()

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
            gid = int(lgid); await self.bot.valkey.sadd("indexed_post_urls", self.selected_url); await self.bot.valkey.sadd(f"guild_indexed_posts:{gid}", self.selected_url)
            await self.bot.valkey.lpush("discord_jobs", json.dumps({"type": "index_confirm", "url": self.selected_url, "guild_id": gid, "channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url)}))
            return await interaction.response.send_message("Indexed!", ephemeral=True)
        view = ui.View(); view.add_item(GuildSelect(self.bot.guilds, self.selected_url))
        await interaction.response.send_message("Select server:", view=view, ephemeral=True)
    async def update_msg(self, interaction):
        start = self.page*10; end = start+10; msg = "\n".join([f"[{r['title']}]({r['url']})" for r in self.results[start:end]])
        await interaction.response.edit_message(content=msg, view=self)

class SearchModal(ui.Modal, title='Search Canny'):
    query = ui.TextInput(label='Search Query', placeholder='Title or keywords...', min_length=2)
    board = ui.TextInput(label='Board', placeholder='e.g. Bug Reports', required=False)
    status = ui.TextInput(label='Status', placeholder='e.g. open, tracked, complete', required=False)
    category = ui.TextInput(label='Category', placeholder='e.g. SDK', required=False)
    sort = ui.TextInput(label='Sort By (votes/new)', placeholder='votes', default='votes', required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        valkey = get_valkey_client(); res = []; cursor = 0
        q = self.query.value.lower(); b = self.board.value.lower() if self.board.value else None
        s = self.status.value.lower() if self.status.value else None
        c = self.category.value.lower() if self.category.value else None

        while True:
            cursor, data = await valkey.hscan("canny_search_index", cursor=cursor, count=100)
            for k, v in data.items():
                p = json.loads(v)
                if q in p['title'].lower():
                    if b and b not in p.get('board', '').lower(): continue
                    if s and s != p.get('status', '').lower(): continue
                    # Category filter might need more indexing in poller, but for now check if present
                    res.append(p)
            if cursor == 0 or len(res) > 300: break

        if self.sort.value.lower() == 'votes': res.sort(key=lambda x: x.get('score', 0), reverse=True)
        else: res.sort(key=lambda x: x.get('url', ''), reverse=True) # Sort by URL as proxy for ID/age

        if not res: return await interaction.followup.send("No results.", ephemeral=True)
        view = SearchView(res, bot=interaction.client)
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

@bot.tree.command(name="search")
async def search(interaction: discord.Interaction): await interaction.response.send_modal(SearchModal())

@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction): await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms")

@bot.tree.command(name="stats")
async def stats(interaction: discord.Interaction):
    idx = await bot.valkey.scard("indexed_post_urls"); tot = await bot.valkey.hlen("canny_search_index")
    await interaction.response.send_message(f"Stats: {tot} found, {idx} indexed.")

@bot.tree.command(name="help")
async def help_cmd(interaction: discord.Interaction):
    msg = "Commands: /stats, /search, /ping, /credit. Context Menu: Index this canny, Check canny status, Post what I indexed in hour."
    if interaction.user.guild_permissions.manage_messages: msg += "\nAdmin: /mode, /set_status_channel, /set_react_channel, /set_language, /bulk_add"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="credit")
async def credit(interaction: discord.Interaction):
    await interaction.response.send_message("Bot by Jules. Inspired by Hackebein architecture. MIT License.", ephemeral=True)

@bot.tree.command(name="mode")
@app_commands.checks.has_permissions(manage_messages=True)
async def mode(interaction: discord.Interaction, mode: str):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "mode", mode.lower()); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message(f"Mode: {mode}")

@bot.tree.command(name="set_status_channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_status_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "status_channel", str(channel.id)); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message("Status channel set.")

@bot.tree.command(name="set_react_channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_react_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "react_channel", str(channel.id)); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message("React channel set.")

@bot.tree.command(name="bulk_add")
@app_commands.checks.has_permissions(manage_messages=True)
async def bulk_add(interaction: discord.Interaction):
    await interaction.response.defer(); found = 0
    async for msg in interaction.channel.history(limit=100):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', msg.content)
        for u in urls:
            if "canny.io" in u or "feedback.vrchat.com" in u:
                await bot.valkey.sadd("indexed_post_urls", u); await bot.valkey.sadd(f"guild_indexed_posts:{interaction.guild_id}", u); found += 1
    await interaction.followup.send(f"Added {found} URLs.")

@bot.tree.command(name="set_language")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_language(interaction: discord.Interaction, lang: str):
    await bot.valkey.hset(f"guild_config:{interaction.guild_id}", "language", lang); await register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message(f"Language: {lang}")

@bot.tree.command(name="update_localization")
async def update_localization(interaction: discord.Interaction, sheet_url: str):
    if interaction.guild_id != 590756888254349315: return await interaction.response.send_message("No.")
    csv_url = sheet_url.replace("/edit#gid=", "/export?format=csv&gid=")
    async with aiohttp.ClientSession() as session:
        async with session.get(csv_url) as resp:
            if resp.status == 200:
                with open("Locale/template.csv", "w") as f: f.write(await resp.text())
                bot.localizer.load(); await interaction.response.send_message("Updated.")
            else: await interaction.response.send_message("Failed.")

@bot.tree.command(name="test_feed")
async def test_feed(interaction: discord.Interaction, url: str):
    if interaction.guild_id != 590756888254349315: return await interaction.response.send_message("No.")
    data = await fetch_canny_data(url); await interaction.response.send_message("Success!" if data else "Failed.")

if __name__ == "__main__":
    t = os.getenv("DISCORD_TOKEN")
    if t: bot.run(t.strip())
    else: print("No token.")
