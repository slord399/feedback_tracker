import os
from redis.asyncio.cluster import RedisCluster, ClusterNode

_valkey_instance = None

def get_valkey_client():
    global _valkey_instance
    if _valkey_instance is not None:
        return _valkey_instance

    startup_nodes = [
        ClusterNode("valkey-1", 6379),
        ClusterNode("valkey-2", 6379),
        ClusterNode("valkey-3", 6379),
        ClusterNode("valkey-10", 6379),
        ClusterNode("valkey-11", 6379),
    ]
    env_nodes = os.getenv("VALKEY_NODES")
    if env_nodes:
        startup_nodes = []
        for node in env_nodes.split(","):
            host, port = node.split(":")
            startup_nodes.append(ClusterNode(host, int(port)))

    _valkey_instance = RedisCluster(
        startup_nodes=startup_nodes,
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5
    )
    return _valkey_instance

async def register_guild(valkey, guild):
    gid = str(guild.id) if hasattr(guild, 'id') else str(guild)
    name = getattr(guild, 'name', "Unknown Server")
    await valkey.sadd("active_guilds", gid)
    if hasattr(guild, 'name'):
        await valkey.hset("guild_names", gid, name)

async def get_active_guilds(valkey):
    return await valkey.smembers("active_guilds")

async def get_all_guilds(valkey):
    gids = await valkey.smembers("active_guilds")
    names = await valkey.hgetall("guild_names")
    return [{"id": gid, "name": names.get(gid, "Unknown Server")} for gid in gids]
