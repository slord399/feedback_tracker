import discord
discord.VoiceClient.warn_nacl = False
from discord import app_commands
from discord.ext import commands, tasks
import os, sys, json, logging, aiohttp, re, time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, register_guild
from Bot.shared.localization import get_localizer
from Bot.shared.canny import fetch_canny_data, extract_post_from_data

logging.basicConfig(level=logging.INFO); logger = logging.getLogger("gateway")

class GuildSelect(discord.ui.Select):
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

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default(); intents.message_content = True
        sid = os.getenv("SHARD_ID"); sc = os.getenv("TOTAL_SHARDS")
        super().__init__(command_prefix="!", intents=intents, shard_id=int(sid) if sid else None, shard_count=int(sc) if sc else None)
        self.valkey = get_valkey_client(); self.localizer = get_localizer()

    async def setup_hook(self):
        self.tree.add_command(app_commands.ContextMenu(name="Index this canny", callback=self.index_this_canny))
        self.tree.add_command(app_commands.ContextMenu(name="Check canny status", callback=self.check_canny_status))
        self.tree.add_command(app_commands.ContextMenu(name="Post what I indexed in hour", callback=self.post_indexed_hour))
        # Only sync on shard 0 to prevent duplicates
        if self.shard_id is None or self.shard_id == 0:
            await self.tree.sync()
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
            return await interaction.response.send_message("Indexed automatically!", ephemeral=True)
        view = discord.ui.View(); view.add_item(GuildSelect(self.guilds, url, message.id))
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

class SearchView(discord.ui.View):
    def __init__(self, results, page=0):
        super().__init__(); self.results = results; self.page = page

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.grey)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1); await self.update_msg(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.grey)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min((len(self.results)-1)//10, self.page + 1); await self.update_msg(interaction)

    @discord.ui.button(label="Index Top", style=discord.ButtonStyle.green)
    async def index_top(self, interaction: discord.Interaction, button: discord.ui.Button):
        url = self.results[self.page*10]['url']
        await bot.valkey.sadd("indexed_post_urls", url); await bot.valkey.sadd(f"guild_indexed_posts:{interaction.guild_id}", url); await bot.valkey.sadd(f"user_indexed_posts:{interaction.user.id}", f"{int(time.time())}|{url}")
        await interaction.response.send_message(f"Indexed {url}", ephemeral=True)

    async def update_msg(self, interaction):
        start = self.page*10; end = start+10; msg = "\n".join([f"[{r['title']}]({r['url']})" for r in self.results[start:end]])
        await interaction.response.edit_message(content=msg, view=self)

@bot.tree.command(name="search")
async def search(interaction: discord.Interaction, query: str, visibility: bool = False):
    await interaction.response.defer(ephemeral=not visibility)
    res = []; cursor = 0
    while True:
        cursor, data = await bot.valkey.hscan("canny_search_index", cursor=cursor, count=100)
        for k, v in data.items():
            if query.lower() in json.loads(v)['title'].lower(): res.append(json.loads(v))
        if cursor == 0: break
    if not res: return await interaction.followup.send("No results.", ephemeral=not visibility)
    await interaction.followup.send("\n".join([f"[{r['title']}]({r['url']})" for r in res[:10]]), view=SearchView(res), ephemeral=not visibility)

@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! Discord API: {round(bot.latency*1000)}ms")

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
    await interaction.response.send_message("This bot is not affiliated with VRChat Inc. License: MIT. Inspired by Hackebein.", ephemeral=True)

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
    await interaction.response.defer()
    found = 0
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
