#!/usr/bin/env python3
"""Monta guia-de-uso.html a partir de template.html + secoes/*.html.

Uso: python gerar_guia.py <pasta-do-repositorio-winzapp> [saida.html]

1. Regenera as tabelas de atalhos do catálogo do repositório informado.
2. Numera e junta as seções (ordem dos nomes de arquivo).
3. Escreve o HTML final com a alpha coberta (cobertura.json).
"""
import json, re, subprocess, sys
from pathlib import Path

AQUI = Path(__file__).parent

def main():
    repo = sys.argv[1]
    saida = Path(sys.argv[2]) if len(sys.argv) > 2 else AQUI / 'guia-de-uso.html'
    gerado = AQUI / 'secoes' / '.atalhos-gerados.html'
    subprocess.run([sys.executable, str(AQUI / 'gerar_atalhos.py'), repo, str(gerado)], check=True)
    cob = json.loads((AQUI / 'cobertura.json').read_text(encoding='utf-8'))
    nav, toc, secoes = [], [], []
    for n, arq in enumerate(sorted((AQUI / 'secoes').glob('[0-9]*.html')), 1):
        texto = arq.read_text(encoding='utf-8')
        m = re.match(r'<!--nav: (.+?)-->\n', texto)
        if not m:
            sys.exit(f'{arq.name}: falta a linha <!--nav: ...-->')
        titulo, corpo = m.group(1).strip(), texto[m.end():]
        corpo = corpo.replace('<!--ATALHOS_GERADOS-->', gerado.read_text(encoding='utf-8'))
        sid = re.sub(r'^\d+-', '', arq.stem)
        sem_emoji = re.sub(r'^\S+\s', '', titulo)
        nav.append(f'    <a href="#{sid}" class="nav-link">{n}. {sem_emoji}</a>')
        toc.append(f'        <li><a href="#{sid}">{sem_emoji}</a></li>')
        secoes.append(f'  <section id="{sid}" class="wiki-section" aria-labelledby="h-{sid}">\n'
                      f'    <h2 id="h-{sid}">{titulo.split(" ", 1)[0]} {n}. {sem_emoji}</h2>\n{corpo}\n  </section>\n')
    html = (AQUI / 'template.html').read_text(encoding='utf-8')
    for k, v in {'NAV': '\n'.join(nav), 'TOC': '\n'.join(toc), 'SECOES': '\n'.join(secoes),
                 'ALPHA': cob['alpha'], 'DATA': cob['data']}.items():
        html = html.replace('{{' + k + '}}', v)
    saida.write_text(html, encoding='utf-8')
    gerado.unlink()
    print(f'{len(secoes)} seções, {len(html)//1024} KB -> {saida} (cobre até a alpha {cob["alpha"]})')

if __name__ == '__main__':
    main()
