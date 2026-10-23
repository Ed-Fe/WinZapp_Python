"""Armazenamento da ponte (SQLite): o neonize nao tem lista de conversas nem
leitura de historico sob demanda, entao a ponte guarda o que chega (pacotes de
historico e mensagens ao vivo) e responde list-chats / get-messages daqui."""
import json
import sqlite3
import threading

ESQUEMA = """
CREATE TABLE IF NOT EXISTS chats(
    jid TEXT PRIMARY KEY, name TEXT DEFAULT '', ts INTEGER DEFAULT 0,
    unread INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
    pinned INTEGER DEFAULT 0, muted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS contacts(
    jid TEXT PRIMARY KEY, name TEXT DEFAULT '', pushname TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS msgs(
    chat TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL, data TEXT NOT NULL,
    raw BLOB, PRIMARY KEY(chat, id));
CREATE INDEX IF NOT EXISTS msgs_por_data ON msgs(chat, ts);
"""

CAMPOS_CONVERSA = {"name", "ts", "unread", "archived", "pinned", "muted"}


class Armazem:
    def __init__(self, caminho):
        self._bd = sqlite3.connect(caminho, check_same_thread=False)
        self._bd.row_factory = sqlite3.Row
        self._trava = threading.RLock()
        with self._trava:
            self._bd.executescript(ESQUEMA)
            try:    # bancos criados antes da coluna raw
                self._bd.execute("ALTER TABLE msgs ADD COLUMN raw BLOB")
            except sqlite3.OperationalError:
                pass
            try:    # bancos criados antes desta coluna
                self._bd.execute("ALTER TABLE chats ADD COLUMN read_ts INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    # ---------------------------------------------------------- conversas
    def conversa(self, jid, **campos):
        """Cria a conversa se nao existir e grava so os campos dados."""
        campos = {k: v for k, v in campos.items() if k in CAMPOS_CONVERSA}
        with self._trava:
            self._bd.execute("INSERT OR IGNORE INTO chats(jid) VALUES (?)", (jid,))
            if campos:
                sets = ", ".join(f"{k}=?" for k in campos)
                self._bd.execute(f"UPDATE chats SET {sets} WHERE jid=?",
                                 (*campos.values(), jid))
            self._bd.commit()

    def ao_chegar(self, jid, ts, de_mim):
        """Mensagem nova: sobe a data da conversa e conta nao lida."""
        with self._trava:
            self._bd.execute("INSERT OR IGNORE INTO chats(jid) VALUES (?)", (jid,))
            self._bd.execute(
                "UPDATE chats SET ts=MAX(ts, ?), unread=unread+? WHERE jid=?",
                (ts, 0 if de_mim else 1, jid))
            self._bd.commit()

    def conversas(self):
        with self._trava:
            return [dict(r) for r in self._bd.execute(
                "SELECT * FROM chats ORDER BY ts DESC")]

    # ----------------------------------------------------------- contatos
    def contato(self, jid, name=None, pushname=None):
        with self._trava:
            self._bd.execute("INSERT OR IGNORE INTO contacts(jid) VALUES (?)", (jid,))
            if name:
                self._bd.execute("UPDATE contacts SET name=? WHERE jid=?", (name, jid))
            if pushname:
                self._bd.execute("UPDATE contacts SET pushname=? WHERE jid=?",
                                 (pushname, jid))
            self._bd.commit()

    def contato_de(self, *jids):
        """Nome e apelido de um contato (mescla as formas do JID dadas)."""
        achado = {}
        with self._trava:
            for jid in [j for j in jids if j]:
                r = self._bd.execute("SELECT name, pushname FROM contacts WHERE jid=?", (jid,)).fetchone()
                if r:
                    achado.update({k: r[k] for k in ("name", "pushname") if r[k]})
        return achado

    def contatos(self):
        with self._trava:
            return [dict(r) for r in self._bd.execute("SELECT * FROM contacts")]

    # ---------------------------------------------------------- mensagens
    def mensagem(self, conversa, wpp, raw=None):
        """Guarda (ou troca) uma mensagem no formato do WhatsApp Web; `raw` e a
        ficha da midia (proto serializado) para baixa-la depois."""
        self.mensagens_em_lote([(conversa, wpp, raw)])

    def mensagens_em_lote(self, linhas):
        """Linhas (conversa, wpp) ou (conversa, wpp, raw)."""
        with self._trava:
            self._bd.executemany(
                "INSERT OR REPLACE INTO msgs(chat, id, ts, data, raw) VALUES (?,?,?,?,?)",
                [(l[0], l[1]["id"]["id"], l[1]["t"], json.dumps(l[1]),
                  l[2] if len(l) > 2 else None) for l in linhas])
            self._bd.commit()

    def ficha(self, id_msg):
        """A ficha da midia de uma mensagem (qualquer conversa) ou None."""
        with self._trava:
            r = self._bd.execute("SELECT raw FROM msgs WHERE id=? AND raw IS NOT NULL",
                                 (id_msg,)).fetchone()
        return bytes(r["raw"]) if r else None

    def mensagens(self, conversa, quantidade=20, antes_de=None):
        """As `quantidade` mais recentes (ou as anteriores a `antes_de`, um id
        de mensagem), em ordem cronologica."""
        with self._trava:
            limite = None
            if antes_de:
                r = self._bd.execute("SELECT ts FROM msgs WHERE chat=? AND id=?",
                                     (conversa, antes_de)).fetchone()
                limite = r["ts"] if r else None
            if limite is None:
                linhas = self._bd.execute(
                    "SELECT data FROM msgs WHERE chat=? ORDER BY ts DESC, rowid DESC "
                    "LIMIT ?", (conversa, quantidade)).fetchall()
            else:
                linhas = self._bd.execute(
                    "SELECT data FROM msgs WHERE chat=? AND (ts<? OR (ts=? AND id<?)) "
                    "ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (conversa, limite, limite, antes_de, quantidade)).fetchall()
        return [json.loads(r["data"]) for r in reversed(linhas)]

    def recebidas_a_ler(self, conversa, limite=3000):
        """As ultimas mensagens recebidas que contam como nao lidas: tantas
        quantas o contador da conversa diz (as do historico e as ao vivo).
        Devolve [(id, remetente_wpp, ts)] e zera a conversa."""
        with self._trava:
            r = self._bd.execute("SELECT unread FROM chats WHERE jid=?", (conversa,)).fetchone()
            n = min(int(r["unread"] or 0), limite) if r else 0
            achadas = []
            if n > 0:
                linhas = self._bd.execute(
                    "SELECT data FROM msgs WHERE chat=? ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (conversa, n * 3 + 50)).fetchall()
                for linha in linhas:
                    w = json.loads(linha["data"])
                    if not w.get("fromMe"):
                        achadas.append((w["id"]["id"], w.get("author") or w.get("from") or conversa, w["t"]))
                        if len(achadas) == n:
                            break
                achadas.reverse()
            self._bd.execute("UPDATE chats SET unread=0 WHERE jid=?", (conversa,))
            self._bd.commit()
        return achadas

    def contagens(self):
        with self._trava:
            c = self._bd.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            m = self._bd.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
        return {"chat": c, "message": m}

    def ultima(self, conversa):
        itens = self.mensagens(conversa, 1)
        return itens[0] if itens else None
