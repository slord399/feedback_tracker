import asyncio

def test_broadcast_condition(user_name, score, status):
    # Broadcast rule: manual indexing always broadcasts, system discovery requires 25+ votes or non-Open
    is_manual = (user_name != "System Discovery")
    meets_criteria = (score >= 25 or status.lower() != "open")

    should_broadcast = is_manual or meets_criteria
    return should_broadcast

def run_tests():
    # Case: Manual indexing, Open status, low score
    # This was the specific issue: it should broadcast now
    assert test_broadcast_condition("Tony", 10, "open") == True

    # Case: Manual indexing, Complete status
    assert test_broadcast_condition("Tony", 10, "complete") == True

    # Case: System Discovery, Open status, low score
    # Should NOT broadcast (it's the system just discovering noise)
    assert test_broadcast_condition("System Discovery", 10, "open") == False

    # Case: System Discovery, Open status, high score
    # Should broadcast (milestone reached)
    assert test_broadcast_condition("System Discovery", 30, "open") == True

    # Case: System Discovery, Planned status
    # Should broadcast (status change/interesting)
    assert test_broadcast_condition("System Discovery", 5, "planned") == True

    print("Broadcast condition tests passed!")

if __name__ == "__main__":
    run_tests()
