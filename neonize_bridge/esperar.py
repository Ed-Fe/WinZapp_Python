import time, requests
P = "http://127.0.0.1:6311"
tok = requests.post(f"{P}/api/teste/SECRETA/generate-token").json()["full"]
for _ in range(240):
    st = requests.get(f"{P}/api/{tok}/status-session").json()["status"]
    if st == "CONNECTED":
        print("CONECTADO; host-device:", bool(requests.get(f"{P}/api/{tok}/host-device").json()["response"]["wid"]["_serialized"])); break
    time.sleep(1)
else: print("tempo esgotado, status", st)
