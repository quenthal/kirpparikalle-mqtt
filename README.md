# Kirppari-Kalle MQTT

Kirppari-Kalle-myyntiseuranta Proxmox LXC:lle.

Palvelu lukee kirjautuneesta Kirppari-Kalle-istunnosta aktiiviset varaukset,
valitun kirpputorin aukioloajat, myyntisaldon ja myydyt tuotteet. Tiedot
julkaistaan MQTT:hen ja Home Assistant löytää sensorit automaattisesti MQTT
Discoveryn avulla.

> Tämä on epävirallinen integraatio. Se käyttää Kirppari-Kallen web-käyttöliittymää
> Playwrightilla eikä virallista API:a. Sivuston HTML-rakenteen muutos voi vaatia
> parserien päivittämistä.

## Nykyinen versio

`1.2.1`

Tähän versioon on koottu projektissa käytössä olleet korjaukset ja viimeisimmät
sovitut lisäykset:

- myyntitarkistus 5 minuutin välein
- myyntisivu haetaan vain aktiivisen varauksen ja aukioloajan aikana
- yksi lopullinen myyntiluku sulkemisajan jälkeen
- saldo luetaan vasta 5 sekunnin animaatio-odotuksen jälkeen
- uusien myyntien tunnistus
- retained nykytila
- ei-retained myyntimuutostapahtuma
- Home Assistant MQTT Discovery
- myyntijakson alku- ja loppuaikasensorit
- metadatahaku käynnistyksessä sekä klo 08 ja 20
- Proxmox LXC:lle sopiva systemd-palvelu
- vanhentunut kirjautuminen näkyy Home Assistantissa ongelma-anturina

## Arkkitehtuuri

```text
Kirppari-Kalle
      |
      | HTTPS / Playwright storage_state
      v
kk_mqtt.py (Proxmox LXC)
      |
      | MQTT
      v
MQTT broker (MQTT_HOST:1883)
      |
      +--> Home Assistant
      +--> MQTT Explorer
```

## Toimintalogiikka

```text
käynnistys
   |
   +--> MQTT-yhteys
   +--> Home Assistant Discovery
   +--> varaukset + aukioloajat
   +--> reservation MQTT state
   |
   v
5 minuutin sykli
   |
   +--> onko aktiivinen varaus?
   |       ei -> ei mysales-hakua
   |
   +--> onko kirppis auki?
           ei -> ei mysales-hakua
           kyllä
             |
             v
          mysales
             |
          odota 5 s
             |
          saldo + tuotteet
             |
          vertaa edelliseen
```

Kun varaus on voimassa, myynnit luetaan vielä kerran noin
`FINAL_READ_DELAY_MINUTES` (oletus 10) minuuttia sulkemisajan jälkeen, jotta
viimeisen 5 minuutin myynnit eivät jää huomaamatta.

Jos `MAX_CONSECUTIVE_FAILURES` (oletus 6) sykliä peräkkäin epäonnistuu,
ohjelma lopettaa ja systemd käynnistää sen uudelleen.

Varaus- ja aukiolotiedot päivitetään lisäksi klo 08 ja klo 20 Suomen ajassa.

## MQTT-topicit

| Topic | Retain | Sisältö |
|---|---:|---|
| `kirpparikalle/status` | kyllä | `online` / `offline` |
| `kirpparikalle/state` | kyllä | saldo, myyty määrä, tarkistusaika |
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
  "checked_at": "2026-10-03T12:01:29+03:00"
}
```

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
`reservation` julkaistaan aina kun sen sisältö muuttuu (esim. `reservation_active`
myyntijakson alkaessa ja päättyessä) sekä jokaisen metadatahaun yhteydessä.

## Home Assistant

MQTT Discovery luo saman Kirppari-Kalle-laitteen alle:

- `sensor.kirppari_kalle_saldo`
- `sensor.kirppari_kalle_myytyja_tuotteita`
- `sensor.kirppari_kalle_tarkistettu`
- `sensor.kirppari_kalle_myynti_alkaa`
- `sensor.kirppari_kalle_myynti_paattyy`
- `event.kirppari_kalle_myyntimuutos`
- `binary_sensor.kirppari_kalle_kirjautuminen_vanhentunut`

Home Assistant voi nimetä entity-ID:t hieman eri tavalla, jos samalla nimellä on jo
olemassa entiteettejä.

## Projektin rakenne

```text
kirpparikalle-mqtt/
├── kk_mqtt.py
├── README.md
├── CHANGELOG.md
├── requirements.txt
├── .gitignore
├── kirpparikalle.env.example
├── systemd/
│   └── kirpparikalle.service
├── tools/
│   └── create_auth_state.py
├── home-assistant/
│   ├── automation.example.yaml
│   ├── auth_expired.example.yaml
│   └── lovelace.example.yaml
├── tests/
│   └── test_parsers.py
└── .github/workflows/
    └── tests.yml
```

## Versiot

Versiot merkitään Git-tageilla (`v1.2.0`) ja muutokset kirjataan
`CHANGELOG.md`:hen. Uusi julkaisu:

1. päivitä `APP_VERSION` tiedostossa `kk_mqtt.py`
2. päivitä versio README:hen ja lisää muutokset `CHANGELOG.md`:hen
3. commitoi, tagaa ja pushaa:

```bash
git commit -am "Release 1.2.1"
git tag -a v1.2.1 -m "v1.2.1"
git push origin main --tags
```

Parseritestit ajetaan GitHub Actionsissa jokaisessa pushissa.

## 1. Kirjautumistilan luonti Windowsissa

Kirppari-Kallen pelkkä `PHPSESSID` ei osoittautunut riittäväksi. Käytössä on
Playwrightin `storage_state`, joka säilyttää koko tarvittavan selainistunnon.

Projektihakemistossa:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install playwright
python -m playwright install chromium
python .\tools\create_auth_state.py
```

Avautuvassa Chromiumissa:

1. kirjaudu Kirppari-Kalleen
2. valitse seurattava kirpputori
3. avaa **Myynnin seuranta**
4. palaa PowerShelliin ja paina Enter

Tuloksena syntyy:

```text
%USERPROFILE%\kirpparikalle_state.json
```

Tiedosto vastaa käytännössä kirjautunutta istuntoa. Älä commitoi tai jaa sitä.

## 2. LXC:n perusasennus

Debian-LXC:

```bash
apt update
apt install -y python3 python3-venv python3-pip ca-certificates
```

Käyttäjä ja hakemistot:

```bash
useradd \
  --system \
  --home /opt/kirpparikalle \
  --shell /usr/sbin/nologin \
  kirpparikalle

mkdir -p /var/lib/kirpparikalle
```

Hae projekti GitHubista `/opt/kirpparikalle`-hakemistoon:

```bash
apt install -y git

git clone https://github.com/quenthal/kirpparikalle-mqtt.git /opt/kirpparikalle
git config --global --add safe.directory /opt/kirpparikalle
```

Tietty versio otetaan käyttöön tagilla, esimerkiksi:

```bash
git -C /opt/kirpparikalle checkout v1.2.1
```

`venv/` ja `ms-playwright/` luodaan saman hakemiston alle, mutta `.gitignore`
pitää ne versionhallinnan ulkopuolella.

## 3. Python ja Playwright

```bash
python3 -m venv /opt/kirpparikalle/venv

/opt/kirpparikalle/venv/bin/pip install --upgrade pip

/opt/kirpparikalle/venv/bin/pip install \
  -r /opt/kirpparikalle/requirements.txt
```

Chromium:

```bash
export PLAYWRIGHT_BROWSERS_PATH=/opt/kirpparikalle/ms-playwright

/opt/kirpparikalle/venv/bin/python \
  -m playwright install --with-deps chromium
```

## 4. Kirjautumistila LXC:hen

Windowsista:

```powershell
scp "$HOME\kirpparikalle_state.json" `
  root@LXC_IP:/var/lib/kirpparikalle/auth.json
```

LXC:ssä:

```bash
chown kirpparikalle:kirpparikalle \
  /var/lib/kirpparikalle/auth.json

chmod 600 \
  /var/lib/kirpparikalle/auth.json
```

## 5. MQTT-asetukset

```bash
cp \
  /opt/kirpparikalle/kirpparikalle.env.example \
  /etc/kirpparikalle.env

nano /etc/kirpparikalle.env
```

Nykyisessä ympäristössä broker on:

```ini
MQTT_HOST=<brokerin osoite>
MQTT_PORT=1883
MQTT_USER=mqtt
MQTT_PASSWORD=<oma salasana>
```

Suojaa asetukset:

```bash
chmod 600 /etc/kirpparikalle.env
```

## 6. Oikeudet

```bash
chown -R kirpparikalle:kirpparikalle \
  /opt/kirpparikalle \
  /var/lib/kirpparikalle
```

## 7. Systemd

```bash
cp \
  /opt/kirpparikalle/systemd/kirpparikalle.service \
  /etc/systemd/system/kirpparikalle.service

systemctl daemon-reload
systemctl enable --now kirpparikalle
```

Palvelu käyttää tarkoituksella:

```ini
After=network.target
```

Testatussa Proxmox LXC:ssä `network-online.target` jäi odottamaan ja esti
automaattikäynnistyksen.

## 8. Aikavyöhyke

Proxmox-hostilta:

```bash
pct set <VMID> --timezone Europe/Helsinki
```

Kontissa:

```bash
timedatectl
```

Ohjelma käyttää lisäksi eksplisiittisesti `Europe/Helsinki`-aikavyöhykettä.

## 9. Lokit

```bash
systemctl status kirpparikalle --no-pager -l
```

```bash
journalctl -u kirpparikalle -f
```

Onnistuneessa käynnistyksessä:

```text
MQTT connected
Home Assistant MQTT Discovery julkaistu (7 entiteettiä)
Tallennettu MQTT state julkaistu uudelleen
Metadata refreshed: Esimerkkikirppis, reservations 1. Next refresh: ...
```

Kun kirppis on auki ja varaus voimassa:

```text
Active reservation and shop open -> reading sales
No changes: balance 16.00 EUR, 3 sales
```

Uuden myynnin jälkeen:

```text
CHANGE: 16.00 -> 24.00 EUR, new 1, removed 0
NEW SALE: 03.10.2026 | Esimerkkituote | 8.00 EUR
```

## 10. MQTT:n tarkistus

```bash
apt install -y mosquitto-clients
```

```bash
mosquitto_sub \
  -h <MQTT_HOST> \
  -p 1883 \
  -u mqtt \
  -P '<salasana>' \
  -t 'kirpparikalle/#' \
  -v
```

Discovery:

```bash
mosquitto_sub \
  -h <MQTT_HOST> \
  -p 1883 \
  -u mqtt \
  -P '<salasana>' \
  -t 'homeassistant/#' \
  -v
```

## 11. Home Assistant -ilmoitus

Valmis nykyisen projektin automaatio:

```text
home-assistant/automation.example.yaml
```

Se käyttää:

```text
notify.notify
```

Vaihda tilalle oma notify-palvelu ja tarvittaessa `target`.

Automaatio kuuntelee suoraan:

```text
kirpparikalle/update
```

Sensorien entity-ID:t eivät siis vaikuta ilmoituksen toimintaan.

Kirjautumisen vanhenemisesta ilmoittaa vastaavasti:

```text
home-assistant/auth_expired.example.yaml
```

## 12. Lovelace

Esimerkki:

```text
home-assistant/lovelace.example.yaml
```

Kortissa näkyvät:

- saldo
- myytyjen tuotteiden määrä
- myynti alkaa
- myynti päättyy
- viimeinen tarkistus

## 13. Tilatiedosto

```text
/var/lib/kirpparikalle/monitor_state.json
```

Sen avulla restart ei ilmoita jo havaittuja myyntejä uutena.

Kun uusi varaus alkaa, sen ensimmäinen onnistunut myyntiluku otetaan lähtötilaksi.

## 14. Metadata

Varaus- ja aukiolotiedot haetaan:

- palvelun käynnistyessä
- noin klo 08
- noin klo 20

Pääsilmukka herää 5 minuutin välein, joten kellonaikapäivitys voi käytännössä
tapahtua muutaman minuutin tavoiteajan jälkeen.

## 15. Kirjautumisen vanheneminen

Jos Kirppari-Kalle palauttaa kirjautumissivun, ohjelma julkaisee
`kirpparikalle/auth` = `expired` ja lopettaa ajon virhekoodilla. Home Assistantissa
anturi `binary_sensor.kirppari_kalle_kirjautuminen_vanhentunut` menee tilaan
*ongelma*; sen varaan voi tehdä ilmoitusautomaation. Anturilla ei ole
availability-sidosta, joten tila näkyy myös palvelun ollessa pysähdyksissä.
Systemd yrittää käynnistää sen uudelleen viiden minuutin kuluttua.

Jos istunto on oikeasti vanhentunut:

1. luo uusi `kirpparikalle_state.json`
2. kopioi se `/var/lib/kirpparikalle/auth.json`
3. korjaa oikeudet
4. restarttaa palvelu

```bash
systemctl restart kirpparikalle
```

## 16. Testit

```bash
cd /opt/kirpparikalle

./venv/bin/python \
  -m unittest discover \
  -s tests \
  -v
```

Testit käyttävät paikallisia esimerkkitekstejä eivätkä ota verkkoyhteyttä
Kirppari-Kalleen.

## 17. Tietoturva

Älä commitoi:

- `auth.json`
- `kirpparikalle_state.json`
- `monitor_state.json`
- oikeaa MQTT-salasanaa

`.gitignore` sisältää näille tavallisimmat suojaukset.

## Päivitys nykyiseen LXC-asennukseen

Seuraavat tiedostot ovat Git-hakemiston ulkopuolella, joten päivitys ei koske
niihin. **Älä poista tai luo niitä uudelleen**:

```text
/var/lib/kirpparikalle/auth.json
/var/lib/kirpparikalle/monitor_state.json
/etc/kirpparikalle.env
```

### A. Ensimmäinen siirtyminen Gitiin

Jos `/opt/kirpparikalle` on aiemmin kopioitu käsin (ei Git-hakemisto), muutetaan
se Git-hakemistoksi paikallaan. Olemassa olevat `venv/` ja `ms-playwright/`
säilyvät, ja projektin tiedostot korvautuvat GitHubin versioilla:

```bash
apt install -y git
systemctl stop kirpparikalle

cd /opt/kirpparikalle
git config --global --add safe.directory /opt/kirpparikalle
git init
git remote add origin https://github.com/quenthal/kirpparikalle-mqtt.git
git fetch origin
git checkout -f -B main origin/main
```

Jatka kohdasta **C**.

### B. Myöhemmät päivitykset

```bash
systemctl stop kirpparikalle

git -C /opt/kirpparikalle pull --ff-only
```

Tai tietty versio:

```bash
git -C /opt/kirpparikalle fetch --tags
git -C /opt/kirpparikalle checkout v1.2.1
```

### C. Riippuvuudet, oikeudet ja systemd

Päivitä riippuvuudet:

```bash
/opt/kirpparikalle/venv/bin/pip install \
  -r /opt/kirpparikalle/requirements.txt
```

Jos Playwrightin versio nousi, päivitä myös Chromium:

```bash
PLAYWRIGHT_BROWSERS_PATH=/opt/kirpparikalle/ms-playwright \
  /opt/kirpparikalle/venv/bin/python -m playwright install chromium
```

Korjaa oikeudet:

```bash
chown -R kirpparikalle:kirpparikalle /opt/kirpparikalle
```

Päivitä systemd-yksikkö:

```bash
cp \
  /opt/kirpparikalle/systemd/kirpparikalle.service \
  /etc/systemd/system/kirpparikalle.service

systemctl daemon-reload
systemctl start kirpparikalle
```

Tarkista:

```bash
journalctl -u kirpparikalle -f
```
