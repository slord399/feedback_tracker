import time
import asyncio

class RateLimiter:
    def __init__(self, valkey, key_prefix, rate, period=1.0):
        """
        rate: number of requests allowed
        period: time window in seconds
        """
        self.valkey = valkey
        self.key_prefix = key_prefix
        self.rate = rate
        self.period = period

    async def acquire(self, identifier="global"):
        key = f"ratelimit:{self.key_prefix}:{identifier}"
        while True:
            # Simple fixed window or sliding window?
            # Requirements say "Global Rate Limit of 2 request per second is mandatory"
            # "respect discord per guild rate limit of 50 request per second"

            # Using a simple Lua script for atomic rate limiting
            lua = """
            local key = KEYS[1]
            local limit = tonumber(ARGV[1])
            local period = tonumber(ARGV[2])
            local current = redis.call("GET", key)
            if current and tonumber(current) >= limit then
                return redis.call("PTTL", key)
            else
                redis.call("INCR", key)
                if not current then
                    redis.call("PEXPIRE", key, period * 1000)
                end
                return 0
            end
            """
            # RedisCluster.register_script is not always available or behaves differently
            # We can use eval
            wait_ms = self.valkey.eval(lua, 1, key, self.rate, self.period)

            if wait_ms == 0:
                return True

            await asyncio.sleep(wait_ms / 1000.0)

global_limiter = None

def get_global_limiter(valkey):
    global global_limiter
    if global_limiter is None:
        global_limiter = RateLimiter(valkey, "global", 2, 1.0)
    return global_limiter

def get_guild_limiter(valkey):
    return RateLimiter(valkey, "guild", 50, 1.0)
