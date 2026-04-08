from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lista de conexiones activas
clientes = []

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clientes.append(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Reenviar el mensaje a todos los demás
            for cliente in clientes:
                if cliente != websocket:
                    await cliente.send_text(data)
    except WebSocketDisconnect:
        clientes.remove(websocket)