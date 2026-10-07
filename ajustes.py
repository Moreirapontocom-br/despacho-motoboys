"""
Ajustes do despacho que o restaurante escolhe no próprio painel (sem mexer no
Render): o modo de despacho e as regras usadas pelo despacho automático.

Modos:
    assistido    o botão "Despachar" mostra a sugestão (quem leva o quê e a
                 economia estimada) e o funcionário só confirma
    automatico   o sistema despacha sozinho, seguindo as regras abaixo
    manual       versões antigas; hoje funciona igual ao assistido (o botão
                 sempre mostra a sugestão antes de despachar)
"""

import json

from sqlalchemy import text

from config import MAX_PARADAS_VIAGEM

MODOS = ("manual", "assistido", "automatico")

PADROES = {
    "modo": "assistido",
    # Máximo de entregas numa mesma saída do restaurante.
    "max_paradas": MAX_PARADAS_VIAGEM,
    # Pedidos mais longe que isto do restaurante não são despachados sozinhos
    # (ficam para o funcionário decidir). 0 = sem limite.
    "raio_max_km": 0,
    # Pedido sem nenhum outro por perto espera até este tempo por um "parceiro"
    # de viagem antes de sair sozinho. 0 = não espera.
    "espera_agrupamento_min": 3,
    # Pedido esperando há mais que isto sai na hora, mesmo sem parceiro.
    "prioridade_min": 20,
    # Pedidos a até esta distância um do outro podem ir na mesma viagem.
    "raio_agrupamento_km": 2.0,
    # Quanto custa cada km rodado (combustível, desgaste, taxa do motoboy...).
    # Usado só para mostrar a economia em reais. 0 = não mostrar em reais.
    "custo_km": 0,
}

# Limites aceitos para cada número (mínimo, máximo).
LIMITES = {"max_paradas": (1, 10), "raio_max_km": (0, 50), "espera_agrupamento_min": (0, 30), "prioridade_min": (1, 120),
           "raio_agrupamento_km": (0.3, 10), "custo_km": (0, 20)}


def ler(con):
    """Ajustes atuais (o que não foi salvo usa o padrão)."""
    valores = dict(PADROES)
    for chave, valor in con.execute(text("SELECT chave, valor FROM ajustes")):
        if chave in valores:
            try:
                valores[chave] = json.loads(valor)
            except ValueError:
                pass
    return valores


def salvar(con, novos):
    """Grava só as chaves conhecidas, já conferidas. Devolve os ajustes completos."""
    for chave, valor in novos.items():
        if chave not in PADROES or valor is None:
            continue
        con.execute(text("DELETE FROM ajustes WHERE chave = :c"), {"c": chave})
        con.execute(text("INSERT INTO ajustes (chave, valor) VALUES (:c, :v)"), {"c": chave, "v": json.dumps(valor)})
    return ler(con)
