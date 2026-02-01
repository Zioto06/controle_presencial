from flask import Flask, render_template, request, redirect, flash, abort
import os
import sqlite3
from datetime import datetime, date

import libsql  # ✅ novo SDK oficial

APP_DIR = os.path.dirname(os.path.abspath(__file__))

# Arquivos locais
DB_PATH = os.environ.get("DB_PATH", os.path.join(APP_DIR, "attendance.db"))
BOLSISTAS_PATH = os.path.join(APP_DIR, "bolsistas.txt")
IPS_PATH = os.path.join(APP_DIR, "ips.txt")

# Turso (Render)
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")  # ex: libsql://....
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")      # token JWT

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave")

# cache da conexão
_CONN = None


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


# ---------- Conexão DB ----------

def using_turso() -> bool:
    return bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


def get_conn():
    """
    Local: sqlite3.connect(DB_PATH)
    Render/Turso: libsql.connect(local_replica.db, sync_url=..., auth_token=...)
    """
    global _CONN
    if _CONN is not None:
        return _CONN

    if using_turso():
        local_replica_path = os.path.join(APP_DIR, "replica.db")

        # ✅ sanitiza valores (remove espaços/linhas)
        sync_url = (TURSO_DATABASE_URL or "").strip().strip('"').strip("'")
        auth = (TURSO_AUTH_TOKEN or "").strip().strip('"').strip("'")

        # ✅ aceita se o usuário colocou https por engano e converte para libsql
        if sync_url.startswith("https://"):
            sync_url = "libsql://" + sync_url.removeprefix("https://")
        if sync_url.startswith("http://"):
            sync_url = "libsql://" + sync_url.removeprefix("http://")

        # ✅ remove barra final
        sync_url = sync_url.rstrip("/")

        _CONN = libsql.connect(
            local_replica_path,
            sync_url=sync_url,
            auth_token=auth
        )
        _CONN.row_factory = sqlite3.Row

        # sincroniza no startup (baixa estado do remoto)
        _CONN.sync()
    else:
        _CONN = sqlite3.connect(DB_PATH)
        _CONN.row_factory = sqlite3.Row

    return _CONN



def db_commit_and_sync():
    """
    Commit local e, se estiver no Turso, sincroniza com o remoto.
    """
    conn = get_conn()
    conn.commit()
    if using_turso():
        conn.sync()


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS registros (
            cpf TEXT NOT NULL,
            dia TEXT NOT NULL,
            nome TEXT NOT NULL,
            entrada TEXT,
            saida TEXT,
            PRIMARY KEY (cpf, dia)
        )
    """)
    db_commit_and_sync()


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
    xff = request.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else request.remote_addr


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

    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM registros WHERE cpf=? AND dia=?",
        (cpf, hoje)
    ).fetchone()

    if r and r["entrada"]:
        return False, "Entrada já registrada hoje."

    if r is None:
        conn.execute(
            "INSERT INTO registros (cpf, dia, nome, entrada) VALUES (?,?,?,?)",
            (cpf, hoje, nome, agora)
        )
    else:
        conn.execute(
            "UPDATE registros SET entrada=? WHERE cpf=? AND dia=?",
            (agora, cpf, hoje)
        )

    db_commit_and_sync()
    return True, "Entrada registrada com sucesso."


def registrar_saida(cpf):
    hoje = date.today().isoformat()
    agora = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    r = conn.execute(
        "SELECT * FROM registros WHERE cpf=? AND dia=?",
        (cpf, hoje)
    ).fetchone()

    if not r or not r["entrada"]:
        return False, "Não há entrada registrada hoje."

    if r["saida"]:
        return False, "Saída já registrada hoje."

    conn.execute(
        "UPDATE registros SET saida=? WHERE cpf=? AND dia=?",
        (agora, cpf, hoje)
    )
    db_commit_and_sync()
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

    conn = get_conn()
    for r in conn.execute(q, p).fetchall():
        tempo = "—"
        secs = 0

        if r["entrada"] and r["saida"]:
            ent = datetime.fromisoformat(r["entrada"])
            sai = datetime.fromisoformat(r["saida"])
            secs = int((sai - ent).total_seconds())
            tempo = format_hhmm_from_seconds(secs)

        total_seconds += max(secs, 0)

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


@app.errorhandler(403)
def forbidden(_):
    return "<h1>403 - Acesso negado</h1>", 403


# ✅ Inicializa o banco quando o app é importado pelo gunicorn
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

