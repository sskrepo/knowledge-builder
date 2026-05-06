from framework.core import urns

def test_make_and_parse():
    u = urns.make("resource", "pod")
    assert u == "urn:faaas:resource:pod"
    p = urns.parse(u)
    assert p["kind"] == "resource" and p["id"] == "pod"

def test_chunk_variant():
    u = urns.content("incidents", "INC-1", 3)
    p = urns.parse(u)
    assert p["chunk"] == 3
