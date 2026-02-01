from flask import Flask, render_template, request, redirect, url_for, flash, abort
import os
from datetime import datetime, date

# Turso/libSQL client
from libsql_client import create_client

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- Arquivos locais ----------
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "attendance.db"))
BOLSISTAS_PATH = os.path.join(APP_DIR, "bolsistas.txt")
IPS_PATH = os.path.join(APP_DIR, "ips.txt")

# ---------- Turso env vars ----------
LIBSQL_URL = os.environ.get("LIBSQL_URL")
LIBSQL_AUTH_TOKEN = os.environ.get("LIBSQL_AUTH_TOKEN")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave")


# ---------- Utilidades ----------

def only_digits(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def format_ddmmyyyy(dia_iso):
    return f"{dia_iso[8:10]}-{dia_iso[5:7]}-{dia_iso[0:4]}"


def format_hhmm(dt_iso):
    return dt_iso[11:16] if dt_iso else "—"


def format_hhmm_from_seconds(seconds):
    if seconds <= 0:
        return "00:00"
    m = seconds // 60
    return f"{m//60:02d}:{m%60:02d}"


# ---------- DB Layer (Turso remoto ou SQLite local) ----------

def using_turso() -> bool:
    return bool(LIBSQL_URL and LIBSQL_AUTH_TOKEN)


def get_turso_client():
    # Cria o client sob demanda
    return create_client(url=LIBSQL_URL, auth_token=LIBSQL_AUTH_TOKEN)


def _sqlite_connect():
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db_execute(sql: str, params=None):
    params = params or []
    if using_turso():
        db = get_turso_client()
        return db.execute(sql, params)
    else:
        # SQLite local (dev)
        with _sqlite_connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur


def db_fetchone(sql: str, params=None):
    params = params or []
    if using_turso():
        db = get_turso_client()
        rows = db.execute(sql, params).rows
        return rows[0] if rows else None
    else:
        with _sqlite_connect() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchone()


def db_fetchall(sql: str, params=None):
    params = params or []
    if using_turso():
        db = get_turso_client()
        return db.execute(sql, params).rows
    else:
        with _sqlite_connect() as conn:
            cur = conn.execute(sql, params)
            return cur.fetchall()


def init_db():
    # Cria tabela no Turso (remoto) ou no SQLite (local)
    if not using_turso():
        # garante pasta do DB local quando rodar no PC
        if os.path.dirname(DB_PATH):
            os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    db_execute("""
        CREATE TABLE IF NOT EXISTS registros (
            cpf TEXT NOT NULL,
            dia TEXT NOT NULL,
            nome TEXT NOT NULL,
            entrada TEXT,
            saida TEXT,
            PRIMARY KEY (cpf, dia)
        )
    """)


# ---------- IP restriction ----------

def load_allowed_ips():
    allowed = set()
    if os.path.exists(IPS_PATH):
        with open(IPS_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    allowed.add(line)
    return allowed


def get_client_ip():
    # Render/Proxies enviam X-Forwarded-For: "ip1, ip2, ..."
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr


@app.before_request
def restrict_by_ip():
    allowed = load_allowed_ips()
    if not allowed:
        return
    if get_client_ip() not in allowed:
        abort(403)


# ---------- Registro ----------

def registrar_entrada(cpf, nome):
    hoje = date.today().isoformat()
    agora = datetime.now().isoformat(timespec="seconds")

    r = db_fetchone(
        "SELECT * FROM registros WHERE cpf=? AND dia=?",
        [cpf, hoje]
    )

    if r and r["entrada"]:
        return False, "Entrada já registrada hoje."

    if r is None:
        db_execute(
            "INSERT INTO registros (cpf, dia, nome, entrada) VALUES (?,?,?,?)",
            [cpf, hoje, nome, agora]
        )
    else:
        db_execute(
            "UPDATE registros SET entrada=? WHERE cpf=? AND dia=?",
            [agora, cpf, hoje]
        )

    return True, "Entrada registrada com sucesso."


def registrar_saida(cpf):
    hoje = date.today().isoformat()
    agora = datetime.now().isoformat(timespec="seconds")

    r = db_fetchone(
        "SELECT * FROM registros WHERE cpf=? AND dia=?",
        [cpf, hoje]
    )

    if not r or not r["entrada"]:
        return False, "Não há entrada registrada hoje."

    if r["saida"]:
        return False, "Saída já registrada hoje."

    db_execute(
        "UPDATE registros SET saida=? WHERE cpf=? AND dia=?",
        [agora, cpf, hoje]
    )

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

    # Carrega bolsistas
    bolsistas = {}
    with open(BOLSISTAS_PATH, encoding="utf-8") as f:
        for l in f:
            l = l.strip()
            if not l or l.startswith("#"):
                continue
            n, c, p = l.split(";")
            bolsistas[c] = {"nome": n, "pin": p}

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
    start = request.args.get("start")
    end = request.args.get("end")

    q = "SELECT * FROM registros"
    p = []
    w = []

    if start:
        w.append("dia >= ?")
        p.append(start)
    if end:
        w.append("dia <= ?")
        p.append(end)
    if w:
        q += " WHERE " + " AND ".join(w)
    q += " ORDER BY dia DESC, nome"

    total_seconds = 0
    records = []

    rows = db_fetchall(q, p)

    for r in rows:
        # r pode ser sqlite3.Row (local) ou dict-like do libsql (remoto)
        entrada = r["entrada"]
        saida = r["saida"]

        tempo = "—"
        secs = 0

        if entrada and saida:
            ent = datetime.fromisoformat(entrada)
            sai = datetime.fromisoformat(saida)
            secs = int((sai - ent).total_seconds())
            tempo = format_hhmm_from_seconds(secs)

        total_seconds += max(secs, 0)

        records.append({
            "nome": r["nome"],
            "dia": format_ddmmyyyy(r["dia"]),
            "entrada": format_hhmm(entrada),
            "saida": format_hhmm(saida),
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


@app.errorhandler(403)
def forbidden(_):
    return "<h1>403 - Acesso negado</h1>", 403


# Inicializa DB tanto em gunicorn quanto em execução direta
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
