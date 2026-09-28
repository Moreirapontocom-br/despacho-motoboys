"""
API do sistema de despacho automático de motoboys.

Como rodar:
    pip install fastapi uvicorn
    uvicorn api_despacho:app --reload

Depois é só abrir no navegador:
    http://127.0.0.1:8000/docs
(o FastAPI cria essa página de testes sozinho, com todos os endpoints)

Esta versão guarda tudo em memória (listas Python), só para fins de
demonstração. Numa API real, os pedidos, motoboys e rotas ficariam
salvos num banco de dados (Postgres, MySQL etc.), para não se perderem
quando a API reiniciar.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import math

app = FastAPI(title="API de Despacho Automático")

# "Banco de dados" em memória (só para teste)
pedidos_pendentes = []
motoboys_cadastrados = []

RESTAURANTE = {"lat": -19.6156, "lng": -43.2258}
rotas_por_motoboy = {}  # guarda a rota atual de cada motoboy


# ---------------------------------------------------------------------
# Formatos de dados que a API aceita (validados automaticamente)
# ---------------------------------------------------------------------
class Pedido(BaseModel):
    id: str
    endereco: str
    lat: float
    lng: float


class Motoboy(BaseModel):
    id: str
    lat: float
    lng: float


# ---------------------------------------------------------------------
# Funções do algoritmo (mesma lógica do protótipo anterior)
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
# Endpoints (as "portas de entrada" que o site/app vai chamar)
# ---------------------------------------------------------------------
@app.post("/pedidos")
def criar_pedido(pedido: Pedido):
    """O sistema de pedidos do restaurante chama isso quando um pedido novo entra."""
    pedidos_pendentes.append(pedido.dict())
    return {"mensagem": "Pedido recebido", "total_pendentes": len(pedidos_pendentes)}


@app.post("/motoboys")
def cadastrar_motoboy(motoboy: Motoboy):
    """O app do motoboy chama isso quando ele fica disponível."""
    motoboys_cadastrados.append(motoboy.dict())
    return {"mensagem": "Motoboy disponível", "total_disponiveis": len(motoboys_cadastrados)}


@app.post("/despachar")
def despachar():
    """
    Roda o algoritmo agora: agrupa os pedidos pendentes, monta as rotas
    e distribui entre os motoboys disponíveis. Depois de despachar,
    limpa a fila (os pedidos já foram roteados).
    """
    if not pedidos_pendentes or not motoboys_cadastrados:
        return {"mensagem": "Sem pedidos ou motoboys suficientes para despachar."}

    lotes = agrupar_pedidos(pedidos_pendentes)
    atribuicoes = atribuir_motoboys(lotes, motoboys_cadastrados)

    resultado = {}
    for motoboy_id, pedidos_do_motoboy in atribuicoes.items():
        if pedidos_do_motoboy:
            rota = ordenar_rota(RESTAURANTE, pedidos_do_motoboy)
            resultado[motoboy_id] = rota
            rotas_por_motoboy[motoboy_id] = rota

    pedidos_pendentes.clear()
    return {"rotas": resultado}


@app.get("/status")
def status():
    """Só para conferir o que está na fila no momento."""
    return {"pedidos_pendentes": len(pedidos_pendentes), "motoboys_disponiveis": len(motoboys_cadastrados)}


@app.get("/rotas/{motoboy_id}")
def rota_do_motoboy(motoboy_id: str):
    """A página do motoboy chama isso para saber a rota dele."""
    return {
        "motoboy_id": motoboy_id,
        "restaurante": RESTAURANTE,
        "paradas": rotas_por_motoboy.get(motoboy_id, []),
    }


@app.get("/motoboy")
def pagina_do_motoboy():
    """Entrega a página com o mapa. Abra em /motoboy?id=NOME"""
    return HTMLResponse(PAGINA_HTML)


# Página do motoboy (mapa), embutida aqui para não depender de outro arquivo
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
 <button id="ir">Ver rota</button>
</header>
<div id="mapa"></div>
<div id="msg"></div>
<ul id="paradas"></ul>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
const id = new URLSearchParams(location.search).get("id") || "";
const nome = document.getElementById("nome"); nome.value = id;
document.getElementById("ir").onclick = () => { location.search = "?id=" + encodeURIComponent(nome.value.trim()); };

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
  if (!id) { document.getElementById("msg").textContent = "Digite seu nome acima e clique em Ver rota."; return; }
  try {
    const texto = await (await fetch("/rotas/" + encodeURIComponent(id))).text();
    if (texto !== ultimo) { ultimo = texto; desenhar(JSON.parse(texto)); }
  } catch (e) { ultimo = ""; document.getElementById("msg").textContent = "Sem conexão com a API."; }
}
atualizar(); setInterval(atualizar, 5000);
</script>
</body>
</html>
"""
