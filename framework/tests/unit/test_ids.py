from framework.core.ids import content_item_id, chunk_id, source_sha

def test_content_item_id_is_deterministic():
    a = content_item_id("jira", "INC-1", 1)
    b = content_item_id("jira", "INC-1", 1)
    assert a == b
    assert content_item_id("jira", "INC-1", 2) != a

def test_chunk_id_format():
    assert chunk_id("abc", 7) == "abc#chunk_7"

def test_source_sha_changes_with_text():
    assert source_sha("a") != source_sha("b")
