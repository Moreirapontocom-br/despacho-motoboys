"""
API de despacho automático de motoboys - versão 3 (com banco de dados e segurança).

Variáveis de ambiente (configuradas no painel do Render, nunca dentro do código):
    DATABASE_URL  endereço do banco Postgres. Se não existir, usa um arquivo
                  SQLite local (só para testes no seu computador).
    API_KEY       chave secreta exigida em /pedidos, /motoboys, /despachar e /status.
                  Deve ser enviada no cabeçalho  X-API-Key.

O motoboy não usa a API_KEY. Cada motoboy tem um código próprio (definido ao
cadastrá-lo) e só consegue ver a rota dele, enviando esse código no cabeçalho X-Codigo.
"""

import json
import math
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

app = FastAPI(title="API de Despacho Automático")

RESTAURANTE = {"lat": -19.6156, "lng": -43.2258}
API_KEY = os.environ.get("API_KEY", "")

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


def ordenar_rota(origem, pedidos):
    restantes = pedidos.copy()
    rota = []
    atual = origem
    while restantes:
        proximo = min(restantes, key=lambda p: haversine(atual["lat"], atual["lng"], p["lat"], p["lng"]))
        rota.append(proximo)
        atual = proximo
        restantes.remove(proximo)
    return rota


def atribuir_motoboys(lotes, motoboys):
    carga = {m["id"]: 0 for m in motoboys}
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


@app.post("/despachar", dependencies=[Depends(exigir_chave)])
def despachar():
    """Agrupa os pedidos pendentes, monta as rotas e distribui entre os motoboys.
    A rota nova de um motoboy substitui a anterior dele."""
    with engine.begin() as con:
        pedidos = [dict(r._mapping) for r in con.execute(text(
            "SELECT id, endereco, lat, lng FROM pedidos WHERE status = 'pendente' ORDER BY criado_em, id"))]
        motoboys = [dict(r._mapping) for r in con.execute(text("SELECT id, lat, lng FROM motoboys"))]
        if not pedidos or not motoboys:
            return {"mensagem": "Sem pedidos ou motoboys suficientes para despachar."}

        atribuicoes = atribuir_motoboys(agrupar_pedidos(pedidos), motoboys)
        resultado = {}
        for motoboy_id, lista in atribuicoes.items():
            if not lista:
                continue
            rota = ordenar_rota(RESTAURANTE, lista)
            con.execute(text("UPDATE pedidos SET status = 'concluido' WHERE motoboy_id = :m AND status = 'despachado'"),
                        {"m": motoboy_id})
            for ordem, p in enumerate(rota, start=1):
                con.execute(text("UPDATE pedidos SET status = 'despachado', motoboy_id = :m, ordem = :o WHERE id = :id"),
                            {"m": motoboy_id, "o": ordem, "id": p["id"]})
            resultado[motoboy_id] = rota
    return {"rotas": resultado}


@app.get("/status", dependencies=[Depends(exigir_chave)])
def status():
    with engine.connect() as con:
        por_status = {s: n for s, n in con.execute(text("SELECT status, COUNT(*) FROM pedidos GROUP BY status"))}
        motoboys = con.execute(text("SELECT COUNT(*) FROM motoboys")).scalar()
    return {"pedidos_por_status": por_status, "motoboys_cadastrados": motoboys}


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
    with urllib.request.urlopen(req, timeout=10) as resp:
        dados = json.load(resp)
    return [{"rotulo": x["display_name"], "lat": float(x["lat"]), "lng": float(x["lon"])} for x in dados]


@app.post("/geocodificar", dependencies=[Depends(exigir_chave)])
def geocodificar(c: Consulta):
    try:
        return {"resultados": buscar_endereco(c.endereco)}
    except Exception:
        raise HTTPException(status_code=502, detail="Não consegui consultar o serviço de endereços agora. Tente de novo.")


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
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f5f5f5;color:#222}
 header{padding:12px 16px;background:#1f2937;color:#fff;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 main{max-width:720px;margin:0 auto;padding:12px}
 section{background:#fff;border-radius:10px;padding:14px;margin:12px 0}
 h2{margin:0 0 10px;font-size:1.05rem}
 input{padding:8px;border-radius:6px;border:1px solid #ccc;font-size:1rem}
 button{padding:8px 14px;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:1rem;cursor:pointer}
 button.verde{background:#10b981} button:disabled{opacity:.5}
 .linha{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
 .linha input{flex:1;min-width:160px}
 ul{list-style:none;margin:0;padding:0}
 li{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-top:1px solid #eee}
 li:first-child{border-top:0}
 .tag{font-size:.8rem;padding:2px 8px;border-radius:10px;background:#e5e7eb;white-space:nowrap}
 .ok{color:#047857} .erro{color:#b91c1c}
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
  <h2>1. Novo pedido</h2>
  <div class="linha">
   <input id="endereco" placeholder="Rua e número (ex.: Rua Tal, 123, Bairro)">
   <input id="cidade" value="Itabira, MG" size="12">
   <button id="buscar">Buscar endereço</button>
  </div>
  <div id="msgBusca"></div>
  <ul id="candidatos"></ul>
 </section>
 <section>
  <h2>2. Pedidos na fila</h2>
  <ul id="fila"></ul>
  <div class="linha" style="margin-top:10px">
   <button id="despachar" class="verde">Despachar agora</button>
  </div>
  <div id="msgDespacho"></div>
 </section>
</main>
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

$("salvar").onclick = () => { chave = $("chave").value.trim(); sessionStorage.setItem("chave", chave); carregarFila(); };

$("buscar").onclick = async () => {
  const end = $("endereco").value.trim();
  if (end.length < 3) { dizer($("msgBusca"), "Digite o endereço.", "erro"); return; }
  textoDigitado = end;
  $("candidatos").innerHTML = "";
  dizer($("msgBusca"), "Buscando...");
  try {
    const r = await api("/geocodificar", "POST", {endereco: end + ", " + $("cidade").value.trim()});
    if (!r.resultados.length) { dizer($("msgBusca"), "Não achei. Tente sem o bairro ou confira o nome da rua.", "erro"); return; }
    dizer($("msgBusca"), "Clique no endereço correto:");
    r.resultados.forEach((c) => {
      const li = document.createElement("li");
      const t = document.createElement("span"); t.textContent = c.rotulo;
      const b = document.createElement("button"); b.textContent = "Usar este";
      b.onclick = () => criarPedido(c, b);
      li.append(t, b); $("candidatos").append(li);
    });
  } catch (e) { dizer($("msgBusca"), e.message, "erro"); }
};

async function criarPedido(c, botao) {
  botao.disabled = true;
  const id = "P" + Date.now().toString(36).toUpperCase();
  try {
    await api("/pedidos", "POST", {id: id, endereco: textoDigitado, lat: c.lat, lng: c.lng});
    dizer($("msgBusca"), "Pedido " + id + " criado.", "ok");
    $("candidatos").innerHTML = ""; $("endereco").value = "";
    carregarFila();
  } catch (e) { dizer($("msgBusca"), e.message, "erro"); botao.disabled = false; }
}

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

carregarFila(); setInterval(carregarFila, 10000);
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
    return {"motoboy_id": motoboy_id, "restaurante": RESTAURANTE, "paradas": paradas}


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
 #mapa{height:50vh}
 #msg{padding:12px 16px}
 #paradas{list-style:none;margin:0;padding:0 8px 8px}
 #paradas li{background:#fff;margin:8px 0;padding:10px 12px;border-radius:8px;display:flex;justify-content:space-between;align-items:center;gap:8px}
 #paradas a{background:#2563eb;color:#fff;padding:6px 10px;border-radius:6px;text-decoration:none}
 .num{background:#2563eb;color:#fff;border-radius:50%;width:26px;height:26px;display:flex;align-items:center;justify-content:center;font-weight:bold;border:2px solid #fff}
 .rest{background:#10b981}
</style>
</head>
<body>
<header>
 <strong>Motoboy:</strong>
 <input id="nome" placeholder="seu nome (id)">
 <input id="codigo" type="password" size="8" placeholder="código">
 <button id="ir">Ver rota</button>
</header>
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
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {maxZoom: 19, attribution: "&copy; OpenStreetMap"}).addTo(mapa);
const camada = L.layerGroup().addTo(mapa);
let ultimo = "";

function icone(texto, classe) {
  return L.divIcon({className: "", html: '<div class="num ' + (classe || "") + '">' + texto + "</div>", iconSize: [26, 26]});
}

function desenhar(dados) {
  const msg = document.getElementById("msg");
  const lista = document.getElementById("paradas");
  camada.clearLayers(); lista.innerHTML = "";
  if (!dados.paradas.length) { msg.textContent = "Nenhuma rota no momento. Esta página atualiza sozinha."; return; }
  msg.textContent = dados.paradas.length + " parada(s) na sua rota:";
  const r = dados.restaurante, pontos = [[r.lat, r.lng]];
  L.marker([r.lat, r.lng], {icon: icone("R", "rest")}).addTo(camada);
  dados.paradas.forEach((p, i) => {
    pontos.push([p.lat, p.lng]);
    L.marker([p.lat, p.lng], {icon: icone(i + 1)}).addTo(camada);
    const li = document.createElement("li");
    const t = document.createElement("span"); t.textContent = (i + 1) + ". " + p.id + " - " + p.endereco;
    const a = document.createElement("a"); a.textContent = "Navegar"; a.target = "_blank";
    a.href = "https://www.google.com/maps/dir/?api=1&destination=" + p.lat + "," + p.lng;
    li.append(t, a); lista.append(li);
  });
  L.polyline(pontos, {color: "#2563eb"}).addTo(camada);
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
