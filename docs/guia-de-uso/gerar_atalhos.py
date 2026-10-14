#!/usr/bin/env python3
"""Gera as tabelas de atalhos do guia a partir do catálogo do WinZapp.

Uso: python gerar_atalhos.py <pasta-do-repositorio-winzapp> [saida.html]

Lê client/core/shortcut_catalog.py e o PO pt-BR do próprio repositório, então
as tabelas nunca ficam desatualizadas em relação ao código.
"""
import ast, collections, html, re, sys
from pathlib import Path

ALT, CTRL, SHIFT = 1, 2, 4
TECLAS = {32: 'Espaço', 13: 'Enter', 27: 'Esc', 127: 'Delete', 9: 'Tab', 44: ',', 46: '.',
          312: 'End', 313: 'Home', 314: 'Seta para a esquerda', 315: 'Seta para cima',
          316: 'Seta para a direita', 317: 'Seta para baixo', 366: 'Page Up', 367: 'Page Down'}
TECLAS.update({340 + i: f'F{i + 1}' for i in range(12)})

GRUPOS = [  # (escopo, título)
    ('main', 'Janela principal e navegação'),
    ('navigation', 'Listas (em geral)'),
    ('device', 'Dispositivos de áudio'),
    ('chats', 'Lista de conversas'),
    ('chat_selection', 'Seleção de conversas'),
    ('archived', 'Conversas arquivadas'),
    ('locked', 'Conversas trancadas'),
    ('search', 'Pesquisa'),
    ('messages', 'Dentro de uma conversa'),
    ('message_list', 'Lista de mensagens'),
    ('composer', 'Campo de mensagem'),
    ('links', 'Links numa mensagem'),
    ('mention', 'Menções'),
    ('autocomplete', 'Autocompletar'),
    ('media', 'Visualizador de mídia e reprodução'),
    ('emoji', 'Seletor de emojis'),
    ('ai_result', 'Janela de resultado (IA e transcrição)'),
    ('status', 'Status'),
    ('status_list', 'Lista de status'),
    ('status_reply', 'Resposta a status'),
    ('calls', 'Aba Ligações'),
    ('call', 'Durante uma ligação'),
    ('incoming', 'Ligação recebida'),
]

# Textos do próprio programa que descrevem a ação errada; corrigidos aqui.
CORRECOES = {
    'chats.ID_ARCHIVE_LIST': 'Arquivar conversa',
}

def carregar_po(po_path):
    texto = po_path.read_text(encoding='utf-8')
    saida, ctx, mid, mstr, cur = {}, None, None, None, None
    for linha in texto.splitlines() + ['']:
        if linha.startswith('msgctxt '):
            if ctx is not None and mstr is not None:
                saida[ctx] = mstr
            ctx, mstr, cur = ast.literal_eval(linha[8:]), None, 'ctx'
        elif linha.startswith('msgid '):
            cur = 'id'
        elif linha.startswith('msgstr '):
            mstr, cur = ast.literal_eval(linha[7:]), 'str'
        elif linha.startswith('"') and cur == 'str':
            mstr += ast.literal_eval(linha)
    if ctx is not None and mstr is not None:
        saida[ctx] = mstr
    return saida

def nome_tecla(mod, key):
    partes = []
    if mod & CTRL: partes.append('Ctrl')
    if mod & ALT: partes.append('Alt')
    if mod & SHIFT: partes.append('Shift')
    if isinstance(key, int) and (65 <= key <= 90 or 48 <= key <= 57):
        partes.append(chr(key))
    else:
        partes.append(TECLAS.get(key, f'#{key}'))
    return '+'.join(partes)

def juntar_numerados(linhas):
    """Junta famílias numeradas (Ctrl+1..9 etc.) numa linha só."""
    familias, resultado, usados = {}, [], set()
    for i, (tecla, acao) in enumerate(linhas):
        m = re.match(r'^(.*) \((\d)\)$', acao)
        if m and tecla[-1].isdigit():
            familias.setdefault(m.group(1), []).append((i, tecla, int(m.group(2))))
    for base, itens in familias.items():
        if len(itens) < 2:
            continue
        digitos = sorted(int(t[-1]) for _, t, _ in itens)
        prefixo = itens[0][1][:-1]
        faixa = (f'{digitos[0]} a {digitos[-1]}' if digitos == list(range(digitos[0], digitos[-1] + 1))
                 else ', '.join(map(str, digitos)))
        usados |= {i for i, _, _ in itens}
        resultado.append((itens[0][0], prefixo + faixa, base))
    for i, (tecla, acao) in enumerate(linhas):
        if i not in usados:
            resultado.append((i, tecla, acao))
    return [(t, a) for _, t, a in sorted(resultado)]

def main():
    repo = Path(sys.argv[1])
    destino = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('secoes/zz-atalhos-gerados.html')
    sys.path.insert(0, str(repo / 'client'))
    from core.shortcut_catalog import SHORTCUTS
    from core.keyboard_shortcuts import default_binding, action_text
    po = carregar_po(repo / 'translations/pt-BR/LC_MESSAGES/winzapp.po')
    tr = lambda k: po.get(k, k)
    por_escopo = collections.defaultdict(list)
    sem_tecla = []
    for s in SHORTCUTS:
        b = default_binding(s, tr)
        acao = CORRECOES.get(s.id) or action_text(s, tr).rstrip(' :')
        if b is None:
            sem_tecla.append((s, acao)); continue
        por_escopo[s.scope].append((nome_tecla(*b), acao))
    for escopo, linhas in list(por_escopo.items()):
        por_escopo[escopo] = juntar_numerados(linhas)
    out = []
    conhecidos = {g for g, _ in GRUPOS}
    ordem = GRUPOS + [(e, e) for e in por_escopo if e not in conhecidos]
    for escopo, titulo in ordem:
        linhas = por_escopo.get(escopo)
        if not linhas:
            continue
        out.append(f'<h3>{html.escape(titulo)}</h3>\n<table class="field-table">\n<tr><th>Atalho</th><th>Ação</th></tr>')
        vistos = set()
        for tecla, acao in linhas:
            if (tecla, acao) in vistos: continue
            vistos.add((tecla, acao))
            out.append(f'<tr><td class="field-name">{html.escape(tecla)}</td><td>{html.escape(acao[:1].upper() + acao[1:])}</td></tr>')
        out.append('</table>')
    if sem_tecla:
        out.append('<h3>Comandos sem tecla definida</h3>\n<p>Estes comandos existem na aba Atalhos de teclado das Configurações, mas vêm sem tecla. Escolha uma lá se quiser usá-los.</p>\n<table class="field-table">\n<tr><th>Comando</th></tr>')
        vistos = set()
        for s, acao in sem_tecla:
            if acao in vistos: continue
            vistos.add(acao)
            out.append(f'<tr><td>{html.escape(acao[:1].upper() + acao[1:])}</td></tr>')
        out.append('</table>')
    destino.write_text('\n'.join(out), encoding='utf-8')
    print(f'{len(SHORTCUTS)} atalhos no catálogo -> {destino}')

if __name__ == '__main__':
    main()
