from flask import Flask, render_template, request, redirect, url_for, flash, abort
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras


# -------------------- Config --------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não configurada.")

TZ_BR = ZoneInfo("America/Sao_Paulo")
TZ_UTC = ZoneInfo("UTC")


# -------------------- Utilidades --------------------
def only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def get_conn():
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


def utc_now():
    return datetime.now(tz=TZ_UTC)


def utc_to_local(dt_utc):
    if not dt_utc:
        return None
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=TZ_UTC)
    return dt_utc.astimezone(TZ_BR)


def format_ddmmyyyy(dia):
    return dia.strftime("%d-%m-%Y") if dia else ""


def format_hhmm(dt_utc):
    if not dt_utc:
        return "—"
    return utc_to_local(dt_utc).strftime("%H:%M")


def format_hhmm_from_seconds(seconds):
    if not seconds or seconds <= 0:
        return "00:00"
    m = seconds // 60
    return f"{m//60:02d}:{m%60:02d}"


# -------------------- Banco / Init --------------------
def init_db():
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

            cur.execute("""
                CREATE TABLE IF NOT EXISTS acessos_negados (
                    id SERIAL PRIMARY KEY,
                    ip TEXT NOT NULL,
                    rota TEXT NOT NULL,
                    user_agent TEXT,
                    data TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_registros_dia ON registros (dia);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_registros_nome ON registros (nome);")

        conn.commit()


# -------------------- IP --------------------
def get_client_ip():
    xff = request.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else request.remote_addr


def load_allowed_ips():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ip FROM ips_permitidos")
            rows = cur.fetchall()
    return {r["ip"] for r in rows}


def registrar_ip_negado(ip, rota):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO acessos_negados (ip, rota, user_agent)
                    VALUES (%s, %s, %s)
                """, (ip, rota, request.headers.get("User-Agent")))
            conn.commit()
    except Exception as e:
        print(f"[ERRO LOG IP] {e}")


@app.before_request
def restrict_by_ip():
    if request.path == "/health":
        return

    client_ip = get_client_ip()
    print(f"[ACESSO] IP tentando acessar: {client_ip} | Rota: {request.path}")

    allowed = load_allowed_ips()

    if not allowed:
        print("[ACESSO] ips_permitidos vazio → acesso liberado")
        return

    if client_ip not in allowed:
        print(f"[BLOQUEADO] IP negado: {client_ip} | Rota: {request.path}")
        registrar_ip_negado(client_ip, request.path)
        abort(403)

    print(f"[PERMITIDO] IP liberado: {client_ip} | Rota: {request.path}")


# -------------------- Bolsistas --------------------
def get_bolsista(cpf):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cpf, nome, pin FROM bolsistas WHERE cpf=%s",
                (cpf,)
            )
            return cur.fetchone()


# -------------------- Registro --------------------
def registrar_entrada(cpf, nome):
    hoje = date.today()
    agora = utc_now()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entrada FROM registros WHERE cpf=%s AND dia=%s",
                (cpf, hoje)
            )
            r = cur.fetchone()

            if r and r["entrada"]:
                return False, "Entrada já registrada hoje."

            if not r:
                cur.execute("""
                    INSERT INTO registros (cpf, dia, nome, entrada)
                    VALUES (%s,%s,%s,%s)
                """, (cpf, hoje, nome, agora))
            else:
                cur.execute("""
                    UPDATE registros
                    SET entrada=%s, nome=%s
                    WHERE cpf=%s AND dia=%s
                """, (agora, nome, cpf, hoje))

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

            if not r or not r["entrada"]:
                return False, "Não há entrada registrada hoje."
            if r["saida"]:
                return False, "Saída já registrada hoje."

            cur.execute("""
                UPDATE registros
                SET saida=%s
                WHERE cpf=%s AND dia=%s
            """, (agora, cpf, hoje))

        conn.commit()

    return True, "Saída registrada com sucesso."


# -------------------- Rotas --------------------
@app.route("/health")
def health():
    return "ok", 200


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
    start = request.args.get("start")
    end = request.args.get("end")

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

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(q, tuple(p))
            rows = cur.fetchall()

    records = []
    total_seconds = 0

    for r in rows:
        tempo = "—"
        secs = 0

        if r["entrada"] and r["saida"]:
            ent = utc_to_local(r["entrada"])
            sai = utc_to_local(r["saida"])
            secs = max(int((sai - ent).total_seconds()), 0)
            tempo = format_hhmm_from_seconds(secs)
            total_seconds += secs

        records.append({
            "nome": r["nome"],
            "dia": format_ddmmyyyy(r["dia"]),
            "entrada": format_hhmm(r["entrada"]),
            "saida": format_hhmm(r["saida"]),
            "tempo": tempo
        })

    return render_template(
        "admin.html",
        records=records,
        total_tempo=format_hhmm_from_seconds(total_seconds),
        total_registros=len(records),
        start=start,
        end=end
    )


# -------------------- Erros --------------------
@app.errorhandler(403)
def forbidden(_):
    return "<h1>403 - Acesso negado</h1>", 403


# -------------------- Init --------------------
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
