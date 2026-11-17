"""Import workflow against a real isolated SQLite store, plus HTTP and bot access checks.

PostgreSQL locking, migrations and concurrent requests are covered separately in CI.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import JSON, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import Base, get_async_session
from app.core.security import create_access_token
from app.finance.api import transactions, uploads
from app.finance.services import statement_imports as service
from app.finance.services.parser import BankStatementParser, ParsedTransaction
from app.models import Category, StatementImport, StatementImportRow, Transaction, User, Vacation

NOW = datetime(2026, 11, 10, 9, tzinfo=timezone.utc)


def parsed(description='Покупка', **kwargs):
    return ParsedTransaction(date=NOW, amount=Decimal('-100'), description=description, bank_name='СберБанк', **kwargs)


class Bridge:
    def __init__(self, session):
        self.session = session

    async def execute(self, stmt, **kwargs):
        return self.session.execute(stmt, **kwargs)

    def add(self, obj):
        self.session.add(obj)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def get(self, model, key):
        return self.session.get(model, key)

    async def delete(self, obj):
        self.session.delete(obj)


@pytest.fixture
def store(monkeypatch):
    engine = create_engine('sqlite://')

    @event.listens_for(engine, 'connect')
    def configure(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')

    # Only the common category dictionary uses PostgreSQL ARRAY.
    monkeypatch.setattr(Category.__table__.c.keywords, 'type', JSON())
    tables = [User.__table__, Category.__table__, StatementImport.__table__, Transaction.__table__,
              StatementImportRow.__table__, Vacation.__table__]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine, expire_on_commit=False) as session:
        session.add_all([User(id=1, telegram_id=111, name='Owner', role='admin'),
                         User(id=2, telegram_id=222, name='Partner', role='user')])
        session.commit()
        yield Bridge(session)
    engine.dispose()


@pytest_asyncio.fixture
async def api(store):
    app = FastAPI()
    app.include_router(uploads.router)
    app.include_router(transactions.router)

    async def dependency():
        yield store

    app.dependency_overrides[get_async_session] = dependency
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        yield client


def auth(user_id=1):
    return {'Authorization': 'Bearer ' + create_access_token(user_id)}


async def stage(store, rows=None, data=b'file', user_id=1, account_key=''):
    return await service.stage_import(store, user_id, 'statement.csv', data, rows or [parsed()], account_key)


async def confirm(store, batch, choices=None):
    preview = await service.preview_import(store, batch)
    return await service.commit_import(store, batch.id, batch.user_id, preview['revision'], choices)


@pytest.mark.asyncio
async def test_distinct_suffixes_and_identical_purchases_are_all_saved(store):
    rows = [parsed('A' * 40 + ' one'), parsed('A' * 40 + ' two'), parsed(), parsed()]
    batch = await stage(store, rows)
    assert store.session.scalars(select(Transaction)).all() == []
    preview = await service.preview_import(store, batch)
    assert preview['new_count'] == 4
    result = await confirm(store, batch)
    assert result['saved_new'] == 4
    txs = store.session.scalars(select(Transaction).order_by(Transaction.id)).all()
    assert [tx.description for tx in txs] == [row.description for row in rows]
    assert [tx.import_occurrence for tx in txs[-2:]] == [1, 2]
    assert all(tx.import_id == batch.id for tx in txs)


@pytest.mark.asyncio
async def test_same_file_and_confirmation_retries_are_idempotent(store):
    batch = await stage(store)
    repeat = await stage(store)
    assert batch.id == repeat.id
    result = await confirm(store, batch)
    repeated = await service.commit_import(store, batch.id, 1, 'old revision')
    assert result['saved_new'] == repeated['saved_new'] == 1
    assert (await stage(store)).status == 'committed'
    assert len(store.session.scalars(select(Transaction)).all()) == 1


@pytest.mark.asyncio
async def test_overlapping_file_requires_explicit_decision_and_preserves_multiplicity(store):
    await confirm(store, await stage(store))
    batch = await stage(store, [parsed(), parsed()], data=b'overlap')
    preview = await service.preview_import(store, batch)
    assert [row['action'] for row in preview['rows']] == ['review', 'new']
    with pytest.raises(service.ImportErrorBase, match='Строка 1'):
        await confirm(store, batch)
    assert len(store.session.scalars(select(Transaction)).all()) == 1
    result = await confirm(store, batch, {1: 'skip'})
    assert result['saved_new'] == 1 and result['skipped_duplicates'] == 1
    assert len(store.session.scalars(select(Transaction)).all()) == 2


@pytest.mark.asyncio
async def test_changed_description_is_reviewed_not_silently_duplicated(store):
    await confirm(store, await stage(store, [parsed('Описание PDF')]))
    batch = await stage(store, [parsed('Описание CSV')], data=b'csv')
    preview = await service.preview_import(store, batch)
    assert preview['review_count'] == 1
    assert preview['rows'][0]['candidates'][0]['description'] == 'Описание PDF'
    assert (await confirm(store, batch, {1: 'add'}))['saved_new'] == 1


@pytest.mark.asyncio
async def test_explicit_new_identical_purchase_gets_next_slot(store):
    await confirm(store, await stage(store))
    batch = await stage(store, data=b'another file')
    assert (await confirm(store, batch, {1: 'add'}))['saved_new'] == 1
    assert [t.import_occurrence for t in store.session.scalars(select(Transaction).order_by(Transaction.id))] == [1, 2]


@pytest.mark.asyncio
async def test_accounts_and_users_do_not_hide_each_others_operations(store):
    for user_id, account_key in [(1, 'main'), (1, 'savings'), (2, 'main')]:
        batch = await stage(store, user_id=user_id, account_key=account_key)
        assert (await service.preview_import(store, batch))['new_count'] == 1
        await confirm(store, batch)
    assert len(store.session.scalars(select(Transaction)).all()) == 3


@pytest.mark.asyncio
async def test_old_unknown_account_is_reviewed_without_modifying_legacy_row(store):
    tx = Transaction(user_id=1, amount=-100, description='Покупка', date=NOW,
                     bank_name='СберБанк', is_parsed_automatically=False)
    store.add(tx)
    await store.commit()
    batch = await stage(store, account_key='main')
    assert (await service.preview_import(store, batch))['review_count'] == 1
    await confirm(store, batch, {1: 'skip'})
    assert (await service.revert_import(store, batch.id, 1))['deleted_transactions'] == 0
    assert store.session.get(Transaction, tx.id) is tx


@pytest.mark.asyncio
async def test_stable_bank_id_deduplicates_and_conflicting_data_cannot_overwrite(store):
    batch = await stage(store, [parsed(external_id='00001')])
    await confirm(store, batch)
    repeat = await stage(store, [parsed(external_id='00001'), parsed(external_id='00001')], data=b'copy')
    preview = await service.preview_import(store, repeat)
    assert [r['action'] for r in preview['rows']] == ['duplicate', 'duplicate_in_file']
    assert (await confirm(store, repeat))['saved_new'] == 0
    conflict = await stage(store, [parsed('Другой текст', external_id='00001')], data=b'conflict')
    assert (await service.preview_import(store, conflict))['conflict_count'] == 1
    with pytest.raises(service.ImportErrorBase, match='только пропустить'):
        await confirm(store, conflict, {1: 'add'})
    await confirm(store, conflict, {1: 'skip'})
    assert store.session.scalar(select(Transaction)).description == 'Покупка'


@pytest.mark.asyncio
async def test_different_bank_ids_are_different_operations(store):
    await confirm(store, await stage(store, [parsed(external_id='one')]))
    batch = await stage(store, [parsed(external_id='two')], data=b'different')
    assert (await service.preview_import(store, batch))['new_count'] == 1
    await confirm(store, batch)


@pytest.mark.asyncio
async def test_inconsistent_id_in_one_file_is_rejected_without_transactions(store):
    with pytest.raises(service.ImportErrorBase, match='разными данными'):
        await stage(store, [parsed(external_id='one'), parsed('Другой текст', external_id='one')])
    await store.rollback()
    assert store.session.scalars(select(StatementImport)).all() == []
    assert store.session.scalars(select(Transaction)).all() == []


@pytest.mark.asyncio
async def test_stale_preview_after_another_import_requires_refresh(store):
    first = await stage(store)
    second = await stage(store, data=b'second')
    preview = await service.preview_import(store, second)
    await confirm(store, first)
    with pytest.raises(service.ImportChanged):
        await service.commit_import(store, second.id, 1, preview['revision'])
    assert (await service.preview_import(store, second))['review_count'] == 1


@pytest.mark.asyncio
async def test_undo_keeps_shared_operation_then_removes_it_with_last_import(store):
    first = await stage(store)
    await confirm(store, first)
    second = await stage(store, data=b'overlap')
    await confirm(store, second, {1: 'skip'})
    assert (await service.revert_import(store, first.id, 1))['deleted_transactions'] == 0
    assert len(store.session.scalars(select(Transaction)).all()) == 1
    assert (await service.revert_import(store, second.id, 1))['deleted_transactions'] == 1
    assert store.session.scalars(select(Transaction)).all() == []
    assert (await service.revert_import(store, second.id, 1))['deleted_transactions'] == 0
    assert (await stage(store)).id != first.id


@pytest.mark.asyncio
async def test_undo_pending_and_user_boundaries(store):
    batch = await stage(store)
    with pytest.raises(service.ImportNotFound):
        await service.owned_import(store, batch.id, 2)
    with pytest.raises(service.ImportNotFound):
        await service.revert_import(store, batch.id, 2)
    assert (await service.revert_import(store, batch.id, 1))['deleted_transactions'] == 0
    with pytest.raises(service.ImportChanged):
        await service.commit_import(store, batch.id, 1, 'ignored')


@pytest.mark.asyncio
async def test_failure_rolls_back_entire_commit(store, monkeypatch):
    batch = await stage(store, [parsed(), parsed('Ещё покупка')])
    original = store.flush
    calls = 0

    async def failing_flush():
        nonlocal calls
        calls += 1
        await original()
        if calls == 2:
            raise RuntimeError('simulated storage failure')

    monkeypatch.setattr(store, 'flush', failing_flush)
    with pytest.raises(RuntimeError):
        await confirm(store, batch)
    await store.rollback()
    assert store.session.scalars(select(Transaction)).all() == []
    assert (await service.owned_import(store, batch.id, 1)).status == 'pending'


@pytest.mark.parametrize('constraint', ['slot', 'bank_id'])
@pytest.mark.asyncio
async def test_database_enforces_import_identity(store, constraint):
    batch = await stage(store, [parsed(external_id='unique' if constraint == 'bank_id' else None)])
    await confirm(store, batch)
    original = store.session.scalar(select(Transaction))
    copy = Transaction(user_id=1, amount=original.amount, description=original.description,
        date=original.date, bank_name=original.bank_name, account_key=original.account_key,
        external_id=original.external_id, import_fingerprint=original.import_fingerprint,
        import_occurrence=original.import_occurrence if constraint == 'slot' else 2)
    store.add(copy)
    with pytest.raises(IntegrityError):
        await store.flush()
    await store.rollback()


def test_csv_keeps_full_account_and_operation_identifiers_and_amount_precision():
    data = ('Дата;Сумма;Описание;Номер счёта;Идентификатор операции\n'
            '10.11.2026;-100,01;Покупка;00000000000000000001;00000000000000000002\n').encode('utf-8')
    row = BankStatementParser.parse_file(data, 'test.csv', 'sber')[0]
    assert row.account_key == '00000000000000000001'
    assert row.external_id == '00000000000000000002'
    assert row.amount == Decimal('-100.01') and row.bank_name == 'СберБанк'


@pytest.mark.asyncio
async def test_http_preview_confirm_history_undo_and_isolation(api, store, monkeypatch):
    monkeypatch.setattr(BankStatementParser, 'parse_file_async', AsyncMock(return_value=[parsed()]))
    assert (await api.get('/api/imports')).status_code == 401
    response = await api.post('/api/upload', headers=auth(2), files={'file': ('test.csv', b'data')})
    assert response.status_code == 200
    preview = response.json()
    assert preview['status'] == 'pending' and preview['saved_new'] == 0
    import_id = preview['import_id']
    assert (await api.get(f'/api/imports/{import_id}', headers=auth(1))).status_code == 404
    body = {'revision': preview['revision']}
    assert (await api.post(f'/api/imports/{import_id}/confirm', headers=auth(1), json=body)).status_code == 404
    assert (await api.post(f'/api/imports/{import_id}/revert', headers=auth(1))).status_code == 404
    # Admin may explicitly select the owner's profile; default history is still personal.
    assert (await api.get(f'/api/imports/{import_id}?target_user_id=2', headers=auth(1))).status_code == 200
    result = await api.post(f'/api/imports/{import_id}/confirm', headers=auth(2), json=body)
    assert result.status_code == 200 and result.json()['saved_new'] == 1
    txs = (await api.get(f'/api/transactions?import_id={import_id}', headers=auth(2))).json()
    assert txs['total'] == 1 and txs['items'][0]['import_id'] == import_id
    assert 'account_key' in txs['items'][0] and 'external_id' in txs['items'][0]
    assert (await api.get(f'/api/transactions?import_id={import_id}', headers=auth(1))).json()['total'] == 0
    assert len((await api.get('/api/imports', headers=auth(2))).json()) == 1
    assert (await api.get('/api/imports', headers=auth(1))).json() == []
    result = await api.post(f'/api/imports/{import_id}/revert', headers=auth(2))
    assert result.status_code == 200 and result.json()['deleted_transactions'] == 1


@pytest.mark.asyncio
async def test_http_ambiguous_and_changed_preview_errors(api, store, monkeypatch):
    await confirm(store, await stage(store))
    monkeypatch.setattr(BankStatementParser, 'parse_file_async', AsyncMock(return_value=[parsed()]))
    data = (await api.post('/api/upload', headers=auth(), files={'file': ('test.csv', b'other')})).json()
    endpoint = f"/api/imports/{data['import_id']}/confirm"
    assert (await api.post(endpoint, headers=auth(), json={'revision': data['revision']})).status_code == 422
    assert (await api.post(endpoint, headers=auth(), json={'revision': '0' * 64})).status_code == 409
    result = await api.post(endpoint, headers=auth(), json={'revision': data['revision'], 'choices': {'1': 'skip'}})
    assert result.status_code == 200 and result.json()['saved_new'] == 0


@pytest.mark.asyncio
async def test_http_persistence_failure_is_not_reported_as_bad_file(api, monkeypatch):
    monkeypatch.setattr(BankStatementParser, 'parse_file_async', AsyncMock(return_value=[parsed()]))
    monkeypatch.setattr(uploads, 'stage_import', AsyncMock(side_effect=RuntimeError('storage failure')))
    broker = AsyncMock()
    monkeypatch.setattr(uploads.BrokerReportParser, 'parse_file_async', broker)
    with pytest.raises(RuntimeError, match='storage failure'):
        await api.post('/api/upload', headers=auth(), files={'file': ('test.csv', b'data')})
    broker.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_confirmation_and_callback_owner_checks(store, monkeypatch):
    from app.bot.handlers import documents
    batch = await stage(store)
    preview = await service.preview_import(store, batch)
    answer, edit = AsyncMock(), AsyncMock()
    callback = SimpleNamespace(data=f"statement:confirm:{batch.id}:{preview['revision'][:16]}",
        answer=answer, message=SimpleNamespace(edit_text=edit, answer=AsyncMock()))
    monkeypatch.setattr(documents, 'check_category_limits', AsyncMock(return_value=[]))
    await documents.handle_import_action(callback, store, store.session.get(User, 2))
    assert answer.call_args.kwargs['show_alert'] is True
    assert store.session.scalars(select(Transaction)).all() == []
    await documents.handle_import_action(callback, store, store.session.get(User, 1))
    assert len(store.session.scalars(select(Transaction)).all()) == 1
    assert 'Добавлено: 1' in edit.call_args.args[0]


def test_timezone_equivalence_and_different_full_descriptions():
    row = SimpleNamespace(date=NOW, amount=Decimal('-100'), bank_name='Bank', account_key='',
                          description='A' * 40 + ' one', external_id=None)
    other = SimpleNamespace(**vars(row))
    other.date = NOW.astimezone(timezone(timedelta(hours=3)))
    assert service.content_key(row) == service.content_key(other)
    other.description = 'A' * 40 + ' two'
    assert service.fingerprint(row) != service.fingerprint(other)
