"""Staged bank imports, provenance and database-enforced import identities.

Existing transactions are deliberately left intact; no speculative deduplication.
"""
from alembic import op
import sqlalchemy as sa

revision: str = '9e0c3f6a2b71'
down_revision: str = '8d9b2a5f1e43'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('statement_imports',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('file_sha256', sa.String(64), nullable=False),
        sa.Column('account_key', sa.String(100), nullable=False, server_default=''),
        sa.Column('bank_name', sa.String(50), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('committed_at', sa.DateTime(timezone=True)),
        sa.Column('reverted_at', sa.DateTime(timezone=True)),
        sa.Column('saved_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('categorized_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('skipped_count', sa.Integer(), nullable=False, server_default='0'),
        sa.CheckConstraint("status IN ('pending', 'committed', 'reverted')", name='ck_statement_import_status'),
    )
    op.create_index('ix_statement_imports_user_id', 'statement_imports', ['user_id'])
    op.create_index('uq_statement_import_active_file', 'statement_imports',
                    ['user_id', 'file_sha256', 'account_key'], unique=True,
                    postgresql_where=sa.text("status <> 'reverted'"), sqlite_where=sa.text("status <> 'reverted'"))
    op.add_column('transactions', sa.Column('import_id', sa.Integer()))
    op.create_foreign_key('fk_transactions_import', 'transactions', 'statement_imports', ['import_id'], ['id'], ondelete='SET NULL')
    op.add_column('transactions', sa.Column('account_key', sa.String(100), nullable=False, server_default=''))
    op.add_column('transactions', sa.Column('external_id', sa.String(200)))
    op.add_column('transactions', sa.Column('import_fingerprint', sa.String(64)))
    op.add_column('transactions', sa.Column('import_occurrence', sa.Integer()))
    op.create_index('ix_transactions_import_id', 'transactions', ['import_id'])
    op.create_index('ix_transactions_user_date', 'transactions', ['user_id', 'date'])
    op.create_unique_constraint('uq_transactions_import_slot', 'transactions',
                               ['user_id', 'import_fingerprint', 'import_occurrence'])
    op.create_index('uq_transactions_external_id', 'transactions',
                    ['user_id', 'bank_name', 'account_key', 'external_id'], unique=True,
                    postgresql_where=sa.text('external_id IS NOT NULL'), sqlite_where=sa.text('external_id IS NOT NULL'))
    op.create_table('statement_import_rows',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('import_id', sa.Integer(), sa.ForeignKey('statement_imports.id', ondelete='CASCADE'), nullable=False),
        sa.Column('row_number', sa.Integer(), nullable=False),
        sa.Column('date', sa.DateTime(timezone=True), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('bank_name', sa.String(50), nullable=False),
        sa.Column('account_key', sa.String(100), nullable=False),
        sa.Column('external_id', sa.String(200)),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id', ondelete='SET NULL')),
        sa.Column('outcome', sa.String(16), nullable=False, server_default='pending'),
        sa.UniqueConstraint('import_id', 'row_number', name='uq_statement_import_row'),
    )
    op.create_index('ix_statement_import_rows_import_id', 'statement_import_rows', ['import_id'])
    op.create_index('ix_statement_import_rows_transaction_id', 'statement_import_rows', ['transaction_id'])


def downgrade():
    op.drop_table('statement_import_rows')
    op.drop_index('uq_transactions_external_id', table_name='transactions')
    op.drop_constraint('uq_transactions_import_slot', 'transactions', type_='unique')
    op.drop_index('ix_transactions_user_date', table_name='transactions')
    op.drop_index('ix_transactions_import_id', table_name='transactions')
    op.drop_constraint('fk_transactions_import', 'transactions', type_='foreignkey')
    for column in ('import_occurrence', 'import_fingerprint', 'external_id', 'account_key', 'import_id'):
        op.drop_column('transactions', column)
    op.drop_table('statement_imports')
