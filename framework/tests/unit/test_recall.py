from framework.eval.metrics.recall import recall_at_k, hit_at_k, recency_weighted_recall_at_k, mrr

def test_recall_at_k():
    assert recall_at_k(["a","b","c"], ["a","d"], k=5) == 0.5
    assert recall_at_k(["x"], ["y"], k=5) == 0.0
    assert recall_at_k(["a","b"], ["a","b"], k=2) == 1.0

def test_hit_at_k():
    assert hit_at_k(["a","b"], ["a"], k=2) == 1
    assert hit_at_k(["x"], ["a"], k=2) == 0

def test_mrr_takes_first_match():
    assert mrr(["x","y","a"], ["a"]) == 1/3

def test_recency_weighted_recall_decays():
    a = recency_weighted_recall_at_k(
        [{"citation": "a", "age_days": 0}], ["a"], k=5)
    b = recency_weighted_recall_at_k(
        [{"citation": "a", "age_days": 365}], ["a"], k=5)
    assert a > b
