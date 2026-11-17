"""Real PostgreSQL checks. Enabled only by an explicit disposable TEST_POSTGRES_URL.

CI migrates its dedicated database before this module. No application database is
used by default. SQLite tests deliberately do not claim to verify row locks.
"""
import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.finance.services import statement_imports as service
from app.models import StatementImport, Transaction, User
from tests.test_statement_imports import parsed

TEST_URL = os.environ.get('TEST_POSTGRES_URL')
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not TEST_URL, reason='requires disposable PostgreSQL')]


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(TEST_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        user = User(telegram_id=-int(uuid.uuid4().hex[:14], 16), name='Import test', role='user')
        session.add(user)
        await session.commit()
        user_id = user.id
    try:
        yield maker, user_id
    finally:
        async with maker() as session:
            await session.execute(delete(User).where(User.id == user_id))
            await session.commit()
        await engine.dispose()


async def test_concurrent_same_file_staging_and_double_confirmation(pg):
    maker, user_id = pg

    async def upload():
        async with maker() as session:
            batch = await service.stage_import(session, user_id, 'same.csv', b'same', [parsed()])
            return batch.id

    first, second = await asyncio.gather(upload(), upload())
    assert first == second
    async with maker() as session:
        batch = await service.owned_import(session, first, user_id)
        preview = await service.preview_import(session, batch)

    async def confirm():
        async with maker() as session:
            return await service.commit_import(session, first, user_id, preview['revision'])

    results = await asyncio.gather(confirm(), confirm())
    assert all(r['saved_new'] == 1 for r in results)
    async with maker() as session:
        assert len((await session.execute(select(Transaction).where(Transaction.user_id == user_id))).scalars().all()) == 1


async def test_concurrent_overlapping_files_require_new_review(pg):
    maker, user_id = pg
    ids, revisions = [], []
    for data in (b'one', b'two'):
        async with maker() as session:
            batch = await service.stage_import(session, user_id, 'test.csv', data, [parsed()])
            ids.append(batch.id)
            revisions.append((await service.preview_import(session, batch))['revision'])

    async def confirm(index):
        async with maker() as session:
            try:
                await service.commit_import(session, ids[index], user_id, revisions[index])
                return 'committed'
            except service.ImportChanged:
                return 'changed'

    assert sorted(await asyncio.gather(confirm(0), confirm(1))) == ['changed', 'committed']
    async with maker() as session:
        assert len((await session.execute(select(Transaction).where(Transaction.user_id == user_id))).scalars().all()) == 1


async def test_migrated_constraints_and_revert_keep_manual_history(pg):
    maker, user_id = pg
    async with maker() as session:
        manual = Transaction(user_id=user_id, amount=-50, date=parsed().date,
                             bank_name='Cash', description='Manual', is_parsed_automatically=False)
        session.add(manual)
        await session.commit()
        batch = await service.stage_import(session, user_id, 'test.csv', b'test', [parsed(), parsed()])
        preview = await service.preview_import(session, batch)
        await service.commit_import(session, batch.id, user_id, preview['revision'])
        result = await service.revert_import(session, batch.id, user_id)
        assert result['deleted_transactions'] == 2
        assert (await session.get(Transaction, manual.id)) is not None
        assert (await session.get(StatementImport, batch.id)).status == 'reverted'
        connection = await session.connection()
        constraints = await connection.run_sync(lambda conn: inspect(conn).get_unique_constraints('transactions'))
        assert 'uq_transactions_import_slot' in {c['name'] for c in constraints}
