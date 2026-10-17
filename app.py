"""GRAND MARKET DELIVERY — Excel catalog + authenticated administrator.

No 1C connection is required. Never commit credentials to GitHub.
"""
import hmac
import json
import os
import re
import secrets
import time
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, request, send_from_directory, session
from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, Numeric, String,
                        Table, Text, and_, bindparam, create_engine, delete, desc, func, insert,
                        or_, select, update)
from sqlalchemy.exc import IntegrityError
from werkzeug.middleware.proxy_fix import ProxyFix

from xlsx_reader import read_products

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder='static')
# Render's proxy terminates HTTPS. Trust one proxy hop only in controlled deployments.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '1') != '0',
    MAX_CONTENT_LENGTH=14 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)
ADMIN_KEY = os.environ.get('ADMIN_KEY', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER_ID = os.environ.get('OWNER_CHAT_ID', '')
ADMIN_ID = os.environ.get('ADMIN_CHAT_ID', '')
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
if DATABASE_URL:
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgres://'):]
    elif DATABASE_URL.startswith('postgresql://'):
        DATABASE_URL = 'postgresql+psycopg://' + DATABASE_URL[len('postgresql://'):]
else:
    DATABASE_URL = 'sqlite:///' + os.path.abspath(os.environ.get('DB_PATH', str(ROOT / 'orders.sqlite3')))
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
metadata = MetaData()
products_table = Table('products', metadata,
    Column('code', String(32), primary_key=True),
    Column('name', Text, nullable=False),
    Column('price', Integer, nullable=False),
    Column('stock', Numeric(15, 3), nullable=False),
    Column('image_url', Text, nullable=False, default=''),
    Column('category', Text, nullable=False, default=''),
    Column('featured', Integer, nullable=False, default=0),
    Column('active', Integer, nullable=False, default=1),
    Column('sort_order', Integer, nullable=False, default=10000),
    Column('updated_at', DateTime, nullable=False, default=datetime.utcnow),
)
orders_table = Table('orders', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('created_at', DateTime, server_default=func.current_timestamp()),
    Column('customer', Text, nullable=False),
    Column('phone', Text, nullable=False),
    Column('address', Text, nullable=False),
    Column('distance', Numeric(7, 2), nullable=False),
    Column('items', Text, nullable=False),
    Column('subtotal', Integer, nullable=False),
    Column('delivery', Integer, nullable=False),
    Column('total', Integer, nullable=False),
    Column('status', Text, nullable=False, server_default='new'),
)
metadata.create_all(engine)


def choose_category(name):
    x = name.upper()
    tests = [
        ('Мева-сабзавот', ('BANAN', 'OLMA', 'APPLE', 'MANDARIN', 'KARTOSHKA', 'POMIDOR', 'BODRING', 'SABZI', 'PIYOZ', 'UZUM', 'KAPUSTA', 'FRUKT', 'OVOSH', 'LIMON', 'GURUCH')),
        ('Нон ва пишириқлар', ('NON ', 'NON_', 'BULOCH', 'LAVASH', 'LEPYOSH', 'BAGET', 'PECHEN', 'PRYANIK')),
        ('Ичимликлар', ('COLA', 'PEPSI', 'SPRITE', 'FANTA', 'SOK', 'SUV', 'WATER', 'STING', 'ENERGY', 'SHARBAT', 'NAPITOK')),
        ('Сут ва тухум', ('TUXUM', 'YOGURT', 'KEFIR', 'SUT ', 'MOLOKO', 'QATIQ', 'SMETANA', 'TVOROG')),
        ('Гўшт ва колбаса', ('TOVUQ', 'FILE', 'KOLBASA', 'SOSISKA', 'GO`SHT', 'GO\'SHT', 'MYASO', 'KURITSA')),
        ('Ширинликлар', ('KONFET', 'SHOKOLAD', 'CHOCOLATE', 'SNICKERS', 'KARAMEL', 'ZEFIR', 'MARMELAD', 'TORT')),
        ('Уй-рўзғор', ('POROSHOK', 'SREDSTVO', 'ARIEL', 'FAIRY', 'GEL', 'SHAMP', 'SOVUN', 'SOAP', 'SALFETKA')),
    ]
    for cat, terms in tests:
        if any(t in x for t in terms):
            return cat
    return 'Бошқа маҳсулотлар'


def seed_catalog():
    with engine.begin() as db:
        if db.execute(select(func.count()).select_from(products_table)).scalar_one() > 0:
            return
        with (ROOT / 'catalog_seed.json').open(encoding='utf-8') as handle:
            seed = json.load(handle)
        now = datetime.utcnow()
        for offset in range(0, len(seed), 400):
            batch = seed[offset:offset+400]
            db.execute(insert(products_table), [{
                'code': str(p['code']), 'name': p['name'], 'price': int(p['price']),
                'stock': Decimal(str(p['stock'])), 'category': choose_category(p['name']),
                'image_url': '', 'featured': p['featured'], 'active': 1,
                'sort_order': p['sort_order'], 'updated_at': now,
            } for p in batch])

seed_catalog()


def parse_number(value, name, *, max_value=100000000):
    try:
        s = str(value).replace(' ', '').replace('\u00a0', '').replace(',', '.')
        v = Decimal(s)
        if not v.is_finite() or v < 0 or v > max_value:
            raise ValueError
        return v
    except (InvalidOperation, ValueError):
        raise ValueError(name + ' нотўғри') from None


def parse_price(value):
    return int(parse_number(value, 'Нарх').quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def parse_stock(value):
    return parse_number(value, 'Қолдиқ', max_value=10000000).quantize(Decimal('0.001'))


def validate_image_url(value):
    value = str(value or '').strip()
    if len(value) > 1024 or (value and (urlsplit(value).scheme != 'https' or not urlsplit(value).netloc)):
        raise ValueError('Расм учун https:// билан бошланувчи URL керак')
    return value


def flag(value, default=0):
    if value is None or str(value).strip() == '':
        return default
    return int(str(value).strip().lower() in ('1', 'true', 'yes', 'да', 'ха'))


def public_item(row):
    p = dict(row)
    return {'id': int(p['code']) if p['code'].isdigit() else p['code'],
            'code': p['code'], 'uz': p['name'], 'ru': p['name'],
            'price': p['price'], 'stock': float(p['stock']),
            'image_url': p['image_url'], 'category': p['category'],
            'featured': bool(p['featured']), 'icon': '🛒'}


def admin_item(row):
    p = dict(row)
    p['stock'] = float(p['stock'])
    p['updated_at'] = p['updated_at'].isoformat() if isinstance(p['updated_at'], datetime) else str(p['updated_at'])
    return p


@app.after_request
def browser_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/admin') or request.path == '/admin' else 'no-cache'
    if request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.get('/')
def homepage():
    return send_from_directory(ROOT, 'index.html')


@app.get('/admin')
def admin_home():
    return send_from_directory(ROOT, 'admin.html')


@app.get('/api/products')
def products_api():
    q = request.args.get('q', request.args.get('search', '')).strip()[:100]
    category = request.args.get('category', '').strip()[:100]
    featured = request.args.get('featured', '') == '1'
    clauses = [products_table.c.active == 1, products_table.c.stock > 0, products_table.c.price > 0]
    if q:
        clauses.append(func.lower(products_table.c.name).like('%' + q.lower().replace('%', '\\%').replace('_', '\\_') + '%', escape='\\'))
    if category:
        clauses.append(products_table.c.category == category)
    if featured:
        clauses.append(products_table.c.featured == 1)
    cond = and_(*clauses)
    with engine.connect() as db:
        if not request.args:
            # Keep the original /api/products JSON-array contract for older clients.
            records = db.execute(select(products_table).where(cond).order_by(products_table.c.sort_order, products_table.c.name)).mappings().all()
            return jsonify([public_item(x) for x in records])
        try:
            page = max(1, int(request.args.get('page', '1')))
            limit = min(60, max(1, int(request.args.get('limit', '24'))))
        except ValueError:
            return jsonify(error='Саҳифа параметри нотўғри'), 400
        total = db.execute(select(func.count()).select_from(products_table).where(cond)).scalar_one()
        records = db.execute(select(products_table).where(cond).order_by(products_table.c.sort_order, products_table.c.name).offset((page-1)*limit).limit(limit)).mappings().all()
    return jsonify(items=[public_item(x) for x in records], total=total, page=page, pages=(total+limit-1)//limit)


@app.get('/api/categories')
def categories_api():
    with engine.connect() as db:
        rows = db.execute(select(products_table.c.category, func.count()).where(and_(products_table.c.active == 1, products_table.c.stock > 0, products_table.c.price > 0)).group_by(products_table.c.category).order_by(products_table.c.category)).all()
    return jsonify([{'category': cat or 'Бошқа маҳсулотлар', 'count': count} for cat, count in rows])


def notify(text):
    if not BOT_TOKEN:
        return
    for chat_id in {x for x in [OWNER_ID, ADMIN_ID] if x}:
        try:
            payload = json.dumps({'chat_id': chat_id, 'text': text}, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request('https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage', data=payload, headers={'Content-Type':'application/json'})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as error:
            app.logger.warning('Telegram хабар хатоси: %s', error)


@app.post('/api/orders')
def new_order():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error='Буюртма формати нотўғри'), 400
    customer = str(body.get('customer', '')).strip()[:100]
    phone = str(body.get('phone', '')).strip()[:40]
    address = str(body.get('address', '')).strip()[:300]
    if not all([customer, phone, address]):
        return jsonify(error='Исм, телефон ва манзил керак'), 400
    try:
        distance = parse_number(body.get('distance', ''), 'Масофа', max_value=50)
    except ValueError as error:
        return jsonify(error=str(error)), 400
    raw = body.get('items', [])
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        return jsonify(error='Саватча бўш ёки жуда катта'), 400
    try:
        wanted = {}
        for item in raw:
            code = str(item.get('id', item.get('code', ''))).strip()
            if not re.fullmatch(r'[0-9A-Za-z_-]{1,32}', code):
                raise ValueError('Маҳсулот коди нотўғри')
            qty = parse_number(item.get('qty', 0), 'Миқдор', max_value=100).quantize(Decimal('0.001'))
            if qty <= 0 or qty < Decimal('0.001'):
                raise ValueError('Миқдор нолдан катта бўлиши керак')
            wanted[code] = wanted.get(code, Decimal('0')) + qty
            if wanted[code] > 100:
                raise ValueError('Миқдор 100 дан ошмасин')
    except (ValueError, AttributeError) as err:
        return jsonify(error=str(err)), 400
    line_items = []
    subtotal = 0
    try:
        with engine.begin() as db:
            records = {p['code']: p for p in db.execute(select(products_table).where(products_table.c.code.in_(wanted))).mappings()}
            for code, qty in wanted.items():
                p = records.get(code)
                if not p or p['active'] != 1 or p['price'] <= 0:
                    raise ValueError('Маҳсулот топилмади ёки сотувда эмас: ' + code)
                if Decimal(p['stock']) < qty:
                    raise ValueError('Қолдиқ етарли эмас: ' + p['name'])
                result = db.execute(update(products_table).where(and_(products_table.c.code == code, products_table.c.stock >= qty)).values(stock=products_table.c.stock - qty, updated_at=datetime.utcnow()))
                if result.rowcount != 1:
                    raise ValueError('Қолдиқ тугаган: ' + p['name'])
                price = int(p['price'])
                subtotal += int((Decimal(price)*qty).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                line_items.append({'id': int(code) if code.isdigit() else code, 'code': code, 'name': p['name'], 'qty': float(qty), 'price': price})
            delivery = 0 if subtotal >= 300000 else (10000 if distance <= 3 else 15000 if distance <= 5 else 20000)
            total = subtotal + delivery
            inserted = db.execute(insert(orders_table).values(customer=customer, phone=phone, address=address, distance=distance, items=json.dumps(line_items, ensure_ascii=False), subtotal=subtotal, delivery=delivery, total=total, status='new'))
            order_id = inserted.inserted_primary_key[0]
    except ValueError as error:
        return jsonify(error=str(error)), 400
    text = f'🛒 GRAND MARKET буюртма #{order_id}\n👤 {customer}\n📞 {phone}\n📍 {address}\n🚗 {distance} км\n' + '\n'.join(f"{it['name']} × {it['qty']:g}" for it in line_items) + f'\nМаҳсулот: {subtotal:,} сўм\nДоставка: {delivery:,} сўм\nЖами: {total:,} сўм\nТўлов: етказганда'
    notify(text)
    return jsonify(id=order_id, total=total, delivery=delivery, status='new'), 201


_FAILED_LOGINS = {}


def legacy_key_valid():
    return bool(ADMIN_KEY) and hmac.compare_digest(request.headers.get('X-Admin-Key', ''), ADMIN_KEY)


def admin_guard(write=False):
    if legacy_key_valid():
        return None
    if not session.get('admin'):
        return jsonify(error='Админ сифатида киринг'), 401
    if write and not hmac.compare_digest(str(request.headers.get('X-CSRF-Token', '')), str(session.get('csrf', 'UNSET'))):
        return jsonify(error='Хавфсизлик токени хато; саҳифани янгиланг'), 403
    return None


def secure_admin(write=False):
    def deco(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            fail = admin_guard(write)
            return fail if fail is not None else fn(*args, **kwargs)
        return guarded
    return deco


@app.post('/api/admin/login')
def admin_login():
    if not ADMIN_KEY:
        return jsonify(error='Render Settings → Environment да ADMIN_KEY ўрнатилмаган'), 503
    ip = request.remote_addr or 'unknown'
    now = time.time()
    failed = [t for t in _FAILED_LOGINS.get(ip, []) if now - t < 600]
    if len(failed) >= 8:
        return jsonify(error='Кўп марта уринилди. 10 дақиқадан кейин қайта урининг'), 429
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not hmac.compare_digest(str(body.get('key', '')), ADMIN_KEY):
        failed.append(now)
        _FAILED_LOGINS[ip] = failed
        return jsonify(error='Админ пароли нотўғри'), 401
    _FAILED_LOGINS.pop(ip, None)
    session.clear()
    session.permanent = True
    session['admin'] = True
    session['csrf'] = secrets.token_urlsafe(32)
    return jsonify(ok=True, csrf=session['csrf'])


@app.get('/api/admin/session')
def admin_session():
    return jsonify(authenticated=bool(session.get('admin')), csrf=session.get('csrf', '') if session.get('admin') else '')


@app.post('/api/admin/logout')
@secure_admin(write=True)
def admin_logout():
    session.clear()
    return jsonify(ok=True)


@app.get('/api/admin/overview')
@secure_admin()
def admin_overview():
    with engine.connect() as db:
        counts = db.execute(select(func.count(), func.sum(func.cast(products_table.c.active == 1, Integer)), func.sum(func.cast(and_(products_table.c.stock > 0, products_table.c.active == 1), Integer)), func.sum(func.cast(products_table.c.featured == 1, Integer))).select_from(products_table)).one()
        order_count = db.execute(select(func.count()).select_from(orders_table)).scalar_one()
    return jsonify(total=counts[0], active=counts[1] or 0, available=counts[2] or 0, featured=counts[3] or 0, orders=order_count)


@app.get('/api/admin/products')
@secure_admin()
def admin_products():
    query = request.args.get('q', '').strip()[:100]
    try:
        page = max(1, int(request.args.get('page', '1')))
    except ValueError:
        page = 1
    conditions = []
    if query:
        pattern = '%' + query.lower().replace('%', '\\%').replace('_', '\\_') + '%'
        conditions.append(or_(func.lower(products_table.c.name).like(pattern, escape='\\'), products_table.c.code.like(pattern, escape='\\')))
    cond = and_(*conditions) if conditions else True
    with engine.connect() as db:
        count = db.execute(select(func.count()).select_from(products_table).where(cond)).scalar_one()
        rows = db.execute(select(products_table).where(cond).order_by(products_table.c.sort_order, products_table.c.name).offset((page-1)*40).limit(40)).mappings().all()
    return jsonify(items=[admin_item(x) for x in rows], total=count, page=page, pages=(count+39)//40)


def normalized_product(data, original=None):
    if not isinstance(data, dict):
        raise ValueError('Маълумот формати нотўғри')
    p = dict(original or {})
    code = str(data.get('code', p.get('code', ''))).strip()
    if not re.fullmatch(r'[0-9A-Za-z_-]{1,32}', code):
        raise ValueError('Маҳсулот коди нотўғри')
    name = str(data.get('name', p.get('name', ''))).strip()
    if not 1 <= len(name) <= 240:
        raise ValueError('Ном 1–240 та белгидан иборат бўлсин')
    p.update(code=code, name=name,
             price=parse_price(data.get('price', p.get('price', ''))),
             stock=parse_stock(data.get('stock', p.get('stock', ''))),
             image_url=validate_image_url(data.get('image_url', p.get('image_url', ''))),
             category=str(data.get('category', p.get('category', '') or choose_category(name))).strip()[:100],
             featured=flag(data.get('featured'), p.get('featured', 0)),
             active=flag(data.get('active'), p.get('active', 1)),
             sort_order=int(p.get('sort_order', 10000)), updated_at=datetime.utcnow())
    return p


@app.post('/api/admin/products')
@secure_admin(write=True)
def add_product():
    try:
        row = normalized_product(request.get_json(silent=True) or {})
        with engine.begin() as db:
            db.execute(insert(products_table).values(**row))
    except ValueError as err:
        return jsonify(error=str(err)), 400
    except IntegrityError:
        return jsonify(error='Бу кодли маҳсулот мавжуд'), 409
    return jsonify(ok=True, code=row['code']), 201


@app.patch('/api/admin/products/<code>')
@secure_admin(write=True)
def update_product(code):
    with engine.begin() as db:
        old = db.execute(select(products_table).where(products_table.c.code == code)).mappings().first()
        if not old:
            return jsonify(error='Маҳсулот топилмади'), 404
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict) or ('code' in data and str(data['code']) != code):
            return jsonify(error='Маҳсулот кодини ўзгартириб бўлмайди'), 400
        try:
            new = normalized_product(data, old)
        except ValueError as err:
            return jsonify(error=str(err)), 400
        db.execute(update(products_table).where(products_table.c.code == code).values(**{k:v for k,v in new.items() if k != 'code'}))
    return jsonify(ok=True)


@app.delete('/api/admin/products/<code>')
@secure_admin(write=True)
def hide_product(code):
    with engine.begin() as db:
        result = db.execute(update(products_table).where(products_table.c.code == code).values(active=0, updated_at=datetime.utcnow()))
    return (jsonify(ok=True) if result.rowcount else (jsonify(error='Маҳсулот топилмади'), 404))


@app.post('/api/admin/import')
@secure_admin(write=True)
def import_excel():
    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename.lower().endswith('.xlsx'):
        return jsonify(error='Фақат .xlsx Excel файлини танланг'), 400
    try:
        sheet, raw = read_products(uploaded.read())
        if not raw:
            raise ValueError('Excel ичида маҳсулотлар йўқ')
        seen, changes = set(), []
        now = datetime.utcnow()
        for r in raw:
            code = str(r['code']).strip()
            if code in seen:
                raise ValueError('Бир хил код икки марта такрорланган: ' + code)
            seen.add(code)
            # Price must be positive for the customer-facing catalog.
            p = normalized_product({
                'code':code, 'name':r['name'], 'price':r['price'], 'stock':r['stock'],
                'image_url':r['image_url'], 'category':r['category'],
                'featured':r['featured'] if r['featured'] != '' else None,
                'active':r['active'] if r['active'] != '' else None,
            })
            p['updated_at'] = now
            changes.append(p)
        deactivate_missing = request.form.get('deactivate_missing') == '1'
        raw_by_code = {str(row['code']).strip(): row for row in raw}
        updated = created = 0
        with engine.begin() as db:
            # A single transaction prevents half-imported catalogs.
            existing = {p['code']:p for p in db.execute(select(products_table)).mappings()}
            pending_updates = []
            pending_inserts = []
            for p in changes:
                old = existing.get(p['code'])
                if old:
                    # Empty image/category/featured cells preserve manual metadata.
                    if not p['image_url']:
                        p['image_url'] = old['image_url']
                    if not str(raw_by_code[p['code']]['category']).strip():
                        p['category'] = old['category'] or choose_category(p['name'])
                    if old['featured'] == 1 and not raw_by_code[p['code']]['featured']:
                        p['featured'] = old['featured']
                    p['sort_order'] = old['sort_order']
                    pending_updates.append(p)
                    updated += 1
                else:
                    pending_inserts.append(p)
                    created += 1
            if pending_updates:
                statement = update(products_table).where(products_table.c.code == bindparam('target_code')).values(
                    name=bindparam('val_name'), price=bindparam('val_price'),
                    stock=bindparam('val_stock'), image_url=bindparam('val_image'),
                    category=bindparam('val_category'), featured=bindparam('val_featured'),
                    active=bindparam('val_active'), sort_order=bindparam('val_sort_order'),
                    updated_at=bindparam('val_updated'))
                for offset in range(0, len(pending_updates), 400):
                    params = [{
                        'target_code':p['code'], 'val_name':p['name'],
                        'val_price':p['price'], 'val_stock':p['stock'],
                        'val_image':p['image_url'], 'val_category':p['category'],
                        'val_featured':p['featured'], 'val_active':p['active'],
                        'val_sort_order':p['sort_order'], 'val_updated':p['updated_at'],
                    } for p in pending_updates[offset:offset+400]]
                    db.execute(statement, params)
            for offset in range(0, len(pending_inserts), 400):
                db.execute(insert(products_table), pending_inserts[offset:offset+400])
            if deactivate_missing:
                absent = [code for code in existing if code not in seen]
                for index in range(0, len(absent), 400):
                    db.execute(update(products_table).where(products_table.c.code.in_(absent[index:index+400])).values(active=0, updated_at=now))
        return jsonify(ok=True, sheet=sheet, created=created, updated=updated, total_rows=len(changes), missing_deactivated=deactivate_missing)
    except ValueError as err:
        return jsonify(error=str(err)), 400
    except Exception:
        app.logger.exception('Excel import failed')
        return jsonify(error='Excel импортида хато. Файл форматини текширинг'), 500


@app.get('/api/admin/orders')
@secure_admin()
def admin_orders():
    with engine.connect() as db:
        rows = db.execute(select(orders_table).order_by(desc(orders_table.c.id)).limit(200)).mappings().all()
    return jsonify([{**dict(x), 'items': json.loads(x['items']), 'created_at': str(x['created_at']), 'distance': float(x['distance'])} for x in rows])


@app.patch('/api/admin/orders/<int:order_id>')
@secure_admin(write=True)
def admin_order_status(order_id):
    status = (request.get_json(silent=True) or {}).get('status')
    allowed = {'new', 'confirmed', 'packing', 'on_the_way', 'delivered', 'cancelled'}
    if status not in allowed:
        return jsonify(error='Буюртма ҳолати нотўғри'), 400
    try:
        with engine.begin() as db:
            prev = db.execute(select(orders_table).where(orders_table.c.id == order_id)).mappings().first()
            if not prev:
                return jsonify(error='Буюртма топилмади'), 404
            if prev['status'] == status:
                return jsonify(ok=True)
            order_items = json.loads(prev['items'])
            if status == 'cancelled' and prev['status'] != 'cancelled':
                for item in order_items:
                    db.execute(update(products_table).where(products_table.c.code == str(item.get('code', item['id']))).values(stock=products_table.c.stock + Decimal(str(item['qty']))))
            elif prev['status'] == 'cancelled' and status != 'cancelled':
                for item in order_items:
                    code = str(item.get('code', item['id']))
                    qty = Decimal(str(item['qty']))
                    result = db.execute(update(products_table).where(and_(products_table.c.code == code, products_table.c.stock >= qty)).values(stock=products_table.c.stock - qty))
                    if not result.rowcount:
                        raise ValueError('Омборда етарли маҳсулот йўқ, бекор қилинган буюртмани тиклаб бўлмайди')
            db.execute(update(orders_table).where(orders_table.c.id == order_id).values(status=status))
    except ValueError as error:
        return jsonify(error=str(error)), 409
    return jsonify(ok=True)


@app.get('/health')
def health():
    return jsonify(status='ok')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), debug=False)
