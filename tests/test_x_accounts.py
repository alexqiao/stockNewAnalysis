from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory, build_engine, initialize_database
from trade_news_analysis.main import create_app
from trade_news_analysis.models import Article, Event, EventArticle, SourceHealth, XAccount, XPost
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.workflow_models import XClaim


@pytest.fixture
def client(session_factory: SessionFactory, settings: Settings) -> Iterator[TestClient]:
    coordinator = PipelineCoordinator(
        session_factory, settings, source_factory=lambda _securities, _settings: [],
    )
    with TestClient(
        create_app(settings, session_factory, coordinator), base_url='http://accounts.test',
    ) as result:
        yield result


def test_delete_account_preserves_promoted_evidence_and_research(
    client: TestClient, session_factory: SessionFactory,
) -> None:
    accounts = client.get('/api/v1/x/accounts').json()
    target, other = accounts[:2]
    now = datetime.now(UTC)
    with session_factory() as session:
        article = Article(
            fingerprint='saved-evidence', canonical_url='https://x.com/author/status/123',
            source=f"X:@{target['handle']}", title='已核验公告', story_cluster_id='cluster',
        )
        event = Event(event_key='saved-event', title='已核验事件')
        session.add_all([article, event])
        session.flush()
        link = EventArticle(article_id=article.id, event_id=event.id)
        post = XPost(
            account_id=target['id'], post_id='123', url=article.canonical_url,
            post_type='original', text='已保存原帖', published_at=now,
            promoted_article_id=article.id, related_event_id=event.id,
        )
        other_post = XPost(
            account_id=other['id'], post_id='456', url='https://x.com/other/status/456',
            post_type='original', published_at=now,
        )
        session.add_all([link, post, other_post])
        session.flush()
        claim = XClaim(
            claim_key='saved-claim', post_id=post.id, author=target['handle'],
            post_url=post.url, published_at=now, claim_text='历史核验主张',
            source_version='version-1', source_snapshot={'text': post.text},
            review_due_at=now + timedelta(days=7),
        )
        session.add_all([
            claim,
            SourceHealth(source=f"X:@{target['handle']}", capability='x_posts'),
            SourceHealth(source=f"X:@{other['handle']}", capability='x_posts'),
        ])
        session.commit()
        article_id, event_id, claim_id = article.id, event.id, claim.id
        post_id, other_post_id = post.id, other_post.id

    response = client.delete(f"/api/v1/x/accounts/{target['id']}")

    assert response.status_code == 200
    assert response.json() == {'deleted': True, 'account_id': target['id']}
    assert target['id'] not in [item['id'] for item in client.get('/api/v1/x/accounts').json()]
    with session_factory() as session:
        assert session.get(XAccount, target['id']) is None
        assert session.get(XPost, post_id) is None
        assert session.get(XPost, other_post_id) is not None
        assert session.get(Article, article_id) is not None
        assert session.get(Event, event_id) is not None
        assert session.scalar(select(EventArticle).where(EventArticle.article_id == article_id))
        saved_claim = session.get(XClaim, claim_id)
        assert saved_claim is not None
        assert saved_claim.post_id is None
        assert saved_claim.source_snapshot == {'text': '已保存原帖'}
        assert session.scalar(select(SourceHealth).where(
            SourceHealth.source == f"X:@{target['handle']}"
        )) is None
        assert session.scalar(select(SourceHealth).where(
            SourceHealth.source == f"X:@{other['handle']}"
        )) is not None
    assert client.delete(f"/api/v1/x/accounts/{target['id']}").status_code == 404
    assert client.post('/api/v1/x/accounts', json={'handle': target['handle']}).status_code == 201


def test_deleted_default_accounts_do_not_reappear_on_restart(
    client: TestClient, settings: Settings,
) -> None:
    for account in client.get('/api/v1/x/accounts').json():
        assert client.delete(f"/api/v1/x/accounts/{account['id']}").status_code == 200
    engine = build_engine(settings.database_url)
    try:
        initialize_database(engine, settings)
    finally:
        engine.dispose()
    assert client.get('/api/v1/x/accounts').json() == []
    assert '暂无账号' in client.get('/x/accounts').text


@pytest.fixture
def account_page(client: TestClient) -> Iterator[tuple[Any, dict[str, str], list[str]]]:
    playwright_api = pytest.importorskip('playwright.sync_api')
    failure: dict[str, str] = {}
    deletes: list[str] = []

    def route_request(route: Any) -> None:
        request = route.request
        url = urlsplit(request.url)
        if url.hostname != 'accounts.test':
            route.abort()
            return
        if request.method == 'DELETE':
            deletes.append(url.path)
            if failure.get('mode') == 'network':
                route.abort()
                return
            if failure.get('mode') == 'server':
                route.fulfill(status=503, content_type='text/plain', body='Unavailable')
                return
        headers = {}
        if 'content-type' in request.headers:
            headers['Content-Type'] = request.headers['content-type']
        response = client.request(
            request.method, url.path, content=request.post_data, headers=headers,
        )
        route.fulfill(status=response.status_code, headers=dict(response.headers),
                      body=response.content)

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
        except playwright_api.Error as exc:
            if 'executable' in str(exc).lower() or 'not found' in str(exc).lower():
                pytest.skip('Local Chrome is required for account button tests')
            raise
        try:
            page = browser.new_page()
            page.route('**/*', route_request)
            page.goto('http://accounts.test/x/accounts')
            yield page, failure, deletes
        finally:
            browser.close()


def test_delete_button_handles_cancel_errors_and_success(
    account_page: tuple[Any, dict[str, str], list[str]], client: TestClient,
) -> None:
    from playwright.sync_api import expect

    page, failure, deletes = account_page
    page.evaluate(
        "() => { window.confirm = () => { throw new Error('Native dialogs unavailable'); }; }"
    )
    page_errors: list[str] = []
    page.on('pageerror', lambda error: page_errors.append(str(error)))
    target = client.get('/api/v1/x/accounts').json()[0]
    row = page.locator(f"tr[data-id=\"{target['id']}\"]")
    button = row.get_by_role('button', name='删除', exact=True)
    confirmation = row.get_by_role('group', name='确认删除账号')
    confirm_button = row.get_by_role('button', name='确认删除', exact=True)
    message = page.get_by_role('status')

    button.click()
    expect(confirmation).to_be_visible()
    expect(confirmation).to_contain_text(target['handle'])
    assert deletes == []
    row.get_by_role('button', name='取消', exact=True).click()
    expect(confirmation).not_to_be_visible()
    expect(row).to_have_count(1)

    for mode, text in [('server', 'HTTP 503'), ('network', '网络连接失败')]:
        failure['mode'] = mode
        button.click()
        confirm_button.click()
        expect(message).to_contain_text(text)
        expect(row).to_have_count(1)
        expect(button).to_be_enabled()

    failure.clear()
    button.click()
    confirm_button.click()
    expect(row).to_have_count(0)
    expect(message).to_contain_text('已删除')
    assert len(deletes) == 3
    assert page_errors == []
    assert target['id'] not in [item['id'] for item in client.get('/api/v1/x/accounts').json()]
    page.reload()
    expect(row).to_have_count(0)


def test_save_and_add_buttons_still_work(
    account_page: tuple[Any, dict[str, str], list[str]], client: TestClient,
) -> None:
    from playwright.sync_api import expect

    page, _, _ = account_page
    target = client.get('/api/v1/x/accounts').json()[0]
    row = page.locator(f"tr[data-id=\"{target['id']}\"]")
    row.locator('[data-field="active"]').uncheck()
    row.get_by_role('button', name='保存', exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('账号已保存')
    saved = next(item for item in client.get('/api/v1/x/accounts').json()
                 if item['id'] == target['id'])
    assert saved['active'] is False
    page.locator('#new-handle').fill('new_author')
    page.get_by_role('button', name='添加', exact=True).click()
    expect(page.locator('tr[data-handle="new_author"]')).to_have_count(1)


def test_x_run_button_recovers_from_failure_and_refreshes_after_collection(
    account_page: tuple[Any, dict[str, str], list[str]],
) -> None:
    from playwright.sync_api import expect

    page, _, _ = account_page
    state = {'status': 'idle', 'reject': True}

    def respond(route: Any) -> None:
        if route.request.method == 'POST':
            if state['reject']:
                route.fulfill(status=503, json={'detail': '服务暂不可用'})
                return
            state['status'] = 'collecting'
            route.fulfill(status=202, json={'status': 'queued'})
            return
        route.fulfill(json={'status': state['status']})

    page.route('**/api/v1/runs/x-ingest', respond)
    page.route('**/api/v1/runs/x-ingest/status', respond)
    page.goto('http://accounts.test/x')
    button = page.get_by_role('button', name='立即抓取', exact=True)
    button.click()
    expect(page.get_by_role('status')).to_contain_text('服务暂不可用')
    expect(button).to_be_enabled()
    state['reject'] = False
    button.click()
    expect(page.get_by_role('status')).to_contain_text('正在抓取')
    expect(button).to_be_disabled()
    with page.expect_navigation():
        state['status'] = 'queued_screening'
    expect(page.get_by_role('status')).to_contain_text('原帖已入库')
    with page.expect_navigation():
        state['status'] = 'completed'
    expect(button).to_be_enabled()
