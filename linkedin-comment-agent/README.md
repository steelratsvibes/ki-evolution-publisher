# LinkedIn Comment Agent

Automatischer Agent der LinkedIn-Kommentare erkennt, Antwort-Entwuerfe generiert und per Email zur Freigabe schickt.

## Was macht der Agent?

1. **Neue Kommentare erkennen** -- Prueft taeglich die letzten 5 LinkedIn-Posts auf neue Kommentare (via Apify Scraper)
2. **Antworten generieren** -- GPT-4o-mini erstellt Antwort-Entwuerfe in Ronalds Stimme
3. **Approval per Email** -- Du bekommst eine Email mit allen Entwuerfen
4. **Freigabe** -- Du antwortest:
   - `OK` -- alle Antworten werden gepostet
   - `OK 1,3` -- nur Antwort 1 und 3 werden gepostet
   - `NEIN 2 zu formell` -- Antwort 2 wird abgelehnt, neue Version wird generiert
5. **Posten** -- Freigegebene Antworten werden automatisch auf LinkedIn gepostet

## Setup

### Voraussetzungen
- Python 3.10+
- LinkedIn Account mit Developer App (w_member_social Scope)
- Apify Account (fuer LinkedIn Scraping)
- OpenAI API Key
- SMTP/IMAP Zugang fuer Email

### Installation
```bash
cd linkedin-comment-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env mit echten Werten fuellen
python3 init_db.py
```

### Verwendung
```bash
# Neue Kommentare holen + Entwuerfe generieren + Email senden
python3 comment_agent.py

# Approval-Inbox pruefen + freigegebene Antworten posten
python3 comment_agent.py --check-approvals
```

### Automatisierung (systemd-Timer auf VPS)
```bash
# Taeglich 09:00 CET: neue Kommentare
linkedin-comments.timer

# Alle 30 Min: Approval-Check
linkedin-approval.timer
```

## Architektur

```
Taeglich 09:00
  -> Apify: LinkedIn-Posts + Kommentare scrapen
  -> SQLite: Neue Kommentare erkennen
  -> GPT-4o-mini: Antwort-Entwuerfe generieren
  -> SMTP: Email an Approval-Adresse

Alle 30 Min
  -> IMAP: Approval-Emails pruefen
  -> LinkedIn API: Freigegebene Antworten posten
```

## Dateien
| Datei | Beschreibung |
|-------|-------------|
| comment_agent.py | Hauptscript (Posts holen, Kommentare, Antworten, Email, Approval) |
| init_db.py | SQLite-Datenbank initialisieren |
| get_token.py | LinkedIn OAuth Token Generator |
| requirements.txt | Python-Abhaengigkeiten |
| .env.example | Vorlage fuer Konfiguration |
