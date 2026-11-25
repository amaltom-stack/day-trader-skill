#!/usr/bin/env python3
"""Trading 212 client for trader-broker. Standard library only.

This is the only file in the pack that sends anything to a broker. It talks
to Trading 212's public API (v0, beta) and it uses the DEMO (practice money)
account unless trader/config.json says "paper": false AND the environment
variable T212_ENV is "live". Both, or it refuses.

The API key and secret come from the environment variables T212_API_KEY and
T212_API_SECRET, set by you on your own computer. This tool never prints them,
never writes them to a file, and never asks for them.

Read only (safe to allow):
  account                 the account summary: DEMO or LIVE, currency, cash
  positions [--symbol S]  open positions: quantity, average price, current price
  orders                  pending orders (not yet filled, canceled or expired)
  instrument --symbol S   Trading 212's ticker for a symbol (TSLA -> TSLA_US_EQ)

Sends an order (keep each on "ask" so Claude's Allow once card shows first):
  place --index N         sends order N from trader/orders.json, only if
                          ticket.py approve recorded your yes for that exact
                          order today and nothing was sent for it yet
  protect --index N       for a bracket entry placed today on demo: sends its
                          stop-loss as a stop sell order once the entry filled
  cancel --id ID          cancels one pending order

An order request is never retried: Trading 212 says its order endpoints are
not idempotent in this beta, so a retry can buy twice. If an answer never
comes back, the order is logged as placed (unconfirmed) and you check
`orders` and `positions` before doing anything else.
"""

import argparse
import base64
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASES = {"demo": "https://demo.trading212.com", "live": "https://live.trading212.com"}
TIMEOUT = 20


def _load_ticket():
    spec = importlib.util.spec_from_file_location("trader_ticket_for_t212", os.path.join(HERE, "ticket.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ticket = _load_ticket()
TicketError = ticket.TicketError


class T212Error(Exception):
    """An answer from Trading 212 that is not a success (status, code, text)."""

    def __init__(self, status, code, text):
        Exception.__init__(self, "%s %s: %s" % (status, code, text))
        self.status, self.code, self.text = status, code, text


class NoAnswer(Exception):
    """No answer came back (timeout or network): the outcome is unknown."""


# ---------------------------------------------------------------- environment and keys

def environment(cfg, env=None):
    """demo or live. Live needs both "paper": false in the config and T212_ENV=live."""
    env = os.environ if env is None else env
    want = str(env.get("T212_ENV", "") or "").strip().lower()
    if cfg.get("paper", True):
        if want == "live":
            raise TicketError("T212_ENV is live but trader/config.json says \"paper\": true. Nothing was sent. "
                              "The demo account is the default; going live is your own change in both places")
        return "demo"
    if want != "live":
        raise TicketError("trader/config.json says \"paper\": false, but T212_ENV is not \"live\". Nothing was "
                          "sent. Live trading needs both, set by you on your own computer")
    return "live"


def credentials(env=None):
    env = os.environ if env is None else env
    key, secret = env.get("T212_API_KEY"), env.get("T212_API_SECRET")
    if not key or not secret:
        raise TicketError("T212_API_KEY and T212_API_SECRET are not set in this environment. Make a key in the "
                          "Trading 212 app (Settings > API) and set both on your own computer (see the "
                          "trader-broker SKILL.md, Setup). Never paste them into the chat")
    return key, secret


def _urlopen(req):
    return urllib.request.urlopen(req, timeout=TIMEOUT)


SEND = _urlopen   # tests replace this


def call(method, path, cfg, body=None, env=None):
    """One request to Trading 212. Returns the decoded JSON (or None). Never retries."""
    base = BASES[environment(cfg, env)]
    key, secret = credentials(env)
    token = base64.b64encode(("%s:%s" % (key, secret)).encode("utf-8")).decode("ascii")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Authorization", "Basic " + token)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "trader-broker/2")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = SEND(req)
        raw = resp.read()
    except urllib.error.HTTPError as e:
        text = ""
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        code, why = "", text.strip()[:300]
        try:
            doc = json.loads(text)
            if isinstance(doc, dict):
                code = str(doc.get("code") or doc.get("errorCode") or "")
                why = str(doc.get("clarification") or doc.get("message") or why)
        except ValueError:
            pass
        if e.code == 401:
            why = "the key or secret was refused (check T212_API_KEY and T212_API_SECRET, and that the key is " \
                  "for this %s account)" % environment(cfg, env)
        elif e.code == 403 and not why:
            why = "the key is missing a permission (scope) for this; edit the key in the Trading 212 app"
        elif e.code == 429:
            why = "rate limited by Trading 212; wait and try once more later. " + why
        raise T212Error(e.code, code, why)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NoAnswer(str(getattr(e, "reason", e)))
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


# ---------------------------------------------------------------- helpers

def _cfg(path):
    return ticket.read_json(path, "config", required=False) or {}


def _record(state_path, rec):
    state = ticket.load_state(state_path)
    state.setdefault("orders", []).append(rec)
    ticket.write_json(state_path, state)


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


def _approved(a):
    """The order, config, today's approval record and the plan, or an error."""
    orders, cfg, risk, state, today, now = ticket._load(a)
    if ticket.broker_of(cfg) != "trading212":
        raise TicketError("trader/config.json does not say \"broker\": \"trading212\"")
    order = ticket._pick(orders, a.index)
    fp = ticket.fingerprint(order)
    rv, why = ticket.risk_verdict(risk, fp, today)
    if rv is None or rv.get("verdict") != "PASS":
        raise TicketError("order %d has no trader-risk PASS today: %s" % (a.index, why or "blocked"))
    rec = ticket.latest(state, fp, today)
    return orders, order, cfg, rec, today, now, fp


# ---------------------------------------------------------------- commands

def cmd_account(a):
    cfg = _cfg(a.config)
    env = environment(cfg)
    s = call("GET", "/api/v0/equity/account/summary", cfg) or {}
    cash = s.get("cash") or {}
    inv = s.get("investments") or {}
    print("Trading 212 %s account %s, currency %s" % ("DEMO (practice money)" if env == "demo"
                                                     else "LIVE, REAL MONEY", s.get("id", "?"), s.get("currency", "?")))
    print("  total value %s, cash available to trade %s, reserved for orders %s"
          % (_fmt(s.get("totalValue")), _fmt(cash.get("availableToTrade")), _fmt(cash.get("reservedForOrders"))))
    print("  invested %s now worth %s, unrealized P/L %s"
          % (_fmt(inv.get("totalCost")), _fmt(inv.get("currentValue")), _fmt(inv.get("unrealizedProfitLoss"))))
    return 0


def cmd_positions(a):
    cfg = _cfg(a.config)
    path = "/api/v0/equity/positions"
    want = ticket.t212_ticker(a.symbol, cfg) if a.symbol else None
    if want:
        path += "?ticker=" + want
    rows = call("GET", path, cfg) or []
    rows = [r for r in rows if not want or (r.get("instrument") or {}).get("ticker", r.get("ticker")) == want]
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No open position%s on the Trading 212 %s account." % (" in " + want if want else "", environment(cfg)))
        return 1
    for r in rows:
        ins = r.get("instrument") or {}
        print("%s  qty %s (available to sell %s)  average paid %s  now %s %s"
              % (ins.get("ticker", r.get("ticker")), _fmt(r.get("quantity")), _fmt(r.get("quantityAvailableForTrading")),
                 _fmt(r.get("averagePricePaid")), _fmt(r.get("currentPrice")), ins.get("currency", "")))
    return 0


def cmd_orders(a):
    cfg = _cfg(a.config)
    rows = call("GET", "/api/v0/equity/orders", cfg) or []
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No pending orders on the Trading 212 %s account." % environment(cfg))
        return 1
    for r in rows:
        print("id %s  %s %s %s qty %s  limit %s stop %s  status %s"
              % (r.get("id"), r.get("type"), r.get("ticker"), "SELL" if (r.get("quantity") or 0) < 0 else "BUY",
                 _fmt(abs(r.get("quantity") or 0)), _fmt(r.get("limitPrice")), _fmt(r.get("stopPrice")),
                 r.get("status")))
    return 0


def cmd_instrument(a):
    cfg = _cfg(a.config)
    sym = a.symbol.strip().upper()
    rows = call("GET", "/api/v0/equity/metadata/instruments", cfg) or []
    guess = ticket.t212_ticker(sym, cfg)
    hits = [r for r in rows if r.get("ticker") == guess or str(r.get("shortName", "")).upper() == sym]
    if not hits:
        print("No Trading 212 instrument for %s (looked for %s). It may not be tradable there." % (sym, guess))
        return 1
    for r in hits:
        print("%s  %s  %s  %s  isin %s%s" % (r.get("ticker"), r.get("name"), r.get("type"), r.get("currencyCode"),
                                             r.get("isin"), "" if r.get("ticker") == guess else
                                             "   (add \"t212_tickers\": {\"%s\": \"%s\"} to trader/config.json)"
                                             % (sym, r.get("ticker"))))
    return 0


def cmd_place(a):
    orders, order, cfg, rec, today, now, fp = _approved(a)
    if not rec or rec.get("status") != "approved":
        raise TicketError("order %d has no yes waiting to be sent today (%s). Nothing was sent. Show the ticket "
                          "and ask the user: ticket.py show --index %d"
                          % (a.index, "last: %s" % rec.get("status") if rec else "no yes recorded", a.index))
    steps, problems, notes = ticket.t212_plan(order, cfg)
    if problems:
        raise TicketError("order %d cannot go to Trading 212: %s" % (a.index, "; ".join(problems)))
    env = environment(cfg)
    credentials()
    step = steps[0]
    base = {"date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"], "side": rec["side"],
            "plain": rec["plain"], "broker": "trading212", "environment": env}
    for k in ("qty", "kind", "max_loss", "stop"):
        if rec.get(k) is not None:
            base[k] = rec[k]
    # written before the request, so a crash in between can never lead to a second send
    _record(a.state, dict(base, at=ticket.ny_iso(now), status="sending", request=step))
    try:
        resp = call(step["method"], step["path"], cfg, step["body"]) or {}
    except T212Error as e:
        _record(a.state, dict(base, at=ticket.ny_iso(now), status="failed",
                              note="Trading 212 said %s %s: %s" % (e.status, e.code, e.text)))
        print("Not placed. Trading 212 said %s %s: %s" % (e.status, e.code, e.text))
        print("Recorded as failed. A failed order is not retried without a new risk check and a new yes.")
        return 1
    except NoAnswer as e:
        _record(a.state, dict(base, at=ticket.ny_iso(now), status="placed",
                              note="UNCONFIRMED: no answer from Trading 212 (%s)" % e))
        print("No answer from Trading 212 (%s). The order MAY have been placed." % e)
        print("It is logged as placed so the daily limits count it. Do not send it again: check")
        print("t212.py orders and t212.py positions --symbol %s first." % rec["symbol"])
        return 1
    new = dict(base, at=ticket.ny_iso(now), status="placed", broker_order_id=str(resp.get("id", "")),
               broker_status=resp.get("status"))
    _record(a.state, new)
    print("Placed on the Trading 212 %s account: %s." % ("DEMO" if env == "demo" else "LIVE", rec["plain"]))
    print("Order id %s, status %s, filled %s." % (resp.get("id"), resp.get("status"), _fmt(resp.get("filledQuantity"))))
    print("Next: t212.py positions --symbol %s shows the fill; record it with" % rec["symbol"])
    print("  ticket.py fill --symbol %s --side %s --qty Q --price P   (P = averagePricePaid)" % (rec["symbol"], rec["side"]))
    if any(s["step"] == "stop-loss" for s in steps):
        print("Then put the stop-loss at the broker: t212.py protect --index %d" % a.index)
    for n in notes:
        print("Note: " + n)
    return 0


def cmd_protect(a):
    orders, order, cfg, rec, today, now, fp = _approved(a)
    steps = ticket.t212_plan(order, cfg)[0]
    leg = [s for s in steps if s["step"] == "stop-loss"]
    if not leg:
        raise TicketError("order %d has no stop-loss leg to send (not a bracket, or a live account)" % a.index)
    if not rec or rec.get("status") != "placed":
        raise TicketError("the entry for order %d is not placed today (last: %s). The stop-loss goes after the "
                          "entry fills" % (a.index, rec.get("status") if rec else "nothing"))
    if str(rec.get("note", "")).startswith("UNCONFIRMED"):
        raise TicketError("the entry is unconfirmed. Check t212.py orders and positions, and record what "
                          "happened, before sending a stop")
    want = ticket._f(order.get("qty")) or 0
    body = dict(leg[0]["body"])
    if a.qty is not None:
        held = ticket._f(a.qty)
    else:
        rows = call("GET", "/api/v0/equity/positions?ticker=" + body["ticker"], cfg) or []
        held = sum(ticket._f(r.get("quantityAvailableForTrading", r.get("quantity"))) or 0 for r in rows
                   if (r.get("instrument") or {}).get("ticker", r.get("ticker")) == body["ticker"])
    if not held or held <= 0:
        print("The entry has not filled yet (no %s shares available to sell). Try again in a moment." % body["ticker"])
        return 1
    qty = min(held, want)
    body["quantity"] = ticket._num(-qty)
    base = {"date": today, "index": a.index, "fingerprint": fp, "symbol": rec["symbol"], "side": "sell",
            "broker": "trading212", "environment": environment(cfg),
            "plain": "stop-loss for %s: SELL %s %s at stop %s" % (rec["plain"].split(" / ")[0], ticket._str_num(qty),
                                                                rec["symbol"], ticket._p(body["stopPrice"]))}
    _record(a.state, dict(base, at=ticket.ny_iso(now), status="protecting", request=dict(leg[0], body=body)))
    try:
        resp = call(leg[0]["method"], leg[0]["path"], cfg, body) or {}
    except T212Error as e:
        _record(a.state, dict(base, at=ticket.ny_iso(now), status="protect_failed",
                              note="Trading 212 said %s %s: %s" % (e.status, e.code, e.text)))
        print("The stop-loss was NOT placed. Trading 212 said %s %s: %s" % (e.status, e.code, e.text))
        print("The position is open with no stop at the broker. trader-alerts watches the stop; tell the user now.")
        return 1
    except NoAnswer as e:
        _record(a.state, dict(base, at=ticket.ny_iso(now), status="protected",
                              note="UNCONFIRMED: no answer from Trading 212 (%s)" % e))
        print("No answer from Trading 212 (%s). Check t212.py orders before sending anything again." % e)
        return 1
    _record(a.state, dict(base, at=ticket.ny_iso(now), status="protected", broker_order_id=str(resp.get("id", ""))))
    print("Stop-loss placed: SELL %s %s if it trades at %s (order id %s, good for today)."
          % (ticket._str_num(qty), rec["symbol"], ticket._p(body["stopPrice"]), resp.get("id")))
    return 0


def cmd_cancel(a):
    cfg = _cfg(a.config)
    if not str(a.id).isdigit():
        raise TicketError("--id wants the numeric order id from t212.py orders")
    call("DELETE", "/api/v0/equity/orders/%s" % a.id, cfg)
    print("Cancel request accepted for order %s on the %s account. It is not guaranteed if the order is already "
          "filling; check t212.py orders." % (a.id, environment(cfg)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trading 212 client for trader-broker (demo by default).")
    sub = ap.add_subparsers(dest="cmd")

    def cfgarg(p):
        p.add_argument("--config", default="trader/config.json")

    def common(p):
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--risk", default="trader/state/risk.json")
        p.add_argument("--state", default="trader/state/broker.json")
        p.add_argument("--now")
        p.add_argument("--today")
        cfgarg(p)

    p = sub.add_parser("account", help="account summary (read only)")
    cfgarg(p)
    p = sub.add_parser("positions", help="open positions (read only)")
    cfgarg(p)
    p.add_argument("--symbol")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("orders", help="pending orders (read only)")
    cfgarg(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("instrument", help="Trading 212 ticker for a symbol (read only)")
    cfgarg(p)
    p.add_argument("--symbol", required=True)
    p = sub.add_parser("place", help="send one approved order")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p = sub.add_parser("protect", help="send the stop-loss for a placed bracket entry (demo)")
    common(p)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--qty", help="shares to protect (default: what Trading 212 says you hold)")
    p = sub.add_parser("cancel", help="cancel one pending order")
    cfgarg(p)
    p.add_argument("--id", required=True)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"account": cmd_account, "positions": cmd_positions, "orders": cmd_orders,
                "instrument": cmd_instrument, "place": cmd_place, "protect": cmd_protect,
                "cancel": cmd_cancel}[a.cmd](a)
    except (TicketError, T212Error, NoAnswer) as e:
        print("t212.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
