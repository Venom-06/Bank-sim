"""Bank Simulator server: Flask + SQLite.
Serves the game and provides the shared document store used by the
bond / share / CTC order books, fills, holdings and the bank list."""
import contextlib, json, os, re, secrets, sqlite3, threading, time
from flask import Flask, jsonify, request, send_from_directory, session
import economy
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
DB = sqlite3.connect(os.environ.get("DB_PATH", "banksim.db"), check_same_thread=False, isolation_level=None, timeout=30)
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("CREATE TABLE IF NOT EXISTS docs(col TEXT, id TEXT, data TEXT, PRIMARY KEY(col, id))")
DB.execute("CREATE TABLE IF NOT EXISTS players(username TEXT PRIMARY KEY, pw TEXT, state TEXT)")
DB.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
QUARTER_MS = int(os.environ.get("QUARTER_SECONDS", 3600)) * 1000  # 1 hour = 1 quarter


def meta(k):
    r = DB.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def init_clock():
    """(Re)start the universal clock when it is first created or the quarter length changes.
    Existing banks are aligned to the highest existing quarter so everyone shows the same quarter."""
    if meta("quarter_ms") == str(QUARTER_MS) and meta("epoch"):
        return
    rows = [(u, json.loads(s)) for u, s in DB.execute("SELECT username, state FROM players").fetchall()]
    base = max([st.get("quarter", 0) for _, st in rows] or [0])
    DB.execute("BEGIN IMMEDIATE")
    for k, v in (("epoch", int(time.time() * 1000) // QUARTER_MS * QUARTER_MS), ("quarter_ms", QUARTER_MS), ("qbase", base)):
        DB.execute("REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    for u, st in rows:
        st.update(quarter=base, lastGlobalQ=0, _v=st.get("_v", 0) + 1)
        DB.execute("UPDATE players SET state=? WHERE username=?", (json.dumps(st), u))
    DB.execute("COMMIT")


init_clock()
lock = threading.Lock()


@contextlib.contextmanager
def tx():
    """Serialised write transaction: safe across threads AND across worker processes."""
    with lock:
        DB.execute("BEGIN IMMEDIATE")
        try:
            yield
            DB.execute("COMMIT")
        except BaseException:
            DB.execute("ROLLBACK")
            raise


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.before_request
def guard():
    if request.path.startswith(("/api/query", "/api/add", "/api/doc", "/api/state")) and not session.get("user"):
        return jsonify(error="login required"), 401


@app.get("/api/clock")
def clock():
    """Universal quarter clock: same for every bank."""
    return jsonify(epoch_ms=int(meta("epoch")), now_ms=int(time.time() * 1000), quarter_ms=QUARTER_MS, base=int(meta("qbase")))


@app.get("/api/me")
def me():
    return jsonify(id=session.get("user"))


@app.post("/api/register")
def register():
    b = request.get_json()
    u = (b.get("username") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,24}", u) or not b.get("password"):
        return jsonify(error="Username: 3-24 letters, numbers, _ or -. Password required."), 400
    with tx():
        if DB.execute("SELECT 1 FROM players WHERE username=?", (u,)).fetchone():
            return jsonify(error="Username already exists"), 409
        DB.execute("INSERT INTO players VALUES(?,?,?)", (u, generate_password_hash(b["password"]), json.dumps(b["state"])))
    session["user"] = u
    return jsonify(ok=True)


@app.post("/api/login")
def login():
    b = request.get_json()
    with lock:
        row = DB.execute("SELECT pw, state FROM players WHERE username=?", (b.get("username", ""),)).fetchone()
    if not row or not check_password_hash(row[0], b.get("password", "")):
        return jsonify(error="Invalid username or password"), 401
    session["user"] = b["username"]
    return jsonify(ok=True, state=json.loads(row[1]))


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/state")
def get_state():
    with lock:
        row = DB.execute("SELECT state FROM players WHERE username=?", (session["user"],)).fetchone()
    return jsonify(state=json.loads(row[0]))


@app.put("/api/state")
def save_state():
    body = request.get_json()
    with tx():
        cur = json.loads(DB.execute("SELECT state FROM players WHERE username=?", (session["user"],)).fetchone()[0])
        if body.get("_v", 0) != cur.get("_v", 0):  # the server advanced a quarter since the browser last loaded
            return jsonify(conflict=True, state=cur), 409
        body["_v"] = cur.get("_v", 0) + 1
        DB.execute("UPDATE players SET state=? WHERE username=?", (json.dumps(body), session["user"]))
    return jsonify(ok=True, _v=body["_v"])


@app.post("/api/query")
def query():
    q = request.get_json()
    with lock:
        rows = [(r[0], json.loads(r[1])) for r in DB.execute("SELECT id, data FROM docs WHERE col=?", (q["col"],))]
    for field, _op, value in q.get("where", []):  # only "==" is used by the game
        rows = [(i, d) for i, d in rows if d.get(field) == value]
    if q.get("orderBy"):
        field, direction = q["orderBy"]
        rows.sort(key=lambda r: r[1].get(field, 0), reverse=(direction == "desc"))
    return jsonify([{"id": i, "data": d} for i, d in rows[: q.get("limit", 100)]])


@app.post("/api/add/<col>")
def add(col):
    doc_id = secrets.token_hex(8)
    with tx():
        DB.execute("INSERT INTO docs VALUES(?,?,?)", (col, doc_id, json.dumps(request.get_json())))
    return jsonify(id=doc_id)


@app.route("/api/doc/<col>/<path:doc_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
def doc(col, doc_id):
    with tx():
        cur = DB.execute("SELECT data FROM docs WHERE col=? AND id=?", (col, doc_id)).fetchone()
        if request.method == "GET":
            return jsonify(exists=bool(cur), data=json.loads(cur[0]) if cur else None)
        if request.method == "DELETE":
            DB.execute("DELETE FROM docs WHERE col=? AND id=?", (col, doc_id))
        else:
            body = request.get_json()
            if request.method == "PATCH":
                body = {**(json.loads(cur[0]) if cur else {}), **body}
            DB.execute("REPLACE INTO docs VALUES(?,?,?)", (col, doc_id, json.dumps(body)))
    return jsonify(ok=True)


def current_quarter():
    return int((time.time() * 1000 - int(meta("epoch"))) // QUARTER_MS)


def run_due_quarters():
    """Advance every bank to the current universal quarter (1 hour = 1 quarter), online or not."""
    g, base = current_quarter(), int(meta("qbase"))
    with tx():
        for username, raw in DB.execute("SELECT username, state FROM players").fetchall():
            st = json.loads(raw)
            last = st.get("lastGlobalQ")
            if last is not None and g > last:
                for _ in range(min(g - last, 48)):
                    economy.advance_quarter(st)
            elif last is not None:
                continue
            st["quarter"] = base + g  # always show the universal quarter number
            st["lastGlobalQ"] = g
            st["_v"] = st.get("_v", 0) + 1
            DB.execute("UPDATE players SET state=? WHERE username=?", (json.dumps(st), username))
            DB.execute("REPLACE INTO docs VALUES('banks', ?, ?)", (username, json.dumps(economy.public_doc(st))))


def scheduler():
    while True:
        try:
            run_due_quarters()
        except Exception as e:  # keep the loop alive
            print("quarter scheduler error:", e)
        time.sleep(5)


# Start the quarter scheduler on import so it also runs under gunicorn (not only `python app.py`).
threading.Thread(target=scheduler, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
