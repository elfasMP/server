from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import time
import json
import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor

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


# ══════════════════════════════
#  BASE DE DATOS
# ══════════════════════════════
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_conn()
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
            date        TEXT NOT NULL
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    log.info("Base de datos lista ✓")


# ══════════════════════════════
#  MODELOS
# ══════════════════════════════
class CommentIn(BaseModel):
    name: str
    text: str
    page: str = "default"

class ReactionIn(BaseModel):
    type: str
    device_id: str


# ══════════════════════════════
#  ENDPOINTS COMENTARIOS
# ══════════════════════════════

@app.get("/comments")
def get_comments(page: str = "default"):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM comments WHERE page = %s ORDER BY timestamp DESC",
        (page,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    # convertir voters de string JSON a lista
    result = []
    for row in rows:
        r = dict(row)
        r["voters"] = json.loads(r["voters"])
        result.append(r)
    return result


@app.post("/comments")
def post_comment(body: CommentIn):
    if not body.name.strip() or not body.text.strip():
        return {"error": "Nombre y texto son obligatorios"}
    if len(body.text) > 500:
        return {"error": "Comentario demasiado largo"}

    new_comment = {
        "id":        str(int(time.time() * 1000)),
        "name":      body.name.strip()[:50],
        "initials":  "".join(w[0].upper() for w in body.name.strip().split()[:2]),
        "text":      body.text.strip(),
        "page":      body.page,
        "likes":     0,
        "dislikes":  0,
        "voters":    "[]",
        "timestamp": time.time(),
        "date":      time.strftime("%d/%m/%Y %H:%M"),
    }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO comments (id, name, initials, text, page, likes, dislikes, voters, timestamp, date)
        VALUES (%(id)s, %(name)s, %(initials)s, %(text)s, %(page)s, %(likes)s, %(dislikes)s, %(voters)s, %(timestamp)s, %(date)s)
    """, new_comment)
    conn.commit()
    cur.close()
    conn.close()

    new_comment["voters"] = []
    log.info(f"Nuevo comentario de '{new_comment['name']}' en '{body.page}'")
    return new_comment


@app.post("/comments/{comment_id}/react")
def react_comment(comment_id: str, body: ReactionIn):
    if body.type not in ("like", "dislike"):
        return {"error": "Tipo inválido"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM comments WHERE id = %s", (comment_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return {"error": "Comentario no encontrado"}

    voters = json.loads(row["voters"])
    if body.device_id in voters:
        cur.close()
        conn.close()
        return {"error": "Ya votaste en este comentario"}

    voters.append(body.device_id)
    column = "likes" if body.type == "like" else "dislikes"

    cur.execute(
        f"UPDATE comments SET {column} = {column} + 1, voters = %s WHERE id = %s",
        (json.dumps(voters), comment_id)
    )
    conn.commit()

    cur.execute("SELECT likes, dislikes FROM comments WHERE id = %s", (comment_id,))
    updated = cur.fetchone()
    cur.close()
    conn.close()

    log.info(f"{body.type} en comentario {comment_id}")
    return {"ok": True, "likes": updated["likes"], "dislikes": updated["dislikes"]}


# ══════════════════════════════
#  CHAT - código original
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
