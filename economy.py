"""Server-side bank economy: one call = one quarter for one bank state (a dict).
Ported from the original browser code so quarters run on the server."""
import random, time


def clamp(v, a, b):
    return max(a, min(b, v))


def equity_of(p):
    return (p["reserve"] + p["loans"]["personal"] + p["loans"]["home"] + p["bonds"]["holdings"] * p["bonds"]["price"] / 100
            - p["deposits"]["savings"] - p["deposits"]["fd"] - p["funding"])


def log(p, market, side, qty, price, total):
    p["transactions"].insert(0, {"market": market, "side": side, "quantity": qty, "price": price, "total": total})
    del p["transactions"][30:]


def advance_quarter(p):
    r, L, D, B = p["rates"], p["loans"], p["deposits"], p["bonds"]
    prev_central = r["central"]
    prev_loans = L["personal"] + L["home"]
    prev_net = p["lastPL"].get("net") or 0
    assets_pre = p["reserve"] + prev_loans + B["holdings"] * B["price"] / 100

    inc_p, inc_h = L["personal"] * r["personal"] / 400, L["home"] * r["home"] / 400
    inc_b = B["holdings"] * r["central"] / 400
    income = inc_p + inc_h + inc_b
    exp_s, exp_f = D["savings"] * r["savings"] / 400, D["fd"] * r["fd"] / 400
    exp_fund = p["funding"] * (r["central"] + 1) / 400
    expense = exp_s + exp_f + exp_fund
    nii = income - expense

    p_risk = clamp(0.4 + max(0, r["central"] + 6 - r["personal"]) * 0.15 + max(0, p["inflation"] - 6) * 0.1, 0.2, 9)
    h_risk = clamp(0.2 + max(0, r["central"] + 3 - r["home"]) * 0.10 + max(0, p["inflation"] - 6) * 0.05, 0.1, 5)
    wo_p, wo_h = L["personal"] * p_risk / 100, L["home"] * h_risk / 100
    provisions = wo_p + wo_h
    L["personal"], L["home"] = max(0, L["personal"] - wo_p), max(0, L["home"] - wo_h)

    opex = assets_pre * 0.0015
    pretax = nii - provisions - opex
    tax = pretax * 0.25 if pretax > 0 else 0
    net = pretax - tax
    p["reserve"] += net
    if B["holdings"] > 0:
        log(p, "Bonds", "COUPON", B["holdings"], prev_central, inc_b)

    D["savings"] = max(0, D["savings"] * (1 + clamp((r["savings"] - r["central"]) * 0.02, -0.06, 0.08)))
    D["fd"] = max(0, D["fd"] * (1 + clamp((r["fd"] - r["central"]) * 0.02, -0.06, 0.08)))
    L["personal"] = max(0, L["personal"] * (1 + clamp((r["central"] + 9 - r["personal"]) * 0.02, -0.10, 0.15)))
    L["home"] = max(0, L["home"] * (1 + clamp((r["central"] + 6 - r["home"]) * 0.02, -0.10, 0.15)))

    loan_growth = ((L["personal"] + L["home"] - prev_loans) / prev_loans) if prev_loans > 0 else 0
    p["gdp"] *= 1 + clamp(0.005 + loan_growth * 0.3, -0.01, 0.03)
    B["cap"] = max(B["cap"], p["gdp"] * 0.5)  # new bonds only when GDP rises above its peak
    p["inflation"] = clamp(p["inflation"] + clamp(loan_growth * 8 - (r["central"] - 4) * 0.15 + (random.random() * 0.4 - 0.2), -1.5, 1.5), 0, 25)
    r["central"] = clamp(r["central"] + clamp((p["inflation"] - 4) * 0.08 + (random.random() * 0.1 - 0.05), -0.4, 0.4), 0.5, 16)
    B["price"] = clamp(B["price"] * (1 - (r["central"] - prev_central) * 0.04), 60, 140)

    equity = equity_of(p)
    roe = (net * 4 / equity) if equity > 0 else 0
    p["multiplier"] = clamp(10 + roe * 20, 5, 18)
    fair = max(0.01, equity * p["multiplier"] / p["sharesOutstanding"])
    surprise = clamp((net - prev_net) / max(1, abs(prev_net)), -1, 1) * 0.05
    p["stock"]["price"] = max(0.01, p["stock"]["price"] * 0.6 + fair * 0.4) * (1 + surprise)
    p["marketCap"] = p["stock"]["price"] * p["sharesOutstanding"]

    p["quarter"] += 1
    eps = net / p["sharesOutstanding"]
    roa = net * 4 / assets_pre * 100 if assets_pre > 0 else 0
    nim = nii * 4 / assets_pre * 100 if assets_pre > 0 else 0
    p["lastPL"] = {"income": {"personal": inc_p, "home": inc_h, "bonds": inc_b, "total": income},
                   "expense": {"savings": exp_s, "fd": exp_f, "funding": exp_fund, "total": expense},
                   "nii": nii, "provisions": provisions, "opex": opex, "pretax": pretax, "tax": tax, "net": net,
                   "eps": eps, "roe": roe * 100, "roa": roa, "nim": nim}
    log(p, "P&L", "PROFIT" if net >= 0 else "LOSS", 1, net, net)
    p["history"].append({"q": p["quarter"], "equity": equity, "sharePrice": p["stock"]["price"], "net": net, "eps": eps, "roe": roe * 100})
    del p["history"][:-24]


def public_doc(p):
    L, D, B = p["loans"], p["deposits"], p["bonds"]
    equity, rwa = equity_of(p), L["personal"] + L["home"]
    dep = D["savings"] + D["fd"]
    bond_val = B["holdings"] * B["price"] / 100
    return {"bankName": p["bankName"], "stockPrice": p["stock"]["price"], "sharesOutstanding": p["sharesOutstanding"],
            "marketCap": p["marketCap"], "dividendPerShare": p.get("dividendPerShare") or 0, "quarter": p["quarter"],
            "inflation": p["inflation"], "gdp": p["gdp"], "centralRate": p["rates"]["central"],
            "equity": equity, "totalAssets": p["reserve"] + rwa + bond_val, "reserve": p["reserve"],
            "loanPersonal": L["personal"], "loanHome": L["home"], "bondMktVal": bond_val,
            "depositsSavings": D["savings"], "depositsFD": D["fd"], "funding": p["funding"],
            "car": (equity / rwa * 100) if rwa > 0 else 100, "crrOk": p["reserve"] >= dep * 0.04,
            "ldr": (rwa / dep * 100) if dep > 0 else 0, "pl": p["lastPL"],
            "history": [{"q": h["q"], "equity": h["equity"]} for h in p["history"][-12:]],
            "updatedAt": int(time.time() * 1000)}
