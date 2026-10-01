"""
Segurança: chave do painel, códigos dos motoboys e limite de tentativas erradas.

- O painel do restaurante exige a API_KEY no cabeçalho X-API-Key.
- Cada motoboy tem um código próprio (cabeçalho X-Codigo). No banco fica só o
  hash do código, nunca o código em si.
- Erros demais bloqueiam por 15 minutos, por IP e (no caso do motoboy) por nome.
"""

import hashlib
import math
import secrets
import threading
import time

from fastapi import Header, HTTPException, Request
from sqlalchemy import text

import config

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
    """IP de quem chamou, usado para contar tentativas erradas.

    Não dá para confiar no primeiro item do X-Forwarded-For: quem escreve esse
    valor é o próprio navegador/atacante, e o Render só acrescenta itens no fim.
    Quem manda um IP inventado a cada tentativa nunca seria bloqueado.
    Por isso a ordem de preferência é:
      1. CF-Connecting-IP: a Cloudflare (que fica na frente do Render) sempre
         escreve esse cabeçalho por cima do que o cliente mandar;
      2. último item do X-Forwarded-For (acrescentado pelo proxy, não pelo cliente);
      3. o IP da conexão (uso local, sem proxy).
    True-Client-IP NÃO é usado: a Cloudflare só sobrescreve esse cabeçalho no plano
    Enterprise; fora dele, o atacante poderia inventar um valor a cada tentativa."""
    valor = request.headers.get("cf-connecting-ip", "").strip()
    if valor:
        return valor
    encaminhado = request.headers.get("x-forwarded-for", "")
    if encaminhado:
        return encaminhado.split(",")[-1].strip()
    return request.client.host if request.client else "?"


# Aviso nos logs se a chave do painel for curta: com uma chave longa e aleatória,
# adivinhar por tentativa e erro é impossível, mesmo sem limite de tentativas.
# Gere uma boa com:  python -c "import secrets; print(secrets.token_urlsafe(32))"
if config.API_KEY and len(config.API_KEY) < 24:
    config.log.warning("API_KEY tem só %d caracteres. Use uma chave de 32+ caracteres aleatórios.", len(config.API_KEY))

# ---------------------------------------------------------------------
# Códigos dos motoboys: no banco fica só um "hash" (uma impressão digital do
# código), nunca o código em si. Quem vir o banco não descobre os códigos.
# ---------------------------------------------------------------------
_PBKDF2_ITERACOES = 200_000


def hash_codigo(codigo: str) -> str:
    sal = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", codigo.encode(), sal, _PBKDF2_ITERACOES)
    return f"pbkdf2${_PBKDF2_ITERACOES}${sal.hex()}${h.hex()}"


# Calcular o hash é propositalmente lento (~0,1 s). Como a página do motoboy
# consulta a cada 5 s, guardamos na memória os códigos que já conferiram certo,
# para não repetir a conta toda vez. Se o código for trocado, o hash muda e o
# que estava guardado deixa de valer.
_codigos_conferidos = set()
_trava_conferidos = threading.Lock()


def _marca(codigo: str, guardado: str) -> str:
    return hashlib.sha256((guardado + "\0" + codigo).encode()).hexdigest()


def codigo_ja_conferido(codigo: str, guardado: str) -> bool:
    """True se este código já conferiu certo antes (está no cache). É rápido e só
    dá True para quem sabe o código, então pode ser usado antes do bloqueio."""
    if not codigo or not guardado.startswith("pbkdf2$"):
        return False
    with _trava_conferidos:
        return _marca(codigo, guardado) in _codigos_conferidos


def codigo_confere(codigo: str, guardado: str) -> bool:
    if not guardado.startswith("pbkdf2$"):
        # Código antigo ainda sem hash (banco.preparar converte todos ao iniciar).
        return secrets.compare_digest(codigo.encode(), guardado.encode())
    marca = _marca(codigo, guardado)
    with _trava_conferidos:
        if marca in _codigos_conferidos:
            return True
    try:
        _, iteracoes, sal, esperado = guardado.split("$")
        h = hashlib.pbkdf2_hmac("sha256", codigo.encode(), bytes.fromhex(sal), int(iteracoes))
    except ValueError:
        return False
    ok = secrets.compare_digest(h.hex(), esperado)
    if ok:
        with _trava_conferidos:
            if len(_codigos_conferidos) > 1000:
                _codigos_conferidos.clear()
            _codigos_conferidos.add(marca)
    return ok


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
    if not config.API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY não configurada no servidor")
    chave_ip = "painel-ip:" + _ip(request)
    _checar_bloqueio(chave_ip)
    if not secrets.compare_digest(x_api_key.encode(), config.API_KEY.encode()):
        _registrar_erro(chave_ip, LIMITE_POR_IP)
        raise HTTPException(status_code=401, detail="Chave de API inválida ou ausente")


def conferir_codigo(con, request: Request, motoboy_id: str, codigo: str):
    """Confere o código do motoboy, com limite de tentativas erradas.

    O bloqueio por nome vale para todos os IPs; sem cuidado, qualquer pessoa que
    soubesse o nome ("Carlos") poderia errar 30 vezes de propósito e deixar o
    motoboy 15 min sem ver a rota. Por isso, quem manda um código que já conferiu
    certo antes (cache em memória) passa direto, mesmo com o nome bloqueado.
    O atacante não consegue usar esse atalho sem saber o código."""
    chave_ip, chave_moto = "moto-ip:" + _ip(request), "moto:" + motoboy_id.lower()
    m = con.execute(text("SELECT codigo FROM motoboys WHERE id = :id"), {"id": motoboy_id}).first()
    if m is not None and codigo_ja_conferido(codigo, m[0]):
        return
    _checar_bloqueio(chave_ip, chave_moto)
    if m is None or not codigo or not codigo_confere(codigo, m[0]):
        _registrar_erro(chave_ip, LIMITE_POR_IP)
        _registrar_erro(chave_moto, LIMITE_POR_MOTOBOY)
        raise HTTPException(status_code=401, detail="Nome ou código incorretos")

