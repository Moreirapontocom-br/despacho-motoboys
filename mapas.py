"""
Mapas e distâncias: busca de endereço, tempo entre pontos pelas ruas e o
desenho do caminho no mapa.

Usa a LocationIQ quando existe LOCATIONIQ_KEY; senão, os serviços públicos
gratuitos (Nominatim e OSRM), que servem só para testes. Se o serviço falhar,
quem chama continua funcionando com a distância em linha reta.
"""

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from config import (INTERVALO_CONSULTA_S, LOCATIONIQ_KEY, LOCATIONIQ_URL, MAX_PONTOS_MATRIZ, OSRM_URL,
                    RESTAURANTE, USER_AGENT, log)


def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


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
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            dados = json.load(resp)
        if dados.get("code") != "Ok":
            raise ValueError(dados.get("code"))
        return dados
    except Exception as e:
        log.warning("Falha no serviço de rotas: %r", e)
        _osrm_falha_ate = time.time() + 60
        return None


def coords(pontos):
    return ";".join(f"{p['lng']:.6f},{p['lat']:.6f}" for p in pontos)


def matriz_duracoes(pontos):
    """Tempo de carro (segundos) entre todos os pares de pontos, pelas ruas. None se indisponível."""
    if len(pontos) < 2 or len(pontos) > MAX_PONTOS_MATRIZ:
        return None
    dados = _osrm_get("matriz", coords(pontos), "annotations=duration")
    matriz = dados.get("durations") if dados else None
    if not matriz or len(matriz) != len(pontos):
        return None
    return matriz


def custo_reta(a, b):
    """Estimativa em segundos pela linha reta (cerca de 30 km/h), usada quando as ruas não estão disponíveis."""
    return haversine(a["lat"], a["lng"], b["lat"], b["lng"]) * 120


def trajeto(origem, paradas):
    """Linha do caminho pelas ruas (lista de [lat, lng]) para desenhar no mapa. None se indisponível.
    O resultado fica guardado enquanto as paradas não mudam, para não consultar a cada atualização."""
    pontos = [origem] + list(paradas)
    if len(pontos) < 2 or len(pontos) > 25:
        return None
    chave = coords(pontos)
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
                                 headers={"User-Agent": USER_AGENT})
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
