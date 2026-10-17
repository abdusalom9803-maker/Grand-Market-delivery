import os, sqlite3, json, urllib.request, hmac, hashlib, secrets, time
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, Response
from xml.sax.saxutils import escape

app = Flask(__name__)
DB = os.environ.get('DB_PATH', 'orders.sqlite3')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
OWNER_ID = os.environ.get('OWNER_CHAT_ID', '')
ADMIN_ID = os.environ.get('ADMIN_CHAT_ID', '')
ADMIN_KEY = os.environ.get('ADMIN_KEY', '')
EXCHANGE_USER = os.environ.get('EXCHANGE_USER', '')
EXCHANGE_PASSWORD = os.environ.get('EXCHANGE_PASSWORD', '')
# Persistent database and import uploads are recommended on a Render disk.
UPLOAD_DIR = os.environ.get('EXCHANGE_UPLOAD_DIR', '/tmp/grand_1c_uploads')
MAX_XML_BYTES = 25 * 1024 * 1024
SESSIONS = {}  # In-memory sessions: use one gunicorn worker.

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            customer TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL,
            distance REAL NOT NULL, items TEXT NOT NULL, subtotal INTEGER NOT NULL,
            delivery INTEGER NOT NULL, total INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'new')''')
        c.execute('''CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY, uz TEXT NOT NULL, ru TEXT NOT NULL,
            price INTEGER NOT NULL DEFAULT 0, stock REAL NOT NULL DEFAULT 0,
            icon TEXT NOT NULL DEFAULT '🛒', active INTEGER NOT NULL DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS exchange_log (
            filename TEXT PRIMARY KEY, processed_at TEXT DEFAULT CURRENT_TIMESTAMP)''')
    os.makedirs(UPLOAD_DIR, exist_ok=True)
init()

DEMO_PRODUCTS = [
    {'id':'demo-1','uz':'Банан 1 кг','ru':'Бананы 1 кг','price':19990,'icon':'🍌','stock':100},
    {'id':'demo-2','uz':'Олма 1 кг','ru':'Яблоки 1 кг','price':13990,'icon':'🍎','stock':100},
    {'id':'demo-3','uz':'Картошка 1 кг','ru':'Картофель 1 кг','price':6990,'icon':'🥔','stock':100},
    {'id':'demo-4','uz':'Тухум 10 дона','ru':'Яйца 10 шт','price':13990,'icon':'🥚','stock':100},
    {'id':'demo-5','uz':'Сув 1,5 л','ru':'Вода 1,5 л','price':4990,'icon':'💧','stock':100},
    {'id':'demo-6','uz':'Товуқ филе 1 кг','ru':'Куриное филе 1 кг','price':42990,'icon':'🍗','stock':100},
]
with db() as c:
    if c.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
        c.executemany('INSERT INTO products(id,uz,ru,price,stock,icon) VALUES(:id,:uz,:ru,:price,:stock,:icon)', DEMO_PRODUCTS)

@app.get('/')
def home():
    return send_from_directory('.', 'index.html')

@app.get('/api/products')
def products():
    with db() as c:
        rows = c.execute('SELECT id,uz,ru,price,stock,icon FROM products WHERE active=1 ORDER BY uz').fetchall()
    return jsonify([dict(r) for r in rows])

def notify(text):
    if not BOT_TOKEN: return
    for chat in set(x for x in (OWNER_ID, ADMIN_ID) if x):
        try:
            payload = json.dumps({'chat_id':chat,'text':text}).encode()
            req = urllib.request.Request('https://api.telegram.org/bot'+BOT_TOKEN+'/sendMessage',
                data=payload, headers={'Content-Type':'application/json'})
            urllib.request.urlopen(req, timeout=8).read()
        except Exception as e:
            app.logger.warning('Telegram notification failed: %s', e)

@app.post('/api/orders')
def order():
    data = request.get_json(silent=True) or {}
    customer = str(data.get('customer','')).strip()[:100]
    phone = str(data.get('phone','')).strip()[:40]
    address = str(data.get('address','')).strip()[:300]
    if not customer or not phone or not address:
        return jsonify(error='Исм, телефон ва манзил керак'),400
    try: distance = float(data.get('distance',0))
    except (TypeError,ValueError): return jsonify(error='Масофа нотўғри'),400
    if not 0 <= distance <= 50: return jsonify(error='Масофа 0–50 км бўлиши керак'),400
    raw = data.get('items',[])
    if not isinstance(raw,list) or not raw or len(raw)>100:
        return jsonify(error='Саватча бўш'),400
    with db() as c:
        catalog = {r['id']:dict(r) for r in c.execute('SELECT * FROM products WHERE active=1')}
    items, subtotal = [], 0
    for x in raw:
        try:
            pid = str(x['id']); qty = int(x['qty'])
        except (KeyError,TypeError,ValueError): return jsonify(error='Маҳсулот хато'),400
        if pid not in catalog or not 1 <= qty <= 100:
            return jsonify(error='Маҳсулот ёки миқдор хато'),400
        p = catalog[pid]
        if p['stock'] < qty: return jsonify(error=f"Омборда етарли эмас: {p['uz']}"),400
        subtotal += p['price'] * qty
        items.append({'id':pid,'name':p['uz'],'qty':qty,'price':p['price']})
    delivery = 0 if subtotal>=300000 else (10000 if distance<=3 else 15000 if distance<=5 else 20000)
    total = subtotal+delivery
    with db() as c:
        cur = c.execute('''INSERT INTO orders(customer,phone,address,distance,items,subtotal,delivery,total)
            VALUES(?,?,?,?,?,?,?,?)''',
            (customer,phone,address,distance,json.dumps(items,ensure_ascii=False),subtotal,delivery,total))
        oid = cur.lastrowid
    msg = (f'🛒 GRAND MARKET буюртма #{oid}\n👤 {customer}\n📞 {phone}\n📍 {address}\n🚗 {distance} км\n'
           + '\n'.join(f"{i['name']} × {i['qty']}" for i in items)
           + f'\nМаҳсулот: {subtotal:,} сўм\nДоставка: {delivery:,} сўм\nЖами: {total:,} сўм\nТўлов: етказганда')
    notify(msg)
    return jsonify(id=oid,total=total,delivery=delivery,status='new'),201

def authorized(fn):
    @wraps(fn)
    def inner(*args,**kwargs):
        key = request.headers.get('X-Admin-Key','')
        if not ADMIN_KEY or not hmac.compare_digest(key, ADMIN_KEY):
            return jsonify(error='Рухсат йўқ'),401
        return fn(*args,**kwargs)
    return inner

@app.get('/api/admin/orders')
@authorized
def orders():
    with db() as c:
        rows=c.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 200').fetchall()
    return jsonify([{**dict(r),'items':json.loads(r['items'])} for r in rows])

@app.patch('/api/admin/orders/<int:oid>')
@authorized
def status(oid):
    s=(request.get_json(silent=True) or {}).get('status')
    if s not in ('new','confirmed','packing','on_the_way','delivered','cancelled'):
        return jsonify(error='Ҳолат хато'),400
    with db() as c: cur=c.execute('UPDATE orders SET status=? WHERE id=?',(s,oid))
    if not cur.rowcount:return jsonify(error='Топилмади'),404
    return jsonify(ok=True)

# 1C CommerceML 2 HTTP exchange protocol (minimal implementation).
# Endpoint for 1C: https://YOUR-SERVICE.onrender.com/1c/exchange
# 1C will call checkauth, init, file, import and query/success.
def reply(text, status=200):
    return Response(text+'\n', status=status, content_type='text/plain; charset=utf-8')

def valid_session():
    sid = request.cookies.get('grand1c') or request.args.get('sessid','')
    now=time.time()
    for key,expires in list(SESSIONS.items()):
        if expires < now: SESSIONS.pop(key,None)
    return bool(sid and SESSIONS.get(sid,0)>now)

def local_name(tag):
    return tag.rsplit('}',1)[-1]

def child(node, name):
    return next((x for x in node if local_name(x.tag)==name),None)

def value(node, name, default=''):
    x=child(node,name)
    return (x.text or '').strip() if x is not None else default

def descendants(node, name):
    return (x for x in node.iter() if local_name(x.tag)==name)

def number(s):
    try: return float(str(s).replace(',','.'))
    except (TypeError,ValueError): return 0.0

def parse_xml(blob):
    if b'<!DOCTYPE' in blob.upper() or b'<!ENTITY' in blob.upper():
        raise ValueError('DOCTYPE/ENTITY not allowed')
    return ET.fromstring(blob)

def import_xml(blob):
    root=parse_xml(blob)
    count=0
    with db() as c:
        for p in descendants(root,'Товар'):
            pid=value(p,'Ид'); name=value(p,'Наименование')
            if not pid or not name: continue
            # Existing prices/stocks retained until offers arrive.
            c.execute('''INSERT INTO products(id,uz,ru,price,stock,icon,active)
                VALUES(?,?,?,0,0,'🛒',1)
                ON CONFLICT(id) DO UPDATE SET uz=excluded.uz,ru=excluded.ru,active=1''',
                (pid,name,name))
            count+=1
        for offer in descendants(root,'Предложение'):
            pid=value(offer,'Ид').split('#')[0]
            if not pid: continue
            name=value(offer,'Наименование') or pid
            price=None
            for pr in descendants(offer,'Цена'):
                amount=value(pr,'ЦенаЗаЕдиницу')
                if amount:
                    price=round(number(amount)); break
            stocktext=value(offer,'Количество')
            if not stocktext:
                stocktext=value(offer,'Остаток')
            stock=number(stocktext) if stocktext else None
            c.execute('''INSERT INTO products(id,uz,ru,price,stock,icon,active)
                VALUES(?,?,?, ?, ?, '🛒',1)
                ON CONFLICT(id) DO UPDATE SET
                price=COALESCE(?,products.price),stock=COALESCE(?,products.stock),active=1''',
                (pid,name,name,price or 0,stock or 0,price,stock))
            count+=1
    return count

def order_xml():
    with db() as c:
        rows=c.execute("SELECT * FROM orders WHERE status NOT IN ('cancelled') ORDER BY id").fetchall()
    out=['<?xml version="1.0" encoding="UTF-8"?>',
         '<КоммерческаяИнформация ВерсияСхемы="2.05" ДатаФормирования="'+datetime.now().strftime('%Y-%m-%dT%H:%M:%S')+'">']
    for r in rows:
        items=json.loads(r['items'])
        out.append('<Документ><Ид>'+str(r['id'])+'</Ид><Номер>'+str(r['id'])+'</Номер>'
                   '<Дата>'+escape(r['created_at'][:10])+'</Дата><ХозОперация>Заказ товара</ХозОперация>'
                   '<Роль>Продавец</Роль><Валюта>UZS</Валюта><Курс>1</Курс><Сумма>'+str(r['total'])+'</Сумма>'
                   '<Контрагенты><Контрагент><Ид>customer-'+str(r['id'])+'</Ид><Наименование>'+escape(r['customer'])+'</Наименование>'
                   '<ПолноеНаименование>'+escape(r['customer'])+'</ПолноеНаименование></Контрагент></Контрагенты>'
                   '<ЗначенияРеквизитов><ЗначениеРеквизита><Наименование>Телефон</Наименование><Значение>'+escape(r['phone'])+'</Значение></ЗначениеРеквизита>'
                   '<ЗначениеРеквизита><Наименование>Адрес доставки</Наименование><Значение>'+escape(r['address'])+'</Значение></ЗначениеРеквизита></ЗначенияРеквизитов><Товары>')
        for i in items:
            out.append('<Товар><Ид>'+escape(str(i['id']))+'</Ид><Наименование>'+escape(i['name'])+'</Наименование>'
                       '<ЦенаЗаЕдиницу>'+str(i['price'])+'</ЦенаЗаЕдиницу><Количество>'+str(i['qty'])+'</Количество>'
                       '<Сумма>'+str(i['price']*i['qty'])+'</Сумма></Товар>')
        if r['delivery']:
            out.append('<Товар><Ид>delivery</Ид><Наименование>Доставка</Наименование><ЦенаЗаЕдиницу>'+str(r['delivery'])+'</ЦенаЗаЕдиницу>'
                       '<Количество>1</Количество><Сумма>'+str(r['delivery'])+'</Сумма></Товар>')
        out.append('</Товары></Документ>')
    out.append('</КоммерческаяИнформация>')
    return '\n'.join(out)

@app.route('/1c/exchange',methods=['GET','POST'])
def exchange():
    kind=request.args.get('type','catalog')
    mode=request.args.get('mode','')
    if mode=='checkauth':
        creds=request.authorization
        if (not EXCHANGE_USER or not EXCHANGE_PASSWORD or not creds or
            not hmac.compare_digest(creds.username or '',EXCHANGE_USER) or
            not hmac.compare_digest(creds.password or '',EXCHANGE_PASSWORD)):
            return reply('failure\nInvalid credentials',401)
        sid=secrets.token_urlsafe(32)
        SESSIONS[sid]=time.time()+3600
        return reply('success\ngrand1c\n'+sid)
    if not valid_session():
        return reply('failure\nNot authenticated',401)
    if mode=='init':
        return reply('zip=no\nfile_limit='+str(MAX_XML_BYTES))
    if mode=='file':
        filename=request.args.get('filename','')
        if not filename or '/' in filename or '\\' in filename or not filename.lower().endswith('.xml'):
            return reply('failure\nOnly XML filenames allowed',400)
        body=request.get_data()
        if len(body)>MAX_XML_BYTES: return reply('failure\nFile too large',413)
        # 1C may send one file in multiple chunks.
        path=os.path.join(UPLOAD_DIR,filename)
        if os.path.exists(path) and os.path.getsize(path)+len(body)>MAX_XML_BYTES:
            return reply('failure\nFile too large',413)
        with open(path,'ab') as f:f.write(body)
        return reply('success')
    if mode=='import':
        filename=request.args.get('filename','')
        if not filename or '/' in filename or '\\' in filename or not filename.lower().endswith('.xml'):
            return reply('failure\nInvalid filename',400)
        path=os.path.join(UPLOAD_DIR,filename)
        if not os.path.isfile(path):return reply('failure\nFile not found',404)
        try:
            with open(path,'rb') as f: blob=f.read(MAX_XML_BYTES+1)
            if len(blob)>MAX_XML_BYTES: raise ValueError('File too large')
            count=import_xml(blob)
            app.logger.info('1C imported %s entries from %s',count,filename)
            os.remove(path)
            return reply('success')
        except (ET.ParseError,ValueError,sqlite3.Error) as e:
            app.logger.warning('1C import error: %s',e)
            return reply('failure\nImport error: '+str(e),400)
    if mode=='query' and kind=='sale':
        return Response(order_xml(),content_type='application/xml; charset=utf-8')
    if mode=='success':
        # For production, add per-order acknowledgement and sync cursor to avoid re-imports.
        return reply('success')
    return reply('failure\nUnsupported mode',400)

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')))
