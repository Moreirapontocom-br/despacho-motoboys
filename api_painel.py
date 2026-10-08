"""
Endereços usados pelo painel do restaurante (e por outros sistemas que mandam
pedidos). Todos exigem login (sessão de e-mail e senha) ou a API_KEY no cabeçalho
X-API-Key, menos a página /painel, que não tem dados: eles só aparecem
depois de entrar.

Cada endereço trabalha só com o restaurante de quem entrou (rid): os pedidos,
motoboys e regras de um restaurante nunca aparecem para outro.
"""

import csv
import io
import secrets
import threading
import time
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

import ajustes
import banco
import contas
import despacho
import paginas
import restaurantes
from config import (AUTOMATICO_INTERVALO_S, PEDIDO_ATRASO_MIN, PRAZO_ENTREGA_MIN, RESTAURANTE_CIDADE, ROTA_ATRASO_MIN,
                    log)
from horarios import agora_utc, fmt_local, gps_recente, inicio_do_dia_utc, para_local, parse_ts
from mapas import buscar_endereco
from seguranca import hash_codigo

# Tudo aqui exige login (e-mail e senha) ou a API_KEY. Algumas ações (regras,
# cadastro de motoboys) também exigem ser dono ou administrador.
router = APIRouter(dependencies=[Depends(contas.exigir_acesso)])
pagina = APIRouter()
Restaurante = Depends(contas.restaurante_atual)


# ---------------------------------------------------------------------
# Formatos de dados aceitos
# ---------------------------------------------------------------------
class DadosPedido(BaseModel):
    """"endereco" é o endereço completo numa linha (usado no Google Maps).
    Os campos separados são opcionais, para quem chama a API do jeito antigo."""
    endereco: str = Field(min_length=1, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    rua: Optional[str] = Field(default=None, max_length=150)
    numero: Optional[str] = Field(default=None, max_length=20)
    bairro: Optional[str] = Field(default=None, max_length=80)
    complemento: Optional[str] = Field(default=None, max_length=120)
    referencia: Optional[str] = Field(default=None, max_length=200)
    cep: Optional[str] = Field(default=None, max_length=12)
    telefone: Optional[str] = Field(default=None, max_length=20)
    cliente: Optional[str] = Field(default=None, max_length=80)


class Pedido(DadosPedido):
    """id é opcional. Sem ele, o servidor cria um. Com ele (ex.: número do pedido
    vindo de outro sistema), mandar o mesmo id duas vezes não duplica o pedido."""
    id: Optional[str] = Field(default=None, min_length=1, max_length=64)


class Motoboy(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    # Mínimo de 6: com 4 dígitos, tentando de vários IPs, dá para adivinhar em poucos dias.
    codigo: str = Field(min_length=6, max_length=64)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    telefone: Optional[str] = Field(default=None, max_length=20)
    tipo: Optional[Literal["proprio", "terceirizado"]] = None


class Despacho(BaseModel):
    """Corpo opcional de /despachar. Sem ele, o sistema decide tudo na hora.
    Com "plano" (vindo de /despachar/previa), grava exatamente a sugestão que o
    funcionário viu e confirmou: {motoboy: [[ids da 1ª viagem], [ids da 2ª]...]}."""
    plano: Optional[Dict[str, List[List[str]]]] = None


class Regras(BaseModel):
    modo: Optional[Literal["manual", "assistido", "automatico"]] = None
    max_paradas: Optional[int] = Field(default=None, ge=1, le=10)
    raio_max_km: Optional[float] = Field(default=None, ge=0, le=50)
    espera_agrupamento_min: Optional[float] = Field(default=None, ge=0, le=30)
    prioridade_min: Optional[float] = Field(default=None, ge=1, le=120)
    raio_agrupamento_km: Optional[float] = Field(default=None, ge=0.3, le=10)
    custo_km: Optional[float] = Field(default=None, ge=0, le=20)


class Turno(BaseModel):
    ativo: bool


class Atribuicao(BaseModel):
    motoboy_id: str = Field(min_length=1)


class Consulta(BaseModel):
    """Busca de endereço. "endereco" é o texto livre. Se "rua" vier preenchida,
    tenta primeiro a busca por campos separados, que costuma acertar mais."""
    endereco: str = Field(min_length=3, max_length=300)
    rua: Optional[str] = Field(default=None, max_length=150)
    numero: Optional[str] = Field(default=None, max_length=20)
    cidade: Optional[str] = Field(default=None, max_length=80)
    estado: Optional[str] = Field(default=None, max_length=40)
    cep: Optional[str] = Field(default=None, max_length=12)


def rotulo(num, pedido_id):
    """Como o pedido aparece para as pessoas: "#12". Pedidos antigos, sem número, usam o id."""
    return f"#{num}" if num else pedido_id


def _limpar_dados(con, rid, dados):
    """Campos vazios viram NULL. Se o bairro já foi usado antes com outra grafia de
    maiúsculas/minúsculas ("amazonas" x "Amazonas"), usa a grafia já salva."""
    for c in banco.CAMPOS_ENDERECO + ("telefone", "cliente"):
        dados[c] = (dados.get(c) or "").strip() or None
    if dados["bairro"]:
        existente = con.execute(text(
            "SELECT bairro FROM pedidos WHERE restaurante_id = :r AND LOWER(bairro) = LOWER(:b) LIMIT 1"),
            {"r": rid, "b": dados["bairro"]}).scalar()
        if existente and existente != dados["bairro"]:
            # Corrige também no endereço de uma linha (formato "Rua, Nº - Bairro, Cidade").
            dados["endereco"] = dados["endereco"].replace(f" - {dados['bairro']},", f" - {existente},", 1)
            dados["bairro"] = existente
    return dados


# ---------------------------------------------------------------------
# Pedidos
# ---------------------------------------------------------------------
_trava_numero = threading.Lock()


@router.post("/pedidos")
def criar_pedido(pedido: Pedido, rid=Restaurante):
    """Registra um pedido novo.
    - Sem id: o servidor gera um id único (usado pelo painel).
    - Com id: se ele já existir, o pedido é ignorado (evita duplicar em reenvios)."""
    sql = text(
        "INSERT INTO pedidos (id, restaurante_id, num, endereco, lat, lng, rua, numero, bairro, complemento, referencia, cep, "
        "telefone, cliente) VALUES (:id, :rid, :num, :endereco, :lat, :lng, :rua, :numero, :bairro, :complemento, :referencia, "
        ":cep, :telefone, :cliente) ON CONFLICT (id) DO NOTHING")
    # A trava garante que dois pedidos criados no mesmo instante não recebam o mesmo número do dia.
    with _trava_numero, banco.engine.begin() as con:
        dados = _limpar_dados(con, rid, pedido.model_dump())
        dados["rid"] = rid
        dados["num"] = (con.execute(text("SELECT COALESCE(MAX(num), 0) FROM pedidos WHERE restaurante_id = :r "
                                         "AND criado_em >= :inicio"), {"r": rid, "inicio": inicio_do_dia_utc()}).scalar() or 0) + 1
        if dados["id"]:
            criado = con.execute(sql, dados).rowcount == 1
        else:
            criado = False
            for _ in range(10):  # colisão é raríssima (16 milhões de combinações), mas tentamos de novo
                dados["id"] = "P" + secrets.token_hex(3).upper()
                if con.execute(sql, dados).rowcount == 1:
                    criado = True
                    break
            if not criado:
                raise HTTPException(status_code=503, detail="Não consegui gerar um número para o pedido. Tente de novo.")
        if not criado:  # já existia: devolve o número que ele recebeu quando foi criado
            existente = con.execute(text("SELECT num FROM pedidos WHERE id = :id AND restaurante_id = :r"),
                                    {"id": dados["id"], "r": rid}).first()
            if existente is None:
                raise HTTPException(status_code=409, detail="Esse id de pedido já é usado por outro restaurante. Use outro id.")
            dados["num"] = existente[0]
        total = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE restaurante_id = :r AND status = 'pendente'"),
                            {"r": rid}).scalar()
    return {"mensagem": "Pedido recebido" if criado else "Pedido já existia (ignorado)",
            "id": dados["id"], "num": dados["num"], "rotulo": rotulo(dados["num"], dados["id"]),
            "criado": criado, "total_pendentes": total}


@router.put("/pedidos/{pedido_id}")
def editar_pedido(pedido_id: str, pedido: DadosPedido, rid=Restaurante):
    """Corrige endereço, ponto no mapa ou telefone de um pedido que ainda está na fila.
    Pedido já despachado não pode ser editado: cancele e crie de novo, para a rota
    do motoboy não mudar sem ele perceber."""
    with despacho.trava_despacho(rid), banco.engine.begin() as con:
        dados = _limpar_dados(con, rid, pedido.model_dump())
        dados.update(id=pedido_id, rid=rid)
        r = con.execute(text(
            "UPDATE pedidos SET endereco = :endereco, lat = :lat, lng = :lng, rua = :rua, numero = :numero, "
            "bairro = :bairro, complemento = :complemento, referencia = :referencia, cep = :cep, telefone = :telefone, cliente = :cliente "
            "WHERE id = :id AND restaurante_id = :rid AND status = 'pendente'"), dados)
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="Esse pedido não está mais na fila (já foi despachado, entregue ou cancelado).")
    return {"mensagem": "Pedido atualizado"}


@router.post("/pedidos/{pedido_id}/cancelar")
def cancelar_pedido(pedido_id: str, rid=Restaurante):
    """Cancela um pedido na fila ou já em rota. Em rota, ele some da tela do motoboy
    na próxima atualização (5 s) e aparece um aviso de cancelamento para ele."""
    with despacho.trava_despacho(rid), banco.engine.begin() as con:
        atual = con.execute(text("SELECT status, motoboy_id, num FROM pedidos WHERE id = :id AND restaurante_id = :r"),
                            {"id": pedido_id, "r": rid}).first()
        if atual is None or atual[0] not in ("pendente", "despachado"):
            raise HTTPException(status_code=409, detail="Esse pedido não pode mais ser cancelado (já foi entregue ou cancelado).")
        # Pedido que estava só na fila não tem motoboy: garante motoboy_id vazio para não gerar aviso.
        con.execute(text("UPDATE pedidos SET status = 'cancelado', cancelado_em = CURRENT_TIMESTAMP, "
                         "motoboy_id = CASE WHEN status = 'despachado' THEN motoboy_id END WHERE id = :id"),
                    {"id": pedido_id})
    aviso = (f" {atual[1]} vai ver um aviso de cancelamento na tela dele. Se ele já estiver com a comida, "
             "vale ligar também." if atual[0] == "despachado" else "")
    return {"mensagem": f"Pedido {rotulo(atual[2], pedido_id)} cancelado.{aviso}"}


@router.get("/pedidos")
def listar_pedidos(rid=Restaurante):
    """Pedidos pendentes e em rota (os concluídos não aparecem).

    Para cada pedido:
      situacao   "ok", "atencao" (passou de 70% do limite) ou "atrasado" (passou de
                 PEDIDO_ATRASO_MIN na fila ou de ROTA_ATRASO_MIN em rota)
      minutos    há quanto tempo está na situação atual
    Só na fila:
      aguardando  esperando um parceiro de viagem (regra de agrupamento)
      agrupar_com rótulos dos pedidos a até 2 km, que podem ir na mesma viagem
      longe       passa do raio máximo do despacho automático"""
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, num, endereco, rua, numero, bairro, complemento, referencia, cep, telefone, cliente, lat, lng, "
            "status, motoboy_id, ordem, criado_em, despachado_em, saiu_em FROM pedidos "
            "WHERE restaurante_id = :r AND status IN ('pendente', 'despachado') "
            "ORDER BY status, motoboy_id, ordem, criado_em"), {"r": rid})]
        regras = ajustes.ler(con, rid)
        rest = restaurantes.ler(con, rid)
    agora = agora_utc()
    pendentes = [p for p in linhas if p["status"] == "pendente"]
    fila = despacho.situacao_fila(rest, pendentes, regras, agora)
    rotulos = {p["id"]: rotulo(p["num"], p["id"]) for p in linhas}
    for p in linhas:
        p["rotulo"] = rotulos[p["id"]]
        criado = parse_ts(p.pop("criado_em"))
        despachado = parse_ts(p.pop("despachado_em"))
        p["saiu"] = bool(p.pop("saiu_em"))
        p["hora"] = para_local(criado).strftime("%H:%M") if criado else ""
        if p["status"] == "pendente":
            desde, limite = criado, PEDIDO_ATRASO_MIN
            f = fila[p["id"]]
            p["aguardando"] = f["aguardando"]
            p["agrupar_com"] = [rotulos[v] for v in f["vizinhos"]]
            p["longe"] = f["longe"]
            p["km"] = f["km"]
        else:
            desde, limite = despachado or criado, ROTA_ATRASO_MIN  # pedidos antigos não têm despachado_em
        minutos = (agora - desde).total_seconds() / 60 if desde is not None else None
        p["minutos"] = int(minutos) if minutos is not None else None
        p["atrasado"] = bool(minutos is not None and minutos >= limite)
        p["situacao"] = ("atrasado" if p["atrasado"] else
                         "atencao" if minutos is not None and minutos >= 0.7 * limite else "ok")
    return {"pedidos": linhas, "restaurante_nome": rest["nome"], "modo": regras["modo"],
            "automatico": dict(_ultimo_automatico.get(rid, {"quando": None, "texto": ""}))}


@router.get("/bairros")
def listar_bairros(rid=Restaurante):
    """Bairros já usados em pedidos, para sugerir no painel enquanto digita."""
    with banco.engine.connect() as con:
        nomes = [r[0] for r in con.execute(text(
            "SELECT DISTINCT bairro FROM pedidos WHERE restaurante_id = :r AND bairro IS NOT NULL AND bairro <> '' "
            "ORDER BY bairro"), {"r": rid})]
    return {"bairros": nomes}


@router.post("/geocodificar")
def geocodificar(c: Consulta, rid=Restaurante):
    with banco.engine.connect() as con:
        centro = restaurantes.ponto(con, rid)
    try:
        resultados = []
        if c.rua and c.rua.strip():
            numero = (c.numero or "").strip()
            rua = c.rua.strip()
            if numero and numero.upper() not in ("S/N", "SN"):
                rua = f"{numero} {rua}"
            try:
                resultados = buscar_endereco(campos={
                    "street": rua, "city": (c.cidade or "").strip(), "state": (c.estado or "").strip(),
                    "postalcode": (c.cep or "").strip(), "country": "Brasil"}, centro=centro)
            except Exception as e:
                log.warning("Falha na busca por campos: %r", e)
        # Completa com a busca em texto livre (que inclui o bairro), sem repetir pontos.
        vistos = {(round(x["lat"], 5), round(x["lng"], 5)) for x in resultados}
        for x in buscar_endereco(c.endereco, centro=centro):
            if (round(x["lat"], 5), round(x["lng"], 5)) not in vistos:
                resultados.append(x)
        return {"resultados": resultados[:6], "centro": centro}
    except Exception as e:
        log.warning("Falha na busca de endereço: %r", e)
        return {"resultados": [], "centro": centro,
                "aviso": "O serviço de endereços não respondeu agora. Clique no mapa no ponto certo do cliente, ou tente buscar de novo."}


# ---------------------------------------------------------------------
# Despacho
# ---------------------------------------------------------------------
def _ler_pendentes(con, rid):
    return [dict(r._mapping) for r in con.execute(text(
        "SELECT id, num, endereco, lat, lng, criado_em FROM pedidos WHERE restaurante_id = :r AND status = 'pendente' "
        "ORDER BY criado_em, id"), {"r": rid})]


def _planejar(base, pedidos, estados, regras):
    """Plano do despacho: {motoboy: [viagem, viagem...]}, cada viagem uma lista de paradas
    (inclui as paradas antigas de quem ainda não saiu, que vão na mesma viagem)."""
    lotes = despacho.agrupar_pedidos(pedidos, raio_km=regras.get("raio_agrupamento_km") or despacho.RAIO_AGRUPAMENTO_KM,
                                     max_por_lote=min(4, regras["max_paradas"]))
    return despacho.planejar(base, lotes, estados, regras["max_paradas"])


def _plano_confirmado(plano_ids, pedidos, estados, abertas):
    """Remonta o plano que o funcionário viu na sugestão, com a situação de AGORA:
    pedidos que já não estão na fila e paradas já entregues saem; motoboy que
    saiu do restaurante nesse meio-tempo não reorganiza mais as paradas antigas."""
    por_id = {p["id"]: p for p in pedidos}
    plano = {}
    for mid, viagens_ids in plano_ids.items():
        if mid not in estados:
            continue  # saiu de turno ou foi removido: os pedidos dele continuam na fila
        estado = estados[mid]
        antigas = {p["id"]: p for p in abertas.get(mid, [])}
        viagens = []
        for ids in viagens_ids:
            viagem = [por_id[i] for i in ids if i in por_id]
            if not estado["saiu"]:
                viagem = [antigas[i] for i in ids if i in antigas] + viagem
            if viagem:
                viagens.append(viagem)
        if not viagens:
            continue
        # Ainda está no restaurante mas a sugestão não juntava as paradas dele: junta agora.
        faltando = [p for p in estado["proxima"] if all(p["id"] != q["id"] for q in viagens[0])]
        viagens[0] = faltando + viagens[0]
        plano[mid] = viagens
    return plano


def _gravar_plano(base, plano, estados, abertas, novos_ids):
    """Ordena as paradas (fora do banco: pode consultar o serviço de rotas) e grava.
    gravar_rota não desfaz uma entrega marcada pelo motoboy enquanto isso."""
    rotas = {mid: despacho.montar_rota(base, viagens) for mid, viagens in plano.items()}
    resultado = {}
    with banco.engine.begin() as con:
        for motoboy_id, viagens in rotas.items():
            gravadas = despacho.gravar_rota(con, motoboy_id, viagens, estados[motoboy_id]["saiu"],
                                            abertas.get(motoboy_id, []))
            novas = [{"id": p["id"], "num": p.get("num"), "endereco": p["endereco"], "lat": p["lat"], "lng": p["lng"]}
                     for p in gravadas if p["id"] in novos_ids]
            if novas:
                resultado[motoboy_id] = novas
    return resultado


def _despachar_sem_trava(rid, somente=None, plano_ids=None):
    """Despacha os pedidos pendentes (ou só os ids em `somente`). Quem chama já
    está dentro de despacho.trava_despacho(rid)."""
    # 1. Lê a situação atual (transação curta, só leitura).
    with banco.engine.connect() as con:
        pedidos = _ler_pendentes(con, rid)
        if somente is not None:
            pedidos = [p for p in pedidos if p["id"] in somente]
        if not pedidos:
            return {"mensagem": "Não há pedidos pendentes para despachar."}
        base = restaurantes.ponto(con, rid)
        estados, abertas = despacho.ler_estados(con, rid, base)
        if not estados:
            return {"mensagem": "Nenhum motoboy de turno agora. Ligue o turno de alguém no painel."}
        regras = ajustes.ler(con, rid)

    # 2. Planeja (ou usa o plano confirmado) e 3. grava.
    if plano_ids is not None:
        plano = _plano_confirmado(plano_ids, pedidos, estados, abertas)
        if not plano:
            return {"mensagem": "A sugestão ficou desatualizada (pedidos ou motoboys mudaram). Veja a sugestão de novo."}
    else:
        plano = _planejar(base, pedidos, estados, regras)
    return {"rotas": _gravar_plano(base, plano, estados, abertas, {p["id"] for p in pedidos})}


@router.post("/despachar")
def despachar(corpo: Optional[Despacho] = None, rid=Restaurante):
    """Agrupa os pedidos pendentes, monta as rotas e distribui entre os motoboys.
    Cada grupo vai para quem consegue entregá-lo mais cedo (veja despacho.planejar).
    Se o motoboy escolhido ainda não saiu do restaurante, os novos pedidos entram
    na mesma viagem e a rota inteira é reorganizada; se já saiu, entram no fim.
    Com corpo {"plano": ...} grava a sugestão confirmada no modo assistido."""
    with despacho.trava_despacho(rid):
        return _despachar_sem_trava(rid, plano_ids=corpo.plano if corpo else None)


def _motivos(base, mid, viagens, estados, abertas, novos_ids, plano):
    """Por que o sistema escolheu este motoboy, em frases curtas, e em quantos
    minutos ele termina tudo e volta ao restaurante (estimativa em linha reta)."""
    est, antigas = estados[mid], abertas.get(mid, [])
    fim = est["volta_s"] + sum(despacho.tempo_viagem(base, v) for v in viagens)
    novos = [p for v in viagens for p in v if p["id"] in novos_ids]
    motivos = []
    if not antigas and not est["saiu"]:
        motivos.append("Está livre no restaurante")
    elif not antigas:
        motivos.append(f"Está voltando ao restaurante (~{max(1, round(est['volta_s'] / 60))} min)")
    elif not est["saiu"]:
        motivos.append(f"Ainda está no restaurante com {len(antigas)} entrega(s): leva tudo na mesma saída")
    else:
        motivos.append(f"Está na rua; volta em ~{max(1, round(est['volta_s'] / 60))} min e leva estes na próxima saída")
    if len(novos) > 1:
        motivos.append(f"Leva {len(novos)} pedidos juntos, perto um do outro")
    # Compara com o melhor outro motoboy levando os mesmos pedidos. Quem já recebeu
    # outros pedidos nesta mesma sugestão só poderia levar estes depois deles.
    alternativas = []
    for outro, e in estados.items():
        if outro == mid:
            continue
        if outro in plano:
            fim_outro = e["volta_s"] + sum(despacho.tempo_viagem(base, v) for v in plano[outro])
            alternativas.append((fim_outro + despacho.tempo_viagem(base, novos), outro, True))
        else:
            alternativas.append((e["volta_s"] + despacho.tempo_viagem(base, list(e["proxima"]) + novos), outro, False))
    if alternativas:
        alt, quem, ocupado = min(alternativas)
        diferenca = round((alt - fim) / 60)
        if diferenca >= 1:
            motivos.append(f"Termina ~{diferenca} min antes que {quem}" +
                           (" (que já leva outros pedidos desta rodada)" if ocupado else ""))
        else:
            motivos.append(f"Empata com {quem}; escolhido por ter menos entregas no momento")
    return {"minutos": max(1, round(fim / 60)), "motivos": motivos}


@router.post("/despachar/previa")
def previa_do_despacho(rid=Restaurante):
    """Mostra o que o despacho faria agora, sem gravar nada: quem leva o quê, em
    quantas viagens, e os quilômetros economizados por juntar entregas."""
    with banco.engine.connect() as con:
        pedidos = _ler_pendentes(con, rid)
        if not pedidos:
            return {"mensagem": "Não há pedidos pendentes para despachar."}
        base = restaurantes.ponto(con, rid)
        estados, abertas = despacho.ler_estados(con, rid, base)
        if not estados:
            return {"mensagem": "Nenhum motoboy de turno agora. Ligue o turno de alguém no painel."}
        regras = ajustes.ler(con, rid)
    plano = _planejar(base, pedidos, estados, regras)
    novos = {p["id"] for p in pedidos}
    sugestoes, km_com, viagens_novas = [], 0.0, 0
    for mid, viagens in plano.items():
        saida = []
        for viagem in viagens:
            # Ordem estimada pela linha reta (rápido); ao confirmar, a ordem usa as ruas.
            ordem = despacho.ordenar_rota(base, viagem, usar_ruas=False)
            antigas = [p for p in ordem if p["id"] not in novos]
            if len(antigas) == len(ordem):
                continue
            # Só conta o que a viagem acrescenta: as paradas antigas já iam acontecer de qualquer jeito.
            extra = despacho.km_viagem(base, ordem) - (
                despacho.km_viagem(base, despacho.ordenar_rota(base, antigas, usar_ruas=False)) if antigas else 0)
            km_com += extra
            viagens_novas += 1
            saida.append({"km": round(extra, 1), "pedidos": [
                {"id": p["id"], "rotulo": rotulo(p.get("num"), p["id"]), "endereco": p["endereco"], "novo": p["id"] in novos}
                for p in ordem]})
        if saida:
            sugestoes.append({"motoboy": mid, "saiu": estados[mid]["saiu"], "viagens": saida,
                              "novos": sum(1 for v in saida for p in v["pedidos"] if p["novo"]),
                              "km": round(sum(v["km"] for v in saida), 1),
                              **_motivos(base, mid, viagens, estados, abertas, novos, plano)})
    km_sem = despacho.km_individual(base, pedidos)
    economia = max(km_sem - km_com, 0)
    # Nenhuma viagem junta 2 pedidos novos: cada um vai sozinho, para quem entrega mais rápido.
    agrupou = any(sum(1 for p in v["pedidos"] if p["novo"]) > 1 for sg in sugestoes for v in sg["viagens"])
    return {"sugestoes": sugestoes, "pendentes": len(pedidos), "viagens": viagens_novas, "agrupou": agrupou,
            "km_com": round(km_com, 1), "km_sem": round(km_sem, 1), "economia_km": round(economia, 1),
            "economia_pct": round(100 * economia / km_sem) if km_sem else 0,
            "economia_reais": round(economia * regras["custo_km"], 2) if regras["custo_km"] else None,
            "plano": {mid: [[p["id"] for p in v] for v in viagens] for mid, viagens in plano.items()},
            "motoboys": sorted(estados)}


# ---------------------------------------------------------------------
# Despacho automático (modo "automatico" nos ajustes de cada restaurante)
#
# Um laço confere as filas a cada AUTOMATICO_INTERVALO_S segundos e despacha os
# pedidos prontos: os que não estão esperando parceiro de viagem e não passam
# do raio máximo. Os que passam do raio ficam para o funcionário decidir.
# ---------------------------------------------------------------------
_ultimo_automatico = {}  # restaurante -> {"quando", "texto"}


def _automatico_no_restaurante(rid):
    with banco.engine.connect() as con:
        regras = ajustes.ler(con, rid)
        if regras["modo"] != "automatico":
            return None
        pedidos = _ler_pendentes(con, rid)
        base = restaurantes.ponto(con, rid)
    fila = despacho.situacao_fila(base, pedidos, regras)
    prontos = {p["id"] for p in pedidos if not fila[p["id"]]["aguardando"] and not fila[p["id"]]["longe"]}
    if not prontos:
        return None
    try:
        with despacho.trava_despacho(rid, espera_s=1):
            r = _despachar_sem_trava(rid, somente=prontos)
    except HTTPException:
        return None  # alguém está despachando agora; tenta na próxima rodada
    if r.get("rotas"):
        partes = [m + ": " + ", ".join(rotulo(p.get("num"), p["id"]) for p in ps) for m, ps in r["rotas"].items()]
        _ultimo_automatico[rid] = {"quando": para_local(agora_utc()).strftime("%H:%M"), "texto": " | ".join(partes)}
    return r


def despacho_automatico_uma_vez():
    """Uma rodada do despacho automático em todos os restaurantes.
    Devolve {restaurante: resultado} dos que despacharam algo, ou None se nenhum fez nada."""
    with banco.engine.connect() as con:
        ids = restaurantes.ids(con)
    feitos = {}
    for rid in ids:
        try:
            r = _automatico_no_restaurante(rid)
        except Exception:
            log.exception("Falha no despacho automático do restaurante %s", rid)
            continue
        if r is not None:
            feitos[rid] = r
    return feitos or None


def _laco_automatico():
    while True:
        time.sleep(AUTOMATICO_INTERVALO_S)
        try:
            despacho_automatico_uma_vez()
        except Exception:
            log.exception("Falha no despacho automático")


def iniciar_despacho_automatico():
    """Chamado uma vez quando o servidor sobe (api_despacho3.py)."""
    threading.Thread(target=_laco_automatico, name="despacho-automatico", daemon=True).start()


@router.get("/ajustes")
def ver_ajustes(rid=Restaurante):
    with banco.engine.connect() as con:
        return {"ajustes": ajustes.ler(con, rid), "limites": ajustes.LIMITES}


@router.put("/ajustes")
def salvar_ajustes(r: Regras, _=Depends(contas.exigir_gestor), rid=Restaurante):
    with banco.engine.begin() as con:
        novos = ajustes.salvar(con, rid, r.model_dump(exclude_none=True))
    return {"mensagem": "Ajustes salvos", "ajustes": novos}


@router.post("/pedidos/{pedido_id}/atribuir")
def atribuir_manual(pedido_id: str, corpo: Atribuicao, rid=Restaurante):
    """Manda um pedido pendente direto para um motoboy escolhido, pulando o
    agrupamento automático. Se o motoboy ainda não saiu, a parada entra na melhor
    posição da rota dele; se já saiu, fica para depois que ele voltar. Funciona
    mesmo com o motoboy fora de turno, já que é uma escolha manual."""
    with despacho.trava_despacho(rid):
        with banco.engine.connect() as con:
            pedido = con.execute(text("SELECT id, endereco, lat, lng FROM pedidos WHERE id = :id AND restaurante_id = :r "
                                      "AND status = 'pendente'"), {"id": pedido_id, "r": rid}).first()
            if pedido is None:
                raise HTTPException(status_code=404, detail="Pedido não encontrado ou já não está mais pendente")
            base = restaurantes.ponto(con, rid)
            estados, abertas = despacho.ler_estados(con, rid, base, ids=[corpo.motoboy_id])
            if corpo.motoboy_id not in estados:
                raise HTTPException(status_code=404, detail="Motoboy não encontrado")

        estado = estados[corpo.motoboy_id]
        novo = {"id": pedido[0], "endereco": pedido[1], "lat": pedido[2], "lng": pedido[3]}
        # Escolha manual: sem limite de paradas por viagem, junta tudo se ele ainda não saiu.
        viagens = despacho.montar_rota(base, [estado["proxima"] + [novo]])
        with banco.engine.begin() as con:
            rota = despacho.gravar_rota(con, corpo.motoboy_id, viagens, estado["saiu"], abertas.get(corpo.motoboy_id, []))
        if not any(p["id"] == pedido_id for p in rota):
            raise HTTPException(status_code=409, detail="O pedido mudou enquanto era atribuído. Atualize a tela e tente de novo.")
        aviso = " Ele já saiu: a entrega fica para depois que ele voltar ao restaurante." if estado["saiu"] else ""
        return {"mensagem": "Pedido atribuído." + aviso, "rota": rota}


# ---------------------------------------------------------------------
# Motoboys
# ---------------------------------------------------------------------
@router.post("/motoboys")
def cadastrar_motoboy(motoboy: Motoboy, _=Depends(contas.exigir_gestor), rid=Restaurante):
    """Cadastra o motoboy ou atualiza a localização/código dele se já existir.
    O nome é o link dele (/m/NOME), então não pode repetir entre restaurantes."""
    dados = motoboy.model_dump()
    dados["codigo"] = hash_codigo(dados["codigo"])  # o banco guarda só o hash
    dados["rid"] = rid
    with banco.engine.begin() as con:
        dados["telefone"] = (dados.get("telefone") or "").strip() or None
        # O WHERE impede que um restaurante troque o código de um motoboy de outro.
        r = con.execute(text(
            "INSERT INTO motoboys (id, restaurante_id, codigo, lat, lng, telefone, tipo) "
            "VALUES (:id, :rid, :codigo, :lat, :lng, :telefone, :tipo) "
            "ON CONFLICT (id) DO UPDATE SET codigo = excluded.codigo, lat = excluded.lat, lng = excluded.lng, "
            "telefone = excluded.telefone, tipo = excluded.tipo WHERE motoboys.restaurante_id = excluded.restaurante_id"),
            dados)
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="Já existe um motoboy com esse nome em outro restaurante. "
                                                        "Use outro nome (ex.: com o sobrenome).")
        total = con.execute(text("SELECT COUNT(*) FROM motoboys WHERE restaurante_id = :r"), {"r": rid}).scalar()
    return {"mensagem": "Motoboy disponível", "total_disponiveis": total}


@router.get("/motoboys")
def listar_motoboys(rid=Restaurante):
    """Lista os motoboys (sem os códigos), quantas paradas cada um tem em aberto e,
    se o GPS dele estiver ativo, a última posição (para o mapa da operação)."""
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT m.id, m.ativo, m.gps_em, m.gps_lat, m.gps_lng, m.telefone, m.tipo, "
            "(SELECT COUNT(*) FROM pedidos p WHERE p.motoboy_id = m.id AND p.status = 'despachado') AS paradas "
            "FROM motoboys m WHERE m.restaurante_id = :r ORDER BY m.id"), {"r": rid})]
        base = restaurantes.ponto(con, rid)
        estados, abertas = despacho.ler_estados(con, rid, base, todos=True)
    agora = agora_utc()
    for m in linhas:
        gps_em = parse_ts(m["gps_em"])
        m["gps_ativo"] = gps_recente(m.pop("gps_em")) and m["gps_lat"] is not None
        # Há quantos segundos chegou a última posição (o painel mostra "atualizado há 20 s").
        m["gps_segundos"] = max(0, int((agora - gps_em).total_seconds())) if m["gps_ativo"] and gps_em else None
        if not m["gps_ativo"]:
            m["gps_lat"] = m["gps_lng"] = None
        # Resumo para o mapa: situação, km e minutos estimados até terminar e voltar.
        est, paradas = estados.get(m["id"]), abertas.get(m["id"], [])
        m["situacao"] = ("em_rota" if est and est["saiu"] and paradas else
                         "saindo" if paradas else "voltando" if est and est["saiu"] else "disponivel")
        m["km_rota"] = round(despacho.km_viagem(base, paradas), 1) if paradas else 0
        m["minutos_livre"] = max(1, round(est["volta_s"] / 60)) if est and est["volta_s"] else 0
        if paradas and est and not est["saiu"]:
            m["minutos_livre"] = max(1, round(despacho.tempo_viagem(base, paradas, ordenar=False) / 60))
    return {"motoboys": linhas, "centro": base}


@router.delete("/motoboys/{motoboy_id}")
def remover_motoboy(motoboy_id: str, _=Depends(contas.exigir_gestor), rid=Restaurante):
    """Remove o motoboy. As paradas que ele ainda não entregou voltam para a fila.
    Usa a trava do despacho: senão, um despacho rodando ao mesmo tempo podia
    entregar pedidos novos a um motoboy que acabou de ser removido."""
    with despacho.trava_despacho(rid), banco.engine.begin() as con:
        r = con.execute(text("DELETE FROM motoboys WHERE id = :id AND restaurante_id = :r"), {"id": motoboy_id, "r": rid})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Motoboy não encontrado")
        devolvidos = con.execute(text(
            "UPDATE pedidos SET status = 'pendente', motoboy_id = NULL, ordem = NULL, despachado_em = NULL, "
            "viagem = NULL, saiu_em = NULL "
            "WHERE motoboy_id = :id AND status = 'despachado'"), {"id": motoboy_id}).rowcount
    return {"mensagem": "Motoboy removido", "pedidos_devolvidos": devolvidos}


@router.post("/motoboys/{motoboy_id}/turno")
def definir_turno(motoboy_id: str, t: Turno, rid=Restaurante):
    """Liga ou desliga o motoboy. Desligar não mexe nas paradas que ele já tem."""
    with banco.engine.begin() as con:
        r = con.execute(text("UPDATE motoboys SET ativo = :a WHERE id = :id AND restaurante_id = :r"),
                        {"a": t.ativo, "id": motoboy_id, "r": rid})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Motoboy não encontrado")
    return {"mensagem": "Turno atualizado"}


# ---------------------------------------------------------------------
# Números e histórico
# ---------------------------------------------------------------------
@router.get("/status")
def status(rid=Restaurante):
    with banco.engine.connect() as con:
        por_status = {s: n for s, n in con.execute(text(
            "SELECT status, COUNT(*) FROM pedidos WHERE restaurante_id = :r GROUP BY status"), {"r": rid})}
        motoboys = con.execute(text("SELECT COUNT(*) FROM motoboys WHERE restaurante_id = :r"), {"r": rid}).scalar()
    return {"pedidos_por_status": por_status, "motoboys_cadastrados": motoboys}


@router.get("/resumo")
def resumo(rid=Restaurante):
    """Números do dia: pendentes, em rota, entregues hoje e o tempo médio entre o
    pedido ser criado e ser entregue. "Hoje" usa o horário do restaurante."""
    p = {"r": rid}
    with banco.engine.connect() as con:
        pendentes = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE restaurante_id = :r AND status = 'pendente'"), p).scalar()
        em_rota = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE restaurante_id = :r AND status = 'despachado'"), p).scalar()
        linhas = con.execute(text(
            "SELECT criado_em, concluido_em FROM pedidos WHERE restaurante_id = :r AND status = 'concluido' "
            "AND concluido_em >= :inicio"), {**p, "inicio": inicio_do_dia_utc()}).all()
    tempos = [(parse_ts(c) - parse_ts(criado)).total_seconds() / 60
              for criado, c in linhas if parse_ts(criado) is not None]
    return {"pendentes": pendentes, "em_rota": em_rota, "entregues_hoje": len(linhas),
            "tempo_medio_min": round(sum(tempos) / len(tempos), 1) if tempos else None}


def _numeros_do_periodo(base, pedidos):
    """Números de um conjunto de entregas concluídas (veja /estatisticas)."""
    viagens = {}
    for p in pedidos:
        viagens.setdefault(p["viagem"] or ("sozinho:" + p["id"]), []).append(p)
    km = sum(despacho.km_viagem(base, sorted(v, key=lambda x: x["ordem"] or 0)) for v in viagens.values())
    km_sem = despacho.km_individual(base, pedidos)
    tempos = [(parse_ts(p["concluido_em"]) - parse_ts(p["criado_em"])).total_seconds() / 60
              for p in pedidos if parse_ts(p["criado_em"]) and parse_ts(p["concluido_em"])]
    em_rota = [(parse_ts(p["concluido_em"]) - parse_ts(p["despachado_em"])).total_seconds() / 60
               for p in pedidos if parse_ts(p["despachado_em"]) and parse_ts(p["concluido_em"])]
    n = len(pedidos)
    return {
        "entregas": n,
        "viagens": len(viagens),
        "agrupados": sum(len(v) for v in viagens.values() if len(v) > 1),
        "km": round(km, 1),
        "km_economizados": round(max(km_sem - km, 0), 1),
        "km_por_entrega": round(km / n, 2) if n else None,
        "tempo_medio_min": round(sum(tempos) / len(tempos), 1) if tempos else None,
        "tempo_em_rota_min": round(sum(em_rota) / len(em_rota), 1) if em_rota else None,
        "no_prazo_pct": round(100 * sum(1 for t in tempos if t <= PRAZO_ENTREGA_MIN) / len(tempos)) if tempos else None,
    }


def _variacao(atual, anterior):
    """Diferença em % (negativa = diminuiu). None quando não dá para comparar."""
    if atual is None or not anterior:
        return None
    return round(100 * (atual - anterior) / anterior)


@router.get("/estatisticas")
def estatisticas(dias: int = 1, rid=Restaurante):
    """Números das entregas concluídas no período (1 = hoje, 7, 30...), por motoboy e
    comparados com o período anterior de mesmo tamanho.

    Os quilômetros são estimados (linha reta x FATOR_RUAS). "km_economizados" é a
    diferença para o caso de cada entrega ter sido uma viagem separada."""
    dias = max(1, min(dias, 365))
    inicio = inicio_do_dia_utc(dias_atras=dias - 1)
    inicio_anterior = inicio_do_dia_utc(dias_atras=2 * dias - 1)
    with banco.engine.connect() as con:
        custo_km = ajustes.ler(con, rid)["custo_km"]
        base = restaurantes.ponto(con, rid)
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, motoboy_id, lat, lng, viagem, ordem, criado_em, despachado_em, concluido_em FROM pedidos "
            "WHERE restaurante_id = :r AND status = 'concluido' AND concluido_em >= :ini"), {"r": rid, "ini": inicio_anterior})]
    atual = [p for p in linhas if parse_ts(p["concluido_em"]) >= inicio]
    anterior = [p for p in linhas if parse_ts(p["concluido_em"]) < inicio]

    geral = _numeros_do_periodo(base, atual)
    geral["motoboys"] = len({p["motoboy_id"] for p in atual if p["motoboy_id"]})
    # Quanto menos se rodou, comparado com cada entrega ser uma viagem separada.
    sem = geral["km"] + geral["km_economizados"]
    geral["economia_pct"] = round(100 * geral["km_economizados"] / sem) if sem else None
    geral["economia_reais"] = round(geral["km_economizados"] * custo_km, 2) if custo_km else None
    antes = _numeros_do_periodo(base, anterior)
    comparacao = {k: _variacao(geral[k], antes[k]) for k in ("km_por_entrega", "tempo_medio_min", "entregas")}
    if geral["entregas"] and antes["entregas"]:
        # Viagens por entrega: quanto menor, mais entregas saíram juntas.
        comparacao["viagens_por_entrega"] = _variacao(geral["viagens"] / geral["entregas"],
                                                      antes["viagens"] / antes["entregas"])

    por_motoboy = {}
    for p in atual:
        por_motoboy.setdefault(p["motoboy_id"] or "?", []).append(p)
    motoboys = [{"motoboy": m, **_numeros_do_periodo(base, ps)} for m, ps in por_motoboy.items()]
    motoboys.sort(key=lambda x: -x["entregas"])
    return {"dias": dias, "geral": geral, "anterior": antes, "comparacao": comparacao, "motoboys": motoboys,
            "prazo_min": PRAZO_ENTREGA_MIN}


@router.get("/historico.csv")
def historico_csv(dias: int = 30, rid=Restaurante):
    """Baixa em CSV as entregas concluídas dos últimos N dias (30 por padrão, no
    máximo 365), para abrir no Excel ou guardar."""
    dias = max(1, min(dias, 365))
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, num, cliente, endereco, bairro, complemento, referencia, telefone, motoboy_id, criado_em, concluido_em "
            "FROM pedidos WHERE restaurante_id = :r AND status = 'concluido' AND concluido_em >= :inicio ORDER BY concluido_em"),
            {"r": rid, "inicio": inicio_do_dia_utc(dias_atras=dias - 1)})]

    saida = io.StringIO()
    saida.write("﻿")  # marca de ordem de bytes: sem isso o Excel no Windows pode exibir acentos errados
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow(["Pedido", "Nº do dia", "Cliente", "Endereço", "Bairro", "Complemento", "Referência", "Telefone",
                       "Motoboy", "Criado em", "Entregue em", "Tempo (min)"])
    for p in linhas:
        criado_t, concluido_t = parse_ts(p["criado_em"]), parse_ts(p["concluido_em"])
        tempo = round((concluido_t - criado_t).total_seconds() / 60, 1) if criado_t else ""
        escritor.writerow([p["id"], p["num"] or "", p["cliente"] or "", p["endereco"], p["bairro"] or "", p["complemento"] or "",
                           p["referencia"] or "", p["telefone"] or "", p["motoboy_id"] or "",
                           fmt_local(criado_t), fmt_local(concluido_t), tempo])

    nome_arquivo = f"historico_{para_local(agora_utc()).strftime('%Y-%m-%d')}.csv"
    return Response(content=saida.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'})


# ---------------------------------------------------------------------
# Página do painel
# ---------------------------------------------------------------------
_PAINEL_HTML = paginas.carregar("painel.html", cidade=RESTAURANTE_CIDADE,
                                pedidoAtrasoMin=int(PEDIDO_ATRASO_MIN), rotaAtrasoMin=int(ROTA_ATRASO_MIN))


@pagina.get("/painel")
def pagina_do_painel():
    """Página do restaurante. Não contém dados: eles só aparecem depois de entrar."""
    return HTMLResponse(_PAINEL_HTML)
