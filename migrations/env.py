from alembic import context

from supplier_chaser.adapters.db import Base, make_engine
from supplier_chaser.config import Settings

engine = make_engine(Settings().database_url)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
