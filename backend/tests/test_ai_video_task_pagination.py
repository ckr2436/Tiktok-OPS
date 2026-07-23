from types import SimpleNamespace

# Initialize the task registry before importing the HTTP router. The production
# app does this during startup; importing the router first would expose the
# intentional Celery task-module cycle only in this isolated unit test.
from app.celery_app import celery_app as _celery_app  # noqa: F401
from app.features.tenants.bandianwa_ai.router_videos import _paged_task_rows


class _FakeQuery:
    def __init__(self, *, id_rows, tasks):
        self.id_rows = list(id_rows)
        self.tasks = list(tasks)
        self.narrow = False
        self.offset_value = None
        self.limit_value = None

    def with_entities(self, *_args):
        self.narrow = True
        return self

    def order_by(self, *_args):
        return self

    def offset(self, value):
        self.offset_value = value
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def filter(self, *_args):
        self.narrow = False
        return self

    def all(self):
        if self.narrow:
            start = int(self.offset_value or 0)
            end = start + int(self.limit_value or len(self.id_rows))
            return [(value,) for value in self.id_rows[start:end]]
        return list(self.tasks)


def test_task_pagination_sorts_only_ids_then_preserves_requested_order():
    query = _FakeQuery(
        id_rows=[9, 7, 5, 3],
        tasks=[SimpleNamespace(id=7), SimpleNamespace(id=9)],
    )

    rows = _paged_task_rows(query, offset=0, size=2)

    assert [row.id for row in rows] == [9, 7]
    assert query.offset_value == 0
    assert query.limit_value == 2


def test_task_pagination_returns_empty_without_hydrating_rows():
    query = _FakeQuery(id_rows=[], tasks=[SimpleNamespace(id=1)])

    assert _paged_task_rows(query, offset=20, size=10) == []
    assert query.narrow is True
