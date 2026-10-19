# Changelog

## 1.2.0

- Home Assistant MQTT Discovery:
  - saldo
  - myytyjen tuotteiden määrä
  - tarkistusaika
  - myynti alkaa
  - myynti päättyy
  - myyntimuutos-event
- `kirpparikalle/state` julkaistaan retained-viestinä jokaisella onnistuneella myyntiluvulla.
- Tallennettu state julkaistaan uudelleen heti palvelun käynnistyessä.
- `kirpparikalle/update` julkaistaan vain oikeasta muutoksesta eikä sitä retainata.
- Varaustieto julkaistaan retained `kirpparikalle/reservation` -topiciin.
- Varausajat yhdistetään kirpputorin aukioloaikoihin.
- Metadata päivitetään käynnistyksessä sekä klo 08 ja 20.
- Systemd käyttää Proxmox LXC:ssä toimivaksi todettua `After=network.target`.
- SIGTERM/SIGINT käsitellään hallitusti.
- Mukana parseritestit, Home Assistant -automaatio ja Lovelace-esimerkki.
- Projekti GitHubissa: Git-pohjaiset asennus- ja päivitysohjeet sekä GitHub Actions -testiajo.

## 1.1.0

- Home Assistant MQTT Discovery.
- Muutosviesteihin `event_type: sales_update`.
- Retained state korjattu myös `No changes` -tilanteeseen.

## 1.0.0

- Playwright storage_state -kirjautuminen.
- Varausten ja aukioloaikojen luku.
- Myyntien tarkistus 5 minuutin välein vain aukioloaikana.
- 5 sekunnin odotus saldoanimaatiolle.
- MQTT state/update/status.
