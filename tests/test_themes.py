from trade_news_analysis.services.themes import canonicalize_theme, theme_key


def test_theme_aliases_merge_only_explicit_synonyms() -> None:
    assert canonicalize_theme("AI算力芯片") == "AI计算芯片"
    assert canonicalize_theme(" ai 数据中心 gpu ") == "数据中心GPU"
    assert canonicalize_theme("HBM存储") == "HBM存储"
    assert theme_key("AI 数据中心-GPU") == "ai数据中心gpu"
