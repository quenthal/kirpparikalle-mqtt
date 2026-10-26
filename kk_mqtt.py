#!/usr/bin/env python3
"""
Kirppari-Kalle -> MQTT -> Home Assistant monitor.

Version 2.0.0

- Plain HTTP (standard library), no browser. Logs in with KK_USERNAME /
  KK_PASSWORD and keeps the session cookie in a file.
- Reads reservations and opening hours at startup and around 08:00 / 20:00.
- Reads sales every 5 minutes only while a reservation is active and the shop is open,
  plus one final read after closing time.
- Keeps the session alive with a light page load (default every 60 minutes).
- Logs in again automatically when the session has expired.
- Reads the exact balance from the page's hidden field (no animation wait).
- Publishes retained current state and non-retained change events to MQTT.
- Publishes Home Assistant MQTT Discovery entities.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import re
import signal
import sys
import threading
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt


APP_VERSION = "2.0.0"
TZ = ZoneInfo("Europe/Helsinki")

DATA_DIR = Path(os.getenv("KK_DATA_DIR", "/var/lib/kirpparikalle"))
AUTH_STATE_FILE = Path(
    os.getenv("KK_AUTH_STATE_FILE", str(DATA_DIR / "auth.json"))
)
MONITOR_STATE_FILE = Path(
    os.getenv("KK_MONITOR_STATE_FILE", str(DATA_DIR / "monitor_state.json"))
)
SESSION_FILE = Path(
    os.getenv("KK_SESSION_FILE", str(DATA_DIR / "session.cookies"))
)
RELOGIN_STATE_FILE = DATA_DIR / "last_login_attempt.json"

BASE_URL = "https://www.kirpparikalle.net/sovellus/"
USER_AGENT = "Mozilla/5.0 (compatible; kirpparikalle-mqtt)"
HTTP_TIMEOUT = 30

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))
FINAL_READ_DELAY_MINUTES = int(os.getenv("FINAL_READ_DELAY_MINUTES", "10"))
MAX_CONSECUTIVE_FAILURES = int(os.getenv("MAX_CONSECUTIVE_FAILURES", "6"))
KEEPALIVE_MINUTES = int(os.getenv("KEEPALIVE_MINUTES", "60"))

# Kirppari-Kalle credentials. Without them only an imported auth.json
# session is used, and its expiry just raises the auth problem sensor.
KK_USERNAME = os.getenv("KK_USERNAME", "")
KK_PASSWORD = os.getenv("KK_PASSWORD", "")
KK_SHOP = os.getenv("KK_SHOP", "")
# Wait this long after a FAILED login before trying again (account lockout).
RELOGIN_MIN_INTERVAL_HOURS = float(os.getenv("RELOGIN_MIN_INTERVAL_HOURS", "6"))
# Minimum gap between logins even after a successful one.
RELOGIN_MIN_GAP = timedelta(minutes=10)

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


class LoginFailed(RuntimeError):
    """Automatic login did not succeed."""


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


def keepalive_due(last_activity: datetime | None, now: datetime) -> bool:
    if KEEPALIVE_MINUTES <= 0 or last_activity is None:
        return False
    return now - last_activity >= timedelta(minutes=KEEPALIVE_MINUTES)


def relogin_allowed(last_attempt: dict | None, now: datetime) -> bool:
    """
    Credentials are set and the previous attempt is old enough:
    RELOGIN_MIN_INTERVAL_HOURS after a failure, RELOGIN_MIN_GAP after success.
    """
    if not (KK_USERNAME and KK_PASSWORD):
        return False
    if not last_attempt:
        return True
    try:
        at = datetime.fromisoformat(last_attempt["at"])
    except (KeyError, TypeError, ValueError):
        return True
    if last_attempt.get("ok"):
        return now - at >= RELOGIN_MIN_GAP
    return now - at >= timedelta(hours=RELOGIN_MIN_INTERVAL_HOURS)


def choose_shop_option(names: list[str], wanted: str) -> int:
    """Index of the shop to pick on the login page's shop list."""
    if wanted:
        matches = [
            i for i, name in enumerate(names)
            if wanted.casefold() in name.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        raise LoginFailed(
            f"KK_SHOP vastaa {len(matches)} kirpputoria ({len(names)} vaihtoehtoa)"
        )
    if len(names) == 1:
        return 0
    raise LoginFailed(
        f"Tunnuksella on {len(names)} kirpputoria; aseta KK_SHOP"
    )


def load_last_login_attempt() -> dict | None:
    try:
        return json.loads(RELOGIN_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_last_login_attempt(when: datetime, ok: bool) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RELOGIN_STATE_FILE.write_text(
        json.dumps({"at": when.isoformat(), "ok": ok}), encoding="utf-8"
    )


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


class PageDoc(HTMLParser):
    """
    Minimal HTML reader: inner_text-like text (scripts/styles skipped,
    block elements on their own lines), tables as rows of cell texts,
    <input> values by id and <h1> texts.
    """

    BLOCK = {
        "tr", "div", "p", "br", "li", "table", "section",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0
        self._cell: list[str] | None = None
        self._row: list[str] | None = None
        self._h1: list[str] | None = None
        self.tables: list[list[list[str]]] = []
        self.inputs: dict[str, str] = {}
        self.h1: list[str] = []
        self.feed(html)
        self.close()
        self.text = "".join(self._parts)

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "input":
            a = dict(attrs)
            if a.get("id"):
                self.inputs[a["id"]] = a.get("value") or ""
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._parts.append("\t")
        if tag == "h1":
            self._h1 = []
        if tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag in ("td", "th") and self._cell is not None:
            if self._row is not None:
                self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self.tables:
                self.tables[-1].append(self._row)
            self._row = None
        if tag == "h1" and self._h1 is not None:
            self.h1.append(" ".join("".join(self._h1).split()))
            self._h1 = None
        if tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self._parts.append(data)
        if self._cell is not None:
            self._cell.append(data)
        if self._h1 is not None:
            self._h1.append(data)


def is_login_page(final_url: str, text: str) -> bool:
    lower = text.lower()

    return (
        "guest.php" in final_url
        or "page=login" in final_url.lower()
        or (
            "kirjaudu sisään" in lower
            and "kirjaudu ulos" not in lower
        )
    )


class KirppariKalleClient:
    """Kirppari-Kalle web UI over plain HTTP with a persistent cookie jar."""

    def __init__(self, cookie_file: Path, opener=None):
        self.cookie_file = cookie_file
        self.jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
        if cookie_file.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError):
                logging.warning("Istuntotiedoston luku epäonnistui; aloitetaan tyhjästä")
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.opener.addheaders = [("User-Agent", USER_AGENT)]

    def has_session(self) -> bool:
        return any(c.name == "PHPSESSID" for c in self.jar)

    def get(self, path: str, params: dict | None = None) -> tuple[str, str]:
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with self.opener.open(url, timeout=HTTP_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.geturl(), resp.read().decode(charset, "replace")

    def page(self, name: str) -> PageDoc:
        final_url, body = self.get("index.php", {"page": name})
        doc = PageDoc(body)
        if is_login_page(final_url, doc.text):
            raise AuthExpired("Kirppari-Kalle-kirjautuminen on vanhentunut.")
        return doc

    def _rpc(self, params: dict) -> dict:
        _, body = self.get("rpc.php", {"request": "json_login", "lang": "fi", **params})
        try:
            return json.loads(body)
        except ValueError:
            raise LoginFailed("rpc.php ei palauttanut JSONia") from None

    def login(self, username: str, password: str, shop: str = "") -> None:
        """
        The same calls the login page makes: username_login, then
        choose_company when the account has several shops.
        Never logs the credentials (they travel in the query string).
        """
        try:
            self.jar.clear()
            self.get("guest.php", {"page": "login"})
            data = self._rpc({
                "method": "username_login",
                "company_id": 1,
                "username": username,
                "password": password,
            })
        except LoginFailed:
            raise
        except Exception as exc:
            raise LoginFailed(f"kirjautumispyyntö epäonnistui ({type(exc).__name__})") from None

        if data.get("status") != "success":
            raise LoginFailed(f"kirjautuminen hylättiin ({data.get('object') or 'tuntematon'})")

        if str(data.get("redirect")).lower() != "true":
            companies = data.get("companies") or {}
            items = list(companies.values()) if isinstance(companies, dict) else list(companies)
            if not items:
                raise LoginFailed("tunnukseen ei ole liitetty kirpputoreja")
            names = [str(item.get("name", "")) for item in items]
            chosen = items[choose_shop_option(names, shop)]
            try:
                data = self._rpc({"method": "choose_company", "company_id": chosen["company_id"]})
            except LoginFailed:
                raise
            except Exception as exc:
                raise LoginFailed(f"kirpputorin valinta epäonnistui ({type(exc).__name__})") from None
            if data.get("status") != "success":
                raise LoginFailed("kirpputorin valinta hylättiin")

        # Verify with a real page; raises AuthExpired if still logged out.
        self.page("myreservations")
        self.save()

    def save(self) -> None:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        self.jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(self.cookie_file, 0o600)

    def import_storage_state(self, path: Path) -> int:
        """Take the cookies of a Playwright storage_state file (auth.json)."""
        data = json.loads(path.read_text(encoding="utf-8"))
        count = 0
        for c in data.get("cookies", []):
            expires = c.get("expires", -1)
            domain = c.get("domain", "")
            self.jar.set_cookie(http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False,
                domain, True, domain.startswith("."),
                c.get("path", "/"), True, bool(c.get("secure")),
                None if expires is None or expires < 0 else int(expires),
                expires is None or expires < 0,
                None, None, {"HttpOnly": None} if c.get("httpOnly") else {},
            ))
            count += 1
        return count


def parse_balance(doc: PageDoc) -> float:
    """
    The page animates the visible balance with JavaScript; the exact value
    is in a hidden field. Falls back to the visible "Saldo 12,34 €" text.
    """
    value = doc.inputs.get("client_balance_float", "").strip()

    if not value:
        match = re.search(
            r"Saldo\s+([0-9\s]+(?:[,.][0-9]{1,2})?)\s*€",
            doc.text,
            re.IGNORECASE,
        )
        if not match:
            raise RuntimeError("Saldoa ei löytynyt mysales-sivulta.")
        value = match.group(1)

    return round(
        float(value.replace("\xa0", "").replace(" ", "").replace(",", ".")),
        2,
    )


def parse_data_updated(text: str) -> datetime | None:
    """ "Tiedot päivitetty: 26.10.2026 11:40" on the sales page."""
    match = re.search(
        r"Tiedot p(?:ä|&auml;)ivitetty:\s*(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
        text,
    )
    if not match:
        return None
    return datetime.strptime(
        f"{match.group(1)} {match.group(2)}", "%d.%m.%Y %H:%M"
    ).replace(tzinfo=TZ)


PRICE_RE = re.compile(r"(\d+(?:[\s\xa0]\d{3})*[,.]\d{2})\s*€")
SALE_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")


def parse_sales(
    tables: list[list[list[str]]],
    stats: dict | None = None,
) -> list[dict]:
    """
    Sold items from the sales tables: a header row (Nro, Nimi, Hinta, ...),
    date rows with a single cell and item rows. Columns are found by header
    name, so hidden extra columns (Määrä, ALV, ...) do not matter.
    Keys stay compatible with the 1.x text parser (date|number|name|price).
    """
    sales: list[dict] = []
    seen: set[str] = set()
    stats = stats if stats is not None else {}
    stats.update(tables=0, rows=0, undated=0)

    for table in tables:
        cols: dict[str, int] | None = None
        current_date: str | None = None

        for row in table:
            lower = [cell.lower() for cell in row]

            if "nro" in lower and "nimi" in lower and "hinta" in lower:
                cols = {name: lower.index(name) for name in ("nro", "nimi", "hinta")}
                stats["tables"] += 1
                continue

            if cols is None:
                continue

            cells = [cell for cell in row if cell]
            if len(cells) == 1:
                date_match = SALE_DATE_RE.match(cells[0])
                if date_match:
                    current_date = date_match.group(1)
                continue

            if len(row) <= max(cols.values()):
                continue

            number = row[cols["nro"]]
            price = PRICE_RE.search(row[cols["hinta"]])
            if not number.isdigit() or not price:
                continue

            if not current_date:
                stats["undated"] += 1
                continue

            sale = {
                "date": current_date,
                "number": int(number),
                "name": row[cols["nimi"]].strip(),
                "price": round(
                    float(
                        price.group(1)
                        .replace("\xa0", "")
                        .replace(" ", "")
                        .replace(",", ".")
                    ),
                    2,
                ),
            }
            key = sale_key(sale)
            if key not in seen:
                seen.add(key)
                sales.append(sale)
                stats["rows"] += 1

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
    data_updated: datetime | None = None,
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
        "data_updated_at": data_updated.isoformat() if data_updated else None,
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
        f"{HA_DISCOVERY_PREFIX}/sensor/{HA_DEVICE_ID}/data_updated_at/config": {
            **common,
            "name": "Tiedot päivitetty",
            "unique_id": "kirpparikalle_data_updated_at",
            "state_topic": MQTT_STATE_TOPIC,
            "value_template": "{{ value_json.data_updated_at | default(none) }}",
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
    data_updated: datetime | None = None,
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
        "data_updated_at": data_updated.isoformat() if data_updated else None,
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
        "data_updated_at": previous.get("data_updated_at"),
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
    client: KirppariKalleClient,
) -> tuple[
    str,
    list[tuple[date, date]],
    dict[int, tuple[dt_time, dt_time] | None],
]:
    reservations = parse_reservations(
        client.page("myreservations").text
    )

    if not reservations:
        logging.warning(
            "Varauksia ei löytynyt myreservations-sivulta"
        )

    company = client.page("companydetails")
    opening_hours = parse_opening_hours(company.text)

    if not any(opening_hours.values()):
        logging.warning(
            "Aukioloaikoja ei löytynyt companydetails-sivulta; "
            "myyntejä ei lueta"
        )

    shop = next((h for h in company.h1 if h), "Kirppari-Kalle")

    return shop, reservations, opening_hours


def read_sales(
    client: KirppariKalleClient,
) -> tuple[float, list[dict], datetime | None]:
    doc = client.page("mysales")
    balance = parse_balance(doc)
    stats: dict = {}
    sales = parse_sales(doc.tables, stats)

    logging.info(
        "Sales page: %d sales tables, %d rows, %d rows without date",
        stats["tables"],
        stats["rows"],
        stats["undated"],
    )

    # The balance is the total of sold items before payouts.
    total = round(sum(sale["price"] for sale in sales), 2)
    if sales and abs(total - balance) > 0.005:
        logging.warning(
            "Myytyjen tuotteiden summa %.2f EUR ei vastaa saldoa %.2f EUR "
            "(%d riviä) – tarkista myyntitaulun luku",
            total,
            balance,
            len(sales),
        )

    return balance, sales, parse_data_updated(doc.text)


def process_sales_change(
    mqtt_client: mqtt.Client,
    shop: str,
    reservation: tuple[date, date],
    balance: float,
    sales: list[dict],
    checked_at: datetime,
    data_updated: datetime | None = None,
) -> None:
    previous = load_monitor_state()

    state_payload = build_state_payload(
        shop,
        reservation,
        balance,
        sales,
        checked_at,
        data_updated,
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
            data_updated,
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
        data_updated,
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


def attempt_login(client: KirppariKalleClient) -> bool:
    """Log in if credentials are set and the attempt pacing allows it."""
    attempt_at = datetime.now(TZ)

    if not relogin_allowed(load_last_login_attempt(), attempt_at):
        if KK_USERNAME and KK_PASSWORD:
            logging.info(
                "Edellinen kirjautumisyritys liian äskettäin "
                "(epäonnistuneen jälkeen odotetaan %s h); ei yritetä nyt",
                f"{RELOGIN_MIN_INTERVAL_HOURS:g}",
            )
        return False

    logging.info("Kirjaudutaan Kirppari-Kalleen")
    save_last_login_attempt(attempt_at, ok=False)

    try:
        client.login(KK_USERNAME, KK_PASSWORD, KK_SHOP)
    except (LoginFailed, AuthExpired) as exc:
        logging.error("Kirjautuminen epäonnistui: %s", exc)
        return False
    except Exception as exc:
        # Not logged in full: request URLs carry credentials.
        logging.error("Kirjautuminen epäonnistui: %s", type(exc).__name__)
        return False

    save_last_login_attempt(attempt_at, ok=True)
    logging.info("Kirjautuminen onnistui")
    return True


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

    if (
        not (KK_USERNAME and KK_PASSWORD)
        and not SESSION_FILE.exists()
        and not AUTH_STATE_FILE.exists()
    ):
        logging.error(
            "KK_USERNAME/KK_PASSWORD puuttuvat eikä tallennettua istuntoa ole"
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

        client = KirppariKalleClient(SESSION_FILE)

        if not client.has_session():
            if KK_USERNAME and KK_PASSWORD:
                logging.info("Ei tallennettua istuntoa")
                if not attempt_login(client):
                    publish_auth_state(mqtt_client, "expired")
                    return 2
            elif AUTH_STATE_FILE.exists():
                # 1.x without credentials: reuse the browser session.
                count = client.import_storage_state(AUTH_STATE_FILE)
                client.save()
                logging.info("auth.json tuotu istuntotiedostoon (%d evästettä)", count)

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
        last_activity: datetime | None = None
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
                    ) = refresh_metadata(client)

                    client.save()

                    last_activity = now

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

                elif keepalive_due(last_activity, now):
                    client.page("myreservations")
                    client.save()
                    last_activity = now
                    logging.info("Keepalive ok")

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

                    balance, sales, data_updated = read_sales(
                        client
                    )

                    client.save()
                    last_activity = now

                    process_sales_change(
                        mqtt_client,
                        shop or "Kirppari-Kalle",
                        active,
                        balance,
                        sales,
                        now,
                        data_updated,
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

                if attempt_login(client):
                    last_activity = datetime.now(TZ)
                    next_meta_refresh = None
                    continue

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
            client.save()
        except Exception:
            pass

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
