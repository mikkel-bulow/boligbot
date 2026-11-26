# Boligbot

Tjekker Kereby, Cityapartment og Taurus cirka hvert 5. minut. Hvis der kommer en ledig bolig, der passer til dine kriterier, skriver den dig automatisk op til fremvisning hos Kereby og Cityapartment. Bagefter sender den dig en mail med skærmbilleder. Hos Taurus får du kun en mail, fordi Taurus ikke har en formular. Den kører hos GitHub, så din computer kan godt være slukket.

Kriterierne står i `config.yaml`:

- **Områder:** K og Christianshavn, V, Frederiksberg, N og Ø
- **Husleje:** højst 15.550 kr./md.
- **Størrelse:** mindst 2 værelser

## Opsætning (ca. 15 min)

### 1. Lav en app-adgangskode til Gmail
Botten sender mails fra din egen Gmail.

1. Slå totrinsbekræftelse til på din Google-konto, hvis den ikke allerede er slået til.
2. Gå til https://myaccount.google.com/apppasswords og opret en adgangskode, fx med navnet "Boligbot".
3. Kopiér den kode på 16 tegn, du får.

Er bulowco.dk en Google Workspace-konto, skal app-adgangskoder være tilladt i admin-konsollen.

### 2. Opret et repository på GitHub
1. Opret en gratis konto på https://github.com.
2. Klik **New repository**, kald det fx `boligbot` og vælg **Public**. Kun offentlige repositories har gratis ubegrænsede kørsler. Dine personlige oplysninger ligger ikke i koden, men som hemmeligheder (se trin 3).
3. Klik **uploading an existing file** og upload alle filerne fra denne mappe, også mappen `.github`. Se tippet nederst, hvis din computer skjuler den.

### 3. Læg dine oplysninger ind som hemmeligheder
Gå til **Settings → Secrets and variables → Actions → New repository secret** og opret én hemmelighed ad gangen:

| Navn | Indhold |
|---|---|
| `BOLIG_NAVN` | Mikkel Bülow |
| `BOLIG_TELEFON` | dit mobilnummer |
| `BOLIG_EMAIL` | den mail, udlejerne skal svare til |
| `BOLIG_ANTAL_BEBOERE` | fx `1` |
| `BOLIG_HVEM` | `Eneboer`, `Par`, `Familie` eller `Bofællesskab` |
| `BOLIG_BESKED` | kort besked til udlejer (valgfri) |
| `GMAIL_USER` | din Gmail-adresse |
| `GMAIL_APP_PASSWORD` | koden fra trin 1 |
| `MAIL_TO` | hvor beskederne skal hen (normalt din egen mail) |

### 4. Kør en prøve
1. Gå til fanen **Actions**, vælg **Boligbot** og klik **Run workflow**.
2. Botten starter i **prøvetilstand** (`dry_run: true`). Den udfylder formularen og sender dig et skærmbillede, men trykker ikke send.
3. Ser skærmbillederne rigtige ud, så åbn `config.yaml`, sæt `dry_run: false` og gem.

Fra da af tilmelder den dig af sig selv. Det gælder også de passende boliger, den fandt i prøvekørslen.

## Godt at vide
- **CAPTCHA og fejl:** Har en formular en CAPTCHA, eller kan felterne ikke findes, sender botten ikke noget. I stedet får du en mail med "⛔ tilmeld dig selv" og linket.
- **Ændringer på siderne:** Hvis en udlejer ændrer sin hjemmeside, så botten finder 0 boliger, får du højst én fejl-mail i døgnet.
- **Sikkerhedsgrænse:** Botten tilmelder højst 3 boliger pr. kørsel.
- **Ændr kriterier:** Ret `config.yaml` direkte på GitHub (blyant-ikonet).
- **Sæt på pause:** Gå til **Actions → Boligbot → ⋯ → Disable workflow**.
- **Forsinkelser:** GitHub kan forsinke planlagte kørsler med 5-15 minutter, når der er travlt.

## Distill.io som ekstra varsling
Distill holder øje med siderne fra deres egne servere og sender en push-besked på telefonen, når de ændrer sig.

1. Opret en konto på https://distill.io og installér browserudvidelsen eller app'en.
2. Åbn https://kereby.dk/bolig/ og klik på Distill-ikonet → **Select parts of page**. Markér listen med boliger og gem.
3. Gør det samme for https://cityapartment.dk/da/lejeboliger-koebenhavn/ og https://www.taurus.dk/boligudlejning/ledige-lejemal/.
4. Sæt hver overvågning til at køre i **Cloud**, så den også kører, når computeren er slukket. Vælg et interval og slå push eller mail til.

Den gratis plan har få cloud-tjek. Vil du have tjek med få minutters mellemrum, kræver det en betalt plan.

## Tip: mappen `.github` er skjult
På Mac trykker du **Cmd + Shift + .** i Finder for at vise skjulte mapper. Du kan også oprette filen direkte på GitHub: **Add file → Create new file**, skriv `.github/workflows/boligbot.yml` som navn og indsæt indholdet.
