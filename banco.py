"""
Banco de dados: conexão, criação das tabelas e atualização de bancos antigos.

Os outros arquivos usam sempre `banco.engine` (e não `from banco import engine`),
para que os testes possam trocar o banco por um temporário.
"""

import sqlite3
from datetime import datetime

from sqlalchemy import create_engine, inspect, text

import config
from seguranca import hash_codigo

# SQLite (testes e uso local): grava horários no mesmo formato do CURRENT_TIMESTAMP.
# O conversor automático que o Python fazia isso está obsoleto desde o Python 3.12.
sqlite3.register_adapter(datetime, lambda d: d.isoformat(" "))

# Campos do endereço separado. Opcionais: pedidos antigos têm só "endereco".
CAMPOS_ENDERECO = ("rua", "numero", "bairro", "complemento", "referencia", "cep")


def criar_engine(url):
    """Conexão com o banco. No Postgres, a sessão é fixada em UTC: assim o
    CURRENT_TIMESTAMP gravado é sempre UTC, qualquer que seja o fuso configurado
    no servidor do banco (o Python compara esses horários com o relógio em UTC)."""
    argumentos = {"options": "-c timezone=UTC"} if url.startswith("postgresql") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=argumentos)


# Colunas que versões anteriores do sistema não tinham. Ao iniciar, as que
# faltarem são criadas; as que já existem ficam como estão.
_COLUNAS_NOVAS = {
    "motoboys": [("ativo", "BOOLEAN NOT NULL DEFAULT TRUE"), ("gps_lat", "DOUBLE PRECISION"),
                 ("gps_lng", "DOUBLE PRECISION"), ("gps_em", "TIMESTAMP")],
    "pedidos": [("concluido_em", "TIMESTAMP"), ("telefone", "TEXT"), ("cancelado_em", "TIMESTAMP"),
                ("despachado_em", "TIMESTAMP"),
                ("num", "INTEGER")]  # número do pedido no dia (#1, #2...), fácil de falar no balcão
               + [(c, "TEXT") for c in CAMPOS_ENDERECO],
}

# A página do motoboy consulta a cada 5 s e o painel a cada 10 s. Sem índices,
# o banco relê a tabela de pedidos inteira em toda consulta, e isso pesa com os meses.
_INDICES = ("idx_pedidos_status ON pedidos (status)",
            "idx_pedidos_motoboy ON pedidos (motoboy_id, status)",
            "idx_pedidos_criado ON pedidos (criado_em)",
            "idx_pedidos_concluido ON pedidos (concluido_em)")


def preparar(eng):
    """Cria as tabelas e atualiza bancos de versões anteriores, sem perder dados."""
    with eng.begin() as con:
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS motoboys (
                id TEXT PRIMARY KEY,
                codigo TEXT NOT NULL,
                lat DOUBLE PRECISION NOT NULL,
                lng DOUBLE PRECISION NOT NULL
            )"""))
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS pedidos (
                id TEXT PRIMARY KEY,
                endereco TEXT NOT NULL,
                lat DOUBLE PRECISION NOT NULL,
                lng DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente',
                motoboy_id TEXT,
                ordem INTEGER,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""))

    # Confere quais colunas já existem antes de criar. Diferente de tentar criar e
    # ignorar o erro, um problema de verdade (permissão, banco fora do ar) aparece.
    for tabela, colunas in _COLUNAS_NOVAS.items():
        existentes = {c["name"] for c in inspect(eng).get_columns(tabela)}
        for nome, tipo in colunas:
            if nome not in existentes:
                with eng.begin() as con:
                    con.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}"))

    with eng.begin() as con:
        for indice in _INDICES:
            con.execute(text(f"CREATE INDEX IF NOT EXISTS {indice}"))
        # Códigos salvos em texto por versões antigas viram hash.
        # Os motoboys continuam entrando com o mesmo código de sempre.
        for id_, codigo in con.execute(text("SELECT id, codigo FROM motoboys WHERE codigo NOT LIKE 'pbkdf2$%'")).all():
            con.execute(text("UPDATE motoboys SET codigo = :c WHERE id = :id"), {"c": hash_codigo(codigo), "id": id_})


engine = criar_engine(config.DATABASE_URL)
preparar(engine)
