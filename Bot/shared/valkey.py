import os
from redis.asyncio.cluster import RedisCluster, ClusterNode

def get_valkey_client():
    # Nodes provided in requirements
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

    return RedisCluster(
        startup_nodes=startup_nodes,
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5
    )

async def register_guild(valkey, guild_id):
    await valkey.sadd("active_guilds", str(guild_id))

async def get_active_guilds(valkey):
    return await valkey.smembers("active_guilds")
