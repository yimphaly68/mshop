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
    event,
    text,
)
from sqlalchemy.exc import OperationalError, ProgrammingError

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
SQLITE_PATH = os.path.join(BASE_DIR, "instance", "stock.db")

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{SQLITE_PATH}")

os.makedirs(os.path.join(BASE_DIR, "instance"), exist_ok=True)
engine = create_engine(DATABASE_URL, future=True)

if engine.dialect.name == "sqlite":
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

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
    Column("refunded_quantity", Integer, nullable=False, server_default="0"),
    Column("exchanged_from_sale_id", Integer, ForeignKey("sales.id", ondelete="SET NULL")),
    Column("buyer_name", Text),
    Column("address", Text),
    Column("phone_number", Text),
    Column("delivery_by", Text),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
    CheckConstraint("refunded_quantity BETWEEN 0 AND quantity", name="ck_sales_refunded_quantity"),
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

expenses = Table(
    "expenses",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("expense_date", Text, nullable=False),
    Column("category", Text),
    Column("description", Text),
    Column("amount", Float, nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)

other_income = Table(
    "other_income",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("income_date", Text, nullable=False),
    Column("source", Text),
    Column("description", Text),
    Column("amount", Float, nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP")),
)


def _existing_columns(conn, table_name):
    if engine.dialect.name == "sqlite":
        return {row[0] for row in conn.execute(text(f"SELECT name FROM pragma_table_info('{table_name}')"))}
    return {row[0] for row in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
    ), {"t": table_name})}


def _run_migration_step(fn):
    """Run one migration step in its own transaction. If it fails because another
    worker process already made the same change concurrently (e.g. at container
    startup with multiple gunicorn workers), that's the desired end state, so the
    race is swallowed rather than crashing the worker."""
    try:
        with engine.begin() as conn:
            fn(conn)
    except (OperationalError, ProgrammingError):
        pass


def init_db():
    _run_migration_step(lambda conn: metadata.create_all(conn))

    def _migrate_items(conn):
        item_columns = _existing_columns(conn, "items")
        if "star_rating" not in item_columns:
            conn.execute(text("ALTER TABLE items ADD COLUMN star_rating INTEGER NOT NULL DEFAULT 0"))
        if "is_best_seller" in item_columns:
            conn.execute(text("UPDATE items SET star_rating = 5 WHERE is_best_seller = 1 AND star_rating = 0"))
    _run_migration_step(_migrate_items)

    def _migrate_sales_refunded(conn):
        if "refunded_quantity" not in _existing_columns(conn, "sales"):
            conn.execute(text("ALTER TABLE sales ADD COLUMN refunded_quantity INTEGER NOT NULL DEFAULT 0"))
    _run_migration_step(_migrate_sales_refunded)

    def _migrate_sales_exchanged(conn):
        if "exchanged_from_sale_id" not in _existing_columns(conn, "sales"):
            conn.execute(text("ALTER TABLE sales ADD COLUMN exchanged_from_sale_id INTEGER"))
    _run_migration_step(_migrate_sales_exchanged)

    def _migrate_sales_buyer_info(conn):
        sales_columns = _existing_columns(conn, "sales")
        if "buyer_name" not in sales_columns:
            conn.execute(text("ALTER TABLE sales ADD COLUMN buyer_name TEXT"))
        if "address" not in sales_columns:
            conn.execute(text("ALTER TABLE sales ADD COLUMN address TEXT"))
    _run_migration_step(_migrate_sales_buyer_info)

    def _migrate_sales_delivery_info(conn):
        sales_columns = _existing_columns(conn, "sales")
        if "phone_number" not in sales_columns:
            conn.execute(text("ALTER TABLE sales ADD COLUMN phone_number TEXT"))
        if "delivery_by" not in sales_columns:
            conn.execute(text("ALTER TABLE sales ADD COLUMN delivery_by TEXT"))
    _run_migration_step(_migrate_sales_delivery_info)
