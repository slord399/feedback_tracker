import asyncio
import json

class MockValkey:
    def __init__(self):
        self.processed = set()
        self.author_posts = {}
        self.author_milestones = {}

    async def sismember(self, name, value):
        return value in self.processed

    async def sadd(self, name, value):
        self.processed.add(value)
        return 1

    async def zincrby(self, name, amount, value):
        if name == "metrics:author_posts":
            self.author_posts[value] = self.author_posts.get(value, 0) + amount
        elif name == "metrics:author_milestones":
            self.author_milestones[value] = self.author_milestones.get(value, 0) + amount
        return self.author_posts.get(value) or self.author_milestones.get(value)

async def test_idempotency():
    valkey = MockValkey()

    async def process_post(pid, author_id, score, status):
        if not await valkey.sismember("metrics:processed_posts", pid):
            await valkey.zincrby("metrics:author_posts", 1, author_id)
            milestones_list = [25, 50, 100]
            milestones_reached = len([m for m in milestones_list if score >= m])
            if status.lower() in ["complete", "completed", "available in future release"]:
                milestones_reached += 1
            if milestones_reached > 0:
                await valkey.zincrby("metrics:author_milestones", milestones_reached, author_id)
            await valkey.sadd("metrics:processed_posts", pid)

    # First time
    await process_post("p1", "a1", 30, "open")
    assert valkey.author_posts["a1"] == 1
    assert valkey.author_milestones["a1"] == 1 # 30 >= 25

    # Second time - should be ignored
    await process_post("p1", "a1", 30, "open")
    assert valkey.author_posts["a1"] == 1
    assert valkey.author_milestones["a1"] == 1

    # Another post for same author
    await process_post("p2", "a1", 60, "complete")
    assert valkey.author_posts["a1"] == 2
    assert valkey.author_milestones["a1"] == 1 + 2 + 1 # 1 (p1) + 2 (60 >= 50) + 1 (complete) = 4

    # Simulate an update to an existing post
    async def update_post(pid, author_id, old_score, new_score, old_status, new_status):
        # This imitates the logic in the poller when old_json exists
        milestones = [25, 50, 100]

        # Status change check
        if old_status != new_status:
            if new_status.lower() in ["complete", "completed", "available in future release"] and await valkey.sismember("metrics:processed_posts", pid):
                await valkey.zincrby("metrics:author_milestones", 1, author_id)

        # Score change check
        current_milestone_val = max([m for m in milestones if new_score >= m] + [0])
        last_milestone_val = max([m for m in milestones if old_score >= m] + [0])
        if current_milestone_val > last_milestone_val:
            if await valkey.sismember("metrics:processed_posts", pid):
                new_passed = [m for m in milestones if new_score >= m and m > last_milestone_val]
                await valkey.zincrby("metrics:author_milestones", len(new_passed), author_id)

    # Update p1: score 30 -> 60. Should add 1 milestone (passed 50)
    await update_post("p1", "a1", 30, 60, "open", "open")
    assert valkey.author_milestones["a1"] == 4 + 1 # 5

    # Update p1: status open -> complete. Should add 1 milestone
    await update_post("p1", "a1", 60, 60, "open", "complete")
    assert valkey.author_milestones["a1"] == 5 + 1 # 6

    # Update a NOT processed post (should not happen in real bot but testing logic)
    # If a post somehow isn't in processed_posts yet, update_post shouldn't increment
    await update_post("p3", "a2", 0, 30, "open", "open")
    assert valkey.author_milestones.get("a2", 0) == 0

    print("Idempotency test passed!")

if __name__ == "__main__":
    asyncio.run(test_idempotency())
