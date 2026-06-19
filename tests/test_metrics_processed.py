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
        # This imitates the top block in the poller
        just_processed = False
        if not await valkey.sismember("metrics:processed_posts", pid):
            await valkey.zincrby("metrics:author_posts", 1, author_id)
            milestones_list = [25, 50, 100]
            milestones_reached = len([m for m in milestones_list if score >= m])
            if status.lower() in ["complete", "completed", "available in future release"]:
                milestones_reached += 1
            if milestones_reached > 0:
                await valkey.zincrby("metrics:author_milestones", milestones_reached, author_id)
            await valkey.sadd("metrics:processed_posts", pid)
            just_processed = True
        return just_processed

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
    assert valkey.author_milestones["a1"] == 1 + 2 + 1 # 1 (p1) + 2 (p2 score) + 1 (p2 status) = 4

    # Simulate the poller loop with update logic
    async def poller_step(pid, author_id, old_score, new_score, old_status, new_status, has_old_json):
        milestones = [25, 50, 100]

        # 1. Top block (initial discovery/processing)
        just_processed = await process_post(pid, author_id, new_score, new_status)

        # 2. Transition blocks (only if old_json existed)
        if has_old_json:
            # Status change check
            if old_status != new_status:
                if new_status.lower() in ["complete", "completed", "available in future release"] and not just_processed:
                    await valkey.zincrby("metrics:author_milestones", 1, author_id)

            # Score change check
            current_milestone_val = max([m for m in milestones if new_score >= m] + [0])
            last_milestone_val = max([m for m in milestones if old_score >= m] + [0])
            if current_milestone_val > last_milestone_val:
                if not just_processed:
                    new_passed = [m for m in milestones if new_score >= m and m > last_milestone_val]
                    await valkey.zincrby("metrics:author_milestones", len(new_passed), author_id)

    # p1: score 30 -> 60. Should add 1 milestone (passed 50)
    await poller_step("p1", "a1", 30, 60, "open", "open", True)
    assert valkey.author_milestones["a1"] == 4 + 1 # 5

    # p1: status open -> complete. Should add 1 milestone
    await poller_step("p1", "a1", 60, 60, "open", "complete", True)
    assert valkey.author_milestones["a1"] == 5 + 1 # 6

    # New post p3 seen for the first time by poller
    await poller_step("p3", "a2", 0, 30, "open", "open", False)
    assert valkey.author_posts["a2"] == 1
    assert valkey.author_milestones["a2"] == 1 # 30 >= 25

    # New post p4 seen for the first time, but it already has high score and is complete
    # (Simulating migration/recursive crawl of old posts)
    await poller_step("p4", "a2", 0, 100, "complete", "complete", False)
    assert valkey.author_posts["a2"] == 2
    assert valkey.author_milestones["a2"] == 1 + 3 + 1 # 1 (p3) + 3 (p4 score) + 1 (p4 status) = 5

    # Now simulate p4 getting an update in a later poll. It should NOT double count its initial state.
    # p4: status complete -> complete (no change), score 100 -> 110 (no milestone change)
    await poller_step("p4", "a2", 100, 110, "complete", "complete", True)
    assert valkey.author_milestones["a2"] == 5 # No change

    print("Idempotency test passed!")

if __name__ == "__main__":
    asyncio.run(test_idempotency())
