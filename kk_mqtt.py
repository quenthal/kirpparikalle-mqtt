#!/usr/bin/env python3
"""
Kirppari-Kalle -> MQTT -> Home Assistant monitor.

Version 1.2.1

- Reuses a Playwright storage_state file for authentication.
- Reads reservations and opening hours at startup and around 08:00 / 20:00.
- Reads sales every 5 minutes only while a reservation is active and the shop is open,
  plus one final read after closing time.
- Waits 5 seconds for the animated balance to settle.
- Publishes retained current state and non-retained change events to MQTT.
- Publishes Home Assistant MQTT Discovery entities.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
from playwright.sync_api import Page, sync_playwright


APP_VERSION = "1.2.1"
TZ = ZoneInfo("Europe/Helsinki")

DATA_DIR = Path(os.getenv("KK_DATA_DIR", "/var/lib/kirpparikalle"))
AUTH_STATE_FILE = Path(
    os.getenv("KK_AUTH_STATE_FILE", str(DATA_DIR / "auth.json"))
)
MONITOR_STATE_FILE = Path(
    os.getenv("KK_MONITOR_STATE_FILE", str(DATA_DIR / "monitor_state.json"))
)

RESERVATIONS_URL = (
    "https://www.kirpparikalle.net/sovellus/index.php?page=myreservations"
)
COMPANY_URL = (
    "https://www.kirpparikalle.net/sovellus/index.php?page=companydetails"
)
SALES_URL = (
    "https://www.kirpparikalle.net/sovellus/index.php?page=mysales"
)

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
SALES_SETTLE_MS = int(os.getenv("SALES_SETTLE_MS", "5000"))
FINAL_READ_DELAY_MINUTES = int(os.getenv("FINAL_READ_DELAY_MINUTES", "10"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "6"))

MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "mqtt")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "kirpparikalle-monitor")

MQTT_STATE_TOPIC = os.getenv("MQTT_STATE_TOPIC", "kirpparikalle/state")
MQTT_UPDATE_TOPIC = os.getenv("MQTT_UPDATE_TOPIC", "kirpparikalle/update")
MQTT_STATUS_TOPIC = os.getenv("MQTT_STATUS_TOPIC", "kirpparikalle/status")
MQTT_RESERVATION_TOPIC = os.getenv(
    "MQTT_RESERVATION_TOPIC", "kirpparikalle/reservation"
)
MQTT_AUTH_TOPIC = os.getenv("MQTT_AUTH_TOPIC", "kirpparikalle/auth")

HA_DISCOVERY_PREFIX = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")
HA_DEVICE_ID = os.getenv("HA_DEVICE_ID", "kirpparikalle_monitor")

METADATA_REFRESH_HOURS = (8, 20)

WEEKDAYS = {
    "maanantai": 0,
    "tiistai": 1,
    "keskiviikko": 2,
    "torstai": 3,
    "perjantai": 4,
    "lauantai": 5,
    "sunnuntai": 6,
    "ma": 0,
    "ti": 1,
    "ke": 2,
    "to": 3,
    "pe": 4,
    "la": 5,
    "su": 6,
}

# Longest names first so that "maanantai" is not matched as "ma".
WEEKDAY_PATTERN = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

STOP_EVENT = threading.Event()


class AuthExpired(RuntimeError):
    """Kirppari-Kalle session is no longer authenticated."""


def handle_stop_signal(signum, frame):
    STOP_EVENT.set()


def parse_reservations(text: str) -> list[tuple[date, date]]:
    reservations: list[tuple[date, date]] = []

    pattern = re.compile(
        r"(\d{1,2}\.\d{1,2}\.\d{4})"
        r".{0,20}?"
        r"[-–]"
        r".{0,20}?"
        r"(\d{1,2}\.\d{1,2}\.\d{4})"
    )

    for match in pattern.finditer(text):
        start = datetime.strptime(match.group(1), "%d.%m.%Y").date()
        end = datetime.strptime(match.group(2), "%d.%m.%Y").date()
        reservations.append((start, end))

    # Kirppari-Kalle may render the same reservation multiple times.
    return list(dict.fromkeys(reservations))


def parse_clock(value: str) -> dt_time:
    value = value.strip().replace(".", ":")
    if ":" in value:
        return datetime.strptime(value, "%H:%M").time()
    return dt_time(int(value), 0)


def weekday_range(start: int, end: int) -> list[int]:
    days: list[int] = []
    current = start

    while True:
        days.append(current)
        if current == end:
            break
        current = (current + 1) % 7

    return days


def parse_opening_hours(text: str) -> dict[int, tuple[dt_time, dt_time] | None]:
    result: dict[int, tuple[dt_time, dt_time] | None] = {
        i: None for i in range(7)
    }

    for raw_line in text.splitlines():
        line = raw_line.strip().lower()

        if ":" not in line:
            continue

        match = re.match(
            rf"^({WEEKDAY_PATTERN})\b"
            rf"(?:\s*[-–]\s*({WEEKDAY_PATTERN})\b)?"
            r"\s*:\s*(.+)$",
            line,
        )

        if not match:
            continue

        start_day = WEEKDAYS[match.group(1)]
        end_day = WEEKDAYS[match.group(2)] if match.group(2) else start_day
        value = match.group(3).strip()
        days = weekday_range(start_day, end_day)

        if "suljettu" in value:
            for day in days:
                result[day] = None
            continue

        time_match = re.search(
            r"(\d{1,2}(?:[:.]\d{2})?)"
            r"\s*[-–]\s*"
            r"(\d{1,2}(?:[:.]\d{2})?)",
            value,
        )

        if not time_match:
            continue

        opening = parse_clock(time_match.group(1))
        closing = parse_clock(time_match.group(2))

        for day in days:
            result[day] = (opening, closing)

    return result


def find_active_reservation(
    reservations: list[tuple[date, date]],
    today: date,
) -> tuple[date, date] | None:
    for start, end in reservations:
        if start <= today <= end:
            return start, end
    return None


def reservation_window(
    reservation: tuple[date, date] | None,
    opening_hours: dict[int, tuple[dt_time, dt_time] | None],
) -> tuple[datetime | None, datetime | None]:
    """
    Convert reservation dates into the actual first opening time
    and last closing time inside the reservation.
    """
    if reservation is None:
        return None, None

    start_date, end_date = reservation

    current = start_date
    start_dt = None

    while current <= end_date:
        hours = opening_hours.get(current.weekday())
        if hours is not None:
            opening, _ = hours
            start_dt = datetime.combine(current, opening, tzinfo=TZ)
            break
        current += timedelta(days=1)

    current = end_date
    end_dt = None

    while current >= start_date:
        hours = opening_hours.get(current.weekday())
        if hours is not None:
            _, closing = hours
            end_dt = datetime.combine(current, closing, tzinfo=TZ)
            break
        current -= timedelta(days=1)

    return start_dt, end_dt


def relevant_reservation(
    reservations: list[tuple[date, date]],
    opening_hours: dict[int, tuple[dt_time, dt_time] | None],
    now: datetime,
) -> tuple[date, date] | None:
    """
    Return the current reservation while its real sales window has not ended.
    Otherwise return the next future reservation.
    """
    candidates = sorted(reservations, key=lambda item: item[0])

    for reservation in candidates:
        _, end_dt = reservation_window(reservation, opening_hours)
        if end_dt is not None and now < end_dt:
            return reservation

    return None


def is_open(
    opening_hours: dict[int, tuple[dt_time, dt_time] | None],
    now: datetime,
) -> bool:
    hours = opening_hours.get(now.weekday())
    if hours is None:
        return False

    opening, closing = hours
    current = now.time().replace(second=0, microsecond=0)

    return opening <= current < closing


def final_read_due(
    opening_hours: dict[int, tuple[dt_time, dt_time] | None],
    now: datetime,
    done_for: date | None,
) -> bool:
    """
    True once per open day after closing time (+ delay), so that sales made
    during the last poll interval before closing are not missed.
    """
    hours = opening_hours.get(now.weekday())
    if hours is None or done_for == now.date():
        return False

    _, closing = hours
    closing_dt = datetime.combine(now.date(), closing, tzinfo=TZ)

    return now >= closing_dt + timedelta(minutes=FINAL_READ_DELAY_MINUTES)


def next_metadata_refresh(now: datetime) -> datetime:
    """Return the next local metadata refresh time: 08:00 or 20:00."""
    for hour in METADATA_REFRESH_HOURS:
        candidate = now.replace(
            hour=hour,
            minute=0,
            second=0,
            microsecond=0,
        )
        if now < candidate:
            return candidate

    return (now + timedelta(days=1)).replace(
        hour=METADATA_REFRESH_HOURS[0],
        minute=0,
        second=0,
        microsecond=0,
    )


def page_is_login(page: Page, text: str) -> bool:
    lower = text.lower()

    return (
        "page=login" in page.url.lower()
        or (
            "kirjaudu sisään" in lower
            and "kirjaudu ulos" not in lower
        )
    )


def get_page_text(page: Page, url: str) -> str:
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    text = page.locator("body").inner_text()

    if page_is_login(page, text):
        raise AuthExpired(
            "Kirppari-Kalle-kirjautuminen on vanhentunut. "
            "Luo uusi auth.json."
        )

    return text


def parse_balance(text: str) -> float:
    match = re.search(
        r"Saldo\s+([0-9\s]+(?:[,.][0-9]{1,2})?)\s*€",
        text,
        re.IGNORECASE,
    )

    if not match:
        raise RuntimeError("Saldoa ei löytynyt mysales-sivulta.")

    value = (
        match.group(1)
        .replace("\xa0", "")
        .replace(" ", "")
        .replace(",", ".")
    )

    return round(float(value), 2)


def parse_sales(text: str) -> list[dict]:
    sales: list[dict] = []
    current_date: str | None = None
    in_table = False

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())

        if (
            line.lower().startswith("nro")
            and "nimi" in line.lower()
            and "hinta" in line.lower()
        ):
            in_table = True
            continue

        if not in_table:
            continue

        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", line):
            current_date = line
            continue

        match = re.match(
            r"^(\d+)\s+(.+?)\s+(\d+[,.]\d{2})\s*€$",
            line,
        )

        if match and current_date:
            sales.append(
                {
                    "date": current_date,
                    "number": int(match.group(1)),
                    "name": match.group(2).strip(),
                    "price": round(
                        float(match.group(3).replace(",", ".")),
                        2,
                    ),
                }
            )

    return sales


def sale_key(sale: dict) -> str:
    return (
        f"{sale['date']}|{sale['number']}|"
        f"{sale['name']}|{sale['price']:.2f}"
    )


def load_monitor_state() -> dict | None:
    if not MONITOR_STATE_FILE.exists():
        return None

    try:
        return json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Vanhan monitorointitilan luku epäonnistui")
        return None


def save_monitor_state(
    shop: str,
    reservation: tuple[date, date],
    balance: float,
    sales: list[dict],
    checked_at: datetime,
) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "shop": shop,
        "reservation": {
            "start": reservation[0].isoformat(),
            "end": reservation[1].isoformat(),
        },
        "balance": balance,
        "sales": sales,
        "checked_at": checked_at.isoformat(),
    }

    temp_file = MONITOR_STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temp_file, MONITOR_STATE_FILE)


def reservation_id(reservation: tuple[date, date]) -> str:
    return f"{reservation[0].isoformat()}/{reservation[1].isoformat()}"


def previous_reservation_id(previous: dict) -> str | None:
    reservation = previous.get("reservation") or {}
    start = reservation.get("start")
    end = reservation.get("end")

    if not start or not end:
        return None

    return f"{start}/{end}"


def mqtt_publish_json(
    client: mqtt.Client,
    topic: str,
    payload: dict,
    retain: bool,
) -> None:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    info = client.publish(
        topic,
        data,
        qos=1,
        retain=retain,
    )

    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish epäonnistui, rc={info.rc}")

    info.wait_for_publish(timeout=10)

    if not info.is_published():
        raise RuntimeError(
            f"MQTT-viestin julkaisu aikakatkaistiin: {topic}"
        )


def ha_device() -> dict:
    return {
        "identifiers": [HA_DEVICE_ID],
        "name": "Kirppari-Kalle",
        "manufacturer": "Kirppari-Kalle",
        "model": "Myynnin seuranta",
    }


def publish_home_assistant_discovery(client: mqtt.Client) -> None:
    origin = {
        "name": "kirpparikalle-mqtt",
        "sw_version": APP_VERSION,
    }

    common = {
        "availability_topic": MQTT_STATUS_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": ha_device(),
        "origin": origin,
    }

    discoveries = {
        f"{HA_DISCOVERY_PREFIX}/sensor/{HA_DEVICE_ID}/balance/config": {
            **common,
            "name": "Saldo",
            "unique_id": "kirpparikalle_balance",
            "state_topic": MQTT_STATE_TOPIC,
            "value_template": "{{ value_json.balance }}",
            "unit_of_measurement": "EUR",
            "device_class": "monetary",
            "suggested_display_precision": 2,
        },
        f"{HA_DISCOVERY_PREFIX}/sensor/{HA_DEVICE_ID}/sold_count/config": {
            **common,
            "name": "Myytyjä tuotteita",
            "unique_id": "kirpparikalle_sold_count",
            "state_topic": MQTT_STATE_TOPIC,
            "value_template": "{{ value_json.sold_count }}",
            "icon": "mdi:counter",
        },
        f"{HA_DISCOVERY_PREFIX}/sensor/{HA_DEVICE_ID}/checked_at/config": {
            **common,
            "name": "Tarkistettu",
            "unique_id": "kirpparikalle_checked_at",
            "state_topic": MQTT_STATE_TOPIC,
            "value_template": "{{ value_json.checked_at }}",
            "device_class": "timestamp",
            "entity_category": "diagnostic",
        },
        f"{HA_DISCOVERY_PREFIX}/sensor/{HA_DEVICE_ID}/reservation_start/config": {
            **common,
            "name": "Myynti alkaa",
            "unique_id": "kirpparikalle_reservation_start",
            "state_topic": MQTT_RESERVATION_TOPIC,
            "value_template": "{{ value_json.reservation_start | default('', true) }}",
            "device_class": "timestamp",
            "icon": "mdi:calendar-start",
        },
        f"{HA_DISCOVERY_PREFIX}/sensor/{HA_DEVICE_ID}/reservation_end/config": {
            **common,
            "name": "Myynti päättyy",
            "unique_id": "kirpparikalle_reservation_end",
            "state_topic": MQTT_RESERVATION_TOPIC,
            "value_template": "{{ value_json.reservation_end | default('', true) }}",
            "device_class": "timestamp",
            "icon": "mdi:calendar-end",
        },
        f"{HA_DISCOVERY_PREFIX}/event/{HA_DEVICE_ID}/sales_update/config": {
            **common,
            "name": "Myyntimuutos",
            "unique_id": "kirpparikalle_sales_update",
            "state_topic": MQTT_UPDATE_TOPIC,
            "event_types": ["sales_update"],
        },
        # No availability: the service exits when the login has expired,
        # and the problem must stay visible while it is offline.
        f"{HA_DISCOVERY_PREFIX}/binary_sensor/{HA_DEVICE_ID}/auth/config": {
            "device": ha_device(),
            "origin": origin,
            "name": "Kirjautuminen vanhentunut",
            "unique_id": "kirpparikalle_auth_expired",
            "state_topic": MQTT_AUTH_TOPIC,
            "payload_on": "expired",
            "payload_off": "ok",
            "device_class": "problem",
            "entity_category": "diagnostic",
        },
    }

    for topic, payload in discoveries.items():
        mqtt_publish_json(client, topic, payload, retain=True)

    logging.info(
        "Home Assistant MQTT Discovery julkaistu (%d entiteettiä)",
        len(discoveries),
    )


def build_state_payload(
    shop: str,
    reservation: tuple[date, date],
    balance: float,
    sales: list[dict],
    checked_at: datetime,
) -> dict:
    return {
        "shop": shop,
        "balance": balance,
        "sold_count": len(sales),
        "reservation": {
            "start": reservation[0].isoformat(),
            "end": reservation[1].isoformat(),
        },
        "checked_at": checked_at.isoformat(),
    }


def republish_saved_state(client: mqtt.Client) -> None:
    """
    Restore retained state immediately after a restart even if the shop is closed.
    """
    previous = load_monitor_state()
    if not previous:
        return

    reservation = previous.get("reservation") or {}
    sales = previous.get("sales") or []

    payload = {
        "shop": previous.get("shop", "Kirppari-Kalle"),
        "balance": previous.get("balance"),
        "sold_count": len(sales),
        "reservation": {
            "start": reservation.get("start"),
            "end": reservation.get("end"),
        },
        "checked_at": previous.get("checked_at"),
    }

    mqtt_publish_json(
        client,
        MQTT_STATE_TOPIC,
        payload,
        retain=True,
    )

    logging.info("Tallennettu MQTT state julkaistu uudelleen")


def build_reservation_payload(
    shop: str,
    reservations: list[tuple[date, date]],
    opening_hours: dict[int, tuple[dt_time, dt_time] | None],
    now: datetime,
) -> dict:
    reservation = relevant_reservation(
        reservations,
        opening_hours,
        now,
    )

    start_dt, end_dt = reservation_window(
        reservation,
        opening_hours,
    )

    period_active = bool(
        start_dt
        and end_dt
        and start_dt <= now < end_dt
    )

    return {
        "shop": shop,
        "reservation_start": start_dt.isoformat() if start_dt else "",
        "reservation_end": end_dt.isoformat() if end_dt else "",
        "reservation_active": period_active,
        "updated_at": now.isoformat(),
    }


def reservation_payload_changed(old: dict | None, new: dict) -> bool:
    """Compare reservation payloads ignoring the updated_at timestamp."""
    if old is None:
        return True

    return {k: v for k, v in old.items() if k != "updated_at"} != {
        k: v for k, v in new.items() if k != "updated_at"
    }


def publish_auth_state(client: mqtt.Client, state: str) -> None:
    info = client.publish(MQTT_AUTH_TOPIC, state, qos=1, retain=True)
    info.wait_for_publish(timeout=10)


def make_mqtt_client() -> mqtt.Client:
    connected = threading.Event()
    failure: list[str] = []

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
    )

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    client.will_set(
        MQTT_STATUS_TOPIC,
        payload="offline",
        qos=1,
        retain=True,
    )

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logging.info("MQTT connected")

            client.publish(
                MQTT_STATUS_TOPIC,
                "online",
                qos=1,
                retain=True,
            )

            connected.set()
        else:
            failure.append(str(reason_code))
            connected.set()
            logging.error(
                "MQTT-yhteys epäonnistui: %s",
                reason_code,
            )

    client.on_connect = on_connect
    client.reconnect_delay_set(
        min_delay=1,
        max_delay=60,
    )

    client.connect(
        MQTT_HOST,
        MQTT_PORT,
        keepalive=60,
    )

    client.loop_start()

    if not connected.wait(timeout=15):
        client.loop_stop()
        raise RuntimeError(
            "MQTT-yhteyden muodostus aikakatkaistiin"
        )

    if failure:
        client.loop_stop()
        raise RuntimeError(
            f"MQTT-yhteys epäonnistui: {failure[-1]}"
        )

    return client


def refresh_metadata(
    page: Page,
) -> tuple[
    str,
    list[tuple[date, date]],
    dict[int, tuple[dt_time, dt_time] | None],
]:
    reservation_text = get_page_text(
        page,
        RESERVATIONS_URL,
    )

    reservations = parse_reservations(
        reservation_text
    )

    if not reservations:
        logging.warning(
            "Varauksia ei löytynyt myreservations-sivulta"
        )

    company_text = get_page_text(
        page,
        COMPANY_URL,
    )

    opening_hours = parse_opening_hours(
        company_text
    )

    if not any(opening_hours.values()):
        logging.warning(
            "Aukioloaikoja ei löytynyt companydetails-sivulta; "
            "myyntejä ei lueta"
        )

    shop = "Kirppari-Kalle"

    headings = page.locator("h1")

    if headings.count() > 0:
        candidate = headings.first.inner_text().strip()

        if candidate:
            shop = candidate

    return shop, reservations, opening_hours


def read_sales(page: Page) -> tuple[float, list[dict]]:
    page.goto(
        SALES_URL,
        wait_until="domcontentloaded",
        timeout=30000,
    )

    # Kirppari-Kalle animates the balance after page load.
    page.wait_for_timeout(SALES_SETTLE_MS)

    text = page.locator("body").inner_text()

    if page_is_login(page, text):
        raise AuthExpired(
            "Kirppari-Kalle-kirjautuminen on vanhentunut. "
            "Luo uusi auth.json."
        )

    return parse_balance(text), parse_sales(text)


def process_sales_change(
    mqtt_client: mqtt.Client,
    shop: str,
    reservation: tuple[date, date],
    balance: float,
    sales: list[dict],
    checked_at: datetime,
) -> None:
    previous = load_monitor_state()

    state_payload = build_state_payload(
        shop,
        reservation,
        balance,
        sales,
        checked_at,
    )

    baseline_changed = (
        previous is None
        or previous.get("shop") != shop
        or previous_reservation_id(previous)
        != reservation_id(reservation)
    )

    if baseline_changed:
        mqtt_publish_json(
            mqtt_client,
            MQTT_STATE_TOPIC,
            state_payload,
            retain=True,
        )

        save_monitor_state(
            shop,
            reservation,
            balance,
            sales,
            checked_at,
        )

        logging.info(
            "Baseline saved: balance %.2f EUR, %d sales",
            balance,
            len(sales),
        )

        return

    old_balance = round(
        float(previous.get("balance", 0)),
        2,
    )

    old_sales = previous.get("sales", [])

    old_map = {
        sale_key(sale): sale
        for sale in old_sales
    }

    current_map = {
        sale_key(sale): sale
        for sale in sales
    }

    new_sales = [
        sale
        for sale in sales
        if sale_key(sale) not in old_map
    ]

    removed_sales = [
        sale
        for sale in old_sales
        if sale_key(sale) not in current_map
    ]

    balance_changed = balance != old_balance
    sales_changed = bool(
        new_sales or removed_sales
    )

    # Always keep the current state retained on the broker.
    mqtt_publish_json(
        mqtt_client,
        MQTT_STATE_TOPIC,
        state_payload,
        retain=True,
    )

    if not balance_changed and not sales_changed:
        logging.info(
            "No changes: balance %.2f EUR, %d sales",
            balance,
            len(sales),
        )

        return

    update_payload = {
        "event_type": "sales_update",
        "shop": shop,
        "observed_at": checked_at.isoformat(),
        "previous_balance": old_balance,
        "balance": balance,
        "balance_change": round(
            balance - old_balance,
            2,
        ),
        "new_sales": new_sales,
        "removed_sales": removed_sales,
    }

    # Change events must never be retained.
    mqtt_publish_json(
        mqtt_client,
        MQTT_UPDATE_TOPIC,
        update_payload,
        retain=False,
    )

    # Persist only after successful MQTT publishing.
    save_monitor_state(
        shop,
        reservation,
        balance,
        sales,
        checked_at,
    )

    logging.info(
        "CHANGE: %.2f -> %.2f EUR, new %d, removed %d",
        old_balance,
        balance,
        len(new_sales),
        len(removed_sales),
    )

    for sale in new_sales:
        logging.info(
            "NEW SALE: %s | %s | %.2f EUR",
            sale["date"],
            sale["name"],
            sale["price"],
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    signal.signal(
        signal.SIGTERM,
        handle_stop_signal,
    )

    signal.signal(
        signal.SIGINT,
        handle_stop_signal,
    )

    if not MQTT_HOST:
        logging.error("MQTT_HOST puuttuu asetuksista")

        return 1

    if not AUTH_STATE_FILE.exists():
        logging.error(
            "Kirjautumistiedostoa ei löydy: %s",
            AUTH_STATE_FILE,
        )

        return 1

    mqtt_client: mqtt.Client | None = None

    try:
        mqtt_client = make_mqtt_client()

        publish_home_assistant_discovery(
            mqtt_client
        )

        republish_saved_state(
            mqtt_client
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
            )

            context = browser.new_context(
                storage_state=str(AUTH_STATE_FILE),
                timezone_id="Europe/Helsinki",
            )

            page = context.new_page()

            shop: str | None = None
            reservations: list[
                tuple[date, date]
            ] = []

            opening_hours: dict[
                int,
                tuple[dt_time, dt_time] | None,
            ] = {}

            next_meta_refresh: datetime | None = None
            reservation_payload: dict | None = None
            auth_state: str | None = None
            final_read_done_for: date | None = None
            consecutive_failures = 0

            while not STOP_EVENT.is_set():
                try:
                    now = datetime.now(TZ)

                    if (
                        shop is None
                        or next_meta_refresh is None
                        or now >= next_meta_refresh
                    ):
                        (
                            shop,
                            reservations,
                            opening_hours,
                        ) = refresh_metadata(page)

                        context.storage_state(
                            path=str(AUTH_STATE_FILE)
                        )

                        if auth_state != "ok":
                            publish_auth_state(mqtt_client, "ok")
                            auth_state = "ok"

                        # Force a publish so updated_at stays fresh.
                        reservation_payload = None

                        next_meta_refresh = (
                            next_metadata_refresh(now)
                        )

                        logging.info(
                            "Metadata refreshed: %s, "
                            "reservations %d. "
                            "Next refresh: %s",
                            shop,
                            len(reservations),
                            next_meta_refresh.strftime(
                                "%d.%m.%Y %H:%M"
                            ),
                        )

                    new_reservation_payload = build_reservation_payload(
                        shop,
                        reservations,
                        opening_hours,
                        now,
                    )

                    if reservation_payload_changed(
                        reservation_payload,
                        new_reservation_payload,
                    ):
                        mqtt_publish_json(
                            mqtt_client,
                            MQTT_RESERVATION_TOPIC,
                            new_reservation_payload,
                            retain=True,
                        )
                        reservation_payload = new_reservation_payload

                    active = find_active_reservation(
                        reservations,
                        now.date(),
                    )

                    open_now = is_open(
                        opening_hours,
                        now,
                    )

                    final_read = bool(
                        active
                        and not open_now
                        and final_read_due(
                            opening_hours,
                            now,
                            final_read_done_for,
                        )
                    )

                    if active and (open_now or final_read):
                        logging.info(
                            "Active reservation and %s -> reading sales",
                            "shop open" if open_now else "final read after closing",
                        )

                        balance, sales = read_sales(
                            page
                        )

                        context.storage_state(
                            path=str(AUTH_STATE_FILE)
                        )

                        process_sales_change(
                            mqtt_client,
                            shop or "Kirppari-Kalle",
                            active,
                            balance,
                            sales,
                            now,
                        )

                        if final_read:
                            final_read_done_for = now.date()

                    else:
                        logging.info(
                            "No sales read: "
                            "reservation=%s, open=%s",
                            "yes" if active else "no",
                            "yes" if open_now else "no",
                        )

                    consecutive_failures = 0

                except AuthExpired as exc:
                    logging.error("%s", exc)

                    try:
                        publish_auth_state(mqtt_client, "expired")
                    except Exception:
                        logging.exception("Auth-tilan julkaisu epäonnistui")

                    return 2

                except Exception:
                    consecutive_failures += 1

                    logging.exception(
                        "Monitoring cycle failed (%d/%d)",
                        consecutive_failures,
                        MAX_CONSECUTIVE_FAILURES,
                    )

                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logging.error(
                            "Liian monta peräkkäistä virhettä, "
                            "lopetetaan (systemd käynnistää uudelleen)"
                        )

                        return 1

                STOP_EVENT.wait(POLL_SECONDS)

            logging.info("Stop requested")

            try:
                context.storage_state(
                    path=str(AUTH_STATE_FILE)
                )
            except Exception:
                pass

            browser.close()

    except Exception:
        logging.exception(
            "Program stopped with an error"
        )

        return 1

    finally:
        if mqtt_client:
            try:
                info = mqtt_client.publish(
                    MQTT_STATUS_TOPIC,
                    "offline",
                    qos=1,
                    retain=True,
                )

                info.wait_for_publish(
                    timeout=5
                )

            except Exception:
                pass

            try:
                mqtt_client.disconnect()
                mqtt_client.loop_stop()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
