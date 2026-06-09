import os
from redis.cluster import RedisCluster, ClusterNode

def get_valkey_client():
    # Nodes provided in requirements
    startup_nodes = [
        ClusterNode("valkey-1", 6379),
        ClusterNode("valkey-2", 6379),
        ClusterNode("valkey-3", 6379),
        ClusterNode("valkey-10", 6379),
        ClusterNode("valkey-11", 6379),
    ]
    # In local dev/test environment, we might want to override this via ENV
    env_nodes = os.getenv("VALKEY_NODES")
    if env_nodes:
        # Expected format: host1:port1,host2:port2
        startup_nodes = []
        for node in env_nodes.split(","):
            host, port = node.split(":")
            startup_nodes.append(ClusterNode(host, int(port)))

    return RedisCluster(startup_nodes=startup_nodes, decode_responses=True)

def register_guild(valkey, guild_id):
    valkey.sadd("active_guilds", str(guild_id))

def get_active_guilds(valkey):
    return valkey.smembers("active_guilds")
