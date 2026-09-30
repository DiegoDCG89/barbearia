"""D.Lourenço Barbearia – site público + ERP + agenda + WhatsApp. Execução: gunicorn -w 1 app:app"""
import logging, math, os, re, sqlite3, hmac
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, g, jsonify, render_template, request

log = logging.getLogger("barbearia")
logging.basicConfig(level=logging.INFO)
app = Flask(__name__)

TZ = ZoneInfo("America/Sao_Paulo")
DB = os.getenv("DB_PATH", "barbearia.db")
FMT = "%Y-%m-%d %H:%M"
OPEN_H, CLOSE_H, STEP = 9, 19, 15
RETURN_DAYS = int(os.getenv("DEFAULT_RETURN_DAYS", 21))
SHOP = {"name": "D.Lourenço Barbearia",
        "address": "Av. Mal. Argôlo, 912, Vila Passos, Lorena - SP, 12604-440"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS barbers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT, calendar_id TEXT,
  commission_pct REAL NOT NULL DEFAULT 50);
CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT,
  price REAL NOT NULL, minutes INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL NOT NULL DEFAULT 0,
  stock INTEGER NOT NULL DEFAULT 0, for_sale INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS stock_moves(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
  qty INTEGER NOT NULL, day TEXT NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS appointments(id INTEGER PRIMARY KEY, client_name TEXT NOT NULL, phone TEXT NOT NULL,
  barber_id INTEGER NOT NULL REFERENCES barbers(id), service_id INTEGER NOT NULL REFERENCES services(id),
  start_at TEXT NOT NULL, end_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'agendado',
  gcal_event_id TEXT, reminder_sent INTEGER NOT NULL DEFAULT 0, return_sent INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_appt ON appointments(barber_id, start_at);
CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, phone TEXT NOT NULL, text TEXT NOT NULL, sent_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN('entrada','saida')),
  amount REAL NOT NULL CHECK(amount>0), day TEXT NOT NULL, description TEXT, category TEXT NOT NULL,
  barber_id INTEGER REFERENCES barbers(id));
"""

# ---------- banco ----------
def connect():
    c = sqlite3.connect(DB, isolation_level=None)  # autocommit; transações explícitas onde preciso
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def db():
    if "db" not in g:
        g.db = connect()
    return g.db

@app.teardown_appcontext
def _close(_):
    c = g.pop("db", None)
    if c:
        c.close()

def init_db():
    c = connect()
    c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM barbers").fetchone():
        c.executemany("INSERT INTO barbers(name,role) VALUES(?,?)", [
            ("João Lourenço", "Mestre Barbeiro"), ("Carlos Silva", "Barbeiro Sênior"),
            ("Pedro Mendes", "Especialista em Barba")])
        c.executemany("INSERT INTO services(name,description,price,minutes) VALUES(?,?,?,?)", [
            ("Corte Clássico", "Tesoura ou máquina.", 45, 30), ("Barba Terapia", "Toalha quente e navalha.", 35, 20),
            ("Combo Corte + Barba", "O pacote completo.", 70, 50), ("Pigmentação", "Cobertura de fios brancos.", 25, 15)])
        c.executemany("INSERT INTO products(name,price,stock,for_sale) VALUES(?,?,?,?)", [
            ("Pomada Modeladora Matte", 40, 15, 1), ("Óleo para Barba", 35, 8, 1), ("Shampoo 3 em 1", 30, 20, 1),
            ("Lâmina de barbear (cx)", 0, 10, 0), ("Toalha descartável (pct)", 0, 12, 0)])
    c.close()

# ---------- utilidades ----------
P = lambda s: datetime.strptime(s, FMT)
now = lambda: datetime.now(TZ).replace(tzinfo=None)

def norm_phone(s):
    d = re.sub(r"\D", "", str(s))
    if len(d) in (10, 11):
        d = "55" + d
    return d if len(d) in (12, 13) and d.startswith("55") else None

def body():
    return request.get_json(silent=True) or {}

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        u, p = os.getenv("ADMIN_USER", "admin"), os.getenv("ADMIN_PASS")
        au = request.authorization
        ok = bool(p) and au and hmac.compare_digest(au.username or "", u) and hmac.compare_digest(au.password or "", p)
        return f(*a, **k) if ok else Response("Acesso restrito", 401, {"WWW-Authenticate": 'Basic realm="ERP"'})
    return w

# ---------- integrações ----------
DEMO = lambda: os.getenv("WA_DEMO") == "1"

def auto_send_enabled():
    return bool(DEMO() or os.getenv("WA_GATEWAY_URL") or os.getenv("WA_TOKEN"))

def send_whatsapp(phone, text):
    """1) gateway local não oficial (Baileys) se WA_GATEWAY_URL; 2) Meta Cloud API se WA_TOKEN; senão False (fila manual)."""
    if DEMO():  # apresentação: nada sai para o WhatsApp real; a mensagem vai para a caixa de saída simulada
        c = connect()
        c.execute("INSERT INTO outbox(phone,text,sent_at) VALUES(?,?,?)", (phone, text, now().strftime(FMT)))
        c.close()
        return True
    gw = os.getenv("WA_GATEWAY_URL")
    if gw:
        try:
            r = requests.post(gw.rstrip("/") + "/send", headers={"X-Secret": os.getenv("WA_GATEWAY_SECRET", "")},
                              json={"to": phone, "text": text}, timeout=30)
            if not r.ok:
                log.error("Gateway WhatsApp %s: %s", r.status_code, r.text)
            return r.ok
        except requests.RequestException as e:
            log.error("Gateway WhatsApp indisponível: %s", e)
            return False
    token, pid = os.getenv("WA_TOKEN"), os.getenv("WA_PHONE_ID")
    if not (token and pid):
        return False
    try:
        r = requests.post(f"https://graph.facebook.com/v20.0/{pid}/messages",
                          headers={"Authorization": f"Bearer {token}"}, timeout=10,
                          json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": text}})
        if not r.ok:
            log.error("WhatsApp %s: %s", r.status_code, r.text)
        return r.ok
    except requests.RequestException as e:
        log.error("WhatsApp erro: %s", e)
        return False

def gcal():
    f = os.getenv("GOOGLE_SA_FILE")
    if not f:
        return None
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    cred = service_account.Credentials.from_service_account_file(f, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=cred, cache_discovery=False)

def gcal_create(a, barber, svc):
    try:
        s = gcal()
        if not s or not barber["calendar_id"]:
            return None
        ev = {"summary": f"{svc['name']} – {a['client_name']}", "location": SHOP["address"],
              "description": f"Cliente: {a['client_name']}\nTel: {a['phone']}",
              "start": {"dateTime": P(a["start_at"]).isoformat(), "timeZone": "America/Sao_Paulo"},
              "end": {"dateTime": P(a["end_at"]).isoformat(), "timeZone": "America/Sao_Paulo"}}
        return s.events().insert(calendarId=barber["calendar_id"], body=ev).execute()["id"]
    except Exception:
        log.exception("Falha ao criar evento no Google Agenda")
        return None

def gcal_delete(cal_id, ev_id):
    try:
        s = gcal()
        if s and cal_id and ev_id:
            s.events().delete(calendarId=cal_id, eventId=ev_id).execute()
    except Exception:
        log.exception("Falha ao remover evento")

def client_gcal_link(a, svc):
    fmt = lambda s: P(s).strftime("%Y%m%dT%H%M%S")
    return "https://calendar.google.com/calendar/render?" + urlencode({
        "action": "TEMPLATE", "text": f"{svc['name']} – {SHOP['name']}", "location": SHOP["address"],
        "dates": f"{fmt(a['start_at'])}/{fmt(a['end_at'])}", "ctz": "America/Sao_Paulo"})

# ---------- agenda ----------
def free_slots(c, barber_id, minutes, day):
    d = datetime.strptime(day, "%Y-%m-%d")
    busy = [(P(r["start_at"]), P(r["end_at"])) for r in c.execute(
        "SELECT start_at,end_at FROM appointments WHERE barber_id=? AND status='agendado' AND start_at LIKE ?",
        (barber_id, day + "%"))]
    out, t, close = [], d.replace(hour=OPEN_H), d.replace(hour=CLOSE_H)
    while t + timedelta(minutes=minutes) <= close:
        e = t + timedelta(minutes=minutes)
        if t > now() and all(e <= bs or t >= be for bs, be in busy):
            out.append(t.strftime("%H:%M"))
        t += timedelta(minutes=STEP)
    return out

# ---------- rotas públicas ----------
@app.get("/")
def index():
    return render_template("index.html", shop=SHOP)

@app.get("/api/public")
def public():
    c = db()
    q = lambda sql: [dict(r) for r in c.execute(sql)]
    return jsonify(barbers=q("SELECT id,name,role FROM barbers"), services=q("SELECT * FROM services"),
                   products=q("SELECT id,name,price,stock FROM products WHERE for_sale=1"))

@app.get("/api/slots")
def slots():
    try:
        svc = db().execute("SELECT minutes FROM services WHERE id=?", (int(request.args["service"]),)).fetchone()
        return jsonify(free_slots(db(), int(request.args["barber"]), svc["minutes"], request.args["date"]))
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Parâmetros inválidos"), 400

@app.post("/api/appointments")
def book():
    j, c = body(), db()
    name, phone = str(j.get("name", "")).strip()[:80], norm_phone(j.get("phone", ""))
    try:
        barber = c.execute("SELECT * FROM barbers WHERE id=?", (int(j["barber"]),)).fetchone()
        svc = c.execute("SELECT * FROM services WHERE id=?", (int(j["service"]),)).fetchone()
        start = P(f"{j['date']} {j['time']}")
    except (KeyError, ValueError, TypeError):
        return jsonify(error="Dados inválidos"), 400
    if not (name and phone and barber and svc):
        return jsonify(error="Informe nome, WhatsApp válido, barbeiro e serviço"), 400
    end = start + timedelta(minutes=svc["minutes"])
    c.execute("BEGIN IMMEDIATE")  # evita dupla reserva simultânea
    try:
        if start.strftime("%H:%M") not in free_slots(c, barber["id"], svc["minutes"], start.strftime("%Y-%m-%d")):
            c.execute("ROLLBACK")
            return jsonify(error="Horário indisponível"), 409
        cur = c.execute("INSERT INTO appointments(client_name,phone,barber_id,service_id,start_at,end_at) VALUES(?,?,?,?,?,?)",
                        (name, phone, barber["id"], svc["id"], start.strftime(FMT), end.strftime(FMT)))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    a = {"client_name": name, "phone": phone, "start_at": start.strftime(FMT), "end_at": end.strftime(FMT)}
    ev = gcal_create(a, barber, svc)
    if ev:
        c.execute("UPDATE appointments SET gcal_event_id=? WHERE id=?", (ev, cur.lastrowid))
    return jsonify(id=cur.lastrowid, google_link=client_gcal_link(a, svc)), 201

# ---------- ERP ----------
@app.get("/admin")
@admin_required
def admin():
    return render_template("admin.html", demo=DEMO())

@app.get("/api/admin/appointments")
@admin_required
def adm_appts():
    day = request.args.get("day", now().strftime("%Y-%m-%d"))
    rows = db().execute("""SELECT a.id,a.client_name,a.phone,a.start_at,a.status,b.name barber,s.name service,s.price
        FROM appointments a JOIN barbers b ON b.id=a.barber_id JOIN services s ON s.id=a.service_id
        WHERE a.start_at LIKE ? ORDER BY a.start_at""", (day + "%",))
    return jsonify([dict(r) for r in rows])

@app.post("/api/admin/appointments/<int:aid>/status")
@admin_required
def adm_status(aid):
    st, c = body().get("status"), db()
    a = c.execute("""SELECT a.*,s.name sname,s.price,b.calendar_id FROM appointments a
        JOIN services s ON s.id=a.service_id JOIN barbers b ON b.id=a.barber_id WHERE a.id=?""", (aid,)).fetchone()
    if not a or a["status"] != "agendado" or st not in ("concluido", "cancelado", "faltou"):
        return jsonify(error="Operação inválida"), 400
    c.execute("UPDATE appointments SET status=? WHERE id=?", (st, aid))
    if st == "concluido":
        c.execute("INSERT INTO transactions(kind,amount,day,description,category,barber_id) VALUES('entrada',?,?,?,'Serviço',?)",
                  (a["price"], a["start_at"][:10], f"{a['sname']} – {a['client_name']}", a["barber_id"]))
    else:
        gcal_delete(a["calendar_id"], a["gcal_event_id"])
    return jsonify(ok=True)

@app.get("/api/admin/summary")
@admin_required
def summary():
    m = request.args.get("month", now().strftime("%Y-%m"))
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        return jsonify(error="Mês inválido"), 400
    c, like = db(), m + "%"
    tot = lambda k: c.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE kind=? AND day LIKE ?", (k, like)).fetchone()[0]
    barbers = []
    for r in c.execute("""SELECT b.name,b.commission_pct pct,COALESCE(SUM(t.amount),0) gross,COUNT(t.id) n FROM barbers b
        LEFT JOIN transactions t ON t.barber_id=b.id AND t.kind='entrada' AND t.day LIKE ? GROUP BY b.id""", (like,)):
        barbers.append({**dict(r), "commission": round(r["gross"] * r["pct"] / 100, 2)})
    cats = [dict(r) for r in c.execute("""SELECT kind,category,SUM(amount) total FROM transactions WHERE day LIKE ?
        GROUP BY kind,category ORDER BY total DESC""", (like,))]
    tx = [dict(r) for r in c.execute("SELECT * FROM transactions WHERE day LIKE ? ORDER BY day DESC,id DESC", (like,))]
    return jsonify(income=tot("entrada"), expense=tot("saida"), balance=tot("entrada") - tot("saida"),
                   barbers=barbers, categories=cats, transactions=tx)

@app.post("/api/admin/transactions")
@admin_required
def add_tx():
    j = body()
    try:
        amount, day = round(float(j["amount"]), 2), datetime.strptime(j["day"], "%Y-%m-%d").strftime("%Y-%m-%d")
        assert amount > 0 and j["kind"] in ("entrada", "saida")
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Dados inválidos"), 400
    db().execute("INSERT INTO transactions(kind,amount,day,description,category) VALUES(?,?,?,?,?)",
                 (j["kind"], amount, day, str(j.get("description", ""))[:120], str(j.get("category", "Gasto"))[:40]))
    return jsonify(ok=True), 201

@app.get("/api/admin/products")
@admin_required
def adm_products():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM products ORDER BY for_sale DESC,name")])

@app.post("/api/admin/sales")
@admin_required
def sale():
    j, c = body(), db()
    try:
        qty, p = int(j["qty"]), c.execute("SELECT * FROM products WHERE id=? AND for_sale=1", (int(j["product"]),)).fetchone()
        assert qty > 0 and p and p["stock"] >= qty
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Venda inválida ou estoque insuficiente"), 400
    day = now().strftime("%Y-%m-%d")
    c.execute("BEGIN")
    c.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, p["id"]))
    c.execute("INSERT INTO stock_moves(product_id,qty,day,reason) VALUES(?,?,?,'venda')", (p["id"], -qty, day))
    c.execute("INSERT INTO transactions(kind,amount,day,description,category) VALUES('entrada',?,?,?,'Produto')",
              (p["price"] * qty, day, f"{qty}x {p['name']}"))
    c.execute("COMMIT")
    return jsonify(ok=True)

@app.post("/api/admin/stock")
@admin_required
def stock_move():
    """qty>0: compra/entrada (cost opcional vira despesa). qty<0: consumo de material."""
    j, c = body(), db()
    try:
        qty, cost = int(j["qty"]), float(j.get("cost") or 0)
        p = c.execute("SELECT * FROM products WHERE id=?", (int(j["product"]),)).fetchone()
        assert qty != 0 and p and p["stock"] + qty >= 0 and cost >= 0
    except (KeyError, ValueError, TypeError, AssertionError):
        return jsonify(error="Movimentação inválida"), 400
    day = now().strftime("%Y-%m-%d")
    c.execute("BEGIN")
    c.execute("UPDATE products SET stock=stock+? WHERE id=?", (qty, p["id"]))
    c.execute("INSERT INTO stock_moves(product_id,qty,day,reason) VALUES(?,?,?,?)", (p["id"], qty, day, "compra" if qty > 0 else "consumo"))
    if qty > 0 and cost > 0:
        c.execute("INSERT INTO transactions(kind,amount,day,description,category) VALUES('saida',?,?,?,'Compra de material')",
                  (cost, day, f"{qty}x {p['name']}"))
    c.execute("COMMIT")
    return jsonify(ok=True)

# ---------- previsão de compra (média móvel ponderada) ----------
def forecast_purchases(c, horizon_days=30, weeks=8, safety=1.15, decay=0.85):
    """Consumo semanal dos últimos `weeks`, com peso maior nas semanas recentes -> taxa diária -> necessidade no horizonte."""
    today, out = now().date(), []
    for p in c.execute("SELECT * FROM products"):
        buckets = [0.0] * weeks
        for m in c.execute("SELECT day,-qty q FROM stock_moves WHERE product_id=? AND qty<0 AND day>=?",
                           (p["id"], (today - timedelta(weeks=weeks)).isoformat())):
            age = (today - datetime.strptime(m["day"], "%Y-%m-%d").date()).days // 7
            if 0 <= age < weeks:
                buckets[age] += m["q"]
        w = [decay ** i for i in range(weeks)]
        daily = sum(b * wi for b, wi in zip(buckets, w)) / sum(w) / 7
        need = daily * horizon_days * safety
        out.append({"product": p["name"], "stock": p["stock"], "daily_rate": round(daily, 2),
                    "days_left": round(p["stock"] / daily) if daily else None,
                    "buy": max(0, math.ceil(need - p["stock"]))})
    return sorted(out, key=lambda r: -r["buy"])

@app.get("/api/admin/forecast")
@admin_required
def forecast():
    return jsonify(forecast_purchases(db(), int(request.args.get("days", 30))))

# ---------- lembretes automáticos ----------
def avg_interval_days(visits):
    gaps = [(b - a).days for a, b in zip(visits, visits[1:]) if (b - a).days > 0]
    return min(max(sum(gaps) / len(gaps), 7), 90) if gaps else RETURN_DAYS

def pending_reminders(c):
    """Lembretes devidos agora: 1h antes do corte e retorno (último corte + média do cliente + 2 dias)."""
    n, out = now(), []
    for a in c.execute("""SELECT a.id,a.phone,a.client_name,a.start_at,b.name bname,s.name sname FROM appointments a
        JOIN barbers b ON b.id=a.barber_id JOIN services s ON s.id=a.service_id
        WHERE a.status='agendado' AND a.reminder_sent=0 AND a.start_at>? AND a.start_at<=?""",
                      (n.strftime(FMT), (n + timedelta(hours=1)).strftime(FMT))).fetchall():
        out.append({"id": a["id"], "kind": "lembrete", "phone": a["phone"], "name": a["client_name"], "when": a["start_at"],
                    "text": f"Olá, {a['client_name']}! Lembrete: seu horário ({a['sname']} com {a['bname']}) é hoje às {a['start_at'][11:]}. {SHOP['name']} – {SHOP['address']}"})
    by_phone = {}
    for r in c.execute("SELECT id,phone,client_name,start_at,return_sent FROM appointments WHERE status='concluido' ORDER BY start_at"):
        by_phone.setdefault(r["phone"], []).append(r)
    for phone, rows in by_phone.items():
        last = rows[-1]
        due = P(last["start_at"]) + timedelta(days=avg_interval_days([P(r["start_at"]) for r in rows]) + 2)
        future = c.execute("SELECT 1 FROM appointments WHERE phone=? AND status='agendado'", (phone,)).fetchone()
        if not last["return_sent"] and not future and n >= due:
            out.append({"id": last["id"], "kind": "retorno", "phone": phone, "name": last["client_name"], "when": due.strftime(FMT),
                        "text": f"Olá, {last['client_name']}! Já faz um tempo desde o seu último corte. Que tal agendar o próximo na {SHOP['name']}?"})
    return out

def mark_done(c, kind, rid):
    col = {"lembrete": "reminder_sent", "retorno": "return_sent"}[kind]
    c.execute(f"UPDATE appointments SET {col}=1 WHERE id=?", (rid,))

def run_reminders():
    """Modo automático (com WA_GATEWAY_URL ou WA_TOKEN; se o envio falhar, o item continua na fila manual). Sem token, os lembretes ficam na fila manual do /admin (links wa.me, custo zero)."""
    if not auto_send_enabled():
        return
    c = connect()
    for r in pending_reminders(c):
        if send_whatsapp(r["phone"], r["text"]):
            mark_done(c, r["kind"], r["id"])
    c.close()

@app.get("/api/admin/reminders")
@admin_required
def adm_reminders():
    return jsonify([{**r, "link": f"https://wa.me/{r['phone']}?text={quote(r['text'])}"} for r in pending_reminders(db())])

@app.post("/api/admin/reminders/done")
@admin_required
def adm_reminder_done():
    j = body()
    if j.get("kind") not in ("lembrete", "retorno") or not isinstance(j.get("id"), int):
        return jsonify(error="Dados inválidos"), 400
    mark_done(db(), j["kind"], j["id"])
    return jsonify(ok=True)

def demo_only(f):
    @wraps(f)
    def w(*a, **k):
        return f(*a, **k) if DEMO() else (jsonify(error="Modo demo desativado"), 404)
    return w

@app.get("/api/admin/outbox")
@admin_required
@demo_only
def outbox():
    return jsonify([dict(r) for r in db().execute("SELECT * FROM outbox ORDER BY id")])

@app.post("/api/admin/demo/seed")
@admin_required
@demo_only
def demo_seed():
    """Cria: um agendamento daqui a 30 min (dispara o lembrete de 1h) e um cliente cujo último corte foi há 25 dias (dispara o retorno)."""
    c, n = db(), now()
    c.execute("DELETE FROM appointments WHERE client_name LIKE '%(demo)'")
    c.execute("DELETE FROM outbox")
    ins = "INSERT INTO appointments(client_name,phone,barber_id,service_id,start_at,end_at,status) VALUES(?,?,1,1,?,?,?)"
    t = n + timedelta(minutes=30)
    c.execute(ins, ("Ana (demo)", "5512988880000", t.strftime(FMT), (t + timedelta(minutes=30)).strftime(FMT), "agendado"))
    o = n - timedelta(days=25)
    c.execute(ins, ("Bruno (demo)", "5512977770000", o.strftime(FMT), (o + timedelta(minutes=30)).strftime(FMT), "concluido"))
    return jsonify(ok=True)

@app.post("/api/admin/demo/run")
@admin_required
@demo_only
def demo_run():
    run_reminders()
    return jsonify(ok=True)

init_db()
if os.getenv("RUN_SCHEDULER", "1") == "1":
    sched = BackgroundScheduler(timezone=TZ)
    sched.add_job(run_reminders, "interval", minutes=1, max_instances=1, coalesce=True)
    sched.start()

if __name__ == "__main__":
    app.run(debug=False)
