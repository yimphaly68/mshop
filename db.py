import os

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, "instance", "stock.db")

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{SQLITE_PATH}")

os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
engine = create_engine(DATABASE_URL, future=True)

metadata = MetaData()

items = Table(
    "items",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", Text, nullable=False),
    Column("category", Text),
    Column("size", Text),
    Column("color", Text),
    Column("cost_price", Float, nullable=False, server_default="0"),
    Column("sell_price", Float, nullable=False, server_default="0"),
    Column("quantity", Integer, nullable=False, server_default="0"),
    Column("date_added", Text, nullable=False),
    Column("image_filename", Text),
    Column("star_rating", Integer, nullable=False, server_default="0"),
    Column("notes", Text),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    CheckConstraint("star_rating BETWEEN 0 AND 5", name="ck_items_star_rating"),
)

sales = Table(
    "sales",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("item_id", Integer, ForeignKey("items.id", ondelete="CASCADE"), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("sale_price", Float, nullable=False),
    Column("sale_date", Text, nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)

advertising = Table(
    "advertising",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("expense_date", Text, nullable=False),
    Column("platform", Text),
    Column("description", Text),
    Column("amount", Float, nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)


def init_db():
    metadata.create_all(engine)
    with engine.begin() as conn:
        columns = {row[0] for row in conn.execute(text("SELECT name FROM pragma_table_info('items')"))} \
            if engine.dialect.name == "sqlite" else \
            {row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'items'"
            ))}
        if "star_rating" not in columns:
            conn.execute(text("ALTER TABLE items ADD COLUMN star_rating INTEGER NOT NULL DEFAULT 0"))
        if "is_best_seller" in columns:
            conn.execute(text("UPDATE items SET star_rating = 5 WHERE is_best_seller = 1 AND star_rating = 0"))
