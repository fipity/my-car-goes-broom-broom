"""search listings and run gone count

Revision ID: 43fb0929245d
Revises: 20d7d833ef70
Create Date: 2026-11-01 20:16:54.657129
"""
from alembic import op
import sqlalchemy as sa


revision = '43fb0929245d'
down_revision = '20d7d833ef70'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'search_listings',
        sa.Column('saved_search_id', sa.Integer(), nullable=False),
        sa.Column('listing_id', sa.Integer(), nullable=False),
        sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('last_run_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['saved_search_id'], ['saved_searches.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['last_run_id'], ['scrape_runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('saved_search_id', 'listing_id'),
    )
    op.create_index(op.f('ix_search_listings_listing_id'), 'search_listings', ['listing_id'], unique=False)
    op.add_column('scrape_runs', sa.Column('gone', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('scrape_runs', 'gone')
    op.drop_index(op.f('ix_search_listings_listing_id'), table_name='search_listings')
    op.drop_table('search_listings')
