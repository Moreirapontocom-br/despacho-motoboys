"""
Endereços usados pela página do motoboy. Não usam a API_KEY: cada motoboy
manda o próprio código no cabeçalho X-Codigo e só vê a rota dele.
"""

from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

import banco
import paginas
import restaurantes
from api_painel import rotulo
from config import FATOR_RUAS
from horarios import agora_utc, gps_recente, inicio_do_dia_utc, parse_ts
from mapas import custo_reta, trajeto
from seguranca import conferir_codigo

router = APIRouter()


class Posicao(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Disponivel(BaseModel):
    ativo: bool


def _estimar_minutos(base, paradas, origem):
    """Minutos estimados de cada trecho (da parada anterior até esta). Quando a
    parada é de uma saída seguinte, o trecho passa pelo restaurante (base). Estimativa
    em linha reta x FATOR_RUAS a ~30 km/h: serve de referência, não é exata."""
    anterior, viagem = origem, None
    for p in paradas:
        if viagem is not None and p.get("viagem") != viagem:
            segundos = custo_reta(anterior, base) + custo_reta(base, p)
            p["volta_antes"] = True  # precisa passar no restaurante para pegar este
        else:
            segundos = custo_reta(anterior, p)
            p["volta_antes"] = False
        p["minutos"] = max(1, round(segundos * FATOR_RUAS / 60))
        anterior, viagem = p, p.get("viagem")


@router.get("/rotas/{motoboy_id}")
def rota_do_motoboy(motoboy_id: str, request: Request, x_codigo: str = Header(default="")):
    with banco.engine.connect() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        motoboy = con.execute(text("SELECT ativo, gps_lat, gps_lng, gps_em, restaurante_id FROM motoboys WHERE id = :id"),
                              {"id": motoboy_id}).first()
        base = restaurantes.ponto(con, motoboy.restaurante_id)
        entregues_hoje = con.execute(text(
            "SELECT COUNT(*) FROM pedidos WHERE motoboy_id = :id AND status = 'concluido' AND concluido_em >= :ini"),
            {"id": motoboy_id, "ini": inicio_do_dia_utc()}).scalar()
        paradas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, num, cliente, endereco, complemento, referencia, telefone, lat, lng, viagem, saiu_em FROM pedidos "
            "WHERE motoboy_id = :id AND status = 'despachado' ORDER BY ordem"),
            {"id": motoboy_id})]
        # Pedidos dele cancelados na última hora, para a página mostrar um aviso
        # (senão a parada só sumiria da lista e ele poderia nem perceber).
        recentes = con.execute(text(
            "SELECT id, num, endereco, complemento, cancelado_em FROM pedidos "
            "WHERE motoboy_id = :id AND status = 'cancelado' AND cancelado_em IS NOT NULL "
            "ORDER BY cancelado_em DESC LIMIT 20"), {"id": motoboy_id}).all()
    limite = agora_utc() - timedelta(hours=1)
    cancelados = [{"id": c.id, "rotulo": rotulo(c.num, c.id), "endereco": c.endereco, "complemento": c.complemento}
                  for c in recentes if (parse_ts(c.cancelado_em) or limite) > limite]
    for p in paradas:
        p["rotulo"] = rotulo(p["num"], p["id"])
        p["saiu"] = bool(p.pop("saiu_em"))
    gps = None
    if gps_recente(motoboy.gps_em) and motoboy.gps_lat is not None:
        gps = {"lat": motoboy.gps_lat, "lng": motoboy.gps_lng}
    # Já na rua com GPS: a estimativa da 1ª parada sai de onde ele está.
    _estimar_minutos(base, paradas, gps if gps and paradas and paradas[0]["saiu"] else base)
    return {"motoboy_id": motoboy_id, "ativo": bool(motoboy.ativo),
            "restaurante": base, "paradas": paradas, "cancelados": cancelados, "gps": gps,
            "hoje": {"entregas": entregues_hoje},
            "trajeto": trajeto(base, paradas) if paradas else None}


@router.post("/rotas/{motoboy_id}/entregar/{pedido_id}")
def marcar_entregue(motoboy_id: str, pedido_id: str, request: Request, x_codigo: str = Header(default="")):
    """O motoboy marca uma parada como entregue (precisa do código dele)."""
    with banco.engine.begin() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        r = con.execute(text(
            "UPDATE pedidos SET status = 'concluido', concluido_em = CURRENT_TIMESTAMP "
            "WHERE id = :p AND motoboy_id = :m AND status = 'despachado'"),
            {"p": pedido_id, "m": motoboy_id})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Pedido não encontrado na sua rota")
    return {"mensagem": "Entrega registrada"}


@router.post("/rotas/{motoboy_id}/saiu")
def avisar_saida(motoboy_id: str, request: Request, x_codigo: str = Header(default="")):
    """O motoboy toca em "Saí do restaurante". Marca as paradas da viagem atual (a
    primeira ainda em aberto) como saídas: daí em diante o despacho não junta
    pedidos novos nela, e eles ficam para a volta."""
    with banco.engine.begin() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        primeira = con.execute(text(
            "SELECT viagem FROM pedidos WHERE motoboy_id = :m AND status = 'despachado' ORDER BY ordem LIMIT 1"),
            {"m": motoboy_id}).first()
        if primeira is None:
            raise HTTPException(status_code=404, detail="Você não tem entregas em aberto.")
        if primeira[0]:
            filtro, params = "viagem = :v", {"v": primeira[0]}
        else:  # pedidos de antes desta versão não têm viagem: marca todas as paradas abertas
            filtro, params = "1 = 1", {}
        n = con.execute(text(
            f"UPDATE pedidos SET saiu_em = CURRENT_TIMESTAMP WHERE motoboy_id = :m AND status = 'despachado' "
            f"AND saiu_em IS NULL AND {filtro}"), {"m": motoboy_id, **params}).rowcount
    return {"mensagem": "Boa entrega!", "paradas": n}


@router.post("/rotas/{motoboy_id}/disponivel")
def definir_disponivel(motoboy_id: str, d: Disponivel, request: Request, x_codigo: str = Header(default="")):
    """O próprio motoboy diz se está disponível para receber rotas (liga/desliga o
    turno dele). É o mesmo "De turno" do painel do restaurante. Desligar não mexe
    nas entregas que ele já tem."""
    with banco.engine.begin() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        con.execute(text("UPDATE motoboys SET ativo = :a WHERE id = :id"), {"a": d.ativo, "id": motoboy_id})
    return {"mensagem": "Você está disponível" if d.ativo else "Turno encerrado", "ativo": d.ativo}


@router.post("/rotas/{motoboy_id}/posicao")
def atualizar_posicao(motoboy_id: str, p: Posicao, request: Request, x_codigo: str = Header(default="")):
    """A página do motoboy chama isto sozinha (a cada ~1 min) quando ele escolhe
    "da minha posição". O despacho usa essa posição, se for recente."""
    with banco.engine.begin() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        con.execute(text("UPDATE motoboys SET gps_lat = :lat, gps_lng = :lng, gps_em = CURRENT_TIMESTAMP WHERE id = :id"),
                    {"lat": p.lat, "lng": p.lng, "id": motoboy_id})
    return {"mensagem": "Posição atualizada"}


_MOTOBOY_HTML = paginas.carregar("motoboy.html")


@router.get("/motoboy")
def pagina_do_motoboy():
    """Página com o mapa. Abra em /motoboy?id=NOME#codigo=CODIGO (não contém dados de clientes).
    O código vai depois do "#" para não ser enviado ao servidor nem gravado nos logs."""
    return HTMLResponse(_MOTOBOY_HTML)


@router.get("/m/{motoboy_id}")
def link_curto_do_motoboy(motoboy_id: str):
    """Link curto do motoboy: /m/NOME (na 1ª vez, /m/NOME#codigo=CODIGO).
    Depois de aberto uma vez, o código fica guardado no celular e o link sozinho já entra."""
    return HTMLResponse(_MOTOBOY_HTML)


@router.get("/m/{motoboy_id}/app.webmanifest")
def manifesto(motoboy_id: str):
    """Permite "Adicionar à tela inicial": vira um ícone que abre direto a rota dele."""
    return JSONResponse({
        "name": f"Entregas — {motoboy_id}", "short_name": motoboy_id[:12] or "Entregas",
        "start_url": f"/m/{motoboy_id}", "scope": "/m/", "display": "standalone",
        "background_color": "#111827", "theme_color": "#111827",
        "icons": [{"src": "/icone.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}],
    }, media_type="application/manifest+json")


_ICONE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="110" fill="#059669"/>'
          '<g fill="none" stroke="#fff" stroke-width="30" stroke-linecap="round" stroke-linejoin="round">'
          '<circle cx="150" cy="350" r="55"/><circle cx="370" cy="350" r="55"/>'
          '<path d="M150 350h120l60-150h-70M330 200l40 150M200 250h90"/></g></svg>')


@router.get("/icone.svg")
def icone():
    return Response(_ICONE, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})
