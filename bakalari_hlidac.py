#!/usr/bin/env python3
"""
Hlídač přístupového systému Bakaláři.

Přihlásí se do webových Bakalářů, přečte dnešní příchody/odchody
(stránka /AccessSystem/Accesses) a za každý nový záznam pošle
notifikaci přes ntfy.sh na telefon.

Nastavení přes proměnné prostředí:
  BAKA_URL       např. https://zsvrsovicka.bakalari.cz
  BAKA_USER      přihlašovací jméno rodiče
  BAKA_PASS      heslo
  NTFY_TOPIC     název kanálu v ntfy (vymysli si dlouhý a náhodný, např. jachym-skola-8f3k29x)
  STATE_FILE     (volitelné) soubor se stavem, výchozí ./bakalari_stav.json
  INTERVAL       (volitelné) po kolika sekundách kontrolovat; když chybí, proběhne jen jedna kontrola
  RUN_MINUTES    (volitelné) po kolika minutách se má skript sám ukončit
  WINDOW         (volitelné) hlídací okno v pražském čase, např. 06:00-18:00

Spuštění:
  pip install requests
  python bakalari_hlidac.py            # jedna kontrola (vhodné pro cron)
  INTERVAL=120 python bakalari_hlidac.py   # běží pořád a kontroluje každé 2 minuty
  python bakalari_hlidac.py --test     # jen vypíše, co na stránce vidí, a pošle zkušební notifikaci
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BAKA_URL = os.environ.get("BAKA_URL", "").rstrip("/")
BAKA_USER = os.environ.get("BAKA_USER", "")
BAKA_PASS = os.environ.get("BAKA_PASS", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = os.environ.get("STATE_FILE", "bakalari_stav.json")
INTERVAL = int(os.environ.get("INTERVAL", "0") or 0)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0") or 0)  # po kolika minutách skončit (0 = nekonečně)
# Hlídací okno podle pražského času, např. "06:00-18:00" (prázdné = bez omezení)
WINDOW = os.environ.get("WINDOW", "")
TZ = ZoneInfo("Europe/Prague")


def window_end_ts():
    """Vrátí čas konce dnešního okna (timestamp), nebo None. Mimo okno vrací 0."""
    if not WINDOW:
        return None
    start, end = WINDOW.split("-")
    now = datetime.now(TZ)
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    s_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    e_dt = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    if not (s_dt <= now < e_dt):
        return 0
    return e_dt.timestamp()

ACCESSES_RE = re.compile(r"const\s+accesses\s*=\s*(\[.*?\]);", re.S)


def login(session: requests.Session) -> None:
    """Přihlásí se stejně jako webový formulář na /login."""
    data = {"username": BAKA_USER, "password": BAKA_PASS, "returnUrl": "/AccessSystem/Accesses"}
    r = session.post(f"{BAKA_URL}/Login", data=data, timeout=30)
    if "formlogin" in r.text or "/login" in r.url.lower():
        # Některé verze přijímají formulář jen přes GET (formulář nemá method=post)
        r = session.get(f"{BAKA_URL}/Login", params=data, timeout=30)
    if "formlogin" in r.text:
        raise RuntimeError("Přihlášení se nepovedlo – zkontroluj jméno a heslo.")


def fetch_accesses(session: requests.Session) -> list:
    r = session.get(f"{BAKA_URL}/AccessSystem/Accesses", timeout=30)
    m = ACCESSES_RE.search(r.text)
    if not m:
        # session vypršela → znovu přihlásit a zkusit ještě jednou
        login(session)
        r = session.get(f"{BAKA_URL}/AccessSystem/Accesses", timeout=30)
        m = ACCESSES_RE.search(r.text)
        if not m:
            raise RuntimeError("Na stránce jsem nenašel data o přístupech (změnila se struktura?).")
    return json.loads(m.group(1))


def events_from(accesses: list) -> list:
    """Z řádků tabulky udělá jednotlivé události (příchod / odchod)."""
    events = []
    for row in accesses:
        day = (row.get("Date") or "")[:10]
        name = row.get("Name") or ""
        for field, label in (("Arrival", "Příchod"), ("Exit", "Odchod")):
            t = (row.get(field) or "").strip()
            if t:
                events.append({"key": f"{day}|{field}|{t}|{name}", "label": label, "time": t, "day": day, "name": name})
    return events


def notify(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print(f"[bez ntfy] {title}: {message}", flush=True)
        return
    # JSON varianta – podporuje diakritiku v titulku. Při chybě to zkusí znovu.
    last = None
    for attempt in range(4):
        try:
            r = requests.post("https://ntfy.sh/", json={"topic": NTFY_TOPIC, "title": title, "message": message,
                                                        "tags": ["school"], "priority": 4}, timeout=15)
            if r.status_code < 300:
                return
            last = f"HTTP {r.status_code} {r.text[:200]}"
            print(f"  ntfy vrátilo chybu: {last}", file=sys.stderr, flush=True)
        except requests.RequestException as ex:
            last = str(ex)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Upozornění se nepodařilo odeslat: {last}")


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": [], "initialized": False}


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def check_once(session: requests.Session) -> None:
    state = load_state()
    seen = set(state["seen"])
    new = [e for e in events_from(fetch_accesses(session)) if e["key"] not in seen]
    for e in new:
        # Při úplně prvním spuštění jen uložíme stav, ať nechodí notifikace za staré záznamy.
        # Do stavu se událost zapíše až po úspěšném odeslání – když se odeslání nepovede,
        # zkusí se to při další kontrole znovu.
        if state.get("initialized"):
            first = e["name"].split()[-1] if e["name"] else "Syn"
            notify(f"{e['label']}: {first} {e['time']}", f"{e['label']} do školy v {e['time']} ({e['day']})"
                   if e["label"] == "Příchod" else f"{e['label']} ze školy v {e['time']} ({e['day']})")
        state["seen"].append(e["key"])
        save_state(state)
    state["initialized"] = True
    save_state(state)
    # Záměrně nevypisujeme časy ani počty – na GitHubu jsou logy veřejné
    print(f"{datetime.now(TZ):%H:%M} kontrola OK", flush=True)


def main() -> None:
    if not (BAKA_URL and BAKA_USER and BAKA_PASS):
        sys.exit("Chybí BAKA_URL, BAKA_USER nebo BAKA_PASS.")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (bakalari-hlidac)"
    login(session)

    if "--test" in sys.argv:
        acc = fetch_accesses(session)
        print(f"Přihlášení OK, data o přístupech nalezena (řádků dnes: {len(acc)}).", flush=True)
        notify("Bakaláři hlídač", "Zkušební notifikace – funguje to.")
        print("Zkušební upozornění přijato serverem ntfy.", flush=True)
        return

    deadline = time.time() + RUN_MINUTES * 60 if RUN_MINUTES else None
    w_end = window_end_ts()
    if w_end == 0:
        print("Mimo hlídací okno, končím.", flush=True)
        return
    if w_end:
        deadline = min(deadline, w_end) if deadline else w_end
    while True:
        try:
            check_once(session)
        except Exception as ex:  # noqa: BLE001
            print(f"{datetime.now(TZ):%H:%M:%S} chyba: {ex}", file=sys.stderr, flush=True)
            session = requests.Session()
            session.headers["User-Agent"] = "Mozilla/5.0 (bakalari-hlidac)"
            try:
                login(session)
            except Exception as ex2:  # noqa: BLE001
                print(f"  opětovné přihlášení selhalo: {ex2}", file=sys.stderr)
        if not INTERVAL or (deadline and time.time() + INTERVAL > deadline):
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""
Hlídač přístupového systému Bakaláři.

Přihlásí se do webových Bakalářů, přečte dnešní příchody/odchody
(stránka /AccessSystem/Accesses) a za každý nový záznam pošle
notifikaci přes ntfy.sh na telefon.

Nastavení přes proměnné prostředí:
  BAKA_URL       např. https://zsvrsovicka.bakalari.cz
  BAKA_USER      přihlašovací jméno rodiče
  BAKA_PASS      heslo
  NTFY_TOPIC     název kanálu v ntfy (vymysli si dlouhý a náhodný, např. jachym-skola-8f3k29x)
  STATE_FILE     (volitelné) soubor se stavem, výchozí ./bakalari_stav.json
  INTERVAL       (volitelné) po kolika sekundách kontrolovat; když chybí, proběhne jen jedna kontrola
  RUN_MINUTES    (volitelné) po kolika minutách se má skript sám ukončit

Spuštění:
  pip install requests
  python bakalari_hlidac.py            # jedna kontrola (vhodné pro cron)
  INTERVAL=120 python bakalari_hlidac.py   # běží pořád a kontroluje každé 2 minuty
  python bakalari_hlidac.py --test     # jen vypíše, co na stránce vidí, a pošle zkušební notifikaci
"""
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

BAKA_URL = os.environ.get("BAKA_URL", "").rstrip("/")
BAKA_USER = os.environ.get("BAKA_USER", "")
BAKA_PASS = os.environ.get("BAKA_PASS", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = os.environ.get("STATE_FILE", "bakalari_stav.json")
INTERVAL = int(os.environ.get("INTERVAL", "0") or 0)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0") or 0)  # po kolika minutách skončit (0 = nekonečně)

ACCESSES_RE = re.compile(r"const\s+accesses\s*=\s*(\[.*?\]);", re.S)


def login(session: requests.Session) -> None:
    """Přihlásí se stejně jako webový formulář na /login."""
    data = {"username": BAKA_USER, "password": BAKA_PASS, "returnUrl": "/AccessSystem/Accesses"}
    r = session.post(f"{BAKA_URL}/Login", data=data, timeout=30)
    if "formlogin" in r.text or "/login" in r.url.lower():
        # Některé verze přijímají formulář jen přes GET (formulář nemá method=post)
        r = session.get(f"{BAKA_URL}/Login", params=data, timeout=30)
    if "formlogin" in r.text:
        raise RuntimeError("Přihlášení se nepovedlo – zkontroluj jméno a heslo.")


def fetch_accesses(session: requests.Session) -> list:
    r = session.get(f"{BAKA_URL}/AccessSystem/Accesses", timeout=30)
    m = ACCESSES_RE.search(r.text)
    if not m:
        # session vypršela → znovu přihlásit a zkusit ještě jednou
        login(session)
        r = session.get(f"{BAKA_URL}/AccessSystem/Accesses", timeout=30)
        m = ACCESSES_RE.search(r.text)
        if not m:
            raise RuntimeError("Na stránce jsem nenašel data o přístupech (změnila se struktura?).")
    return json.loads(m.group(1))


def events_from(accesses: list) -> list:
    """Z řádků tabulky udělá jednotlivé události (příchod / odchod)."""
    events = []
    for row in accesses:
        day = (row.get("Date") or "")[:10]
        name = row.get("Name") or ""
        for field, label in (("Arrival", "Příchod"), ("Exit", "Odchod")):
            t = (row.get(field) or "").strip()
            if t:
                events.append({"key": f"{day}|{field}|{t}|{name}", "label": label, "time": t, "day": day, "name": name})
    return events


def notify(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print(f"[bez ntfy] {title}: {message}")
        return
    # JSON varianta – podporuje diakritiku v titulku
    requests.post("https://ntfy.sh/", json={"topic": NTFY_TOPIC, "title": title, "message": message, "tags": ["school"]}, timeout=15)


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": [], "initialized": False}


def save_state(state: dict) -> None:
    state["seen"] = state["seen"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def check_once(session: requests.Session) -> None:
    state = load_state()
    seen = set(state["seen"])
    new = [e for e in events_from(fetch_accesses(session)) if e["key"] not in seen]
    for e in new:
        # Při úplně prvním spuštění jen uložíme stav, ať nechodí notifikace za staré záznamy
        if state.get("initialized"):
            first = e["name"].split()[-1] if e["name"] else "Syn"
            notify(f"{e['label']}: {first} {e['time']}", f"{e['label']} do školy v {e['time']} ({e['day']})"
                   if e["label"] == "Příchod" else f"{e['label']} ze školy v {e['time']} ({e['day']})")
        state["seen"].append(e["key"])
    state["initialized"] = True
    save_state(state)
    # Záměrně nevypisujeme časy ani počty – na GitHubu jsou logy veřejné
    print(f"{datetime.now():%H:%M} kontrola OK")


def main() -> None:
    if not (BAKA_URL and BAKA_USER and BAKA_PASS):
        sys.exit("Chybí BAKA_URL, BAKA_USER nebo BAKA_PASS.")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (bakalari-hlidac)"
    login(session)

    if "--test" in sys.argv:
        acc = fetch_accesses(session)
        print(f"Přihlášení OK, data o přístupech nalezena (řádků dnes: {len(acc)}).")
        notify("Bakaláři hlídač", "Zkušební notifikace – funguje to.")
        return

    deadline = time.time() + RUN_MINUTES * 60 if RUN_MINUTES else None
    while True:
        try:
            check_once(session)
        except Exception as ex:  # noqa: BLE001
            print(f"{datetime.now():%H:%M:%S} chyba: {ex}", file=sys.stderr)
            session = requests.Session()
            session.headers["User-Agent"] = "Mozilla/5.0 (bakalari-hlidac)"
            try:
                login(session)
            except Exception as ex2:  # noqa: BLE001
                print(f"  opětovné přihlášení selhalo: {ex2}", file=sys.stderr)
        if not INTERVAL or (deadline and time.time() + INTERVAL > deadline):
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
