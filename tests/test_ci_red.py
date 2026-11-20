def test_ci_turns_red() -> None:
    """故意失敗，確認 CI 會變紅；下一個 commit 會刪掉。"""
    assert 1 == 2
