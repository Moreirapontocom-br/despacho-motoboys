"""
Despacho automático de motoboys: ponto de entrada do sistema.

O Render inicia por aqui (uvicorn api_despacho3:app). Este arquivo só junta as
partes; cada assunto fica no seu próprio arquivo:

    config.py        variáveis de ambiente (chaves, local do restaurante, ajustes)
    horarios.py      horários e fuso (o banco guarda tudo em UTC)
    banco.py         conexão, tabelas e atualização de bancos antigos
    seguranca.py     chave do painel, códigos dos motoboys, limite de tentativas
    contas.py        login com e-mail e senha, sessões, dono x funcionário
    api_contas.py    endereços de login e da equipe
    mapas.py         busca de endereço, distâncias e rotas pelas ruas
    despacho.py      algoritmo: agrupar pedidos, escolher motoboy, ordenar paradas
    api_painel.py    endereços do painel do restaurante (exigem a API_KEY)
    api_motoboy.py   endereços da página do motoboy (exigem o código dele)
    paginas.py       carrega painel.html e motoboy.html
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

import api_contas
import api_motoboy
import api_painel


@asynccontextmanager
async def ciclo_de_vida(app):
    # Laço do despacho automático. Só faz algo quando o modo "automatico" está
    # ligado no painel. Funciona porque o servidor roda numa única cópia (worker).
    api_painel.iniciar_despacho_automatico()
    yield


app = FastAPI(title="API de Despacho Automático", lifespan=ciclo_de_vida)
app.include_router(api_contas.router)
app.include_router(api_painel.router)
app.include_router(api_painel.pagina)
app.include_router(api_motoboy.router)
