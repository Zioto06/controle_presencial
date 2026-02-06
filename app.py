from flask import Flask, render_template, request, redirect, url_for, flash, abort
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras


# -------------------- Config --------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave")

DATABASE_URL = os.environ.get("DATABASE_URL")  # Render fornece isso
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL não configurada. Defina nas Environment Variables do Render."
    )

TZ_BR = ZoneInfo("America/Sao_Paulo")
TZ_UTC = ZoneInfo("UTC")


# -------------------- Utilidades --------------------
def only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def get_conn():
    # RealDictCursor: r["campo"] em vez de r[0]
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


def init_db():
    # Cria tabelas caso não existam
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS registros (
                    cpf TEXT NOT NULL,
                    dia DATE NOT NULL,
                    nome TEXT NOT NULL,
                    entrada TIMESTAMP,
                    saida TIMESTAMP,
                    PRIMARY KEY (cpf, dia)
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS bolsistas (
                    cpf TEXT PRIMARY KEY,
                    nome TEXT NOT NULL,
                    pin TEXT NOT NULL
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ips_permitidos (
                    ip TEXT PRIMARY KEY
                );
            """)

            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_registros_dia ON registros (dia);"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_registros_nome ON registros (nome);"
            )

        conn.commit()


def utc_now():
    # Sempre gravar em UTC
    return datetime.now(tz=TZ_UTC)


def utc_to_local(dt_utc):
    """Converte datetime UTC -> horário Brasil para exibição/cálculo."""
    if not dt_utc:
        return None
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=TZ_UTC)
    return dt_utc.astimezone(TZ_BR)


def format_ddmmyyyy(dia_date):
    # dia_date é DATE do Postgres -> datetime.date
    if not dia_date:
        return ""
    return dia_date.strftime("%d-%m-%Y")


def format_hhmm(dt_utc):
    if not dt_utc:
        return "—"
    local = utc_to_local(dt_utc)
    return local.strftime("%H:%M")


def format_hhmm_from_seconds(seconds):
    if not seconds or seconds <= 0:
        return "00:00"
    m = seconds // 60
    return f"{m//60:02d}:{m%60:02d}"


# -------------------- IP restriction --------------------
def load_allowed_ips():
    """Lê ips_permitidos. Se vazio -> libera geral."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ip FROM ips_permitidos")
            rows = cur.fetchall()
    return {r["ip"] for r in rows}


def get_client_ip():
    # Render usa proxy -> X-Forwarded-For normalmente vem com o IP real do cliente
    xff = request.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else request.remote_addr


@app.before_request
def restrict_by_ip():
    allowed = load_allowed_ips()
    if not allowed:
        return  # tabela vazia: libera geral
    if get_client_ip() not in allowed:
        abort(403)


# -------------------- Bolsistas --------------------
def get_bolsista(cpf: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cpf, nome, pin FROM bolsistas WHERE cpf = %s",
                (cpf,)
            )
            return cur.fetchone()


# -------------------- Registro --------------------
def registrar_entrada(cpf, nome):
    hoje = date.today()          # date local do servidor (não impacta muito)
    agora = utc_now()            # timestamp UTC correto

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entrada FROM registros WHERE cpf=%s AND dia=%s",
                (cpf, hoje)
            )
            r = cur.fetchone()

            if r and r.get("entrada"):
                return False, "Entrada já registrada hoje."

            if not r:
                cur.execute(
                    "INSERT INTO registros (cpf, dia, nome, entrada) VALUES (%s,%s,%s,%s)",
                    (cpf, hoje, nome, agora),
                )
            else:
                cur.execute(
                    "UPDATE registros SET entrada=%s, nome=%s WHERE cpf=%s AND dia=%s",
                    (agora, nome, cpf, hoje),
                )

        conn.commit()

    return True, "Entrada registrada com sucesso."


def registrar_saida(cpf):
    hoje = date.today()
    agora = utc_now()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entrada, saida FROM registros WHERE cpf=%s AND dia=%s",
                (cpf, hoje)
            )
            r = cur.fetchone()

            if not r or not r.get("entrada"):
                return False, "Não há entrada registrada hoje."
            if r.get("saida"):
                return False, "Saída já registrada hoje."

            cur.execute(
                "UPDATE registros SET saida=%s WHERE cpf=%s AND dia=%s",
                (agora, cpf, hoje),
            )

        conn.commit()

    return True, "Saída registrada com sucesso."


# -------------------- Rotas --------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/registrar", methods=["POST"])
def registrar():
    cpf = only_digits(request.form.get("cpf"))
    pin = only_digits(request.form.get("pin"))
    action = request.form.get("action")

    if len(cpf) != 11:
        flash("CPF inválido.", "error")
        return redirect(url_for("index"))

    bolsista = get_bolsista(cpf)
    if not bolsista or bolsista["pin"] != pin:
        flash("CPF ou PIN inválido.", "error")
        return redirect(url_for("index"))

    if action == "entrada":
        ok, msg = registrar_entrada(cpf, bolsista["nome"])
    else:
        ok, msg = registrar_saida(cpf)

    flash(msg, "success" if ok else "error")
    return redirect(url_for("index"))


@app.route("/admin")
def admin():
    start = request.args.get("start")  # yyyy-mm-dd
    end = request.args.get("end")      # yyyy-mm-dd

    q = "SELECT cpf, dia, nome, entrada, saida FROM registros"
    p = []
    w = []

    if start:
        w.append("dia >= %s")
        p.append(start)

    if end:
        w.append("dia <= %s")
        p.append(end)

    if w:
        q += " WHERE " + " AND ".join(w)

    q += " ORDER BY dia DESC, nome"

    records = []
    total_seconds = 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, tuple(p))
            rows = cur.fetchall()

    for r in rows:
        tempo = "—"
        secs = 0

        if r.get("entrada") and r.get("saida"):
            ent_local = utc_to_local(r["entrada"])
            sai_local = utc_to_local(r["saida"])
            secs = int((sai_local - ent_local).total_seconds())
            secs = max(secs, 0)
            tempo = format_hhmm_from_seconds(secs)
            total_seconds += secs

        records.append({
            "nome": r["nome"],
            "dia": format_ddmmyyyy(r["dia"]),
            "entrada": format_hhmm(r.get("entrada")),
            "saida": format_hhmm(r.get("saida")),
            "tempo": tempo
        })

    return render_template(
        "admin.html",
        records=records,
        total_tempo=format_hhmm_from_seconds(total_seconds),
        total_registros=len(records),
        start=start,
        end=end,
    )


@app.errorhandler(403)
def forbidden(_):
    return "<h1>403 - Acesso negado</h1>", 403


# -------------------- Entrypoint --------------------
# No Render/Gunicorn, este bloco NÃO roda.
# Mas deixamos init_db() sempre ser chamado ao importar o módulo,
# garantindo que tabelas existam.
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
