import os
import valkey.asyncio
from valkey.asyncio.cluster import ValkeyCluster, ClusterNode

_valkey_instance = None
_valkey_pubsub_instance = None

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

    _valkey_instance = ValkeyCluster(
        startup_nodes=startup_nodes,
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5
    )
    return _valkey_instance

def get_valkey_pubsub_client():
    """
    Returns a standard Valkey client (non-cluster) for Pub/Sub operations,
    as ValkeyCluster in valkey-py 6.1.1 does not yet support .pubsub().
    """
    global _valkey_pubsub_instance
    if _valkey_pubsub_instance is not None:
        return _valkey_pubsub_instance

    startup_nodes = [
        ("valkey-1", 6379),
        ("valkey-2", 6379),
        ("valkey-3", 6379),
        ("valkey-10", 6379),
        ("valkey-11", 6379),
    ]
    env_nodes = os.getenv("VALKEY_NODES")
    if env_nodes:
        startup_nodes = []
        for node in env_nodes.split(","):
            host, port = node.split(":")
            startup_nodes.append((host, int(port)))

    host, port = startup_nodes[0]
    _valkey_pubsub_instance = valkey.asyncio.Valkey(
        host=host,
        port=port,
        decode_responses=True,
        socket_timeout=None, # Pub/Sub listeners need no timeout or very long one
        socket_connect_timeout=5
    )
    return _valkey_pubsub_instance

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

async def refresh_valkey_cluster(valkey_client):
    """
    Forces a re-initialization of the cluster map to clear bad states and
    update node information.
    """
    try:
        if hasattr(valkey_client, 'nodes_manager'):
            await valkey_client.nodes_manager.initialize()
            return True
    except Exception:
        pass
    return False
