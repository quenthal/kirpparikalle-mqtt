# Kirppari-Kalle MQTT

Kirppari-Kalle-myyntiseuranta Home Assistantille MQTT:n kautta.

Palvelu kirjautuu Kirppari-Kalleen ja lukee valitun kirpputorin aktiiviset
varaukset, aukioloajat, myyntisaldon ja myydyt tuotteet. Tiedot julkaistaan
MQTT:hen, ja Home Assistant löytää sensorit automaattisesti MQTT Discoveryn avulla.

> Tämä on epävirallinen integraatio. Se lukee Kirppari-Kallen web-käyttöliittymää
> tavallisilla HTTP-pyynnöillä eikä käytä virallista API:a. Sivuston rakenteen
> muutos voi vaatia parserien päivittämistä.

## Nykyinen versio

`2.0.0`

- pelkkä HTTP Pythonin vakiokirjastolla – ei selainta, ei Playwrightia
- kirjautuminen tunnuksilla, istunto säilyy evästetiedostossa, automaattinen
  uudelleenkirjautuminen
- tarkka saldo sivun piilokentästä (ei animaatio-odotusta)
- myyntitarkistus 5 minuutin välein aktiivisen varauksen ja aukioloajan aikana
  sekä kerran sulkemisajan jälkeen
- uusien myyntien tunnistus, retained nykytila, ei-retained muutostapahtuma
- Home Assistant MQTT Discovery (8 entiteettiä)
- istunnon ylläpitohaku tunnin välein
- vanhentunut/epäonnistunut kirjautuminen näkyy Home Assistantissa ongelma-anturina

## Arkkitehtuuri

```text
Kirppari-Kalle (www.kirpparikalle.net)
      |
      | HTTPS (kirjautuminen + sivut, evästetiedosto)
      v
kk_mqtt.py (esim. Proxmox LXC + systemd)
      |
      | MQTT
      v
MQTT broker  --> Home Assistant
```

Ainoa ulkoinen riippuvuus on `paho-mqtt` (ja `tzdata`).

## Toimintalogiikka

```text
käynnistys
   +--> MQTT-yhteys, Home Assistant Discovery, tallennetun tilan uudelleenjulkaisu
   +--> istunto evästetiedostosta (tai kirjautuminen tunnuksilla)
   +--> varaukset + aukioloajat -> reservation-topic
   v
5 minuutin sykli
   +--> varaus voimassa ja kirppis auki (tai sulkemisen jälkeinen loppuluku)?
   |       kyllä -> mysales: saldo + tuotteet -> vertaa edelliseen
   +--> muuten tarvittaessa ylläpitohaku (KEEPALIVE_MINUTES)
```

- Varaus- ja aukiolotiedot päivitetään käynnistyksessä sekä klo 08 ja 20 Suomen ajassa.
- Kun varaus on voimassa, myynnit luetaan vielä kerran noin
  `FINAL_READ_DELAY_MINUTES` (oletus 10) minuuttia sulkemisajan jälkeen.
- Jos `MAX_CONSECUTIVE_FAILURES` (oletus 6) sykliä peräkkäin epäonnistuu, ohjelma
  lopettaa ja systemd käynnistää sen uudelleen.
- Myyntisivun luvun jälkeen tarkistetaan, että myytyjen tuotteiden summa vastaa
  saldoa; poikkeamasta tulee lokiin varoitus.

## Kirjautuminen

Ohjelma kirjautuu samoilla kutsuilla kuin Kirppari-Kallen kirjautumissivu ja
valitsee kirpputorin, jos tunnukseen on liitetty useita:

```ini
KK_USERNAME=<käyttäjätunnus>
KK_PASSWORD=<salasana>
# Vain jos tunnuksella on useita kirpputoreja (osa nimestä riittää):
KK_SHOP=<kirpputorin nimi>
```

Istunnon eväste tallennetaan tiedostoon `/var/lib/kirpparikalle/session.cookies`
(oikeudet 600), joten uudelleenkäynnistys ei kirjaudu turhaan uudestaan.

Kun istunto vanhenee, ohjelma kirjautuu uudelleen. Tunnuksen lukittumisen
estämiseksi epäonnistuneen yrityksen jälkeen odotetaan
`RELOGIN_MIN_INTERVAL_HOURS` (oletus 6) tuntia ja onnistuneenkin jälkeen
vähintään 10 minuuttia. Sillä välin `kirpparikalle/auth` on `expired`, Home
Assistantin ongelma-anturi on päällä ja ohjelma lopettaa virhekoodilla 2
(systemd yrittää uudelleen 5 minuutin välein).

Salasanaa ei kirjoiteta lokiin. Käytä Kirppari-Kalleen salasanaa, jota et käytä
muualla, ja pidä `/etc/kirpparikalle.env` vain rootin luettavana (`chmod 600`).

Ilman tunnuksia ohjelma voi käyttää 1.x-version `auth.json`-istuntoa (ks.
päivitysohje), mutta istunnon vanhetessa se vain hälyttää.

## MQTT-topicit

| Topic | Retain | Sisältö |
|---|---:|---|
| `kirpparikalle/status` | kyllä | `online` / `offline` |
| `kirpparikalle/state` | kyllä | saldo, myyty määrä, tarkistusaika, tietojen päivitysaika |
| `kirpparikalle/update` | ei | vain oikea myyntimuutos |
| `kirpparikalle/reservation` | kyllä | myyntijakson alku/loppu |
| `kirpparikalle/auth` | kyllä | `ok` / `expired` |

Esimerkki `kirpparikalle/state`:

```json
{
  "shop": "Esimerkkikirppis",
  "balance": 16.0,
  "sold_count": 3,
  "reservation": {
    "start": "2026-10-03",
    "end": "2026-10-10"
  },
  "checked_at": "2026-10-03T12:01:29+03:00",
  "data_updated_at": "2026-10-03T12:01:00+03:00"
}
```

`data_updated_at` on Kirppari-Kallen oma "Tiedot päivitetty" -aika.

Esimerkki `kirpparikalle/update`:

```json
{
  "event_type": "sales_update",
  "shop": "Esimerkkikirppis",
  "observed_at": "2026-10-03T12:01:29+03:00",
  "previous_balance": 16.0,
  "balance": 24.0,
  "balance_change": 8.0,
  "new_sales": [
    {
      "date": "03.10.2026",
      "number": 123,
      "name": "Esimerkkituote",
      "price": 8.0
    }
  ],
  "removed_sales": []
}
```

`update` ei ole retained, joten vanha myyntitapahtuma ei laukea uudestaan
Home Assistantin tai brokerin restartin jälkeen.

Esimerkki `kirpparikalle/reservation`:

```json
{
  "shop": "Esimerkkikirppis",
  "reservation_start": "2026-10-03T10:00:00+03:00",
  "reservation_end": "2026-10-10T16:00:00+03:00",
  "reservation_active": true,
  "updated_at": "2026-10-03T12:00:00+03:00"
}
```

Varauspäivämäärät yhdistetään kirpputorin todellisiin aukioloaikoihin.
`reservation` julkaistaan aina kun sen sisältö muuttuu sekä jokaisen
metadatahaun yhteydessä.

## Home Assistant

MQTT Discovery luo saman Kirppari-Kalle-laitteen alle:

- `sensor.kirppari_kalle_saldo`
- `sensor.kirppari_kalle_myytyja_tuotteita`
- `sensor.kirppari_kalle_tarkistettu`
- `sensor.kirppari_kalle_tiedot_paivitetty`
- `sensor.kirppari_kalle_myynti_alkaa`
- `sensor.kirppari_kalle_myynti_paattyy`
- `event.kirppari_kalle_myyntimuutos`
- `binary_sensor.kirppari_kalle_kirjautuminen_vanhentunut`

Home Assistant voi nimetä entity-ID:t hieman eri tavalla, jos samalla nimellä on jo
olemassa entiteettejä. Kirjautumisanturilla ei ole availability-sidosta, joten
sen tila näkyy myös palvelun ollessa pysähdyksissä.

Esimerkit:

- `home-assistant/automation.example.yaml` – ilmoitus uusista myynneistä
  (kuuntelee suoraan `kirpparikalle/update`-topicia)
- `home-assistant/auth_expired.example.yaml` – ilmoitus kirjautumisongelmasta
- `home-assistant/lovelace.example.yaml` – kortti

Vaihda automaatioihin oma notify-palvelu (`notify.notify`) ja tarvittaessa `target`.

## Projektin rakenne

```text
kirpparikalle-mqtt/
├── kk_mqtt.py
├── README.md
├── CHANGELOG.md
├── requirements.txt
├── kirpparikalle.env.example
├── systemd/
│   └── kirpparikalle.service
├── tools/
│   └── create_auth_state.py      (vain 1.x-istunnon luontiin, valinnainen)
├── home-assistant/
│   ├── automation.example.yaml
│   ├── auth_expired.example.yaml
│   └── lovelace.example.yaml
├── tests/
│   └── test_parsers.py
└── .github/workflows/
    └── tests.yml
```

## Asennus (Debian-LXC)

```bash
apt update
apt install -y python3 python3-venv ca-certificates git

useradd --system --home /opt/kirpparikalle --shell /usr/sbin/nologin kirpparikalle
mkdir -p /var/lib/kirpparikalle

git clone https://github.com/quenthal/kirpparikalle-mqtt.git /opt/kirpparikalle
git -C /opt/kirpparikalle checkout v2.0.0

python3 -m venv /opt/kirpparikalle/venv
/opt/kirpparikalle/venv/bin/pip install -r /opt/kirpparikalle/requirements.txt

cp /opt/kirpparikalle/kirpparikalle.env.example /etc/kirpparikalle.env
chmod 600 /etc/kirpparikalle.env
nano /etc/kirpparikalle.env      # MQTT_* ja KK_*

chown -R kirpparikalle:kirpparikalle /opt/kirpparikalle /var/lib/kirpparikalle

cp /opt/kirpparikalle/systemd/kirpparikalle.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kirpparikalle
```

Palvelu käyttää tarkoituksella `After=network.target`: testatussa Proxmox LXC:ssä
`network-online.target` jäi odottamaan ja esti automaattikäynnistyksen.

Aikavyöhyke: Proxmox-hostilla `pct set <VMID> --timezone Europe/Helsinki`.
Ohjelma käyttää lisäksi eksplisiittisesti `Europe/Helsinki`-aikavyöhykettä.

## Lokit

```bash
journalctl -u kirpparikalle -f
```

Onnistuneessa käynnistyksessä:

```text
MQTT connected
Home Assistant MQTT Discovery julkaistu (8 entiteettiä)
Tallennettu MQTT state julkaistu uudelleen
Kirjaudutaan Kirppari-Kalleen            (vain jos istuntoa ei ole)
Kirjautuminen onnistui
Metadata refreshed: Esimerkkikirppis, reservations 1. Next refresh: ...
```

Kun kirppis on auki ja varaus voimassa:

```text
Active reservation and shop open -> reading sales
Sales page: 1 sales tables, 3 rows, 0 rows without date
No changes: balance 16.00 EUR, 3 sales
```

Uuden myynnin jälkeen:

```text
CHANGE: 16.00 -> 24.00 EUR, new 1, removed 0
NEW SALE: 03.10.2026 | Esimerkkituote | 8.00 EUR
```

MQTT:n tarkistus: `mosquitto_sub -h <MQTT_HOST> -u <käyttäjä> -P '<salasana>' -t 'kirpparikalle/#' -v`
(Discovery: `-t 'homeassistant/#'`).

## Tilatiedostot

`/var/lib/kirpparikalle/`:

- `monitor_state.json` – edellinen myyntiluku; restart ei ilmoita jo havaittuja
  myyntejä uutena. Uuden varauksen ensimmäinen luku otetaan lähtötilaksi.
- `session.cookies` – Kirppari-Kallen istunto (salaisuus, 600)
- `last_login_attempt.json` – edellisen kirjautumisyrityksen aika ja tulos

## Päivitys

```bash
systemctl stop kirpparikalle
git -C /opt/kirpparikalle fetch --tags
git -C /opt/kirpparikalle checkout v2.0.0
/opt/kirpparikalle/venv/bin/pip install -r /opt/kirpparikalle/requirements.txt
chown -R kirpparikalle:kirpparikalle /opt/kirpparikalle
cp /opt/kirpparikalle/systemd/kirpparikalle.service /etc/systemd/system/
systemctl daemon-reload
systemctl start kirpparikalle
```

`/etc/kirpparikalle.env` ja `/var/lib/kirpparikalle/` ovat Git-hakemiston
ulkopuolella, eikä päivitys koske niihin.

### 1.x → 2.0

1. Lisää `/etc/kirpparikalle.env`:iin `KK_USERNAME`, `KK_PASSWORD` ja tarvittaessa
   `KK_SHOP`.
2. Päivitä kuten yllä. Ensimmäinen käynnistys kirjautuu tunnuksilla ja luo
   `session.cookies`-tiedoston. (Ilman tunnuksia 1.x:n `auth.json`-istunto otetaan
   käyttöön sellaisenaan.)
3. Playwright ja Chromium (`ms-playwright/`) jäävät levylle, joten paluu 1.x:ään
   onnistuu. Kun 2.0 on todettu toimivaksi, ne voi poistaa:
   `rm -rf /opt/kirpparikalle/ms-playwright` ja
   `/opt/kirpparikalle/venv/bin/pip uninstall -y playwright`.

`SALES_SETTLE_MS` ei ole enää käytössä.

## Testit

```bash
/opt/kirpparikalle/venv/bin/python -m unittest discover -s tests -v
```

Testit käyttävät paikallisia esimerkkisivuja eivätkä ota verkkoyhteyttä
Kirppari-Kalleen. GitHub Actions ajaa ne jokaisessa pushissa.

## Versiot

Versiot merkitään Git-tageilla (`v2.0.0`) ja muutokset kirjataan `CHANGELOG.md`:hen:
päivitä `APP_VERSION` (`kk_mqtt.py`), README ja CHANGELOG, commitoi ja tagaa.

## Tietoturva

Älä commitoi tai jaa: `/etc/kirpparikalle.env` (MQTT-salasana, Kirppari-Kallen
tunnukset), `session.cookies`, `auth.json`, `kirpparikalle_state.json`,
`monitor_state.json` (sisältää myyntitiedot). `.gitignore` sisältää näille
tavallisimmat suojaukset.
