from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from trade_news_analysis.services.metrics import build_metrics

TEMPLATES = Path(__file__).parents[1] / "src/trade_news_analysis/templates"


def render(macro: str, value: Any) -> str:
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    template = environment.from_string(
        "{% from '_workflow.html' import " + macro + " %}{{ " + macro + "(value) }}"
    )
    return template.render(value=value)


def example_claim() -> dict[str, Any]:
    return {
        "id": 3, "author": "researcher", "post_url": "https://x.com/researcher/status/123",
        "published_at": "2026-09-16T08:00:00+00:00", "review_due_at": "2026-09-23T08:00:00+00:00",
        "claim_text": "<script>claim injection</script>", "claim_kind": "fact_claim",
        "status": "partially_supported", "status_label": "部分有依据", "source_truncated": True,
        "verification_needs": ["核对原始公告"], "note": "核对中 <script>note injection</script>",
        "independent_evidence_count": 2, "official_support_count": 1, "official_conflict_count": 1,
        "evidence": [
            {"url": "https://issuer.example/support", "source_url": "https://issuer.example/origin",
             "stance": "support", "is_official": True, "note": "披露已签署合同"},
            {"url": "https://issuer.example/correction", "source_url": "",
             "stance": "conflict", "is_official": True, "note": "补充说明交付条件"},
        ],
        "source_snapshot": {"text": "Original saved post", "quoted_text": "Quoted text"},
        "securities": [{"security_id": 2, "match_basis": "entity"}],
        "suggested_sources": [{"url": "https://issuer.example/suggestion", "title": "候选公告",
                               "reason": "仅关键词匹配", "is_official": True}],
        "history": [{"created_at": "2026-09-16T08:00:00+00:00", "reason": "manual_review",
                     "from_status": "pending", "to_status": "partially_supported",
                     "note": "人工核对", "snapshot": {"claim_text": "Original claim"}}],
    }


def test_saved_task_renders_explicit_update_form_and_existing_note() -> None:
    html = render("task_controls", {
        "id": 8, "status": "done", "status_label": "已完成核验", "note": "已检查公告日期",
        "review_due_at": "2026-09-23T08:00:00+00:00", "needs_refresh": False, "history": [],
    })
    assert 'data-workflow-form="task"' in html
    assert 'data-record-id="8"' in html
    assert "已检查公告日期" in html
    assert 'name="note"' in html
    assert 'name="review_due_at"' in html
    assert 'value="done" selected' in html


def test_unsaved_task_offers_capture_without_invalid_update_form() -> None:
    html = render("task_controls", {
        "id": None, "status": "pending", "needs_refresh": True, "history": [],
    })
    assert "data-workflow-capture" in html
    assert "先保存当前自选行动" in html
    assert 'data-workflow-form="task"' not in html


def test_claim_editor_preserves_complete_existing_evidence_and_escapes_untrusted_text() -> None:
    html = render("claims_panel", [example_claim()])
    assert 'data-workflow-form="claim"' in html
    assert 'data-record-id="3"' in html
    assert "&lt;script&gt;claim injection&lt;/script&gt;" in html
    assert "<script>claim injection</script>" not in html
    assert "&lt;script&gt;note injection&lt;/script&gt;" in html
    assert 'value="https://issuer.example/support"' in html
    assert 'value="https://issuer.example/correction"' in html
    assert 'value="https://issuer.example/origin"' in html
    assert 'value="conflict" selected' in html
    assert "独立材料 2 份 · 官方支持 1 份 · 官方反证 1 份" in html
    assert "原帖正文有截断" in html
    assert "本次保存的完整材料列表" in html
    assert "关键词相似不能证明主张成立" in html
    assert "当时主张：Original claim" in html


def test_changed_claim_requires_refresh_before_manual_verification() -> None:
    claim = {**example_claim(), "needs_refresh": True}
    html = render("claims_panel", [claim])
    assert "data-workflow-capture" in html
    assert 'data-workflow-form="claim"' not in html
    assert "data-add-candidate" not in html


def test_empty_claim_panel_explains_capture_action() -> None:
    html = render("claims_panel", [])
    assert "尚无已保存的待核验主张" in html
    assert "data-workflow-capture" in html


def test_metrics_page_displays_every_evidence_rule_without_hiding_mixed_history(
    session: Session,
) -> None:
    metrics = build_metrics(session)
    version = metrics["evaluation_version"]
    groups = metrics["by_evaluation_version"][version]["by_evidence_rule_version"]
    mixed = "mixed:legacy-v1+original-sources-v2"
    groups[mixed] = {**groups["legacy-v1"], "sample_size": 77}
    environment = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    html = environment.get_template("metrics.html").render(
        metrics=metrics, url_for=lambda _name, *, path: "/static" + path,
    )
    assert 'data-evidence-rule="legacy-v1"' in html
    assert 'data-evidence-rule="original-sources-v2"' in html
    mixed_row = html.split(f'data-evidence-rule="{mixed}"', 1)[1].split("</tr>", 1)[0]
    assert "<td>77</td>" in mixed_row
    assert "当前主统计" in html
