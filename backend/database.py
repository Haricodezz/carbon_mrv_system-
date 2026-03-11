import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Read DATABASE_URL from environment (set this on Render / Railway / etc.)
# Format: postgresql://USER:PASSWORD@HOST:PORT/DBNAME
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:%23Hari012345@localhost:5432/carbon_mrv"
)

# Render provides "postgres://" — SQLAlchemy needs "postgresql://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()