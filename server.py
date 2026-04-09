from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import time
import json
import logging

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
#  CONFIGURACIÓN — ajusta aquí
# ══════════════════════════════
MAX_MESSAGE_LENGTH   = 500   # caracteres máximos por mensaje
MAX_MESSAGES_PER_SEC = 20    # mensajes máximos por segundo por cliente
MAX_CLIENTS          = 20    # conexiones simultáneas máximas
PING_INTERVAL        = 30    # segundos entre cada ping


class Client:
    def __init__(self, websocket: WebSocket):
        self.ws = websocket
        self.ip = websocket.client.host
        self.message_times = []  # timestamps de mensajes recientes
        self.warned = False      # ya fue advertido por spam

    def is_rate_limited(self) -> bool:
        """Devuelve True si el cliente mandó demasiados mensajes en el último segundo."""
        now = time.time()
        self.message_times = [t for t in self.message_times if now - t < 1.0]
        if len(self.message_times) >= MAX_MESSAGES_PER_SEC:
            return True
        self.message_times.append(now)
        return False


# Diccionario: websocket -> Client
clients: dict[WebSocket, Client] = {}


async def ping_loop():
    """Manda ping a todos los clientes cada PING_INTERVAL segundos."""
    while True:
        await asyncio.sleep(PING_INTERVAL)
        muertos = []
        for ws, client in list(clients.items()):
            try:
                await ws.send_json({"type": "ping"})
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
    """Envía un mensaje a todos los clientes conectados."""
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
    # Rechazar si hay demasiados clientes
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

            # ── Validar tamaño ──
            if len(data) > MAX_MESSAGE_LENGTH:
                await websocket.send_json({
                    "type": "error",
                    "text": f"Mensaje demasiado largo (máx {MAX_MESSAGE_LENGTH} caracteres)"
                })
                log.warning(f"{client.ip} mandó mensaje de {len(data)} chars (límite: {MAX_MESSAGE_LENGTH})")
                continue

            # ── Validar rate limit ──
            if client.is_rate_limited():
                if not client.warned:
                    await websocket.send_json({
                        "type": "error",
                        "text": "Estás enviando mensajes muy rápido, espera un momento."
                    })
                    client.warned = True
                    log.warning(f"{client.ip} está siendo limitado por rate limit")
                continue
            else:
                client.warned = False

            # ── Validar que sea JSON válido ──
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError:
                await websocket.send_json({
                    "type": "error",
                    "text": "Mensaje con formato inválido."
                })
                continue

            # ── Reenviar a todos los demás ──
            await broadcast(parsed, exclude=websocket)
            log.info(f"Mensaje de {client.ip} ({parsed.get('name','?')}): {str(parsed.get('text',''))[:60]}")

    except WebSocketDisconnect:
        clients.pop(websocket, None)
        log.info(f"Cliente desconectado: {client.ip} | Total: {len(clients)}")
