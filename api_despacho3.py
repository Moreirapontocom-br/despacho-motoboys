"""
API de despacho automático de motoboys - versão 3 (com banco de dados e segurança).

Variáveis de ambiente (configuradas no painel do Render, nunca dentro do código):
    DATABASE_URL  endereço do banco Postgres. Se não existir, usa um arquivo
                  SQLite local (só para testes no seu computador).
    RESTAURANTE_LAT / RESTAURANTE_LNG / RESTAURANTE_ENDERECO  (opcionais) local do restaurante.
    LOCATIONIQ_KEY (recomendado para uso comercial) chave da LocationIQ. Com ela, busca de
                  endereço, rotas pelas ruas e imagens do mapa passam a usar a LocationIQ,
                  que permite uso comercial no plano gratuito (com o crédito na tela).
                  Sem ela, o sistema usa os serviços públicos gratuitos (Nominatim, OSRM
                  e OpenStreetMap), que servem só para testes.
    OSRM_URL      (opcional) serviço de rotas pelas ruas. Padrão: servidor público de demonstração
                  do OSRM (uso não comercial, sem garantia). Use "desligado" para só linha reta.
    API_KEY       chave secreta exigida em /pedidos, /motoboys, /despachar e /status.
                  Deve ser enviada no cabeçalho  X-API-Key.

O motoboy não usa a API_KEY. Cada motoboy tem um código próprio (definido ao
cadastrá-lo) e só consegue ver a rota dele, enviando esse código no cabeçalho X-Codigo.
"""

import csv
import io
import itertools
import json
import logging
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

app = FastAPI(title="API de Despacho Automático")

def _coord(nome, padrao):
    """Lê uma coordenada das variáveis de ambiente (aceita vírgula ou ponto decimal)."""
    try:
        return float(os.environ.get(nome, "").strip().replace(",", ".") or padrao)
    except ValueError:
        return padrao


# Local do restaurante. Para mudar, defina RESTAURANTE_LAT e RESTAURANTE_LNG no Render.
# RESTAURANTE_ENDERECO (opcional): endereço em texto, usado como ponto de partida no Google Maps.
RESTAURANTE = {"lat": _coord("RESTAURANTE_LAT", -19.6156), "lng": _coord("RESTAURANTE_LNG", -43.2258),
               "endereco": os.environ.get("RESTAURANTE_ENDERECO", "").strip()}
API_KEY = os.environ.get("API_KEY", "")

# Por quantos minutos a posição de GPS de um motoboy é considerada atual para o despacho.
# Passado esse tempo, volta a usar a posição do cadastro dele.
GPS_VALIDADE_MIN = int(_coord("GPS_VALIDADE_MIN", 10))

# Fuso horário do restaurante, em horas de diferença para o UTC (o banco guarda tudo em UTC).
# O Brasil não tem mais horário de verão desde 2019, então um número fixo é suficiente.
# Para mudar (outro estado, por exemplo), crie TIMEZONE_OFFSET_HORAS no Render.
TIMEZONE_OFFSET_HORAS = _coord("TIMEZONE_OFFSET_HORAS", -3)

# Depois de quantos minutos pendente (sem despachar) um pedido é marcado como atrasado no painel.
PEDIDO_ATRASO_MIN = _coord("PEDIDO_ATRASO_MIN", 15)


def _parse_ts(v):
    """Converte um horário vindo do banco (datetime já pronto, ou texto no caso do
    SQLite de testes/uso local) para um datetime em UTC ingênuo (sem timezone)."""
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace(" ", "T", 1))
    if v.tzinfo:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _gps_recente(gps_em):
    """True se o horário do último GPS (vindo do banco) está dentro da validade.
    Tolera pequenas diferenças de fuso entre bancos: se a conta der negativa
    (relógio do banco um pouco à frente), ainda considera recente."""
    t = _parse_ts(gps_em)
    if t is None:
        return False
    try:
        return abs((datetime.utcnow() - t).total_seconds()) <= GPS_VALIDADE_MIN * 60
    except Exception:
        return False

# ---------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///despacho_local.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

with engine.begin() as con:
    con.execute(text("""
        CREATE TABLE IF NOT EXISTS motoboys (
            id TEXT PRIMARY KEY,
            codigo TEXT NOT NULL,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            ativo BOOLEAN NOT NULL DEFAULT TRUE,
            gps_lat DOUBLE PRECISION,
            gps_lng DOUBLE PRECISION,
            gps_em TIMESTAMP
        )"""))

# Migração: bancos criados por uma versão anterior deste código não têm estas colunas.
# Cada uma numa transação própria: se já existir, o erro não afeta as demais.
for _coluna in ("ativo BOOLEAN NOT NULL DEFAULT TRUE", "gps_lat DOUBLE PRECISION",
                "gps_lng DOUBLE PRECISION", "gps_em TIMESTAMP"):
    try:
        with engine.begin() as con:
            con.execute(text(f"ALTER TABLE motoboys ADD COLUMN {_coluna}"))
    except Exception:
        pass

with engine.begin() as con:
    con.execute(text("""
        CREATE TABLE IF NOT EXISTS pedidos (
            id TEXT PRIMARY KEY,
            endereco TEXT NOT NULL,
            lat DOUBLE PRECISION NOT NULL,
            lng DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL DEFAULT 'pendente',
            motoboy_id TEXT,
            ordem INTEGER,
            criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            concluido_em TIMESTAMP
        )"""))

# Migração da tabela de pedidos. As colunas de endereço separado (rua, número,
# bairro...) são opcionais: pedidos antigos ficam só com "endereco" e continuam funcionando.
CAMPOS_ENDERECO = ("rua", "numero", "bairro", "complemento", "referencia", "cep")
for _coluna in ["concluido_em TIMESTAMP", "telefone TEXT", "cancelado_em TIMESTAMP"] + [f"{c} TEXT" for c in CAMPOS_ENDERECO]:
    try:
        with engine.begin() as con:
            con.execute(text(f"ALTER TABLE pedidos ADD COLUMN {_coluna}"))
    except Exception:
        pass


# ---------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------
# Impede que despacho, atribuição, edição e cancelamento mexam nos mesmos pedidos ao mesmo tempo.
_trava_despacho = threading.Lock()

# Limite de tentativas erradas (chave do painel e código do motoboy).
# Fica na memória do servidor: se ele reiniciar, os contadores zeram (aceitável).
# Contamos por endereço de internet (IP) e, no caso do motoboy, também por nome,
# com um limite maior, para que alguém tentando de vários IPs também seja barrado.
JANELA_TENTATIVAS_S = 10 * 60   # conta os erros dos últimos 10 minutos
BLOQUEIO_S = 15 * 60            # tempo de bloqueio depois de passar do limite
LIMITE_POR_IP = 8
LIMITE_POR_MOTOBOY = 30
_trava_tentativas = threading.Lock()
_erros = {}          # chave -> lista de horários dos erros
_bloqueado_ate = {}  # chave -> horário em que o bloqueio acaba


def _ip(request: Request):
    """IP de quem chamou. No Render, o IP real vem no cabeçalho X-Forwarded-For."""
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _checar_bloqueio(*chaves):
    agora = time.time()
    with _trava_tentativas:
        restante = max((_bloqueado_ate.get(k, 0) - agora for k in chaves), default=0)
    if restante > 0:
        minutos = math.ceil(restante / 60)
        raise HTTPException(status_code=429,
                            detail=f"Muitas tentativas erradas. Espere {minutos} minuto(s) e tente de novo.")


def _registrar_erro(chave, limite):
    agora = time.time()
    with _trava_tentativas:
        lista = [t for t in _erros.get(chave, []) if agora - t < JANELA_TENTATIVAS_S]
        lista.append(agora)
        _erros[chave] = lista
        if len(lista) >= limite:
            _bloqueado_ate[chave] = agora + BLOQUEIO_S
            _erros.pop(chave, None)
        if len(_erros) > 5000:  # limpeza para a memória não crescer sem fim
            for k in [k for k, v in _erros.items() if agora - v[-1] > JANELA_TENTATIVAS_S]:
                _erros.pop(k, None)
            for k in [k for k, t in _bloqueado_ate.items() if t < agora]:
                _bloqueado_ate.pop(k, None)


def exigir_chave(request: Request, x_api_key: str = Header(default="")):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY não configurada no servidor")
    chave_ip = "painel-ip:" + _ip(request)
    _checar_bloqueio(chave_ip)
    if not secrets.compare_digest(x_api_key.encode(), API_KEY.encode()):
        _registrar_erro(chave_ip, LIMITE_POR_IP)
        raise HTTPException(status_code=401, detail="Chave de API inválida ou ausente")


def conferir_codigo(con, request: Request, motoboy_id: str, codigo: str):
    """Confere o código do motoboy, com limite de tentativas erradas."""
    chave_ip, chave_moto = "moto-ip:" + _ip(request), "moto:" + motoboy_id.lower()
    _checar_bloqueio(chave_ip, chave_moto)
    m = con.execute(text("SELECT codigo FROM motoboys WHERE id = :id"), {"id": motoboy_id}).first()
    if m is None or not secrets.compare_digest(codigo.encode(), m[0].encode()):
        _registrar_erro(chave_ip, LIMITE_POR_IP)
        _registrar_erro(chave_moto, LIMITE_POR_MOTOBOY)
        raise HTTPException(status_code=401, detail="Nome ou código incorretos")


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
    id: str = Field(min_length=1, max_length=64)


def _limpar_dados(con, dados):
    """Campos vazios viram NULL. Se o bairro já foi usado antes com outra grafia de
    maiúsculas/minúsculas ("amazonas" x "Amazonas"), usa a grafia já salva."""
    for c in CAMPOS_ENDERECO + ("telefone",):
        dados[c] = (dados.get(c) or "").strip() or None
    if dados["bairro"]:
        existente = con.execute(text(
            "SELECT bairro FROM pedidos WHERE LOWER(bairro) = LOWER(:b) LIMIT 1"), {"b": dados["bairro"]}).scalar()
        if existente and existente != dados["bairro"]:
            # Corrige também no endereço de uma linha (formato "Rua, Nº - Bairro, Cidade").
            dados["endereco"] = dados["endereco"].replace(f" - {dados['bairro']},", f" - {existente},", 1)
            dados["bairro"] = existente
    return dados


class Motoboy(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    codigo: str = Field(min_length=4, max_length=64)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


# ---------------------------------------------------------------------
# Algoritmo (igual ao das versões anteriores)
# ---------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


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


# ---------------------------------------------------------------------
# Rotas pelas ruas (OSRM). Se o serviço falhar, tudo continua em linha reta.
# ---------------------------------------------------------------------
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").strip().rstrip("/")
# Chave da LocationIQ (opcional). Quando existe, rotas, busca de endereço e mapa usam a LocationIQ.
LOCATIONIQ_KEY = os.environ.get("LOCATIONIQ_KEY", "").strip()
LOCATIONIQ_URL = "https://us1.locationiq.com/v1"
# Intervalo mínimo entre consultas: o plano gratuito da LocationIQ aceita 2 por segundo;
# os serviços públicos pedem no máximo 1 por segundo.
INTERVALO_CONSULTA_S = 0.6 if LOCATIONIQ_KEY else 1.1
MAX_PONTOS_MATRIZ = 25 if LOCATIONIQ_KEY else 90  # a matriz da LocationIQ aceita até 25 pontos

# Imagens do mapa. Ao contrário das outras consultas, estas são pedidas pelo navegador,
# então a chave fica visível na página. Por isso existe LOCATIONIQ_MAPA_KEY: uma segunda
# chave, travada no painel da LocationIQ para funcionar só no endereço do seu site.
# Se não existir, usa LOCATIONIQ_KEY mesmo.
_chave_mapa = os.environ.get("LOCATIONIQ_MAPA_KEY", "").strip() or LOCATIONIQ_KEY
if _chave_mapa:
    MAPA_URL = "https://{s}-tiles.locationiq.com/v3/streets/r/{z}/{x}/{y}.png?key=" + urllib.parse.quote(_chave_mapa)
    MAPA_CREDITO = ('<a href="https://locationiq.com" target="_blank">Search by LocationIQ.com</a> | '
                    '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>')
else:
    MAPA_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    MAPA_CREDITO = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> | rotas: OSRM'


def _com_mapa(html):
    """Coloca o endereço das imagens do mapa e o crédito obrigatório dentro da página."""
    return html.replace("<script>\n", "<script>\nconst MAPA_URL = " + json.dumps(MAPA_URL)
                        + ";\nconst MAPA_CREDITO = " + json.dumps(MAPA_CREDITO) + ";\n", 1)
_trava_osrm = threading.Lock()
_ultimo_osrm = 0.0
_osrm_falha_ate = 0.0
_cache_trajetos = {}


def _osrm_get(tipo, coords, extra):
    """Consulta o serviço de rotas. tipo: "matriz" ou "rota". Devolve o JSON ou None se falhar.
    Respeita o limite de consultas por segundo e, depois de uma falha, não tenta de novo por 60 s.
    A LocationIQ usa o mesmo formato de resposta do OSRM, só muda o endereço."""
    global _ultimo_osrm, _osrm_falha_ate
    if OSRM_URL.lower() in ("off", "desligado"):
        return None
    if LOCATIONIQ_KEY:
        servico = "matrix" if tipo == "matriz" else "directions"
        url = f"{LOCATIONIQ_URL}/{servico}/driving/{coords}?{extra}&key={urllib.parse.quote(LOCATIONIQ_KEY)}"
    elif OSRM_URL:
        servico = "table" if tipo == "matriz" else "route"
        url = f"{OSRM_URL}/{servico}/v1/driving/{coords}?{extra}"
    else:
        return None
    if time.time() < _osrm_falha_ate:
        return None
    with _trava_osrm:
        espera = INTERVALO_CONSULTA_S - (time.time() - _ultimo_osrm)
        if espera > 0:
            time.sleep(espera)
        _ultimo_osrm = time.time()
    req = urllib.request.Request(url,
                                 headers={"User-Agent": "despacho-motoboys/1.0 (painel de restaurante pequeno)"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            dados = json.load(resp)
        if dados.get("code") != "Ok":
            raise ValueError(dados.get("code"))
        return dados
    except Exception as e:
        logging.getLogger("uvicorn.error").warning("Falha no serviço de rotas: %r", e)
        _osrm_falha_ate = time.time() + 60
        return None


def _coords(pontos):
    return ";".join(f"{p['lng']:.6f},{p['lat']:.6f}" for p in pontos)


def matriz_duracoes(pontos):
    """Tempo de carro (segundos) entre todos os pares de pontos, pelas ruas. None se indisponível."""
    if len(pontos) < 2 or len(pontos) > MAX_PONTOS_MATRIZ:
        return None
    dados = _osrm_get("matriz", _coords(pontos), "annotations=duration")
    matriz = dados.get("durations") if dados else None
    if not matriz or len(matriz) != len(pontos):
        return None
    return matriz


def _custo_reta(a, b):
    """Estimativa em segundos pela linha reta (cerca de 30 km/h), usada quando as ruas não estão disponíveis."""
    return haversine(a["lat"], a["lng"], b["lat"], b["lng"]) * 120


def ordenar_rota(origem, pedidos):
    """Melhor ordem das paradas saindo do restaurante (menor tempo total).
    Até 7 paradas testa todas as ordens; acima disso usa o vizinho mais próximo."""
    if len(pedidos) <= 1:
        return pedidos.copy()
    pontos = [origem] + pedidos
    mat = matriz_duracoes(pontos)

    def custo(i, j):
        if mat is not None and mat[i][j] is not None:
            return mat[i][j]
        return _custo_reta(pontos[i], pontos[j])

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


def trajeto(origem, paradas):
    """Linha do caminho pelas ruas (lista de [lat, lng]) para desenhar no mapa. None se indisponível.
    O resultado fica guardado enquanto as paradas não mudam, para não consultar a cada atualização."""
    pontos = [origem] + list(paradas)
    if len(pontos) < 2 or len(pontos) > 25:
        return None
    chave = _coords(pontos)
    if chave in _cache_trajetos:
        return _cache_trajetos[chave]
    dados = _osrm_get("rota", chave, "overview=simplified&geometries=geojson")
    if not dados or not dados.get("routes"):
        return None
    linha = [[round(lat, 5), round(lng, 5)] for lng, lat in dados["routes"][0]["geometry"]["coordinates"]]
    if len(_cache_trajetos) > 200:
        _cache_trajetos.clear()
    _cache_trajetos[chave] = linha
    return linha


def atribuir_motoboys(lotes, motoboys, carga_inicial=None):
    carga = {m["id"]: (carga_inicial or {}).get(m["id"], 0) for m in motoboys}
    atribuicoes = {m["id"]: [] for m in motoboys}
    for lote in lotes:
        primeiro = lote[0]
        escolhido = min(motoboys, key=lambda m: (carga[m["id"]], haversine(m["lat"], m["lng"], primeiro["lat"], primeiro["lng"])))
        atribuicoes[escolhido["id"]].extend(lote)
        carga[escolhido["id"]] += len(lote)
    return atribuicoes


# ---------------------------------------------------------------------
# Endpoints protegidos pela chave (usados pelo restaurante/conectores)
# ---------------------------------------------------------------------
@app.post("/pedidos", dependencies=[Depends(exigir_chave)])
def criar_pedido(pedido: Pedido):
    """Registra um pedido novo. Se o id já existir, ele é ignorado (evita duplicar)."""
    with engine.begin() as con:
        dados = _limpar_dados(con, pedido.model_dump())
        r = con.execute(text(
            "INSERT INTO pedidos (id, endereco, lat, lng, rua, numero, bairro, complemento, referencia, cep, telefone) "
            "VALUES (:id, :endereco, :lat, :lng, :rua, :numero, :bairro, :complemento, :referencia, :cep, :telefone) "
            "ON CONFLICT (id) DO NOTHING"), dados)
        criado = r.rowcount == 1
        total = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'")).scalar()
    return {"mensagem": "Pedido recebido" if criado else "Pedido já existia (ignorado)", "total_pendentes": total}


@app.put("/pedidos/{pedido_id}", dependencies=[Depends(exigir_chave)])
def editar_pedido(pedido_id: str, pedido: DadosPedido):
    """Corrige endereço, ponto no mapa ou telefone de um pedido que ainda está na fila.
    Pedido já despachado não pode ser editado: cancele e crie de novo, para a rota
    do motoboy não mudar sem ele perceber."""
    if not _trava_despacho.acquire(timeout=15):
        raise HTTPException(status_code=503, detail="Já existe um despacho em andamento. Tente de novo em alguns segundos.")
    try:
        with engine.begin() as con:
            dados = _limpar_dados(con, pedido.model_dump())
            dados["id"] = pedido_id
            r = con.execute(text(
                "UPDATE pedidos SET endereco = :endereco, lat = :lat, lng = :lng, rua = :rua, numero = :numero, "
                "bairro = :bairro, complemento = :complemento, referencia = :referencia, cep = :cep, telefone = :telefone "
                "WHERE id = :id AND status = 'pendente'"), dados)
            if r.rowcount == 0:
                raise HTTPException(status_code=409, detail="Esse pedido não está mais na fila (já foi despachado, entregue ou cancelado).")
        return {"mensagem": "Pedido atualizado"}
    finally:
        _trava_despacho.release()


@app.post("/pedidos/{pedido_id}/cancelar", dependencies=[Depends(exigir_chave)])
def cancelar_pedido(pedido_id: str):
    """Cancela um pedido na fila ou já em rota. Em rota, ele some da tela do motoboy
    na próxima atualização (5 s). O pedido fica guardado no banco como 'cancelado'."""
    if not _trava_despacho.acquire(timeout=15):
        raise HTTPException(status_code=503, detail="Já existe um despacho em andamento. Tente de novo em alguns segundos.")
    try:
        with engine.begin() as con:
            atual = con.execute(text("SELECT status, motoboy_id FROM pedidos WHERE id = :id"), {"id": pedido_id}).first()
            if atual is None or atual[0] not in ("pendente", "despachado"):
                raise HTTPException(status_code=409, detail="Esse pedido não pode mais ser cancelado (já foi entregue ou cancelado).")
            con.execute(text("UPDATE pedidos SET status = 'cancelado', cancelado_em = CURRENT_TIMESTAMP WHERE id = :id"),
                        {"id": pedido_id})
        aviso = f" Avise {atual[1]}: a parada já saiu da rota dele." if atual[0] == "despachado" else ""
        return {"mensagem": f"Pedido {pedido_id} cancelado.{aviso}"}
    finally:
        _trava_despacho.release()


@app.post("/motoboys", dependencies=[Depends(exigir_chave)])
def cadastrar_motoboy(motoboy: Motoboy):
    """Cadastra o motoboy ou atualiza a localização/código dele se já existir."""
    with engine.begin() as con:
        con.execute(text(
            "INSERT INTO motoboys (id, codigo, lat, lng) VALUES (:id, :codigo, :lat, :lng) "
            "ON CONFLICT (id) DO UPDATE SET codigo = excluded.codigo, lat = excluded.lat, lng = excluded.lng"),
            motoboy.model_dump())
        total = con.execute(text("SELECT COUNT(*) FROM motoboys")).scalar()
    return {"mensagem": "Motoboy disponível", "total_disponiveis": total}


@app.get("/motoboys", dependencies=[Depends(exigir_chave)])
def listar_motoboys():
    """Lista os motoboys (sem os códigos), quantas paradas cada um tem em aberto,
    e se a posição de GPS dele está ativa agora (enviada há menos de GPS_VALIDADE_MIN minutos)."""
    with engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT m.id, m.ativo, m.gps_em, "
            "(SELECT COUNT(*) FROM pedidos p WHERE p.motoboy_id = m.id AND p.status = 'despachado') AS paradas "
            "FROM motoboys m ORDER BY m.id"))]
    for m in linhas:
        m["gps_ativo"] = _gps_recente(m.pop("gps_em"))
    return {"motoboys": linhas, "centro": RESTAURANTE}


@app.delete("/motoboys/{motoboy_id}", dependencies=[Depends(exigir_chave)])
def remover_motoboy(motoboy_id: str):
    """Remove o motoboy. As paradas que ele ainda não entregou voltam para a fila."""
    with engine.begin() as con:
        r = con.execute(text("DELETE FROM motoboys WHERE id = :id"), {"id": motoboy_id})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Motoboy não encontrado")
        devolvidos = con.execute(text(
            "UPDATE pedidos SET status = 'pendente', motoboy_id = NULL, ordem = NULL "
            "WHERE motoboy_id = :id AND status = 'despachado'"), {"id": motoboy_id}).rowcount
    return {"mensagem": "Motoboy removido", "pedidos_devolvidos": devolvidos}


class Turno(BaseModel):
    ativo: bool


@app.post("/motoboys/{motoboy_id}/turno", dependencies=[Depends(exigir_chave)])
def definir_turno(motoboy_id: str, t: Turno):
    """Liga ou desliga o motoboy. Desligar não mexe nas paradas que ele já tem;
    para devolvê-las à fila, use remover ou o botão Entregue de cada uma."""
    with engine.begin() as con:
        r = con.execute(text("UPDATE motoboys SET ativo = :a WHERE id = :id"), {"a": t.ativo, "id": motoboy_id})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Motoboy não encontrado")
    return {"mensagem": "Turno atualizado"}




@app.post("/despachar", dependencies=[Depends(exigir_chave)])
def despachar():
    """Agrupa os pedidos pendentes, monta as rotas e distribui entre os motoboys.
    As paradas novas entram no fim da rota de cada motoboy; as que ele ainda não
    marcou como entregues continuam na rota dele.

    _trava_despacho impede que dois cliques em "Despachar agora" (ou dois pedidos
    simultâneos) rodem ao mesmo tempo e embaralhem as rotas um do outro. Isso
    funciona porque o servidor roda numa única cópia (um "worker"); se um dia o
    Render for configurado para rodar várias cópias ao mesmo tempo, essa trava
    precisaria virar uma trava no próprio banco de dados."""
    if not _trava_despacho.acquire(timeout=15):
        raise HTTPException(status_code=503, detail="Já existe um despacho em andamento. Tente de novo em alguns segundos.")
    try:
        with engine.begin() as con:
            pedidos = [dict(r._mapping) for r in con.execute(text(
                "SELECT id, endereco, lat, lng FROM pedidos WHERE status = 'pendente' ORDER BY criado_em, id"))]
            motoboys_raw = [dict(r._mapping) for r in con.execute(text(
                "SELECT id, lat, lng, gps_lat, gps_lng, gps_em FROM motoboys WHERE ativo"))]
            motoboys = [{"id": m["id"],
                         "lat": m["gps_lat"] if _gps_recente(m["gps_em"]) else m["lat"],
                         "lng": m["gps_lng"] if _gps_recente(m["gps_em"]) else m["lng"]}
                        for m in motoboys_raw]
            if not pedidos:
                return {"mensagem": "Não há pedidos pendentes para despachar."}
            if not motoboys:
                return {"mensagem": "Nenhum motoboy de turno agora. Ligue o turno de alguém no painel."}

            existentes = {r[0]: (r[1], r[2]) for r in con.execute(text(
                "SELECT motoboy_id, COUNT(*), COALESCE(MAX(ordem), 0) FROM pedidos "
                "WHERE status = 'despachado' GROUP BY motoboy_id"))}
            carga_inicial = {m: n for m, (n, _) in existentes.items()}
            atribuicoes = atribuir_motoboys(agrupar_pedidos(pedidos), motoboys, carga_inicial)
            resultado = {}
            for motoboy_id, lista in atribuicoes.items():
                if not lista:
                    continue
                rota = ordenar_rota(RESTAURANTE, lista)
                inicio = existentes.get(motoboy_id, (0, 0))[1]
                for ordem, p in enumerate(rota, start=inicio + 1):
                    con.execute(text("UPDATE pedidos SET status = 'despachado', motoboy_id = :m, ordem = :o WHERE id = :id"),
                                {"m": motoboy_id, "o": ordem, "id": p["id"]})
                resultado[motoboy_id] = rota
        return {"rotas": resultado}
    finally:
        _trava_despacho.release()


class Atribuicao(BaseModel):
    motoboy_id: str = Field(min_length=1)


@app.post("/pedidos/{pedido_id}/atribuir", dependencies=[Depends(exigir_chave)])
def atribuir_manual(pedido_id: str, corpo: Atribuicao):
    """Manda um pedido pendente direto para um motoboy escolhido, pulando o
    agrupamento automático. A parada entra na melhor posição da rota que esse
    motoboy já tem (recalculada), não necessariamente no fim. Funciona mesmo
    se o motoboy estiver fora de turno, já que é uma escolha manual do restaurante."""
    if not _trava_despacho.acquire(timeout=15):
        raise HTTPException(status_code=503, detail="Já existe um despacho em andamento. Tente de novo em alguns segundos.")
    try:
        with engine.begin() as con:
            pedido = con.execute(text("SELECT id, endereco, lat, lng FROM pedidos WHERE id = :id AND status = 'pendente'"),
                                  {"id": pedido_id}).first()
            if pedido is None:
                raise HTTPException(status_code=404, detail="Pedido não encontrado ou já não está mais pendente")
            if con.execute(text("SELECT 1 FROM motoboys WHERE id = :id"), {"id": corpo.motoboy_id}).first() is None:
                raise HTTPException(status_code=404, detail="Motoboy não encontrado")

            ja_tem = [dict(r._mapping) for r in con.execute(text(
                "SELECT id, endereco, lat, lng FROM pedidos WHERE motoboy_id = :m AND status = 'despachado'"),
                {"m": corpo.motoboy_id})]
            novo = {"id": pedido[0], "endereco": pedido[1], "lat": pedido[2], "lng": pedido[3]}
            rota = ordenar_rota(RESTAURANTE, ja_tem + [novo])
            for ordem, p in enumerate(rota, start=1):
                con.execute(text("UPDATE pedidos SET status = 'despachado', motoboy_id = :m, ordem = :o WHERE id = :id"),
                            {"m": corpo.motoboy_id, "o": ordem, "id": p["id"]})
        return {"mensagem": "Pedido atribuído", "rota": rota}
    finally:
        _trava_despacho.release()


@app.get("/status", dependencies=[Depends(exigir_chave)])
def status():
    with engine.connect() as con:
        por_status = {s: n for s, n in con.execute(text("SELECT status, COUNT(*) FROM pedidos GROUP BY status"))}
        motoboys = con.execute(text("SELECT COUNT(*) FROM motoboys")).scalar()
    return {"pedidos_por_status": por_status, "motoboys_cadastrados": motoboys}


@app.get("/resumo", dependencies=[Depends(exigir_chave)])
def resumo():
    """Números do dia para o painel: pendentes, em rota, entregues hoje e o tempo
    médio entre o pedido ser criado e ser marcado como entregue. "Hoje" usa o
    fuso do restaurante (TIMEZONE_OFFSET_HORAS), não o horário do banco (UTC).
    Pedidos entregues antes desta atualização não têm concluido_em salvo e por
    isso não entram na conta de "hoje" nem na média, mesmo que tenham sido
    entregues no dia — só afeta o dia da migração, não o uso normal depois dela."""
    agora_local = datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET_HORAS)
    inicio_local = agora_local.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_utc = inicio_local - timedelta(hours=TIMEZONE_OFFSET_HORAS)
    with engine.connect() as con:
        pendentes = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'")).scalar()
        em_rota = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE status = 'despachado'")).scalar()
        linhas = con.execute(text(
            "SELECT criado_em, concluido_em FROM pedidos WHERE status = 'concluido' AND concluido_em IS NOT NULL")).all()
    entregues_hoje, soma_minutos = 0, 0.0
    for criado, concluido in linhas:
        concluido_t = _parse_ts(concluido)
        if concluido_t is None or concluido_t < inicio_utc:
            continue
        entregues_hoje += 1
        criado_t = _parse_ts(criado)
        if criado_t is not None:
            soma_minutos += (concluido_t - criado_t).total_seconds() / 60
    tempo_medio_min = round(soma_minutos / entregues_hoje, 1) if entregues_hoje else None
    return {"pendentes": pendentes, "em_rota": em_rota, "entregues_hoje": entregues_hoje,
            "tempo_medio_min": tempo_medio_min}


def _fmt_local(dt_utc):
    """Formata um datetime UTC (ou None) no horário local do restaurante, como texto."""
    if dt_utc is None:
        return ""
    return (dt_utc + timedelta(hours=TIMEZONE_OFFSET_HORAS)).strftime("%d/%m/%Y %H:%M")


@app.get("/historico.csv", dependencies=[Depends(exigir_chave)])
def historico_csv(dias: int = 30):
    """Baixa em CSV as entregas concluídas dos últimos N dias (30 por padrão, no
    máximo 365), para abrir no Excel ou guardar. Pedidos concluídos antes desta
    atualização não têm concluido_em salvo e por isso não aparecem aqui."""
    dias = max(1, min(dias, 365))
    agora_local = datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET_HORAS)
    inicio_local = (agora_local - timedelta(days=dias - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_utc = inicio_local - timedelta(hours=TIMEZONE_OFFSET_HORAS)
    with engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, endereco, bairro, complemento, referencia, telefone, motoboy_id, criado_em, concluido_em FROM pedidos "
            "WHERE status = 'concluido' AND concluido_em IS NOT NULL ORDER BY concluido_em"))]

    saida = io.StringIO()
    saida.write("\ufeff")  # marca de ordem de bytes: sem isso o Excel no Windows pode exibir acentos errados
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow(["Pedido", "Endereço", "Bairro", "Complemento", "Referência", "Telefone",
                       "Motoboy", "Criado em", "Entregue em", "Tempo (min)"])
    for p in linhas:
        concluido_t = _parse_ts(p["concluido_em"])
        if concluido_t is None or concluido_t < inicio_utc:
            continue
        criado_t = _parse_ts(p["criado_em"])
        tempo = round((concluido_t - criado_t).total_seconds() / 60, 1) if criado_t is not None else ""
        escritor.writerow([p["id"], p["endereco"], p["bairro"] or "", p["complemento"] or "",
                           p["referencia"] or "", p["telefone"] or "", p["motoboy_id"] or "",
                            _fmt_local(criado_t), _fmt_local(concluido_t), tempo])

    nome_arquivo = f"historico_{agora_local.strftime('%Y-%m-%d')}.csv"
    return Response(content=saida.getvalue(), media_type="text/csv; charset=utf-8",
                     headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'})


# ---------------------------------------------------------------------
# Painel do restaurante (protegido pela API_KEY digitada na própria página)
# ---------------------------------------------------------------------
class Consulta(BaseModel):
    """Busca de endereço. "endereco" é o texto livre (jeito antigo, ainda aceito).
    Se "rua" vier preenchida, tenta primeiro a busca por campos separados, que
    costuma acertar mais, e completa com a busca em texto livre."""
    endereco: str = Field(min_length=3, max_length=300)
    rua: Optional[str] = Field(default=None, max_length=150)
    numero: Optional[str] = Field(default=None, max_length=20)
    cidade: Optional[str] = Field(default=None, max_length=80)
    estado: Optional[str] = Field(default=None, max_length=40)
    cep: Optional[str] = Field(default=None, max_length=12)


_trava_geo = threading.Lock()
_ultimo_geo = 0.0


def buscar_endereco(consulta=None, campos=None):
    """Endereço -> lista de lat/lng candidatos (LocationIQ se houver chave; senão Nominatim/OpenStreetMap).
    consulta: texto livre. campos: dict com street/city/state/postalcode (busca estruturada).
    O Nominatim público pede no máximo 1 consulta por segundo e um User-Agent que
    identifique o aplicativo; ambos são respeitados aqui."""
    global _ultimo_geo
    with _trava_geo:
        espera = INTERVALO_CONSULTA_S - (time.time() - _ultimo_geo)
        if espera > 0:
            time.sleep(espera)
        _ultimo_geo = time.time()
    d = 0.2  # ~22 km em volta do restaurante: só procura nesta região
    base = {"format": "json", "limit": 5, "countrycodes": "br", "accept-language": "pt-BR", "bounded": 1,
            "viewbox": f"{RESTAURANTE['lng'] - d},{RESTAURANTE['lat'] + d},{RESTAURANTE['lng'] + d},{RESTAURANTE['lat'] - d}"}
    if campos:
        base.update({k: v for k, v in campos.items() if v})
    else:
        base["q"] = consulta
    if LOCATIONIQ_KEY:
        base["key"] = LOCATIONIQ_KEY
        url = LOCATIONIQ_URL + ("/search/structured?" if campos else "/search?")
    else:
        url = "https://nominatim.openstreetmap.org/search?"
    req = urllib.request.Request(url + urllib.parse.urlencode(base),
                                 headers={"User-Agent": "despacho-motoboys/1.0 (painel de restaurante pequeno)"})
    for tentativa in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                dados = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:  # a LocationIQ responde 404 quando não encontra nada
                return []
            if tentativa == 2:
                raise
            time.sleep(2)
        except Exception:
            if tentativa == 2:
                raise
            time.sleep(2)
    return [{"rotulo": x["display_name"], "lat": float(x["lat"]), "lng": float(x["lon"])} for x in dados]


@app.post("/geocodificar", dependencies=[Depends(exigir_chave)])
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
                logging.getLogger("uvicorn.error").warning("Falha na busca por campos: %r", e)
        # Completa com a busca em texto livre (que inclui o bairro), sem repetir pontos.
        vistos = {(round(x["lat"], 5), round(x["lng"], 5)) for x in resultados}
        for x in buscar_endereco(c.endereco):
            if (round(x["lat"], 5), round(x["lng"], 5)) not in vistos:
                resultados.append(x)
        return {"resultados": resultados[:6], "centro": RESTAURANTE}
    except Exception as e:
        logging.getLogger("uvicorn.error").warning("Falha na busca de endereço: %r", e)
        return {"resultados": [], "centro": RESTAURANTE,
                "aviso": "O serviço de endereços não respondeu agora. Clique no mapa no ponto certo do cliente, ou tente buscar de novo."}


@app.get("/pedidos", dependencies=[Depends(exigir_chave)])
def listar_pedidos():
    """Pedidos pendentes e em rota (os concluídos não aparecem).
    Um pedido pendente há mais de PEDIDO_ATRASO_MIN minutos sem ser despachado
    vem marcado com "atrasado": true, para o painel destacar."""
    with engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, endereco, rua, numero, bairro, complemento, referencia, cep, telefone, lat, lng, "
            "status, motoboy_id, ordem, criado_em FROM pedidos WHERE status IN ('pendente', 'despachado') "
            "ORDER BY status, motoboy_id, ordem, criado_em"))]
    agora = datetime.utcnow()
    for p in linhas:
        criado = _parse_ts(p.pop("criado_em"))
        p["atrasado"] = bool(p["status"] == "pendente" and criado is not None
                              and (agora - criado).total_seconds() / 60 >= PEDIDO_ATRASO_MIN)
    return {"pedidos": linhas}


@app.get("/bairros", dependencies=[Depends(exigir_chave)])
def listar_bairros():
    """Bairros já usados em pedidos, para sugerir no painel enquanto digita."""
    with engine.connect() as con:
        nomes = [r[0] for r in con.execute(text(
            "SELECT DISTINCT bairro FROM pedidos WHERE bairro IS NOT NULL AND bairro <> '' ORDER BY bairro"))]
    return {"bairros": nomes}


@app.get("/painel")
def pagina_do_painel():
    """Página do restaurante. Não contém dados: eles só aparecem depois de digitar a API_KEY."""
    return HTMLResponse(PAINEL_HTML)


PAINEL_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR" translate="no">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="google" content="notranslate">
<title>Painel do restaurante</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<style>
 #mapaConfirma{height:340px;border-radius:8px;margin:8px 0}
 .pino{width:22px;height:22px;border-radius:50%;background:#dc2626;border:3px solid #fff;box-shadow:0 0 4px #000}
 body{font-family:system-ui,sans-serif;margin:0;background:#f5f5f5;color:#222}
 header{padding:12px 16px;background:#1f2937;color:#fff;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 main{max-width:720px;margin:0 auto;padding:12px}
 section{background:#fff;border-radius:10px;padding:14px;margin:12px 0}
 h2{margin:0 0 10px;font-size:1.05rem}
 input{padding:8px;border-radius:6px;border:1px solid #ccc;font-size:1rem}
 button{padding:8px 14px;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:1rem;cursor:pointer}
 button.verde{background:#10b981} button:disabled{opacity:.5}
 button.cinza{background:#6b7280} button.on{background:#10b981} button.off{background:#9ca3af}
 .linha{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
 .linha input{flex:1;min-width:160px}
 ul{list-style:none;margin:0;padding:0}
 li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-top:1px solid #eee;flex-wrap:wrap}
 li:first-child{border-top:0}
 .tag{font-size:.8rem;padding:2px 8px;border-radius:10px;background:#e5e7eb;white-space:nowrap}
 .tag.atrasado{background:#fee2e2;color:#b91c1c;font-weight:600}
 li.atrasado{background:#fef2f2}
 .ok{color:#047857} .erro{color:#b91c1c}
 .cartoes{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
 .cartao{background:#f5f5f5;border-radius:8px;padding:10px;text-align:center}
 .cartao .num{font-size:1.6rem;font-weight:700} .cartao .rot{font-size:.8rem;color:#555}
 .sn{display:flex;align-items:center;gap:4px;font-size:.9rem;white-space:nowrap}
 .extra{font-size:.85rem;color:#555}
 .aviso-edicao{background:#fef3c7;color:#92400e;padding:8px;border-radius:6px;margin-bottom:8px;font-weight:600}
 button.perigo{background:#b91c1c}
 .acoes{display:flex;gap:6px;flex-wrap:wrap}
</style>
</head>
<body>
<header>
 <strong>Chave do restaurante:</strong>
 <input id="chave" type="password" placeholder="cole a API_KEY aqui" size="24">
 <button id="salvar">Entrar</button>
</header>
<main>
 <section>
  <h2>Resumo de hoje</h2>
  <div class="cartoes" id="resumo">
   <div class="cartao"><div class="num" id="rPendentes">—</div><div class="rot">Pendentes</div></div>
   <div class="cartao"><div class="num" id="rEmRota">—</div><div class="rot">Em rota</div></div>
   <div class="cartao"><div class="num" id="rEntregues">—</div><div class="rot">Entregues hoje</div></div>
   <div class="cartao"><div class="num" id="rTempo">—</div><div class="rot">Tempo médio</div></div>
  </div>
  <div class="linha" style="margin-top:10px;align-items:center">
   <button id="baixarHistorico" class="cinza">Baixar histórico (CSV)</button>
   <span style="font-size:.9rem;color:#555">últimos <input id="diasHistorico" type="number" min="1" max="365" value="30" style="width:60px;padding:4px"> dia(s)</span>
  </div>
  <div id="msgHistorico"></div>
 </section>
 <section>
  <h2 id="tituloPedido">1. Novo pedido</h2>
  <div id="avisoEdicao" class="aviso-edicao" style="display:none"></div>
  <div class="linha">
   <input id="rua" placeholder="Rua * (ex.: Rua São Paulo)" style="flex:3">
   <input id="numero" placeholder="Número *" style="flex:1;min-width:90px">
   <label class="sn"><input type="checkbox" id="semNumero"> Sem número</label>
  </div>
  <div class="linha">
   <input id="bairro" list="listaBairros" placeholder="Bairro * (ex.: Amazonas)" autocomplete="off">
   <datalist id="listaBairros"></datalist>
   <input id="cep" placeholder="CEP (opcional)" inputmode="numeric" maxlength="9" style="flex:0 1 140px;min-width:120px">
  </div>
  <div class="linha">
   <input id="complemento" placeholder="Complemento (ex.: apto 201, bloco B, casa 2)">
  </div>
  <div class="linha">
   <input id="referencia" placeholder="Ponto de referência (ex.: em frente à padaria)">
  </div>
  <div class="linha">
   <input id="telefone" placeholder="Telefone do cliente (opcional) ex.: (31) 99999-9999" inputmode="tel" maxlength="16">
  </div>
  <div class="linha">
   <input id="cidade" value="Itabira, MG" style="flex:0 1 160px;min-width:120px" title="Cidade, UF">
   <button id="buscar">Buscar endereço</button>
   <button id="limpar" class="cinza">Limpar</button>
  </div>
  <div style="font-size:.85rem;color:#555;margin-bottom:6px">* obrigatório. Complemento e referência não entram na busca do mapa; só aparecem para o motoboy.</div>
  <div id="msgBusca"></div>
  <ul id="candidatos"></ul>
  <div id="confirmacao" style="display:none">
   <strong>Confirme o ponto:</strong> arraste o pino vermelho até a porta do cliente (ou clique no mapa).
   <div id="mapaConfirma"></div>
   <div class="linha">
    <button id="confirmar" class="verde">Confirmar pedido neste ponto</button>
    <button id="cancelar" style="background:#6b7280">Cancelar</button>
   </div>
  </div>
 </section>
 <section>
  <h2>2. Pedidos na fila</h2>
  <div id="alertaAtraso" class="erro" style="display:none;font-weight:600;margin-bottom:6px"></div>
  <ul id="fila"></ul>
  <div class="linha" style="margin-top:10px">
   <button id="despachar" class="verde">Despachar agora</button>
  </div>
  <div id="msgDespacho"></div>
 </section>
 <section>
  <h2>3. Motoboys</h2>
  <ul id="listaMoto"></ul>
  <div class="linha" style="margin-top:10px">
   <input id="motoNome" placeholder="Nome (ex.: Carlos)">
   <input id="motoCodigo" placeholder="Código (mínimo 6)">
   <button id="salvarMoto">Salvar motoboy</button>
  </div>
  <div style="font-size:.9rem;color:#555">Nome já existente: o código dele é trocado. O nome vale com maiúsculas e minúsculas. Motoboy novo entra "De turno".</div>
  <div id="msgMoto" style="word-break:break-all"></div>
 </section>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
let chave = sessionStorage.getItem("chave") || "";
const $ = (i) => document.getElementById(i);
$("chave").value = chave;

async function api(caminho, metodo, corpo) {
  const resp = await fetch(caminho, {
    method: metodo || "GET",
    headers: {"X-API-Key": chave, "Content-Type": "application/json"},
    body: corpo ? JSON.stringify(corpo) : undefined
  });
  if (resp.status === 401) {
    // Para de consultar sozinho com a chave errada (senão o servidor bloqueia o computador por excesso de erros).
    chave = ""; sessionStorage.removeItem("chave");
    throw new Error("Chave incorreta. Digite de novo e clique em Entrar.");
  }
  if (resp.status === 429) { chave = ""; }
  if (!resp.ok) { let d = ""; try { d = (await resp.json()).detail; } catch (e) {} throw new Error(d || "Erro " + resp.status); }
  return resp.json();
}

function dizer(el, texto, classe) { el.textContent = texto; el.className = classe || ""; }

$("salvar").onclick = () => { chave = $("chave").value.trim(); sessionStorage.setItem("chave", chave); atualizarTudo(); carregarBairros(); };

$("semNumero").onchange = () => {
  const sn = $("semNumero").checked;
  $("numero").disabled = sn;
  if (sn) $("numero").value = "";
};

$("telefone").oninput = () => {
  const d = $("telefone").value.replace(/\D/g, "").slice(0, 11);
  let t = d;
  if (d.length > 2) t = "(" + d.slice(0, 2) + ") " + d.slice(2);
  if (d.length > 6) t = "(" + d.slice(0, 2) + ") " + d.slice(2, d.length - 4) + "-" + d.slice(d.length - 4);
  $("telefone").value = t;
};

$("cep").oninput = () => {
  const d = $("cep").value.replace(/\D/g, "").slice(0, 8);
  $("cep").value = d.length > 5 ? d.slice(0, 5) + "-" + d.slice(5) : d;
};

// Lê os campos do formulário. Devolve {erro: "..."} se faltar algo obrigatório.
function lerEndereco() {
  const v = (i) => $(i).value.trim().replace(/\s+/g, " ");
  const e = {rua: v("rua"), numero: $("semNumero").checked ? "S/N" : v("numero"), bairro: v("bairro"),
             complemento: v("complemento"), referencia: v("referencia"), cep: v("cep"), telefone: v("telefone")};
  const partesCidade = v("cidade").split(",");
  e.cidade = (partesCidade[0] || "").trim();
  e.estado = (partesCidade[1] || "").trim();
  if (e.rua.length < 3) return {erro: "Digite o nome da rua."};
  if (!e.numero) return {erro: "Digite o número (ou marque \"Sem número\")."};
  if (e.bairro.length < 2) return {erro: "Digite o bairro."};
  if (e.cep && e.cep.replace(/\D/g, "").length !== 8) return {erro: "CEP incompleto (8 números). Pode deixar vazio."};
  const digitosTel = e.telefone.replace(/\D/g, "").length;
  if (e.telefone && (digitosTel < 10 || digitosTel > 11)) return {erro: "Telefone incompleto: use DDD + número, ex.: (31) 99999-9999. Pode deixar vazio."};
  // Endereço numa linha, no formato que o Google Maps entende bem. Sem complemento/referência.
  const cidadeUf = e.cidade + (e.estado ? " - " + e.estado : "");
  e.endereco = e.rua + (e.numero !== "S/N" ? ", " + e.numero : "") + " - " + e.bairro + ", " + cidadeUf + (e.cep ? ", " + e.cep : "");
  return e;
}

let editandoId = null;  // id do pedido sendo corrigido, ou null quando é pedido novo

function limparFormulario() {
  ["rua", "numero", "bairro", "complemento", "referencia", "cep", "telefone"].forEach((i) => $(i).value = "");
  $("semNumero").checked = false; $("numero").disabled = false;
  editandoId = null;
  $("tituloPedido").textContent = "1. Novo pedido";
  $("avisoEdicao").style.display = "none";
  $("confirmar").textContent = "Confirmar pedido neste ponto";
}

// Coloca um pedido da fila no formulário para corrigir. Já abre o mapa no ponto salvo:
// se só mudou o complemento ou o telefone, basta clicar em "Salvar alterações".
function editarPedido(p) {
  limparFormulario();
  editandoId = p.id;
  $("rua").value = p.rua || p.endereco;  // pedidos antigos só têm o endereço numa linha
  if (p.numero === "S/N") { $("semNumero").checked = true; $("numero").disabled = true; }
  else $("numero").value = p.numero || "";
  $("bairro").value = p.bairro || "";
  $("complemento").value = p.complemento || "";
  $("referencia").value = p.referencia || "";
  $("cep").value = p.cep || "";
  $("telefone").value = p.telefone || "";
  $("tituloPedido").textContent = "1. Corrigindo pedido " + p.id;
  $("avisoEdicao").textContent = "Corrigindo o pedido " + p.id + ". Mudou a rua ou o número? Clique em Buscar endereço. Senão, confira o pino e clique em Salvar alterações.";
  $("avisoEdicao").style.display = "block";
  $("confirmar").textContent = "Salvar alterações";
  $("candidatos").innerHTML = ""; dizer($("msgBusca"), "");
  mostrarConfirmacao({lat: p.lat, lng: p.lng});
  $("tituloPedido").scrollIntoView({behavior: "smooth"});
}
$("limpar").onclick = () => { limparFormulario(); $("candidatos").innerHTML = ""; $("confirmacao").style.display = "none"; dizer($("msgBusca"), ""); };

$("buscar").onclick = async () => {
  const e = lerEndereco();
  if (e.erro) { dizer($("msgBusca"), e.erro, "erro"); return; }
  $("candidatos").innerHTML = "";
  dizer($("msgBusca"), "Buscando...");
  try {
    const r = await api("/geocodificar", "POST", {
      endereco: e.rua + (e.numero !== "S/N" ? ", " + e.numero : "") + ", " + e.bairro + ", " + e.cidade + (e.estado ? ", " + e.estado : ""),
      rua: e.rua, numero: e.numero, cidade: e.cidade, estado: e.estado, cep: e.cep || null});
    if (!r.resultados.length) { dizer($("msgBusca"), r.aviso || "Não achei o endereço. Clique no mapa no ponto certo.", "erro"); mostrarConfirmacao(r.centro); return; }
    dizer($("msgBusca"), "Clique no endereço correto:");
    r.resultados.forEach((c) => {
      const li = document.createElement("li");
      const t = document.createElement("span"); t.textContent = c.rotulo;
      const b = document.createElement("button"); b.textContent = "Usar este";
      b.onclick = () => mostrarConfirmacao(c);
      li.append(t, b); $("candidatos").append(li);
    });
  } catch (e) { dizer($("msgBusca"), e.message, "erro"); }
};

let mapaC = null, pino = null;
function mostrarConfirmacao(c) {
  $("confirmacao").style.display = "block";
  if (!mapaC) {
    mapaC = L.map("mapaConfirma");
    L.tileLayer(MAPA_URL, {maxZoom: 19, subdomains: "abc", attribution: MAPA_CREDITO}).addTo(mapaC);
    pino = L.marker([c.lat, c.lng], {draggable: true, icon: L.divIcon({className: "", html: '<div class="pino"></div>', iconSize: [22, 22]})}).addTo(mapaC);
    mapaC.on("click", (e) => pino.setLatLng(e.latlng));
  }
  mapaC.invalidateSize();
  mapaC.setView([c.lat, c.lng], 18);
  pino.setLatLng([c.lat, c.lng]);
  $("confirmacao").scrollIntoView({behavior: "smooth", block: "nearest"});
}

$("cancelar").onclick = () => { $("confirmacao").style.display = "none"; if (editandoId) { limparFormulario(); dizer($("msgBusca"), "Correção cancelada."); } };

$("confirmar").onclick = async () => {
  // Lê os campos de novo: se a pessoa corrigiu o complemento depois de buscar, vale o corrigido.
  const e = lerEndereco();
  if (e.erro) { dizer($("msgBusca"), e.erro, "erro"); return; }
  const ponto = pino.getLatLng();
  $("confirmar").disabled = true;
  const corpo = {endereco: e.endereco, lat: ponto.lat, lng: ponto.lng, rua: e.rua, numero: e.numero, bairro: e.bairro,
                 complemento: e.complemento, referencia: e.referencia, cep: e.cep, telefone: e.telefone};
  try {
    if (editandoId) {
      await api("/pedidos/" + encodeURIComponent(editandoId), "PUT", corpo);
      dizer($("msgBusca"), "Pedido " + editandoId + " corrigido.", "ok");
    } else {
      const id = "P" + Date.now().toString(36).toUpperCase();
      await api("/pedidos", "POST", Object.assign({id: id}, corpo));
      dizer($("msgBusca"), "Pedido " + id + " criado.", "ok");
    }
    $("candidatos").innerHTML = ""; limparFormulario(); $("confirmacao").style.display = "none";
    carregarFila(); carregarBairros();
  } catch (e) { dizer($("msgBusca"), e.message, "erro"); }
  $("confirmar").disabled = false;
};

async function carregarFila() {
  if (!chave) return;
  try {
    const r = await api("/pedidos");
    const ul = $("fila"); ul.innerHTML = "";
    if (!r.pedidos.length) { const li = document.createElement("li"); li.textContent = "Nenhum pedido na fila."; ul.append(li); }
    let atrasados = 0;
    r.pedidos.forEach((p) => {
      const li = document.createElement("li");
      if (p.atrasado) { li.className = "atrasado"; atrasados++; }
      const info = document.createElement("div"); info.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap";
      const t = document.createElement("span"); t.textContent = p.id + " - " + p.endereco;
      const extras = [p.complemento, p.referencia ? "Ref.: " + p.referencia : "", p.telefone ? "Tel.: " + p.telefone : ""].filter(Boolean).join(" · ");
      if (extras) { const x = document.createElement("div"); x.className = "extra"; x.textContent = extras; t.append(x); }
      const s = document.createElement("span"); s.className = "tag" + (p.atrasado ? " atrasado" : "");
      s.textContent = p.atrasado ? "esperando há mais de " + PEDIDO_ATRASO_MIN_TXT
        : p.status === "pendente" ? "aguardando" : p.motoboy_id + " (parada " + p.ordem + ")";
      info.append(t, s); li.append(info);
      const acoes = document.createElement("div"); acoes.className = "acoes";
      if (p.status === "pendente") {
        const ed = document.createElement("button"); ed.textContent = "Editar"; ed.className = "cinza";
        ed.onclick = () => editarPedido(p);
        acoes.append(ed);
      }
      const cx = document.createElement("button"); cx.textContent = "Cancelar pedido"; cx.className = "perigo";
      cx.onclick = async () => {
        const emRota = p.status === "despachado";
        if (!confirm("Cancelar o pedido " + p.id + "?\n" + p.endereco + (emRota ? "\n\nEle já está com " + p.motoboy_id + ": a parada sai da rota dele." : ""))) return;
        cx.disabled = true;
        try {
          const r2 = await api("/pedidos/" + encodeURIComponent(p.id) + "/cancelar", "POST");
          dizer($("msgDespacho"), r2.mensagem, "ok");
          if (editandoId === p.id) { limparFormulario(); $("confirmacao").style.display = "none"; }
          carregarFila(); carregarResumo();
        } catch (e) { dizer($("msgDespacho"), e.message, "erro"); cx.disabled = false; }
      };
      if (p.status === "pendente" && listaMotoboys.length) {
        const sel = document.createElement("select");
        listaMotoboys.forEach((m) => { const o = document.createElement("option"); o.value = m.id; o.textContent = m.id; sel.append(o); });
        const bt = document.createElement("button"); bt.textContent = "Atribuir";
        bt.onclick = async () => {
          bt.disabled = true;
          try { await api("/pedidos/" + encodeURIComponent(p.id) + "/atribuir", "POST", {motoboy_id: sel.value}); carregarFila(); }
          catch (e) { dizer($("msgDespacho"), e.message, "erro"); bt.disabled = false; }
        };
        acoes.append(sel, bt);
      }
      acoes.append(cx);
      li.append(acoes);
      ul.append(li);
    });
    const aviso = $("alertaAtraso");
    if (atrasados) { aviso.style.display = "block"; aviso.textContent = "⚠ " + atrasados + " pedido(s) esperando há mais de " + PEDIDO_ATRASO_MIN_TXT + " sem despachar."; }
    else { aviso.style.display = "none"; }
  } catch (e) { dizer($("msgDespacho"), e.message, "erro"); }
}

$("despachar").onclick = async () => {
  $("despachar").disabled = true;
  dizer($("msgDespacho"), "Despachando...");
  try {
    const r = await api("/despachar", "POST");
    if (r.mensagem) { dizer($("msgDespacho"), r.mensagem, "erro"); }
    else {
      const partes = Object.entries(r.rotas).map(([m, l]) => m + ": " + l.length + " pedido(s)");
      dizer($("msgDespacho"), "Despachado! " + partes.join(" | "), "ok");
    }
    carregarFila();
  } catch (e) { dizer($("msgDespacho"), e.message, "erro"); }
  $("despachar").disabled = false;
};

let centro = null;
let listaMotoboys = [];
const PEDIDO_ATRASO_MIN_TXT = "__PEDIDO_ATRASO_MIN__ min";
async function carregarMotos() {
  if (!chave) return;
  try {
    const r = await api("/motoboys");
    centro = r.centro;
    listaMotoboys = r.motoboys;
    const ul = $("listaMoto"); ul.innerHTML = "";
    if (!r.motoboys.length) { const li = document.createElement("li"); li.textContent = "Nenhum motoboy cadastrado."; ul.append(li); }
    r.motoboys.forEach((m) => {
      const li = document.createElement("li");
      const t = document.createElement("span"); t.textContent = m.id + " (" + m.paradas + " parada(s) em aberto)" + (m.gps_ativo ? " — GPS ativo" : "");
      const acoes = document.createElement("div"); acoes.className = "acoes";
      const g = document.createElement("button"); g.className = m.ativo ? "on" : "off"; g.textContent = m.ativo ? "De turno" : "Fora de turno";
      g.onclick = async () => {
        g.disabled = true;
        try { await api("/motoboys/" + encodeURIComponent(m.id) + "/turno", "POST", {ativo: !m.ativo}); carregarMotos(); }
        catch (e) { dizer($("msgMoto"), e.message, "erro"); g.disabled = false; }
      };
      const b = document.createElement("button"); b.textContent = "Remover"; b.className = "cinza"; b.style.background = "#b91c1c";
      b.onclick = async () => {
        if (!confirm("Remover " + m.id + "?" + (m.paradas ? "\nAs " + m.paradas + " parada(s) dele voltam para a fila." : ""))) return;
        try { await api("/motoboys/" + encodeURIComponent(m.id), "DELETE"); dizer($("msgMoto"), m.id + " removido.", "ok"); carregarMotos(); carregarFila(); }
        catch (e) { dizer($("msgMoto"), e.message, "erro"); }
      };
      acoes.append(g, b); li.append(t, acoes); ul.append(li);
    });
  } catch (e) { dizer($("msgMoto"), e.message, "erro"); }
}

$("salvarMoto").onclick = async () => {
  const nome = $("motoNome").value.trim(), codigo = $("motoCodigo").value.trim();
  if (!nome) { dizer($("msgMoto"), "Digite o nome do motoboy.", "erro"); return; }
  if (codigo.length < 6) { dizer($("msgMoto"), "O código precisa ter pelo menos 6 caracteres.", "erro"); return; }
  $("salvarMoto").disabled = true;
  try {
    if (!centro) await carregarMotos();
    if (!centro) throw new Error("Entre com a chave primeiro.");
    await api("/motoboys", "POST", {id: nome, codigo: codigo, lat: centro.lat, lng: centro.lng});
    const link = location.origin + "/motoboy?id=" + encodeURIComponent(nome) + "&codigo=" + encodeURIComponent(codigo);
    dizer($("msgMoto"), "Salvo! Link para enviar ao motoboy: " + link, "ok");
    $("motoNome").value = ""; $("motoCodigo").value = "";
    carregarMotos();
  } catch (e) { dizer($("msgMoto"), e.message, "erro"); }
  $("salvarMoto").disabled = false;
};

async function carregarResumo() {
  if (!chave) return;
  try {
    const r = await api("/resumo");
    $("rPendentes").textContent = r.pendentes;
    $("rEmRota").textContent = r.em_rota;
    $("rEntregues").textContent = r.entregues_hoje;
    $("rTempo").textContent = r.tempo_medio_min === null ? "—" : r.tempo_medio_min + " min";
  } catch (e) { /* não interrompe o resto do painel por causa disso */ }
}

$("baixarHistorico").onclick = async () => {
  if (!chave) { dizer($("msgHistorico"), "Entre com a chave primeiro.", "erro"); return; }
  const dias = $("diasHistorico").value || 30;
  $("baixarHistorico").disabled = true;
  dizer($("msgHistorico"), "Gerando arquivo...");
  try {
    const resp = await fetch("/historico.csv?dias=" + encodeURIComponent(dias), {headers: {"X-API-Key": chave}});
    if (!resp.ok) throw new Error("Não consegui gerar o histórico agora.");
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = "historico.csv";
    document.body.append(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    dizer($("msgHistorico"), "Baixado.", "ok");
  } catch (e) { dizer($("msgHistorico"), e.message, "erro"); }
  $("baixarHistorico").disabled = false;
};

async function carregarBairros() {
  if (!chave) return;
  try {
    const r = await api("/bairros");
    const dl = $("listaBairros"); dl.innerHTML = "";
    r.bairros.forEach((b) => { const o = document.createElement("option"); o.value = b; dl.append(o); });
  } catch (e) { /* sugestão é só uma ajuda; sem ela o campo funciona igual */ }
}

// carregarMotos primeiro: carregarFila usa a lista de motoboys para o seletor de "Atribuir".
async function atualizarTudo() { await carregarMotos(); await carregarFila(); await carregarResumo(); }
atualizarTudo();
carregarBairros();
setInterval(atualizarTudo, 10000);
</script>
</body>
</html>
"""
PAINEL_HTML = _com_mapa(PAINEL_HTML.replace("__PEDIDO_ATRASO_MIN__", str(int(PEDIDO_ATRASO_MIN))))


# ---------------------------------------------------------------------
# Área do motoboy (protegida pelo código dele)
# ---------------------------------------------------------------------
@app.get("/rotas/{motoboy_id}")
def rota_do_motoboy(motoboy_id: str, request: Request, x_codigo: str = Header(default="")):
    with engine.connect() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        paradas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, endereco, complemento, referencia, telefone, lat, lng FROM pedidos "
            "WHERE motoboy_id = :id AND status = 'despachado' ORDER BY ordem"),
            {"id": motoboy_id})]
    return {"motoboy_id": motoboy_id, "restaurante": RESTAURANTE, "paradas": paradas,
            "trajeto": trajeto(RESTAURANTE, paradas) if paradas else None}


@app.post("/rotas/{motoboy_id}/entregar/{pedido_id}")
def marcar_entregue(motoboy_id: str, pedido_id: str, request: Request, x_codigo: str = Header(default="")):
    """O motoboy marca uma parada como entregue (precisa do código dele)."""
    with engine.begin() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        r = con.execute(text(
            "UPDATE pedidos SET status = 'concluido', concluido_em = CURRENT_TIMESTAMP "
            "WHERE id = :p AND motoboy_id = :m AND status = 'despachado'"),
            {"p": pedido_id, "m": motoboy_id})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Pedido não encontrado na sua rota")
    return {"mensagem": "Entrega registrada"}


class Posicao(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


@app.post("/rotas/{motoboy_id}/posicao")
def atualizar_posicao(motoboy_id: str, p: Posicao, request: Request, x_codigo: str = Header(default="")):
    """A página do motoboy chama isto sozinha (a cada ~1 min) quando ele escolhe
    "da minha posição". O despacho usa essa posição, se for recente, para saber
    quem está mais perto de verdade; senão usa a posição do cadastro."""
    with engine.begin() as con:
        conferir_codigo(con, request, motoboy_id, x_codigo)
        con.execute(text("UPDATE motoboys SET gps_lat = :lat, gps_lng = :lng, gps_em = CURRENT_TIMESTAMP WHERE id = :id"),
                    {"lat": p.lat, "lng": p.lng, "id": motoboy_id})
    return {"mensagem": "Posição atualizada"}


@app.get("/motoboy")
def pagina_do_motoboy():
    """Página com o mapa. Abra em /motoboy?id=NOME&codigo=CODIGO (não contém dados de clientes)."""
    return HTMLResponse(PAGINA_HTML)


PAGINA_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Minha rota</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f5f5f5;color:#222}
 header{padding:12px 16px;background:#1f2937;color:#fff;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 header input{padding:6px;border-radius:6px;border:0}
 header button{padding:6px 12px;border:0;border-radius:6px;background:#10b981;color:#fff}
 #modo{padding:8px 16px;background:#e5e7eb}
 #modo select{padding:6px;border-radius:6px;font-size:1rem}
 #mapa{height:50vh}
 #msg{padding:12px 16px}
 #paradas{list-style:none;margin:0;padding:0 8px 8px}
 #paradas li{background:#fff;margin:8px 0;padding:10px 12px;border-radius:8px;display:flex;justify-content:space-between;align-items:center;gap:8px}
 #paradas a{background:#2563eb;color:#fff;padding:6px 10px;border-radius:6px;text-decoration:none}
 .num{background:#2563eb;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-weight:bold;border:2px solid #fff}
 .rest{background:#10b981}
 .acoes{display:flex;gap:6px;flex-shrink:0;flex-wrap:wrap;justify-content:flex-end}
 .ent{background:#10b981;color:#fff;border:0;border-radius:6px;padding:6px 10px;font-size:1rem;cursor:pointer}
 .ent:disabled{opacity:.5}
 .completa{display:inline-block;margin-left:8px;background:#10b981;color:#fff;padding:6px 10px;border-radius:6px;text-decoration:none}
 #paradas a.sec{background:#6b7280}
 .extra{font-size:.9rem;color:#b45309;font-weight:600;margin-top:3px}
 #paradas a.ligar{background:#059669}
</style>
</head>
<body>
<header>
 <strong>Motoboy:</strong>
 <input id="nome" placeholder="seu nome (id)">
 <input id="codigo" type="password" size="8" placeholder="código">
 <button id="ir">Ver rota</button>
</header>
<div id="modo">Saindo de: <select id="sel"><option value="rest">do restaurante</option><option value="rua">da minha posição</option></select></div>
<div id="mapa"></div>
<div id="msg"></div>
<ul id="paradas"></ul>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
const qs = new URLSearchParams(location.search);
const id = qs.get("id") || "", codigo = qs.get("codigo") || "";
const nome = document.getElementById("nome"); nome.value = id;
const cod = document.getElementById("codigo"); cod.value = codigo;
document.getElementById("ir").onclick = () => { location.search = "?id=" + encodeURIComponent(nome.value.trim()) + "&codigo=" + encodeURIComponent(cod.value.trim()); };

const mapa = L.map("mapa").setView([-19.6156, -43.2258], 14);
L.tileLayer(MAPA_URL, {maxZoom: 19, subdomains: "abc", attribution: MAPA_CREDITO}).addTo(mapa);
const camada = L.layerGroup().addTo(mapa);
let ultimo = "";
let modo = "rest";
try { if (localStorage.getItem("modo") === "rua") modo = "rua"; } catch (e) {}
const sel = document.getElementById("sel"); sel.value = modo;
sel.onchange = () => { modo = sel.value; try { localStorage.setItem("modo", modo); } catch (e) {} ultimo = ""; atualizar(); vigiarPosicao(); };

// Em "da minha posição", a página manda o GPS sozinha de vez em quando, para o
// despacho saber quem está mais perto de verdade. Se o celular negar a permissão
// de localização, isso é ignorado e o resto da página continua funcionando normal.
let vigia = null, ultimoEnvioGps = 0;
function vigiarPosicao() {
  if (vigia !== null) { navigator.geolocation.clearWatch(vigia); vigia = null; }
  if (modo !== "rua" || !id || !codigo || !navigator.geolocation) return;
  vigia = navigator.geolocation.watchPosition(
    (pos) => {
      const agora = Date.now();
      if (parado || agora - ultimoEnvioGps < 45000) return;  // no máximo 1 envio a cada 45 s
      ultimoEnvioGps = agora;
      fetch("/rotas/" + encodeURIComponent(id) + "/posicao", {
        method: "POST", headers: {"X-Codigo": codigo, "Content-Type": "application/json"},
        body: JSON.stringify({lat: pos.coords.latitude, lng: pos.coords.longitude})
      }).catch(() => {});
    },
    () => {},
    {enableHighAccuracy: false, maximumAge: 60000, timeout: 20000}
  );
}
vigiarPosicao();

function icone(texto, classe) {
  return L.divIcon({className: "", html: '<div class="num ' + (classe || "") + '">' + texto + "</div>", iconSize: [26, 26]});
}

function desenhar(dados) {
  const msg = document.getElementById("msg");
  const lista = document.getElementById("paradas");
  camada.clearLayers(); lista.innerHTML = "";
  if (!dados.paradas.length) { msg.textContent = "Nenhuma rota no momento. Esta página atualiza sozinha."; return; }
  msg.textContent = dados.paradas.length + " parada(s) na sua rota:";
  const ll = (x) => x.lat + "," + x.lng;
  const ps = dados.paradas.slice(0, 10);
  const origem = encodeURIComponent(dados.restaurante.endereco || ll(dados.restaurante));
  const pOrigem = modo === "rua" ? "" : "&origin=" + origem;
  const meio = ps.slice(0, -1).map((x) => x.endereco).join("|");
  const rc = document.createElement("a"); rc.textContent = modo === "rua" ? "Rota completa no Maps (da minha posição)" : "Rota completa no Maps (saindo do restaurante)"; rc.target = "_blank"; rc.className = "completa";
  rc.href = "https://www.google.com/maps/dir/?api=1" + pOrigem + "&destination=" + encodeURIComponent(ps[ps.length - 1].endereco) + (meio ? "&waypoints=" + encodeURIComponent(meio) : "");
  msg.append(" ", rc);
  const r = dados.restaurante, pontos = [[r.lat, r.lng]];
  L.marker([r.lat, r.lng], {icon: icone("R", "rest")}).addTo(camada);
  dados.paradas.forEach((p, i) => {
    pontos.push([p.lat, p.lng]);
    L.marker([p.lat, p.lng], {icon: icone(i + 1)}).addTo(camada);
    const li = document.createElement("li");
    const t = document.createElement("span"); t.textContent = (i + 1) + ". " + p.id + " - " + p.endereco;
    const extras = [p.complemento, p.referencia ? "Ref.: " + p.referencia : ""].filter(Boolean);
    extras.forEach((txt) => { const x = document.createElement("div"); x.className = "extra"; x.textContent = txt; t.append(x); });
    const a = document.createElement("a"); a.textContent = "Navegar"; a.target = "_blank";
    a.href = "https://www.google.com/maps/dir/?api=1" + pOrigem + "&destination=" + encodeURIComponent(p.endereco);
    const b = document.createElement("a"); b.textContent = "Pelo ponto"; b.target = "_blank"; b.className = "sec";
    b.href = "https://www.google.com/maps/dir/?api=1" + pOrigem + "&destination=" + p.lat + "," + p.lng;
    let lig = null;
    const digitos = (p.telefone || "").replace(/\D/g, "");
    if (digitos.length >= 10) {
      lig = document.createElement("a"); lig.textContent = "Ligar"; lig.className = "ligar";
      lig.href = "tel:+55" + digitos;
    }
    const e = document.createElement("button"); e.textContent = "Entregue"; e.className = "ent";
    e.onclick = async () => {
      if (!confirm("Marcar como entregue?\n" + p.endereco + (p.complemento ? "\n" + p.complemento : ""))) return;
      e.disabled = true;
      try {
        const resp = await fetch("/rotas/" + encodeURIComponent(id) + "/entregar/" + encodeURIComponent(p.id), {method: "POST", headers: {"X-Codigo": codigo}});
        if (!resp.ok && resp.status !== 404) throw new Error("falhou");
      } catch (err) { alert("Não consegui registrar a entrega. Tente de novo."); e.disabled = false; return; }
      atualizar();
    };
    const acoes = document.createElement("div"); acoes.className = "acoes"; acoes.append(a, b); if (lig) acoes.append(lig); acoes.append(e);
    li.append(t, acoes); lista.append(li);
  });
  const linha = (dados.trajeto && dados.trajeto.length > 1) ? dados.trajeto : pontos;
  L.polyline(linha, {color: "#2563eb", weight: 5, opacity: 0.8}).addTo(camada);
  mapa.fitBounds(pontos, {padding: [30, 30]});
}

let parado = false;
async function atualizar() {
  if (parado) return;
  if (!id) { document.getElementById("msg").textContent = "Digite seu nome e código acima e clique em Ver rota."; return; }
  try {
    const resp = await fetch("/rotas/" + encodeURIComponent(id), {headers: {"X-Codigo": codigo}});
    if (resp.status === 401 || resp.status === 429) {
      // Para de tentar sozinho: repetir com o código errado bloquearia o celular por excesso de erros.
      parado = true; ultimo = ""; camada.clearLayers(); document.getElementById("paradas").innerHTML = "";
      let txt = "Nome ou código incorretos. Confira e clique em Ver rota.";
      if (resp.status === 429) { try { txt = (await resp.json()).detail; } catch (e) {} }
      document.getElementById("msg").textContent = txt; return;
    }
    const texto = await resp.text();
    if (texto !== ultimo) { ultimo = texto; desenhar(JSON.parse(texto)); }
  } catch (e) { ultimo = ""; document.getElementById("msg").textContent = "Sem conexão com a API."; }
}
atualizar(); setInterval(atualizar, 5000);
</script>
</body>
</html>
"""
PAGINA_HTML = _com_mapa(PAGINA_HTML)
