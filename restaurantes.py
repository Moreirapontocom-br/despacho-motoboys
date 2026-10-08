"""
Restaurantes: vários usam o mesmo sistema, cada um com os próprios pedidos,
motoboys, regras e equipe.

O endereço de cada restaurante (de onde os motoboys saem) fica no banco. O dono
informa ao criar a conta pelo convite e só ele (ou o administrador) pode mudar.
O total de restaurantes é limitado por MAX_RESTAURANTES (6 por padrão).
"""

from fastapi import HTTPException
from sqlalchemy import text

import config

CAMPOS = "id, nome, endereco, cidade, lat, lng"


def ler(con, rid):
    """O restaurante (dict) ou None."""
    r = con.execute(text(f"SELECT {CAMPOS} FROM restaurantes WHERE id = :r"), {"r": rid}).first()
    return dict(r._mapping) if r else None


def ponto(con, rid):
    """Onde o restaurante fica: {"lat", "lng", "endereco"} (o formato usado no despacho)."""
    r = ler(con, rid)
    if r is None:
        raise HTTPException(status_code=404, detail="Restaurante não encontrado.")
    return {"lat": r["lat"], "lng": r["lng"], "endereco": r["endereco"]}


def listar(con):
    return [dict(r._mapping) for r in con.execute(text(f"SELECT {CAMPOS} FROM restaurantes ORDER BY id"))]


def ids(con):
    return [r[0] for r in con.execute(text("SELECT id FROM restaurantes ORDER BY id"))]


def conferir_limite(con):
    if len(ids(con)) >= config.MAX_RESTAURANTES:
        raise HTTPException(status_code=409, detail=f"O sistema já tem {config.MAX_RESTAURANTES} restaurantes, o máximo "
                                                    "configurado. Para aumentar, mude MAX_RESTAURANTES no Render.")


def criar(con, nome, endereco, cidade, lat, lng):
    """Cria um restaurante e devolve o id. Quem chama já está numa transação."""
    conferir_limite(con)
    rid = (con.execute(text("SELECT MAX(id) FROM restaurantes")).scalar() or 0) + 1
    con.execute(text("INSERT INTO restaurantes (id, nome, endereco, cidade, lat, lng) VALUES (:id, :n, :e, :c, :lat, :lng)"),
                {"id": rid, "n": nome.strip(), "e": endereco.strip(), "c": cidade.strip(), "lat": lat, "lng": lng})
    return rid


def atualizar(con, rid, nome, endereco, cidade, lat, lng):
    con.execute(text("UPDATE restaurantes SET nome = :n, endereco = :e, cidade = :c, lat = :lat, lng = :lng WHERE id = :id"),
                {"id": rid, "n": nome.strip(), "e": endereco.strip(), "c": cidade.strip(), "lat": lat, "lng": lng})
