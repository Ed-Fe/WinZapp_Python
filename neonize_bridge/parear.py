"""Pareia a conta de teste pela ponte por codigo. Le TELEFONE de
configuracao-chamada-whatsapp.txt do TeleZapp; nao imprime o numero."""
import re, sys, time, requests, socketio
P = "http://127.0.0.1:6311"
cfg = open(r"C:\github\TeleZapp\experimentos\configuracao-chamada-whatsapp.txt", encoding="utf-8").read()
fone = re.search(r"^TELEFONE=(\d+)", cfg, re.M).group(1)
tok = requests.post(f"{P}/api/teste/SECRETA/generate-token").json()["full"]
ev = []
sio = socketio.Client()
for e in ("phoneCode", "phoneCodeError", "session-logged", "status-find", "qrCode"):
    sio.on(e, lambda d, e=e: ev.append((e, d)))
sio.connect(P, socketio_path="socket.io", auth={"token": tok, "session": "teste"},
            headers={"apikey": tok}, transports=["websocket"])
requests.post(f"{P}/api/{tok}/start-session", json={"phone": fone})
for _ in range(8):
    time.sleep(1)
    for e, d in ev:
        if e == "phoneCode": print("CODIGO:", d["data"], flush=True); ev.clear(); break
    st = requests.get(f"{P}/api/{tok}/status-session").json()["status"]
    if st == "CONNECTED": break
print("status final:", st)
print("host-device ok:", bool(requests.get(f"{P}/api/{tok}/host-device").json()["response"]["wid"]["_serialized"]))
