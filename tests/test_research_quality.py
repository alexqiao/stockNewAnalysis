from trade_news_analysis.models import Security
from trade_news_analysis.services.research_quality import (
    apply_company_data_caps,
    company_data_quality,
)


def test_missing_company_data_caps_model_dimensions() -> None:
    security = Security(
        market="A",
        exchange="SZ",
        symbol="000001.SZ",
        name="示例公司",
    )
    dimensions = {
        "business_purity": 5.0,
        "scale_elasticity": 5.0,
        "transmission_clarity": 5.0,
    }

    adjusted, gaps = apply_company_data_caps(security, dimensions)

    assert company_data_quality(security)[0] == 0
    assert adjusted == {
        "business_purity": 1.0,
        "scale_elasticity": 2.0,
        "transmission_clarity": 3.0,
    }
    assert gaps == ["行业分类", "主营业务与收入结构", "市值"]
