---
name: trader-broker
description: >-
  Order Executor. Sends approved orders to Trading 212 through its public
  API, on the demo (practice money) account by default, never without your
  yes. Shows each order as a plain ticket (a bracket shows its stop-loss,
  take-profit and the max loss at the stop) plus the exact request t212.py
  will send, records your yes word for word, then Claude's own Allow once /
  Deny card asks again before t212.py runs. Puts the stop-loss at Trading 212
  after the entry fills, builds the sell ticket that closes a day trade by
  your flat-by time, records fills, and logs each trade's max loss so
  trader-risk can hold the daily limit. Covers making a Trading 212 API key
  (kept in environment variables on your own computer, never in chat), the
  Interactive Brokers connector, and keeping order commands on "ask". Use
  when the user says "place the order", "buy it", "execute", "send it to my
  broker", "close my position", "flatten", "get me out", "connect Trading
  212", "set up the demo account", or after /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches the market only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the `t212.py place` command is on "ask" in Claude Code.

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/t212.py" place --index 1             # sends it (Allow once first)
python3 "${CLAUDE_SKILL_DIR}/t212.py" positions --symbol TSLA     # read only
python3 "${CLAUDE_SKILL_DIR}/t212.py" protect --index 1           # the stop-loss, after the fill
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol TSLA --side buy --qty 8 --price 364.20
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol TSLA --qty 8
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, builds the flatten ticket, and keeps the log in
`trader/state/broker.json`. `t212.py` is the only file in the pack that sends
anything, and `place` refuses unless `ticket.py approve` recorded a yes for
that exact order today and nothing was sent for it yet. `--now "2026-10-19 10:15"`
pins the clock (New York time).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, sending, placed, failed, protected and filled, with each entry's max loss |
| `trader/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty` |
| `trader/plan.json` | the flat-by time for `flatten` |
| `trader/config.json` | `"broker": "trading212"`, `"paper": true` (demo), optional `"t212_tickers"` |

## Where it runs

`t212.py` needs two things only your own computer has: network access to
trading212.com and your API key in environment variables. So the step that
sends an order runs in **Claude Code on your computer** (terminal, desktop app
or IDE), started in the folder that holds `trader/`. Uploaded to claude.ai,
the skill still shows tickets and records your yes, but its sandbox cannot
reach Trading 212 and has no keys: there, place the order yourself in the
Trading 212 app with the ticket in front of you. Trading 212 has no official
MCP server or Claude connector (checked 2026-11-25), so this pack talks to
the API directly instead.

## Setup A: Trading 212 (public API, demo by default)

Checked against docs.trading212.com on 2026-11-25 (API v0, beta). The API
works only for **Invest and Stocks ISA** accounts (not CFD).

1. In the Trading 212 app, switch to **Practice** (the demo account). Make
   the key there: Settings > API (Beta) > Generate API key. A demo key works
   on demo.trading212.com only; a live key is a separate key made on the live
   account. Give it the permissions this pack uses: account, portfolio,
   metadata, orders read and orders execute. An IP restriction to your own
   address is a good idea. Trading 212 shows the **API key and the API secret
   once**: copy them into the next step, not into the chat.
2. Set them as environment variables on your own computer, where Claude Code
   runs. macOS or Linux, in `~/.zshrc` or `~/.bashrc`:

```bash
export T212_API_KEY="your key, typed in this file only"
export T212_API_SECRET="your secret, typed in this file only"
```

   Windows (Command Prompt, once): `setx T212_API_KEY "..."` and
   `setx T212_API_SECRET "..."`, then open a new terminal.
   **Keys never go in the chat**, in `trader/config.json`, in this repository
   or in any project `.claude/settings.json`. Never ask for them, never echo
   them, never read the shell profile back into the conversation, and if the
   user pastes a key anyway, tell them to delete that message and revoke the
   key in the Trading 212 app.
3. In `trader/config.json`: `"broker": "trading212"` and `"paper": true`.
   `t212.py` uses demo.trading212.com unless the config says `"paper": false`
   **and** the environment variable `T212_ENV` is `live`. If only one of the
   two says live, it refuses and sends nothing.
4. **Keep the sending commands on ask.** In Claude Code, add this to
   `.claude/settings.json` in the folder you trade from (also in
   `claude_settings.example.json` here), so Claude's own Allow once / Deny card
   appears before any command that sends, changes or cancels an order:

```json
{
  "permissions": {
    "ask": [
      "Bash(python3 *t212.py place*)",
      "Bash(python3 *t212.py protect*)",
      "Bash(python3 *t212.py cancel*)"
    ]
  }
}
```

   Never put these in `allow`, and never trade from a session in a mode that
   skips permission prompts.
5. Check it: `t212.py account` (read only) must print
   `Trading 212 DEMO (practice money) account` with your practice balance.

**Trading 212 against a bracket order.** Trading 212's API has market, limit,
stop and stop-limit orders, by number of shares (a sell is a negative
quantity), and nothing else. The gaps, and what this skill does about each:

| Trading 212's API | what trader-broker does |
| --- | --- |
| no bracket, one-cancels-other or take-profit orders | the entry goes first; after it fills, `protect` sends the stop-loss as its own stop sell order (good for today). The take-profit is not sent: trader-alerts watches it and you close with `flatten` and a new yes |
| **live account: market orders only** (limit, stop and stop-limit work on demo only) | on live, a limit entry is NOT READY, and a bracket is NOT READY unless `"allow_stop_by_alert": true`, because its stop could not sit at the broker. Then only trader-alerts watches the stop |
| no orders by amount (`notional`) | NOT READY: give a number of shares |
| no time in force on a market order; one sent while the market is closed waits and fills at the next open | trader-risk's entry window keeps entries inside the session |
| `DAY` or `GOOD_TILL_CANCEL` only on limit and stop orders | day and gtc map across; opg, cls, ioc and fok are NOT READY |
| no trailing stops; extended hours only on market orders | NOT READY; this pack trades the regular session |
| orders only in the account's main currency; prices in the stock's own currency | sizes and limits are in US dollars. With a EUR or GBP account, Trading 212 converts and charges its FX fee, so the cash used is not exactly the dollar amount on the ticket |
| order requests are not idempotent (a retry can buy twice) | `t212.py` never retries. No answer means the order is logged as placed (unconfirmed) and you check `orders` and `positions` first |
| tickers like `TSLA_US_EQ` | `TSLA` becomes `TSLA_US_EQ`. Check any symbol with `t212.py instrument --symbol XYZ`, and put an exception in `"t212_tickers"` |
| rate limits: market orders 50 a minute, limit and stop orders 1 every 2 seconds | one order per yes stays far below them |

## Setup B: Interactive Brokers (connector in Claude's directory)

Interactive Brokers publishes a verified connector: claude.ai/directory/interactive-brokers
(connector URL `https://api.ibkr.com/v1/api/mcp`). Add it from Customize >
Connectors > Browse connectors and sign in to IBKR there, not in the chat.
Its listing says Claude can draft trade instructions that are sent to the
user's IBKR account for their review and approval, and that Claude never
submits orders directly to the market. So with IBKR the final yes happens in
IBKR as well. Set `"broker": "ibkr"` in `trader/config.json`. Set its tools
that draft orders to Needs approval too.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk` (or, for "NOT READY for
   Trading 212", to `/trader-budget` to change the order).
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order (for example "sure", "yes to all", or a yes that names a different
   stock or amount). Ask again. Never turn a reply into a yes yourself.
4. Only after "Recorded your yes", run `t212.py place --index N`. Claude's
   card appears: Allow once or Deny. It sends exactly the request the ticket
   printed and logs placed (with Trading 212's order id) or failed by itself.
   If the user denies the card: `ticket.py result --index N --status denied`.
   A failed order is not retried without a new check and a new yes.
5. `t212.py positions --symbol TSLA` shows the fill. Record it:
   `ticket.py fill --symbol TSLA --side buy --qty 8 --price <averagePricePaid>`.
6. For a bracket on demo: `t212.py protect --index N` (Allow once) puts the
   stop-loss at Trading 212 for the shares it says you hold. The user's yes
   covered it: it was on the ticket. If it fails, tell the user at once: the
   position is open with no stop at the broker.

## Steps: while it is open, and out by the close

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). On demo the stop order at Trading 212 does
  the stopping; the target is only an alert.
- When the stop-loss fills, record it:
  `ticket.py fill --symbol TSLA --side sell --qty 8 --price 358.32`.
- At the target, or by `flat_by` (15:55 New York), close what is still open:
  1. `t212.py positions --symbol TSLA` (read only) for the shares held.
  2. `ticket.py flatten --symbol TSLA --qty 8` writes a market sell for those
     shares to `trader/orders.json`.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order, as above.
  5. Cancel the pending stop-loss first, or it would sell the same shares:
     `t212.py orders` shows it, `t212.py cancel --id ID` (Allow once). Then
     `t212.py place --index 1`, then record the fill.

## Example: the reel

```
ORDER 1 of 1, DEMO account (practice money), Trading 212
  BUY 8 TSLA, market, good for today
  stop-loss 358.32
  take-profit 370.04
  max loss about $46.88 if the stop fills at 358.32 (it can fill worse in a fast market)
  why: opening-range breakout on TSLA: 8 shares, bound by your $50 max loss per trade (8 x 5.86 = $46.88 at the stop)
  READY: trader-risk PASS, waiting for your yes
  Claude will run t212.py place --index 1, which sends exactly this to demo.trading212.com:
    POST /api/v0/equity/orders/market
    {"ticker": "TSLA_US_EQ", "quantity": 8, "extendedHours": false}
  then, once the entry fills, t212.py protect --index 1 sends the stop-loss:
    POST /api/v0/equity/orders/stop
    {"ticker": "TSLA_US_EQ", "quantity": -8, "stopPrice": 358.32, "timeValidity": "DAY"}
  note: take-profit 370.04 is not placed at Trading 212: its API has no bracket or one-cancels-other orders, ...
```

The user replies "yes". Claude records it and runs `t212.py place --index 1`;
the card shows that command with two buttons: Allow once, Deny. Allow once
sends it to the demo account. The `why` and the plan stay in the file; they
are never sent.

## What it never does

- Never places an order without all three gates, and never two orders on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys or passwords.
- Never switches `"paper"` or `T212_ENV` to live. The user does that themselves.
- Never retries an order request: Trading 212 can take a repeat as a second order.
- Never cancels an order the user did not see on a ticket, and never sells shares the ticket does not name.
- Never promises a fill price: a market order and a stop fill at whatever the market gives.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Demo (practice money) by default (`"paper": true`). Going live is the user's own decision, in their
  own config and their own environment (`T212_ENV=live`).
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order command is on "ask"
  (`.claude/settings.json` permissions). Never put an order command in "allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables on the user's own computer, never in the chat or a file in
  this pack.
