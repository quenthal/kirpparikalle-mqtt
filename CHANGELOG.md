# Changelog

## 1.2.2

- Istunnon ylläpitohaku: kevyt sivulataus, jos mitään sivua ei ole haettu
  `KEEPALIVE_MINUTES` (oletus 60) minuuttiin. 1.2.1 haki varausten ulkopuolella
  vain klo 08 ja 20, mikä jätti istunnon 12 tunniksi käyttämättä.
- Valinnainen automaattinen uudelleenkirjautuminen (`KK_USERNAME`, `KK_PASSWORD`,
  tarvittaessa `KK_SHOP`); enintään yksi yritys `RELOGIN_MIN_INTERVAL_HOURS`
  (oletus 6) tunnissa. Epäonnistuessa toiminta kuten ennen (`expired`).
- Tunnusten kanssa `auth.json` ei ole pakollinen käynnistyksessä.

## 1.2.1

- Aukioloajat: tuki pistemuotoisille kellonajoille (`10.00-18.00`, aiemmin tulkittiin
  00:00–18:00) ja lyhennetyille viikonpäiville (`Ti-Pe`).
- Lopullinen myyntiluku `FINAL_READ_DELAY_MINUTES` (oletus 10) minuuttia sulkemisen
  jälkeen; viimeisen pollausvälin myynnit eivät enää jää lukematta.
- `kirpparikalle/reservation` julkaistaan aina sisällön muuttuessa, ei vain klo 08/20
  (`reservation_active` ja jakson päättyminen päivittyvät heti).
- Uusi retained `kirpparikalle/auth` (`ok`/`expired`) ja Home Assistantin
  ongelma-anturi vanhentuneelle kirjautumiselle + esimerkkiautomaatio.
- Ohjelma lopettaa `MAX_CONSECUTIVE_FAILURES` (oletus 6) peräkkäisen epäonnistuneen
  syklin jälkeen, jotta systemd käynnistää sen (ja selaimen) uudelleen.
- Varoitus lokiin, jos varauksia tai aukioloaikoja ei löydy.
- `MQTT_HOST` on pakollinen asetus (ei enää oletusosoitetta).
- Esimerkeistä poistettu ympäristökohtaiset tiedot; README:n update-esimerkkiin
  lisätty `observed_at`.

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
