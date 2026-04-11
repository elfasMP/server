from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import time
import json
import logging
import os

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
COMMENTS_FILE        = "comments.json"


# ══════════════════════════════
#  COMENTARIOS - helpers JSON
# ══════════════════════════════
def load_comments() -> list:
    if not os.path.exists(COMMENTS_FILE):
        return []
    try:
        with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_comments(comments: list):
    with open(COMMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(comments, f, ensure_ascii=False, indent=2)


# ══════════════════════════════
#  MODELOS
# ══════════════════════════════
class CommentIn(BaseModel):
    name: str
    text: str

class ReactionIn(BaseModel):
    type: str        # "like" o "dislike"
    device_id: str   # id único del dispositivo para limitar 1 voto


# ══════════════════════════════
#  ENDPOINTS COMENTARIOS
# ══════════════════════════════

@app.get("/comments")
def get_comments():
    """Devuelve todos los comentarios ordenados por fecha (más nuevo primero)."""
    comments = load_comments()
    return sorted(comments, key=lambda c: c["timestamp"], reverse=True)


@app.post("/comments")
def post_comment(body: CommentIn):
    """Guarda un nuevo comentario."""
    if not body.name.strip() or not body.text.strip():
        return {"error": "Nombre y texto son obligatorios"}
    if len(body.text) > 500:
        return {"error": "Comentario demasiado largo"}

    comments = load_comments()
    new_comment = {
        "id": str(int(time.time() * 1000)),
        "name": body.name.strip()[:50],
        "initials": "".join(w[0].upper() for w in body.name.strip().split()[:2]),
        "text": body.text.strip(),
        "timestamp": time.time(),
        "date": time.strftime("%d/%m/%Y %H:%M"),
        "likes": 0,
        "dislikes": 0,
        "voters": []   # lista de device_id que ya votaron
    }
    comments.append(new_comment)
    save_comments(comments)
    log.info(f"Nuevo comentario de '{new_comment['name']}'")
    return new_comment


@app.post("/comments/{comment_id}/react")
def react_comment(comment_id: str, body: ReactionIn):
    """Da like o dislike a un comentario. 1 voto por dispositivo."""
    if body.type not in ("like", "dislike"):
        return {"error": "Tipo inválido"}

    comments = load_comments()
    for c in comments:
        if c["id"] == comment_id:
            if body.device_id in c.get("voters", []):
                return {"error": "Ya votaste en este comentario"}
            c[body.type + "s"] += 1
            c.setdefault("voters", []).append(body.device_id)
            save_comments(comments)
            log.info(f"{body.type} en comentario {comment_id} de dispositivo {body.device_id[:8]}")
            return {"ok": True, "likes": c["likes"], "dislikes": c["dislikes"]}

    return {"error": "Comentario no encontrado"}


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
                log.warning(f"Cliente {client.ip} no respondió al ping, desconectando.")
                muertos.append(ws)
        for ws in muertos:
            clients.pop(ws, None)
            try:
                await ws.close()
            except Exception:
                pass


@app.on_event("startup")
async def startup():
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
