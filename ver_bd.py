import sqlite3

conn = sqlite3.connect("attendance.db")
cur = conn.cursor()

cur.execute("SELECT cpf, nome, dia, entrada, saida FROM registros ORDER BY dia DESC, nome")
rows = cur.fetchall()

for r in rows:
    print(r)

conn.close()