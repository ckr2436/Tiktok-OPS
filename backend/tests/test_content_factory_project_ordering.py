from types import SimpleNamespace
from unittest.mock import MagicMock

from app.features.tenants.hermes_agent import router as hermes_router


def test_member_project_list_uses_newest_id_as_stable_tiebreaker(monkeypatch):
    query = MagicMock()
    query.order_by.return_value.limit.return_value.all.return_value = []
    monkeypatch.setattr(
        hermes_router,
        "visible_project_query",
        lambda *_args, **_kwargs: query,
    )
    monkeypatch.setattr(
        hermes_router,
        "ensure_user_can_use_task",
        lambda *_args, **_kwargs: None,
    )

    result = hermes_router.list_content_factory_projects(
        workspace_id=3,
        me=SimpleNamespace(id=6),
        db=MagicMock(),
    )

    order_terms = query.order_by.call_args.args
    assert [str(term) for term in order_terms] == [
        "hermes_content_factory_projects.updated_at DESC",
        "hermes_content_factory_projects.id DESC",
    ]
    assert result == {"items": []}


def test_admin_project_list_uses_newest_id_as_stable_tiebreaker():
    db = MagicMock()
    query = MagicMock()
    db.query.return_value.filter.return_value = query
    query.order_by.return_value.limit.return_value.all.return_value = []

    result = hermes_router.list_admin_content_factory_projects(
        workspace_id=3,
        creator_user_id=None,
        me=SimpleNamespace(id=1),
        db=db,
    )

    order_terms = query.order_by.call_args.args
    assert [str(term) for term in order_terms] == [
        "hermes_content_factory_projects.updated_at DESC",
        "hermes_content_factory_projects.id DESC",
    ]
    assert result == {"items": []}
