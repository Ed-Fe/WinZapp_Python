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
CREATE TABLE IF NOT EXISTS fixadas(
    msg TEXT PRIMARY KEY, chat TEXT NOT NULL, ts INTEGER DEFAULT 0, duracao INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS votos(
    msg TEXT NOT NULL, quem TEXT NOT NULL, ids TEXT NOT NULL, ts INTEGER DEFAULT 0,
    PRIMARY KEY(msg, quem));
CREATE TABLE IF NOT EXISTS reacoes(
    msg TEXT NOT NULL, quem TEXT NOT NULL, emoji TEXT NOT NULL, ts INTEGER DEFAULT 0,
    PRIMARY KEY(msg, quem));
CREATE TABLE IF NOT EXISTS recibos(
    msg TEXT NOT NULL, quem TEXT NOT NULL, etapa TEXT NOT NULL, ts INTEGER DEFAULT 0,
    PRIMARY KEY(msg, quem, etapa));
"""

# Mensagens que a ponte fabrica a partir de eventos (mensagens_de_sistema.py):
# nao sao de ninguem a ler nem a marcar como lidas.
TIPOS_DE_SISTEMA = ("gp2", "call_log")

CAMPOS_CONVERSA = {"name", "ts", "unread", "archived", "pinned", "muted", "locked"}


#: etapa de recibo -> ack do WhatsApp Web
ACK_DA_ETAPA = {"delivered": 2, "read": 3, "played": 4}


def ack_dos_recibos(recibos):
    """O ack mais alto entre os recibos [(quem, etapa, ts)]; 1 (enviada) se
    nenhum chegou."""
    return max([1] + [ACK_DA_ETAPA.get(etapa, 1) for _, etapa, _ in recibos])


class Armazem:
    def __init__(self, caminho):
        self._bd = sqlite3.connect(caminho, check_same_thread=False)
        self._bd.row_factory = sqlite3.Row
        self._trava = threading.RLock()
        with self._trava:
            self._bd.executescript(ESQUEMA)
            try:    # bancos criados antes da coluna locked
                self._bd.execute("ALTER TABLE chats ADD COLUMN locked INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
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

    def copiar_estado(self, origem, destino):
        """Poe em `destino` o arquivada/fixada/silenciada de `origem`: a mesma
        conversa guardada sob dois enderecos (numero e LID) muda junta."""
        with self._trava:
            r = self._bd.execute(
                "SELECT archived, pinned, muted FROM chats WHERE jid=?", (origem,)).fetchone()
            if r is not None:
                self._bd.execute(
                    "UPDATE chats SET archived=?, pinned=?, muted=? WHERE jid=?",
                    (r["archived"], r["pinned"], r["muted"], destino))
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

    def mudar_mensagem(self, id_msg, **campos):
        """Muda campos do JSON de uma mensagem guardada (estrela, tipo...)."""
        with self._trava:
            r = self._bd.execute("SELECT chat, data FROM msgs WHERE id=?", (id_msg,)).fetchone()
            if r is None:
                return False
            dados = json.loads(r["data"])
            dados.update(campos)
            self._bd.execute("UPDATE msgs SET data=? WHERE id=?", (json.dumps(dados), id_msg))
            self._bd.commit()
        return True

    def aplicar_revogacoes(self):
        """Poe como apagada, no lugar, a mensagem original de cada aviso de
        'apagar para todos' guardado (ate agora so o aviso era guardado, e a
        mensagem voltava a aparecer quando o app recarregava a conversa)."""
        with self._trava:
            alvos = [json.loads(r["data"]).get("protocolMessageKey", {}).get("id")
                     for r in self._bd.execute(
                         "SELECT data FROM msgs WHERE json_extract(data,'$.type')='revoked'")]
        feitas = 0
        for alvo in [a for a in alvos if a]:
            if self.mudar_mensagem(alvo, type="revoked", body=""):
                feitas += 1
        return feitas

    def reagir(self, id_msg, quem, emoji, ts=0):
        """Guarda a reacao de `quem` a uma mensagem; emoji vazio tira a reacao."""
        with self._trava:
            if emoji:
                self._bd.execute(
                    "INSERT OR REPLACE INTO reacoes(msg, quem, emoji, ts) VALUES (?,?,?,?)",
                    (id_msg, quem, emoji, ts))
            else:
                self._bd.execute("DELETE FROM reacoes WHERE msg=? AND quem=?", (id_msg, quem))
            self._bd.commit()

    def fixar_mensagem(self, id_msg, chat, fixar, ts=0, duracao=0):
        """Fixa (ou solta) uma mensagem na conversa. `duracao` em segundos; 0 =
        sem fim conhecido."""
        with self._trava:
            if fixar:
                self._bd.execute(
                    "INSERT OR REPLACE INTO fixadas(msg, chat, ts, duracao) VALUES (?,?,?,?)",
                    (id_msg, chat, ts, duracao))
            else:
                self._bd.execute("DELETE FROM fixadas WHERE msg=?", (id_msg,))
            self._bd.commit()

    def fixadas_de(self, chats, agora):
        """Mensagens (formato WhatsApp Web) fixadas nas conversas dadas, sem as
        que ja passaram do prazo."""
        achadas = []
        with self._trava:
            linhas = [r for chat in chats for r in self._bd.execute(
                "SELECT msg, ts, duracao FROM fixadas WHERE chat=? ORDER BY ts", (chat,))]
        for r in linhas:
            if r["duracao"] and r["ts"] and r["ts"] + r["duracao"] <= agora:
                continue
            w = self.mensagem_wpp(r["msg"])
            if w is not None:
                achadas.append(w)
        return achadas

    def votar(self, id_msg, quem, indices, ts=0):
        """Guarda o voto de `quem` numa enquete (posicoes das opcoes); lista vazia
        tira o voto."""
        with self._trava:
            if indices:
                self._bd.execute(
                    "INSERT OR REPLACE INTO votos(msg, quem, ids, ts) VALUES (?,?,?,?)",
                    (id_msg, quem, json.dumps(list(indices)), ts))
            else:
                self._bd.execute("DELETE FROM votos WHERE msg=? AND quem=?", (id_msg, quem))
            self._bd.commit()

    def votos_de(self, id_msg):
        """[(quem, [posicoes])] dos votos de uma enquete."""
        with self._trava:
            return [(r["quem"], json.loads(r["ids"])) for r in self._bd.execute(
                "SELECT quem, ids FROM votos WHERE msg=? ORDER BY ts, rowid", (id_msg,))]

    def reacoes_de(self, id_msg):
        """[(quem, emoji)] das reacoes a uma mensagem, da mais antiga a mais nova."""
        with self._trava:
            return [(r["quem"], r["emoji"]) for r in self._bd.execute(
                "SELECT quem, emoji FROM reacoes WHERE msg=? ORDER BY ts, rowid", (id_msg,))]

    def apagar_mensagem(self, id_msg):
        with self._trava:
            self._bd.execute("DELETE FROM msgs WHERE id=?", (id_msg,))
            self._bd.commit()

    def mensagem_wpp(self, id_msg):
        """A mensagem (formato WhatsApp Web) de um id, em qualquer conversa, ou None."""
        with self._trava:
            r = self._bd.execute("SELECT data FROM msgs WHERE id=?", (id_msg,)).fetchone()
        return json.loads(r["data"]) if r else None

    def ficha(self, id_msg):
        """A ficha da midia de uma mensagem (qualquer conversa) ou None."""
        with self._trava:
            r = self._bd.execute("SELECT raw FROM msgs WHERE id=? AND raw IS NOT NULL",
                                 (id_msg,)).fetchone()
        return bytes(r["raw"]) if r else None

    def mensagens(self, conversa, quantidade=20, antes_de=None):
        """As `quantidade` mais recentes (ou as anteriores a `antes_de`, um id
        de mensagem), em ordem cronologica. `conversa` e uma chave ou varias
        (os dois enderecos, numero e LID, da mesma conversa). Uma ancora
        desconhecida devolve [] e nunca as mais novas: o cliente leria como
        pagina anterior o que na verdade e o fim da conversa."""
        chats = (conversa,) if isinstance(conversa, str) else tuple(conversa)
        marcas = ",".join("?" * len(chats))
        with self._trava:
            limite = None
            if antes_de:
                r = self._bd.execute(
                    f"SELECT ts FROM msgs WHERE chat IN ({marcas}) AND id=?",
                    (*chats, antes_de)).fetchone()
                if r is None:       # a ancora pode estar sob outro endereco da conversa
                    r = self._bd.execute("SELECT ts FROM msgs WHERE id=? LIMIT 1",
                                         (antes_de,)).fetchone()
                if r is None:
                    return []
                limite = r["ts"]
            if limite is None:
                linhas = self._bd.execute(
                    f"SELECT data FROM msgs WHERE chat IN ({marcas}) "
                    "ORDER BY ts DESC, rowid DESC LIMIT ?", (*chats, quantidade)).fetchall()
            else:
                linhas = self._bd.execute(
                    f"SELECT data FROM msgs WHERE chat IN ({marcas}) "
                    "AND (ts<? OR (ts=? AND id<?)) ORDER BY ts DESC, rowid DESC LIMIT ?",
                    (*chats, limite, limite, antes_de, quantidade)).fetchall()
        return [json.loads(r["data"]) for r in reversed(linhas)]

    def para_resposta(self, mensagens):
        """Copias das mensagens no formato que get-messages e message-by-id
        devolvem: `star` sempre booleano (sem ele o cliente nunca reconcilia as
        estrelas locais) e, nas minhas, `ack` (1 enviada, 2 entregue, 3 lida,
        4 ouvida) vindo dos recibos guardados, no minimo 1: o cliente monta os
        tiques so do `ack` e a sincronizacao troca o registro local pelo lido."""
        saida = []
        for w in mensagens:
            w = {**w, "star": bool(w.get("star"))}
            if w.get("fromMe"):
                w["ack"] = ack_dos_recibos(self.recibos_de(w["id"]["id"]))
            saida.append(w)
        return saida

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
                    if not w.get("fromMe") and w.get("type") not in TIPOS_DE_SISTEMA:
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

    def contagem_de_estados(self):
        """(arquivadas, fixadas): so para ver se uma copia do estado mudou algo."""
        with self._trava:
            a = self._bd.execute("SELECT COUNT(*) FROM chats WHERE archived=1").fetchone()[0]
            f = self._bd.execute("SELECT COUNT(*) FROM chats WHERE pinned>0").fetchone()[0]
        return a, f

    def existe_conversa(self, jid):
        with self._trava:
            return self._bd.execute("SELECT 1 FROM chats WHERE jid=?", (jid,)).fetchone() is not None

    def ultima_recebida(self, conversa):
        """O id da mensagem recebida mais recente da conversa, ou ''."""
        for w in reversed(self.mensagens(conversa, 60)):
            if not w.get("fromMe") and w.get("type") not in TIPOS_DE_SISTEMA:
                return w["id"]["id"]
        return ""

    def nao_lidas(self, conversa):
        with self._trava:
            r = self._bd.execute("SELECT unread FROM chats WHERE jid=?", (conversa,)).fetchone()
        return int(r["unread"] or 0) if r else 0

    def ultima_bruta(self, conversa):
        """A mensagem mais recente guardada (qualquer autor), ou None."""
        itens = self.mensagens(conversa, 1)
        return itens[0] if itens else None

    def ultima(self, conversa):
        itens = self.mensagens(conversa, 1)
        return itens[0] if itens else None

    # ------------------------------------------- limpar / apagar conversa
    def limpar_conversa(self, jid, manter_estrelas=True):
        """Apaga as mensagens guardadas da conversa (so as sem estrela, se
        `manter_estrelas`) e zera o contador de nao lidas. Devolve quantas
        mensagens sairam."""
        with self._trava:
            filtro = " AND COALESCE(json_extract(data,'$.star'),0)=0" if manter_estrelas else ""
            apagadas = self._bd.execute(
                "DELETE FROM msgs WHERE chat=?" + filtro, (jid,)).rowcount
            self._bd.execute(
                "DELETE FROM fixadas WHERE chat=? AND msg NOT IN (SELECT id FROM msgs WHERE chat=?)",
                (jid, jid))
            self._bd.execute("UPDATE chats SET unread=0 WHERE jid=?", (jid,))
            self._bd.commit()
        return apagadas

    def apagar_conversa(self, jid):
        """Tira a conversa da lista, com todas as mensagens (estreladas tambem)."""
        with self._trava:
            self.limpar_conversa(jid, manter_estrelas=False)
            self._bd.execute("DELETE FROM chats WHERE jid=?", (jid,))
            self._bd.commit()

    # ----------------------------------------------------------- recibos
    def recibo(self, id_msg, quem, etapa, ts=0):
        """Guarda que `quem` chegou a `etapa` ("delivered", "read", "played")
        de uma mensagem nossa. Fica a primeira hora em que a etapa foi vista."""
        with self._trava:
            self._bd.execute(
                "INSERT OR IGNORE INTO recibos(msg, quem, etapa, ts) VALUES (?,?,?,?)",
                (id_msg, quem, etapa, ts))
            self._bd.commit()

    def recibos_de(self, id_msg):
        """[(quem, etapa, ts)] dos recibos guardados de uma mensagem."""
        with self._trava:
            return [(r["quem"], r["etapa"], r["ts"]) for r in self._bd.execute(
                "SELECT quem, etapa, ts FROM recibos WHERE msg=? ORDER BY rowid", (id_msg,))]
