import unittest
from datetime import date, datetime

from kk_mqtt import (
    TZ,
    next_metadata_refresh,
    parse_balance,
    parse_opening_hours,
    parse_reservations,
    parse_sales,
    reservation_window,
)


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
        55      Nokian Light talvikumpparit, 30 8,00 €
        7       Vihreät housut, 122     3,00 €
        70      Jonathan sadehaalari, 116       5,00 €
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


if __name__ == "__main__":
    unittest.main()
