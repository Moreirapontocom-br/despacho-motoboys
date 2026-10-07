"""
Contas do restaurante: login com e-mail e senha.

Quem acessa o painel:
    dono          tudo, inclusive regras, motoboys e equipe
    funcionario   opera o dia a dia: pedidos, despacho, turno dos motoboys,
                  relatórios. Não mexe em regras, cadastro de motoboys nem equipe
    administrador quem tem a API_KEY (o dono do sistema, guardada no Render).
                  Acesso total, entra pelo link "Entrar com a chave de
                  administrador" e pode trocar a senha de qualquer pessoa.

A API_KEY também é usada por sistemas que mandam pedidos (integrações). O
restaurante nunca precisa conhecê-la.

Convites: o administrador gera um link de convite para o dono criar a conta;
o dono (ou o administrador) gera convites para funcionários. Cada link vale
uma vez só e expira em CONVITE_DIAS. No banco fica só o hash do link.

Senhas: no banco fica só o hash (PBKDF2, igual aos códigos dos motoboys).
Sessões: depois do login, o navegador recebe um token aleatório que dura
SESSAO_DIAS. No banco fica só o hash do token. "Sair" apaga a sessão.
"""

import hashlib
import secrets
from datetime import timedelta

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import text

import banco
import config
import seguranca
from horarios import agora_utc

SESSAO_DIAS = 30
CONVITE_DIAS = 7
PAPEIS = ("dono", "funcionario")
SENHA_MINIMA = 8


def preparar(eng):
    with eng.begin() as con:
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS usuarios (
                email TEXT PRIMARY KEY,
                nome TEXT NOT NULL,
                senha TEXT NOT NULL,
                papel TEXT NOT NULL DEFAULT 'funcionario',
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""))
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS sessoes (
                token TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expira_em TIMESTAMP NOT NULL
            )"""))
        con.execute(text("CREATE INDEX IF NOT EXISTS idx_sessoes_email ON sessoes (email)"))
        con.execute(text("""
            CREATE TABLE IF NOT EXISTS convites (
                token TEXT PRIMARY KEY,
                papel TEXT NOT NULL,
                criado_por TEXT,
                criado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expira_em TIMESTAMP NOT NULL,
                usado_em TIMESTAMP
            )"""))


preparar(banco.engine)


def normalizar_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_token(token: str) -> str:
    # O token é aleatório e longo (256 bits): um hash rápido basta.
    return hashlib.sha256(token.encode()).hexdigest()


def criar_sessao(con, email: str) -> str:
    token = secrets.token_urlsafe(32)
    agora = agora_utc()
    con.execute(text("DELETE FROM sessoes WHERE expira_em < :agora"), {"agora": agora})
    con.execute(text("INSERT INTO sessoes (token, email, expira_em) VALUES (:t, :e, :x)"),
                {"t": _hash_token(token), "e": email, "x": agora + timedelta(days=SESSAO_DIAS)})
    return token


def apagar_sessao(con, token: str):
    con.execute(text("DELETE FROM sessoes WHERE token = :t"), {"t": _hash_token(token)})


def apagar_sessoes_de(con, email: str):
    con.execute(text("DELETE FROM sessoes WHERE email = :e"), {"e": email})


def validar_senha(senha: str):
    if len(senha or "") < SENHA_MINIMA:
        raise HTTPException(status_code=422, detail=f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.")


def conferir_api_key(request: Request, chave: str):
    """Confere a API_KEY com o mesmo limite de tentativas do painel."""
    seguranca.exigir_chave(request, chave)


def conferir_login(con, request: Request, email: str, senha: str):
    """Confere e-mail e senha, com limite de tentativas por IP e por e-mail.
    Devolve a linha do usuário ou levanta 401."""
    chave_ip, chave_email = "login-ip:" + seguranca._ip(request), "login:" + email
    seguranca._checar_bloqueio(chave_ip, chave_email)
    u = con.execute(text("SELECT email, nome, senha, papel FROM usuarios WHERE email = :e"), {"e": email}).first()
    # Confere a senha mesmo sem usuário (com um hash qualquer), para o tempo de
    # resposta não revelar quais e-mails existem.
    ok = seguranca.codigo_confere(senha or "", u.senha if u else _HASH_FALSO)
    if u is None or not ok:
        seguranca._registrar_erro(chave_ip, seguranca.LIMITE_POR_IP)
        seguranca._registrar_erro(chave_email, seguranca.LIMITE_POR_MOTOBOY)
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")
    return u


_HASH_FALSO = seguranca.hash_codigo(secrets.token_hex(16))


def exigir_acesso(request: Request, x_api_key: str = Header(default=""), authorization: str = Header(default="")):
    """Quem está usando o painel. Aceita a sessão (login com e-mail e senha) ou a
    API_KEY (administrador e integrações). Devolve {"tipo", "papel", "nome", "email"}."""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        with banco.engine.connect() as con:
            u = con.execute(text(
                "SELECT u.email, u.nome, u.papel FROM sessoes s JOIN usuarios u ON u.email = s.email "
                "WHERE s.token = :t AND s.expira_em > :agora"), {"t": _hash_token(token), "agora": agora_utc()}).first()
        if u is None:
            raise HTTPException(status_code=401, detail="Sessão expirada. Entre de novo.")
        return {"tipo": "sessao", "papel": u.papel, "nome": u.nome, "email": u.email}
    seguranca.exigir_chave(request, x_api_key)
    return {"tipo": "admin", "papel": "admin", "nome": "Administrador", "email": None}


def exigir_gestor(acesso=Depends(exigir_acesso)):
    """Só dono ou administrador (regras, cadastro de motoboys, equipe)."""
    if acesso["papel"] not in ("dono", "admin"):
        raise HTTPException(status_code=403, detail="Só o dono do restaurante pode fazer isso.")
    return acesso


def tem_usuarios(con) -> bool:
    return con.execute(text("SELECT COUNT(*) FROM usuarios")).scalar() > 0


def config_ok():
    if not config.API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY não configurada no servidor")


# ---------------------------------------------------------------------
# Convites (link de uso único para criar conta)
# ---------------------------------------------------------------------
def criar_convite(con, papel: str, criado_por: str) -> str:
    token = secrets.token_urlsafe(24)
    con.execute(text("INSERT INTO convites (token, papel, criado_por, expira_em) VALUES (:t, :p, :c, :x)"),
                {"t": _hash_token(token), "p": papel, "c": criado_por,
                 "x": agora_utc() + timedelta(days=CONVITE_DIAS)})
    return token


def ler_convite(con, token: str):
    """O convite, se ainda vale (não usado e não vencido); senão None."""
    return con.execute(text(
        "SELECT token, papel, expira_em FROM convites WHERE token = :t AND usado_em IS NULL AND expira_em > :agora"),
        {"t": _hash_token(token or ""), "agora": agora_utc()}).first()


def usar_convite(con, token: str) -> bool:
    """Marca como usado. Devolve False se outra pessoa usou no mesmo instante."""
    r = con.execute(text("UPDATE convites SET usado_em = :agora WHERE token = :t AND usado_em IS NULL AND expira_em > :agora"),
                    {"t": _hash_token(token or ""), "agora": agora_utc()})
    return r.rowcount == 1
