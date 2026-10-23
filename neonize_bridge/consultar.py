import json, sys, time, requests
P = "http://127.0.0.1:6311"
tok = requests.post(f"{P}/api/teste/SECRETA/generate-token").json()["full"]
if requests.get(f"{P}/api/{tok}/status-session").json()["status"] != "CONNECTED":
    requests.post(f"{P}/api/{tok}/start-session", json={})
    for _ in range(30):
        time.sleep(1)
        if requests.get(f"{P}/api/{tok}/status-session").json()["status"] == "CONNECTED": break
print("status:", requests.get(f"{P}/api/{tok}/status-session").json()["status"])
chats = requests.post(f"{P}/api/{tok}/list-chats", json={}).json()
print("conversas:", len(chats), "contatos:", len(requests.get(f"{P}/api/{tok}/all-contacts").json()))
for c in chats[:5]:
    print(" ", c["id"]["_serialized"][:6] + "...", c["name"], c["unreadCount"])
    if len(sys.argv) > 1:
        print("   msgs:", len(requests.get(f"{P}/api/{tok}/get-messages/{c['id']['_serialized']}?count=5").json()))
