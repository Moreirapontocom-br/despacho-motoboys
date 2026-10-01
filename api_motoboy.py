"""
Endereços usados pela página do motoboy. Não usam a API_KEY: cada motoboy
manda o próprio código no cabeçalho X-Codigo e só vê a rota dele.
"""

from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

import banco
import paginas
from api_painel import rotulo
from config import RESTAURANTE
from horarios import agora_utc, parse_ts
from mapas import trajeto
from seguranca import conferir_codigo

router = APIRouter()


class Posicao(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


@router.get("/rotas/{motoboy_id}")
def rota_do_motoboy(motoboy_id: str, request: Request, x_codigo: str = Header(default="")):
    with banco.engine.connect() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        paradas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, num, endereco, complemento, referencia, telefone, lat, lng FROM pedidos "
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
    return {"motoboy_id": motoboy_id, "restaurante": RESTAURANTE, "paradas": paradas, "cancelados": cancelados,
            "trajeto": trajeto(RESTAURANTE, paradas) if paradas else None}


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
