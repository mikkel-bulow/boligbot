"""Boligbot: tjekker Kereby, Cityapartment og Taurus for nye lejeboliger,
skriver automatisk op til fremvisning hos Kereby og Cityapartment, og mailer Mikkel.

Kører på GitHub Actions. Personlige oplysninger kommer fra miljøvariabler (GitHub Secrets).
"""
import json
import os
import re
import smtplib
import sys
import time
import traceback
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state" / "seen.json"
SHOTS = ROOT / "screenshots"

URLS = {
    "Kereby": os.getenv("KEREBY_URL", "https://kereby.dk/bolig/"),
    "Cityapartment": os.getenv("CITY_URL", "https://cityapartment.dk/da/lejeboliger-koebenhavn/"),
    "Taurus": os.getenv("TAURUS_URL", "https://www.taurus.dk/boligudlejning/ledige-lejemal/"),
    "Wanamove": [u for u in os.getenv("WANAMOVE_URLS", "https://wanamove.dk/ https://wanamove.dk/for-udlejer/4-boliger/").split()],
}
# Bydele uden for dine områder – bruges til at skelne "forkert område" fra "ukendt område" hos Wanamove
ANDRE_BYDELE = ["valby", "vanløse", "brønshøj", "nordvest", "bispebjerg", "amager", "sydhavn", "københavn s",
                "kbh s", "københavn sv", "københavn nv", "islands brygge", "ørestad", "hellerup", "gentofte",
                "husum", "rødovre", "hvidovre", "herlev", "glostrup", "lyngby", "kastrup", "tårnby", "albertslund",
                "ballerup", "brøndby", "aarhus", "odense", "aalborg"]

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DRY_RUN = str(os.getenv("DRY_RUN", CFG.get("dry_run", True))).lower() in ("1", "true", "yes")

ME = {
    "navn": os.getenv("BOLIG_NAVN", "Mikkel Bülow"),
    "telefon": os.getenv("BOLIG_TELEFON", ""),
    "email": os.getenv("BOLIG_EMAIL", ""),
    "antal_beboere": int(os.getenv("BOLIG_ANTAL_BEBOERE", "1") or 1),
    "hvem": os.getenv("BOLIG_HVEM", "Eneboer"),  # Eneboer / Par / Familie / Bofællesskab
    "besked": os.getenv("BOLIG_BESKED", ""),
}
MAIL_USER = os.getenv("GMAIL_USER", "")
MAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "")
MAIL_TO = os.getenv("MAIL_TO", MAIL_USER)

SUCCESS_WORDS = ["tak for", "modtaget", "er sendt", "er blevet sendt", "vi kontakter",
                 "vender tilbage", "thank you", "booket", "bekræft"]
CAPTCHA_SEL = ('iframe[src*="recaptcha/api2/anchor"], iframe[src*="hcaptcha"], '
               'iframe[src*="challenges.cloudflare.com"], .g-recaptcha:not([data-size="invisible"])')
CAPTCHA_CHALLENGE_SEL = 'iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com"][title*="challenge"]'


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"listings": {}, "last_error_mail": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- parsing helpers

def to_int(s):
    if s is None:
        return None
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else None


def postnr_ok(p):
    return p is not None and any(lo <= p <= hi for lo, hi in CFG["postnr_ranges"])


ADDR_RE = re.compile(r"^[A-Za-zÆØÅæøåÉéÜüÖö'´`\.\- ]{3,}\s\d+\s?[A-Za-z]?\b.*$")


def guess_address(lines, slug):
    for ln in lines:
        l = ln.strip()
        low = l.lower()
        if ADDR_RE.match(l) and not re.search(r"\bkr\b|\bdkk\b|værelse|\bm²|\bm2\b|post nr|ledig", low):
            return l.title() if l.isupper() else l
    # fald tilbage på link-slug
    s = slug.strip("/").split("/")[-1].replace("-", " ")
    return s.title()


def card_text(link_locator):
    """Gå op fra et link til det nærmeste element, der ligner et helt boligkort."""
    return link_locator.evaluate(
        """(a) => {
            let el = a;
            for (let i = 0; i < 8 && el; i++) {
                const t = el.innerText || '';
                if (/(kr\\.?\\s*\\/\\s*md|DKK|kr\\.)/i.test(t) && /m(²|2)/.test(t)) return t;
                el = el.parentElement;
            }
            return a.innerText || '';
        }"""
    )


def collect_cards(page, href_pattern):
    """Returnér {absolut_url: kort-tekst} for alle links der matcher mønsteret."""
    out = {}
    links = page.locator(f'a[href*="{href_pattern}"]')
    for i in range(links.count()):
        a = links.nth(i)
        try:
            href = a.evaluate("a => a.href").split("#")[0].split("?")[0]
        except Exception:
            continue
        if href.rstrip("/").endswith(href_pattern.rstrip("/")):
            continue  # selve oversigtssiden
        try:
            txt = card_text(a)
        except Exception:
            continue
        if len(txt) > len(out.get(href, "")):
            out[href] = txt
    return out


def accept_cookies(page):
    for name in ["Tillad alle", "Accepter alle", "Accepter", "Tillad", "OK", "Accept all"]:
        try:
            b = page.get_by_role("button", name=name, exact=True)
            if b.count() and b.first.is_visible():
                b.first.click(timeout=2000)
                return
        except Exception:
            pass


def goto(page, url):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    accept_cookies(page)
    # scroll for lazy-loadede kort
    for _ in range(6):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(400)


# ---------------------------------------------------------------- scrapers

KEREBY_STATUS = {"available": "", "reserved": "Reserveret", "completed": "Udlejet"}


def scrape_kereby(page):
    """Kerebys boligkort (Jorato) har postnr., værelser, husleje og status som data-felter på hvert kort.
    Dem læser vi direkte. Tekst-gætteri gik galt, fordi m² står som "m" + hævet "2"."""
    goto(page, URLS["Kereby"])
    cards = page.locator("article[data-jorato-card], article.jorato-case-card")
    res = []
    for i in range(cards.count()):
        try:
            d = cards.nth(i).evaluate("""(a) => ({
                id: a.dataset.cardId || '', state: a.dataset.state || '', zip: a.dataset.zip || '',
                rooms: a.dataset.rooms || '', rent: a.dataset.rent || '', size: a.dataset.size || '',
                link: (a.querySelector('a[href*="/bolig/"]') || {}).href || '',
                text: a.innerText || ''})""")
        except Exception:
            continue
        lines = [l.strip() for l in d["text"].splitlines() if l.strip()]
        status = KEREBY_STATUS.get(d["state"], d["state"])
        if not d["state"]:  # data-feltet mangler: fald tilbage på kortets egen tekst
            status = "Udlejet" if re.search(r"\budlejet\b", d["text"], re.I) else (
                "Reserveret" if re.search(r"\breserveret\b", d["text"], re.I) else "")
        if not d["link"] and not status:
            status = "Uden link"  # et ledigt kort uden link kan vi ikke tilmelde os
        url = d["link"].split("#")[0].split("?")[0] if d["link"] else f'{URLS["Kereby"]}#kort-{d["id"] or i}'
        adr_line = next((l for l in lines if re.search(r",\s*\d{4}\s", l)), None)
        adresse = re.sub(r",\s*\d{4}\s.*$", "", adr_line) if adr_line else guess_address(lines, url)
        res.append(dict(udlejer="Kereby", url=url, adresse=adresse,
                        postnr=to_int(d["zip"]), husleje=to_int(d["rent"]),
                        vaerelser=to_int(d["rooms"]), m2=to_int(d["size"]),
                        status=status, ledig_fra=""))
    return res


def scrape_city(page):
    goto(page, URLS["Cityapartment"])
    res = []
    for url, txt in collect_cards(page, "/cityapartments/").items():
        lines = [l for l in txt.splitlines() if l.strip()]
        status = "Udlejet" if re.search(r"\budlejet\b", txt, re.I) else (
            "Reserveret" if re.search(r"\breserveret\b", txt, re.I) else "")
        m_pn = re.search(r"Post\s*nr\.?\s*(\d{4})", txt, re.I)
        m_rent = re.search(r"(\d[\d\.]{3,})\s*DKK", txt)
        m_rooms = re.search(r"(\d+)\s*-\s*værelses", txt, re.I) or re.search(r"(\d+)\s*værelser", txt, re.I)
        m_m2 = re.search(r"(\d+)\s*m(?:²|2)\b", txt)
        m_led = re.search(r"Ledig fra\s*(\d{2}/\d{2}/\d{4})", txt, re.I)
        res.append(dict(udlejer="Cityapartment", url=url, adresse=guess_address(lines, url),
                        postnr=to_int(m_pn.group(1)) if m_pn else None,
                        husleje=to_int(m_rent.group(1)) if m_rent else None,
                        vaerelser=int(m_rooms.group(1)) if m_rooms else None,
                        m2=int(m_m2.group(1)) if m_m2 else None, status=status,
                        ledig_fra=m_led.group(1) if m_led else ""))
    return res


def scrape_taurus(page):
    goto(page, URLS["Taurus"])
    res = []
    links = page.locator('a[href*="lejebolig/?id="]')
    seen = {}
    for i in range(links.count()):
        a = links.nth(i)
        try:
            href = a.evaluate("a => a.href")
            txt = a.evaluate("""(a)=>{let el=a;for(let i=0;i<8&&el;i++){const t=el.innerText||'';
                 if(/kr/i.test(t)&&t.length>30)return t;el=el.parentElement;}return a.innerText||'';}""")
        except Exception:
            continue
        if len(txt) > len(seen.get(href, "")):
            seen[href] = txt
    for url, txt in seen.items():
        bydel = next((b for b in CFG["taurus_bydele"] if b.lower() in txt.lower()), None)
        if not bydel or re.search(r"p-plads|parkering|erhverv", txt, re.I):
            continue
        m_rent = re.search(r"(\d{1,3}\.\d{3})", txt)
        m_rooms = re.search(r"(\d+)\s*vær", txt, re.I)
        m_vej = re.search(r"([A-ZÆØÅ][\wæøåÆØÅéü\.' ]*?(?:gade|vej|allé|alle|plads|pladsen|stræde|boulevard|"
                          r"strøg|torv|bro|have|parken|vænge|brygge|kaj|gang))\b", txt)
        vej = m_vej.group(1).strip() if m_vej else "Bolig"
        res.append(dict(udlejer="Taurus", url=url, adresse=f"{vej} ({bydel})",
                        postnr=None, husleje=to_int(m_rent.group(1)) if m_rent else None,
                        vaerelser=int(m_rooms.group(1)) if m_rooms else None, m2=None,
                        status="", ledig_fra=""))
    return res


def scrape_wanamove(page):
    res, seen = [], set()
    for url in URLS["Wanamove"]:
        try:
            goto(page, url)
            page.wait_for_selector("article.m-productlist-item", timeout=15000)
        except Exception as e:
            log("Wanamove: ingen boliger på", url, e.__class__.__name__)
            continue
        arts = page.locator("article.m-productlist-item")
        for i in range(arts.count()):
            a = arts.nth(i)
            try:
                link = a.locator("a.m-productlist-link").first.evaluate("e => e.href")
                if link in seen:
                    continue
                seen.add(link)
                title = a.locator(".m-productlist-title").first.inner_text().strip()
                txt = a.inner_text()
                soldout = a.locator(".m-productlist-soldout").count() > 0 or "udsolgt" in txt.lower()
            except Exception:
                continue
            m_rent = re.search(r"([\d\.]{4,}),-\s*(?:/\s*md|pr)", txt, re.I)
            m_aconto = re.search(r"a conto[^\d]*([\d\.]+)", txt, re.I)
            m_rooms = re.search(r"(\d+)\s*vær", txt, re.I) or re.match(r"(\d)V\b", title)
            m_m2 = re.search(r"(\d+)\s*kvm", txt, re.I)
            m_frist = re.search(r"frist\s*(\d{4}-\d{2}-\d{2})", txt, re.I)
            m_billet = re.search(r"(\d+,\d{2})\s*DKK", txt)
            low = (title + " " + txt).lower()
            bydel = next((b for b in CFG.get("wanamove_bydele", []) if b.lower() in low), None)
            if not bydel and any(b in low for b in ANDRE_BYDELE):
                bydel = "UDENFOR"
            ekstra = []
            if m_aconto:
                ekstra.append(f"a conto {m_aconto.group(1)} kr")
            if m_frist:
                ekstra.append(f"ansøgningsfrist {m_frist.group(1)}")
            if m_billet:
                ekstra.append(f"ansøgning koster {m_billet.group(1)} kr")
            res.append(dict(udlejer="Wanamove", url=link, adresse=title, postnr=None,
                            husleje=to_int(m_rent.group(1)) if m_rent else None,
                            vaerelser=int(m_rooms.group(1)) if m_rooms else None,
                            m2=int(m_m2.group(1)) if m_m2 else None,
                            status="Udsolgt" if soldout else "", ledig_fra="",
                            bydel=bydel, ekstra=", ".join(ekstra)))
    return res


def enrich_rooms_from_detail(page, item):
    """Cityapartment-kort viser ikke altid antal værelser – læs det fra boligsiden."""
    try:
        goto(page, item["url"])
        txt = page.inner_text("body")
        m = re.search(r"(\d+)\s*-\s*værelses", txt, re.I) or re.search(r"(\d+)\s*værelser", txt, re.I)
        if m:
            item["vaerelser"] = int(m.group(1))
        if re.search(r"\budlejet\b", txt, re.I) and not item["status"]:
            item["status"] = "Udlejet?"
    except Exception as e:
        log("kunne ikke læse detaljer", item["url"], e)


def enrich_taurus(page, item):
    """Hent fuld adresse, postnr. og kontaktperson fra Taurus' boligside. Returnér kontakt-tekst."""
    try:
        goto(page, item["url"])
        txt = page.inner_text("body")
        m = re.search(r"([A-ZÆØÅ][A-Za-zÆØÅæøåéü'\. ]{2,40}\s\d+[A-Za-z]?(?:,\s*[^\n,]{1,20})?)\s*,?\s*\n?\s*"
                      r"(\d{4})\s+(København|Frederiksberg)", txt)
        if m:
            item["adresse"] = m.group(1).strip()
            item["postnr"] = int(m.group(2))
        mails = re.findall(r"[\w\.-]+@taurus\.dk", txt)
        mail = next((x for x in mails if not x.lower().startswith(("info", "udlejning", "kundeservice"))),
                    mails[0] if mails else None)
        if not mail:
            return None
        navn = None
        i = txt.find(mail)
        before = [l.strip() for l in txt[max(0, i - 300):i].splitlines() if l.strip()]
        for l in reversed(before):
            if re.fullmatch(r"[A-ZÆØÅ][a-zæøåé]+(?: [A-ZÆØÅ][a-zæøåé\-]+){1,3}", l):
                navn = l
                break
        return f"{navn} ({mail})" if navn else mail
    except Exception as e:
        log("kunne ikke læse Taurus-side", item["url"], e)
        return None


# ---------------------------------------------------------------- forms

def find_form(page, key_texts):
    forms = page.locator("form")
    for i in range(forms.count()):
        f = forms.nth(i)
        try:
            t = f.inner_text(timeout=2000).lower()
        except Exception:
            continue
        if any(k.lower() in t for k in key_texts):
            return f
    return None


def fill_field(scope, labels, value, kind="text"):
    """Udfyld et felt ud fra synlig label/placeholder/name. Returnér True hvis det lykkedes."""
    for lab in labels:
        cands = [scope.get_by_label(lab, exact=False), scope.get_by_placeholder(lab, exact=False)]
        key = re.sub(r"[^a-z]", "", lab.lower())[:5]
        if key:
            cands.append(scope.locator(f'[name*="{key}" i], [id*="{key}" i]'))
        for c in cands:
            try:
                for j in range(c.count()):
                    el = c.nth(j)
                    if not el.is_visible():
                        continue
                    tag = el.evaluate("e => e.tagName.toLowerCase()")
                    if kind == "select" or tag == "select":
                        if tag != "select":
                            continue
                        el.select_option(label=value, timeout=3000)
                    else:
                        if tag not in ("input", "textarea"):
                            continue
                        el.fill(value, timeout=3000)
                    return True
            except Exception:
                continue
    return False


def check_consent(scope):
    boxes = scope.locator('input[type="checkbox"]')
    n = boxes.count()
    for i in range(n):
        b = boxes.nth(i)
        try:
            label = b.evaluate("""(e)=>{const l=e.closest('label')||document.querySelector(`label[for="${e.id}"]`);
                                   return (l?l.innerText:(e.parentElement||{}).innerText||'')}""").lower()
        except Exception:
            label = ""
        if n == 1 or any(w in label for w in ("persondata", "accepter", "samtykke", "betingelser", "privatliv", "gdpr")):
            try:
                if not b.is_checked():
                    b.check(force=True, timeout=3000)
            except Exception:
                pass


def visible_captcha(page):
    """Sand kun for en CAPTCHA man skal klikke på. Usynlig reCAPTCHA (size=invisible / v3) tæller ikke."""
    c = page.locator(CAPTCHA_SEL)
    for i in range(c.count()):
        el = c.nth(i)
        try:
            src = (el.get_attribute("src") or "")
            if "size=invisible" in src:
                continue
            box = el.bounding_box()
            if el.is_visible() and box and box["width"] > 60 and box["height"] > 40:
                return True
        except Exception:
            continue
    return False


def submit_form(page, scope):
    if visible_captcha(page):
        return "captcha", "Formularen har en CAPTCHA, man skal klikke på – ikke sendt."
    btn = scope.locator('button[type="submit"], input[type="submit"]')
    if not btn.count():
        btn = scope.get_by_role("button", name=re.compile(r"send|book|tilmeld", re.I))
    if not btn.count():
        return "fejl", "Fandt ingen send-knap."
    before = page.inner_text("body").lower()
    btn.first.click(timeout=5000)
    page.wait_for_timeout(6000)
    if page.locator(CAPTCHA_CHALLENGE_SEL).count() and page.locator(CAPTCHA_CHALLENGE_SEL).first.is_visible():
        return "captcha", "Der kom en CAPTCHA-udfordring efter tryk på send."
    after = page.inner_text("body").lower()
    if re.search(r"recaptcha.{0,40}(mislykkedes|failed)|submission failed|uautoriseret", after):
        return "captcha", "Siden afviste tilmeldingen (reCAPTCHA) – tilmeld dig selv via linket."
    if any(w in after and w not in before for w in SUCCESS_WORDS):
        return "tilmeldt", "Formularen er sendt, og siden bekræftede det."
    return "usikker", "Formularen er sendt, men siden viste ingen tydelig bekræftelse – tjek skærmbilledet."


def dansk_nummer(tlf):
    """Kereby har landekoden i et felt for sig (+45), så nummeret skal stå uden 45/+45 foran."""
    d = re.sub(r"\D", "", tlf or "")
    if d.startswith("0045"):
        d = d[4:]
    elif d.startswith("45") and len(d) == 10:
        d = d[2:]
    return d


# Udsagn, botten må bekræfte hos Kereby. Hele sætningen skal passe, så et udsagn med
# ekstra betingelser (fx krav om indkomst) ikke bliver bekræftet ved et uheld.
KEREBY_UDSAGN = [
    ("ikke_rki", r"jeg bekræfter,? at jeg ikke er registreret i rki\.?"),
    ("ingen_kaeledyr", r"jeg bekræfter,? at jeg ikke har (?:nogen |nogle )?(?:kæledyr|husdyr)\.?"),
    ("max_personer", r"jeg bekræfter,? at der ikke skal bo mere end (\d+) personer? i lejemålet\.?"),
]


def kereby_svar(tekst):
    """True hvis botten må bekræfte udsagnet for Mikkel."""
    t = re.sub(r"\s+", " ", tekst).strip().lower()
    tilladt = CFG.get("kereby_bekraeft", {}) or {}
    for key, pattern in KEREBY_UDSAGN:
        m = re.fullmatch(pattern, t)
        if m:
            if not tilladt.get(key):
                return False
            if key == "max_personer":
                return ME["antal_beboere"] <= int(m.group(1))
            return True
    return False


def book_kereby(page, item, shot_base):
    """Kerebys "Book fremvisning" er en guide i 3 trin: dato og tid, kontaktoplysninger og
    bekræftelser. Botten vælger den første ledige dato og det første tidspunkt."""
    shots = []

    def shot(navn):
        p = f"{shot_base}-{navn}.png"
        try:
            panel = page.locator(".jorato-case-detail__booking-panel").first
            if panel.is_visible():
                panel.screenshot(path=p)
            else:
                page.screenshot(path=p)
        except Exception:
            page.screenshot(path=p)
        shots.append(p)

    modal = page.locator("[data-jorato-booking-modal]").first
    if not page.locator("[data-jorato-booking-modal]").count():
        page.screenshot(path=f"{shot_base}-side.png")
        return "fejl", "Fandt ikke Kerebys bookingvindue – siden har måske ændret sig. Ikke sendt.", [f"{shot_base}-side.png"]
    openers = page.locator("[data-jorato-booking-open]")
    for i in range(openers.count()):
        if openers.nth(i).is_visible():
            openers.nth(i).click(timeout=5000)
            break
    else:
        return "fejl", "Fandt ingen synlig \"Book fremvisning\"-knap. Ikke sendt.", shots
    modal.wait_for(state="visible", timeout=10000)

    # Trin 1: første ledige dato (hverdage fra om 2 dage) og første tidspunkt
    cal = modal.locator("[data-jorato-booking-calendar]")
    dag = None
    for _ in range(3):
        days = cal.locator("td:not(.is-disabled):not(.is-empty) button.pika-day")
        if days.count():
            dag = days.first
            break
        nxt = cal.locator("button.pika-next")
        if not nxt.count():
            break
        nxt.first.click(timeout=3000)
        page.wait_for_timeout(400)
    if dag is None:
        shot("kalender")
        return "fejl", "Kalenderen viste ingen ledige datoer de næste 3 måneder. Ikke sendt.", shots
    dato = "{}/{}/{}".format(dag.get_attribute("data-pika-day"), int(dag.get_attribute("data-pika-month")) + 1,
                             dag.get_attribute("data-pika-year"))
    dag.click(timeout=3000)  # rigtigt museklik – kalenderen reagerer ikke på JavaScript-klik
    pill = modal.locator("[data-jorato-booking-time-pills] button").first
    tid = pill.inner_text().strip()
    pill.click(timeout=3000)
    tidspunkt = f"{dato} kl. {tid}"
    next1 = modal.locator('[data-jorato-booking-next="1"]')
    page.wait_for_timeout(300)
    if not next1.is_enabled():
        shot("kalender")
        return "fejl", f"Kunne ikke vælge {tidspunkt} i kalenderen. Ikke sendt.", shots
    next1.click(timeout=5000)
    modal.locator('[data-jorato-booking-step="2"]').wait_for(state="visible", timeout=5000)

    # Trin 2: kontaktoplysninger
    tlf = dansk_nummer(ME["telefon"])
    if not (ME["navn"] and ME["email"] and tlf):
        return "fejl", "Navn, email eller telefonnummer mangler i GitHub Secrets. Ikke sendt.", shots
    modal.locator("[data-jorato-booking-name]").fill(ME["navn"], timeout=5000)
    modal.locator("[data-jorato-booking-email]").fill(ME["email"], timeout=5000)
    try:
        modal.locator("[data-jorato-booking-extension]").select_option("45", timeout=3000)
    except Exception:
        pass
    modal.locator("[data-jorato-booking-phone]").fill(tlf, timeout=5000)
    try:
        modal.locator("[data-jorato-booking-language]").select_option("danish", timeout=3000)
    except Exception:
        pass
    if ME["besked"]:
        modal.locator("[data-jorato-booking-comment]").fill(ME["besked"][:500], timeout=5000)
    # Siden tegner knapperne forfra, når et felt mister fokus. Sker det midt i et klik, bliver
    # klikket væk. Derfor flytter vi fokus væk fra felterne først.
    page.evaluate("() => document.activeElement && document.activeElement.blur && document.activeElement.blur()")
    # Afkrydsningsfeltet er skjult bag en pæn grafik, og etiketten indeholder et link,
    # så vi sætter flueben direkte på feltet.
    modal.locator("[data-jorato-booking-privacy]").evaluate("e => { if (!e.checked) e.click(); }")
    page.wait_for_timeout(500)

    knap2 = modal.locator("[data-jorato-booking-actions-step2] button.jorato-case-detail__booking-btn--primary").first
    if not knap2.is_enabled():
        shot("udfyldt")
        return "fejl", "Kereby godtog ikke kontaktoplysningerne (knappen blev ikke aktiv). Ikke sendt.", shots
    bekraeftet = []
    if "næste" in knap2.inner_text().lower():
        # Trin 3: udsagn der skal bekræftes
        trin3 = modal.locator('[data-jorato-booking-step="3"]')
        for _ in range(3):  # klikket kan blive væk, hvis knappen tegnes forfra imens
            modal.locator("[data-jorato-booking-actions-step2] button.jorato-case-detail__booking-btn--primary").first.click(timeout=5000)
            try:
                trin3.wait_for(state="visible", timeout=2000)
                break
            except PWTimeout:
                continue
        if not trin3.is_visible():
            shot("udfyldt")
            return "fejl", "Kom ikke videre til Kerebys bekræftelsestrin. Ikke sendt.", shots
        spm = modal.locator("[data-jorato-booking-questions] .jorato-case-detail__booking-question")
        tekster = [spm.nth(i).locator("p").inner_text().strip() for i in range(spm.count())]
        ukendte = [t for t in tekster if not kereby_svar(t)]
        if ukendte:
            shot("spoergsmaal")
            return ("spoergsmaal", "Kereby beder dig bekræfte noget, botten ikke må svare på for dig: "
                    + " / ".join(ukendte) + " Tilmeld dig selv via linket, hvis det passer.", shots)
        for i in range(spm.count()):
            spm.nth(i).locator('input[type="checkbox"]').evaluate("e => { if (!e.checked) e.click(); }")
        bekraeftet = tekster
        page.wait_for_timeout(400)
        knap = modal.locator("[data-jorato-booking-actions-step3] button.jorato-case-detail__booking-btn--primary").first
    else:
        knap = knap2

    knaptekst = knap.inner_text().strip()
    shot("udfyldt")
    if "bestil" not in knaptekst.lower():
        return "fejl", f"Fandt ikke \"Bestil fremvisning\"-knappen (fandt \"{knaptekst}\"). Ikke sendt.", shots
    if not knap.is_enabled():
        return "fejl", "\"Bestil fremvisning\" blev ikke aktiv – noget blev ikke godtaget. Ikke sendt.", shots
    noter = f" Bekræftet for dig: {' '.join(bekraeftet)}" if bekraeftet else ""
    if DRY_RUN:
        return "proeve", f"Prøvekørsel: alt er udfyldt til {tidspunkt}, men IKKE sendt.{noter}", shots

    rest = page.evaluate("() => ((window.joratoTemplatesCaseDetail || {}).bookingRestUrl || '')") or ""
    rest_sti = rest.split("?")[0]

    def er_booking(r):
        if r.request.method != "POST":
            return False
        return r.url.split("?")[0] == rest_sti if rest_sti else "/booking" in r.url

    try:
        with page.expect_response(er_booking, timeout=30000) as info:
            knap.click(timeout=5000)
        svar = info.value
        try:
            data = svar.json()
        except Exception:
            data = {}
        data = data if isinstance(data, dict) else {}
    except PWTimeout:
        page.wait_for_timeout(2000)
        shot("efter")
        return ("usikker", f"Trykkede \"Bestil fremvisning\" ({tidspunkt}), men fik intet svar fra Kereby – "
                "tjek skærmbilledet, og tilmeld dig selv, hvis du er i tvivl.", shots)
    page.wait_for_timeout(1500)
    shot("efter")
    if svar.ok and data.get("ok"):
        return ("tilmeldt", f"Fremvisning bestilt til {tidspunkt} (første ledige tid). "
                f"Kereby vender tilbage og bekræfter tiden.{noter}", shots)
    fejltekst = str(data.get("message") or "").strip()
    if not fejltekst:
        try:
            el = modal.locator("[data-jorato-booking-error]")
            fejltekst = el.inner_text().strip() if el.count() and el.first.is_visible() else ""
        except Exception:
            pass
    return ("fejl", f"Kereby afviste tilmeldingen (HTTP {svar.status})"
            + (f": {fejltekst}" if fejltekst else "") + ". Tilmeld dig selv via linket.", shots)


def book(page, item):
    """Udfyld (og send, hvis ikke dry run) Book fremvisning-formularen."""
    goto(page, item["url"])
    shot_base = SHOTS / re.sub(r"[^a-z0-9]+", "-", item["adresse"].lower()).strip("-")
    SHOTS.mkdir(exist_ok=True)
    if item["udlejer"] == "Kereby":
        return book_kereby(page, item, shot_base)
    # Åbn formularen (Cityapartment: anker #kontakt)
    try:
        t = page.get_by_text(re.compile(r"^\s*Book fremvisning\s*$", re.I))
        for i in range(t.count()):
            if t.nth(i).is_visible():
                t.nth(i).click(timeout=3000)
                page.wait_for_timeout(1500)
                break
    except Exception:
        pass

    scope = find_form(page, ["antal beboere", "hvem skal bo"]) or page
    antal = "1 person" if ME["antal_beboere"] == 1 else f'{ME["antal_beboere"]} personer'
    ok = [fill_field(scope, ["Navn", "Name"], ME["navn"]),
          fill_field(scope, ["Tlf", "Telefon", "Phone"], ME["telefon"]),
          fill_field(scope, ["Email", "E-mail"], ME["email"]),
          fill_field(scope, ["Antal beboere"], antal, kind="select"),
          fill_field(scope, ["Hvem skal bo i lejemålet", "Hvem skal bo"], ME["hvem"], kind="select")]
    if ME["besked"]:
        fill_field(scope, ["Besked", "Kommentar", "Message"], ME["besked"])
    check_consent(scope)

    try:
        (scope if scope is not page else page).screenshot(path=f"{shot_base}-udfyldt.png")
    except Exception:
        page.screenshot(path=f"{shot_base}-udfyldt.png", full_page=True)
    shots = [f"{shot_base}-udfyldt.png"]

    if not all(ok):
        return "fejl", "Kunne ikke finde alle felterne i formularen – ikke sendt.", shots
    if DRY_RUN:
        return "proeve", "Prøvekørsel: formularen er udfyldt men IKKE sendt.", shots
    status, msg = submit_form(page, scope)
    page.screenshot(path=f"{shot_base}-efter.png", full_page=False)
    shots.append(f"{shot_base}-efter.png")
    return status, msg, shots


# ---------------------------------------------------------------- mail

def send_mail(subject, rows_html, rows_txt, attachments=()):
    if not (MAIL_USER and MAIL_PASS):
        log("Mail ikke konfigureret; ville sende:", subject, "\n", rows_txt)
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, MAIL_USER, MAIL_TO
    msg.set_content(rows_txt)
    msg.add_alternative(f'<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">{rows_html}</div>',
                        subtype="html")
    for p in attachments:
        try:
            msg.add_attachment(Path(p).read_bytes(), maintype="image", subtype="png", filename=Path(p).name)
        except Exception:
            pass
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(MAIL_USER, MAIL_PASS)
        s.send_message(msg)
    log("Mail sendt:", subject)


def fmt(item):
    parts = [item["udlejer"]]
    if item.get("postnr"):
        parts.append(str(item["postnr"]))
    if item.get("vaerelser"):
        parts.append(f'{item["vaerelser"]} vær.')
    if item.get("m2"):
        parts.append(f'{item["m2"]} m²')
    if item.get("husleje"):
        parts.append(f'{item["husleje"]:,} kr./md.'.replace(",", "."))
    if item.get("ledig_fra"):
        parts.append(f'ledig {item["ledig_fra"]}')
    return ", ".join(parts)


# ---------------------------------------------------------------- main

def main():
    state = load_state()
    L = state["listings"]
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    report = []      # (item, status, besked)
    shots_all = []
    errors = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(locale="da-DK", viewport={"width": 1280, "height": 1600},
                                  user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        page = ctx.new_page()

        listings = []
        for name, fn in [("Kereby", scrape_kereby), ("Cityapartment", scrape_city), ("Taurus", scrape_taurus),
                         ("Wanamove", scrape_wanamove)]:
            try:
                got = fn(page)
                log(f"{name}: {len(got)} boliger fundet")
                if not got and name != "Taurus":
                    errors.append(f"{name}: fandt 0 boliger – siden har måske ændret sig.")
                listings += got
            except Exception as e:
                errors.append(f"{name}: {e.__class__.__name__}: {e}")
                traceback.print_exc()

        booked = 0
        for item in listings:
            key = item["url"]
            prev = L.get(key, {})
            first_seen = key not in L
            L.setdefault(key, {"fundet": now, **{k: item[k] for k in ("udlejer", "adresse")}})

            if item["udlejer"] == "Wanamove":
                if not first_seen or item["status"]:
                    if item["status"] and first_seen:
                        L[key]["status"] = "udsolgt"
                    continue
                L[key]["status"] = "set"
                if item["bydel"] == "UDENFOR":
                    continue
                if item["husleje"] and item["husleje"] > CFG["max_husleje"]:
                    continue
                if item["vaerelser"] and item["vaerelser"] < CFG["min_vaerelser"]:
                    continue
                L[key]["status"] = "mail"
                note = "Man skriver sig ikke op hos Wanamove – køb en ansøgning på boligsiden."
                if not item["bydel"]:
                    note += " Bydelen kunne ikke genkendes, så tjek selv om den passer."
                if item["ekstra"]:
                    note = item["ekstra"].capitalize() + ". " + note
                report.append((item, "wanamove", note))
                continue

            if item["udlejer"] == "Taurus":
                if not first_seen:
                    continue
                L[key]["status"] = "set"
                if item["husleje"] and item["husleje"] > CFG["max_husleje"]:
                    continue
                if item["vaerelser"] and item["vaerelser"] < CFG["min_vaerelser"]:
                    continue
                kontakt = enrich_taurus(page, item)
                L[key].update(adresse=item["adresse"], status="mail")
                report.append((item, "taurus", "Taurus har ingen formular – skriv til " +
                               (kontakt or "kontaktpersonen på boligsiden") + "."))
                continue

            if item["status"]:
                continue  # udlejet / reserveret
            if not postnr_ok(item["postnr"]):
                continue
            if item["husleje"] is None or item["husleje"] > CFG["max_husleje"]:
                continue
            if item["vaerelser"] is None:
                enrich_rooms_from_detail(page, item)
            if item["status"]:
                continue
            if prev.get("status") in ("tilmeldt", "usikker", "captcha", "fejl", "spoergsmaal"):
                continue  # allerede forsøgt i en rigtig kørsel
            if DRY_RUN and prev.get("status") == "proeve":
                continue
            if item["vaerelser"] is None:
                if prev.get("status") != "ukendt_vaer":
                    L[key]["status"] = "ukendt_vaer"
                    report.append((item, "ukendt", "Antal værelser kunne ikke læses – ikke tilmeldt automatisk."))
                continue
            if item["vaerelser"] < CFG["min_vaerelser"]:
                continue
            if not CFG.get("auto_tilmelding", {}).get(item["udlejer"], True):
                if prev.get("status") != "mail":
                    L[key].update(status="mail", forsoegt=now)
                    report.append((item, "selv", f"{item['udlejer']} afviser automatisk tilmelding – "
                                   "klik på linket og tryk \"Book fremvisning\" hurtigst muligt."))
                continue
            if booked >= CFG["max_tilmeldinger_pr_koersel"]:
                log("Sikkerhedsgrænse nået; resten tages i næste kørsel")
                break

            log("Tilmelder:", item["adresse"], item["url"])
            try:
                status, msg, shots = book(page, item)
            except Exception as e:
                status, msg, shots = "fejl", f"{e.__class__.__name__}: {e}", []
                traceback.print_exc()
            booked += 1
            L[key].update(status=status, forsoegt=now)
            report.append((item, status, msg))
            shots_all += shots

        browser.close()

    if report:
        labels = {"tilmeldt": "✅ Tilmeldt fremvisning", "usikker": "⚠️ Sendt – ikke bekræftet",
                  "proeve": "🧪 Prøvekørsel (ikke sendt)", "captcha": "⛔ CAPTCHA – tilmeld dig selv",
                  "fejl": "⛔ Fejl – tilmeld dig selv", "taurus": "📬 Ny hos Taurus",
                  "wanamove": "📬 Ny hos Wanamove",
                  "selv": "🔔 Ny – tilmeld dig selv nu",
                  "ukendt": "❔ Ny – tjek selv",
                  "spoergsmaal": "⛔ Kereby spørger om noget – tilmeld dig selv"}
        n_ok = sum(1 for _, s, _ in report if s == "tilmeldt")
        n_selv = sum(1 for _, s_, _ in report if s_ in ("selv", "captcha", "fejl", "usikker", "spoergsmaal"))
        subject = (f"Boligbot: tilmeldt {n_ok} fremvisning(er)" if n_ok else
                   f"Boligbot: ny bolig – tilmeld dig selv nu ({report[0][0]['adresse']})" if n_selv else
                   f"Boligbot: {len(report)} ny(e) bolig(er)") + (" [prøvekørsel]" if DRY_RUN else "")
        html, txt = [], []
        for it, s, m in report:
            html.append(f'<p><b>{labels.get(s, s)}</b><br><a href="{escape(it["url"])}">{escape(it["adresse"])}</a>'
                        f'<br>{escape(fmt(it))}<br><i>{escape(m)}</i></p>')
            txt.append(f'{labels.get(s, s)}\n{it["adresse"]}\n{it["url"]}\n{fmt(it)}\n{m}\n')
        send_mail(subject, "".join(html), "\n".join(txt), shots_all)

    # fejl-mail højst én gang i døgnet pr. fejltype
    for err in errors:
        k = err.split(":")[0]
        last = state["last_error_mail"].get(k)
        if not last or (time.time() - last) > 86400:
            send_mail(f"Boligbot: problem med {k}", f"<p>{escape(err)}</p>", err)
            state["last_error_mail"][k] = time.time()
        log("FEJL:", err)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
