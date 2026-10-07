"""
Testes automáticos do sistema de despacho.

Rodam sozinhos no GitHub a cada envio de código (veja .github/workflows/testes.yml).
Para rodar no computador:  pip install fastapi sqlalchemy httpx pytest  e depois  pytest

Tudo acontece num banco SQLite temporário: os dados reais do restaurante nunca são tocados.
Os serviços externos (mapa, rotas, busca de endereço) ficam desligados durante os testes.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

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

import api_despacho3  # noqa: E402
import api_painel  # noqa: E402
import banco  # noqa: E402
import config  # noqa: E402
import despacho  # noqa: E402
import horarios  # noqa: E402
import seguranca  # noqa: E402

CHAVE = {"X-API-Key": os.environ["API_KEY"]}
CODIGO = "123456"
R = config.RESTAURANTE


# ---------------------------------------------------------------------
# Ajudantes
# ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
def banco_limpo():
    """Cada teste começa com o banco vazio e sem bloqueios de tentativas."""
    with banco.engine.begin() as con:
        con.execute(text("DELETE FROM pedidos"))
        con.execute(text("DELETE FROM motoboys"))
        con.execute(text("DELETE FROM ajustes"))
        con.execute(text("DELETE FROM sessoes"))
        con.execute(text("DELETE FROM usuarios"))
        con.execute(text("DELETE FROM convites"))
    seguranca._erros.clear()
    seguranca._bloqueado_ate.clear()
    yield


@pytest.fixture
def api():
    return TestClient(api_despacho3.app)


def sql(consulta, **params):
    with banco.engine.begin() as con:
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
    for _ in range(seguranca.LIMITE_POR_IP):
        api.get("/status", headers={"X-API-Key": "errada"})
    assert api.get("/status", headers={"X-API-Key": "errada"}).status_code == 429


def test_ip_inventado_no_x_forwarded_for_nao_escapa_do_bloqueio(api):
    """Um atacante que troca o primeiro IP do X-Forwarded-For a cada tentativa continua sendo contado."""
    for i in range(seguranca.LIMITE_POR_IP):
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
    for i in range(seguranca.LIMITE_POR_IP):
        api.get("/status", headers={"X-API-Key": "errada", "True-Client-IP": f"1.2.3.{i}"})
    r = api.get("/status", headers={"X-API-Key": "errada", "True-Client-IP": "5.5.5.5"})
    assert r.status_code == 429


def test_atacante_nao_tranca_o_motoboy_que_ja_esta_usando_a_pagina(api):
    """Alguém erra o código do Carlos de vários IPs até bloquear o nome dele.
    O atacante fica bloqueado, mas o Carlos, que já tinha entrado, continua vendo a rota."""
    novo_motoboy(api, "Carlos")
    assert ver_rota(api, "Carlos").status_code == 200  # Carlos já está com a página aberta
    for i in range(seguranca.LIMITE_POR_MOTOBOY):
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
    sql("UPDATE pedidos SET criado_em = datetime('now', :m)", m=f"-{int(config.PEDIDO_ATRASO_MIN) + 1} minutes")
    p = api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]
    assert p["atrasado"] is True


def test_pedido_em_rota_ha_muito_tempo_fica_atrasado(api):
    novo_motoboy(api, "Carlos")
    novo_pedido(api)
    api.post("/despachar", headers=CHAVE)
    p = api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]
    assert p["status"] == "despachado" and p["atrasado"] is False
    sql("UPDATE pedidos SET despachado_em = datetime('now', :m)", m=f"-{int(config.ROTA_ATRASO_MIN) + 1} minutes")
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

    original = despacho.montar_rota
    ja_entregou = []

    def montar_com_entrega_no_meio(viagens):
        if not ja_entregou:  # Ana marca a entrega enquanto o despacho está calculando
            ja_entregou.append(True)
            assert api.post(f"/rotas/Ana/entregar/{primeiro}", headers={"X-Codigo": CODIGO}).status_code == 200
        return original(viagens)

    monkeypatch.setattr(despacho, "montar_rota", montar_com_entrega_no_meio)
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
    assert "Minhas entregas" in api.get("/motoboy").text


# ---------------------------------------------------------------------
# Atualização de um banco criado por versões antigas do sistema
# ---------------------------------------------------------------------
def test_banco_antigo_e_atualizado_sem_perder_dados(monkeypatch):
    """Simula o banco de uma versão anterior (sem as colunas novas e com o código do
    motoboy em texto) e confere que banco.preparar atualiza tudo sem perder dados."""
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

    antigo = banco.criar_engine("sqlite:///" + caminho)
    try:
        banco.preparar(antigo)
        banco.preparar(antigo)  # rodar de novo (todo reinício do servidor) não pode dar erro
        monkeypatch.setattr(banco, "engine", antigo)
        api = TestClient(api_despacho3.app)
        with antigo.connect() as c:
            assert c.execute(text("SELECT codigo FROM motoboys")).scalar().startswith("pbkdf2$")
        r = api.get("/rotas/Veterano", headers={"X-Codigo": "codigo-antigo"})
        assert r.status_code == 200                                    # código antigo continua valendo
        assert [p["id"] for p in r.json()["paradas"]] == ["VELHO1"]   # pedido antigo continua lá
        assert r.json()["paradas"][0]["rotulo"] == "VELHO1"           # sem número do dia: mostra o id
        assert api.get("/motoboys", headers=CHAVE).json()["motoboys"][0]["ativo"]  # coluna nova com o padrão
    finally:
        antigo.dispose()


# ---------------------------------------------------------------------
# Horários e fuso
# ---------------------------------------------------------------------
def test_inicio_do_dia_usa_o_horario_do_restaurante():
    """01/10 às 02:00 em UTC ainda é 30/09 às 23:00 em Itabira (UTC-3).
    Então "hoje" começou em 30/09 00:00 local = 30/09 03:00 UTC."""
    agora = datetime(2026, 10, 1, 2, 0)
    assert horarios.inicio_do_dia_utc(agora) == datetime(2026, 9, 30, 3, 0)
    assert horarios.inicio_do_dia_utc(agora, dias_atras=6) == datetime(2026, 9, 24, 3, 0)
    assert horarios.fmt_local(agora) == "30/09/2026 23:00"


def test_gps_muito_no_futuro_nao_vale():
    """Antes, qualquer diferença de fuso passava (o sistema usava o valor absoluto).
    Agora só uma folga pequena de relógio é aceita."""
    agora = datetime(2026, 10, 1, 12, 0)
    assert horarios.gps_recente(agora - timedelta(minutes=2), agora)
    assert horarios.gps_recente(agora + timedelta(seconds=30), agora)
    assert not horarios.gps_recente(agora - timedelta(minutes=config.GPS_VALIDADE_MIN + 1), agora)
    assert not horarios.gps_recente(agora + timedelta(hours=3), agora)   # erro de fuso
    assert not horarios.gps_recente(None, agora)


def test_horario_gravado_pelo_banco_e_utc():
    agora = horarios.agora_utc()
    assert agora.tzinfo is None
    with banco.engine.connect() as c:
        do_banco = horarios.parse_ts(c.execute(text("SELECT CURRENT_TIMESTAMP")).scalar())
    assert abs((do_banco - agora).total_seconds()) < 60


# ---------------------------------------------------------------------
# Painel
# ---------------------------------------------------------------------
def test_painel_recebe_as_configuracoes(api):
    html = api.get("/painel").text
    assert "/*__CONFIG__*/" not in html
    assert '"cidade": "Itabira, MG"' in html
    assert f'"pedidoAtrasoMin": {int(config.PEDIDO_ATRASO_MIN)}' in html
    assert "<\\/a>" in html  # o crédito do mapa não fecha o <script> antes da hora


def test_mapa_da_operacao_so_mostra_gps_recente(api):
    novo_motoboy(api, "ComGps")
    novo_motoboy(api, "GpsVelho")
    sql("UPDATE motoboys SET gps_lat = -19.62, gps_lng = -43.22, gps_em = CURRENT_TIMESTAMP WHERE id = 'ComGps'")
    sql("UPDATE motoboys SET gps_lat = -19.63, gps_lng = -43.23, gps_em = :t WHERE id = 'GpsVelho'",
        t=horarios.agora_utc() - timedelta(hours=2))
    motos = {m["id"]: m for m in api.get("/motoboys", headers=CHAVE).json()["motoboys"]}
    assert motos["ComGps"]["gps_ativo"] and motos["ComGps"]["gps_lat"] == -19.62
    assert not motos["GpsVelho"]["gps_ativo"] and motos["GpsVelho"]["gps_lat"] is None


def test_lista_de_pedidos_traz_a_hora_local(api):
    novo_pedido(api)
    sql("UPDATE pedidos SET criado_em = :t", t=datetime(2026, 10, 1, 2, 5))
    assert api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]["hora"] == "23:05"


# ---------------------------------------------------------------------
# Modo assistido: sugestão antes de despachar
# ---------------------------------------------------------------------
def test_previa_nao_grava_nada_e_mostra_economia(api):
    novo_motoboy(api, "Carlos")
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    b = novo_pedido(api, lat=-19.631, lng=-43.241)["id"]
    r = api.post("/despachar/previa", headers=CHAVE).json()
    assert sql("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'").scalar() == 2
    assert r["viagens"] == 1
    assert {p["id"] for p in r["sugestoes"][0]["viagens"][0]["pedidos"]} == {a, b}
    assert r["economia_km"] > 0 and r["km_com"] < r["km_sem"]


def test_confirmar_previa_grava_a_sugestao(api):
    novo_motoboy(api, "Carlos")
    novo_motoboy(api, "Ana")
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    plano = api.post("/despachar/previa", headers=CHAVE).json()["plano"]
    escolhido = next(iter(plano))
    r = api.post("/despachar", json={"plano": plano}, headers=CHAVE).json()
    assert list(r["rotas"]) == [escolhido]
    assert rota(escolhido) == [a]


def test_previa_confirmada_ignora_pedido_cancelado_no_meio_tempo(api):
    novo_motoboy(api, "Carlos")
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    b = novo_pedido(api, lat=-19.631, lng=-43.241)["id"]
    plano = api.post("/despachar/previa", headers=CHAVE).json()["plano"]
    api.post(f"/pedidos/{b}/cancelar", headers=CHAVE)
    api.post("/despachar", json={"plano": plano}, headers=CHAVE)
    assert rota("Carlos") == [a]


def test_previa_de_motoboy_que_saiu_de_turno_fica_na_fila(api):
    novo_motoboy(api, "Carlos")
    novo_pedido(api)
    plano = api.post("/despachar/previa", headers=CHAVE).json()["plano"]
    api.post("/motoboys/Carlos/turno", json={"ativo": False}, headers=CHAVE)
    novo_motoboy(api, "Ana")
    r = api.post("/despachar", json={"plano": plano}, headers=CHAVE).json()
    assert "desatualizada" in r["mensagem"]
    assert sql("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'").scalar() == 1


# ---------------------------------------------------------------------
# Fila: atenção, agrupamento e regras
# ---------------------------------------------------------------------
def test_pedido_sozinho_aguarda_parceiro_e_libera_quando_chega_outro(api):
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    pedidos = {p["id"]: p for p in api.get("/pedidos", headers=CHAVE).json()["pedidos"]}
    assert pedidos[a]["aguardando"] is True
    b = novo_pedido(api, lat=-19.631, lng=-43.241)
    pedidos = {p["id"]: p for p in api.get("/pedidos", headers=CHAVE).json()["pedidos"]}
    assert pedidos[a]["aguardando"] is False
    assert pedidos[a]["agrupar_com"] == [b["rotulo"]]


def test_pedido_esperando_muito_nao_aguarda_mais(api):
    a = novo_pedido(api)["id"]
    sql("UPDATE pedidos SET criado_em = :t WHERE id = :p", t=horarios.agora_utc() - timedelta(minutes=12), p=a)
    p = api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]
    assert p["aguardando"] is False
    assert p["situacao"] == "atencao"  # 12 de 15 min: passou de 70%


def test_situacao_atrasado(api):
    a = novo_pedido(api)["id"]
    sql("UPDATE pedidos SET criado_em = :t WHERE id = :p", t=horarios.agora_utc() - timedelta(minutes=30), p=a)
    assert api.get("/pedidos", headers=CHAVE).json()["pedidos"][0]["situacao"] == "atrasado"


def test_ajustes_salvam_e_validam(api):
    assert api.get("/ajustes", headers=CHAVE).json()["ajustes"]["modo"] == "assistido"
    r = api.put("/ajustes", json={"modo": "assistido", "max_paradas": 3}, headers=CHAVE)
    assert r.json()["ajustes"]["modo"] == "assistido" and r.json()["ajustes"]["max_paradas"] == 3
    assert api.put("/ajustes", json={"modo": "qualquer"}, headers=CHAVE).status_code == 422
    assert api.put("/ajustes", json={"max_paradas": 0}, headers=CHAVE).status_code == 422


def test_max_paradas_dos_ajustes_divide_as_viagens(api):
    api.put("/ajustes", json={"max_paradas": 1}, headers=CHAVE)
    novo_motoboy(api, "Carlos")
    novo_pedido(api, lat=-19.630, lng=-43.240)
    novo_pedido(api, lat=-19.631, lng=-43.241)
    assert api.post("/despachar/previa", headers=CHAVE).json()["viagens"] == 2


# ---------------------------------------------------------------------
# Despacho automático
# ---------------------------------------------------------------------
def test_automatico_desligado_nao_faz_nada(api):
    novo_motoboy(api, "Carlos")
    novo_pedido(api)
    assert api_painel.despacho_automatico_uma_vez() is None
    assert rota("Carlos") == []


def test_automatico_despacha_quem_esta_pronto_e_segura_quem_aguarda(api):
    api.put("/ajustes", json={"modo": "automatico", "espera_agrupamento_min": 5}, headers=CHAVE)
    novo_motoboy(api, "Carlos")
    sozinho = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    api_painel.despacho_automatico_uma_vez()
    assert rota("Carlos") == []  # ainda esperando um parceiro de viagem
    parceiro = novo_pedido(api, lat=-19.631, lng=-43.241)["id"]
    api_painel.despacho_automatico_uma_vez()
    assert set(rota("Carlos")) == {sozinho, parceiro}
    assert len({r[0] for r in sql("SELECT viagem FROM pedidos WHERE motoboy_id = 'Carlos'")}) == 1


def test_automatico_nao_manda_pedido_alem_do_raio(api):
    api.put("/ajustes", json={"modo": "automatico", "raio_max_km": 1, "espera_agrupamento_min": 0}, headers=CHAVE)
    novo_motoboy(api, "Carlos")
    longe = novo_pedido(api, lat=R["lat"] - 0.1, lng=R["lng"])["id"]  # ~11 km
    perto = novo_pedido(api, lat=R["lat"] - 0.002, lng=R["lng"])["id"]
    api_painel.despacho_automatico_uma_vez()
    assert rota("Carlos") == [perto]
    assert sql("SELECT status FROM pedidos WHERE id = :p", p=longe).scalar() == "pendente"
    pedido_longe = [p for p in api.get("/pedidos", headers=CHAVE).json()["pedidos"] if p["id"] == longe][0]
    assert pedido_longe["longe"] is True


# ---------------------------------------------------------------------
# Motoboy avisa que saiu
# ---------------------------------------------------------------------
def test_motoboy_avisa_saida_e_pedido_novo_fica_para_a_volta(api):
    novo_motoboy(api, "Carlos")
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    api.post("/despachar", headers=CHAVE)
    assert api.post("/rotas/Carlos/saiu", headers={"X-Codigo": CODIGO}).status_code == 200
    b = novo_pedido(api, lat=-19.631, lng=-43.241)["id"]
    api.post("/despachar", headers=CHAVE)
    assert rota("Carlos") == [a, b]
    viagens = dict(sql("SELECT id, viagem FROM pedidos").all())
    assert viagens[a] != viagens[b]
    paradas = ver_rota(api, "Carlos").json()["paradas"]
    assert [p["saiu"] for p in paradas] == [True, False]


def test_avisar_saida_exige_codigo(api):
    novo_motoboy(api, "Carlos")
    assert api.post("/rotas/Carlos/saiu", headers={"X-Codigo": "errado"}).status_code == 401


# ---------------------------------------------------------------------
# Cadastro do motoboy e estatísticas
# ---------------------------------------------------------------------
def test_cadastro_com_telefone_e_tipo(api):
    r = api.post("/motoboys", json={"id": "Joao", "codigo": CODIGO, "lat": R["lat"], "lng": R["lng"],
                                    "telefone": "(31) 99999-0000", "tipo": "terceirizado"}, headers=CHAVE)
    assert r.status_code == 200
    m = api.get("/motoboys", headers=CHAVE).json()["motoboys"][0]
    assert (m["telefone"], m["tipo"]) == ("(31) 99999-0000", "terceirizado")
    r = api.post("/motoboys", json={"id": "X", "codigo": CODIGO, "lat": R["lat"], "lng": R["lng"], "tipo": "outro"},
                 headers=CHAVE)
    assert r.status_code == 422


def test_estatisticas_contam_viagens_agrupadas_e_economia(api):
    novo_motoboy(api, "Carlos")
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    b = novo_pedido(api, lat=-19.631, lng=-43.241)["id"]
    api.post("/despachar", headers=CHAVE)
    for p in (a, b):
        api.post(f"/rotas/Carlos/entregar/{p}", headers={"X-Codigo": CODIGO})
    e = api.get("/estatisticas?dias=1", headers=CHAVE).json()
    g = e["geral"]
    assert (g["entregas"], g["viagens"], g["agrupados"], g["motoboys"]) == (2, 1, 2, 1)
    assert g["km"] > 0 and g["km_economizados"] > 0
    assert g["no_prazo_pct"] == 100
    assert e["motoboys"][0]["motoboy"] == "Carlos" and e["motoboys"][0]["entregas"] == 2


def test_estatisticas_sem_entregas(api):
    g = api.get("/estatisticas?dias=7", headers=CHAVE).json()["geral"]
    assert g["entregas"] == 0 and g["tempo_medio_min"] is None


def test_previa_explica_a_escolha(api):
    novo_motoboy(api, "Carlos")
    novo_motoboy(api, "Ana")
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    api.post(f"/pedidos/{a}/atribuir", json={"motoboy_id": "Ana"}, headers=CHAVE)
    sql("UPDATE pedidos SET saiu_em = CURRENT_TIMESTAMP WHERE id = :p", p=a)  # Ana saiu para entregar
    novo_pedido(api, lat=-19.620, lng=-43.230)
    r = api.post("/despachar/previa", headers=CHAVE).json()
    sg = r["sugestoes"][0]
    assert sg["motoboy"] == "Carlos"
    assert sg["minutos"] >= 1 and sg["km"] > 0
    assert any("livre" in m for m in sg["motivos"])
    assert any("antes que Ana" in m for m in sg["motivos"])
    assert sorted(r["motoboys"]) == ["Ana", "Carlos"]


def test_escolher_outro_motoboy_na_previa(api):
    novo_motoboy(api, "Carlos")
    novo_motoboy(api, "Ana")
    a = novo_pedido(api)["id"]
    r = api.post("/despachar/previa", headers=CHAVE).json()
    escolhido = r["sugestoes"][0]["motoboy"]
    outro = "Ana" if escolhido == "Carlos" else "Carlos"
    api.post("/despachar", json={"plano": {outro: [[a]]}}, headers=CHAVE)
    assert rota(outro) == [a] and rota(escolhido) == []


def test_lista_de_motoboys_mostra_situacao(api):
    novo_motoboy(api, "Carlos")
    novo_motoboy(api, "Ana")
    novo_pedido(api)
    api.post("/pedidos/" + sql("SELECT id FROM pedidos").scalar() + "/atribuir", json={"motoboy_id": "Ana"}, headers=CHAVE)
    m = {x["id"]: x for x in api.get("/motoboys", headers=CHAVE).json()["motoboys"]}
    assert m["Carlos"]["situacao"] == "disponivel" and m["Carlos"]["km_rota"] == 0
    assert m["Ana"]["situacao"] == "saindo" and m["Ana"]["km_rota"] > 0 and m["Ana"]["minutos_livre"] >= 1
    api.post("/rotas/Ana/saiu", headers={"X-Codigo": CODIGO})
    assert api.get("/motoboys", headers=CHAVE).json()["motoboys"][0]["situacao"] == "em_rota"


# ---------------------------------------------------------------------
# Página do motoboy (versão "uma entrega por vez")
# ---------------------------------------------------------------------
def test_link_curto_abre_a_pagina_e_tem_manifesto(api):
    r = api.get("/m/Joao")
    assert r.status_code == 200 and "Minhas entregas" in r.text
    man = api.get("/m/Joao/app.webmanifest").json()
    assert man["start_url"] == "/m/Joao" and man["display"] == "standalone"
    assert api.get("/icone.svg").headers["content-type"].startswith("image/svg")


def test_rota_traz_cliente_tempos_e_entregas_do_dia(api):
    novo_motoboy(api, "Carlos")
    a = novo_pedido(api, lat=-19.630, lng=-43.240, cliente="Maria")["id"]
    b = novo_pedido(api, lat=-19.631, lng=-43.241)["id"]
    api.post("/despachar", headers=CHAVE)
    d = ver_rota(api, "Carlos").json()
    assert d["ativo"] is True and d["hoje"]["entregas"] == 0
    paradas = {p["id"]: p for p in d["paradas"]}
    assert paradas[a]["cliente"] == "Maria"
    assert all(p["minutos"] >= 1 for p in d["paradas"])
    api.post(f"/rotas/Carlos/entregar/{a}", headers={"X-Codigo": CODIGO})
    assert ver_rota(api, "Carlos").json()["hoje"]["entregas"] == 1
    assert [p["id"] for p in ver_rota(api, "Carlos").json()["paradas"]] == [b]


def test_parada_da_proxima_saida_avisa_que_passa_no_restaurante(api):
    novo_motoboy(api, "Carlos")
    novo_pedido(api, lat=-19.630, lng=-43.240)
    api.post("/despachar", headers=CHAVE)
    api.post("/rotas/Carlos/saiu", headers={"X-Codigo": CODIGO})
    novo_pedido(api, lat=-19.600, lng=-43.210)
    api.post("/despachar", headers=CHAVE)
    paradas = ver_rota(api, "Carlos").json()["paradas"]
    assert [p["volta_antes"] for p in paradas] == [False, True]


def test_motoboy_se_marca_disponivel_ou_encerra_turno(api):
    novo_motoboy(api, "Carlos")
    h = {"X-Codigo": CODIGO}
    assert api.post("/rotas/Carlos/disponivel", json={"ativo": False}, headers=h).status_code == 200
    assert not api.get("/motoboys", headers=CHAVE).json()["motoboys"][0]["ativo"]
    assert ver_rota(api, "Carlos").json()["ativo"] is False
    api.post("/rotas/Carlos/disponivel", json={"ativo": True}, headers=h)
    assert api.get("/motoboys", headers=CHAVE).json()["motoboys"][0]["ativo"]
    assert api.post("/rotas/Carlos/disponivel", json={"ativo": False}, headers={"X-Codigo": "errado"}).status_code == 401


def test_painel_mostra_ha_quanto_tempo_veio_o_gps(api):
    novo_motoboy(api, "Carlos")
    api.post("/rotas/Carlos/posicao", json={"lat": R["lat"] - 0.01, "lng": R["lng"]}, headers={"X-Codigo": CODIGO})
    m = api.get("/motoboys", headers=CHAVE).json()["motoboys"][0]
    assert m["gps_ativo"] is True and 0 <= m["gps_segundos"] < 30


# ---------------------------------------------------------------------
# Distância de agrupamento e economia em reais
# ---------------------------------------------------------------------
def test_distancia_de_agrupamento_ajustavel(api):
    novo_motoboy(api, "Carlos")
    novo_motoboy(api, "Ana")
    novo_pedido(api, lat=-19.630, lng=-43.240)
    novo_pedido(api, lat=-19.645, lng=-43.240)  # ~1,7 km do outro
    assert api.post("/despachar/previa", headers=CHAVE).json()["agrupou"] is True
    api.put("/ajustes", json={"raio_agrupamento_km": 1}, headers=CHAVE)
    r = api.post("/despachar/previa", headers=CHAVE).json()
    assert r["agrupou"] is False and r["viagens"] == 2
    assert all(not p["agrupar_com"] for p in api.get("/pedidos", headers=CHAVE).json()["pedidos"])


def test_economia_em_reais_e_porcentagem(api):
    novo_motoboy(api, "Carlos")
    a = novo_pedido(api, lat=-19.630, lng=-43.240)["id"]
    b = novo_pedido(api, lat=-19.631, lng=-43.241)["id"]
    previa = api.post("/despachar/previa", headers=CHAVE).json()
    assert previa["economia_reais"] is None and previa["economia_pct"] > 0
    api.put("/ajustes", json={"custo_km": 0.8}, headers=CHAVE)
    previa = api.post("/despachar/previa", headers=CHAVE).json()
    assert previa["economia_reais"] == round(previa["economia_km"] * 0.8, 2) or previa["economia_reais"] > 0
    api.post("/despachar", headers=CHAVE)
    for p in (a, b):
        api.post(f"/rotas/Carlos/entregar/{p}", headers={"X-Codigo": CODIGO})
    g = api.get("/estatisticas?dias=1", headers=CHAVE).json()["geral"]
    assert g["economia_pct"] > 0
    assert g["economia_reais"] == round(g["km_economizados"] * 0.8, 2)


# ---------------------------------------------------------------------
# Login com e-mail e senha
# ---------------------------------------------------------------------
SENHA = "senha-boa-123"


def criar_dono(api, email="dono@rest.com"):
    r = api.post("/auth/primeiro-acesso", json={"chave": os.environ["API_KEY"], "nome": "Dono", "email": email, "senha": SENHA})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def entrar(api, email, senha=SENHA):
    return api.post("/auth/entrar", json={"email": email, "senha": senha})


def test_primeiro_acesso_exige_a_chave_e_so_vale_uma_vez(api):
    assert api.get("/auth/estado").json()["tem_usuarios"] is False
    r = api.post("/auth/primeiro-acesso", json={"chave": "errada", "nome": "X", "email": "x@x.com", "senha": SENHA})
    assert r.status_code == 401
    criar_dono(api)
    assert api.get("/auth/estado").json()["tem_usuarios"] is True
    r = api.post("/auth/primeiro-acesso", json={"chave": os.environ["API_KEY"], "nome": "Y", "email": "y@y.com", "senha": SENHA})
    assert r.status_code == 409


def test_login_com_email_e_senha_abre_o_painel(api):
    criar_dono(api, "Dono@Rest.com")
    assert entrar(api, "dono@rest.com", "errada!!").status_code == 401
    r = entrar(api, "  DONO@rest.com ")
    assert r.status_code == 200 and r.json()["papel"] == "dono"
    h = {"Authorization": "Bearer " + r.json()["token"]}
    assert api.get("/pedidos", headers=h).status_code == 200
    assert api.get("/auth/eu", headers=h).json()["nome"] == "Dono"
    api.post("/auth/sair", headers=h)
    assert api.get("/pedidos", headers=h).status_code == 401
    assert api.get("/pedidos", headers={"Authorization": "Bearer inventado"}).status_code == 401


def test_senha_errada_varias_vezes_bloqueia(api):
    criar_dono(api)
    for _ in range(seguranca.LIMITE_POR_IP):
        entrar(api, "dono@rest.com", "errada!!")
    assert entrar(api, "dono@rest.com").status_code == 429


def test_senha_fica_guardada_com_hash(api):
    criar_dono(api)
    guardada = sql("SELECT senha FROM usuarios").scalar()
    assert guardada.startswith("pbkdf2$") and SENHA not in guardada
    assert len(sql("SELECT token FROM sessoes").scalar()) == 64  # só o hash do token


def test_funcionario_opera_mas_nao_muda_regras_nem_motoboys(api):
    dono = criar_dono(api)
    r = api.post("/usuarios", json={"nome": "Bia", "email": "bia@rest.com", "senha": SENHA, "papel": "funcionario"}, headers=dono)
    assert r.status_code == 200
    func = {"Authorization": "Bearer " + entrar(api, "bia@rest.com").json()["token"]}
    novo_motoboy(api, "Carlos")
    pid = novo_pedido(api)["id"]
    assert api.post("/despachar", headers=func).status_code == 200
    assert rota("Carlos") == [pid]
    assert api.post("/motoboys/Carlos/turno", json={"ativo": False}, headers=func).status_code == 200
    assert api.put("/ajustes", json={"max_paradas": 2}, headers=func).status_code == 403
    assert api.post("/motoboys", json={"id": "Z", "codigo": CODIGO, "lat": R["lat"], "lng": R["lng"]}, headers=func).status_code == 403
    assert api.delete("/motoboys/Carlos", headers=func).status_code == 403
    assert api.get("/usuarios", headers=func).status_code == 403
    assert api.put("/ajustes", json={"max_paradas": 2}, headers=dono).status_code == 200


def test_dono_redefine_senha_e_pessoa_e_desconectada(api):
    dono = criar_dono(api)
    api.post("/usuarios", json={"nome": "Bia", "email": "bia@rest.com", "senha": SENHA}, headers=dono)
    func = {"Authorization": "Bearer " + entrar(api, "bia@rest.com").json()["token"]}
    api.post("/usuarios/bia@rest.com/senha", json={"senha": "nova-senha-456"}, headers=dono)
    assert api.get("/pedidos", headers=func).status_code == 401
    assert entrar(api, "bia@rest.com", "nova-senha-456").status_code == 200


def test_administrador_com_a_chave_recupera_a_senha_do_dono(api):
    criar_dono(api)
    r = api.post("/usuarios/dono@rest.com/senha", json={"senha": "recuperada-789"}, headers=CHAVE)
    assert r.status_code == 200
    assert entrar(api, "dono@rest.com", "recuperada-789").status_code == 200


def test_trocar_a_propria_senha(api):
    dono = criar_dono(api)
    assert api.post("/auth/senha", json={"atual": "errada!!", "nova": "outra-senha-1"}, headers=dono).status_code == 400
    r = api.post("/auth/senha", json={"atual": SENHA, "nova": "outra-senha-1"}, headers=dono)
    assert r.status_code == 200
    assert api.get("/pedidos", headers=dono).status_code == 401  # sessões antigas encerradas
    assert api.get("/pedidos", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 200


def test_nao_remove_a_si_mesmo_nem_o_unico_dono(api):
    dono = criar_dono(api)
    assert api.delete("/usuarios/dono@rest.com", headers=dono).status_code == 400
    assert api.delete("/usuarios/dono@rest.com", headers=CHAVE).status_code == 400
    api.post("/usuarios", json={"nome": "Bia", "email": "bia@rest.com", "senha": SENHA}, headers=dono)
    assert api.delete("/usuarios/bia@rest.com", headers=dono).status_code == 200


def test_senha_curta_e_email_invalido(api):
    dono = criar_dono(api)
    assert api.post("/usuarios", json={"nome": "B", "email": "b@r.com", "senha": "123"}, headers=dono).status_code == 422
    assert api.post("/usuarios", json={"nome": "B", "email": "semarroba", "senha": SENHA}, headers=dono).status_code == 422


# ---------------------------------------------------------------------
# Convites por link
# ---------------------------------------------------------------------
def test_administrador_convida_o_dono_e_o_link_vale_uma_vez(api):
    c = api.post("/convites", json={"papel": "dono"}, headers=CHAVE).json()["convite"]
    assert api.get("/auth/convite", params={"convite": c}).json() == {"valido": True, "papel": "dono"}
    r = api.post("/auth/aceitar-convite", json={"convite": c, "nome": "Rafa", "email": "rafa@r.com", "senha": SENHA})
    assert r.status_code == 200 and r.json()["papel"] == "dono"
    assert api.get("/pedidos", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 200
    assert api.get("/auth/convite", params={"convite": c}).json()["valido"] is False
    r = api.post("/auth/aceitar-convite", json={"convite": c, "nome": "Outro", "email": "o@r.com", "senha": SENHA})
    assert r.status_code == 410
    assert "convite" not in sql("SELECT token FROM convites").scalar()  # só o hash


def test_dono_convida_funcionario_mas_nao_outro_dono(api):
    dono = criar_dono(api)
    assert api.post("/convites", json={"papel": "dono"}, headers=dono).status_code == 403
    assert api.post("/usuarios", json={"nome": "D2", "email": "d2@r.com", "senha": SENHA, "papel": "dono"}, headers=dono).status_code == 403
    c = api.post("/convites", json={"papel": "funcionario"}, headers=dono).json()["convite"]
    r = api.post("/auth/aceitar-convite", json={"convite": c, "nome": "Bia", "email": "bia@r.com", "senha": SENHA})
    assert r.json()["papel"] == "funcionario"


def test_convite_vencido_ou_inventado_nao_vale(api):
    c = api.post("/convites", json={"papel": "funcionario"}, headers=CHAVE).json()["convite"]
    sql("UPDATE convites SET expira_em = :t", t=horarios.agora_utc() - timedelta(minutes=1))
    assert api.get("/auth/convite", params={"convite": c}).json()["valido"] is False
    r = api.post("/auth/aceitar-convite", json={"convite": "inventado-123456", "nome": "X", "email": "x@r.com", "senha": SENHA})
    assert r.status_code == 410
    assert api.post("/convites", json={"papel": "funcionario"}).status_code == 401
