from flask import Flask, render_template, request, redirect, url_for, flash, abort
import os
from datetime import datetime, date
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave")


# ---------- Utilidades ----------
def only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _normalize_database_url(url: str) -> str:
    """
    Render fornece DATABASE_URL normalmente. Algumas vezes vem sem sslmode.
    Esta função garante sslmode=require quando estiver em produção no Render.
    """
    if not url:
        return url

    # Render costuma usar postgres:// ; psycopg2 aceita, mas vamos manter.
    parsed = urlparse(url)
    q = parse_qs(parsed.query)

    # Se já tem sslmode, respeita
    if "sslmode" not in q:
        q["sslmode"] = ["require"]

    new_query = urlencode(q, doseq=True)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )


def get_db_url() -> str:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL não configurada. No Render, crie um PostgreSQL e vincule ao serviço."
        )
    return _normalize_database_url(db_url)


def get_conn():
    return psycopg2.connect(get_db_url(), cursor_factory=RealDictCursor)


def init_db():
    """
    Cria as tabelas necessárias no Postgres.
    - registros: presença (cpf + dia chave primária)
    - bolsistas: credencial (cpf + pin)
    - ips_permitidos: lista de IPs liberados (opcional)
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS registros (
        cpf    TEXT NOT NULL,
        dia    DATE NOT NULL,
        nome   TEXT NOT NULL,
        entrada TIMESTAMP,
        saida   TIMESTAMP,
        PRIMARY KEY (cpf, dia)
    );

    CREATE TABLE IF NOT EXISTS bolsistas (
        cpf  TEXT PRIMARY KEY,
        nome TEXT NOT NULL,
        pin  TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ips_permitidos (
        ip TEXT PRIMARY KEY
    );

    CREATE INDEX IF NOT EXISTS idx_registros_dia ON registros (dia);
    CREATE INDEX IF NOT EXISTS idx_registros_nome ON registros (nome);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


def format_ddmmyyyy(dia_date):
    # dia_date pode vir como datetime.date (Postgres)
    if not dia_date:
        return "—"
    return dia_date.strftime("%d-%m-%Y")


def format_hhmm(dt_value):
    # dt_value pode vir como datetime.datetime (Postgres)
    if not dt_value:
        return "—"
    return dt_value.strftime("%H:%M")


def format_hhmm_from_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "00:00"
    m = seconds // 60
    return f"{m//60:02d}:{m%60:02d}"


# ---------- IP restriction ----------
def load_allowed_ips():
    allowed = set()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ip FROM ips_permitidos")
            rows = cur.fetchall()
    for r in rows:
        allowed.add(r["ip"])
    return allowed


def get_client_ip():
    # Importante: o Render usa proxy, então X-Forwarded-For é relevante
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr


@app.before_request
def restrict_by_ip():
    allowed = load_allowed_ips()

    # Se não houver IP cadastrado, libera (mesmo comportamento do seu código
    # quando ips.txt está vazio ou não existe).
    if not allowed:
        return

    if get_client_ip() not in allowed:
        abort(403)


# ---------- Bolsistas ----------
def get_bolsistas_dict():
    """
    Retorna dict {cpf: {"nome":..., "pin":...}}
    """
    bolsistas = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT cpf, nome, pin FROM bolsistas")
            rows = cur.fetchall()

    for r in rows:
        bolsistas[r["cpf"]] = {"nome": r["nome"], "pin": r["pin"]}
    return bolsistas


# ---------- Registro ----------
def registrar_entrada(cpf, nome):
    hoje = date.today()
    agora = datetime.now()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cpf, dia, entrada FROM registros WHERE cpf=%s AND dia=%s",
                (cpf, hoje),
            )
            r = cur.fetchone()

            if r and r.get("entrada"):
                return False, "Entrada já registrada hoje."

            if r is None:
                cur.execute(
                    "INSERT INTO registros (cpf, dia, nome, entrada) VALUES (%s,%s,%s,%s)",
                    (cpf, hoje, nome, agora),
                )
            else:
                cur.execute(
                    "UPDATE registros SET entrada=%s WHERE cpf=%s AND dia=%s",
                    (agora, cpf, hoje),
                )

        conn.commit()

    return True, "Entrada registrada com sucesso."


def registrar_saida(cpf):
    hoje = date.today()
    agora = datetime.now()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cpf, dia, entrada, saida FROM registros WHERE cpf=%s AND dia=%s",
                (cpf, hoje),
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


# ---------- Rotas ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/registrar", methods=["POST"])
def registrar():
    cpf = only_digits(request.form.get("cpf"))
    pin = only_digits(request.form.get("pin"))
    action = request.form.get("action")

    bolsistas = get_bolsistas_dict()

    if cpf not in bolsistas or bolsistas[cpf]["pin"] != pin:
        flash("CPF ou PIN inválido.", "error")
        return redirect("/")

    if action == "entrada":
        ok, msg = registrar_entrada(cpf, bolsistas[cpf]["nome"])
    else:
        ok, msg = registrar_saida(cpf)

    flash(msg, "success" if ok else "error")
    return redirect("/")


@app.route("/admin")
def admin():
    # start/end chegam como yyyy-mm-dd (string)
    start = request.args.get("start")
    end = request.args.get("end")

    where = []
    params = []

    # dia é DATE no Postgres
    if start:
        where.append("dia >= %s")
        params.append(start)
    if end:
        where.append("dia <= %s")
        params.append(end)

    q = """
        SELECT cpf, dia, nome, entrada, saida
        FROM registros
    """
    if where:
        q += " WHERE " + " AND ".join(where)

    q += " ORDER BY dia DESC, nome"

    total_seconds = 0
    records = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, params)
            rows = cur.fetchall()

    for r in rows:
        tempo = "—"
        secs = 0

        ent = r.get("entrada")
        sai = r.get("saida")

        if ent and sai:
            secs = int((sai - ent).total_seconds())
            tempo = format_hhmm_from_seconds(secs)
            total_seconds += max(secs, 0)

        records.append(
            {
                "nome": r["nome"],
                "dia": format_ddmmyyyy(r["dia"]),
                "entrada": format_hhmm(ent),
                "saida": format_hhmm(sai),
                "tempo": tempo,
            }
        )

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


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
