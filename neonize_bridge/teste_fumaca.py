"""Fumaca da ponte: sobe, pede token, inicia a sessao e espera o QR."""
import sys, time, threading, requests, socketio
P = "http://127.0.0.1:6311"
r = requests.post(f"{P}/api/teste/SECRETA/generate-token"); print(r.status_code, r.json()["full"][:20])
tok = r.json()["full"]
eventos = []
sio = socketio.Client()
for ev in ("qrCode", "status-find", "session-logged", "phoneCode"):
    sio.on(ev, lambda d, ev=ev: eventos.append((ev, str(d)[:80])))
sio.connect(P, socketio_path="socket.io", auth={"token": tok, "session": "teste"},
            headers={"apikey": tok}, transports=["websocket"])
print("start:", requests.post(f"{P}/api/{tok}/start-session", json={}).json())
for _ in range(30):
    time.sleep(1)
    if any(e[0] == "qrCode" for e in eventos): break
print("status:", requests.get(f"{P}/api/{tok}/status-session").json()["status"])
print("check:", requests.get(f"{P}/api/{tok}/check-connection-session").json())
print("eventos:", eventos)
requests.post(f"{P}/api/{tok}/close-session"); sio.disconnect()
