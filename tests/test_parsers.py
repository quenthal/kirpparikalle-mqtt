import json
import tempfile
import unittest
import urllib.parse
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

import kk_mqtt
from kk_mqtt import (
    TZ,
    AuthExpired,
    KirppariKalleClient,
    LoginFailed,
    PageDoc,
    parse_data_updated,
    choose_shop_option,
    keepalive_due,
    relogin_allowed,
    build_reservation_payload,
    final_read_due,
    next_metadata_refresh,
    parse_balance,
    parse_opening_hours,
    parse_reservations,
    parse_sales,
    reservation_payload_changed,
    reservation_window,
)

# Shape of the real mysales page (values invented): hidden exact balance,
# a header row with hidden detail columns, date rows and item rows, and an
# "all products" table repeating the same rows.
SALES_HTML = """
<h1 id="top_page_title" class="h3">Myynnin seuranta</h1>
<h2>Saldo</h2><div id="client_balance_container">&nbsp;</div>
<input type="hidden" id="client_balance_float" value="16.00"/>
<div>Tiedot p&auml;ivitetty: 03.10.2026 12:01</div>
<table><tbody>
<tr><th>Nro</th><th>Nimi</th><th>Hinta</th><th class="detail_view">M&auml;&auml;r&auml;</th>
<th class="detail_view">ALV</th><th class="detail_view">Paikka</th></tr>
<tr><td colspan="6">03.10.2026</td></tr>
<tr><td>55</td><td>Talvisaappaat, 30</td><td>8,00 €</td><td>1</td><td>0 %</td><td>12</td></tr>
<tr><td>7</td><td>Housut,  122</td><td>3,00&nbsp;€</td><td>1</td><td>0 %</td><td>12</td></tr>
<tr><td>70</td><td>Sadehaalari, 116</td><td>5,00 €</td><td>1</td><td>0 %</td><td>12</td></tr>
</tbody></table>
<div id="products_sold_all_container"><table>
<tr><th>Nro</th><th>Nimi</th><th>Hinta</th></tr>
<tr><td>03.10.2026 myydyt tuotteet</td></tr>
<tr><td>55</td><td>Talvisaappaat, 30</td><td>8,00 €</td></tr>
</table></div>
"""

HOURS_TEXT = """
Maanantai: suljettu
Tiistai - Perjantai: 10-18
Lauantai - Sunnuntai: 10-16
"""


class ParserTests(unittest.TestCase):
    def test_reservations_are_deduplicated(self):
        text = """
        La 03.10.2026 - La 10.10.2026
        La 03.10.2026 - La 10.10.2026
        """

        self.assertEqual(
            parse_reservations(text),
            [
                (
                    date(2026, 10, 3),
                    date(2026, 10, 10),
                )
            ],
        )

    def test_opening_hours(self):
        text = """
        Maanantai: suljettu
        Tiistai - Perjantai: 10-18
        Lauantai - Sunnuntai: 10-16
        """

        hours = parse_opening_hours(text)

        self.assertIsNone(hours[0])
        self.assertEqual(hours[1][0].hour, 10)
        self.assertEqual(hours[4][1].hour, 18)
        self.assertEqual(hours[5][1].hour, 16)

    def test_sales_and_balance(self):
        doc = PageDoc(SALES_HTML)

        self.assertEqual(parse_balance(doc), 16.0)
        self.assertEqual(
            parse_data_updated(doc.text),
            datetime(2026, 10, 3, 12, 1, tzinfo=TZ),
        )

        stats = {}
        sales = parse_sales(doc.tables, stats)

        # The "all products" table repeats the rows; duplicates are dropped.
        self.assertEqual(len(sales), 3)
        self.assertEqual(stats, {"tables": 2, "rows": 3, "undated": 0})
        self.assertEqual(sum(item["price"] for item in sales), 16.0)
        # Same keys as the 1.x text parser produced.
        self.assertEqual(
            kk_mqtt.sale_key(sales[0]), "03.10.2026|55|Talvisaappaat, 30|8.00"
        )

    def test_balance_falls_back_to_visible_text(self):
        doc = PageDoc("<h1>Saldo</h1><div>1 234,50 €</div>")
        self.assertEqual(parse_balance(doc), 1234.5)

    def test_page_text_feeds_text_parsers(self):
        doc = PageDoc(
            "<h1>Esimerkkikirppis</h1><script>var x = '01.01.2000 - 02.01.2000';</script>"
            "<table><tr><td>La 03.10.2026</td><td>-</td><td>La 10.10.2026</td></tr></table>"
            "<div>Tiistai - Perjantai: 10-18</div>"
        )
        self.assertEqual(doc.h1, ["Esimerkkikirppis"])
        self.assertEqual(
            parse_reservations(doc.text), [(date(2026, 10, 3), date(2026, 10, 10))]
        )
        self.assertEqual(parse_opening_hours(doc.text)[2], (time(10, 0), time(18, 0)))

    def test_reservation_window(self):
        hours = parse_opening_hours(
            """
            Maanantai: suljettu
            Tiistai - Perjantai: 10-18
            Lauantai - Sunnuntai: 10-16
            """
        )

        start, end = reservation_window(
            (
                date(2026, 10, 3),
                date(2026, 10, 10),
            ),
            hours,
        )

        self.assertEqual(
            start,
            datetime(
                2026,
                10,
                3,
                10,
                0,
                tzinfo=TZ,
            ),
        )

        self.assertEqual(
            end,
            datetime(
                2026,
                10,
                10,
                16,
                0,
                tzinfo=TZ,
            ),
        )

    def test_next_metadata_refresh(self):
        morning = datetime(
            2026,
            10,
            3,
            7,
            30,
            tzinfo=TZ,
        )

        self.assertEqual(
            next_metadata_refresh(morning).hour,
            8,
        )

        afternoon = datetime(
            2026,
            10,
            3,
            12,
            0,
            tzinfo=TZ,
        )

        self.assertEqual(
            next_metadata_refresh(afternoon).hour,
            20,
        )

    def test_opening_hours_dot_times(self):
        hours = parse_opening_hours(
            "Tiistai - Perjantai: 10.00-18.00\n"
            "Lauantai: 10:30–16:00"
        )

        self.assertEqual(hours[1], (time(10, 0), time(18, 0)))
        self.assertEqual(hours[5], (time(10, 30), time(16, 0)))

    def test_opening_hours_abbreviations(self):
        hours = parse_opening_hours(
            "Ma: suljettu\n"
            "Ti-Pe: 10-18\n"
            "La - Su: 10-16\n"
            "Maksutavat: kortti"
        )

        self.assertIsNone(hours[0])
        self.assertEqual(hours[3], (time(10, 0), time(18, 0)))
        self.assertEqual(hours[6], (time(10, 0), time(16, 0)))

    def test_final_read_due(self):
        hours = parse_opening_hours(HOURS_TEXT)
        saturday = date(2026, 10, 3)

        def at(hour, minute):
            return datetime(2026, 10, 3, hour, minute, tzinfo=TZ)

        # Closes 16:00, final read 10 minutes later, once per day.
        self.assertFalse(final_read_due(hours, at(15, 55), None))
        self.assertFalse(final_read_due(hours, at(16, 5), None))
        self.assertTrue(final_read_due(hours, at(16, 12), None))
        self.assertFalse(final_read_due(hours, at(16, 17), saturday))

        monday = datetime(2026, 10, 5, 12, 0, tzinfo=TZ)
        self.assertFalse(final_read_due(hours, monday, None))

    def test_reservation_payload_follows_time(self):
        hours = parse_opening_hours(HOURS_TEXT)
        reservations = [(date(2026, 10, 3), date(2026, 10, 10))]

        before = build_reservation_payload(
            "Kirppis", reservations, hours,
            datetime(2026, 10, 3, 9, 0, tzinfo=TZ),
        )
        during = build_reservation_payload(
            "Kirppis", reservations, hours,
            datetime(2026, 10, 3, 10, 5, tzinfo=TZ),
        )
        later = build_reservation_payload(
            "Kirppis", reservations, hours,
            datetime(2026, 10, 3, 11, 0, tzinfo=TZ),
        )
        after = build_reservation_payload(
            "Kirppis", reservations, hours,
            datetime(2026, 10, 10, 16, 0, tzinfo=TZ),
        )

        self.assertFalse(before["reservation_active"])
        self.assertTrue(during["reservation_active"])
        self.assertTrue(reservation_payload_changed(before, during))
        # Only updated_at differs.
        self.assertFalse(reservation_payload_changed(during, later))
        self.assertTrue(reservation_payload_changed(None, later))

        self.assertFalse(after["reservation_active"])
        self.assertEqual(after["reservation_start"], "")


class SessionTests(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 12, 0, tzinfo=TZ)

    def test_keepalive_due(self):
        with mock.patch.object(kk_mqtt, "KEEPALIVE_MINUTES", 60):
            self.assertFalse(keepalive_due(None, self.NOW))
            self.assertFalse(keepalive_due(self.NOW - timedelta(minutes=59), self.NOW))
            self.assertTrue(keepalive_due(self.NOW - timedelta(minutes=60), self.NOW))

        with mock.patch.object(kk_mqtt, "KEEPALIVE_MINUTES", 0):
            self.assertFalse(keepalive_due(self.NOW - timedelta(days=1), self.NOW))

    def test_relogin_allowed(self):
        def attempt(hours_ago, ok):
            return {"at": (self.NOW - timedelta(hours=hours_ago)).isoformat(), "ok": ok}

        with mock.patch.multiple(
            kk_mqtt, KK_USERNAME="", KK_PASSWORD="", RELOGIN_MIN_INTERVAL_HOURS=6
        ):
            self.assertFalse(relogin_allowed(None, self.NOW))

        with mock.patch.multiple(
            kk_mqtt, KK_USERNAME="u", KK_PASSWORD="p", RELOGIN_MIN_INTERVAL_HOURS=6
        ):
            self.assertTrue(relogin_allowed(None, self.NOW))
            # After a failure: wait the full interval.
            self.assertFalse(relogin_allowed(attempt(5, False), self.NOW))
            self.assertTrue(relogin_allowed(attempt(6, False), self.NOW))
            # After a success: only a short gap.
            self.assertFalse(relogin_allowed(attempt(0.1, True), self.NOW))
            self.assertTrue(relogin_allowed(attempt(0.5, True), self.NOW))

    def test_choose_shop_option(self):
        names = ["Kirppis A", "Kirppis B"]

        self.assertEqual(choose_shop_option(["Ainoa"], ""), 0)
        self.assertEqual(choose_shop_option(names, "kirppis b"), 1)

        with self.assertRaises(LoginFailed):
            choose_shop_option(names, "")
        with self.assertRaises(LoginFailed):
            choose_shop_option(names, "kirppis")
        with self.assertRaises(LoginFailed):
            choose_shop_option(names, "C")


class FakeResponse:
    def __init__(self, url, body):
        self.url, self.body = url, body
        self.headers = mock.Mock(get_content_charset=lambda: "utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url

    def read(self):
        return self.body.encode()


class FakeSite:
    """Kirppari-Kalle login endpoints as seen with a real account."""

    def __init__(self, shops, password="secret"):
        self.shops = shops
        self.password = password
        self.logged_in = False
        self.chosen = None
        self.addheaders = []
        self.calls = []

    def open(self, url, timeout=None):
        parsed = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        self.calls.append(q.get("method") or q.get("page"))
        base = "https://example.invalid/sovellus/"

        if parsed.path.endswith("guest.php"):
            return FakeResponse(url, "<form><input id='username'/></form>Kirjaudu sisään")
        if parsed.path.endswith("rpc.php"):
            if q["method"] == "username_login":
                if q["password"] != self.password:
                    return FakeResponse(url, json.dumps(
                        {"status": "error", "object": "username_or_password"}))
                if len(self.shops) == 1:
                    self.logged_in = True
                    return FakeResponse(url, json.dumps({"status": "success", "redirect": "true"}))
                return FakeResponse(url, json.dumps({
                    "status": "success", "redirect": False, "keylist": [],
                    "companies": {
                        str(i): {"company_id": 100 + i, "name": n}
                        for i, n in enumerate(self.shops)
                    },
                }))
            if q["method"] == "choose_company":
                self.chosen = int(q["company_id"])
                self.logged_in = True
                return FakeResponse(url, json.dumps({"status": "success", "redirect": "true"}))
        if parsed.path.endswith("index.php"):
            if not self.logged_in:
                return FakeResponse(base + "guest.php?page=login", "Kirjaudu sisään")
            return FakeResponse(url, "<a>Kirjaudu ulos</a>")
        raise AssertionError(url)


class LoginFlowTests(unittest.TestCase):
    def client(self, site):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return KirppariKalleClient(Path(tmp.name) / "session.cookies", opener=site)

    def test_single_shop_redirects(self):
        site = FakeSite(["Ainoa"])
        client = self.client(site)
        client.login("user", "secret")
        self.assertIsNone(site.chosen)
        self.assertEqual(site.calls[-1], "myreservations")
        self.assertTrue(client.cookie_file.exists())
        self.assertEqual(client.cookie_file.stat().st_mode & 0o777, 0o600)

    def test_shop_is_chosen_by_name(self):
        site = FakeSite(["Kirppis A", "Kirppis B", "Kirppis C"])
        self.client(site).login("user", "secret", "kirppis b")
        self.assertEqual(site.chosen, 101)

    def test_rejected_login_fails(self):
        site = FakeSite(["Ainoa"])
        with self.assertRaises(LoginFailed) as ctx:
            self.client(site).login("user", "wrong")
        self.assertNotIn("wrong", str(ctx.exception))

    def test_ambiguous_shop_fails(self):
        site = FakeSite(["Kirppis A", "Kirppis B"])
        with self.assertRaises(LoginFailed):
            self.client(site).login("user", "secret")

    def test_expired_session_is_detected(self):
        site = FakeSite(["Ainoa"])
        with self.assertRaises(AuthExpired):
            self.client(site).page("mysales")

    def test_import_storage_state(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / "auth.json"
        state.write_text(json.dumps({"cookies": [{
            "name": "PHPSESSID", "value": "x", "domain": ".kirpparikalle.net",
            "path": "/", "expires": -1, "httpOnly": True, "secure": True,
        }]}))
        client = self.client(FakeSite(["Ainoa"]))
        self.assertFalse(client.has_session())
        self.assertEqual(client.import_storage_state(state), 1)
        self.assertTrue(client.has_session())
        client.save()
        reloaded = KirppariKalleClient(client.cookie_file, opener=FakeSite(["Ainoa"]))
        self.assertTrue(reloaded.has_session())


if __name__ == "__main__":
    unittest.main()
