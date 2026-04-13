from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
import asyncio
import time
import json
import logging
import os
import html
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ══════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════
MAX_MESSAGE_LENGTH   = 500
MAX_MESSAGES_PER_SEC = 20
MAX_CLIENTS          = 20
PING_INTERVAL        = 30
DATABASE_URL         = os.environ.get("DATABASE_URL")
CACHE_TTL            = 5
MAX_HTTP_REQ_PER_SEC = 10
COMMENT_COOLDOWN_SECS = 60  # 1 comentario por minuto por IP


# ══════════════════════════════
#  POOL DE CONEXIONES
# ══════════════════════════════
db_pool = None

def get_conn():
    conn = db_pool.getconn()
    conn.cursor_factory = RealDictCursor
    return conn

def release_conn(conn):
    db_pool.putconn(conn)

def init_db():
    global db_pool
    db_pool = pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=10,
        dsn=DATABASE_URL,
        cursor_factory=RealDictCursor
    )
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                initials    TEXT NOT NULL,
                text        TEXT NOT NULL,
                page        TEXT NOT NULL DEFAULT 'default',
                likes       INTEGER DEFAULT 0,
                dislikes    INTEGER DEFAULT 0,
                voters      TEXT DEFAULT '[]',
                timestamp   REAL NOT NULL,
                date        TEXT NOT NULL,
                parent_id   TEXT DEFAULT NULL
            )
        """)
        cur.execute("ALTER TABLE comments ADD COLUMN IF NOT EXISTS parent_id TEXT DEFAULT NULL")
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)
    log.info("Base de datos lista ✓")


# ══════════════════════════════
#  CACHÉ
# ══════════════════════════════
cache: dict = {}

def get_cache(key):
    if key in cache:
        data, ts = cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del cache[key]
    return None

def set_cache(key, data):
    cache[key] = (data, time.time())

def invalidate_cache(page):
    cache.pop(f"comments_{page}", None)


# ══════════════════════════════
#  RATE LIMIT HTTP
# ══════════════════════════════
http_requests: dict = defaultdict(list)

def is_http_rate_limited(ip: str) -> bool:
    now = time.time()
    http_requests[ip] = [t for t in http_requests[ip] if now - t < 1.0]
    if len(http_requests[ip]) >= MAX_HTTP_REQ_PER_SEC:
        return True
    http_requests[ip].append(now)
    return False


# ══════════════════════════════
#  LÍMITE DE COMENTARIOS
# ══════════════════════════════
comment_cooldown: dict = {}

def is_comment_limited(ip: str) -> bool:
    now = time.time()
    last = comment_cooldown.get(ip, 0)
    if now - last < COMMENT_COOLDOWN_SECS:
        remaining = int(COMMENT_COOLDOWN_SECS - (now - last))
        return remaining
    comment_cooldown[ip] = now
    return False


# ══════════════════════════════
#  MODELOS
# ══════════════════════════════
class CommentIn(BaseModel):
    name: str
    text: str
    page: str = "default"
    parent_id: str = None

    @validator('name')
    def clean_name(cls, v):
        v = v.strip()[:50]
        if not v:
            raise ValueError('Nombre vacío')
        return html.escape(v)

    @validator('text')
    def clean_text(cls, v):
        v = v.strip()[:500]
        if not v:
            raise ValueError('Texto vacío')
        return html.escape(v)

    @validator('page')
    def clean_page(cls, v):
        return v.strip()[:50]

    @validator('parent_id')
    def clean_parent(cls, v):
        if v:
            return v.strip()[:20]
        return v

class ReactionIn(BaseModel):
    type: str
    device_id: str

    @validator('device_id')
    def clean_device(cls, v):
        return v.strip()[:60]


# ══════════════════════════════
#  ENDPOINTS COMENTARIOS
# ══════════════════════════════

@app.get("/comments")
def get_comments(request: Request, page: str = "default"):
    ip = request.client.host
    if is_http_rate_limited(ip):
        return {"error": "Demasiadas solicitudes, espera un momento"}

    page = page.strip()[:50]
    cache_key = f"comments_{page}"
    cached = get_cache(cache_key)
    if cached is not None:
        return cached

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM comments
            WHERE page = %s AND parent_id IS NULL
            ORDER BY timestamp DESC
        """, (page,))
        parents = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT * FROM comments
            WHERE page = %s AND parent_id IS NOT NULL
            ORDER BY timestamp ASC
        """, (page,))
        replies = [dict(r) for r in cur.fetchall()]
        cur.close()
    finally:
        release_conn(conn)

    reply_map = {}
    for r in replies:
        r["voters"] = json.loads(r["voters"])
        pid = r["parent_id"]
        reply_map.setdefault(pid, []).append(r)

    for p in parents:
        p["voters"] = json.loads(p["voters"])
        p["replies"] = reply_map.get(p["id"], [])

    set_cache(cache_key, parents)
    return parents


@app.post("/comments")
def post_comment(request: Request, body: CommentIn):
    ip = request.client.host

    if is_http_rate_limited(ip):
        return {"error": "Demasiadas solicitudes, espera un momento"}

    # Solo aplicar cooldown a comentarios principales, no a respuestas
    if not body.parent_id:
        remaining = is_comment_limited(ip)
        if remaining:
            log.warning(f"IP {ip} bloqueada por cooldown, faltan {remaining}s")
            return {"error": f"Espera {remaining} segundos antes de comentar de nuevo"}

    new_comment = {
        "id":        str(int(time.time() * 1000)),
        "name":      body.name,
        "initials":  "".join(w[0].upper() for w in body.name.split()[:2]),
        "text":      body.text,
        "page":      body.page,
        "likes":     0,
        "dislikes":  0,
        "voters":    "[]",
        "timestamp": time.time(),
        "date":      time.strftime("%d/%m/%Y %H:%M"),
        "parent_id": body.parent_id,
    }

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO comments (id, name, initials, text, page, likes, dislikes, voters, timestamp, date, parent_id)
            VALUES (%(id)s, %(name)s, %(initials)s, %(text)s, %(page)s, %(likes)s, %(dislikes)s, %(voters)s, %(timestamp)s, %(date)s, %(parent_id)s)
        """, new_comment)
        conn.commit()
        cur.close()
    finally:
        release_conn(conn)

    invalidate_cache(body.page)
    new_comment["voters"] = []
    new_comment["replies"] = []
    tipo = "respuesta" if body.parent_id else "comentario"
    log.info(f"Nuevo {tipo} de '{body.name}' en '{body.page}' desde {ip}")
    return new_comment


@app.post("/comments/{comment_id}/react")
def react_comment(request: Request, comment_id: str, body: ReactionIn):
    ip = request.client.host
    if is_http_rate_limited(ip):
        return {"error": "Demasiadas solicitudes, espera un momento"}

    if body.type not in ("like", "dislike"):
        return {"error": "Tipo inválido"}

    comment_id = comment_id.strip()[:20]

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM comments WHERE id = %s", (comment_id,))
        row = cur.fetchone()

        if not row:
            cur.close()
            return {"error": "Comentario no encontrado"}

        voters = json.loads(row["voters"])
        if body.device_id in voters:
            cur.close()
            return {"error": "Ya votaste en este comentario"}

        voters.append(body.device_id)
        column = "likes" if body.type == "like" else "dislikes"
        cur.execute(
            f"UPDATE comments SET {column} = {column} + 1, voters = %s WHERE id = %s",
            (json.dumps(voters), comment_id)
        )
        conn.commit()

        cur.execute("SELECT likes, dislikes, page FROM comments WHERE id = %s", (comment_id,))
        updated = cur.fetchone()
        cur.close()
    finally:
        release_conn(conn)

    invalidate_cache(updated["page"])
    log.info(f"{body.type} en {comment_id} desde {ip}")
    return {"ok": True, "likes": updated["likes"], "dislikes": updated["dislikes"]}


# ══════════════════════════════
#  CHAT
# ══════════════════════════════
class Client:
    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.ip = websocket.client.host
        self.message_times = []
        self.warned = False

    def is_rate_limited(self) -> bool:
        now = time.time()
        self.message_times = [t for t in self.message_times if now - t < 1.0]
        if len(self.message_times) >= MAX_MESSAGES_PER_SEC:
            return True
        self.message_times.append(now)
        return False


clients: dict[WebSocket, Client] = {}


async def ping_loop():
    while True:
        await asyncio.sleep(PING_INTERVAL)
        muertos = []
        for ws, client in list(clients.items()):
            try:
                await ws.send_text("")
            except Exception:
                log.warning(f"Cliente {client.ip} no respondió al ping.")
                muertos.append(ws)
        for ws in muertos:
            clients.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass


@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(ping_loop())
    log.info("Servidor iniciado ✓")


async def broadcast(data: dict, exclude: WebSocket = None):
    muertos = []
    for ws in list(clients.keys()):
        if ws == exclude:
            continue
        try:
            await ws.send_json(data)
        except Exception:
            muertos.append(ws)
    for ws in muertos:
        clients.pop(ws, None)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if len(clients) >= MAX_CLIENTS:
        await websocket.close(code=1008, reason="Servidor lleno")
        log.warning("Conexión rechazada: servidor lleno")
        return

    await websocket.accept()
    client = Client(websocket)
    clients[websocket] = client
    log.info(f"Cliente conectado: {client.ip} | Total: {len(clients)}")

    try:
        while True:
            data = await websocket.receive_text()

            if len(data) > MAX_MESSAGE_LENGTH:
                await websocket.send_json({
                    "type": "error",
                    "text": f"Mensaje demasiado largo (máx {MAX_MESSAGE_LENGTH} caracteres)"
                })
                continue

            if client.is_rate_limited():
                if not client.warned:
                    await websocket.send_json({
                        "type": "error",
                        "text": "Estás enviando mensajes muy rápido, espera un momento."
                    })
                    client.warned = True
                continue
            else:
                client.warned = False

            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "text": "Mensaje con formato inválido."
                })
                continue

            await broadcast(parsed, exclude=websocket)
            log.info(f"Mensaje de {client.ip} ({parsed.get('name','?')}): {str(parsed.get('text',''))[:60]}")

    except WebSocketDisconnect:
        clients.pop(websocket, None)
        log.info(f"Cliente desconectado: {client.ip} | Total: {len(clients)}")
