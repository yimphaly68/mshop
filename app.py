import calendar
import html
import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, timedelta
from functools import wraps

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
from flask import (
    Blueprint, Flask, abort, g, jsonify, make_response, render_template, request,
    redirect, session, url_for, flash,
)
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from db import (
    count_live_visitors, engine, get_setting, init_db, record_visitor_ping, set_setting,
)

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

def get_telegram_config(db):
    """Bot used for business-ops notifications (new sale, new item, expenses)."""
    token = get_setting(db, "telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = get_setting(db, "telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    return token, chat_id


def get_chat_click_telegram_config(db):
    """Separate bot used only for "visitor tapped Chat" photo notifications, kept
    apart from the business-ops bot above so the two groups don't mix."""
    token = get_setting(db, "chat_click_bot_token")
    chat_id = get_setting(db, "chat_click_chat_id")
    return token, chat_id


def _telegram_post(token, method, payload):
    """Best-effort call to the Telegram Bot API. Silently does nothing on failure,
    and never lets a Telegram failure break the action that triggered it."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass


def notify_telegram(db, message):
    token, chat_id = get_telegram_config(db)
    if not token or not chat_id:
        return
    _telegram_post(token, "sendMessage", {"chat_id": chat_id, "text": message, "parse_mode": "HTML"})


def notify_telegram_photo(token, chat_id, photo, caption):
    """`photo` must be a publicly reachable https:// URL — Telegram fetches it itself."""
    if not token or not chat_id or not photo:
        return
    _telegram_post(token, "sendPhoto", {
        "chat_id": chat_id,
        "photo": photo,
        "caption": caption,
        "parse_mode": "HTML",
    })

app = Flask(__name__)
# Traefik terminates HTTPS and forwards plain HTTP to this container; without this,
# Flask doesn't know the original request was HTTPS and builds http:// URLs for
# things like the automatic trailing-slash redirect (e.g. /pe -> /pe/), which
# Traefik then has no route for (it only listens on the HTTPS entrypoint) — a 404.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-stock-control-secret")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB upload limit
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "1") == "1"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.permanent_session_lifetime = timedelta(days=30)
app.jinja_env.globals["CURRENCY"] = CURRENCY

os.makedirs(UPLOAD_DIR, exist_ok=True)
init_db()

# The staff/management tool (dashboard, items, sales, ...) lives under /pe so it
# doesn't collide with the public storefront now sitting at the bare domain.
admin_bp = Blueprint("admin", __name__, url_prefix="/pe")
public_bp = Blueprint("public", __name__)


# ---------------------------------------------------------------------------
# Auth (login page + session cookie) — set the env vars below to require a
# login. Left unset, the app runs with no login, same as local dev always has.
# Only the admin blueprint (/pe/*) is ever gated; the public storefront is
# always open to visitors.
# ---------------------------------------------------------------------------

AUTH_USERS = {}  # username -> {"password_hash": ..., "is_admin": bool} — built-in, from env vars
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


def get_db_user(db, username):
    """A staff/admin account created from Settings (in the `users` table), on top
    of the built-in AUTH_USERS above."""
    if not username:
        return None
    row = db.execute(
        text("SELECT password_hash, is_admin FROM users WHERE username = :u"), {"u": username}
    ).mappings().first()
    if not row:
        return None
    return {"password_hash": row["password_hash"], "is_admin": bool(row["is_admin"])}


@app.before_request
def require_login():
    if not AUTH_USERS or request.blueprint != "admin" or request.endpoint == "admin.login":
        return  # auth disabled, or this route doesn't need it
    username = session.get("username")
    user = AUTH_USERS.get(username) or get_db_user(get_db(), username)
    if not user:
        return redirect(url_for("admin.login", next=request.path))
    g.username = username
    g.is_admin = user["is_admin"]


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = AUTH_USERS.get(username) or get_db_user(get_db(), username)
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["username"] = username
            next_url = request.form.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("admin.dashboard")
            return redirect(next_url)
        flash("Incorrect username or password.", "danger")
    return render_template("login.html", next=request.args.get("next", ""))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if AUTH_USERS and not g.get("is_admin", False):
            flash("Only an admin can do that.", "danger")
            return redirect(request.referrer or url_for("admin.dashboard"))
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


def absolute_photo_url(value):
    """Same as photo_url, but always a fully-qualified https:// URL — needed for
    Telegram's sendPhoto, which fetches the image itself and can't resolve a
    relative /static/... path."""
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return url_for("static", filename="uploads/" + value, _external=True)


def telegram_chat_url(username, item_name=None):
    if not username:
        return None
    text_msg = f"Hi, I'm interested in: {item_name}" if item_name else "Hi, I'm interested in your shop items"
    return f"https://t.me/{username}?text=" + urllib.parse.quote(text_msg)


app.jinja_env.globals["telegram_chat_url"] = telegram_chat_url


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


def parse_optional_float(value):
    """Like parse_float, but a blank/missing value means "not set" (None)
    rather than 0 — used for fields where 0 and "no value" mean different things."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


DASHBOARD_PERIODS = [
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("this_week", "This Week"),
    ("last_week", "Last Week"),
    ("this_month", "This Month"),
    ("last_month", "Last Month"),
    ("this_year", "This Year"),
    ("last_year", "Last Year"),
]
DASHBOARD_PERIOD_KEYS = {key for key, _ in DASHBOARD_PERIODS}


def _period_range(period):
    today = date.today()
    if period == "today":
        return today, today
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d, d
    if period == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if period == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if period == "this_month":
        start = today.replace(day=1)
        end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        return start, end
    if period == "last_month":
        last_month_end = today.replace(day=1) - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    if period == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if period == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    return None, None


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


def _date_filtered_rows(db, table, date_column, date_from, date_to, limit=8):
    sql = f"SELECT * FROM {table} WHERE 1=1"
    params = {}
    if date_from:
        sql += f" AND {date_column} >= :date_from"
        params["date_from"] = date_from
    if date_to:
        sql += f" AND {date_column} <= :date_to"
        params["date_to"] = date_to
    sql += f" ORDER BY {date_column} DESC, id DESC LIMIT :limit"
    params["limit"] = limit
    return db.execute(text(sql), params).mappings().all()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@admin_bp.route("/")
def dashboard():
    db = get_db()

    period = request.args.get("period", "")
    if period not in DASHBOARD_PERIOD_KEYS:
        period = ""
    period_start, period_end = _period_range(period)
    date_from = period_start.isoformat() if period_start else ""
    date_to = period_end.isoformat() if period_end else ""
    period_label = dict(DASHBOARD_PERIODS).get(period, "All Time")

    items = db.execute(text("SELECT * FROM items")).mappings().all()
    total_items = len(items)
    total_units = sum(i["quantity"] for i in items)
    stock_value_cost = sum(i["quantity"] * i["cost_price"] for i in items)
    stock_value_retail = sum(i["quantity"] * i["sell_price"] for i in items)
    out_of_stock = [i for i in items if i["quantity"] <= 0]
    low_stock = [i for i in items if 0 < i["quantity"] <= LOW_STOCK_THRESHOLD]
    best_sellers = sorted(
        (i for i in items if i["star_rating"] >= 4),
        key=lambda i: i["star_rating"],
        reverse=True,
    )[:5]

    sales_where = " WHERE 1=1"
    sales_params = {}
    if date_from:
        sales_where += " AND s.sale_date >= :date_from"
        sales_params["date_from"] = date_from
    if date_to:
        sales_where += " AND s.sale_date <= :date_to"
        sales_params["date_to"] = date_to

    sales_summary = db.execute(text(
        "SELECT COALESCE(SUM(s.quantity - s.refunded_quantity),0) AS units_sold, "
        "COALESCE(SUM((s.quantity - s.refunded_quantity) * s.sale_price),0) AS gross_revenue, "
        "COALESCE(SUM(s.discount),0) AS total_discount "
        "FROM sales s" + sales_where
    ), sales_params).mappings().one()

    cogs_row = db.execute(text(
        "SELECT COALESCE(SUM((s.quantity - s.refunded_quantity) * i.cost_price),0) AS cogs "
        "FROM sales s JOIN items i ON i.id = s.item_id" + sales_where
    ), sales_params).mappings().one()

    ad_spend = _date_filtered_total(db, "advertising", "expense_date", date_from, date_to)
    other_expenses = _date_filtered_total(db, "expenses", "expense_date", date_from, date_to)
    other_income = _date_filtered_total(db, "other_income", "income_date", date_from, date_to)

    revenue = (sales_summary["gross_revenue"] or 0) - (sales_summary["total_discount"] or 0)
    cogs = cogs_row["cogs"] or 0
    gross_profit = revenue - cogs
    net_profit = gross_profit + other_income - ad_spend - other_expenses

    recent_sales = db.execute(text(
        "SELECT s.*, i.name AS item_name, i.size AS item_size, i.color AS item_color "
        "FROM sales s JOIN items i ON i.id = s.item_id" + sales_where +
        " ORDER BY s.sale_date DESC, s.id DESC LIMIT 8"
    ), sales_params).mappings().all()

    recent_expenses = _date_filtered_rows(db, "expenses", "expense_date", date_from, date_to)
    recent_income = _date_filtered_rows(db, "other_income", "income_date", date_from, date_to)

    live_visitor_count = count_live_visitors(db)

    return render_template(
        "dashboard.html",
        live_visitor_count=live_visitor_count,
        total_items=total_items,
        total_units=total_units,
        stock_value_cost=stock_value_cost,
        stock_value_retail=stock_value_retail,
        out_of_stock=out_of_stock,
        low_stock=low_stock,
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
        recent_expenses=recent_expenses,
        recent_income=recent_income,
        dashboard_periods=DASHBOARD_PERIODS,
        period=period,
        period_label=period_label,
        period_start=period_start,
        period_end=period_end,
    )


@admin_bp.route("/api/live-visitors")
def live_visitors():
    db = get_db()
    return jsonify(count=count_live_visitors(db))


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@admin_bp.route("/items")
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
    restock_items = db.execute(text(
        "SELECT * FROM items WHERE quantity > 0 AND quantity <= :threshold ORDER BY quantity, name"
    ), {"threshold": RESTOCK_ALERT_THRESHOLD}).mappings().all()

    return render_template(
        "items.html",
        items=items,
        q=q,
        status=status,
        category=category,
        categories=categories,
        low_stock_threshold=LOW_STOCK_THRESHOLD,
        restock_items=restock_items,
        restock_alert_threshold=RESTOCK_ALERT_THRESHOLD,
    )


@admin_bp.route("/items/new", methods=["GET", "POST"])
def item_new():
    if request.method == "POST":
        db = get_db()
        filename = save_photo(request.files.get("photo"))
        name = request.form.get("name", "").strip()
        size = request.form.get("size", "").strip()
        color = request.form.get("color", "").strip()
        quantity = parse_int(request.form.get("quantity"))
        cost_price = parse_float(request.form.get("cost_price"))
        sell_price = parse_float(request.form.get("sell_price"))
        discount_price = parse_optional_float(request.form.get("discount_price"))
        if discount_price is not None and not (0 < discount_price < sell_price):
            flash("Discount price must be less than the sell price — not saved.", "warning")
            discount_price = None

        db.execute(text(
            "INSERT INTO items (name, category, size, color, cost_price, sell_price, "
            "discount_price, quantity, date_added, image_filename, star_rating, notes) "
            "VALUES (:name, :category, :size, :color, :cost_price, :sell_price, "
            ":discount_price, :quantity, :date_added, :image_filename, :star_rating, :notes)"
        ), {
            "name": name,
            "category": request.form.get("category", "").strip(),
            "size": size,
            "color": color,
            "cost_price": cost_price,
            "sell_price": sell_price,
            "discount_price": discount_price,
            "quantity": quantity,
            "date_added": request.form.get("date_added") or date.today().isoformat(),
            "image_filename": filename,
            "star_rating": max(0, min(5, parse_int(request.form.get("star_rating")))),
            "notes": request.form.get("notes", "").strip(),
        })
        db.commit()
        flash("Item added.", "success")

        variant = ", ".join(v for v in [size, color] if v)
        notify_telegram(
            db,
            f"🆕 <b>New Item Added</b>\n"
            f"{html.escape(name)}{' (' + html.escape(variant) + ')' if variant else ''}\n"
            f"Qty: {quantity} | Cost: {CURRENCY}{cost_price:.2f} | Sell: {CURRENCY}{sell_price:.2f}"
        )
        return redirect(url_for("admin.items_list"))

    return render_template("item_form.html", item=None, today=date.today().isoformat())


@admin_bp.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
def item_edit(item_id):
    db = get_db()
    item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).mappings().first()
    if item is None:
        flash("Item not found.", "danger")
        return redirect(url_for("admin.items_list"))

    if request.method == "POST":
        filename = item["image_filename"]
        new_file = save_photo(request.files.get("photo"))
        if new_file:
            delete_photo(filename)
            filename = new_file
        elif request.form.get("remove_photo"):
            delete_photo(filename)
            filename = None

        sell_price = parse_float(request.form.get("sell_price"))
        discount_price = parse_optional_float(request.form.get("discount_price"))
        if discount_price is not None and not (0 < discount_price < sell_price):
            flash("Discount price must be less than the sell price — not saved.", "warning")
            discount_price = None

        db.execute(text(
            "UPDATE items SET name=:name, category=:category, size=:size, color=:color, "
            "cost_price=:cost_price, sell_price=:sell_price, discount_price=:discount_price, "
            "quantity=:quantity, date_added=:date_added, image_filename=:image_filename, "
            "star_rating=:star_rating, notes=:notes WHERE id=:id"
        ), {
            "name": request.form.get("name", "").strip(),
            "category": request.form.get("category", "").strip(),
            "size": request.form.get("size", "").strip(),
            "color": request.form.get("color", "").strip(),
            "cost_price": parse_float(request.form.get("cost_price")),
            "sell_price": sell_price,
            "discount_price": discount_price,
            "quantity": parse_int(request.form.get("quantity")),
            "date_added": request.form.get("date_added") or date.today().isoformat(),
            "image_filename": filename,
            "star_rating": max(0, min(5, parse_int(request.form.get("star_rating")))),
            "notes": request.form.get("notes", "").strip(),
            "id": item_id,
        })
        db.commit()
        flash("Item updated.", "success")
        return redirect(url_for("admin.items_list"))

    return render_template("item_form.html", item=item, today=date.today().isoformat())


@admin_bp.route("/items/<int:item_id>/delete", methods=["POST"])
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
    return redirect(url_for("admin.items_list"))


@admin_bp.route("/items/<int:item_id>/set-rating", methods=["POST"])
def item_set_rating(item_id):
    db = get_db()
    item = db.execute(
        text("SELECT star_rating FROM items WHERE id = :id"), {"id": item_id}
    ).mappings().first()
    if item is None:
        flash("Item not found.", "danger")
        return redirect(url_for("admin.items_list"))

    clicked = max(0, min(5, parse_int(request.form.get("star"))))
    new_rating = 0 if clicked == item["star_rating"] else clicked
    db.execute(text("UPDATE items SET star_rating = :rating WHERE id = :id"), {"rating": new_rating, "id": item_id})
    db.commit()
    return redirect(request.referrer or url_for("admin.items_list"))


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

@admin_bp.route("/sales")
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


@admin_bp.route("/sales/new", methods=["POST"])
def sale_new():
    db = get_db()
    item_id = parse_int(request.form.get("item_id"))
    quantity = parse_int(request.form.get("quantity"))
    item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).mappings().first()

    if not item:
        flash("Please choose a valid item.", "danger")
        return redirect(url_for("admin.sales_list"))
    if quantity <= 0:
        flash("Quantity sold must be greater than zero.", "danger")
        return redirect(url_for("admin.sales_list"))
    if quantity > item["quantity"]:
        flash(f"Only {item['quantity']} in stock for {item['name']} — cannot sell {quantity}.", "danger")
        return redirect(url_for("admin.sales_list"))

    sale_price = parse_float(request.form.get("sale_price"), item["sell_price"])
    sale_date = request.form.get("sale_date") or date.today().isoformat()
    buyer_name = request.form.get("buyer_name", "").strip()
    delivery_fee = parse_float(request.form.get("delivery_fee"), 0)
    discount = parse_float(request.form.get("discount"), 0)
    discount = max(0, min(discount, quantity * sale_price))

    db.execute(text(
        "INSERT INTO sales (item_id, quantity, sale_price, sale_date, buyer_name, address, "
        "phone_number, delivery_by, delivery_fee, discount) "
        "VALUES (:item_id, :quantity, :sale_price, :sale_date, :buyer_name, :address, "
        ":phone_number, :delivery_by, :delivery_fee, :discount)"
    ), {
        "item_id": item_id,
        "quantity": quantity,
        "sale_price": sale_price,
        "sale_date": sale_date,
        "buyer_name": buyer_name,
        "address": request.form.get("address", "").strip(),
        "phone_number": request.form.get("phone_number", "").strip(),
        "delivery_by": request.form.get("delivery_by", "").strip(),
        "delivery_fee": delivery_fee,
        "discount": discount,
    })
    db.execute(
        text("UPDATE items SET quantity = quantity - :quantity WHERE id = :id"),
        {"quantity": quantity, "id": item_id},
    )
    db.commit()
    flash(f"Sale recorded: {quantity} x {item['name']}.", "success")

    total = quantity * sale_price - discount + delivery_fee
    notify_telegram(
        db,
        f"💰 <b>New Sale</b>\n"
        f"{quantity} x {html.escape(item['name'])}\n"
        f"Total: {CURRENCY}{total:.2f}"
        + (f"\nBuyer: {html.escape(buyer_name)}" if buyer_name else "")
    )
    return redirect(url_for("admin.sales_list"))


@admin_bp.route("/sales/<int:sale_id>/edit-delivery", methods=["POST"])
def sale_edit_delivery(sale_id):
    db = get_db()
    sale = db.execute(
        text("SELECT id, quantity, sale_price, discount FROM sales WHERE id = :id"), {"id": sale_id}
    ).mappings().first()
    if not sale:
        flash("Sale not found.", "danger")
        return redirect(url_for("admin.sales_list"))

    sale_price = parse_float(request.form.get("sale_price"), sale["sale_price"])
    if sale_price <= 0:
        flash("Price must be greater than zero — not updated.", "danger")
        sale_price = sale["sale_price"]

    discount = parse_float(request.form.get("discount"), sale["discount"])
    discount = max(0, min(discount, sale["quantity"] * sale_price))

    db.execute(text(
        "UPDATE sales SET buyer_name=:buyer_name, address=:address, "
        "phone_number=:phone_number, delivery_by=:delivery_by, delivery_fee=:delivery_fee, "
        "sale_price=:sale_price, discount=:discount WHERE id=:id"
    ), {
        "buyer_name": request.form.get("buyer_name", "").strip(),
        "address": request.form.get("address", "").strip(),
        "phone_number": request.form.get("phone_number", "").strip(),
        "delivery_by": request.form.get("delivery_by", "").strip(),
        "delivery_fee": parse_float(request.form.get("delivery_fee"), 0),
        "sale_price": sale_price,
        "discount": discount,
        "id": sale_id,
    })
    db.commit()
    flash("Buyer and delivery info updated.", "success")
    return redirect(url_for("admin.sales_list"))


@admin_bp.route("/sales/<int:sale_id>/delete", methods=["POST"])
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
    return redirect(url_for("admin.sales_list"))


@admin_bp.route("/sales/<int:sale_id>/refund", methods=["POST"])
def sale_refund(sale_id):
    db = get_db()
    sale = db.execute(text("SELECT * FROM sales WHERE id = :id"), {"id": sale_id}).mappings().first()
    if not sale:
        flash("Sale not found.", "danger")
        return redirect(url_for("admin.sales_list"))

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

    return redirect(url_for("admin.sales_list"))


@admin_bp.route("/sales/<int:sale_id>/exchange", methods=["POST"])
def sale_exchange(sale_id):
    db = get_db()
    sale = db.execute(text("SELECT * FROM sales WHERE id = :id"), {"id": sale_id}).mappings().first()
    if not sale:
        flash("Sale not found.", "danger")
        return redirect(url_for("admin.sales_list"))

    remaining = sale["quantity"] - sale["refunded_quantity"]
    exchange_qty = parse_int(request.form.get("exchange_quantity"))
    new_item_id = parse_int(request.form.get("new_item_id"))
    new_item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": new_item_id}).mappings().first()

    if exchange_qty <= 0:
        flash("Exchange quantity must be greater than zero.", "danger")
        return redirect(url_for("admin.sales_list"))
    if exchange_qty > remaining:
        flash(f"Only {remaining} unit(s) from this sale can still be exchanged.", "danger")
        return redirect(url_for("admin.sales_list"))
    if not new_item:
        flash("Please choose a valid item to exchange for.", "danger")
        return redirect(url_for("admin.sales_list"))
    if new_item["quantity"] < exchange_qty:
        flash(f"Only {new_item['quantity']} in stock for {new_item['name']} — cannot exchange {exchange_qty}.", "danger")
        return redirect(url_for("admin.sales_list"))

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
        "INSERT INTO sales (item_id, quantity, sale_price, sale_date, exchanged_from_sale_id, "
        "buyer_name, address, phone_number, delivery_by, delivery_fee) "
        "VALUES (:item_id, :quantity, :sale_price, :sale_date, :exchanged_from_sale_id, "
        ":buyer_name, :address, :phone_number, :delivery_by, :delivery_fee)"
    ), {
        "item_id": new_item_id,
        "quantity": exchange_qty,
        "sale_price": new_price,
        "sale_date": new_date,
        "exchanged_from_sale_id": sale_id,
        "buyer_name": sale["buyer_name"],
        "address": sale["address"],
        "phone_number": sale["phone_number"],
        "delivery_by": sale["delivery_by"],
        "delivery_fee": sale["delivery_fee"],
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
    return redirect(url_for("admin.sales_list"))


@admin_bp.route("/sales/<int:sale_id>/receipt")
def sale_receipt(sale_id):
    db = get_db()
    sale = db.execute(text(
        "SELECT s.*, i.name AS item_name, i.size AS item_size, i.color AS item_color "
        "FROM sales s JOIN items i ON i.id = s.item_id WHERE s.id = :id"
    ), {"id": sale_id}).mappings().first()
    if not sale:
        flash("Sale not found.", "danger")
        return redirect(url_for("admin.sales_list"))
    return render_template("receipt.html", sale=sale)


@admin_bp.route("/sales/receipt-combined")
def sale_receipt_combined():
    db = get_db()
    sale_ids = [parse_int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()]
    sale_ids = [i for i in sale_ids if i > 0]
    if not sale_ids:
        flash("No sales selected to print.", "danger")
        return redirect(url_for("admin.sales_list"))

    placeholders = ", ".join(f":id{i}" for i in range(len(sale_ids)))
    params = {f"id{i}": sid for i, sid in enumerate(sale_ids)}
    sales = db.execute(text(
        "SELECT s.*, i.name AS item_name, i.size AS item_size, i.color AS item_color "
        f"FROM sales s JOIN items i ON i.id = s.item_id WHERE s.id IN ({placeholders}) "
        "ORDER BY s.id"
    ), params).mappings().all()

    if not sales:
        flash("Sale(s) not found.", "danger")
        return redirect(url_for("admin.sales_list"))

    def first_nonempty(field):
        for s in sales:
            if s[field]:
                return s[field]
        return ""

    subtotal = sum(s["quantity"] * s["sale_price"] for s in sales)
    delivery_fee = sum(s["delivery_fee"] for s in sales)
    discount = sum(s["discount"] for s in sales)

    return render_template(
        "receipt_combined.html",
        sales=sales,
        buyer_name=first_nonempty("buyer_name"),
        address=first_nonempty("address"),
        phone_number=first_nonempty("phone_number"),
        delivery_by=first_nonempty("delivery_by"),
        sale_date=sales[0]["sale_date"],
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        discount=discount,
        total=subtotal - discount + delivery_fee,
    )


# ---------------------------------------------------------------------------
# Advertising spend
# ---------------------------------------------------------------------------

@admin_bp.route("/advertising")
def advertising_list():
    db = get_db()
    expenses = db.execute(text("SELECT * FROM advertising ORDER BY expense_date DESC, id DESC")).mappings().all()
    total = sum(e["amount"] for e in expenses)
    return render_template("advertising.html", expenses=expenses, total=total, today=date.today().isoformat())


@admin_bp.route("/advertising/new", methods=["POST"])
def advertising_new():
    db = get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Amount must be greater than zero.", "danger")
        return redirect(url_for("admin.advertising_list"))
    platform = request.form.get("platform", "").strip()
    db.execute(text(
        "INSERT INTO advertising (expense_date, platform, description, amount) "
        "VALUES (:expense_date, :platform, :description, :amount)"
    ), {
        "expense_date": request.form.get("expense_date") or date.today().isoformat(),
        "platform": platform,
        "description": request.form.get("description", "").strip(),
        "amount": amount,
    })
    db.commit()
    flash("Advertising expense logged.", "success")

    notify_telegram(
        db,
        f"📢 <b>Advertising Expense</b>\n"
        f"{html.escape(platform) if platform else 'Advertising'}: {CURRENCY}{amount:.2f}"
    )
    return redirect(url_for("admin.advertising_list"))


@admin_bp.route("/advertising/<int:expense_id>/delete", methods=["POST"])
@admin_required
def advertising_delete(expense_id):
    db = get_db()
    db.execute(text("DELETE FROM advertising WHERE id = :id"), {"id": expense_id})
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("admin.advertising_list"))


# ---------------------------------------------------------------------------
# Other expenses (rent, utilities, wages, etc.)
# ---------------------------------------------------------------------------

@admin_bp.route("/expenses")
def expenses_list():
    db = get_db()
    expenses = db.execute(text("SELECT * FROM expenses ORDER BY expense_date DESC, id DESC")).mappings().all()
    total = sum(e["amount"] for e in expenses)
    return render_template("expenses.html", expenses=expenses, total=total, today=date.today().isoformat())


@admin_bp.route("/expenses/new", methods=["POST"])
def expense_new():
    db = get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Amount must be greater than zero.", "danger")
        return redirect(url_for("admin.expenses_list"))
    category = request.form.get("category", "").strip()
    db.execute(text(
        "INSERT INTO expenses (expense_date, category, description, amount) "
        "VALUES (:expense_date, :category, :description, :amount)"
    ), {
        "expense_date": request.form.get("expense_date") or date.today().isoformat(),
        "category": category,
        "description": request.form.get("description", "").strip(),
        "amount": amount,
    })
    db.commit()
    flash("Expense logged.", "success")

    notify_telegram(
        db,
        f"💸 <b>Expense Logged</b>\n"
        f"{html.escape(category) if category else 'Expense'}: {CURRENCY}{amount:.2f}"
    )
    return redirect(url_for("admin.expenses_list"))


@admin_bp.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@admin_required
def expense_delete(expense_id):
    db = get_db()
    db.execute(text("DELETE FROM expenses WHERE id = :id"), {"id": expense_id})
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("admin.expenses_list"))


# ---------------------------------------------------------------------------
# Other income (non-sales revenue)
# ---------------------------------------------------------------------------

@admin_bp.route("/income")
def income_list():
    db = get_db()
    income = db.execute(text("SELECT * FROM other_income ORDER BY income_date DESC, id DESC")).mappings().all()
    total = sum(i["amount"] for i in income)
    return render_template("income.html", income=income, total=total, today=date.today().isoformat())


@admin_bp.route("/income/new", methods=["POST"])
def income_new():
    db = get_db()
    amount = parse_float(request.form.get("amount"))
    if amount <= 0:
        flash("Amount must be greater than zero.", "danger")
        return redirect(url_for("admin.income_list"))
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
    return redirect(url_for("admin.income_list"))


@admin_bp.route("/income/<int:income_id>/delete", methods=["POST"])
@admin_required
def income_delete(income_id):
    db = get_db()
    db.execute(text("DELETE FROM other_income WHERE id = :id"), {"id": income_id})
    db.commit()
    flash("Income entry deleted.", "success")
    return redirect(url_for("admin.income_list"))


# ---------------------------------------------------------------------------
# Reports (profit & loss)
# ---------------------------------------------------------------------------

@admin_bp.route("/reports")
def reports():
    db = get_db()
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    sql = (
        "SELECT i.id, i.name, i.size, i.color, i.cost_price, "
        "SUM(s.quantity - s.refunded_quantity) AS units_sold, "
        "SUM((s.quantity - s.refunded_quantity) * s.sale_price) - SUM(s.discount) AS revenue, "
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


@admin_bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    db = get_db()

    if request.method == "POST":
        new_token = request.form.get("telegram_bot_token", "").strip()
        new_chat_id = request.form.get("telegram_chat_id", "").strip()
        if new_token:
            set_setting(db, "telegram_bot_token", new_token)
        if new_chat_id:
            set_setting(db, "telegram_chat_id", new_chat_id)

        new_chat_click_token = request.form.get("chat_click_bot_token", "").strip()
        new_chat_click_chat_id = request.form.get("chat_click_chat_id", "").strip()
        if new_chat_click_token:
            set_setting(db, "chat_click_bot_token", new_chat_click_token)
        if new_chat_click_chat_id:
            set_setting(db, "chat_click_chat_id", new_chat_click_chat_id)

        if "public_telegram_username" in request.form:
            public_telegram_username = request.form.get("public_telegram_username", "").strip().lstrip("@")
            set_setting(db, "public_telegram_username", public_telegram_username)
        db.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))

    token, chat_id = get_telegram_config(db)
    chat_click_token, chat_click_chat_id = get_chat_click_telegram_config(db)
    db_users = db.execute(
        text("SELECT id, username, is_admin, created_at FROM users ORDER BY created_at")
    ).mappings().all()
    return render_template(
        "settings.html",
        telegram_token_set=bool(token),
        telegram_chat_id=chat_id or "",
        chat_click_token_set=bool(chat_click_token),
        chat_click_chat_id=chat_click_chat_id or "",
        public_telegram_username=get_setting(db, "public_telegram_username") or "",
        built_in_users=[(u, info["is_admin"]) for u, info in AUTH_USERS.items()],
        db_users=db_users,
    )


@admin_bp.route("/settings/users/new", methods=["POST"])
@admin_required
def user_new():
    db = get_db()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "staff")

    if not username or not password:
        flash("Username and password are both required.", "danger")
        return redirect(url_for("admin.settings"))
    if username in AUTH_USERS or get_db_user(db, username):
        flash(f"An account named \"{username}\" already exists.", "danger")
        return redirect(url_for("admin.settings"))

    db.execute(text(
        "INSERT INTO users (username, password_hash, is_admin) VALUES (:username, :password_hash, :is_admin)"
    ), {
        "username": username,
        "password_hash": generate_password_hash(password),
        "is_admin": role == "admin",
    })
    db.commit()
    flash(f"Account \"{username}\" created.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/settings/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def user_delete(user_id):
    db = get_db()
    user = db.execute(text("SELECT username FROM users WHERE id = :id"), {"id": user_id}).mappings().first()
    if user and user["username"] == g.get("username"):
        flash("You can't delete the account you're currently logged in as.", "danger")
        return redirect(url_for("admin.settings"))
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.commit()
    flash("Account removed.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/settings/test-telegram", methods=["POST"])
@admin_required
def settings_test_telegram():
    db = get_db()
    token, chat_id = get_telegram_config(db)
    if not token or not chat_id:
        flash("Set the bot token and chat ID first, then save before testing.", "danger")
    else:
        notify_telegram(db, "✅ <b>Test notification</b>\nIf you can see this in your Telegram group, it's working!")
        flash("Test notification sent — check your Telegram group.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/settings/test-chat-click-telegram", methods=["POST"])
@admin_required
def settings_test_chat_click_telegram():
    db = get_db()
    token, chat_id = get_chat_click_telegram_config(db)
    if not token or not chat_id:
        flash("Set this bot's token and group chat ID first, then save before testing.", "danger")
    else:
        _telegram_post(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "✅ <b>Test notification</b>\nIf you can see this in your Telegram group, it's working!",
            "parse_mode": "HTML",
        })
        flash("Test notification sent — check your Telegram group.", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


# ---------------------------------------------------------------------------
# Public storefront — open to every visitor, no login. Read-only: browse the
# catalog and reach the shop on Telegram. Never exposes cost_price.
# ---------------------------------------------------------------------------

PUBLIC_FEED_BATCH_SIZE = 9

@public_bp.route("/")
def index():
    db = get_db()
    items = list(db.execute(text("SELECT * FROM items ORDER BY created_at DESC")).mappings().all())
    # Discounted items make the first impression; everything else follows.
    # Each group is shuffled on its own so the promo items stay on top but still vary.
    discounted = [i for i in items if i["discount_price"]]
    others = [i for i in items if not i["discount_price"]]
    random.shuffle(discounted)
    random.shuffle(others)
    items = discounted + others
    batches = [
        items[i:i + PUBLIC_FEED_BATCH_SIZE]
        for i in range(0, len(items), PUBLIC_FEED_BATCH_SIZE)
    ]
    public_telegram_username = get_setting(db, "public_telegram_username")
    return render_template(
        "public_feed.html",
        batches=batches,
        has_discounts=bool(discounted),
        public_telegram_username=public_telegram_username,
    )


@public_bp.route("/item/<int:item_id>")
def item_detail(item_id):
    db = get_db()
    item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).mappings().first()
    if not item:
        abort(404)

    if item["category"]:
        similar = db.execute(text(
            "SELECT * FROM items WHERE category = :category AND id != :id"
        ), {"category": item["category"], "id": item_id}).mappings().all()
    else:
        similar = db.execute(text(
            "SELECT * FROM items WHERE name = :name AND id != :id"
        ), {"name": item["name"], "id": item_id}).mappings().all()
    similar = list(similar)
    random.shuffle(similar)
    similar = similar[:12]

    public_telegram_username = get_setting(db, "public_telegram_username")
    return render_template(
        "public_item.html",
        item=item,
        similar_items=similar,
        public_telegram_username=public_telegram_username,
    )


@public_bp.route("/chat-click/<int:item_id>", methods=["POST"])
def chat_click(item_id):
    """A visitor tapped Chat on an item — send its photo to the shop's Telegram
    group so the owner instantly sees which item is being asked about."""
    db = get_db()
    item = db.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).mappings().first()
    if item:
        photo = absolute_photo_url(item["image_filename"])
        if photo:
            caption = (
                f"👀 <b>Visitor tapped Chat</b>\n"
                f"{html.escape(item['name'])}\n"
                f"{CURRENCY}{item['sell_price']:.2f}"
            )
            token, chat_id = get_chat_click_telegram_config(db)
            notify_telegram_photo(token, chat_id, photo, caption)
    return ("", 204)


@public_bp.route("/heartbeat", methods=["POST"])
def heartbeat():
    """Lightweight presence ping from the public storefront, used to count how
    many visitors are browsing right now (see admin.live_visitors)."""
    db = get_db()
    session_id = request.cookies.get("visitor_id") or uuid.uuid4().hex
    record_visitor_ping(db, session_id)
    db.commit()
    resp = make_response(("", 204))
    resp.set_cookie("visitor_id", session_id, max_age=60 * 60 * 24, httponly=True, samesite="Lax")
    return resp


# ---------------------------------------------------------------------------
# Legacy URL redirects — the admin tool used to live at the bare paths below;
# send old bookmarks/links to their new /pe/... home instead of 404ing.
# ---------------------------------------------------------------------------

for _legacy_path in ("items", "sales", "advertising", "expenses", "income", "reports", "settings", "login"):
    def _make_legacy_redirect(target=_legacy_path):
        def _view():
            return redirect(f"/pe/{target}", code=301)
        return _view
    app.add_url_rule(f"/{_legacy_path}", endpoint=f"legacy_{_legacy_path}", view_func=_make_legacy_redirect())


app.register_blueprint(admin_bp)
app.register_blueprint(public_bp)


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5050)
