import os
import uuid
from datetime import date, timedelta
from functools import wraps

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
from flask import Flask, g, render_template, request, redirect, session, url_for, flash
from sqlalchemy import text
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from db import engine, init_db

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

CURRENCY = "$"  # change this symbol if your shop uses a different currency
LOW_STOCK_THRESHOLD = 3
RESTOCK_ALERT_THRESHOLD = 2  # dashboard carousel: items at or below this need reordering

USE_CLOUDINARY = bool(os.environ.get("CLOUDINARY_URL"))
if USE_CLOUDINARY:
    cloudinary.config()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-stock-control-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB upload limit
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.permanent_session_lifetime = timedelta(days=30)
app.jinja_env.globals["CURRENCY"] = CURRENCY

os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db()


# ---------------------------------------------------------------------------
# Auth (login page + session cookie) — set the env vars below to require a
# login. Left unset, the app runs with no login, same as local dev always has.
# ---------------------------------------------------------------------------

AUTH_USERS = {}  # username -> {"password_hash": ..., "is_admin": bool}
if os.environ.get("AUTH_ADMIN_USERNAME") and os.environ.get("AUTH_ADMIN_PASSWORD_HASH"):
    AUTH_USERS[os.environ["AUTH_ADMIN_USERNAME"]] = {
        "password_hash": os.environ["AUTH_ADMIN_PASSWORD_HASH"],
        "is_admin": True,
    }
if os.environ.get("AUTH_STAFF_USERNAME") and os.environ.get("AUTH_STAFF_PASSWORD_HASH"):
    AUTH_USERS[os.environ["AUTH_STAFF_USERNAME"]] = {
        "password_hash": os.environ["AUTH_STAFF_PASSWORD_HASH"],
        "is_admin": False,
    }


@app.before_request
def require_login():
    if not AUTH_USERS or request.endpoint in ("login", "static"):
        return  # auth disabled, or this route doesn't need it
    user = AUTH_USERS.get(session.get("username"))
    if not user:
        return redirect(url_for("login", next=request.path))
    g.username = session["username"]
    g.is_admin = user["is_admin"]


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = AUTH_USERS.get(username)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["username"] = username
            next_url = request.form.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("dashboard")
            return redirect(next_url)
        flash("Incorrect username or password.", "danger")
    return render_template("login.html", next=request.args.get("next", ""))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if AUTH_USERS and not g.get("is_admin", False):
            flash("Only an admin can do that.", "danger")
            return redirect(request.referrer or url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


app.jinja_env.globals["current_username"] = lambda: g.get("username")
app.jinja_env.globals["is_admin"] = lambda: g.get("is_admin", not AUTH_USERS)


def get_db():
    if "db" not in g:
        g.db = engine.connect()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def photo_url(value):
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return url_for("static", filename="uploads/" + value)


app.jinja_env.globals["photo_url"] = photo_url


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_photo(file_storage):
    """Save an uploaded photo (to Cloudinary if configured, else locally); return the stored value or None."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_file(file_storage.filename):
        flash("Photo skipped: unsupported file type (use png, jpg, jpeg, gif or webp).", "warning")
        return None

    if USE_CLOUDINARY:
        result = cloudinary.uploader.upload(file_storage, folder="mym-shop-stock")
        return result["secure_url"]

    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, secure_filename(filename)))
    return filename


def delete_photo(value):
    if not value or value.startswith("http://") or value.startswith("https://"):
        return  # Cloudinary-hosted photos are left in place; not worth the extra bookkeeping for a small shop.
    path = os.path.join(UPLOAD_DIR, value)
    if os.path.exists(path):
        os.remove(path)


def parse_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date_filtered_total(db, table, date_column, date_from, date_to):
    sql = f"SELECT COALESCE(SUM(amount),0) AS total FROM {table} WHERE 1=1"
    params = {}
    if date_from:
        sql += f" AND {date_column} >= :date_from"
        params["date_from"] = date_from
    if date_to:
        sql += f" AND {date_column} <= :date_to"
        params["date_to"] = date_to
    return db.execute(text(sql), params).mappings().one()["total"] or 0


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def dashboard():
    db = get_db()

    items = db.execute(text("SELECT * FROM items")).mappings().all()
    total_items = len(items)
    total_units = sum(i["quantity"] for i in items)
    stock_value_cost = sum(i["quantity"] * i["cost_price"] for i in items)
    stock_value_retail = sum(i["quantity"] * i["sell_price"] for i in items)
    out_of_stock = [i for i in items if i["quantity"] <= 0]
    low_stock = [i for i in items if 0 < i["quantity"] <= LOW_STOCK_THRESHOLD]
    restock_items = sorted(
        (i for i in items if i["quantity"] <= RESTOCK_ALERT_THRESHOLD),
        key=lambda i: (i["quantity"], i["name"]),
    )
    best_sellers = sorted(
        (i for i in items if i["star_rating"] >= 4),
        key=lambda i: i["star_rating"],
        reverse=True,
    )[:5]

    sales_summary = db.execute(text(
        "SELECT COALESCE(SUM(quantity - refunded_quantity),0) AS units_sold, "
        "COALESCE(SUM((quantity - refunded_quantity) * sale_price),0) AS revenue "
        "FROM sales"
    )).mappings().one()

    cogs_row = db.execute(text(
        "SELECT COALESCE(SUM((s.quantity - s.refunded_quantity) * i.cost_price),0) AS cogs "
        "FROM sales s JOIN items i ON i.id = s.item_id"
    )).mappings().one()

    ad_spend_row = db.execute(text(
        "SELECT COALESCE(SUM(amount),0) AS total FROM advertising"
    )).mappings().one()

    other_expenses_row = db.execute(text(
        "SELECT COALESCE(SUM(amount),0) AS total FROM expenses"
    )).mappings().one()

    other_income_row = db.execute(text(
        "SELECT COALESCE(SUM(amount),0) AS total FROM other_income"
    )).mappings().one()

    revenue = sales_summary["revenue"] or 0
    cogs = cogs_row["cogs"] or 0
    ad_spend = ad_spend_row["total"] or 0
    other_expenses = other_expenses_row["total"] or 0
    other_income = other_income_row["total"] or 0
    gross_profit = revenue - cogs
    net_profit = gross_profit + other_income - ad_spend - other_expenses

    recent_sales = db.execute(text(
        "SELECT s.*, i.name AS item_name, i.size AS item_size, i.color AS item_color "
        "FROM sales s JOIN items i ON i.id = s.item_id "
        "ORDER BY s.sale_date DESC, s.id DESC LIMIT 8"
    )).mappings().all()

    return render_template(
        "dashboard.html",
        total_items=total_items,
        total_units=total_units,
        stock_value_cost=stock_value_cost,
        stock_value_retail=stock_value_retail,
        out_of_stock=out_of_stock,
        low_stock=low_stock,
        restock_items=restock_items,
        restock_alert_threshold=RESTOCK_ALERT_THRESHOLD,
        best_sellers=best_sellers,
        units_sold=sales_summary["units_sold"] or 0,
        revenue=revenue,
        cogs=cogs,
        ad_spend=ad_spend,
        other_expenses=other_expenses,
        other_income=other_income,
        gross_profit=gross_profit,
        net_profit=net_profit,
        recent_sales=recent_sales,
    )


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@app.route("/items")
def items_list():
    db = get_db()
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "all")
    category = request.args.get("category", "")

    sql = "SELECT * FROM items WHERE 1=1"
    params = {}
    if q:
        sql += " AND (name LIKE :like OR category LIKE :like OR color LIKE :like OR size LIKE :like)"
        params["like"] = f"%{q}%"
    if category:
        sql += " AND category = :category"
        params["category"] = category
    if status == "in_stock":
        sql += " AND quantity > 0"
    elif status == "out_of_stock":
        sql += " AND quantity <= 0"
    elif status == "best_seller":
        sql += " AND star_rating >= 4"
    elif status == "low_stock":
        sql += " AND quantity > 0 AND quantity <= :threshold"
        params["threshold"] = LOW_STOCK_THRESHOLD

    sql += " ORDER BY created_at DESC"
    items = db.execute(text(sql), params).mappings().all()
    categories = db.execute(text(
        "SELECT DISTINCT category FROM items WHERE category IS NOT NULL AND category != '' ORDER BY category"
    )).mappings().all()

    return render_template(
        "items.html",
        items=items,
        q=q,
        status=status,
        category=category,
        categories=categories,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
    )


@app.route("/items/new", methods=["GET", "POST"])
def item_new():
    if request.method == "POST":
        db = get_db()
        filename = save_photo(request.files.get("photo"))
        db.execute(text(
            "INSERT INTO items (name, category, size, color, cost_price, sell_price, "
            "quantity, date_added, image_filename, star_rating, notes) "
            "VALUES (:name, :category, :size, :color, :cost_price, :sell_price, "
            ":quantity, :date_added, :image_filename, :star_rating, :notes)"
        ), {
            "name": request.form.get("name", "").strip(),
            "category": request.form.get("category", "").strip(),
            "size": request.form.get("size", "").strip(),
            "color": request.form.get("color", "").strip(),
            "cost_price": parse_float(request.form.get("cost_price")),
            "sell_price": parse_float(request.form.get("sell_price")),
            "quantity": parse_int(request.form.get("quantity")),
            "date_added": request.form.get("date_added") or date.today().isoformat(),
            "image_filename": filename,
            "star_rating": max(0, min(5, parse_int(request.form.get("star_rating")))),
            "notes": request.form.get("notes", "").strip(),
        })
        db.commit()
        flash("Item added.", "success")
        return redirect(url_for("items_list"))

    return render_template("item_form.html", item=None, today=date.today().isoformat())


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id):
    db = get_db()
    item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).mappings().first()
    if item is None:
        flash("Item not found.", "danger")
        return redirect(url_for("items_list"))

    if request.method == "POST":
        filename = item["image_filename"]
        new_file = save_photo(request.files.get("photo"))
        if new_file:
            delete_photo(filename)
            filename = new_file
        elif request.form.get("remove_photo"):
            delete_photo(filename)
            filename = None

        db.execute(text(
            "UPDATE items SET name=:name, category=:category, size=:size, color=:color, "
            "cost_price=:cost_price, sell_price=:sell_price, quantity=:quantity, "
            "date_added=:date_added, image_filename=:image_filename, star_rating=:star_rating, "
            "notes=:notes WHERE id=:id"
        ), {
            "name": request.form.get("name", "").strip(),
            "category": request.form.get("category", "").strip(),
            "size": request.form.get("size", "").strip(),
            "color": request.form.get("color", "").strip(),
            "cost_price": parse_float(request.form.get("cost_price")),
            "sell_price": parse_float(request.form.get("sell_price")),
            "quantity": parse_int(request.form.get("quantity")),
            "date_added": request.form.get("date_added") or date.today().isoformat(),
            "image_filename": filename,
            "star_rating": max(0, min(5, parse_int(request.form.get("star_rating")))),
            "notes": request.form.get("notes", "").strip(),
            "id": item_id,
        })
        db.commit()
        flash("Item updated.", "success")
        return redirect(url_for("items_list"))

    return render_template("item_form.html", item=item, today=date.today().isoformat())


@app.route("/items/<int:item_id>/delete", methods=["POST"])
@admin_required
def item_delete(item_id):
    db = get_db()
    item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).mappings().first()
    if item:
        delete_photo(item["image_filename"])
        db.execute(text("DELETE FROM sales WHERE item_id = :id"), {"id": item_id})
        db.execute(text("DELETE FROM items WHERE id = :id"), {"id": item_id})
        db.commit()
        flash("Item deleted.", "success")
    return redirect(url_for("items_list"))


@app.route("/items/<int:item_id>/set-rating", methods=["POST"])
def item_set_rating(item_id):
    db = get_db()
    item = db.execute(
        text("SELECT star_rating FROM items WHERE id = :id"), {"id": item_id}
    ).mappings().first()
    if item is None:
        flash("Item not found.", "danger")
        return redirect(url_for("items_list"))

    clicked = max(0, min(5, parse_int(request.form.get("star"))))
    new_rating = 0 if clicked == item["star_rating"] else clicked
    db.execute(text("UPDATE items SET star_rating = :rating WHERE id = :id"), {"rating": new_rating, "id": item_id})
    db.commit()
    return redirect(request.referrer or url_for("items_list"))


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

@app.route("/sales")
def sales_list():
    db = get_db()
    sales = db.execute(text(
        "SELECT s.*, i.name AS item_name, i.size AS item_size, i.color AS item_color, "
        "i.cost_price AS item_cost_price, orig.name AS exchanged_from_item_name "
        "FROM sales s JOIN items i ON i.id = s.item_id "
        "LEFT JOIN sales os ON os.id = s.exchanged_from_sale_id "
        "LEFT JOIN items orig ON orig.id = os.item_id "
        "ORDER BY s.sale_date DESC, s.id DESC"
    )).mappings().all()
    items = db.execute(text("SELECT * FROM items ORDER BY name")).mappings().all()
    return render_template("sales.html", sales=sales, items=items, today=date.today().isoformat())


@app.route("/sales/new", methods=["POST"])
def sale_new():
    db = get_db()
    item_id = parse_int(request.form.get("item_id"))
    quantity = parse_int(request.form.get("quantity"))
    item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).mappings().first()

    if not item:
        flash("Please choose a valid item.", "danger")
        return redirect(url_for("sales_list"))
    if quantity <= 0:
        flash("Quantity sold must be greater than zero.", "danger")
        return redirect(url_for("sales_list"))
    if quantity > item["quantity"]:
        flash(f"Only {item['quantity']} in stock for {item['name']} — cannot sell {quantity}.", "danger")
        return redirect(url_for("sales_list"))

    sale_price = parse_float(request.form.get("sale_price"), item["sell_price"])
    sale_date = request.form.get("sale_date") or date.today().isoformat()

    db.execute(text(
        "INSERT INTO sales (item_id, quantity, sale_price, sale_date) "
        "VALUES (:item_id, :quantity, :sale_price, :sale_date)"
    ), {"item_id": item_id, "quantity": quantity, "sale_price": sale_price, "sale_date": sale_date})
    db.execute(
        text("UPDATE items SET quantity = quantity - :quantity WHERE id = :id"),
        {"quantity": quantity, "id": item_id},
    )
    db.commit()
    flash(f"Sale recorded: {quantity} x {item['name']}.", "success")
    return redirect(url_for("sales_list"))


@app.route("/sales/<int:sale_id>/delete", methods=["POST"])
@admin_required
def sale_delete(sale_id):
    db = get_db()
    sale = db.execute(text("SELECT * FROM sales WHERE id = :id"), {"id": sale_id}).mappings().first()
    if sale:
        # Only restore stock for the portion that wasn't already refunded.
        remaining = sale["quantity"] - sale["refunded_quantity"]
        db.execute(
            text("UPDATE items SET quantity = quantity + :quantity WHERE id = :id"),
            {"quantity": remaining, "id": sale["item_id"]},
        )
        db.execute(text("DELETE FROM sales WHERE id = :id"), {"id": sale_id})
        db.commit()
        flash("Sale removed and stock restored.", "success")
    return redirect(url_for("sales_list"))


@app.route("/sales/<int:sale_id>/refund", methods=["POST"])
def sale_refund(sale_id):
    db = get_db()
    sale = db.execute(text("SELECT * FROM sales WHERE id = :id"), {"id": sale_id}).mappings().first()
    if not sale:
        flash("Sale not found.", "danger")
        return redirect(url_for("sales_list"))

    remaining = sale["quantity"] - sale["refunded_quantity"]
    refund_qty = parse_int(request.form.get("refund_quantity"))

    if refund_qty <= 0:
        flash("Refund quantity must be greater than zero.", "danger")
    elif refund_qty > remaining:
        flash(f"Only {remaining} unit(s) from this sale can still be refunded.", "danger")
    else:
        db.execute(
            text("UPDATE sales SET refunded_quantity = refunded_quantity + :qty WHERE id = :id"),
            {"qty": refund_qty, "id": sale_id},
        )
        db.execute(
            text("UPDATE items SET quantity = quantity + :qty WHERE id = :id"),
            {"qty": refund_qty, "id": sale["item_id"]},
        )
        db.commit()
        flash(f"Refunded {refund_qty} unit(s) — {CURRENCY}{refund_qty * sale['sale_price']:.2f}, stock restored.", "success")

    return redirect(url_for("sales_list"))


@app.route("/sales/<int:sale_id>/exchange", methods=["POST"])
def sale_exchange(sale_id):
    db = get_db()
    sale = db.execute(text("SELECT * FROM sales WHERE id = :id"), {"id": sale_id}).mappings().first()
    if not sale:
        flash("Sale not found.", "danger")
        return redirect(url_for("sales_list"))

    remaining = sale["quantity"] - sale["refunded_quantity"]
    exchange_qty = parse_int(request.form.get("exchange_quantity"))
    new_item_id = parse_int(request.form.get("new_item_id"))
    new_item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": new_item_id}).mappings().first()

    if exchange_qty <= 0:
        flash("Exchange quantity must be greater than zero.", "danger")
        return redirect(url_for("sales_list"))
    if exchange_qty > remaining:
        flash(f"Only {remaining} unit(s) from this sale can still be exchanged.", "danger")
        return redirect(url_for("sales_list"))
    if not new_item:
        flash("Please choose a valid item to exchange for.", "danger")
        return redirect(url_for("sales_list"))
    if new_item["quantity"] < exchange_qty:
        flash(f"Only {new_item['quantity']} in stock for {new_item['name']} — cannot exchange {exchange_qty}.", "danger")
        return redirect(url_for("sales_list"))

    new_price = parse_float(request.form.get("new_sale_price"), new_item["sell_price"])
    new_date = request.form.get("exchange_date") or date.today().isoformat()

    # Return the original item to stock and mark that portion as refunded.
    db.execute(
        text("UPDATE sales SET refunded_quantity = refunded_quantity + :qty WHERE id = :id"),
        {"qty": exchange_qty, "id": sale_id},
    )
    db.execute(
        text("UPDATE items SET quantity = quantity + :qty WHERE id = :id"),
        {"qty": exchange_qty, "id": sale["item_id"]},
    )
    # Record the new item as a fresh sale, linked back to the original.
    db.execute(text(
        "INSERT INTO sales (item_id, quantity, sale_price, sale_date, exchanged_from_sale_id) "
        "VALUES (:item_id, :quantity, :sale_price, :sale_date, :exchanged_from_sale_id)"
    ), {
        "item_id": new_item_id,
        "quantity": exchange_qty,
        "sale_price": new_price,
        "sale_date": new_date,
        "exchanged_from_sale_id": sale_id,
    })
    db.execute(
        text("UPDATE items SET quantity = quantity - :qty WHERE id = :id"),
        {"qty": exchange_qty, "id": new_item_id},
    )
    db.commit()

    price_diff = new_price - sale["sale_price"]
    if price_diff > 0:
        diff_note = f"customer pays {CURRENCY}{price_diff:.2f} more"
    elif price_diff < 0:
        diff_note = f"refund {CURRENCY}{-price_diff:.2f} difference"
    else:
        diff_note = "no price difference"
    flash(f"Exchanged {exchange_qty} unit(s) for {new_item['name']} ({diff_note}).", "success")
    return redirect(url_for("sales_list"))


# ---------------------------------------------------------------------------
# Advertising spend
# ---------------------------------------------------------------------------

@app.route("/advertising")
def advertising_list():
    db = get_db()
    expenses = db.execute(text("SELECT * FROM advertising ORDER BY expense_date DESC, id DESC")).mappings().all()
    total = sum(e["amount"] for e in expenses)
    return render_template("advertising.html", expenses=expenses, total=total, today=date.today().isoformat())


@app.route("/advertising/new", methods=["POST"])
def advertising_new():
    db = get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Amount must be greater than zero.", "danger")
        return redirect(url_for("advertising_list"))
    db.execute(text(
        "INSERT INTO advertising (expense_date, platform, description, amount) "
        "VALUES (:expense_date, :platform, :description, :amount)"
    ), {
        "expense_date": request.form.get("expense_date") or date.today().isoformat(),
        "platform": request.form.get("platform", "").strip(),
        "description": request.form.get("description", "").strip(),
        "amount": amount,
    })
    db.commit()
    flash("Advertising expense logged.", "success")
    return redirect(url_for("advertising_list"))


@app.route("/advertising/<int:expense_id>/delete", methods=["POST"])
@admin_required
def advertising_delete(expense_id):
    db = get_db()
    db.execute(text("DELETE FROM advertising WHERE id = :id"), {"id": expense_id})
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("advertising_list"))


# ---------------------------------------------------------------------------
# Other expenses (rent, utilities, wages, etc.)
# ---------------------------------------------------------------------------

@app.route("/expenses")
def expenses_list():
    db = get_db()
    expenses = db.execute(text("SELECT * FROM expenses ORDER BY expense_date DESC, id DESC")).mappings().all()
    total = sum(e["amount"] for e in expenses)
    return render_template("expenses.html", expenses=expenses, total=total, today=date.today().isoformat())


@app.route("/expenses/new", methods=["POST"])
def expense_new():
    db = get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Amount must be greater than zero.", "danger")
        return redirect(url_for("expenses_list"))
    db.execute(text(
        "INSERT INTO expenses (expense_date, category, description, amount) "
        "VALUES (:expense_date, :category, :description, :amount)"
    ), {
        "expense_date": request.form.get("expense_date") or date.today().isoformat(),
        "category": request.form.get("category", "").strip(),
        "description": request.form.get("description", "").strip(),
        "amount": amount,
    })
    db.commit()
    flash("Expense logged.", "success")
    return redirect(url_for("expenses_list"))


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@admin_required
def expense_delete(expense_id):
    db = get_db()
    db.execute(text("DELETE FROM expenses WHERE id = :id"), {"id": expense_id})
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("expenses_list"))


# ---------------------------------------------------------------------------
# Other income (non-sales revenue)
# ---------------------------------------------------------------------------

@app.route("/income")
def income_list():
    db = get_db()
    income = db.execute(text("SELECT * FROM other_income ORDER BY income_date DESC, id DESC")).mappings().all()
    total = sum(i["amount"] for i in income)
    return render_template("income.html", income=income, total=total, today=date.today().isoformat())


@app.route("/income/new", methods=["POST"])
def income_new():
    db = get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Amount must be greater than zero.", "danger")
        return redirect(url_for("income_list"))
    db.execute(text(
        "INSERT INTO other_income (income_date, source, description, amount) "
        "VALUES (:income_date, :source, :description, :amount)"
    ), {
        "income_date": request.form.get("income_date") or date.today().isoformat(),
        "source": request.form.get("source", "").strip(),
        "description": request.form.get("description", "").strip(),
        "amount": amount,
    })
    db.commit()
    flash("Income logged.", "success")
    return redirect(url_for("income_list"))


@app.route("/income/<int:income_id>/delete", methods=["POST"])
@admin_required
def income_delete(income_id):
    db = get_db()
    db.execute(text("DELETE FROM other_income WHERE id = :id"), {"id": income_id})
    db.commit()
    flash("Income entry deleted.", "success")
    return redirect(url_for("income_list"))


# ---------------------------------------------------------------------------
# Reports (profit & loss)
# ---------------------------------------------------------------------------

@app.route("/reports")
def reports():
    db = get_db()
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    sql = (
        "SELECT i.id, i.name, i.size, i.color, i.cost_price, "
        "SUM(s.quantity - s.refunded_quantity) AS units_sold, "
        "SUM((s.quantity - s.refunded_quantity) * s.sale_price) AS revenue, "
        "SUM((s.quantity - s.refunded_quantity) * i.cost_price) AS cogs "
        "FROM sales s JOIN items i ON i.id = s.item_id WHERE 1=1"
    )
    params = {}
    if date_from:
        sql += " AND s.sale_date >= :date_from"
        params["date_from"] = date_from
    if date_to:
        sql += " AND s.sale_date <= :date_to"
        params["date_to"] = date_to
    sql += " GROUP BY i.id, i.name, i.size, i.color, i.cost_price ORDER BY revenue DESC"
    per_item = db.execute(text(sql), params).mappings().all()

    total_revenue = sum(r["revenue"] or 0 for r in per_item)
    total_cogs = sum(r["cogs"] or 0 for r in per_item)
    gross_profit = total_revenue - total_cogs

    ad_spend = _date_filtered_total(db, "advertising", "expense_date", date_from, date_to)
    other_expenses = _date_filtered_total(db, "expenses", "expense_date", date_from, date_to)
    other_income = _date_filtered_total(db, "other_income", "income_date", date_from, date_to)

    net_profit = gross_profit + other_income - ad_spend - other_expenses

    return render_template(
        "reports.html",
        per_item=per_item,
        total_revenue=total_revenue,
        total_cogs=total_cogs,
        gross_profit=gross_profit,
        ad_spend=ad_spend,
        other_expenses=other_expenses,
        other_income=other_income,
        net_profit=net_profit,
        date_from=date_from,
        date_to=date_to,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5050)
