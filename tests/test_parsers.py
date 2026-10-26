import unittest
from unittest import mock
from datetime import date, datetime, time, timedelta

import kk_mqtt
from kk_mqtt import (
    TZ,
    LoginFailed,
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
        text = """
        Saldo
        16,00 €
        Nro     Nimi    Hinta
        03.10.2026
        55      Talvisaappaat, 30       8,00 €
        7       Housut, 122     3,00 €
        70      Sadehaalari, 116        5,00 €
        """

        self.assertEqual(
            parse_balance(text),
            16.0,
        )

        sales = parse_sales(text)

        self.assertEqual(
            len(sales),
            3,
        )

        self.assertEqual(
            sum(item["price"] for item in sales),
            16.0,
        )

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
        with mock.patch.multiple(
            kk_mqtt, KK_USERNAME="", KK_PASSWORD="", RELOGIN_MIN_INTERVAL_HOURS=6
        ):
            self.assertFalse(relogin_allowed(None, self.NOW))

        with mock.patch.multiple(
            kk_mqtt, KK_USERNAME="u", KK_PASSWORD="p", RELOGIN_MIN_INTERVAL_HOURS=6
        ):
            self.assertTrue(relogin_allowed(None, self.NOW))
            self.assertFalse(relogin_allowed(self.NOW - timedelta(hours=5), self.NOW))
            self.assertTrue(relogin_allowed(self.NOW - timedelta(hours=6), self.NOW))

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


class FakeOptions:
    def __init__(self, page):
        self.page = page

    def count(self):
        return len(self.page.shops) if self.page.show_shops else 0

    def nth(self, i):
        page = self.page

        class Option:
            def inner_text(self):
                return page.shops[i]

            def click(self):
                page.clicked.append(page.shops[i])
                page.url = "https://example.invalid/sovellus/index.php"

        return Option()


class FakeLoginPage:
    """Mimics the login page: form -> (shop list ->) index.php."""

    def __init__(self, shops, accept=True):
        self.url = ""
        self.shops = shops
        self.accept = accept
        self.show_shops = False
        self.filled = {}
        self.clicked = []

    def goto(self, url, **kwargs):
        self.url = "https://example.invalid/sovellus/guest.php?page=login"

    def fill(self, selector, value):
        self.filled[selector] = value

    def click(self, selector):
        if not self.accept:
            return
        if len(self.shops) > 1:
            self.show_shops = True
        else:
            self.url = "https://example.invalid/sovellus/index.php"

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        assert selector == ".login_company_option"
        return FakeOptions(self)


class LoginFlowTests(unittest.TestCase):
    def run_login(self, page, shop=""):
        with mock.patch.multiple(
            kk_mqtt, KK_USERNAME="user", KK_PASSWORD="secret", KK_SHOP=shop
        ), mock.patch.object(kk_mqtt, "get_page_text") as verify:
            kk_mqtt.login(page)
        verify.assert_called_once()

    def test_single_shop_redirects(self):
        page = FakeLoginPage(["Ainoa"])
        self.run_login(page)
        self.assertEqual(page.filled, {"#username": "user", "#password": "secret"})
        self.assertIn("index.php", page.url)

    def test_shop_is_chosen_by_name(self):
        page = FakeLoginPage(["Kirppis A", "Kirppis B"])
        self.run_login(page, shop="kirppis b")
        self.assertEqual(page.clicked, ["Kirppis B"])

    def test_rejected_login_fails(self):
        page = FakeLoginPage(["Ainoa"], accept=False)
        with self.assertRaises(LoginFailed):
            self.run_login(page)

    def test_ambiguous_shop_fails(self):
        page = FakeLoginPage(["Kirppis A", "Kirppis B"])
        with self.assertRaises(LoginFailed):
            self.run_login(page)


if __name__ == "__main__":
    unittest.main()
