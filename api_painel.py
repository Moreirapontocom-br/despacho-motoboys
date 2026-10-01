"""
Endereços usados pelo painel do restaurante (e por outros sistemas que mandam
pedidos). Todos exigem a API_KEY no cabeçalho X-API-Key, menos a página /painel,
que não tem dados: eles só aparecem depois de digitar a chave.
"""

import csv
import io
import secrets
import threading
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

import banco
import despacho
import paginas
from config import PEDIDO_ATRASO_MIN, RESTAURANTE, RESTAURANTE_CIDADE, RESTAURANTE_NOME, ROTA_ATRASO_MIN, log
from horarios import agora_utc, fmt_local, gps_recente, inicio_do_dia_utc, para_local, parse_ts
from mapas import buscar_endereco
from seguranca import exigir_chave, hash_codigo

router = APIRouter(dependencies=[Depends(exigir_chave)])
pagina = APIRouter()


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


def _limpar_dados(con, dados):
    """Campos vazios viram NULL. Se o bairro já foi usado antes com outra grafia de
    maiúsculas/minúsculas ("amazonas" x "Amazonas"), usa a grafia já salva."""
    for c in banco.CAMPOS_ENDERECO + ("telefone",):
        dados[c] = (dados.get(c) or "").strip() or None
    if dados["bairro"]:
        existente = con.execute(text(
            "SELECT bairro FROM pedidos WHERE LOWER(bairro) = LOWER(:b) LIMIT 1"), {"b": dados["bairro"]}).scalar()
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
def criar_pedido(pedido: Pedido):
    """Registra um pedido novo.
    - Sem id: o servidor gera um id único (usado pelo painel).
    - Com id: se ele já existir, o pedido é ignorado (evita duplicar em reenvios)."""
    sql = text(
        "INSERT INTO pedidos (id, num, endereco, lat, lng, rua, numero, bairro, complemento, referencia, cep, telefone) "
        "VALUES (:id, :num, :endereco, :lat, :lng, :rua, :numero, :bairro, :complemento, :referencia, :cep, :telefone) "
        "ON CONFLICT (id) DO NOTHING")
    # A trava garante que dois pedidos criados no mesmo instante não recebam o mesmo número do dia.
    with _trava_numero, banco.engine.begin() as con:
        dados = _limpar_dados(con, pedido.model_dump())
        dados["num"] = (con.execute(text("SELECT COALESCE(MAX(num), 0) FROM pedidos WHERE criado_em >= :inicio"),
                                    {"inicio": inicio_do_dia_utc()}).scalar() or 0) + 1
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
            dados["num"] = con.execute(text("SELECT num FROM pedidos WHERE id = :id"), {"id": dados["id"]}).scalar()
        total = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'")).scalar()
    return {"mensagem": "Pedido recebido" if criado else "Pedido já existia (ignorado)",
            "id": dados["id"], "num": dados["num"], "rotulo": rotulo(dados["num"], dados["id"]),
            "criado": criado, "total_pendentes": total}


@router.put("/pedidos/{pedido_id}")
def editar_pedido(pedido_id: str, pedido: DadosPedido):
    """Corrige endereço, ponto no mapa ou telefone de um pedido que ainda está na fila.
    Pedido já despachado não pode ser editado: cancele e crie de novo, para a rota
    do motoboy não mudar sem ele perceber."""
    with despacho.trava_despacho(), banco.engine.begin() as con:
        dados = _limpar_dados(con, pedido.model_dump())
        dados["id"] = pedido_id
        r = con.execute(text(
            "UPDATE pedidos SET endereco = :endereco, lat = :lat, lng = :lng, rua = :rua, numero = :numero, "
            "bairro = :bairro, complemento = :complemento, referencia = :referencia, cep = :cep, telefone = :telefone "
            "WHERE id = :id AND status = 'pendente'"), dados)
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="Esse pedido não está mais na fila (já foi despachado, entregue ou cancelado).")
    return {"mensagem": "Pedido atualizado"}


@router.post("/pedidos/{pedido_id}/cancelar")
def cancelar_pedido(pedido_id: str):
    """Cancela um pedido na fila ou já em rota. Em rota, ele some da tela do motoboy
    na próxima atualização (5 s) e aparece um aviso de cancelamento para ele."""
    with despacho.trava_despacho(), banco.engine.begin() as con:
        atual = con.execute(text("SELECT status, motoboy_id, num FROM pedidos WHERE id = :id"), {"id": pedido_id}).first()
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
def listar_pedidos():
    """Pedidos pendentes e em rota (os concluídos não aparecem), marcados com
    "atrasado": true quando passam de PEDIDO_ATRASO_MIN na fila ou de ROTA_ATRASO_MIN
    em rota. "minutos" diz há quanto tempo está na situação atual."""
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, num, endereco, rua, numero, bairro, complemento, referencia, cep, telefone, lat, lng, "
            "status, motoboy_id, ordem, criado_em, despachado_em FROM pedidos WHERE status IN ('pendente', 'despachado') "
            "ORDER BY status, motoboy_id, ordem, criado_em"))]
    agora = agora_utc()
    for p in linhas:
        p["rotulo"] = rotulo(p["num"], p["id"])
        criado = parse_ts(p.pop("criado_em"))
        despachado = parse_ts(p.pop("despachado_em"))
        p["hora"] = para_local(criado).strftime("%H:%M") if criado else ""
        if p["status"] == "pendente":
            desde, limite = criado, PEDIDO_ATRASO_MIN
        else:
            desde, limite = despachado or criado, ROTA_ATRASO_MIN  # pedidos antigos não têm despachado_em
        minutos = (agora - desde).total_seconds() / 60 if desde is not None else None
        p["minutos"] = int(minutos) if minutos is not None else None
        p["atrasado"] = bool(minutos is not None and minutos >= limite)
    return {"pedidos": linhas, "restaurante_nome": RESTAURANTE_NOME}


@router.get("/bairros")
def listar_bairros():
    """Bairros já usados em pedidos, para sugerir no painel enquanto digita."""
    with banco.engine.connect() as con:
        nomes = [r[0] for r in con.execute(text(
            "SELECT DISTINCT bairro FROM pedidos WHERE bairro IS NOT NULL AND bairro <> '' ORDER BY bairro"))]
    return {"bairros": nomes}


@router.post("/geocodificar")
def geocodificar(c: Consulta):
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
                    "postalcode": (c.cep or "").strip(), "country": "Brasil"})
            except Exception as e:
                log.warning("Falha na busca por campos: %r", e)
        # Completa com a busca em texto livre (que inclui o bairro), sem repetir pontos.
        vistos = {(round(x["lat"], 5), round(x["lng"], 5)) for x in resultados}
        for x in buscar_endereco(c.endereco):
            if (round(x["lat"], 5), round(x["lng"], 5)) not in vistos:
                resultados.append(x)
        return {"resultados": resultados[:6], "centro": RESTAURANTE}
    except Exception as e:
        log.warning("Falha na busca de endereço: %r", e)
        return {"resultados": [], "centro": RESTAURANTE,
                "aviso": "O serviço de endereços não respondeu agora. Clique no mapa no ponto certo do cliente, ou tente buscar de novo."}


# ---------------------------------------------------------------------
# Despacho
# ---------------------------------------------------------------------
@router.post("/despachar")
def despachar():
    """Agrupa os pedidos pendentes, monta as rotas e distribui entre os motoboys.
    Cada grupo vai para quem consegue entregá-lo mais cedo (veja despacho.planejar).
    Se o motoboy escolhido ainda não saiu do restaurante, os novos pedidos entram
    na mesma viagem e a rota inteira é reorganizada; se já saiu, entram no fim."""
    with despacho.trava_despacho():
        # 1. Lê a situação atual (transação curta, só leitura).
        with banco.engine.connect() as con:
            pedidos = [dict(r._mapping) for r in con.execute(text(
                "SELECT id, num, endereco, lat, lng FROM pedidos WHERE status = 'pendente' ORDER BY criado_em, id"))]
            if not pedidos:
                return {"mensagem": "Não há pedidos pendentes para despachar."}
            estados, abertas = despacho.ler_estados(con)
            if not estados:
                return {"mensagem": "Nenhum motoboy de turno agora. Ligue o turno de alguém no painel."}

        # 2. Planeja e ordena as paradas sem o banco aberto (pode consultar o serviço de rotas).
        plano = despacho.planejar(despacho.agrupar_pedidos(pedidos), estados)
        rotas = {mid: despacho.montar_rota(viagens) for mid, viagens in plano.items()}

        # 3. Grava tudo de uma vez (transação curta). gravar_rota não desfaz uma
        #    entrega marcada pelo motoboy enquanto o passo 2 estava calculando.
        novos_ids = {p["id"] for p in pedidos}
        resultado = {}
        with banco.engine.begin() as con:
            for motoboy_id, rota in rotas.items():
                gravadas = despacho.gravar_rota(con, motoboy_id, rota, estados[motoboy_id]["saiu"],
                                                abertas.get(motoboy_id, []))
                # Na resposta, só os pedidos novos deste despacho.
                resultado[motoboy_id] = [p for p in gravadas if p["id"] in novos_ids]
        return {"rotas": resultado}


@router.post("/pedidos/{pedido_id}/atribuir")
def atribuir_manual(pedido_id: str, corpo: Atribuicao):
    """Manda um pedido pendente direto para um motoboy escolhido, pulando o
    agrupamento automático. Se o motoboy ainda não saiu, a parada entra na melhor
    posição da rota dele; se já saiu, fica para depois que ele voltar. Funciona
    mesmo com o motoboy fora de turno, já que é uma escolha manual."""
    with despacho.trava_despacho():
        with banco.engine.connect() as con:
            pedido = con.execute(text("SELECT id, endereco, lat, lng FROM pedidos WHERE id = :id AND status = 'pendente'"),
                                 {"id": pedido_id}).first()
            if pedido is None:
                raise HTTPException(status_code=404, detail="Pedido não encontrado ou já não está mais pendente")
            estados, abertas = despacho.ler_estados(con, ids=[corpo.motoboy_id])
            if corpo.motoboy_id not in estados:
                raise HTTPException(status_code=404, detail="Motoboy não encontrado")

        estado = estados[corpo.motoboy_id]
        novo = {"id": pedido[0], "endereco": pedido[1], "lat": pedido[2], "lng": pedido[3]}
        # Escolha manual: sem limite de paradas por viagem, junta tudo se ele ainda não saiu.
        rota = despacho.montar_rota([estado["proxima"] + [novo]])
        with banco.engine.begin() as con:
            rota = despacho.gravar_rota(con, corpo.motoboy_id, rota, estado["saiu"], abertas.get(corpo.motoboy_id, []))
        if not any(p["id"] == pedido_id for p in rota):
            raise HTTPException(status_code=409, detail="O pedido mudou enquanto era atribuído. Atualize a tela e tente de novo.")
        aviso = " Ele já saiu: a entrega fica para depois que ele voltar ao restaurante." if estado["saiu"] else ""
        return {"mensagem": "Pedido atribuído." + aviso, "rota": rota}


# ---------------------------------------------------------------------
# Motoboys
# ---------------------------------------------------------------------
@router.post("/motoboys")
def cadastrar_motoboy(motoboy: Motoboy):
    """Cadastra o motoboy ou atualiza a localização/código dele se já existir."""
    dados = motoboy.model_dump()
    dados["codigo"] = hash_codigo(dados["codigo"])  # o banco guarda só o hash
    with banco.engine.begin() as con:
        con.execute(text(
            "INSERT INTO motoboys (id, codigo, lat, lng) VALUES (:id, :codigo, :lat, :lng) "
            "ON CONFLICT (id) DO UPDATE SET codigo = excluded.codigo, lat = excluded.lat, lng = excluded.lng"),
            dados)
        total = con.execute(text("SELECT COUNT(*) FROM motoboys")).scalar()
    return {"mensagem": "Motoboy disponível", "total_disponiveis": total}


@router.get("/motoboys")
def listar_motoboys():
    """Lista os motoboys (sem os códigos), quantas paradas cada um tem em aberto e,
    se o GPS dele estiver ativo, a última posição (para o mapa da operação)."""
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT m.id, m.ativo, m.gps_em, m.gps_lat, m.gps_lng, "
            "(SELECT COUNT(*) FROM pedidos p WHERE p.motoboy_id = m.id AND p.status = 'despachado') AS paradas "
            "FROM motoboys m ORDER BY m.id"))]
    for m in linhas:
        m["gps_ativo"] = gps_recente(m.pop("gps_em")) and m["gps_lat"] is not None
        if not m["gps_ativo"]:
            m["gps_lat"] = m["gps_lng"] = None
    return {"motoboys": linhas, "centro": RESTAURANTE}


@router.delete("/motoboys/{motoboy_id}")
def remover_motoboy(motoboy_id: str):
    """Remove o motoboy. As paradas que ele ainda não entregou voltam para a fila.
    Usa a trava do despacho: senão, um despacho rodando ao mesmo tempo podia
    entregar pedidos novos a um motoboy que acabou de ser removido."""
    with despacho.trava_despacho(), banco.engine.begin() as con:
        r = con.execute(text("DELETE FROM motoboys WHERE id = :id"), {"id": motoboy_id})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Motoboy não encontrado")
        devolvidos = con.execute(text(
            "UPDATE pedidos SET status = 'pendente', motoboy_id = NULL, ordem = NULL, despachado_em = NULL "
            "WHERE motoboy_id = :id AND status = 'despachado'"), {"id": motoboy_id}).rowcount
    return {"mensagem": "Motoboy removido", "pedidos_devolvidos": devolvidos}


@router.post("/motoboys/{motoboy_id}/turno")
def definir_turno(motoboy_id: str, t: Turno):
    """Liga ou desliga o motoboy. Desligar não mexe nas paradas que ele já tem."""
    with banco.engine.begin() as con:
        r = con.execute(text("UPDATE motoboys SET ativo = :a WHERE id = :id"), {"a": t.ativo, "id": motoboy_id})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Motoboy não encontrado")
    return {"mensagem": "Turno atualizado"}


# ---------------------------------------------------------------------
# Números e histórico
# ---------------------------------------------------------------------
@router.get("/status")
def status():
    with banco.engine.connect() as con:
        por_status = {s: n for s, n in con.execute(text("SELECT status, COUNT(*) FROM pedidos GROUP BY status"))}
        motoboys = con.execute(text("SELECT COUNT(*) FROM motoboys")).scalar()
    return {"pedidos_por_status": por_status, "motoboys_cadastrados": motoboys}


@router.get("/resumo")
def resumo():
    """Números do dia: pendentes, em rota, entregues hoje e o tempo médio entre o
    pedido ser criado e ser entregue. "Hoje" usa o horário do restaurante."""
    with banco.engine.connect() as con:
        pendentes = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'")).scalar()
        em_rota = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE status = 'despachado'")).scalar()
        linhas = con.execute(text(
            "SELECT criado_em, concluido_em FROM pedidos WHERE status = 'concluido' AND concluido_em >= :inicio"),
            {"inicio": inicio_do_dia_utc()}).all()
    tempos = [(parse_ts(c) - parse_ts(criado)).total_seconds() / 60
              for criado, c in linhas if parse_ts(criado) is not None]
    return {"pendentes": pendentes, "em_rota": em_rota, "entregues_hoje": len(linhas),
            "tempo_medio_min": round(sum(tempos) / len(tempos), 1) if tempos else None}


@router.get("/historico.csv")
def historico_csv(dias: int = 30):
    """Baixa em CSV as entregas concluídas dos últimos N dias (30 por padrão, no
    máximo 365), para abrir no Excel ou guardar."""
    dias = max(1, min(dias, 365))
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, num, endereco, bairro, complemento, referencia, telefone, motoboy_id, criado_em, concluido_em "
            "FROM pedidos WHERE status = 'concluido' AND concluido_em >= :inicio ORDER BY concluido_em"),
            {"inicio": inicio_do_dia_utc(dias_atras=dias - 1)})]

    saida = io.StringIO()
    saida.write("﻿")  # marca de ordem de bytes: sem isso o Excel no Windows pode exibir acentos errados
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow(["Pedido", "Nº do dia", "Endereço", "Bairro", "Complemento", "Referência", "Telefone",
                       "Motoboy", "Criado em", "Entregue em", "Tempo (min)"])
    for p in linhas:
        criado_t, concluido_t = parse_ts(p["criado_em"]), parse_ts(p["concluido_em"])
        tempo = round((concluido_t - criado_t).total_seconds() / 60, 1) if criado_t else ""
        escritor.writerow([p["id"], p["num"] or "", p["endereco"], p["bairro"] or "", p["complemento"] or "",
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
    """Página do restaurante. Não contém dados: eles só aparecem depois de digitar a API_KEY."""
    return HTMLResponse(_PAINEL_HTML)
