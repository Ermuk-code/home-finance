"""Цепочка миграций Alembic линейна и имеет единственную «голову» (проверка без запуска БД)."""
import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _revisions():
    result = {}
    for path in VERSIONS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        rev = re.search(r"^revision: str = '([0-9a-f]+)'", text, re.M).group(1)
        down = re.search(r"^down_revision: .*?= (None|'([0-9a-f]+)')", text, re.M)
        result[rev] = down.group(2)
    return result


def test_single_head_and_linear_chain():
    revs = _revisions()
    parents = [p for p in revs.values() if p]
    assert len(parents) == len(set(parents)), "у одной ревизии несколько потомков (ветвление)"
    heads = [r for r in revs if r not in parents]
    assert heads == ["3e8a5c1d9b47"]
    roots = [r for r, p in revs.items() if p is None]
    assert len(roots) == 1
    # цепочка проходит через все ревизии
    seen, cur = 0, heads[0]
    while cur:
        seen += 1
        cur = revs[cur]
    assert seen == len(revs)
