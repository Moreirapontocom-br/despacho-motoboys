"""
Configurações do sistema, lidas das variáveis de ambiente (painel do Render,
em Environment). Nenhuma chave ou senha fica escrita no código.

A lista completa, com explicação, está no README.md.
"""

import logging
import os
import urllib.parse

log = logging.getLogger("uvicorn.error")


def _numero(nome, padrao):
    """Lê um número das variáveis de ambiente (aceita vírgula ou ponto decimal)."""
    try:
        return float(os.environ.get(nome, "").strip().replace(",", ".") or padrao)
    except ValueError:
        return padrao


# ---------------------------------------------------------------------
# Restaurantes
#
# Cada restaurante tem o próprio endereço, guardado no banco (o dono informa ao
# criar a conta e pode mudar na aba 👤 Conta). As variáveis abaixo só valem para
# instalações antigas, de um restaurante só: na atualização, os dados que já
# existiam passam a ser desse restaurante. Também centralizam o mapa antes do login.
# ---------------------------------------------------------------------
# Quantos restaurantes o sistema aceita. Para aumentar, mude no Render.
MAX_RESTAURANTES = int(_numero("MAX_RESTAURANTES", 6))
RESTAURANTE ={"lat": _numero("RESTAURANTE_LAT", -19.6156), "lng": _numero("RESTAURANTE_LNG", -43.2258),
               "endereco": os.environ.get("RESTAURANTE_ENDERECO", "").strip()}
# Aparece na mensagem de WhatsApp enviada ao cliente.
RESTAURANTE_NOME = os.environ.get("RESTAURANTE_NOME", "").strip()
# Cidade que já vem preenchida no cadastro de pedidos ("Cidade, UF").
RESTAURANTE_CIDADE = os.environ.get("RESTAURANTE_CIDADE", "").strip() or "Itabira, MG"

# Fuso horário do restaurante, em horas de diferença para o UTC (o banco guarda tudo em UTC).
# O Brasil não tem mais horário de verão desde 2019, então um número fixo é suficiente.
TIMEZONE_OFFSET_HORAS = _numero("TIMEZONE_OFFSET_HORAS", -3)

# ---------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY", "")

# ---------------------------------------------------------------------
# Despacho
# ---------------------------------------------------------------------
# Por quantos minutos a posição de GPS de um motoboy é considerada atual.
GPS_VALIDADE_MIN = int(_numero("GPS_VALIDADE_MIN", 10))
# Minutos na fila (sem despachar) até o pedido aparecer como atrasado no painel.
PEDIDO_ATRASO_MIN = _numero("PEDIDO_ATRASO_MIN", 15)
# Minutos em rota (desde o despacho) até o pedido aparecer como atrasado.
ROTA_ATRASO_MIN = _numero("ROTA_ATRASO_MIN", 40)
# PARADA_MIN: minutos gastos em cada entrega (estacionar, subir, receber).
# SAIDA_MIN: sem GPS, minutos após o despacho para considerar que o motoboy já saiu.
# MAX_PARADAS_VIAGEM: máximo de entregas numa mesma saída do restaurante.
PARADA_MIN = _numero("PARADA_MIN", 3)
SAIDA_MIN = _numero("SAIDA_MIN", 5)
MAX_PARADAS_VIAGEM = int(_numero("MAX_PARADAS_VIAGEM", 5))
# Prazo total (do pedido criado até a entrega) usado nas estatísticas de "entregue no prazo".
PRAZO_ENTREGA_MIN = _numero("PRAZO_ENTREGA_MIN", 45)
# Os quilômetros das estatísticas são estimados pela linha reta vezes este fator
# (as ruas fazem curvas; 1,3 é uma média comum em cidades).
FATOR_RUAS = _numero("FATOR_RUAS", 1.3)
# No modo automático, de quantos em quantos segundos o sistema confere a fila.
AUTOMATICO_INTERVALO_S = _numero("AUTOMATICO_INTERVALO_S", 20)

# ---------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------
# Sem DATABASE_URL, usa um arquivo SQLite local (só para testes no computador).
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///despacho_local.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# ---------------------------------------------------------------------
# Mapas e rotas
# ---------------------------------------------------------------------
# Serviço de rotas pelas ruas. "desligado" usa só linha reta.
OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").strip().rstrip("/")
# Com a chave da LocationIQ, rotas, busca de endereço e mapa usam a LocationIQ
# (permite uso comercial). Sem ela, usa os serviços públicos, que são só para testes.
LOCATIONIQ_KEY = os.environ.get("LOCATIONIQ_KEY", "").strip()
LOCATIONIQ_URL = "https://us1.locationiq.com/v1"
# Intervalo mínimo entre consultas: a LocationIQ gratuita aceita 2 por segundo;
# os serviços públicos pedem no máximo 1 por segundo.
INTERVALO_CONSULTA_S = 0.6 if LOCATIONIQ_KEY else 1.1
MAX_PONTOS_MATRIZ = 25 if LOCATIONIQ_KEY else 90  # a matriz da LocationIQ aceita até 25 pontos
USER_AGENT = "despacho-motoboys/1.0 (painel de restaurante pequeno)"

# Imagens do mapa: pedidas pelo navegador, então a chave fica visível na página.
# Por isso existe LOCATIONIQ_MAPA_KEY, uma segunda chave travada no painel da
# LocationIQ para funcionar só no endereço do seu site.
_chave_mapa = os.environ.get("LOCATIONIQ_MAPA_KEY", "").strip() or LOCATIONIQ_KEY
if _chave_mapa:
    MAPA_URL = "https://{s}-tiles.locationiq.com/v3/streets/r/{z}/{x}/{y}.png?key=" + urllib.parse.quote(_chave_mapa)
    MAPA_CREDITO = ('<a href="https://locationiq.com" target="_blank">Search by LocationIQ.com</a> | '
                    '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>')
else:
    MAPA_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    MAPA_CREDITO = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a> | rotas: OSRM'
