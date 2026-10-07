"""
Algoritmo de despacho: agrupa os pedidos, escolhe o motoboy e ordena as paradas.

As funções daqui não conhecem a web (FastAPI); quem chama é o api_painel.py.
"""

import itertools
import secrets
import threading
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import text

from config import FATOR_RUAS, MAX_PARADAS_VIAGEM, PARADA_MIN, RESTAURANTE, SAIDA_MIN
from horarios import agora_utc, gps_recente, parse_ts
from mapas import custo_reta, haversine, matriz_duracoes

# Impede que despacho, atribuição, edição, cancelamento e remoção de motoboy mexam
# nos mesmos pedidos ao mesmo tempo. Funciona porque o servidor roda numa única
# cópia (um "worker"); com várias cópias, precisaria virar uma trava no banco.
_trava = threading.Lock()


@contextmanager
def trava_despacho(espera_s=15):
    """Use com `with trava_despacho():`. Espera até 15 s; depois disso, avisa o usuário."""
    if not _trava.acquire(timeout=espera_s):
        raise HTTPException(status_code=503, detail="Já existe um despacho em andamento. Tente de novo em alguns segundos.")
    try:
        yield
    finally:
        _trava.release()




def agrupar_pedidos(pedidos, raio_km=2.0, max_por_lote=4):
    pendentes = pedidos.copy()
    lotes = []
    while pendentes:
        ancora = pendentes.pop(0)
        lote = [ancora]
        restantes = []
        for pedido in pendentes:
            perto = haversine(ancora["lat"], ancora["lng"], pedido["lat"], pedido["lng"]) <= raio_km
            if perto and len(lote) < max_por_lote:
                lote.append(pedido)
            else:
                restantes.append(pedido)
        lotes.append(lote)
        pendentes = restantes
    return lotes


def ordenar_rota(origem, pedidos, usar_ruas=True):
    """Melhor ordem das paradas saindo do restaurante (menor tempo total).
    Até 7 paradas testa todas as ordens; acima disso usa o vizinho mais próximo.
    usar_ruas=False usa só a linha reta (rápido, sem consultar serviço externo),
    para as estimativas feitas na escolha do motoboy."""
    if len(pedidos) <= 1:
        return pedidos.copy()
    pontos = [origem] + pedidos
    mat = matriz_duracoes(pontos) if usar_ruas else None

    def custo(i, j):
        if mat is not None and mat[i][j] is not None:
            return mat[i][j]
        return custo_reta(pontos[i], pontos[j])

    n = len(pedidos)
    if n <= 7:
        melhor = min(itertools.permutations(range(1, n + 1)),
                     key=lambda ordem: custo(0, ordem[0]) + sum(custo(ordem[k], ordem[k + 1]) for k in range(n - 1)))
        return [pontos[i] for i in melhor]
    restantes, atual, rota = list(range(1, n + 1)), 0, []
    while restantes:
        prox = min(restantes, key=lambda j: custo(atual, j))
        rota.append(pontos[prox])
        restantes.remove(prox)
        atual = prox
    return rota



# ---------------------------------------------------------------------
# Escolha do motoboy
#
# Todo motoboy precisa passar no restaurante para pegar a comida. Então o que
# importa não é quem está mais perto do cliente, e sim quem consegue terminar
# a entrega mais cedo: tempo até voltar ao restaurante (terminando as paradas
# que ainda tem) + tempo da nova viagem.
#
# Um motoboy que recebeu pedidos mas ainda não saiu do restaurante pode levar
# os novos na mesma viagem; nesse caso a rota inteira é reorganizada. Se ele
# já saiu, os novos ficam para depois que ele voltar (ficam no fim da lista).
# As estimativas usam linha reta (~30 km/h) para ser rápido; a ordem final
# das paradas continua usando as ruas de verdade.
# ---------------------------------------------------------------------
def tempo_viagem(paradas, origem=None, ordenar=True):
    """Segundos para sair de 'origem' (padrão: restaurante), fazer as entregas e voltar ao restaurante."""
    if not paradas:
        return custo_reta(origem, RESTAURANTE) if origem else 0
    origem = origem or RESTAURANTE
    rota = ordenar_rota(origem, list(paradas), usar_ruas=False) if ordenar else list(paradas)
    pontos = [origem] + rota + [RESTAURANTE]
    return sum(custo_reta(pontos[i], pontos[i + 1]) for i in range(len(pontos) - 1)) + len(rota) * PARADA_MIN * 60


def estado_motoboy(m, abertas, ultima_entrega, agora=None):
    """Situação de um motoboy para o despacho.
    m: dict com lat/lng do cadastro e gps_lat/gps_lng/gps_em.
    abertas: paradas ainda não entregues (em ordem), cada uma com lat, lng, despachado_em.
    ultima_entrega: horário da última entrega concluída por ele (ou None).
    Devolve {"saiu": bool, "volta_s": segundos até estar livre no restaurante,
             "proxima": paradas que ainda podem ser juntadas na mesma viagem}."""
    agora = agora or agora_utc()
    gps = gps_recente(m.get("gps_em")) and m.get("gps_lat") is not None
    pos = {"lat": m["gps_lat"], "lng": m["gps_lng"]} if gps else None
    longe = pos is not None and haversine(pos["lat"], pos["lng"], RESTAURANTE["lat"], RESTAURANTE["lng"]) > 0.3

    if not abertas:
        return {"saiu": longe, "volta_s": custo_reta(pos, RESTAURANTE) if longe else 0, "proxima": []}

    # O próprio motoboy avisou na página dele que saiu: não há o que adivinhar.
    saiu_avisado = any(p.get("saiu_em") for p in abertas)
    horarios = [parse_ts(p.get("despachado_em")) for p in abertas]
    desde = None if any(h is None for h in horarios) else min(horarios)
    ultima = parse_ts(ultima_entrega)
    if saiu_avisado:
        saiu = True
    elif gps and not longe:
        # GPS diz que ele está no restaurante. Só conta como "já saiu" se já entregou algo desta viagem.
        saiu = desde is None or (ultima is not None and ultima >= desde)
    else:
        saiu = (longe or desde is None or (ultima is not None and ultima >= desde)
                or (agora - desde).total_seconds() >= SAIDA_MIN * 60)

    if not saiu:
        return {"saiu": False, "volta_s": 0, "proxima": list(abertas)}
    if longe:
        volta = tempo_viagem(abertas, origem=pos, ordenar=False)
    else:
        decorrido = (agora - desde).total_seconds() if desde else float("inf")
        volta = max(tempo_viagem(abertas, ordenar=False) - decorrido, len(abertas) * PARADA_MIN * 60)
    return {"saiu": True, "volta_s": volta, "proxima": []}


def planejar(lotes, estados, max_paradas=MAX_PARADAS_VIAGEM):
    """Distribui os lotes de pedidos novos. estados: {motoboy_id: estado_motoboy(...)}.
    Para cada lote, escolhe o motoboy que termina a entrega dele mais cedo
    (empate: quem tem menos paradas).

    Devolve {motoboy_id: [viagem, viagem, ...]} só para quem recebeu pedidos novos.
    Cada viagem é uma lista de paradas que saem juntas do restaurante. Se o motoboy
    ainda não saiu, a primeira viagem já inclui as paradas que ele tinha."""
    sim = {}
    for mid, e in estados.items():
        sim[mid] = {"volta": e["volta_s"], "viagens": [list(e["proxima"])] if e["proxima"] else [],
                    "total": len(e["proxima"]), "recebeu": False}
    for lote in lotes:
        melhor = None
        for mid, s in sim.items():
            atual = s["viagens"][-1] if s["viagens"] else []
            if len(atual) + len(lote) <= max_paradas:
                fim = s["volta"] + tempo_viagem(atual + lote)
            else:  # viagem atual cheia: este lote vai numa viagem seguinte
                fim = s["volta"] + tempo_viagem(atual) + tempo_viagem(lote)
            chave = (round(fim), s["total"], mid)
            if melhor is None or chave < melhor[0]:
                melhor = (chave, mid)
        s = sim[melhor[1]]
        if s["viagens"] and len(s["viagens"][-1]) + len(lote) <= max_paradas:
            s["viagens"][-1] = s["viagens"][-1] + lote
        else:
            if s["viagens"]:
                s["volta"] += tempo_viagem(s["viagens"][-1])
            s["viagens"].append(list(lote))
        s["total"] += len(lote)
        s["recebeu"] = True
    return {mid: s["viagens"] for mid, s in sim.items() if s["recebeu"]}


def ler_estados(con, ids=None):
    """Lê do banco a situação dos motoboys (todos os de turno, ou os ids pedidos)."""
    if ids is None:
        motoboys = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, lat, lng, gps_lat, gps_lng, gps_em FROM motoboys WHERE ativo"))]
    else:
        motoboys = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, lat, lng, gps_lat, gps_lng, gps_em FROM motoboys WHERE id = :id"), {"id": ids[0]})]
    abertas = {}
    for r in con.execute(text(
            "SELECT id, endereco, lat, lng, motoboy_id, ordem, despachado_em, viagem, saiu_em FROM pedidos "
            "WHERE status = 'despachado' ORDER BY motoboy_id, ordem")):
        abertas.setdefault(r.motoboy_id, []).append(dict(r._mapping))
    ultimas = {r[0]: r[1] for r in con.execute(text(
        "SELECT motoboy_id, MAX(concluido_em) FROM pedidos WHERE status = 'concluido' GROUP BY motoboy_id"))}
    estados = {m["id"]: estado_motoboy(m, abertas.get(m["id"], []), ultimas.get(m["id"])) for m in motoboys}
    return estados, abertas


def montar_rota(viagens):
    """Ordena as paradas de cada viagem. Devolve a lista de viagens, cada uma já
    na ordem de entrega.
    Pode consultar o serviço de rotas (alguns segundos), por isso deve ser chamada
    FORA da transação do banco, para não segurar o banco esperando a internet."""
    return [ordenar_rota(RESTAURANTE, viagem) for viagem in viagens if viagem]


def _nova_viagem():
    return "V" + secrets.token_hex(4).upper()


def gravar_rota(con, motoboy_id, viagens, saiu, abertas):
    """Grava a ordem das paradas (viagens: saída de montar_rota). Se o motoboy ainda
    não saiu, reescreve a rota inteira (as paradas que ele já tinha podem mudar de
    posição). Se já saiu, as paradas dele ficam como estão e as viagens novas entram
    depois delas.

    Cada viagem recebe um identificador (coluna "viagem"), usado nas estatísticas
    para saber quais entregas saíram juntas. A viagem que junta paradas antigas
    continua com o identificador que elas já tinham.

    O UPDATE só mexe em pedidos ainda pendentes ou já deste motoboy em rota. Assim,
    se ele marcar uma parada como entregue enquanto o despacho está calculando,
    ela não volta para a rota. Devolve só as paradas que foram gravadas."""
    ordem = 0 if not saiu else max((p["ordem"] or 0 for p in abertas), default=0)
    gravadas = []
    for viagem in viagens:
        ids_viagem = [p.get("viagem") for p in viagem if p.get("viagem")]
        id_viagem = ids_viagem[0] if ids_viagem else _nova_viagem()
        for p in viagem:
            ordem += 1
            r = con.execute(text(
                "UPDATE pedidos SET status = 'despachado', motoboy_id = :m, ordem = :o, viagem = :v, "
                "despachado_em = COALESCE(despachado_em, CURRENT_TIMESTAMP) WHERE id = :id "
                "AND (status = 'pendente' OR (status = 'despachado' AND motoboy_id = :m))"),
                {"m": motoboy_id, "o": ordem, "v": id_viagem, "id": p["id"]})
            if r.rowcount == 1:
                gravadas.append(p)
    return gravadas


# ---------------------------------------------------------------------
# Quilômetros e economia (estimativas para o painel e as estatísticas)
#
# Usa linha reta vezes FATOR_RUAS: não consulta serviço externo, então é
# instantâneo e funciona mesmo com o serviço de rotas fora do ar.
# ---------------------------------------------------------------------
def km_viagem(paradas):
    """Km estimados de uma saída: restaurante -> paradas (na ordem dada) -> restaurante."""
    if not paradas:
        return 0.0
    pontos = [RESTAURANTE] + list(paradas) + [RESTAURANTE]
    reta = sum(haversine(pontos[i]["lat"], pontos[i]["lng"], pontos[i + 1]["lat"], pontos[i + 1]["lng"])
               for i in range(len(pontos) - 1))
    return reta * FATOR_RUAS


def km_individual(paradas):
    """Km estimados se cada entrega fosse uma viagem separada (ida e volta)."""
    return sum(2 * haversine(RESTAURANTE["lat"], RESTAURANTE["lng"], p["lat"], p["lng"]) for p in paradas) * FATOR_RUAS


# ---------------------------------------------------------------------
# Situação de cada pedido na fila (usada no painel e no despacho automático)
# ---------------------------------------------------------------------
RAIO_AGRUPAMENTO_KM = 2.0


def situacao_fila(pendentes, regras, agora=None):
    """Para cada pedido pendente (dicts com id, lat, lng, criado_em), devolve
    {id: {"vizinhos": [ids a até 2 km], "aguardando": bool, "longe": bool, "km": float}}.

    aguardando: o pedido está sozinho, é recente e ainda vale esperar um parceiro
                de viagem (regra "espera_agrupamento_min").
    longe:      passa do raio máximo do despacho automático (regra "raio_max_km").
    Pedidos esperando há mais de "prioridade_min" nunca ficam aguardando."""
    agora = agora or agora_utc()
    espera = regras.get("espera_agrupamento_min") or 0
    prioridade = regras.get("prioridade_min") or 0
    raio_max = regras.get("raio_max_km") or 0
    resultado = {}
    for p in pendentes:
        vizinhos = [q["id"] for q in pendentes if q["id"] != p["id"]
                    and haversine(p["lat"], p["lng"], q["lat"], q["lng"]) <= RAIO_AGRUPAMENTO_KM]
        criado = parse_ts(p.get("criado_em"))
        minutos = (agora - criado).total_seconds() / 60 if criado else 0
        km = haversine(RESTAURANTE["lat"], RESTAURANTE["lng"], p["lat"], p["lng"])
        resultado[p["id"]] = {
            "vizinhos": vizinhos,
            "aguardando": bool(espera and not vizinhos and minutos < espera and not (prioridade and minutos >= prioridade)),
            "longe": bool(raio_max and km > raio_max),
            "km": round(km, 1),
        }
    return resultado
