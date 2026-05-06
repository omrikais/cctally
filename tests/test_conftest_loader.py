from conftest import load_script


def test_load_script_returns_namespace_with_block_dataclass():
    ns = load_script()
    assert "Block" in ns
    assert "_group_entries_into_blocks" in ns
    assert "cmd_blocks" in ns
    assert "BLOCK_DURATION" in ns
