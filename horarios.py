"""
Horários. Regra única do sistema: o banco guarda tudo em UTC, sem fuso, e o
horário do restaurante (TIMEZONE_OFFSET_HORAS) só é usado para mostrar na tela
e para saber onde começa o "hoje".

Para isso valer também no Postgres, banco.py manda a conexão usar UTC (senão o
CURRENT_TIMESTAMP gravado dependeria do fuso configurado no servidor do banco).
"""

from datetime import datetime, timedelta, timezone

import config

# Tolerância para relógios levemente diferentes entre o servidor e o banco.
FOLGA_RELOGIO_S = 120


def agora_utc():
    """Agora, em UTC e sem fuso (o mesmo formato que o banco guarda).
    Substitui datetime.utcnow(), que está obsoleto desde o Python 3.12."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_ts(v):
    """Converte um horário vindo do banco (datetime já pronto, ou texto no caso do
    SQLite) para datetime em UTC sem fuso."""
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace(" ", "T", 1))
    if v.tzinfo:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def gps_recente(gps_em, agora=None):
    """True se o último GPS foi recebido há menos de GPS_VALIDADE_MIN minutos.
    Um horário um pouco no futuro (até FOLGA_RELOGIO_S) ainda conta como recente;
    muito no futuro indica erro de fuso e não é aceito."""
    t = parse_ts(gps_em)
    if t is None:
        return False
    idade = ((agora or agora_utc()) - t).total_seconds()
    return -FOLGA_RELOGIO_S <= idade <= config.GPS_VALIDADE_MIN * 60


def para_local(dt_utc):
    return dt_utc + timedelta(hours=config.TIMEZONE_OFFSET_HORAS)


def inicio_do_dia_utc(agora=None, dias_atras=0):
    """Meia-noite (no horário do restaurante) de hoje, ou de N dias atrás, em UTC."""
    local = para_local(agora or agora_utc()) - timedelta(days=dias_atras)
    meia_noite = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return meia_noite - timedelta(hours=config.TIMEZONE_OFFSET_HORAS)


def fmt_local(dt_utc):
    """Data e hora no horário do restaurante, como texto (ou "" se não houver)."""
    return para_local(dt_utc).strftime("%d/%m/%Y %H:%M") if dt_utc else ""
