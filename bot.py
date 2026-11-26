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
}

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

def scrape_kereby(page):
    goto(page, URLS["Kereby"])
    res = []
    for url, txt in collect_cards(page, "/bolig/").items():
        lines = [l for l in txt.splitlines() if l.strip()]
        status = "Udlejet" if re.search(r"\budlejet\b", txt, re.I) else (
            "Reserveret" if re.search(r"\breserveret\b", txt, re.I) else "")
        m_pn = re.search(r"-(\d{4})-", url) or re.search(r"\b(\d{4})\s+(?:København|Frederiksberg|Hellerup|Valby)", txt)
        m_rent = re.search(r"([\d\.]{4,})\s*kr\.?\s*/\s*md", txt, re.I)
        m_rooms = re.search(r"(\d+)\s*værelser", txt, re.I) or re.search(r"(\d+)-værelses", txt, re.I)
        m_m2 = re.search(r"(\d+)\s*m(?:²|2)\b", txt)
        res.append(dict(udlejer="Kereby", url=url, adresse=guess_address(lines, url),
                        postnr=to_int(m_pn.group(1)) if m_pn else None,
                        husleje=to_int(m_rent.group(1)) if m_rent else None,
                        vaerelser=int(m_rooms.group(1)) if m_rooms else None,
                        m2=int(m_m2.group(1)) if m_m2 else None, status=status, ledig_fra=""))
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
    if any(w in after and w not in before for w in SUCCESS_WORDS):
        return "tilmeldt", "Formularen er sendt, og siden bekræftede det."
    return "usikker", "Formularen er sendt, men siden viste ingen tydelig bekræftelse – tjek skærmbilledet."


def book(page, item):
    """Udfyld (og send, hvis ikke dry run) Book fremvisning-formularen."""
    goto(page, item["url"])
    shot_base = SHOTS / re.sub(r"[^a-z0-9]+", "-", item["adresse"].lower()).strip("-")
    # Åbn formularen (Kereby: knap/modal, Cityapartment: anker #kontakt)
    try:
        t = page.get_by_text(re.compile(r"^\s*Book fremvisning\s*$", re.I))
        for i in range(t.count()):
            if t.nth(i).is_visible():
                t.nth(i).click(timeout=3000)
                page.wait_for_timeout(1500)
                break
    except Exception:
        pass

    if item["udlejer"] == "Kereby":
        scope = find_form(page, ["telefon", "sprog"]) or page
        ok = [fill_field(scope, ["Navn", "Fulde navn", "Name"], ME["navn"]),
              fill_field(scope, ["Email", "E-mail"], ME["email"]),
              fill_field(scope, ["Telefonnummer", "Telefon", "Tlf", "Phone"], ME["telefon"])]
        fill_field(scope, ["Sprog", "Language"], "Dansk", kind="select")
        if ME["besked"]:
            fill_field(scope, ["Kommentar", "Besked", "Comment"], ME["besked"])
    else:
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

    SHOTS.mkdir(exist_ok=True)
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
        for name, fn in [("Kereby", scrape_kereby), ("Cityapartment", scrape_city), ("Taurus", scrape_taurus)]:
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
            if prev.get("status") in ("tilmeldt", "usikker", "captcha", "fejl"):
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
                  "ukendt": "❔ Ny – tjek selv"}
        n_ok = sum(1 for _, s, _ in report if s == "tilmeldt")
        subject = (f"Boligbot: tilmeldt {n_ok} fremvisning(er)" if n_ok else
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
