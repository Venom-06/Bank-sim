
from flask import Flask, render_template, request, jsonify, session, redirect
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, json, os, time, threading, math, random, calendar
from functools import wraps

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-in-production")
# Keep the Flask login session available to same-origin browser requests.
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
DB = os.environ.get("BANKSIM_DB", "banksim.db")
TURN_SECONDS = 10
TAX_RATE = 0.25
lock = threading.RLock()
game_started_at = time.time()
# Each quarter is 1 hour. Quarter boundaries are aligned to UTC/GMT
# half-hour marks, so the schedule is 12:30, 13:30, 14:30, ... UTC.
# Example: at 12:24 the next quarter is at 12:30; at 12:45 it is 13:30.
_now = time.time()
HALF_HOUR_ANCHOR = 30 * 60
next_tick_at = (math.floor((_now - HALF_HOUR_ANCHOR) / TURN_SECONDS) + 1) * TURN_SECONDS + HALF_HOUR_ANCHOR
game_loop_started = False

def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            bank_name TEXT NOT NULL,
            reserve REAL NOT NULL DEFAULT 2000000,
            shares REAL NOT NULL DEFAULT 3000000,
            share_price REAL NOT NULL DEFAULT 10,
            savings_rate REAL NOT NULL DEFAULT 2.4,
            fd_rate REAL NOT NULL DEFAULT 7,
            fd_term INTEGER NOT NULL DEFAULT 4,
            loan_rate REAL NOT NULL DEFAULT 10,
            loan_term INTEGER NOT NULL DEFAULT 13,
            central_rate REAL NOT NULL DEFAULT 4.1,
            inflation REAL NOT NULL DEFAULT 4.5,
            term INTEGER NOT NULL DEFAULT 0,
            branches TEXT NOT NULL DEFAULT '[]',
            savings_balance REAL NOT NULL DEFAULT 0,
            fd_balance REAL NOT NULL DEFAULT 0,
            loan_limit REAL,
            ctc_holdings REAL NOT NULL DEFAULT 0,
            ctc_price REAL NOT NULL DEFAULT 1.0,
            currency REAL NOT NULL DEFAULT 50000,
            dividend_ps REAL NOT NULL DEFAULT 0,
            last_pl TEXT NOT NULL DEFAULT '{}',
            history TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username);

        CREATE TABLE IF NOT EXISTS share_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_id INTEGER NOT NULL,
            bank_id INTEGER NOT NULL,
            shares REAL NOT NULL DEFAULT 0,
            avg_price REAL NOT NULL DEFAULT 0,
            UNIQUE(investor_id, bank_id)
        );

        CREATE TABLE IF NOT EXISTS savings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_id INTEGER NOT NULL,
            bank_id INTEGER NOT NULL,
            balance REAL NOT NULL,
            rate REAL NOT NULL,
            UNIQUE(investor_id, bank_id)
        );

        CREATE TABLE IF NOT EXISTS fixed_deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_id INTEGER NOT NULL,
            bank_id INTEGER NOT NULL,
            principal REAL NOT NULL,
            rate REAL NOT NULL,
            term_left INTEGER NOT NULL,
            created_term INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lender_id INTEGER NOT NULL,
            borrower_id INTEGER NOT NULL,
            principal REAL NOT NULL,
            rate REAL NOT NULL DEFAULT 10,
            term_left INTEGER NOT NULL DEFAULT 13,
            created_term INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            qty REAL DEFAULT 0,
            price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            note TEXT,
            term INTEGER NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS currency_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BID','ASK')), price REAL NOT NULL,
            qty REAL NOT NULL, remaining REAL NOT NULL, locked REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN'
        );
        CREATE INDEX IF NOT EXISTS idx_currency_orders_book ON currency_orders(side,status,price,created_at);
        """)
        # Global monetary scale migration. All currency/valuation values are
        # reduced by 10x once; rates, percentages, share counts and quarter
        # counts are unchanged. This keeps existing games compatible with the
        # smaller economy while new accounts start at the same scale.
        con.execute("CREATE TABLE IF NOT EXISTS game_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        # Currency: new players start with 50K INR.
        cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
        if "currency" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN currency REAL NOT NULL DEFAULT 50000")
        if "loan_rate" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN loan_rate REAL NOT NULL DEFAULT 10")
        if "loan_term" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN loan_term INTEGER NOT NULL DEFAULT 13")
        con.execute("UPDATE users SET currency=50000 WHERE currency IS NULL")
        con.execute("UPDATE users SET loan_rate=10 WHERE loan_rate IS NULL")
        con.execute("UPDATE users SET loan_term=13 WHERE loan_term IS NULL")
        scaled = con.execute("SELECT value FROM game_meta WHERE key='monetary_scale_v3'").fetchone()
        if not scaled:
            con.execute("UPDATE users SET reserve=reserve/10.0, share_price=share_price/10.0, savings_balance=savings_balance/10.0, fd_balance=fd_balance/10.0, loan_limit=CASE WHEN loan_limit IS NULL THEN NULL ELSE loan_limit/10.0 END, dividend_ps=dividend_ps/10.0")
            con.execute("UPDATE share_holdings SET avg_price=avg_price/10.0")
            con.execute("UPDATE savings SET balance=balance/10.0")
            con.execute("UPDATE fixed_deposits SET principal=principal/10.0")
            con.execute("UPDATE loans SET principal=principal/10.0")
            con.execute("UPDATE transactions SET price=price/10.0, amount=amount/10.0")
            rows = con.execute("SELECT id, branches FROM users").fetchall()
            for row in rows:
                try:
                    branches = json.loads(row["branches"] or "[]")
                except Exception:
                    branches = []
                changed = False
                for b in branches:
                    if "cost" in b:
                        b["cost"] = float(b.get("cost", 0) or 0) / 10.0
                        changed = True
                    if "upgradesCost" in b:
                        b["upgradesCost"] = float(b.get("upgradesCost", 0) or 0) / 10.0
                        changed = True
                if changed:
                    con.execute("UPDATE users SET branches=? WHERE id=?", (json.dumps(branches), row["id"]))
            con.execute("INSERT INTO game_meta(key,value) VALUES('monetary_scale_v3','10x_to_1x')")
            con.commit()

def branch_value(branches):
    # Book value includes both the original branch purchase and upgrades.
    total = 0.0
    for b in branches or []:
        base = float(b.get('cost', 0) or 0)
        upgrades = float(b.get('upgradesCost', 0) or 0)
        total += base + upgrades
    return total

def branches_value(branches):
    return branch_value(branches)

def branch_cost(branch_count):
    return 700_000 * (2 ** branch_count)

def branch_upgrade_cost(branch):
    """Cost of the next branch upgrade.

    A newly bought branch starts at level 1. The first upgrade costs 1M,
    the second 2M, then 4M, then 8M. Level 5 is the maximum.
    """
    level = max(1, int(branch.get("level", 1) or 1))
    if level >= 5:
        return 0.0
    return 100_000 * (2 ** (level - 1))

def branch_upgrade_bonus(level):
    """Income bonus for the branch's current upgrade level.

    Level 1 is the base branch. Each completed upgrade adds another 10
    percentage points: L2 +10%, L3 +20%, L4 +30%, L5 +40%.
    """
    level = max(1, min(5, int(level or 1)))
    return 0.10 * (level - 1)

def branch_income(level):
    # Base branch income is 300K per quarter.
    base_income = 300_000
    return base_income * (1 + branch_upgrade_bonus(level))

def user_dict(row):
    d = dict(row)
    d["branches"] = json.loads(d["branches"] or "[]")
    d["last_pl"] = json.loads(d["last_pl"] or "{}")
    d["history"] = json.loads(d["history"] or "[]")

    # Loans are real balance-sheet items: loans lent are assets and loans
    # Borrowed funds are liabilities. Net worth is the residual value after liabilities.
    with db() as con:
        loans_lent = con.execute(
            "SELECT COALESCE(SUM(principal),0) FROM loans WHERE lender_id=? AND status='active'",
            (d["id"],)
        ).fetchone()[0]
        loans_borrowed = con.execute(
            "SELECT COALESCE(SUM(principal),0) FROM loans WHERE borrower_id=? AND status='active'",
            (d["id"],)
        ).fetchone()[0]

    branch_assets = branches_value(d["branches"])
    deposits = d["savings_balance"] + d["fd_balance"]
    total_assets = d["reserve"] + branch_assets + loans_lent
    liabilities = deposits + loans_borrowed
    net_worth = total_assets - liabilities
    market_cap = d["share_price"] * d["shares"]
    car = net_worth / total_assets * 100 if total_assets > 0 else 0
    crr = d["reserve"] >= deposits * 0.04
    ldr = loans_lent / deposits * 100 if deposits > 0 else 0
    d.update(
        net_worth=net_worth,
        total_assets=total_assets,
        market_cap=market_cap,
        deposits=deposits,
        liabilities=liabilities,
        loans_lent=loans_lent,
        loans_borrowed=loans_borrowed,
        car=car,
        crr=crr,
        ldr=ldr,
    )
    return d

INFLOW_CURRENCY_RATE = 0.015
OUTFLOW_CURRENCY_RATE = 0.01

def add_currency(con, user_id, amount, inflow=True):
    amount = float(amount or 0)
    if amount <= 0:
        return
    rate = INFLOW_CURRENCY_RATE if inflow else OUTFLOW_CURRENCY_RATE
    delta = amount * rate
    if inflow:
        con.execute("UPDATE users SET currency=currency+? WHERE id=?", (delta, user_id))
    else:
        con.execute("UPDATE users SET currency=MAX(0,currency-?) WHERE id=?", (delta, user_id))

def log_tx(con, user_id, kind, qty=0, price=0, amount=0, note=""):
    row = con.execute("SELECT term FROM users WHERE id=?", (user_id,)).fetchone()
    term = row["term"] if row else 0
    inflows = {"LOAN RECEIVED", "SAVINGS RECEIVED", "FD RECEIVED", "LOAN MATURITY", "FD MATURITY"}
    outflows = {"LOAN LENT", "SAVINGS DEPOSIT", "FD OPENED", "LOAN REPAID", "BRANCH BOUGHT", "BRANCH UPGRADED"}
    if kind in inflows:
        add_currency(con, user_id, amount, True)
    elif kind in outflows:
        add_currency(con, user_id, amount, False)
    con.execute("""INSERT INTO transactions
        (user_id,kind,qty,price,amount,note,term,created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (user_id, kind, qty, price, amount, note, term, time.time()))

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify(error="Login required"), 401
        return fn(*args, **kwargs)
    return wrapper

def advance_all_banks():
    """Advance every bank exactly one 10-second quarter.

    All cash movements are recorded first, then each bank's P&L and balance
    sheet are calculated from the resulting balances.  Peer-loan interest is
    treated as income for the lender and interest expense for the borrower.
    Loan principal remains on the lender's asset side and borrower's liability
    side until maturity.
    """
    with lock, db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY id").fetchall()
        if not rows:
            return

        # Snapshot loan interest before changing term_left.
        loan_rows = con.execute(
            "SELECT * FROM loans WHERE status='active'"
        ).fetchall()
        loan_income = {r["id"]: 0.0 for r in rows}
        loan_expense = {r["id"]: 0.0 for r in rows}

        # 1) Customer deposit interest and FD maturities.
        bank_quarter = {}
        for r in rows:
            branches = json.loads(r["branches"] or "[]")
            bank_quarter[r["id"]] = {
                "branches": branches,
                "branch_income": sum(branch_income(int(b.get("level", 1))) for b in branches),
                "savings_expense": 0.0,
                "fd_expense": 0.0,
                "savings_total": 0.0,
                "fd_total": 0.0,
            }

        for r in rows:
            bid = r["id"]
            q = bank_quarter[bid]

            srows = con.execute("SELECT * FROM savings WHERE bank_id=?", (bid,)).fetchall()
            for dep in srows:
                interest = dep["balance"] * (dep["rate"] / 400.0)
                new_bal = dep["balance"] + interest
                q["savings_expense"] += interest
                q["savings_total"] += new_bal
                con.execute("UPDATE savings SET balance=? WHERE id=?", (new_bal, dep["id"]))
                con.execute("UPDATE users SET reserve=reserve-? WHERE id=?", (interest, bid))
                con.execute("UPDATE users SET savings_balance=savings_balance+? WHERE id=?", (interest, bid))
                # Bank pays savings interest: currency outflow.
                add_currency(con, bid, interest, False)
                add_currency(con, dep["investor_id"], interest, True)

            frows = con.execute("SELECT * FROM fixed_deposits WHERE bank_id=?", (bid,)).fetchall()
            for fd in frows:
                interest = fd["principal"] * (fd["rate"] / 400.0)
                q["fd_expense"] += interest
                con.execute("UPDATE users SET reserve=reserve-? WHERE id=?", (interest, bid))
                add_currency(con, bid, interest, False)
                add_currency(con, fd["investor_id"], interest, True)

                left = fd["term_left"] - 1
                if left <= 0:
                    # Principal was a bank liability.  Pay principal + current
                    # quarter's interest to the depositor and remove the FD.
                    payout = fd["principal"] + interest
                    con.execute("UPDATE users SET reserve=reserve-? WHERE id=?", (fd["principal"], bid))
                    con.execute("UPDATE users SET reserve=reserve+? WHERE id=?", (payout, fd["investor_id"]))
                    con.execute("UPDATE users SET fd_balance=MAX(0,fd_balance-?) WHERE id=?", (fd["principal"], bid))
                    con.execute("DELETE FROM fixed_deposits WHERE id=?", (fd["id"],))
                    log_tx(con, fd["investor_id"], "FD MATURITY", 1, fd["rate"], payout, r["bank_name"])
                else:
                    con.execute("UPDATE fixed_deposits SET term_left=? WHERE id=?", (left, fd["id"]))
                    q["fd_total"] += fd["principal"]

        # 2) Peer-loan interest and principal maturity.  Each loan is settled
        # once, outside the bank loop, so multiple banks cannot double-charge it.
        for loan in loan_rows:
            lender_id = loan["lender_id"]
            borrower_id = loan["borrower_id"]
            interest = loan["principal"] * loan["rate"] / 400.0
            loan_income[lender_id] = loan_income.get(lender_id, 0.0) + interest
            loan_expense[borrower_id] = loan_expense.get(borrower_id, 0.0) + interest

            con.execute("UPDATE users SET reserve=reserve-? WHERE id=?", (interest, borrower_id))
            con.execute("UPDATE users SET reserve=reserve+? WHERE id=?", (interest, lender_id))
            add_currency(con, borrower_id, interest, False)
            add_currency(con, lender_id, interest, True)

            left = loan["term_left"] - 1
            if left <= 0:
                con.execute("UPDATE users SET reserve=reserve-? WHERE id=?", (loan["principal"], borrower_id))
                con.execute("UPDATE users SET reserve=reserve+? WHERE id=?", (loan["principal"], lender_id))
                con.execute("UPDATE loans SET term_left=0,status='repaid' WHERE id=?", (loan["id"],))
                log_tx(con, borrower_id, "LOAN REPAID", 1, loan["rate"], loan["principal"], "Peer loan")
                log_tx(con, lender_id, "LOAN MATURITY", 1, loan["rate"], loan["principal"], "Peer loan")
            else:
                con.execute("UPDATE loans SET term_left=? WHERE id=?", (left, loan["id"]))

        # 3) Calculate P&L and close the quarter for each bank.
        for r in rows:
            bid = r["id"]
            q = bank_quarter[bid]
            rr = con.execute("SELECT * FROM users WHERE id=?", (bid,)).fetchone()

            # Keep the existing simulation's operating-cost assumption, but
            # calculate it from actual operating assets after settlements.
            active_lent = con.execute(
                "SELECT COALESCE(SUM(principal),0) FROM loans WHERE lender_id=? AND status='active'",
                (bid,)
            ).fetchone()[0]
            branch_assets = branches_value(q["branches"])
            operating_assets = rr["reserve"] + branch_assets + active_lent
            opex = max(0.0, operating_assets * 0.0015)

            interest_income = loan_income.get(bid, 0.0)
            interest_expense = q["savings_expense"] + q["fd_expense"] + loan_expense.get(bid, 0.0)
            nii = interest_income - interest_expense
            pretax = q["branch_income"] + nii - opex
            tax = max(pretax, 0.0) * TAX_RATE
            net = pretax - tax

            con.execute("UPDATE users SET reserve=reserve+? WHERE id=?", (net, bid))
            if q["branch_income"] > 0:
                add_currency(con, bid, q["branch_income"], True)

            # Re-read after profit posting for correct balance-sheet/share math.
            post = con.execute("SELECT * FROM users WHERE id=?", (bid,)).fetchone()
            active_borrowed = con.execute(
                "SELECT COALESCE(SUM(principal),0) FROM loans WHERE borrower_id=? AND status='active'",
                (bid,)
            ).fetchone()[0]
            total_assets = post["reserve"] + branch_assets + active_lent
            liabilities = post["savings_balance"] + post["fd_balance"] + active_borrowed
            net_worth_post = total_assets - liabilities

            # Share valuation uses net worth internally; no equity metric is exposed in the UI.
            net_worth_return = net / net_worth_post if net_worth_post > 0 else 0
            multiplier = max(5, min(18, 10 + net_worth_return * 20))
            fair = max(0.01, net_worth_post * multiplier / max(1, r["shares"]))
            old_net = json.loads(r["last_pl"] or "{}").get("net", 0)
            surprise = max(-0.05, min(0.05, (net - old_net) / max(1, abs(old_net)) * 0.05))
            price = max(0.01, r["share_price"] * 0.6 + fair * 0.4)
            price *= (1 + surprise)

            # Inflation is now independent of a central-bank/central-rate feature.
            inflation = max(0, min(25, r["inflation"] + random.uniform(-0.2, 0.2)))
            term = r["term"] + 1

            last_pl = {
                "income": {
                    "branches": q["branch_income"],
                    "interest": interest_income,
                    "total": q["branch_income"] + interest_income,
                },
                "expense": {
                    "savings": q["savings_expense"],
                    "fd": q["fd_expense"],
                    "loan_interest": loan_expense.get(bid, 0.0),
                    "total": interest_expense,
                },
                "interest_income": interest_income,
                "interest_expense": interest_expense,
                "nii": nii,
                "opex": opex,
                "pretax": pretax,
                "tax": tax,
                "net": net,
                "eps": net / max(1, r["shares"]),
                "roa": net / max(1, total_assets) * 100,
                "nim": nii / max(1, total_assets) * 100,
            }
            hist = json.loads(r["history"] or "[]")
            hist.append({"term": term, "netWorth": net_worth_post, "sharePrice": price, "net": net})
            hist = hist[-24:]

            con.execute("""UPDATE users SET
                savings_balance=?, fd_balance=?, term=?, inflation=?,
                share_price=?, last_pl=?, history=? WHERE id=?""",
                (q["savings_total"], q["fd_total"], term, inflation,
                 price, json.dumps(last_pl), json.dumps(hist), bid))

        con.commit()

def game_loop():
    # The next quarter occurs on the next UTC/GMT half-hour boundary (one hour apart).
    # Use an absolute wall-clock schedule so the displayed countdown and
    # the actual server tick stay synchronized.
    global next_tick_at
    while True:
        now = time.time()
        wait = next_tick_at - now
        if wait > 0:
            time.sleep(wait)
            continue

        try:
            advance_all_banks()
        except Exception as e:
            app.logger.exception("Game tick failed: %s", e)

        # Advance from the scheduled time rather than from the end of work.
        # If the server was delayed, catch up without running multiple
        # quarters in a single request.
        next_tick_at += TURN_SECONDS
        now = time.time()
        if next_tick_at <= now:
            next_tick_at = now + TURN_SECONDS

@app.route("/")
def index():
    return render_template("index.html", turn_seconds=TURN_SECONDS)

@app.post("/api/register")
def register():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    bank_name = (data.get("bank_name") or "").strip() or f"{username}'s Bank"
    password = data.get("password") or ""
    if len(username) < 3 or len(password) < 4:
        return jsonify(error="Username must be 3+ characters and password 4+ characters."), 400
    with lock, db() as con:
        try:
            # New accounts start with a market cap of 30M (3,000,000 shares x
            # the 10/share default) — set explicitly here so it's correct even
            # against a database file created before this default changed.
            cur = con.execute("""INSERT INTO users
                (username,password_hash,bank_name,shares,share_price,created_at) VALUES (?,?,?,?,?,?)""",
                (username, generate_password_hash(password), bank_name, 3_000_000, 10, time.time()))
            uid = cur.lastrowid
            con.commit()
        except sqlite3.IntegrityError:
            return jsonify(error="Username already exists."), 409
    session["user_id"] = uid
    return jsonify(ok=True)

@app.post("/api/login")
def login():
    data = request.get_json() or {}
    with db() as con:
        r = con.execute("SELECT * FROM users WHERE username=?", ((data.get("username") or "").strip(),)).fetchone()
    if not r or not check_password_hash(r["password_hash"], data.get("password") or ""):
        return jsonify(error="Invalid username or password."), 401
    session["user_id"] = r["id"]
    return jsonify(ok=True)

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/api/state")
@login_required
def state():
    with db() as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not r: return jsonify(error="User not found"), 404
        d = user_dict(r)
        tx = [dict(x) for x in con.execute(
            "SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 30",
            (r["id"],)).fetchall()]
        owed = [dict(x) for x in con.execute(
            """SELECT l.*, u.bank_name AS lender_name FROM loans l JOIN users u ON u.id=l.lender_id
               WHERE l.borrower_id=? AND l.status='active'""", (r["id"],)).fetchall()]
        lent = [dict(x) for x in con.execute(
            """SELECT l.*, u.bank_name AS borrower_name FROM loans l JOIN users u ON u.id=l.borrower_id
               WHERE l.lender_id=? AND l.status='active'""", (r["id"],)).fetchall()]
        d.update(transactions=tx, owed=owed, lent= lent)
        return jsonify(d)

@app.get("/api/market")
@login_required
def market():
    with db() as con:
        rows = con.execute("""SELECT id,bank_name,share_price,shares,term,inflation,
                              reserve,savings_balance,fd_balance,dividend_ps,
                              savings_rate,fd_rate,fd_term,loan_rate,loan_term
                              FROM users ORDER BY (share_price*shares) DESC""").fetchall()
        return jsonify([dict(r, market_cap=r["share_price"]*r["shares"]) for r in rows])

@app.post("/api/rates")
@login_required
def rates():
    data = request.get_json() or {}
    fd = float(data.get("fd", 7))
    sav = float(data.get("savings", 2.4))
    term = int(data.get("fd_term", 4))
    loan_rate = float(data.get("loan_rate", 10))
    loan_term = int(data.get("loan_term", 13))
    if not (0 <= fd <= 100 and 0 <= sav <= 100 and 0 <= loan_rate <= 100 and 1 <= term <= 40 and 1 <= loan_term <= 40):
        return jsonify(error="Invalid rate or term."), 400
    with lock, db() as con:
        con.execute("UPDATE users SET fd_rate=?,savings_rate=?,fd_term=?,loan_rate=?,loan_term=? WHERE id=?",
                    (fd, sav, term, loan_rate, loan_term, session["user_id"]))
        con.commit()
    return jsonify(ok=True)

@app.post("/api/branch/buy")
@login_required
def buy_branch():
    with lock, db() as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        branches = json.loads(r["branches"] or "[]")
        cost = branch_cost(len(branches))
        if r["reserve"] < cost:
            return jsonify(error="Insufficient reserve."), 400
        branches.append({"level": 1, "cost": cost, "upgradesCost": 0})
        con.execute("UPDATE users SET reserve=reserve-?,branches=? WHERE id=?",
                    (cost, json.dumps(branches), r["id"]))
        log_tx(con, r["id"], "BRANCH BOUGHT", 1, 0, cost)
        con.commit()
    return jsonify(ok=True)

@app.post("/api/branch/upgrade")
@login_required
def upgrade_branch():
    data = request.get_json() or {}
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return jsonify(error="Invalid branch."), 400

    with lock, db() as con:
        r = con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not r:
            return jsonify(error="User not found."), 404
        branches = json.loads(r["branches"] or "[]")
        if index < 0 or index >= len(branches):
            return jsonify(error="Branch not found."), 404

        branch = branches[index]
        old_level = max(1, int(branch.get("level", 1) or 1))
        if old_level >= 5:
            return jsonify(error="This branch is already at the maximum Level 5."), 400

        cost = branch_upgrade_cost(branch)
        if r["reserve"] < cost:
            return jsonify(error=f"Insufficient reserve. Upgrade costs {cost:,.2f}."), 400

        branch["level"] = old_level + 1
        branch["upgradesCost"] = float(branch.get("upgradesCost", 0) or 0) + cost
        branches[index] = branch
        con.execute("UPDATE users SET reserve=reserve-?,branches=? WHERE id=?",
                    (cost, json.dumps(branches), r["id"]))
        log_tx(con, r["id"], "BRANCH UPGRADED", 1, old_level + 1, cost, f"Branch {index + 1} → Level {old_level + 1}")
        con.commit()
    return jsonify(ok=True, index=index, level=old_level + 1, cost=cost)

@app.post("/api/loan/limit")
@login_required
def loan_limit():
    amount = float((request.get_json() or {}).get("amount", 0))
    if amount < 0: return jsonify(error="Invalid amount."), 400
    with db() as con:
        con.execute("UPDATE users SET loan_limit=? WHERE id=?", (amount, session["user_id"]))
        con.commit()
    return jsonify(ok=True)

@app.post("/api/loan/request")
@login_required
def loan_request():
    data = request.get_json() or {}
    lender_id = int(data.get("bank_id", 0))
    amount = float(data.get("amount", 0))
    if not math.isfinite(amount) or lender_id == session["user_id"] or amount <= 0:
        return jsonify(error="Invalid loan request."), 400
    with lock, db() as con:
        lender = con.execute("SELECT * FROM users WHERE id=?", (lender_id,)).fetchone()
        borrower = con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if not lender: return jsonify(error="Bank not found."), 404
        lent = con.execute("SELECT COALESCE(SUM(principal),0) x FROM loans WHERE lender_id=? AND status='active'",
                           (lender_id,)).fetchone()["x"]
        limit = lender["loan_limit"] if lender["loan_limit"] is not None else lender["reserve"] * 0.25
        available = max(0, limit - lent)
        amount = min(amount, available, lender["reserve"])
        if amount <= 0: return jsonify(error="Lender has no available loan capacity."), 400
        con.execute("UPDATE users SET reserve=reserve+? WHERE id=?", (amount, borrower["id"]))
        con.execute("UPDATE users SET reserve=reserve-? WHERE id=?", (amount, lender_id))
        con.execute("""INSERT INTO loans
            (lender_id,borrower_id,principal,rate,term_left,created_term)
            VALUES (?,?,?,?,?,?)""", (lender_id, borrower["id"], amount, lender["loan_rate"], lender["loan_term"], borrower["term"]))
        log_tx(con, borrower["id"], "LOAN RECEIVED", 1, lender["loan_rate"], amount, lender["bank_name"])
        log_tx(con, lender_id, "LOAN LENT", 1, lender["loan_rate"], amount, borrower["bank_name"])
        con.commit()
    return jsonify(ok=True, amount=amount)

@app.post("/api/deposit/savings")
@login_required
def deposit_savings():
    data=request.get_json() or {}
    bank_id=int(data.get("bank_id",0)); amount=float(data.get("amount",0))
    if not math.isfinite(amount) or bank_id == session["user_id"] or amount <= 0: return jsonify(error="Invalid deposit."),400
    with lock, db() as con:
        investor=con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        bank=con.execute("SELECT * FROM users WHERE id=?", (bank_id,)).fetchone()
        if not bank: return jsonify(error="Bank not found."),404
        amount=min(amount, investor["reserve"])
        if amount<=0:return jsonify(error="Insufficient reserve."),400
        old=con.execute("SELECT * FROM savings WHERE investor_id=? AND bank_id=?",
                        (investor["id"],bank_id)).fetchone()
        if old:
            con.execute("UPDATE savings SET balance=?,rate=? WHERE id=?",
                        (old["balance"]+amount,bank["savings_rate"],old["id"]))
        else:
            con.execute("INSERT INTO savings(investor_id,bank_id,balance,rate) VALUES(?,?,?,?)",
                        (investor["id"],bank_id,amount,bank["savings_rate"]))
        con.execute("UPDATE users SET reserve=reserve-? WHERE id=?", (amount,investor["id"]))
        con.execute("UPDATE users SET reserve=reserve+?,savings_balance=savings_balance+? WHERE id=?",
                    (amount,amount,bank_id))
        log_tx(con, investor["id"], "SAVINGS DEPOSIT", 1, bank["savings_rate"], amount, bank["bank_name"])
        log_tx(con, bank_id, "SAVINGS RECEIVED", 1, bank["savings_rate"], amount, investor["bank_name"])
        con.commit()
    return jsonify(ok=True,amount=amount)

@app.post("/api/deposit/fd")
@login_required
def deposit_fd():
    data=request.get_json() or {}
    bank_id=int(data.get("bank_id",0)); amount=float(data.get("amount",0))
    if not math.isfinite(amount) or bank_id == session["user_id"] or amount <= 0:return jsonify(error="Invalid FD."),400
    with lock, db() as con:
        investor=con.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        bank=con.execute("SELECT * FROM users WHERE id=?", (bank_id,)).fetchone()
        if not bank:return jsonify(error="Bank not found."),404
        amount=min(amount,investor["reserve"])
        if amount<=0:return jsonify(error="Insufficient reserve."),400
        con.execute("""INSERT INTO fixed_deposits
            (investor_id,bank_id,principal,rate,term_left,created_term)
            VALUES(?,?,?,?,?,?)""",
            (investor["id"],bank_id,amount,bank["fd_rate"],bank["fd_term"],investor["term"]))
        con.execute("UPDATE users SET reserve=reserve-? WHERE id=?",(amount,investor["id"]))
        con.execute("UPDATE users SET reserve=reserve+?,fd_balance=fd_balance+? WHERE id=?",
                    (amount,amount,bank_id))
        log_tx(con,investor["id"],"FD OPENED",1,bank["fd_rate"],amount,bank["bank_name"])
        log_tx(con,bank_id,"FD RECEIVED",1,bank["fd_rate"],amount,investor["bank_name"])
        con.commit()
    return jsonify(ok=True,amount=amount)

@app.get('/api/currency/book')
@login_required
def currency_book():
    """INR market: the only listed currency. Normal game transactions remain plain numbers."""
    with db() as con:
        bids=con.execute("""SELECT o.id,o.user_id,o.price,o.remaining,o.created_at,u.username
            FROM currency_orders o JOIN users u ON u.id=o.user_id
            WHERE o.side='BID' AND o.status='OPEN' AND o.remaining>0
            ORDER BY o.price DESC,o.created_at ASC,o.id ASC LIMIT 20""").fetchall()
        asks=con.execute("""SELECT o.id,o.user_id,o.price,o.remaining,o.created_at,u.username
            FROM currency_orders o JOIN users u ON u.id=o.user_id
            WHERE o.side='ASK' AND o.status='OPEN' AND o.remaining>0
            ORDER BY o.price ASC,o.created_at ASC,o.id ASC LIMIT 20""").fetchall()
        mine=con.execute("SELECT id,side,price,remaining,created_at,status FROM currency_orders WHERE user_id=? AND status='OPEN' AND remaining>0 ORDER BY created_at DESC,id DESC",(session['user_id'],)).fetchall()
        price=con.execute("SELECT ctc_price FROM users WHERE id=?",(session['user_id'],)).fetchone()['ctc_price']
        return jsonify(currency='INR', bids=[dict(r) for r in bids], asks=[dict(r) for r in asks], my_orders=[dict(r) for r in mine],
                       last_price=float(price or 1.0), best_bid=float(bids[0]['price']) if bids else None,
                       best_ask=float(asks[0]['price']) if asks else None)

def _match_currency(con):
    trades=[]
    while True:
        bid=con.execute("SELECT * FROM currency_orders WHERE side='BID' AND status='OPEN' AND remaining>1e-12 ORDER BY price DESC,created_at ASC,id ASC LIMIT 1").fetchone()
        ask=con.execute("SELECT * FROM currency_orders WHERE side='ASK' AND status='OPEN' AND remaining>1e-12 ORDER BY price ASC,created_at ASC,id ASC LIMIT 1").fetchone()
        if not bid or not ask or float(bid['price'])+1e-12 < float(ask['price']) or bid['user_id']==ask['user_id']: break
        price=float(bid['price']) if (bid['created_at'],bid['id']) <= (ask['created_at'],ask['id']) else float(ask['price'])
        qty=min(float(bid['remaining']),float(ask['remaining'])); amount=qty*price
        con.execute('UPDATE users SET reserve=reserve+? WHERE id=?',(float(bid['price'])*qty-amount,bid['user_id']))
        con.execute('UPDATE users SET reserve=reserve+? WHERE id=?',(amount,ask['user_id']))
        con.execute('UPDATE users SET currency=currency+? WHERE id=?',(qty,bid['user_id']))
        brem=float(bid['remaining'])-qty; arem=float(ask['remaining'])-qty
        con.execute("UPDATE currency_orders SET remaining=?,locked=?,status=? WHERE id=?",(brem,brem*float(bid['price']),'FILLED' if brem<=1e-12 else 'OPEN',bid['id']))
        con.execute("UPDATE currency_orders SET remaining=?,locked=?,status=? WHERE id=?",(arem,arem,'FILLED' if arem<=1e-12 else 'OPEN',ask['id']))
        con.execute('UPDATE users SET ctc_price=?',(price,))
        log_tx(con,bid['user_id'],'INR BUY',qty,price,amount,'INR currency market match')
        log_tx(con,ask['user_id'],'INR SELL',qty,price,amount,'INR currency market match')
        trades.append({'qty':qty,'price':price,'amount':amount})
    return trades

@app.post('/api/currency/order')
@login_required
def currency_order():
    data=request.get_json() or {}; side=str(data.get('side','')).upper()
    try: price=float(data.get('price',0)); qty=float(data.get('qty',0))
    except (TypeError,ValueError): return jsonify(error='Invalid price or quantity.'),400
    if side not in ('BID','ASK') or price<=0 or qty<=0 or not math.isfinite(price) or not math.isfinite(qty):
        return jsonify(error='Enter a valid INR bid/ask price and quantity.'),400
    with lock, db() as con:
        p=con.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
        locked=price*qty if side=='BID' else qty
        if side=='BID':
            if float(p['reserve'])+1e-9<locked:return jsonify(error=f'Insufficient reserve. Need {locked:,.2f}.'),400
            con.execute('UPDATE users SET reserve=reserve-? WHERE id=?',(locked,p['id']))
        else:
            if float(p['currency'])+1e-9<qty:return jsonify(error=f'Not enough INR. You hold {float(p["currency"]):g} INR.'),400
            con.execute('UPDATE users SET currency=currency-? WHERE id=?',(qty,p['id']))
        cur=con.execute("INSERT INTO currency_orders(user_id,side,price,qty,remaining,locked,created_at,status) VALUES(?,?,?,?,?,?,?,'OPEN')",(p['id'],side,price,qty,qty,locked,time.time()))
        oid=cur.lastrowid; trades=_match_currency(con); con.commit()
    return jsonify(ok=True,order_id=oid,trades=trades)

@app.post('/api/currency/cancel')
@login_required
def currency_cancel():
    data=request.get_json() or {}
    try: oid=int(data.get('order_id'))
    except (TypeError,ValueError): return jsonify(error='Invalid order.'),400
    with lock, db() as con:
        o=con.execute("SELECT * FROM currency_orders WHERE id=? AND user_id=? AND status='OPEN'",(oid,session['user_id'])).fetchone()
        if not o:return jsonify(error='Open order not found.'),404
        if o['side']=='BID': con.execute('UPDATE users SET reserve=reserve+? WHERE id=?',(float(o['locked']),session['user_id']))
        else: con.execute('UPDATE users SET currency=currency+? WHERE id=?',(float(o['remaining']),session['user_id']))
        con.execute("UPDATE currency_orders SET remaining=0,locked=0,status='CANCELLED' WHERE id=?",(oid,)); con.commit()
    return jsonify(ok=True)

@app.post('/api/currency/trade')
@login_required
def currency_trade():
    data=request.get_json() or {}; side=str(data.get('side','BUY')).upper()
    try: qty=float(data.get('qty',0))
    except (TypeError,ValueError): return jsonify(error='Invalid quantity.'),400
    if side not in ('BUY','SELL') or qty<=0:return jsonify(error='Invalid quantity or side.'),400
    with lock, db() as con:
        p=con.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
        if side=='BUY':
            o=con.execute("SELECT * FROM currency_orders WHERE side='ASK' AND status='OPEN' AND remaining>0 AND user_id!=? ORDER BY price ASC,created_at ASC,id ASC LIMIT 1",(p['id'],)).fetchone()
            if not o:return jsonify(error='No INR ask available. Place an ASK order first.'),400
            q=min(qty,float(o['remaining'])); price=float(o['price']); amount=q*price
            if float(p['reserve'])<amount:return jsonify(error='Insufficient reserve.'),400
            con.execute('UPDATE users SET reserve=reserve-?,currency=currency+? WHERE id=?',(amount,q,p['id']))
            con.execute('UPDATE users SET reserve=reserve+? WHERE id=?',(amount,o['user_id']))
        else:
            o=con.execute("SELECT * FROM currency_orders WHERE side='BID' AND status='OPEN' AND remaining>0 AND user_id!=? ORDER BY price DESC,created_at ASC,id ASC LIMIT 1",(p['id'],)).fetchone()
            if not o:return jsonify(error='No INR bid available. Place a BID order first.'),400
            q=min(qty,float(o['remaining'])); price=float(o['price']); amount=q*price
            if float(p['currency'])<q:return jsonify(error='Not enough INR.'),400
            con.execute('UPDATE users SET currency=currency-?,reserve=reserve+? WHERE id=?',(q,amount,p['id']))
            con.execute('UPDATE users SET currency=currency+? WHERE id=?',(q,o['user_id']))
        rem=float(o['remaining'])-q; locked=(rem*float(o['price'])) if o['side']=='BID' else rem
        con.execute("UPDATE currency_orders SET remaining=?,locked=?,status=? WHERE id=?",(rem,locked,'FILLED' if rem<=1e-12 else 'OPEN',o['id']))
        con.execute('UPDATE users SET ctc_price=?',(price,)); log_tx(con,p['id'],'INR '+side,q,price,amount,'INR currency market order'); con.commit()
    return jsonify(ok=True,qty=q,price=price,total=amount)

@app.get("/api/profile/<int:bank_id>")
@login_required
def profile(bank_id):
    with db() as con:
        r=con.execute("SELECT * FROM users WHERE id=?", (bank_id,)).fetchone()
        if not r:
            return jsonify(error="Bank not found"), 404

        d = user_dict(r)
        # Public financial details for the visited bank.
        active_lent = con.execute(
            "SELECT COALESCE(SUM(principal),0) FROM loans WHERE lender_id=? AND status='active'",
            (bank_id,)
        ).fetchone()[0]
        active_borrowed = con.execute(
            "SELECT COALESCE(SUM(principal),0) FROM loans WHERE borrower_id=? AND status='active'",
            (bank_id,)
        ).fetchone()[0]

        d.update(
            loans_lent=active_lent,
            loans_borrowed=active_borrowed,
            liabilities=d["deposits"] + active_borrowed,
            last_pl=d["last_pl"],
            history=d["history"],
        )
        return jsonify(d)

@app.get("/api/time")
def game_time():
    # This is tied to the actual server game-loop schedule, not a separately
    # rounded epoch timestamp. The browser therefore counts down to the real
    # next quarter.
    now = time.time()
    return jsonify(
        server_time=now,
        turn_seconds=TURN_SECONDS,
        next_turn=next_tick_at,
        seconds_left=max(0, next_tick_at - now)
    )

# Initialize the database and start the game loop when imported by WSGI/Gunicorn.
# In WSGI deployments, __name__ is not "__main__", so relying only on the
# __main__ block would leave the SQLite tables uninitialized and cause HTTP 500
# errors on login/register.
def start_background_services():
    global game_loop_started
    if game_loop_started:
        return
    with lock:
        if game_loop_started:
            return
        init_db()
        threading.Thread(target=game_loop, daemon=True, name="banksim-game-loop").start()
        game_loop_started = True

start_background_services()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
