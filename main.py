import os, json, sqlite3, urllib.request, urllib.error, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from pathlib import Path
BASE=Path(__file__).parent
DB=BASE/('comprengo.db' if (BASE/'comprengo.db').exists() else 'ofertix.db')
TOKEN=os.getenv('ML_ACCESS_TOKEN','')
ADMIN_KEY=os.getenv('OFERTIX_ADMIN_KEY','')
def connect():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row
 c.execute('CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY,title TEXT NOT NULL, price REAL, image TEXT, category TEXT, ranking INTEGER, affiliate_url TEXT, original_url TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')
 c.commit();return c
def api(path):
 req=urllib.request.Request('https://api.mercadolibre.com'+path,headers={'Authorization':'Bearer '+TOKEN,'Accept':'application/json','User-Agent':'OFERTIX/1.0'})
 with urllib.request.urlopen(req,timeout=18) as res:return json.load(res)
def valid_link(url):
 try:
  p=urlparse(url);return p.scheme=='https' and bool(p.hostname) and (p.hostname=='mercadolivre.com.br' or p.hostname.endswith('.mercadolivre.com.br') or p.hostname=='mercadolibre.com' or p.hostname.endswith('.mercadolibre.com') or p.hostname=='meli.la')
 except:return False
class Handler(BaseHTTPRequestHandler):
 def send(self,status,obj):
  data=json.dumps(obj,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def body(self):
  n=int(self.headers.get('Content-Length',0));return json.loads(self.rfile.read(n)) if 0<n<100000 else {}
 def admin(self):
  if not ADMIN_KEY:self.send(503,{'error':'Defina OFERTIX_ADMIN_KEY no servidor antes de usar funções administrativas.'});return False
  if self.headers.get('X-Admin-Key')!=ADMIN_KEY:self.send(403,{'error':'Chave de administrador inválida.'});return False
  return True
 def do_GET(self):
  p=urlparse(self.path)
  if p.path=='/api/products':
   with connect() as c: data=[dict(r) for r in c.execute('SELECT id,title,price,image,category,ranking,affiliate_url,updated_at FROM products WHERE affiliate_url IS NOT NULL AND affiliate_url<>"" ORDER BY COALESCE(ranking,999),updated_at DESC LIMIT 300')]
   self.send(200,{'products':data});return
  if p.path=='/api/admin/products':
   if not self.admin():return
   with connect() as c:data=[dict(r) for r in c.execute('SELECT * FROM products ORDER BY COALESCE(ranking,999),updated_at DESC LIMIT 500')]
   self.send(200,{'products':data});return
  if p.path=='/' or p.path=='/index.html':
   data=(BASE/'index.html').read_bytes();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
  self.send(404,{'error':'Não encontrado'})
 def do_POST(self):
  if not self.admin():return
  try:
   p=urlparse(self.path);body=self.body()
   if p.path=='/api/admin/link':
    pid=str(body.get('id','')).strip().upper();url=str(body.get('affiliate_url','')).strip()
    if not pid or not valid_link(url):return self.send(400,{'error':'Informe ID e link HTTPS válido do Mercado Livre.'})
    with connect() as c:
     cur=c.execute('UPDATE products SET affiliate_url=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(url,pid))
     if not cur.rowcount:return self.send(404,{'error':'Produto não encontrado'})
    return self.send(200,{'ok':True})
   if p.path=='/api/admin/import':
    if not TOKEN:return self.send(400,{'error':'Configure ML_ACCESS_TOKEN. Conta de afiliado aprovada não equivale a autorização da API.'})
    category=str(body.get('category','')).upper().strip()
    if not category.startswith('MLB') or not category[3:].isdigit():return self.send(400,{'error':'Categoria inválida: use ID MLB numérico, ex.: MLB1051'})
    highlights=api('/highlights/MLB/category/'+category)
    ranked=[x for x in highlights.get('content',[]) if x.get('type')=='ITEM' and x.get('id','').startswith('MLB')]
    # API includes PRODUCT/USER_PRODUCT. Those require a separate catalog resolution step.
    saved=0;fail=0
    with connect() as c:
     for x in ranked[:20]:
      try:
       item=api('/items/'+x['id'])
       title=item.get('title') or x['id'];url=item.get('permalink','');pic=item.get('thumbnail','')
       if not pic and item.get('pictures'):pic=item['pictures'][0].get('secure_url','')
       c.execute('INSERT INTO products (id,title,price,image,category,ranking,original_url) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,price=excluded.price,image=excluded.image,category=excluded.category,ranking=excluded.ranking,original_url=excluded.original_url,updated_at=CURRENT_TIMESTAMP',(x['id'],title,item.get('price'),pic,category,x.get('position',999),url))
       saved+=1
      except Exception:fail+=1
    self.send(200,{'imported':saved,'failed':fail,'not_imported_catalog_products':len(highlights.get('content',[]))-len(ranked),'note':'Links de afiliado anteriores são preservados. Novos produtos ficam ocultos até cadastrar seu link válido. Produtos de catálogo exigem resolução separada.'});return
   self.send(404,{'error':'Rota inexistente'})
  except urllib.error.HTTPError as e:self.send(502,{'error':f'API Mercado Livre retornou HTTP {e.code}. Confira token e acesso ao recurso.'})
  except Exception as e:self.send(500,{'error':str(e)[:250]})
if __name__=='__main__':
 connect().close();port=int(os.getenv('PORT','8080'))
 print('OFERTIX em http://localhost:'+str(port),flush=True)
 if not ADMIN_KEY:print('AVISO: configure OFERTIX_ADMIN_KEY para usar administração.',flush=True)
 ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
