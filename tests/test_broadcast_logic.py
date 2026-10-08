import pytest

def check_broadcast_condition(user_name, score, status):
    # Broadcast rule: manual indexing always broadcasts, system discovery requires 25+ votes or non-Open
    is_manual = (user_name != "System Discovery")
    meets_criteria = (score >= 25 or status.lower() != "open")

    should_broadcast = is_manual or meets_criteria
    return should_broadcast

def test_broadcast_cases():
    # Case: Manual indexing, Open status, low score
    assert check_broadcast_condition("Tony", 10, "open") == True

    # Case: Manual indexing, Complete status
    assert check_broadcast_condition("Tony", 10, "complete") == True

    # Case: System Discovery, Open status, low score
    assert check_broadcast_condition("System Discovery", 10, "open") == False

    # Case: System Discovery, Open status, high score
    assert check_broadcast_condition("System Discovery", 30, "open") == True

    # Case: System Discovery, Planned status
    assert check_broadcast_condition("System Discovery", 5, "planned") == True
