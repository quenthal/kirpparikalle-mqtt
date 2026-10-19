#!/usr/bin/env python3
"""
Create a Playwright storage_state file for Kirppari-Kalle.

Run this on a desktop computer with a graphical browser.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


URL = "https://www.kirpparikalle.net/sovellus/index.php?page=mysales"


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--output",
        default=str(
            Path.home()
            / "kirpparikalle_state.json"
        ),
        help="Output storage_state JSON path",
    )

    args = parser.parse_args()

    output = (
        Path(args.output)
        .expanduser()
        .resolve()
    )

    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False
        )

        context = browser.new_context()

        page = context.new_page()

        page.goto(
            URL,
            wait_until="domcontentloaded",
        )

        print()
        print(
            "1. Kirjaudu Kirppari-Kalleen "
            "avautuneessa selaimessa."
        )
        print(
            "2. Valitse oikea kirpputori."
        )
        print(
            "3. Avaa Myynnin seuranta."
        )
        print()

        input(
            "Kun oikea myyntisivu näkyy, "
            "paina Enter... "
        )

        text = page.locator(
            "body"
        ).inner_text()

        if "Kirjaudu ulos" not in text:
            print(
                "VAROITUS: kirjautunutta "
                "tilaa ei tunnistettu."
            )

        context.storage_state(
            path=str(output)
        )

        browser.close()

    print(
        f"Kirjautumistila tallennettu: "
        f"{output}"
    )

    print(
        "Käsittele tiedostoa salaisuutena."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
