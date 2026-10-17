import os, sqlite3, json, urllib.request, hmac, hashlib, time
from flask import Flask, request, jsonify, send_from_directory
from functools import wraps
app=Flask(__name__,static_folder='static')
DB=os.environ.get('DB_PATH','orders.sqlite3')
BOT_TOKEN=os.environ.get('BOT_TOKEN','')
OWNER_ID=os.environ.get('OWNER_CHAT_ID','')
ADMIN_ID=os.environ.get('ADMIN_CHAT_ID','')
ADMIN_KEY=os.environ.get('ADMIN_KEY','')

def db():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;return c

def init():
 with db() as c:
  c.execute('CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,customer TEXT NOT NULL,phone TEXT NOT NULL,address TEXT NOT NULL,distance REAL NOT NULL,items TEXT NOT NULL,subtotal INTEGER NOT NULL,delivery INTEGER NOT NULL,total INTEGER NOT NULL,status TEXT NOT NULL DEFAULT "new")')
init()
PRODUCTS=[{'id':1,'uz':'Банан 1 кг','ru':'Бананы 1 кг','price':19990,'icon':'🍌'},{'id':2,'uz':'Олма 1 кг','ru':'Яблоки 1 кг','price':13990,'icon':'🍎'},{'id':3,'uz':'Картошка 1 кг','ru':'Картофель 1 кг','price':6990,'icon':'🥔'},{'id':4,'uz':'Тухум 10 дона','ru':'Яйца 10 шт','price':13990,'icon':'🥚'},{'id':5,'uz':'Сув 1,5 л','ru':'Вода 1,5 л','price':4990,'icon':'💧'},{'id':6,'uz':'Товуқ филе 1 кг','ru':'Куриное филе 1 кг','price':42990,'icon':'🍗'}]
@app.get('/')
def home():
    return send_from_directory('.', 'index.html')
@app.get('/api/products')
def products():return jsonify(PRODUCTS)
def notify(text):
 if not BOT_TOKEN:return
 for chat in set(x for x in (OWNER_ID,ADMIN_ID) if x):
  try:
   data=json.dumps({'chat_id':chat,'text':text}).encode()
   req=urllib.request.Request('https://api.telegram.org/bot'+BOT_TOKEN+'/sendMessage',data=data,headers={'Content-Type':'application/json'})
   urllib.request.urlopen(req,timeout=8).read()
  except Exception as e:app.logger.warning('Telegram send failed: %s',e)
@app.post('/api/orders')
def order():
 data=request.get_json(silent=True) or {}
 customer=str(data.get('customer','')).strip()[:100];phone=str(data.get('phone','')).strip()[:40];address=str(data.get('address','')).strip()[:300]
 if not customer or not phone or not address:return jsonify(error='Исм, телефон ва манзил керак'),400
 try:distance=float(data.get('distance',0))
 except (ValueError,TypeError):return jsonify(error='Масофа нотўғри'),400
 if not 0<=distance<=50:return jsonify(error='Масофа 0–50 км бўлиши керак'),400
 raw=data.get('items',[])
 if not isinstance(raw,list) or not raw or len(raw)>100:return jsonify(error='Саватча бўш'),400
 byid={p['id']:p for p in PRODUCTS};items=[];subtotal=0
 for x in raw:
  try:pid=int(x['id']);qty=int(x['qty'])
  except (ValueError,TypeError,KeyError):return jsonify(error='Маҳсулот хато'),400
  if pid not in byid or not 1<=qty<=100:return jsonify(error='Маҳсулот ёки миқдор хато'),400
  p=byid[pid];subtotal+=p['price']*qty;items.append({'id':pid,'name':p['uz'],'qty':qty,'price':p['price']})
 delivery=0 if subtotal>=300000 else (10000 if distance<=3 else 15000 if distance<=5 else 20000)
 total=subtotal+delivery
 with db() as c:
  cur=c.execute('INSERT INTO orders(customer,phone,address,distance,items,subtotal,delivery,total) VALUES(?,?,?,?,?,?,?,?)',(customer,phone,address,distance,json.dumps(items,ensure_ascii=False),subtotal,delivery,total));oid=cur.lastrowid
 msg=f'🛒 GRAND MARKET буюртма #{oid}\n👤 {customer}\n📞 {phone}\n📍 {address}\n🚗 {distance} км\n'+ '\n'.join(f"{i['name']} × {i['qty']}" for i in items)+f'\nМаҳсулот: {subtotal:,} сўм\nДоставка: {delivery:,} сўм\nЖами: {total:,} сўм\nТўлов: етказганда (онлайн тўлов ҳали уланмаган)'
 notify(msg)
 return jsonify(id=oid,total=total,delivery=delivery,status='new'),201

def authorized(fn):
 @wraps(fn)
 def inner(*args,**kwargs):
  if not ADMIN_KEY or not hmac.compare_digest(request.headers.get('X-Admin-Key',''),ADMIN_KEY):return jsonify(error='Рухсат йўқ'),401
  return fn(*args,**kwargs)
 return inner
@app.get('/api/admin/orders')
@authorized
def orders():
 with db() as c: rows=c.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 200').fetchall()
 return jsonify([{**dict(r),'items':json.loads(r['items'])} for r in rows])
@app.patch('/api/admin/orders/<int:oid>')
@authorized
def status(oid):
 s=(request.get_json(silent=True) or {}).get('status')
 if s not in ['new','confirmed','packing','on_the_way','delivered','cancelled']:return jsonify(error='Ҳолат хато'),400
 with db() as c:cur=c.execute('UPDATE orders SET status=? WHERE id=?',(s,oid))
 if not cur.rowcount:return jsonify(error='Топилмади'),404
 return jsonify(ok=True)
if __name__=='__main__':app.run(host='127.0.0.1',port=int(os.environ.get('PORT','5000')))
