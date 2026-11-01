"""initial schema

Revision ID: 20d7d833ef70
Revises: 
Create Date: 2026-11-01 11:21:16.590215
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '20d7d833ef70'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('postcodes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('postcode', sa.String(length=4), nullable=False),
    sa.Column('suburb', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=3), nullable=False),
    sa.Column('lat', sa.Float(), nullable=False),
    sa.Column('lon', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_postcodes_postcode'), 'postcodes', ['postcode'], unique=False)
    op.create_table('raw_payloads',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_listing_id', sa.String(length=128), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('content_type', sa.String(length=16), nullable=False),
    sa.Column('payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('payload_text', sa.Text(), nullable=True),
    sa.Column('parser_version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_raw_source_listing', 'raw_payloads', ['source', 'source_listing_id'], unique=False)
    op.create_table('saved_searches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('sources', postgresql.ARRAY(sa.String(length=32)), nullable=True),
    sa.Column('schedule_enabled', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('sources',
    sa.Column('name', sa.String(length=32), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('last_count', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('vehicles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('vin', sa.String(length=32), nullable=True),
    sa.Column('rego', sa.String(length=16), nullable=True),
    sa.Column('canonical_listing_id', sa.Integer(), nullable=True),
    sa.Column('match_method', sa.String(length=16), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_vehicles_rego'), 'vehicles', ['rego'], unique=False)
    op.create_index(op.f('ix_vehicles_vin'), 'vehicles', ['vin'], unique=False)
    op.create_table('listings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('source_listing_id', sa.String(length=128), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('make', sa.String(length=64), nullable=True),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('variant', sa.String(length=128), nullable=True),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('odometer_km', sa.Integer(), nullable=True),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('price_type', sa.String(length=16), nullable=False),
    sa.Column('body_type', sa.String(length=32), nullable=True),
    sa.Column('transmission', sa.String(length=16), nullable=True),
    sa.Column('fuel_type', sa.String(length=32), nullable=True),
    sa.Column('engine', sa.String(length=64), nullable=True),
    sa.Column('drivetrain', sa.String(length=16), nullable=True),
    sa.Column('colour', sa.String(length=32), nullable=True),
    sa.Column('seller_type', sa.String(length=16), nullable=False),
    sa.Column('suburb', sa.String(length=64), nullable=True),
    sa.Column('state', sa.String(length=3), nullable=True),
    sa.Column('postcode', sa.String(length=4), nullable=True),
    sa.Column('images', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('rego', sa.String(length=16), nullable=True),
    sa.Column('vin', sa.String(length=32), nullable=True),
    sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('excluded_reason', sa.String(length=32), nullable=True),
    sa.Column('latest_raw_id', sa.BigInteger(), nullable=True),
    sa.Column('vehicle_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['latest_raw_id'], ['raw_payloads.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source', 'source_listing_id', name='uq_listing_source_id')
    )
    op.create_index('ix_listings_active_seen', 'listings', ['is_active', 'last_seen'], unique=False)
    op.create_index('ix_listings_make_model_year', 'listings', ['make', 'model', 'year'], unique=False)
    op.create_index(op.f('ix_listings_odometer_km'), 'listings', ['odometer_km'], unique=False)
    op.create_index(op.f('ix_listings_price'), 'listings', ['price'], unique=False)
    op.create_index(op.f('ix_listings_state'), 'listings', ['state'], unique=False)
    op.create_index(op.f('ix_listings_vehicle_id'), 'listings', ['vehicle_id'], unique=False)
    # vehicles <-> listings reference each other, so this key is added after both exist
    op.create_foreign_key('fk_vehicles_canonical_listing', 'vehicles', 'listings',
                          ['canonical_listing_id'], ['id'], ondelete='SET NULL')
    op.create_table('scrape_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('saved_search_id', sa.Integer(), nullable=True),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('trigger', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('found', sa.Integer(), nullable=False),
    sa.Column('new', sa.Integer(), nullable=False),
    sa.Column('updated', sa.Integer(), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['saved_search_id'], ['saved_searches.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source'], ['sources.name'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_scrape_runs_saved_search_id'), 'scrape_runs', ['saved_search_id'], unique=False)
    op.create_index('ix_scrape_runs_status', 'scrape_runs', ['status', 'id'], unique=False)
    op.create_table('price_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('listing_id', sa.Integer(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('price_type', sa.String(length=16), nullable=False),
    sa.Column('observed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_price_history_listing', 'price_history', ['listing_id', 'observed_at'], unique=False)
    op.create_table('valuations',
    sa.Column('listing_id', sa.Integer(), nullable=False),
    sa.Column('estimate', sa.Integer(), nullable=True),
    sa.Column('p25', sa.Integer(), nullable=True),
    sa.Column('p75', sa.Integer(), nullable=True),
    sa.Column('diff_abs', sa.Integer(), nullable=True),
    sa.Column('diff_pct', sa.Float(), nullable=True),
    sa.Column('label', sa.String(length=16), nullable=True),
    sa.Column('confidence', sa.String(length=8), nullable=True),
    sa.Column('method', sa.String(length=16), nullable=True),
    sa.Column('n_comps', sa.Integer(), nullable=False),
    sa.Column('comp_ids', postgresql.ARRAY(sa.Integer()), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['listing_id'], ['listings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('listing_id')
    )
    op.create_index(op.f('ix_valuations_diff_pct'), 'valuations', ['diff_pct'], unique=False)
    op.create_index(op.f('ix_valuations_label'), 'valuations', ['label'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_valuations_label'), table_name='valuations')
    op.drop_index(op.f('ix_valuations_diff_pct'), table_name='valuations')
    op.drop_table('valuations')
    op.drop_index('ix_price_history_listing', table_name='price_history')
    op.drop_table('price_history')
    op.drop_index('ix_scrape_runs_status', table_name='scrape_runs')
    op.drop_index(op.f('ix_scrape_runs_saved_search_id'), table_name='scrape_runs')
    op.drop_table('scrape_runs')
    op.drop_constraint('fk_vehicles_canonical_listing', 'vehicles', type_='foreignkey')
    op.drop_index(op.f('ix_listings_vehicle_id'), table_name='listings')
    op.drop_index(op.f('ix_listings_state'), table_name='listings')
    op.drop_index(op.f('ix_listings_price'), table_name='listings')
    op.drop_index(op.f('ix_listings_odometer_km'), table_name='listings')
    op.drop_index('ix_listings_make_model_year', table_name='listings')
    op.drop_index('ix_listings_active_seen', table_name='listings')
    op.drop_table('listings')
    op.drop_index(op.f('ix_vehicles_vin'), table_name='vehicles')
    op.drop_index(op.f('ix_vehicles_rego'), table_name='vehicles')
    op.drop_table('vehicles')
    op.drop_table('sources')
    op.drop_table('saved_searches')
    op.drop_index('ix_raw_source_listing', table_name='raw_payloads')
    op.drop_table('raw_payloads')
    op.drop_index(op.f('ix_postcodes_postcode'), table_name='postcodes')
    op.drop_table('postcodes')
