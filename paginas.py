"""
Carrega as páginas HTML (painel.html e motoboy.html) e coloca nelas as
configurações que o navegador precisa: endereço das imagens do mapa, crédito
obrigatório, local do restaurante e, no painel, cidade e limites de atraso.
"""

import json
from pathlib import Path

import config

PASTA = Path(__file__).resolve().parent
MARCADOR = "/*__CONFIG__*/"


def carregar(nome, **extra):
    html = (PASTA / nome).read_text(encoding="utf-8")
    if MARCADOR not in html:
        raise RuntimeError(f"{nome} precisa ter {MARCADOR} dentro do primeiro <script>")
    dados = {"mapaUrl": config.MAPA_URL, "mapaCredito": config.MAPA_CREDITO,
             "restaurante": {"lat": config.RESTAURANTE["lat"], "lng": config.RESTAURANTE["lng"]}, **extra}
    # "<\/" evita que um texto com "</script>" feche o script antes da hora.
    js = "const CONFIG = " + json.dumps(dados, ensure_ascii=False).replace("</", "<\\/") + ";"
    return html.replace(MARCADOR, js, 1)
