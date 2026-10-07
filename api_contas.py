"""
Endereços de login e da equipe do restaurante (veja contas.py).
"""

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

import banco
import contas
from seguranca import hash_codigo

router = APIRouter()


class PrimeiroAcesso(BaseModel):
    chave: str = Field(min_length=1, max_length=200)
    nome: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=120)
    senha: str = Field(max_length=200)


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    senha: str = Field(max_length=200)


class NovoUsuario(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=120)
    senha: str = Field(max_length=200)
    papel: Literal["dono", "funcionario"] = "funcionario"


class NovoConvite(BaseModel):
    papel: Literal["dono", "funcionario"] = "funcionario"


class AceitarConvite(BaseModel):
    convite: str = Field(min_length=10, max_length=200)
    nome: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=120)
    senha: str = Field(max_length=200)


class NovaSenha(BaseModel):
    senha: str = Field(max_length=200)


class TrocaSenha(BaseModel):
    atual: str = Field(max_length=200)
    nova: str = Field(max_length=200)


def _email_valido(email):
    email = contas.normalizar_email(email)
    if "@" not in email or "." not in email.split("@")[-1] or " " in email:
        raise HTTPException(status_code=422, detail="E-mail inválido.")
    return email


# ---------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------
@router.get("/auth/estado")
def estado():
    """Se já existe alguma conta (senão o painel mostra "Criar conta do dono")."""
    with banco.engine.connect() as con:
        return {"tem_usuarios": contas.tem_usuarios(con)}


@router.post("/auth/primeiro-acesso")
def primeiro_acesso(d: PrimeiroAcesso, request: Request):
    """Cria a conta do dono. Só funciona enquanto não existe nenhuma conta, e
    exige a API_KEY: quem não tem a chave não consegue "tomar" o sistema."""
    contas.config_ok()
    contas.conferir_api_key(request, d.chave)
    email = _email_valido(d.email)
    contas.validar_senha(d.senha)
    with banco.engine.begin() as con:
        if contas.tem_usuarios(con):
            raise HTTPException(status_code=409, detail="A conta do dono já foi criada. Entre com e-mail e senha.")
        con.execute(text("INSERT INTO usuarios (email, nome, senha, papel) VALUES (:e, :n, :s, 'dono')"),
                    {"e": email, "n": d.nome.strip(), "s": hash_codigo(d.senha)})
        token = contas.criar_sessao(con, email)
    return {"token": token, "nome": d.nome.strip(), "email": email, "papel": "dono"}


@router.get("/auth/convite")
def ver_convite(convite: str = ""):
    """Se o link de convite ainda vale, e para qual papel (o painel mostra o formulário certo)."""
    with banco.engine.connect() as con:
        c = contas.ler_convite(con, convite)
    if c is None:
        return {"valido": False}
    return {"valido": True, "papel": c.papel}


@router.post("/auth/aceitar-convite")
def aceitar_convite(d: AceitarConvite, request: Request):
    """Cria a conta a partir de um link de convite (uso único)."""
    chave_ip = "convite-ip:" + contas.seguranca._ip(request)
    contas.seguranca._checar_bloqueio(chave_ip)
    email = _email_valido(d.email)
    contas.validar_senha(d.senha)
    with banco.engine.begin() as con:
        c = contas.ler_convite(con, d.convite)
        if c is None:
            contas.seguranca._registrar_erro(chave_ip, contas.seguranca.LIMITE_POR_IP)
            raise HTTPException(status_code=410, detail="Este convite não vale mais (já foi usado ou venceu). Peça um novo.")
        existe = con.execute(text("SELECT 1 FROM usuarios WHERE email = :e"), {"e": email}).first()
        if existe:
            raise HTTPException(status_code=409, detail="Já existe uma conta com esse e-mail. Use \"Entrar\".")
        if not contas.usar_convite(con, d.convite):
            raise HTTPException(status_code=410, detail="Este convite acabou de ser usado. Peça um novo.")
        con.execute(text("INSERT INTO usuarios (email, nome, senha, papel) VALUES (:e, :n, :s, :p)"),
                    {"e": email, "n": d.nome.strip(), "s": hash_codigo(d.senha), "p": c.papel})
        token = contas.criar_sessao(con, email)
    return {"token": token, "nome": d.nome.strip(), "email": email, "papel": c.papel}


@router.post("/auth/entrar")
def entrar(d: Login, request: Request):
    email = contas.normalizar_email(d.email)
    with banco.engine.begin() as con:
        u = contas.conferir_login(con, request, email, d.senha)
        token = contas.criar_sessao(con, u.email)
    return {"token": token, "nome": u.nome, "email": u.email, "papel": u.papel}


@router.get("/auth/eu")
def quem_sou(acesso=Depends(contas.exigir_acesso)):
    return acesso


@router.post("/auth/sair")
def sair(authorization: str = Header(default="")):
    if authorization.lower().startswith("bearer "):
        with banco.engine.begin() as con:
            contas.apagar_sessao(con, authorization[7:].strip())
    return {"mensagem": "Até logo!"}


@router.post("/auth/senha")
def trocar_minha_senha(d: TrocaSenha, request: Request, acesso=Depends(contas.exigir_acesso)):
    """A pessoa troca a própria senha (precisa da atual). As outras sessões dela
    (outros computadores e celulares) são encerradas."""
    if acesso["tipo"] != "sessao":
        raise HTTPException(status_code=400, detail="O administrador não tem senha: ele entra com a chave.")
    contas.validar_senha(d.nova)
    with banco.engine.begin() as con:
        try:
            contas.conferir_login(con, request, acesso["email"], d.atual)
        except HTTPException as e:
            if e.status_code == 401:  # 401 faria o painel achar que a sessão acabou
                raise HTTPException(status_code=400, detail="Senha atual incorreta.")
            raise
        con.execute(text("UPDATE usuarios SET senha = :s WHERE email = :e"), {"s": hash_codigo(d.nova), "e": acesso["email"]})
        contas.apagar_sessoes_de(con, acesso["email"])
        token = contas.criar_sessao(con, acesso["email"])
    return {"mensagem": "Senha trocada.", "token": token}


# ---------------------------------------------------------------------
# Equipe (só dono ou administrador)
# ---------------------------------------------------------------------
@router.get("/usuarios")
def listar_usuarios(acesso=Depends(contas.exigir_gestor)):
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT email, nome, papel FROM usuarios ORDER BY papel, nome"))]
    return {"usuarios": linhas, "eu": acesso.get("email")}


@router.post("/usuarios")
def criar_usuario(d: NovoUsuario, acesso=Depends(contas.exigir_gestor)):
    if d.papel == "dono" and acesso["papel"] != "admin":
        raise HTTPException(status_code=403, detail="Só o administrador do sistema cadastra um dono.")
    email = _email_valido(d.email)
    contas.validar_senha(d.senha)
    with banco.engine.begin() as con:
        r = con.execute(text("INSERT INTO usuarios (email, nome, senha, papel) VALUES (:e, :n, :s, :p) "
                             "ON CONFLICT (email) DO NOTHING"),
                        {"e": email, "n": d.nome.strip(), "s": hash_codigo(d.senha), "p": d.papel})
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="Já existe uma conta com esse e-mail.")
    return {"mensagem": f"Conta de {d.nome.strip()} criada."}


@router.post("/convites")
def gerar_convite(d: NovoConvite, acesso=Depends(contas.exigir_gestor)):
    """Gera um link de convite (uso único, vence em 7 dias). Convite de dono, só o
    administrador gera: assim só você decide quem é dono de cada sistema."""
    if d.papel == "dono" and acesso["papel"] != "admin":
        raise HTTPException(status_code=403, detail="Só o administrador do sistema convida um dono.")
    with banco.engine.begin() as con:
        token = contas.criar_convite(con, d.papel, acesso.get("email") or "admin")
    return {"convite": token, "dias": contas.CONVITE_DIAS, "papel": d.papel}


@router.post("/usuarios/{email}/senha")
def definir_senha(email: str, d: NovaSenha, acesso=Depends(contas.exigir_gestor)):
    """Dono ou administrador define uma senha nova para alguém (ex.: esqueceu).
    A pessoa é desconectada de todos os aparelhos."""
    email = contas.normalizar_email(email)
    contas.validar_senha(d.senha)
    with banco.engine.begin() as con:
        r = con.execute(text("UPDATE usuarios SET senha = :s WHERE email = :e"), {"s": hash_codigo(d.senha), "e": email})
        if r.rowcount == 0:
            raise HTTPException(status_code=404, detail="Conta não encontrada.")
        contas.apagar_sessoes_de(con, email)
    return {"mensagem": "Senha nova definida. Passe para a pessoa e peça para ela trocar depois."}


@router.delete("/usuarios/{email}")
def remover_usuario(email: str, acesso=Depends(contas.exigir_gestor)):
    email = contas.normalizar_email(email)
    if email == acesso.get("email"):
        raise HTTPException(status_code=400, detail="Você não pode remover a sua própria conta.")
    with banco.engine.begin() as con:
        u = con.execute(text("SELECT papel FROM usuarios WHERE email = :e"), {"e": email}).first()
        if u is None:
            raise HTTPException(status_code=404, detail="Conta não encontrada.")
        if u.papel == "dono" and con.execute(text("SELECT COUNT(*) FROM usuarios WHERE papel = 'dono'")).scalar() <= 1:
            raise HTTPException(status_code=400, detail="Não dá para remover o único dono.")
        contas.apagar_sessoes_de(con, email)
        con.execute(text("DELETE FROM usuarios WHERE email = :e"), {"e": email})
    return {"mensagem": "Conta removida. A pessoa foi desconectada."}
