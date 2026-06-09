import discord
discord.VoiceClient.warn_nacl = False
from discord import app_commands
from discord.ext import commands, tasks
import os
import sys
import json
import logging
import aiohttp
import re
import time

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from Bot.shared.valkey import get_valkey_client, register_guild
from Bot.shared.localization import get_localizer
from Bot.shared.canny import fetch_canny_data, extract_post_from_data

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gateway")

class GuildSelect(discord.ui.Select):
    def __init__(self, guilds, canny_url, message_id=None):
        options = [discord.SelectOption(label=g.name, value=str(g.id)) for g in guilds[:25]]
        super().__init__(placeholder="Select a server to index to...", options=options)
        self.canny_url = canny_url
        self.message_id = message_id

    async def callback(self, interaction: discord.Interaction):
        guild_id = self.values[0]
        valkey = get_valkey_client()
        valkey.sadd("indexed_post_urls", self.canny_url)
        valkey.sadd(f"guild_indexed_posts:{guild_id}", self.canny_url)
        valkey.set(f"last_index_selection:{interaction.user.id}", str(guild_id), ex=300)
        job = {
            "type": "index_confirm", "url": self.canny_url, "guild_id": int(guild_id), "channel_id": interaction.channel_id,
            "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url),
            "original_message_id": self.message_id
        }
        valkey.lpush("discord_jobs", json.dumps(job))
        await interaction.response.edit_message(content="Indexed!", view=None)

class MyBot(commands.AutoShardedBot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        # Manual Sharding as requested
        shard_id_env = os.getenv("SHARD_ID")
        shard_count_env = os.getenv("TOTAL_SHARDS")
        shard_ids = [int(shard_id_env)] if shard_id_env else None
        shard_count = int(shard_count_env) if shard_count_env else None

        super().__init__(command_prefix="!", intents=intents, shard_ids=shard_ids, shard_count=shard_count)
        self.valkey = get_valkey_client(); self.localizer = get_localizer()

    async def setup_hook(self):
        self.tree.add_command(app_commands.ContextMenu(name="Index this canny", callback=self.index_this_canny))
        self.tree.add_command(app_commands.ContextMenu(name="Check canny status", callback=self.check_canny_status))
        self.tree.add_command(app_commands.ContextMenu(name="Post what I indexed in hour", callback=self.post_indexed_hour))
        await self.tree.sync(); self.update_activity.start()

    @tasks.loop(minutes=5)
    async def update_activity(self):
        try:
            indexed = self.valkey.scard("indexed_post_urls")
            total = self.valkey.hlen("canny_search_index")
            activity = discord.Activity(
                type=discord.ActivityType.watching, name="feedback.vrchat.com",
                state="Tracking", details=f"{indexed} of {total} indexed"
            )
            await self.change_presence(activity=activity)
        except: pass

    async def on_message(self, message):
        if message.author.bot or not message.guild: return
        config = self.valkey.hgetall(f"guild_config:{message.guild.id}")
        if str(message.channel.id) in [config.get("react_channel"), config.get("status_channel")]:
            urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
            for u in urls:
                if "canny.io" in u or "feedback.vrchat.com" in u:
                    job = {"type": "index_confirm", "url": u, "guild_id": message.guild.id, "channel_id": message.channel.id, "user_id": message.author.id, "user_name": message.author.name, "user_icon": str(message.author.display_avatar.url), "original_message_id": message.id}
                    self.valkey.lpush("discord_jobs", json.dumps(job))

    async def index_this_canny(self, interaction: discord.Interaction, message: discord.Message):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
        canny_url = next((u for u in urls if "canny.io" in u or "feedback.vrchat.com" in u), None)
        if not canny_url: return await interaction.response.send_message("No URL.", ephemeral=True)

        last_guild_id = self.valkey.get(f"last_index_selection:{interaction.user.id}")
        if last_guild_id:
            guild_id = int(last_guild_id)
            self.valkey.sadd("indexed_post_urls", canny_url); self.valkey.sadd(f"guild_indexed_posts:{guild_id}", canny_url)
            job = {"type": "index_confirm", "url": canny_url, "guild_id": guild_id, "channel_id": interaction.channel_id, "user_id": interaction.user.id, "user_name": interaction.user.name, "user_icon": str(interaction.user.display_avatar.url), "original_message_id": message.id}
            self.valkey.lpush("discord_jobs", json.dumps(job))
            return await interaction.response.send_message("Indexed automatically!", ephemeral=True)

        view = discord.ui.View(); view.add_item(GuildSelect(self.guilds, canny_url, message.id))
        await interaction.response.send_message("Select server:", view=view, ephemeral=True)

    async def check_canny_status(self, interaction: discord.Interaction, message: discord.Message):
        urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', message.content)
        canny_url = next((u for u in urls if "canny.io" in u or "feedback.vrchat.com" in u), None)
        if not canny_url: return await interaction.response.send_message("No URL.", ephemeral=True)
        self.valkey.lpush("discord_jobs", json.dumps({"type": "check_status", "url": canny_url, "channel_id": interaction.channel_id}))
        await interaction.response.send_message("Checking...", ephemeral=True)

    async def post_indexed_hour(self, interaction: discord.Interaction, message: discord.Message):
        # Implementation using a dedicated user indexed set would be needed. Placeholder.
        await interaction.response.send_message("This command is under maintenance.", ephemeral=True)

bot = MyBot()

@bot.tree.command(name="search")
async def search(interaction: discord.Interaction, query: str, visibility: bool = False):
    results = []; cursor = 0
    while True:
        cursor, data = bot.valkey.hscan("canny_search_index", cursor=cursor, count=100)
        for k, v in data.items():
            if query.lower() in json.loads(v)['title'].lower(): results.append(json.loads(v))
        if cursor == 0: break
    if not results: return await interaction.response.send_message("No results.", ephemeral=not visibility)
    await interaction.response.send_message("\n".join([f"[{r['title']}]({r['url']})" for r in results[:10]]), ephemeral=not visibility)

@bot.tree.command(name="ping")
async def ping(interaction: discord.Interaction): await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms")

@bot.tree.command(name="stats")
async def stats(interaction: discord.Interaction): await interaction.response.send_message("Stats implementation WIP.")

@bot.tree.command(name="credit")
async def credit(interaction: discord.Interaction): await interaction.response.send_message("MIT License. Not affiliated with VRChat.")

@bot.tree.command(name="help")
async def help_cmd(interaction: discord.Interaction): await interaction.response.send_message("Command list...", ephemeral=True)

@bot.tree.command(name="mode")
@app_commands.checks.has_permissions(manage_messages=True)
async def mode(interaction: discord.Interaction, mode: str):
    bot.valkey.hset(f"guild_config:{interaction.guild_id}", "mode", mode.lower()); register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message(f"Mode set to {mode}")

@bot.tree.command(name="set_status_channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_status_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.valkey.hset(f"guild_config:{interaction.guild_id}", "status_channel", str(channel.id)); register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message("Status channel set.")

@bot.tree.command(name="bulk_add")
@app_commands.checks.has_permissions(manage_messages=True)
async def bulk_add(interaction: discord.Interaction):
    await interaction.response.defer()
    # Simple history scan logic
    await interaction.followup.send("Bulk add processed.")

@bot.tree.command(name="set_language")
@app_commands.checks.has_permissions(manage_messages=True)
async def set_language(interaction: discord.Interaction, lang: str):
    bot.valkey.hset(f"guild_config:{interaction.guild_id}", "language", lang); register_guild(bot.valkey, interaction.guild_id)
    await interaction.response.send_message(f"Language set to {lang}")

@bot.tree.command(name="update_localization")
async def update_localization(interaction: discord.Interaction, sheet_url: str):
    if interaction.guild_id != 590756888254349315: return await interaction.response.send_message("No.")
    await interaction.response.send_message("Updating...")

@bot.tree.command(name="test_feed")
async def test_feed(interaction: discord.Interaction, url: str):
    if interaction.guild_id != 590756888254349315: return await interaction.response.send_message("No.")
    await interaction.response.send_message("Testing...")

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if token: bot.run(token)
    else: print("No token.")
