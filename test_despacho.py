"""
Testes automáticos do sistema de despacho.

Rodam sozinhos no GitHub a cada envio de código (veja .github/workflows/testes.yml).
Para rodar no computador:  pip install fastapi sqlalchemy httpx pytest  e depois  pytest

Tudo acontece num banco SQLite temporário: os dados reais do restaurante nunca são tocados.
Os serviços externos (mapa, rotas, busca de endereço) ficam desligados durante os testes.
"""

import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Configuração do ambiente de teste. Precisa vir ANTES de importar o sistema.
_PASTA_TEMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_PASTA_TEMP, "teste.db")
os.environ["API_KEY"] = "chave-de-teste-bem-comprida-1234567890"
os.environ["OSRM_URL"] = "desligado"
os.environ["RESTAURANTE_NOME"] = "Restaurante Teste"
for _v in ("LOCATIONIQ_KEY", "LOCATIONIQ_MAPA_KEY", "RESTAURANTE_LAT", "RESTAURANTE_LNG"):
    os.environ.pop(_v, None)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import api_despacho3 as sistema  # noqa: E402

CHAVE = {"X-API-Key": os.environ["API_KEY"]}
CODIGO = "123456"
R = sistema.RESTAURANTE


# ---------------------------------------------------------------------
# Ajudantes
# ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
def banco_limpo():
    """Cada teste começa com o banco vazio e sem bloqueios de tentativas."""
    with sistema.engine.begin() as con:
        con.execute(text("DELETE FROM pedidos"))
        con.execute(text("DELETE FROM motoboys"))
    sistema._erros.clear()
    sistema._bloqueado_ate.clear()
    yield


@pytest.fixture
def api():
    return TestClient(sistema.app)


def sql(consulta, **params):
    with sistema.engine.begin() as con:
        return con.execute(text(consulta), params)


def novo_motoboy(api, nome, lat=None, lng=None):
    r = api.post("/motoboys", json={"id": nome, "codigo": CODIGO, "lat": lat or R["lat"], "lng": lng or R["lng"]},
                 headers=CHAVE)
    assert r.status_code == 200


def novo_pedido(api, lat=-19.62, lng=-43.23, **extra):
    corpo = {"endereco": "Rua Teste, 1 - Centro, Itabira - MG", "lat": lat, "lng": lng, **extra}
    r = api.post("/pedidos", json=corpo, headers=CHAVE)
    assert r.status_code == 200
    return r.json()


def rota(motoboy):
    """Ids das paradas em aberto do motoboy, na ordem."""
    return [r.id for r in sql("SELECT id FROM pedidos WHERE motoboy_id = :m AND status = 'despachado' "
                              "ORDER BY ordem", m=motoboy)]


def ver_rota(api, motoboy, codigo=CODIGO):
    return api.get(f"/rotas/{motoboy}", headers={"X-Codigo": codigo})


# ---------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------
def test_painel_exige_chave(api):
    assert api.get("/status").status_code == 401
    assert api.get("/status", headers={"X-API-Key": "errada"}).status_code == 401
    assert api.get("/status", headers=CHAVE).status_code == 200


def test_chave_errada_bloqueia_depois_de_varias_tentativas(api):
    for _ in range(sistema.LIMITE_POR_IP):
        api.get("/status", headers={"X-API-Key": "errada"})
    assert api.get("/status", headers={"X-API-Key": "errada"}).status_code == 429


def test_ip_inventado_no_x_forwarded_for_nao_escapa_do_bloqueio(api):
    """Um atacante que troca o primeiro IP do X-Forwarded-For a cada tentativa continua sendo contado."""
    for i in range(sistema.LIMITE_POR_IP):
        api.get("/status", headers={"X-API-Key": "errada", "X-Forwarded-For": f"1.2.3.{i}, 9.9.9.9"})
    r = api.get("/status", headers={"X-API-Key": "errada", "X-Forwarded-For": "5.5.5.5, 9.9.9.9"})
    assert r.status_code == 429


def test_codigo_do_motoboy_fica_guardado_com_hash(api):
    novo_motoboy(api, "Carlos")
    guardado = sql("SELECT codigo FROM motoboys WHERE id = 'Carlos'").scalar()
    assert guardado.startswith("pbkdf2$")
    assert CODIGO not in guardado


def test_motoboy_so_ve_a_rota_com_o_codigo_certo(api):
    novo_motoboy(api, "Carlos")
    assert ver_rota(api, "Carlos").status_code == 200
    assert ver_rota(api, "Carlos", "errado").status_code == 401
    assert ver_rota(api, "Carlos", "").status_code == 401
    assert ver_rota(api, "Ninguem").status_code == 401


def test_listagem_de_motoboys_nao_mostra_codigos(api):
    novo_motoboy(api, "Carlos")
    resposta = api.get("/motoboys", headers=CHAVE).text
    assert "codigo" not in resposta and "pbkdf2" not in resposta


def test_codigo_do_motoboy_precisa_de_6_caracteres(api):
    r = api.post("/motoboys", json={"id": "Curto", "codigo": "1234", "lat": R["lat"], "lng": R["lng"]},
                 headers=CHAVE)
    assert r.status_code == 422


def test_true_client_ip_inventado_nao_escapa_do_bloqueio(api):
    """Fora do plano Enterprise da Cloudflare, o True-Client-IP é escrito pelo próprio
    atacante. Trocar esse valor a cada tentativa não pode zerar a contagem."""
    for i in range(sistema.LIMITE_POR_IP):
        api.get("/status", headers={"X-API-Key": "errada", "True-Client-IP": f"1.2.3.{i}"})
    r = api.get("/status", headers={"X-API-Key": "errada", "True-Client-IP": "5.5.5.5"})
    assert r.status_code == 429


def test_atacante_nao_tranca_o_motoboy_que_ja_esta_usando_a_pagina(api):
    """Alguém erra o código do Carlos de vários IPs até bloquear o nome dele.
    O atacante fica bloqueado, mas o Carlos, que já tinha entrado, continua vendo a rota."""
    novo_motoboy(api, "Carlos")
    assert ver_rota(api, "Carlos").status_code == 200  # Carlos já está com a página aberta
    for i in range(sistema.LIMITE_POR_MOTOBOY):
        api.get("/rotas/Carlos", headers={"X-Codigo": "chute", "CF-Connecting-IP": f"10.0.0.{i}"})
    atacante = api.get("/rotas/Carlos", headers={"X-Codigo": "chute", "CF-Connecting-IP": "10.0.1.1"})
    assert atacante.status_code == 429
    carlos = api.get("/rotas/Carlos", headers={"X-Codigo": CODIGO, "CF-Connecting-IP": "200.1.1.1"})
    assert carlos.status_code == 200


# ---------------------------------------------------------------------
# Pedidos
# ---------------------------------------------------------------------
def test_numero_do_pedido_segue_a_sequencia_do_dia(api):
    numeros = [novo_pedido(api)["rotulo"] for _ in range(3)]
    assert numeros == ["#1", "#2", "#3"]


def test_numero_do_pedido_reinicia_no_dia_seguinte(api):
    novo_pedido(api)
    novo_pedido(api)
    sql("UPDATE pedidos SET criado_em = datetime('now', '-2 days')")
    assert novo_pedido(api)["rotulo"] == "#1"


def test_mesmo_id_enviado_duas_vezes_nao_duplica(api):
    a = novo_pedido(api, id="EXT-1")
    b = novo_pedido(api, id="EXT-1")
    assert a["criado"] is True and b["criado"] is False
    assert a["num"] == b["num"]
    assert sql("SELECT COUNT(*) FROM pedidos").scalar() == 1


def test_ids_gerados_pelo_servidor_nao_se_repetem(api):
    ids = {novo_pedido(api)["id"] for _ in range(30)}
    assert len(ids) == 30


def test_pedido_parado_na_fila_fica_atrasado(api):
    novo_pedido(api)
    sql("UPDATE pedidos SET criado_em = datetime('now', :m)", m=f"-{int(sistema.PEDIDO_ATRASO_MIN) + 1} minutes")
    p = api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]
    assert p["atrasado"] is True


def test_pedido_em_rota_ha_muito_tempo_fica_atrasado(api):
    novo_motoboy(api, "Carlos")
    novo_pedido(api)
    api.post("/despachar", headers=CHAVE)
    p = api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]
    assert p["status"] == "despachado" and p["atrasado"] is False
    sql("UPDATE pedidos SET despachado_em = datetime('now', :m)", m=f"-{int(sistema.ROTA_ATRASO_MIN) + 1} minutes")
    p = api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]
    assert p["atrasado"] is True


def test_so_edita_pedido_que_ainda_esta_na_fila(api):
    novo_motoboy(api, "Carlos")
    pid = novo_pedido(api)["id"]
    corpo = {"endereco": "Rua Nova, 2", "lat": -19.61, "lng": -43.22}
    assert api.put(f"/pedidos/{pid}", json=corpo, headers=CHAVE).status_code == 200
    api.post("/despachar", headers=CHAVE)
    assert api.put(f"/pedidos/{pid}", json=corpo, headers=CHAVE).status_code == 409


# ---------------------------------------------------------------------
# Despacho
# ---------------------------------------------------------------------
def test_despachar_sem_pedidos_ou_sem_motoboys(api):
    assert "Não há pedidos" in api.post("/despachar", headers=CHAVE).json()["mensagem"]
    novo_pedido(api)
    assert "Nenhum motoboy" in api.post("/despachar", headers=CHAVE).json()["mensagem"]


def test_pedidos_em_direcoes_diferentes_vao_para_motoboys_diferentes(api):
    for nome in ("A", "B", "C"):
        novo_motoboy(api, nome)
    novo_pedido(api, lat=R["lat"] + 0.04, lng=R["lng"])   # norte
    novo_pedido(api, lat=R["lat"] - 0.04, lng=R["lng"])   # sul
    novo_pedido(api, lat=R["lat"], lng=R["lng"] + 0.04)   # leste
    rotas = api.post("/despachar", headers=CHAVE).json()["rotas"]
    assert len(rotas) == 3 and all(len(v) == 1 for v in rotas.values())


def test_motoboy_livre_ganha_de_quem_ainda_esta_na_rua(api):
    novo_motoboy(api, "NaRua")
    novo_motoboy(api, "Livre")
    # NaRua tem uma entrega longe e o GPS dele mostra que já saiu.
    sql("INSERT INTO pedidos (id, endereco, lat, lng, status, motoboy_id, ordem, despachado_em) "
        "VALUES ('X', 'longe', -19.68, -43.30, 'despachado', 'NaRua', 1, CURRENT_TIMESTAMP)")
    sql("UPDATE motoboys SET gps_lat = -19.66, gps_lng = -43.28, gps_em = CURRENT_TIMESTAMP WHERE id = 'NaRua'")
    pid = novo_pedido(api)["id"]
    api.post("/despachar", headers=CHAVE)
    assert rota("Livre") == [pid]


def test_escolhe_quem_termina_mais_cedo_e_nao_quem_tem_menos_paradas(api):
    """Longe: sem paradas, mas a 8 km do restaurante (precisa voltar para pegar a comida).
    Perto: ainda no restaurante, já com uma entrega ao lado do pedido novo. Perto termina antes."""
    novo_motoboy(api, "Longe")
    novo_motoboy(api, "Perto")
    sql("UPDATE motoboys SET gps_lat = -19.69, gps_lng = -43.2258, gps_em = CURRENT_TIMESTAMP WHERE id = 'Longe'")
    vizinho = novo_pedido(api, lat=-19.6200, lng=-43.2300)["id"]
    api.post(f"/pedidos/{vizinho}/atribuir", json={"motoboy_id": "Perto"}, headers=CHAVE)
    pid = novo_pedido(api, lat=-19.6205, lng=-43.2305)["id"]
    api.post("/despachar", headers=CHAVE)
    assert pid in rota("Perto")


def test_quem_ainda_nao_saiu_leva_o_pedido_novo_na_mesma_viagem(api):
    novo_motoboy(api, "Ana")
    primeiro = novo_pedido(api, lat=-19.625, lng=-43.235)["id"]
    api.post("/despachar", headers=CHAVE)
    # Pedido novo mais perto do restaurante: deve entrar ANTES do primeiro (rota reorganizada).
    segundo = novo_pedido(api, lat=-19.620, lng=-43.230)["id"]
    api.post("/despachar", headers=CHAVE)
    assert rota("Ana") == [segundo, primeiro]


def test_quem_ja_saiu_recebe_o_pedido_novo_no_fim(api):
    novo_motoboy(api, "Ana")
    primeiro = novo_pedido(api, lat=-19.625, lng=-43.235)["id"]
    api.post("/despachar", headers=CHAVE)
    sql("UPDATE pedidos SET despachado_em = datetime('now', '-20 minutes')")  # saiu há 20 min
    segundo = novo_pedido(api, lat=-19.620, lng=-43.230)["id"]
    api.post("/despachar", headers=CHAVE)
    assert rota("Ana") == [primeiro, segundo]


def test_atribuicao_manual_respeita_se_o_motoboy_ja_saiu(api):
    novo_motoboy(api, "Bia")
    longe = novo_pedido(api, lat=-19.64, lng=-43.25)["id"]
    api.post(f"/pedidos/{longe}/atribuir", json={"motoboy_id": "Bia"}, headers=CHAVE)
    perto = novo_pedido(api, lat=-19.62, lng=-43.23)["id"]
    r = api.post(f"/pedidos/{perto}/atribuir", json={"motoboy_id": "Bia"}, headers=CHAVE).json()
    assert rota("Bia") == [perto, longe]          # ainda no restaurante: reorganiza
    assert "já saiu" not in r["mensagem"]

    sql("UPDATE pedidos SET despachado_em = datetime('now', '-20 minutes')")
    outro = novo_pedido(api, lat=-19.615, lng=-43.226)["id"]
    r = api.post(f"/pedidos/{outro}/atribuir", json={"motoboy_id": "Bia"}, headers=CHAVE).json()
    assert rota("Bia")[-1] == outro               # já saiu: vai para o fim
    assert "já saiu" in r["mensagem"]


def test_motoboy_fora_de_turno_nao_recebe_no_despacho_automatico(api):
    novo_motoboy(api, "Folga")
    api.post("/motoboys/Folga/turno", json={"ativo": False}, headers=CHAVE)
    novo_pedido(api)
    assert "Nenhum motoboy" in api.post("/despachar", headers=CHAVE).json()["mensagem"]


def test_remover_motoboy_devolve_as_paradas_para_a_fila(api):
    novo_motoboy(api, "Carlos")
    pid = novo_pedido(api)["id"]
    api.post("/despachar", headers=CHAVE)
    api.delete("/motoboys/Carlos", headers=CHAVE)
    status, motoboy, despachado = sql("SELECT status, motoboy_id, despachado_em FROM pedidos WHERE id = :p",
                                      p=pid).first()
    assert (status, motoboy, despachado) == ("pendente", None, None)


def test_pedido_entregue_durante_o_despacho_nao_volta_para_a_rota(api, monkeypatch):
    """O despacho lê os pedidos, calcula as rotas (pode levar segundos) e só depois grava.
    Se o motoboy aperta "Entregue" nesse meio-tempo, a entrega não pode ser desfeita."""
    novo_motoboy(api, "Ana")
    primeiro = novo_pedido(api, lat=-19.625, lng=-43.235)["id"]
    api.post("/despachar", headers=CHAVE)
    segundo = novo_pedido(api, lat=-19.620, lng=-43.230)["id"]

    original = sistema._montar_rota
    ja_entregou = []

    def montar_com_entrega_no_meio(viagens):
        if not ja_entregou:  # Ana marca a entrega enquanto o despacho está calculando
            ja_entregou.append(True)
            assert api.post(f"/rotas/Ana/entregar/{primeiro}", headers={"X-Codigo": CODIGO}).status_code == 200
        return original(viagens)

    monkeypatch.setattr(sistema, "_montar_rota", montar_com_entrega_no_meio)
    api.post("/despachar", headers=CHAVE)

    assert ja_entregou
    assert sql("SELECT status FROM pedidos WHERE id = :p", p=primeiro).scalar() == "concluido"
    assert rota("Ana") == [segundo]


# ---------------------------------------------------------------------
# Tela do motoboy
# ---------------------------------------------------------------------
def test_motoboy_marca_entrega(api):
    novo_motoboy(api, "Carlos")
    pid = novo_pedido(api)["id"]
    api.post("/despachar", headers=CHAVE)
    r = api.post(f"/rotas/Carlos/entregar/{pid}", headers={"X-Codigo": CODIGO})
    assert r.status_code == 200
    assert ver_rota(api, "Carlos").json()["paradas"] == []
    assert api.get("/resumo", headers=CHAVE).json()["entregues_hoje"] == 1


def test_motoboy_nao_marca_entrega_de_outro(api):
    novo_motoboy(api, "Carlos")
    novo_motoboy(api, "Ana")
    pid = novo_pedido(api)["id"]
    api.post(f"/pedidos/{pid}/atribuir", json={"motoboy_id": "Carlos"}, headers=CHAVE)
    assert api.post(f"/rotas/Ana/entregar/{pid}", headers={"X-Codigo": CODIGO}).status_code == 404


def test_cancelamento_em_rota_avisa_o_motoboy(api):
    novo_motoboy(api, "Carlos")
    pid = novo_pedido(api)["id"]
    api.post("/despachar", headers=CHAVE)
    api.post(f"/pedidos/{pid}/cancelar", headers=CHAVE)
    dados = ver_rota(api, "Carlos").json()
    assert dados["paradas"] == []
    assert [c["id"] for c in dados["cancelados"]] == [pid]


def test_cancelamento_de_pedido_na_fila_nao_avisa_ninguem(api):
    novo_motoboy(api, "Carlos")
    pid = novo_pedido(api)["id"]
    api.post(f"/pedidos/{pid}/cancelar", headers=CHAVE)
    assert ver_rota(api, "Carlos").json()["cancelados"] == []


def test_paginas_abrem(api):
    assert "Painel do restaurante" in api.get("/painel").text
    assert "Minha rota" in api.get("/motoboy").text


# ---------------------------------------------------------------------
# Atualização de um banco criado por versões antigas do sistema
# ---------------------------------------------------------------------
def test_banco_antigo_e_atualizado_sem_perder_dados(monkeypatch):
    """Simula o banco de uma versão anterior (sem as colunas novas e com o código do
    motoboy em texto) e confere que, ao iniciar, o sistema atualiza tudo sozinho."""
    caminho = os.path.join(_PASTA_TEMP, "antigo.db")
    con = sqlite3.connect(caminho)
    con.executescript("""
        CREATE TABLE motoboys (id TEXT PRIMARY KEY, codigo TEXT NOT NULL,
                               lat DOUBLE PRECISION NOT NULL, lng DOUBLE PRECISION NOT NULL);
        CREATE TABLE pedidos (id TEXT PRIMARY KEY, endereco TEXT NOT NULL, lat DOUBLE PRECISION NOT NULL,
                              lng DOUBLE PRECISION NOT NULL, status TEXT NOT NULL DEFAULT 'pendente',
                              motoboy_id TEXT, ordem INTEGER, criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO motoboys VALUES ('Veterano', 'codigo-antigo', -19.6156, -43.2258);
        INSERT INTO pedidos (id, endereco, lat, lng, status, motoboy_id, ordem)
            VALUES ('VELHO1', 'Rua Velha, 1', -19.62, -43.23, 'despachado', 'Veterano', 1);
    """)
    con.commit()
    con.close()

    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + caminho)
    arquivo = Path(sistema.__file__)
    spec = importlib.util.spec_from_file_location("sistema_migrado", arquivo)
    migrado = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrado)
    try:
        api = TestClient(migrado.app)
        with migrado.engine.connect() as c:
            assert c.execute(text("SELECT codigo FROM motoboys")).scalar().startswith("pbkdf2$")
        r = api.get("/rotas/Veterano", headers={"X-Codigo": "codigo-antigo"})
        assert r.status_code == 200                                    # código antigo continua valendo
        assert [p["id"] for p in r.json()["paradas"]] == ["VELHO1"]   # pedido antigo continua lá
        assert r.json()["paradas"][0]["rotulo"] == "VELHO1"           # sem número do dia: mostra o id
    finally:
        migrado.engine.dispose()
