import base64
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from _load import load, ROOT, FIX

t212 = load("trader-broker", "t212")
ticket = t212.ticket
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
ORDERS_PATH = os.path.join(FIX, "trader-risk", "orders-tsla.json")
NOW = "2026-10-19 10:15"
KEYS = {"T212_API_KEY": "demo-key-123", "T212_API_SECRET": "demo-secret-456"}


class FakeResp(object):
    def __init__(self, body):
        self.body = body

    def read(self):
        return json.dumps(self.body).encode("utf-8") if self.body is not None else b""


class FakeT212(object):
    """Stands in for Trading 212: records every request, answers from a script."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.sent = []

    def __call__(self, req):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.sent.append({"method": req.get_method(), "url": req.full_url, "body": body,
                          "auth": req.get_header("Authorization")})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return FakeResp(a)


def http_error(code, body):
    return urllib.error.HTTPError("https://demo.trading212.com", code, "err", {},
                                  io.BytesIO(json.dumps(body).encode("utf-8")))


class Environment(unittest.TestCase):
    def test_demo_by_default_and_live_needs_both_switches(self):
        self.assertEqual(t212.environment({}, {}), "demo")
        self.assertEqual(t212.environment({"paper": True}, {}), "demo")
        with self.assertRaises(ticket.TicketError):
            t212.environment({"paper": False}, {})
        with self.assertRaises(ticket.TicketError):
            t212.environment({"paper": True}, {"T212_ENV": "live"})
        self.assertEqual(t212.environment({"paper": False}, {"T212_ENV": "live"}), "live")

    def test_missing_keys_are_explained_not_requested_in_chat(self):
        with self.assertRaises(ticket.TicketError) as cm:
            t212.credentials({})
        self.assertIn("Never paste them into the chat", str(cm.exception))


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        shutil.copy(ORDERS_PATH, self.orders)
        self.cfg = os.path.join(self.d, "config.json")
        shutil.copy(CFG_PATH, self.cfg)
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.none = os.path.join(self.d, "none")
        self.env = mock.patch.dict(os.environ, KEYS, clear=False)
        self.env.start()
        os.environ.pop("T212_ENV", None)
        self.old_send = t212.SEND

    def tearDown(self):
        t212.SEND = self.old_send
        self.env.stop()
        shutil.rmtree(self.d)

    def args(self, *extra):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", NOW]

    def run_t212(self, argv, answers):
        fake = FakeT212(answers)
        t212.SEND = fake
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = t212.main(argv)
        return code, out.getvalue(), err.getvalue(), fake.sent

    def ready_and_approved(self):
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.none,
                        "--prices", self.none, "--state", self.state, "--now", NOW, "--out", self.risk])
            self.assertEqual(ticket.main(self.args("approve", "--index", "1", "--reply", "yes")), 0)

    def statuses(self):
        with open(self.state) as fh:
            return [r["status"] for r in json.load(fh)["orders"]]

    def test_nothing_is_sent_without_a_yes(self):
        with redirect_stdout(io.StringIO()):
            check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.none,
                        "--prices", self.none, "--state", self.state, "--now", NOW, "--out", self.risk])
        code, _, err, sent = self.run_t212(self.args("place", "--index", "1"), [])
        self.assertEqual((code, sent), (1, []))
        self.assertIn("no yes", err)

    def test_place_sends_exactly_the_ticket_once_to_demo(self):
        self.ready_and_approved()
        code, out, _, sent = self.run_t212(self.args("place", "--index", "1"),
                                           [{"id": 4711, "status": "NEW", "filledQuantity": 0}])
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["url"], "https://demo.trading212.com/api/v0/equity/orders/market")
        self.assertEqual(sent[0]["body"], {"ticker": "TSLA_US_EQ", "quantity": 8, "extendedHours": False})
        self.assertEqual(sent[0]["auth"], "Basic " + base64.b64encode(b"demo-key-123:demo-secret-456").decode())
        self.assertNotIn("demo-key-123", out)
        self.assertNotIn("demo-secret-456", out)
        self.assertIn("DEMO", out)
        self.assertIn("t212.py protect --index 1", out)
        self.assertEqual(self.statuses(), ["approved", "sending", "placed"])
        with open(self.state) as fh:
            placed = json.load(fh)["orders"][-1]
        self.assertEqual((placed["broker_order_id"], placed["max_loss"]), ("4711", 46.88))
        # a second place for the same yes sends nothing
        code, _, err, sent = self.run_t212(self.args("place", "--index", "1"), [])
        self.assertEqual((code, sent), (1, []))
        # trader-risk counts it toward today's limits
        with open(self.state) as fh:
            st = json.load(fh)
        res = check.check([ticket.read_json(self.orders, "o")["orders"][0]], {"budget": 5000}, state=st,
                          now=check.parse_now("2026-10-19 10:30"))
        self.assertEqual(res["today"]["entries_placed"], 1)

        # the stop-loss goes after the fill, for the shares Trading 212 says are held
        code, out, _, sent = self.run_t212(
            self.args("protect", "--index", "1"),
            [[{"instrument": {"ticker": "TSLA_US_EQ"}, "quantity": 8, "quantityAvailableForTrading": 8}],
             {"id": 4712, "status": "NEW"}])
        self.assertEqual(code, 0, out)
        self.assertEqual(sent[1]["url"], "https://demo.trading212.com/api/v0/equity/orders/stop")
        self.assertEqual(sent[1]["body"], {"ticker": "TSLA_US_EQ", "quantity": -8, "stopPrice": 358.32,
                                           "timeValidity": "DAY"})
        self.assertEqual(self.statuses()[-1], "protected")

    def test_a_refusal_is_logged_as_failed_and_not_retried(self):
        self.ready_and_approved()
        code, out, _, sent = self.run_t212(
            self.args("place", "--index", "1"),
            [http_error(400, {"code": "InsufficientResources", "clarification": "Not enough cash"})])
        self.assertEqual((code, len(sent)), (1, 1))
        self.assertIn("InsufficientResources", out)
        self.assertEqual(self.statuses(), ["approved", "sending", "failed"])

    def test_no_answer_counts_as_placed_and_warns(self):
        self.ready_and_approved()
        code, out, _, sent = self.run_t212(self.args("place", "--index", "1"),
                                           [urllib.error.URLError("timed out")])
        self.assertEqual((code, len(sent)), (1, 1))
        self.assertIn("MAY have been placed", out)
        self.assertEqual(self.statuses()[-1], "placed")
        code, _, err, sent = self.run_t212(self.args("protect", "--index", "1"), [])
        self.assertEqual((code, sent), (1, []))
        self.assertIn("unconfirmed", err)

    def test_a_live_config_without_t212_env_sends_nothing(self):
        with open(self.cfg) as fh:
            c = json.load(fh)
        c["paper"] = False
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)
        code, _, err, sent = self.run_t212(["account", "--config", self.cfg], [])
        self.assertEqual((code, sent), (1, []))
        self.assertIn("T212_ENV", err)

    def test_read_only_commands(self):
        code, out, _, sent = self.run_t212(["account", "--config", self.cfg], [
            {"id": 1, "currency": "EUR", "totalValue": 5000, "cash": {"availableToTrade": 5000},
             "investments": {}}])
        self.assertEqual(code, 0)
        self.assertIn("DEMO (practice money)", out)
        self.assertEqual(sent[0]["url"], "https://demo.trading212.com/api/v0/equity/account/summary")
        code, out, _, _ = self.run_t212(["orders", "--config", self.cfg], [
            [{"id": 9, "type": "STOP", "ticker": "TSLA_US_EQ", "quantity": -8, "stopPrice": 358.32, "status": "NEW"}]])
        self.assertIn("id 9  STOP TSLA_US_EQ SELL qty 8", out)
        code, out, _, sent = self.run_t212(["cancel", "--id", "9", "--config", self.cfg], [None])
        self.assertEqual((code, sent[0]["method"]), (0, "DELETE"))


if __name__ == "__main__":
    unittest.main()
