"""
Endereços de login, da equipe e dos dados de cada restaurante (veja contas.py
e restaurantes.py).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

import banco
import config
import contas
import restaurantes
from horarios import inicio_do_dia_utc
from mapas import buscar_endereco
from seguranca import hash_codigo

router = APIRouter()


class DadosRestaurante(BaseModel):
    """lat/lng: o ponto confirmado no mapa (de onde os motoboys saem)."""
    nome: str = Field(min_length=1, max_length=80)
    endereco: str = Field(min_length=3, max_length=200)
    cidade: str = Field(min_length=2, max_length=80)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class BuscaEndereco(BaseModel):
    endereco: str = Field(min_length=3, max_length=300)
    convite: Optional[str] = Field(default=None, max_length=200)


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
    # Obrigatório no convite de dono de restaurante novo.
    restaurante: Optional[DadosRestaurante] = None


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
    exige a API_KEY: quem não tem a chave não consegue "tomar" o sistema.
    Vem das instalações de um restaurante só: a conta fica no restaurante padrão.
    Para restaurantes novos, use o convite de dono."""
    contas.config_ok()
    contas.conferir_api_key(request, d.chave)
    email = _email_valido(d.email)
    contas.validar_senha(d.senha)
    with banco.engine.begin() as con:
        if contas.tem_usuarios(con):
            raise HTTPException(status_code=409, detail="A conta do dono já foi criada. Entre com e-mail e senha.")
        con.execute(text("INSERT INTO usuarios (email, nome, senha, papel, restaurante_id) VALUES (:e, :n, :s, 'dono', :r)"),
                    {"e": email, "n": d.nome.strip(), "s": hash_codigo(d.senha), "r": banco.restaurante_padrao(con)})
        token = contas.criar_sessao(con, email)
    return {"token": token, "nome": d.nome.strip(), "email": email, "papel": "dono"}


@router.get("/auth/convite")
def ver_convite(convite: str = ""):
    """Se o link de convite ainda vale, e para qual papel (o painel mostra o formulário certo).
    novo_restaurante: o convite cria um restaurante (o painel pede o endereço dele)."""
    with banco.engine.connect() as con:
        c = contas.ler_convite(con, convite)
    if c is None:
        return {"valido": False}
    return {"valido": True, "papel": c.papel, "novo_restaurante": c.restaurante_id is None}


def _buscar(texto):
    """Busca de endereço sem limitar a região (o restaurante pode ser em qualquer cidade)."""
    try:
        return {"resultados": buscar_endereco(texto)[:5]}
    except Exception as e:
        config.log.warning("Falha na busca do endereço do restaurante: %r", e)
        return {"resultados": [], "aviso": "O serviço de endereços não respondeu agora. Clique no mapa no ponto do restaurante."}


@router.post("/auth/convite/endereco")
def localizar_pelo_convite(d: BuscaEndereco):
    """Acha o endereço do restaurante no mapa enquanto o dono cria a conta.
    Só funciona com um convite válido (não é uma busca aberta para qualquer um)."""
    with banco.engine.connect() as con:
        if contas.ler_convite(con, d.convite or "") is None:
            raise HTTPException(status_code=410, detail="Este convite não vale mais (já foi usado ou venceu). Peça um novo.")
    return _buscar(d.endereco)


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
        rid = c.restaurante_id
        if rid is None:  # convite de dono de restaurante novo: cria o restaurante com o endereço informado
            if d.restaurante is None:
                raise HTTPException(status_code=422, detail="Informe o nome e o endereço do restaurante.")
            r = d.restaurante
            rid = restaurantes.criar(con, r.nome, r.endereco, r.cidade, r.lat, r.lng)
        if not contas.usar_convite(con, d.convite):
            raise HTTPException(status_code=410, detail="Este convite acabou de ser usado. Peça um novo.")
        con.execute(text("INSERT INTO usuarios (email, nome, senha, papel, restaurante_id) VALUES (:e, :n, :s, :p, :r)"),
                    {"e": email, "n": d.nome.strip(), "s": hash_codigo(d.senha), "p": c.papel, "r": rid})
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
    """Quem entrou e o restaurante em que está mexendo (None: administrador ainda não escolheu)."""
    with banco.engine.connect() as con:
        r = restaurantes.ler(con, acesso["restaurante_id"]) if acesso["restaurante_id"] is not None else None
    return {**acesso, "restaurante": r}


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
# Equipe (só dono ou administrador). Cada dono só vê e mexe na equipe do
# próprio restaurante; o administrador, na do restaurante escolhido.
# ---------------------------------------------------------------------
@router.get("/usuarios")
def listar_usuarios(acesso=Depends(contas.exigir_gestor), rid=Depends(contas.restaurante_atual)):
    with banco.engine.connect() as con:
        linhas = [dict(r._mapping) for r in con.execute(text(
            "SELECT email, nome, papel FROM usuarios WHERE restaurante_id = :r ORDER BY papel, nome"), {"r": rid})]
    return {"usuarios": linhas, "eu": acesso.get("email")}


@router.post("/usuarios")
def criar_usuario(d: NovoUsuario, acesso=Depends(contas.exigir_gestor), rid=Depends(contas.restaurante_atual)):
    if d.papel == "dono" and acesso["papel"] != "admin":
        raise HTTPException(status_code=403, detail="Só o administrador do sistema cadastra um dono.")
    email = _email_valido(d.email)
    contas.validar_senha(d.senha)
    with banco.engine.begin() as con:
        r = con.execute(text("INSERT INTO usuarios (email, nome, senha, papel, restaurante_id) VALUES (:e, :n, :s, :p, :r) "
                             "ON CONFLICT (email) DO NOTHING"),
                        {"e": email, "n": d.nome.strip(), "s": hash_codigo(d.senha), "p": d.papel, "r": rid})
        if r.rowcount == 0:
            raise HTTPException(status_code=409, detail="Já existe uma conta com esse e-mail.")
    return {"mensagem": f"Conta de {d.nome.strip()} criada."}


@router.post("/convites")
def gerar_convite(d: NovoConvite, acesso=Depends(contas.exigir_gestor)):
    """Gera um link de convite (uso único, vence em 7 dias).
    - Funcionário: entra no restaurante de quem convidou.
    - Dono: só o administrador gera, e o link cria um restaurante novo (o dono
      informa o endereço ao criar a conta). Assim só você decide quem entra no sistema."""
    if d.papel == "dono":
        if acesso["papel"] != "admin":
            raise HTTPException(status_code=403, detail="Só o administrador do sistema convida um dono.")
        rid = None
        with banco.engine.connect() as con:
            restaurantes.conferir_limite(con)
    else:
        rid = contas.restaurante_atual(acesso)
    with banco.engine.begin() as con:
        token = contas.criar_convite(con, d.papel, acesso.get("email") or "admin", rid)
    return {"convite": token, "dias": contas.CONVITE_DIAS, "papel": d.papel}


def _conta_do_restaurante(con, email, acesso):
    """Papel da conta, se ela for do restaurante de quem pede (o administrador mexe em qualquer uma)."""
    u = con.execute(text("SELECT papel, restaurante_id FROM usuarios WHERE email = :e"), {"e": email}).first()
    if u is None or (acesso["papel"] != "admin" and u.restaurante_id != acesso["restaurante_id"]):
        raise HTTPException(status_code=404, detail="Conta não encontrada.")
    return u


@router.post("/usuarios/{email}/senha")
def definir_senha(email: str, d: NovaSenha, acesso=Depends(contas.exigir_gestor)):
    """Dono ou administrador define uma senha nova para alguém (ex.: esqueceu).
    A pessoa é desconectada de todos os aparelhos."""
    email = contas.normalizar_email(email)
    contas.validar_senha(d.senha)
    with banco.engine.begin() as con:
        _conta_do_restaurante(con, email, acesso)
        con.execute(text("UPDATE usuarios SET senha = :s WHERE email = :e"), {"s": hash_codigo(d.senha), "e": email})
        contas.apagar_sessoes_de(con, email)
    return {"mensagem": "Senha nova definida. Passe para a pessoa e peça para ela trocar depois."}


@router.delete("/usuarios/{email}")
def remover_usuario(email: str, acesso=Depends(contas.exigir_gestor)):
    email = contas.normalizar_email(email)
    if email == acesso.get("email"):
        raise HTTPException(status_code=400, detail="Você não pode remover a sua própria conta.")
    with banco.engine.begin() as con:
        u = _conta_do_restaurante(con, email, acesso)
        donos = con.execute(text("SELECT COUNT(*) FROM usuarios WHERE papel = 'dono' AND restaurante_id = :r"),
                            {"r": u.restaurante_id}).scalar()
        if u.papel == "dono" and donos <= 1:
            raise HTTPException(status_code=400, detail="Não dá para remover o único dono do restaurante.")
        contas.apagar_sessoes_de(con, email)
        con.execute(text("DELETE FROM usuarios WHERE email = :e"), {"e": email})
    return {"mensagem": "Conta removida. A pessoa foi desconectada."}


# ---------------------------------------------------------------------
# Dados do restaurante (endereço de onde os motoboys saem)
# ---------------------------------------------------------------------
@router.get("/restaurante")
def ver_restaurante(rid=Depends(contas.restaurante_atual)):
    with banco.engine.connect() as con:
        return restaurantes.ler(con, rid)


@router.put("/restaurante")
def mudar_restaurante(d: DadosRestaurante, _=Depends(contas.exigir_gestor), rid=Depends(contas.restaurante_atual)):
    """Só o dono (ou o administrador) muda o nome e o endereço do restaurante."""
    with banco.engine.begin() as con:
        restaurantes.atualizar(con, rid, d.nome, d.endereco, d.cidade, d.lat, d.lng)
        return {"mensagem": "Dados do restaurante salvos.", "restaurante": restaurantes.ler(con, rid)}


@router.post("/restaurante/localizar")
def localizar_restaurante(d: BuscaEndereco, _=Depends(contas.exigir_gestor)):
    """Acha no mapa o endereço novo do restaurante (o dono confere o pino antes de salvar)."""
    return _buscar(d.endereco)


_CONTAGENS = {
    "na_fila": "SELECT COUNT(*) FROM pedidos WHERE restaurante_id = :r AND status = 'pendente'",
    "em_rota": "SELECT COUNT(*) FROM pedidos WHERE restaurante_id = :r AND status = 'despachado'",
    "entregues_hoje": "SELECT COUNT(*) FROM pedidos WHERE restaurante_id = :r AND status = 'concluido' AND concluido_em >= :ini",
    "motoboys_de_turno": "SELECT COUNT(*) FROM motoboys WHERE restaurante_id = :r AND ativo",
}


@router.get("/restaurantes")
def listar_restaurantes(_=Depends(contas.exigir_admin)):
    """Só o administrador: todos os restaurantes e o que cada um está fazendo hoje."""
    inicio = inicio_do_dia_utc()
    with banco.engine.connect() as con:
        lista = restaurantes.listar(con)
        for r in lista:
            p = {"r": r["id"], "ini": inicio}
            for campo, sql in _CONTAGENS.items():
                r[campo] = con.execute(text(sql), p).scalar()
            r["donos"] = [e for (e,) in con.execute(text(
                "SELECT email FROM usuarios WHERE restaurante_id = :r AND papel = 'dono' ORDER BY email"), p)]
    return {"restaurantes": lista, "limite": config.MAX_RESTAURANTES}
