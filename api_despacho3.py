"""
API de despacho automático de motoboys - versão 3 (com banco de dados e segurança).

Variáveis de ambiente (configuradas no painel do Render, nunca dentro do código):
    DATABASE_URL  endereço do banco Postgres. Se não existir, usa um arquivo
                  SQLite local (só para testes no seu computador).
    RESTAURANTE_LAT / RESTAURANTE_LNG / RESTAURANTE_ENDERECO  (opcionais) local do restaurante.
    OSRM_URL      (opcional) serviço de rotas pelas ruas. Padrão: servidor público de demonstração
                  do OSRM (uso não comercial, sem garantia). Use "desligado" para só linha reta.
    API_KEY       chave secreta exigida em /pedidos, /motoboys, /despachar e /status.
                  Deve ser enviada no cabeçalho  X-API-Key.

O motoboy não usa a API_KEY. Cada motoboy tem um código próprio (definido ao
cadastrá-lo) e só consegue ver a rota dele, enviando esse código no cabeçalho X-Codigo.
"""

import itertools
import json
import logging
import math
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
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

try:
    with engine.begin() as con:
        con.execute(text("ALTER TABLE pedidos ADD COLUMN concluido_em TIMESTAMP"))
except Exception:
    pass


# ---------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------
def exigir_chave(x_api_key: str = Header(default="")):
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY não configurada no servidor")
    if not secrets.compare_digest(x_api_key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Chave de API inválida ou ausente")


# ---------------------------------------------------------------------
# Formatos de dados aceitos
# ---------------------------------------------------------------------
class Pedido(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    endereco: str = Field(min_length=1, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


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
_trava_osrm = threading.Lock()
_ultimo_osrm = 0.0
_osrm_falha_ate = 0.0
_cache_trajetos = {}


def _osrm_get(caminho):
    """Consulta o serviço de rotas. Devolve o JSON ou None se falhar.
    Respeita 1 consulta por segundo e, depois de uma falha, não tenta de novo por 60 s."""
    global _ultimo_osrm, _osrm_falha_ate
    if not OSRM_URL or OSRM_URL.lower() in ("off", "desligado"):
        return None
    if time.time() < _osrm_falha_ate:
        return None
    with _trava_osrm:
        espera = 1.1 - (time.time() - _ultimo_osrm)
        if espera > 0:
            time.sleep(espera)
        _ultimo_osrm = time.time()
    req = urllib.request.Request(OSRM_URL + caminho,
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
    if len(pontos) < 2 or len(pontos) > 90:
        return None
    dados = _osrm_get("/table/v1/driving/" + _coords(pontos) + "?annotations=duration")
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
    dados = _osrm_get("/route/v1/driving/" + chave + "?overview=simplified&geometries=geojson")
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
        r = con.execute(text(
            "INSERT INTO pedidos (id, endereco, lat, lng) VALUES (:id, :endereco, :lat, :lng) "
            "ON CONFLICT (id) DO NOTHING"), pedido.model_dump())
        criado = r.rowcount == 1
        total = con.execute(text("SELECT COUNT(*) FROM pedidos WHERE status = 'pendente'")).scalar()
    return {"mensagem": "Pedido recebido" if criado else "Pedido já existia (ignorado)", "total_pendentes": total}


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


_trava_despacho = threading.Lock()


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


# ---------------------------------------------------------------------
# Painel do restaurante (protegido pela API_KEY digitada na própria página)
# ---------------------------------------------------------------------
class Consulta(BaseModel):
    endereco: str = Field(min_length=3, max_length=300)


_trava_geo = threading.Lock()
_ultimo_geo = 0.0


def buscar_endereco(consulta):
    """Endereço em texto -> lista de lat/lng candidatos (Nominatim/OpenStreetMap).
    A regra do serviço gratuito pede no máximo 1 consulta por segundo e um
    User-Agent que identifique o aplicativo; ambos são respeitados aqui."""
    global _ultimo_geo
    with _trava_geo:
        espera = 1.1 - (time.time() - _ultimo_geo)
        if espera > 0:
            time.sleep(espera)
        _ultimo_geo = time.time()
    d = 0.2  # ~22 km em volta do restaurante: só procura nesta região
    params = urllib.parse.urlencode({
        "q": consulta, "format": "jsonv2", "limit": 5, "countrycodes": "br",
        "accept-language": "pt-BR", "bounded": 1,
        "viewbox": f"{RESTAURANTE['lng'] - d},{RESTAURANTE['lat'] + d},{RESTAURANTE['lng'] + d},{RESTAURANTE['lat'] - d}",
    })
    req = urllib.request.Request(
        "https://nominatim.openstreetmap.org/search?" + params,
        headers={"User-Agent": "despacho-motoboys/1.0 (painel de restaurante pequeno)"})
    for tentativa in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                dados = json.load(resp)
            break
        except Exception:
            if tentativa == 2:
                raise
            time.sleep(2)
    return [{"rotulo": x["display_name"], "lat": float(x["lat"]), "lng": float(x["lon"])} for x in dados]


@app.post("/geocodificar", dependencies=[Depends(exigir_chave)])
def geocodificar(c: Consulta):
    try:
        return {"resultados": buscar_endereco(c.endereco), "centro": RESTAURANTE}
    except Exception as e:
        logging.getLogger("uvicorn.error").warning("Falha na busca de endereço: %r", e)
        return {"resultados": [], "centro": RESTAURANTE,
                "aviso": "O serviço de endereços não respondeu agora. Clique no mapa no ponto certo do cliente, ou tente buscar de novo."}


@app.get("/pedidos", dependencies=[Depends(exigir_chave)])
def listar_pedidos():
    """Pedidos pendentes e em rota (os concluídos não aparecem)."""
    with engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, endereco, status, motoboy_id, ordem FROM pedidos WHERE status <> 'concluido' "
            "ORDER BY status, motoboy_id, ordem, criado_em"))]
    return {"pedidos": linhas}


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
 li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-top:1px solid #eee}
 li:first-child{border-top:0}
 .tag{font-size:.8rem;padding:2px 8px;border-radius:10px;background:#e5e7eb;white-space:nowrap}
 .ok{color:#047857} .erro{color:#b91c1c}
 .cartoes{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
 .cartao{background:#f5f5f5;border-radius:8px;padding:10px;text-align:center}
 .cartao .num{font-size:1.6rem;font-weight:700} .cartao .rot{font-size:.8rem;color:#555}
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
 </section>
 <section>
  <h2>1. Novo pedido</h2>
  <div class="linha">
   <input id="endereco" placeholder="Rua e número (ex.: Rua Tal, 123, Bairro)">
   <input id="cidade" value="Itabira, MG" size="12">
   <button id="buscar">Buscar endereço</button>
  </div>
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
let textoDigitado = "";

async function api(caminho, metodo, corpo) {
  const resp = await fetch(caminho, {
    method: metodo || "GET",
    headers: {"X-API-Key": chave, "Content-Type": "application/json"},
    body: corpo ? JSON.stringify(corpo) : undefined
  });
  if (resp.status === 401) throw new Error("Chave incorreta.");
  if (!resp.ok) { let d = ""; try { d = (await resp.json()).detail; } catch (e) {} throw new Error(d || "Erro " + resp.status); }
  return resp.json();
}

function dizer(el, texto, classe) { el.textContent = texto; el.className = classe || ""; }

$("salvar").onclick = () => { chave = $("chave").value.trim(); sessionStorage.setItem("chave", chave); carregarFila(); carregarMotos(); carregarResumo(); };

$("buscar").onclick = async () => {
  const end = $("endereco").value.trim();
  if (end.length < 3) { dizer($("msgBusca"), "Digite o endereço.", "erro"); return; }
  textoDigitado = end;
  $("candidatos").innerHTML = "";
  dizer($("msgBusca"), "Buscando...");
  try {
    const r = await api("/geocodificar", "POST", {endereco: end + ", " + $("cidade").value.trim()});
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
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: "&copy; OpenStreetMap"}).addTo(mapaC);
    pino = L.marker([c.lat, c.lng], {draggable: true, icon: L.divIcon({className: "", html: '<div class="pino"></div>', iconSize: [22, 22]})}).addTo(mapaC);
    mapaC.on("click", (e) => pino.setLatLng(e.latlng));
  }
  mapaC.invalidateSize();
  mapaC.setView([c.lat, c.lng], 18);
  pino.setLatLng([c.lat, c.lng]);
  $("confirmacao").scrollIntoView({behavior: "smooth", block: "nearest"});
}

$("cancelar").onclick = () => { $("confirmacao").style.display = "none"; };

$("confirmar").onclick = async () => {
  const ponto = pino.getLatLng();
  $("confirmar").disabled = true;
  const id = "P" + Date.now().toString(36).toUpperCase();
  try {
    await api("/pedidos", "POST", {id: id, endereco: textoDigitado + ", " + $("cidade").value.trim(), lat: ponto.lat, lng: ponto.lng});
    dizer($("msgBusca"), "Pedido " + id + " criado.", "ok");
    $("candidatos").innerHTML = ""; $("endereco").value = ""; $("confirmacao").style.display = "none";
    carregarFila();
  } catch (e) { dizer($("msgBusca"), e.message, "erro"); }
  $("confirmar").disabled = false;
};

async function carregarFila() {
  if (!chave) return;
  try {
    const r = await api("/pedidos");
    const ul = $("fila"); ul.innerHTML = "";
    if (!r.pedidos.length) { const li = document.createElement("li"); li.textContent = "Nenhum pedido na fila."; ul.append(li); }
    r.pedidos.forEach((p) => {
      const li = document.createElement("li");
      const t = document.createElement("span"); t.textContent = p.id + " - " + p.endereco;
      const s = document.createElement("span"); s.className = "tag";
      s.textContent = p.status === "pendente" ? "aguardando" : p.motoboy_id + " (parada " + p.ordem + ")";
      li.append(t, s); ul.append(li);
    });
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
async function carregarMotos() {
  if (!chave) return;
  try {
    const r = await api("/motoboys");
    centro = r.centro;
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

carregarFila(); carregarMotos(); carregarResumo();
setInterval(() => { carregarFila(); carregarMotos(); carregarResumo(); }, 10000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------
# Área do motoboy (protegida pelo código dele)
# ---------------------------------------------------------------------
@app.get("/rotas/{motoboy_id}")
def rota_do_motoboy(motoboy_id: str, x_codigo: str = Header(default="")):
    with engine.connect() as con:
        m = con.execute(text("SELECT codigo FROM motoboys WHERE id = :id"), {"id": motoboy_id}).first()
        if m is None or not secrets.compare_digest(x_codigo.encode(), m[0].encode()):
            raise HTTPException(status_code=401, detail="Nome ou código incorretos")
        paradas = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, endereco, lat, lng FROM pedidos WHERE motoboy_id = :id AND status = 'despachado' ORDER BY ordem"),
            {"id": motoboy_id})]
    return {"motoboy_id": motoboy_id, "restaurante": RESTAURANTE, "paradas": paradas,
            "trajeto": trajeto(RESTAURANTE, paradas) if paradas else None}


@app.post("/rotas/{motoboy_id}/entregar/{pedido_id}")
def marcar_entregue(motoboy_id: str, pedido_id: str, x_codigo: str = Header(default="")):
    """O motoboy marca uma parada como entregue (precisa do código dele)."""
    with engine.begin() as con:
        m = con.execute(text("SELECT codigo FROM motoboys WHERE id = :id"), {"id": motoboy_id}).first()
        if m is None or not secrets.compare_digest(x_codigo.encode(), m[0].encode()):
            raise HTTPException(status_code=401, detail="Nome ou código incorretos")
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
def atualizar_posicao(motoboy_id: str, p: Posicao, x_codigo: str = Header(default="")):
    """A página do motoboy chama isto sozinha (a cada ~1 min) quando ele escolhe
    "da minha posição". O despacho usa essa posição, se for recente, para saber
    quem está mais perto de verdade; senão usa a posição do cadastro."""
    with engine.begin() as con:
        m = con.execute(text("SELECT codigo FROM motoboys WHERE id = :id"), {"id": motoboy_id}).first()
        if m is None or not secrets.compare_digest(x_codigo.encode(), m[0].encode()):
            raise HTTPException(status_code=401, detail="Nome ou código incorretos")
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
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: "&copy; OpenStreetMap | rotas: OSRM"}).addTo(mapa);
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
      if (agora - ultimoEnvioGps < 45000) return;  // no máximo 1 envio a cada 45 s
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
    const a = document.createElement("a"); a.textContent = "Navegar"; a.target = "_blank";
    a.href = "https://www.google.com/maps/dir/?api=1" + pOrigem + "&destination=" + encodeURIComponent(p.endereco);
    const b = document.createElement("a"); b.textContent = "Pelo ponto"; b.target = "_blank"; b.className = "sec";
    b.href = "https://www.google.com/maps/dir/?api=1" + pOrigem + "&destination=" + p.lat + "," + p.lng;
    const e = document.createElement("button"); e.textContent = "Entregue"; e.className = "ent";
    e.onclick = async () => {
      if (!confirm("Marcar como entregue?\n" + p.endereco)) return;
      e.disabled = true;
      try {
        const resp = await fetch("/rotas/" + encodeURIComponent(id) + "/entregar/" + encodeURIComponent(p.id), {method: "POST", headers: {"X-Codigo": codigo}});
        if (!resp.ok && resp.status !== 404) throw new Error("falhou");
      } catch (err) { alert("Não consegui registrar a entrega. Tente de novo."); e.disabled = false; return; }
      atualizar();
    };
    const acoes = document.createElement("div"); acoes.className = "acoes"; acoes.append(a, b, e);
    li.append(t, acoes); lista.append(li);
  });
  const linha = (dados.trajeto && dados.trajeto.length > 1) ? dados.trajeto : pontos;
  L.polyline(linha, {color: "#2563eb", weight: 5, opacity: 0.8}).addTo(camada);
  mapa.fitBounds(pontos, {padding: [30, 30]});
}

async function atualizar() {
  if (!id) { document.getElementById("msg").textContent = "Digite seu nome e código acima e clique em Ver rota."; return; }
  try {
    const resp = await fetch("/rotas/" + encodeURIComponent(id), {headers: {"X-Codigo": codigo}});
    if (resp.status === 401) { ultimo = ""; camada.clearLayers(); document.getElementById("paradas").innerHTML = ""; document.getElementById("msg").textContent = "Nome ou código incorretos."; return; }
    const texto = await resp.text();
    if (texto !== ultimo) { ultimo = texto; desenhar(JSON.parse(texto)); }
  } catch (e) { ultimo = ""; document.getElementById("msg").textContent = "Sem conexão com a API."; }
}
atualizar(); setInterval(atualizar, 5000);
</script>
</body>
</html>
"""
