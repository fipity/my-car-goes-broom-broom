"""listing coordinates, saved search seen_at, postcode suburb index

Revision ID: 5b1e7c2d9a10
Revises: 43fb0929245d
Create Date: 2026-11-01 21:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '5b1e7c2d9a10'
down_revision = '43fb0929245d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('listings', sa.Column('lat', sa.Float(), nullable=True))
    op.add_column('listings', sa.Column('lon', sa.Float(), nullable=True))
    op.add_column('saved_searches', sa.Column('seen_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_postcodes_suburb_state', 'postcodes', [sa.text('lower(suburb)'), 'state'])


def downgrade() -> None:
    op.drop_index('ix_postcodes_suburb_state', table_name='postcodes')
    op.drop_column('saved_searches', 'seen_at')
    op.drop_column('listings', 'lon')
    op.drop_column('listings', 'lat')
