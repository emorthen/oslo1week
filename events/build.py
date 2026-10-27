#!/usr/bin/env python3
"""Henter arrangementer fra Oslo-scener og bygger en statisk index.html.

Kjør:  python3 build.py
Viser i dag + 7 dager frem, gruppert per dag.
"""
import html
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")
DAYS_AHEAD = 7
OUT = Path(__file__).parent / "index.html"
UA = {"User-Agent": "Mozilla/5.0 (events-page for personal use)"}

def get(url, as_json=False):
    with urlopen(Request(url, headers=UA), timeout=30) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def group_from(text, default):
    t = text.lower()
    if re.search(r"stand-?up|comedy|humor|kabaret|improv", t):
        return "humor"
    if re.search(r"teater|theat|play|opera|ballett|musikal|dans|framsyn|forestilling", t):
        return "scene"
    if re.search(r"konsert|concert|music|rock|pop|jazz|blues|hiphop|hip-hop|metal|punk|soul|"
                 r"folk|elektronisk|dj|klassisk|singer|country|reggae", t):
        return "musikk"
    return default


def ev(start, title, venue, url, kind="", note="", image="", group="annet"):
    """start: datetime i Oslo-tid, eller date (ukjent klokkeslett)."""
    return {"start": start, "title": html.unescape(title).strip(), "venue": venue.strip(),
            "url": url, "kind": kind, "note": note, "image": image, "group": group}


def from_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(OSLO)


def first(*vals):
    return next((v for v in vals if v), "")


# --- Kilder -----------------------------------------------------------------

def broadcast_events(host, default_venue):
    """Rockefeller og Herr Nilsen deler samme plattform."""
    out = []
    for e in get(f"https://{host}/api/eventsEdge?", True):
        if (e.get("custom_fields") or {}).get("published") is False:
            continue
        tags = [t for t in e.get("tags", []) if t]
        img = first((e.get("photo") or {}).get("urls", {}).get("small"), (e.get("imagekit") or {}).get("url"))
        out.append(ev(from_utc(e["start_time"]), e["name"], (e.get("place") or {}).get("name", default_venue),
                      f"https://{host}/events/e/{e['id']}", ", ".join(tags[:2]), image=img,
                      group=group_from(" ".join(tags), "musikk")))
    return out


def rockefeller():
    return broadcast_events("www.rockefeller.no", "Rockefeller")


def herr_nilsen():
    return broadcast_events("www.herrnilsen.no", "Herr Nilsen")


def salt():
    url = ("https://demo.broadcastapp.no/api/layoutWidgetCors?limit=200&venue=0bDKm3RN9l"
           "&recommended=false&hostname=www.salted.no&city=")
    out = []
    for e in get(url, True)["results"]:
        cf = e.get("custom_fields") or {}
        if cf.get("published") is False:
            continue
        kind = ", ".join(x for x in [e.get("type")] + list(e.get("tags") or []) if x)[:60]
        note = "utsolgt" if cf.get("soldOut") else "avlyst" if cf.get("eventStatus") == "CANCELLED" else ""
        out.append(ev(from_utc(e["start_time"]), e["name"], "SALT", cf.get("ticketUrl") or "https://www.salted.no/program",
                      kind, note, (e.get("imagekitRef") or {}).get("url", ""), group_from(kind, "musikk")))
    return out


def local_env():
    """Leser KEY=verdi fra events/.env (ligger i .gitignore)."""
    try:
        lines = (Path(__file__).parent / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    return dict(l.split("=", 1) for l in lines if "=" in l and not l.lstrip().startswith("#"))


def becco():
    # Widgeten på becco.no/program/ henter fra Broadcast med en nøkkel. Den ligger ikke i repoet:
    # sett BECCO_KEY som miljøvariabel (GitHub-secret) eller i events/.env lokalt.
    key = os.environ.get("BECCO_KEY") or local_env().get("BECCO_KEY")
    if not key:
        raise RuntimeError("BECCO_KEY mangler")
    url = ("https://demo.broadcastapp.no/api/layoutWidgetCors?limit=200&venue=none&recommended=false"
           f"&hostname=becco.no&city=Oslo&widget=listingsmodal&key={key}")
    out = []
    for e in get(url, True)["results"]:
        cf = e.get("custom_fields") or {}
        if cf.get("published") is False or (e.get("venue") or {}).get("name") != "Becco":
            continue
        kind = ", ".join(x for x in [cf.get("zone")] + list(e.get("tags") or []) if x)[:60]
        note = "utsolgt" if cf.get("soldOut") else "avlyst" if cf.get("eventStatus") == "CANCELLED" else ""
        out.append(ev(from_utc(e["start_time"]), e["name"], "Becco", cf.get("ticketUrl") or "https://becco.no/program/",
                      kind, note, (e.get("imagekitRef") or {}).get("url", ""),
                      group_from(f'{kind} {e["name"]}', "musikk")))
    return out


def konserthus():
    out = []
    for e in get("https://oslokonserthus.no/api/program", True):
        for d in e.get("alledatoer", []):
            status = d.get("billettstatus", {}).get("value", "")
            note = status if status in ("utsolgt", "avlyst", "utsatt") else ""
            kind = ", ".join(e.get("kategori", [])[:2])
            out.append(ev(datetime.fromisoformat(d["start"]).astimezone(OSLO), e["title"], "Oslo Konserthus",
                          e["url"], kind, note, (e.get("bilde") or {}).get("thumb", ""),
                          group_from(kind, "musikk")))
    return out


def parkteatret():
    out = []
    page = get("https://www.parkteatret.no/program")
    for card in page.split('<article class="event-card">')[1:]:
        m = re.search(r'event-card__heading[^>]*><a href="([^"]+)">(.*?)</a>', card, re.S)
        d = re.search(r'--date">\s*(\d\d)\.(\d\d)\.(\d\d)', card)
        if not (m and d):
            continue
        t = re.search(r"Dørene åpner</div>\s*<div[^>]*>\s*(\d\d):(\d\d)", card)
        img = re.search(r'event-card__image"\s+src="([^"]+)"', card)
        day = date(2000 + int(d[3]), int(d[2]), int(d[1]))
        start = datetime(day.year, day.month, day.day, int(t[1]), int(t[2]), tzinfo=OSLO) if t else day
        out.append(ev(start, m[2], "Parkteatret", m[1], "dørene åpner" if t else "",
                      image=img[1] if img else "", group="musikk"))
    return out


def latter():
    out, seen = [], set()
    page = get("https://latter.no/forestillinger")
    for m in re.finditer(r'<a href="(https://latter\.no/forestillinger/[^"?]+\?date=(\d{4})-(\d\d)-(\d\d))[^"]*"'
                         r'(.*?)<h2>(.*?)</h2>', page, re.S):
        key = (m[6], m[2], m[3], m[4])
        if key in seen:
            continue
        seen.add(key)
        img = re.search(r'data-src="([^"]+)"', m[5])
        out.append(ev(date(int(m[2]), int(m[3]), int(m[4])), m[6], "Latter", m[1], "standup",
                      image=img[1] if img else "", group="humor"))
    return out


def nieu():
    out, p = [], 1
    while True:
        try:
            batch = get(f"https://nieu.no/wp-json/wp/v2/arrangement?per_page=100&page={p}", True)
        except Exception:
            break
        for e in batch:
            m = e.get("spectra_custom_meta") or {}
            iso = (m.get("tidspunkt_iso") or [None])[0]
            if not iso:
                continue
            kind = (m.get("type_label") or [""])[0].lower()
            imgs = e.get("uagb_featured_image_src") or {}
            img = (imgs.get("medium_large") or imgs.get("medium") or imgs.get("full") or [""])[0]
            title = e["title"]["rendered"]
            grp = "annet" if re.search(r"quiz|kviss|sport", f"{title} {kind}", re.I) else group_from(kind, "humor")
            out.append(ev(datetime.fromisoformat(iso).astimezone(OSLO), title,
                          (m.get("sted_label") or ["nieu"])[0], e["link"], kind, image=img, group=grp))
        if len(batch) < 100:
            break
        p += 1
    return out


def folketeateret():
    page = get("https://www.folketeateret.no/forestillinger/kalender")
    raw = html.unescape(page[page.index('data-calendar="') + 15:])
    data, _ = json.JSONDecoder().raw_decode(raw)
    prods = data["productions"]
    out = []
    for e in data["events"]:
        p = prods.get(str(e["productionId"]), {})
        h, mnt = e["time"].split(":")
        d = date.fromisoformat(e["date"])
        genre = ", ".join(p.get("genres", [])[:1]).lower()
        out.append(ev(datetime(d.year, d.month, d.day, int(h), int(mnt), tzinfo=OSLO),
                      e.get("title") or p.get("title", "?"), "Folketeateret",
                      f"https://www.folketeateret.no/forestillinger/{p.get('slug', '')}", genre,
                      "utsolgt" if e.get("totalTicketsLeft") == 0 else "", p.get("productionImage", ""),
                      group_from(genre, "scene")))
    return out


def operaen():
    listing = get("https://www.operaen.no/forestillinger/")
    urls = sorted(set(re.findall(r'href="(/forestillinger/[a-z0-9-]+/)"', listing)))

    def one(path):
        url = "https://www.operaen.no" + path
        res = []
        for blob in re.findall(r'<script[^>]*ld(?:\+|&#x2B;)json[^>]*>(.*?)</script>', get(url), re.S):
            try:
                node = json.loads(blob)
            except ValueError:
                continue
            for n in (node if isinstance(node, list) else [node]):
                for g in n.get("@graph", [n]):
                    if "Event" in str(g.get("@type")) and g.get("startDate"):
                        img = g.get("image")
                        res.append(ev(datetime.fromisoformat(g["startDate"]).astimezone(OSLO), g.get("name", path),
                                      "Operaen", url, "opera / ballett",
                                      image=img[0] if isinstance(img, list) and img else img or "", group="scene"))
        return res

    with ThreadPoolExecutor(8) as ex:
        return [e for r in ex.map(one, urls) for e in r]


MONTHS = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august",
          "september", "oktober", "november", "desember"]


def spektrum_dates(text):
    """'Fredag 18. til søndag 20. desember 2026' -> [date, ...], og evt. klokkeslett."""
    t = text.lower()
    m = re.search(r"(" + "|".join(MONTHS) + r")\s+(\d{4})", t)
    if not m:
        return [], None
    nums = [int(x) for x in re.findall(r"(\d{1,2})\.", t[:m.start()])]
    if not nums:
        return [], None
    days = range(nums[0], nums[-1] + 1) if " til " in t[:m.start()] else nums
    month, year = MONTHS.index(m[1]) + 1, int(m[2])
    kl = re.search(r"kl\.?\s*(\d{1,2})[.:](\d{2})", t)
    out = []
    for d in days:
        try:
            out.append(date(year, month, d))
        except ValueError:
            pass
    return out, ((int(kl[1]), int(kl[2])) if kl else None)


def oslo_spektrum():
    page = get("https://oslospektrum.no/")
    out, seen = [], set()
    for b in page.split("b-enkelt-element-arrliste")[1:]:
        title = re.search(r"<h3[^>]*>\s*(.*?)\s*</h3>", b, re.S)
        when = re.search(r'<p class="ord-deling">\s*([^<]*?)\s*</p>', b)
        link = re.search(r'href="(https://oslospektrum\.no/arrangementer/[^"]+)"', b)
        if not (title and when and link):
            continue
        kind = re.search(r'b-grid-arrangementstype">\s*([^<]*?)\s*<', b)
        kind = kind[1] if kind else ""
        img = re.search(r'data-srcset="[^"]*?(\S+-300x\d+\.\w+) 300w', b) or re.search(r'<img[^>]*?src="(https[^"]+)"', b)
        days, tm = spektrum_dates(html.unescape(when[1]))
        note = "utsolgt" if re.search(r"utsolgt", when[1], re.I) and len(days) == 1 else ""
        for d in days:
            if (title[1], d) in seen:
                continue
            seen.add((title[1], d))
            start = datetime(d.year, d.month, d.day, *tm, tzinfo=OSLO) if tm else d
            out.append(ev(start, title[1], "Oslo Spektrum", link[1], kind.lower(), note,
                          img[1] if img else "", group_from(kind, "musikk")))
    return out


def det_norske_teatret():
    page = get("https://www.detnorsketeatret.no/framsyningar")
    raw = html.unescape(page[page.index('plays="') + 7:])
    plays, _ = json.JSONDecoder().raw_decode(raw[1:] if raw.startswith("{[") else raw)
    out = []
    for p in plays:
        img = ((p.get("image") or {}).get("img") or {}).get("src", "")
        for e in p.get("events", []):
            kind = {"play": "teater", "festival": "festival", "insight": "innsikt"}.get(p.get("type"), p.get("type", ""))
            note = "utsolgt" if e.get("soldOut") else ""
            hall = e.get("hallName")
            out.append(ev(from_utc(e["startDate"]), e.get("title") or p["name"], "Det Norske Teatret",
                          e.get("link") or p.get("link", ""), f"{hall}, {kind}" if hall else kind, note, img,
                          "scene" if p.get("type") == "play" else "annet"))
    return out


# (navn, funksjon, metode, kilde-URL)
SOURCES = [
    ("Rockefeller", rockefeller, "skjult API", "https://www.rockefeller.no/api/eventsEdge"),
    ("Herr Nilsen", herr_nilsen, "skjult API", "https://www.herrnilsen.no/api/eventsEdge"),
    ("SALT", salt, "widget-API", "https://www.salted.no/program"),
    ("Becco", becco, "widget-API", "https://becco.no/program/"),
    ("Latter", latter, "HTML", "https://latter.no/forestillinger"),
    ("Nieu", nieu, "WordPress-API", "https://nieu.no/wp-json/wp/v2/arrangement"),
    ("Parkteatret", parkteatret, "HTML", "https://www.parkteatret.no/program"),
    ("Oslo Konserthus", konserthus, "API", "https://oslokonserthus.no/api/program"),
    ("Oslo Spektrum", oslo_spektrum, "HTML", "https://oslospektrum.no/"),
    ("Operaen", operaen, "JSON-LD", "https://www.operaen.no/forestillinger/"),
    ("Folketeateret", folketeateret, "innebygd JSON", "https://www.folketeateret.no/forestillinger/kalender"),
    ("Det Norske Teatret", det_norske_teatret, "innebygd JSON", "https://www.detnorsketeatret.no/framsyningar"),
]


# --- Byggelogg (leses live av buildlog.html) ---------------------------------

LOG_JS = Path(__file__).parent / "buildlog.js"
LOG_PREFIX = "window.BUILDLOG="


class RunLog:
    def __init__(self):
        self.lock = threading.Lock()
        try:
            prev = json.loads(LOG_JS.read_text(encoding="utf-8")[len(LOG_PREFIX):].rstrip(";\n"))["runs"]
        except (OSError, ValueError, KeyError):
            prev = []
        self.prev = prev[:19]
        self.run = {"id": datetime.now(OSLO).strftime("%Y%m%d-%H%M%S"), "started": time.time(),
                    "finished": None, "state": "running", "total": 0, "groups": {}, "sources": [
                        {"name": n, "method": m, "url": u, "state": "queued", "started": None,
                         "duration": None, "fetched": 0, "inwindow": 0, "error": None}
                        for n, _, m, u in SOURCES]}
        self.flush()

    def flush(self):
        data = {"updated": time.time(), "runs": [self.run] + self.prev}
        tmp = LOG_JS.with_suffix(".tmp")
        tmp.write_text(LOG_PREFIX + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")
        tmp.replace(LOG_JS)

    def update(self, name, **kw):
        with self.lock:
            next(s for s in self.run["sources"] if s["name"] == name).update(kw)
            self.flush()

    def finish(self, events, failed):
        with self.lock:
            groups = {}
            for e in events:
                groups[e["group"]] = groups.get(e["group"], 0) + 1
            self.run.update(finished=time.time(), state="error" if failed else "ok",
                            total=len(events), groups=groups)
            self.flush()


# --- Bygg side --------------------------------------------------------------

WEEKDAYS = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]


def day_of(e):
    s = e["start"]
    return s.date() if isinstance(s, datetime) else s


def sort_key(e):
    s = e["start"]
    return (s.hour, s.minute) if isinstance(s, datetime) else (-1, 0)


def day_names(d, today):
    long = f"{WEEKDAYS[d.weekday()]} {d.day}. {MONTHS[d.month - 1]}"
    if d == today:
        return "I dag", "I dag", long
    if d == today + timedelta(days=1):
        return "I morgen", WEEKDAYS[d.weekday()][:3].capitalize(), long
    return long.split(" ")[0].capitalize(), WEEKDAYS[d.weekday()][:3].capitalize(), long


def card(e):
    esc = html.escape
    s = e["start"]
    timed = isinstance(s, datetime)
    t = s.strftime("%H:%M") if timed else "–"
    ts = int(s.timestamp() * 1000) if timed else 0
    meta = " · ".join(x for x in (e["venue"], e["kind"]) if x)
    note = f'<span class="badge badge-{esc(e["note"])}">{esc(e["note"])}</span>' if e["note"] else ""
    img = f'<img src="{esc(e["image"])}" alt="" loading="lazy" onerror="this.remove()">' if e["image"] else ""
    search = esc(f'{e["title"]} {e["venue"]} {e["kind"]}'.lower())
    return (f'<li class="card g-{e["group"]}" data-group="{e["group"]}" data-venue="{esc(e["venue"])}" '
            f'data-ts="{ts}" data-q="{search}">'
            f'<a href="{esc(e["url"])}" target="_blank" rel="noopener">'
            f'<span class="time">{t}</span>'
            f'<span class="thumb">{img}</span>'
            f'<span class="body"><span class="title">{esc(e["title"])}</span>{note}'
            f'<span class="meta">{esc(meta)}</span></span></a></li>')


def render(events, failed, today):
    esc = html.escape
    pills, sections = [], []
    for i in range(DAYS_AHEAD + 1):
        d = today + timedelta(days=i)
        rows = sorted((e for e in events if day_of(e) == d), key=lambda e: (sort_key(e), e["title"]))
        title, short, long = day_names(d, today)
        sub = f'<span class="sub">{esc(long)}</span>' if title in ("I dag", "I morgen") else ""
        pills.append(f'<a href="#d{i}" class="pill" data-day="d{i}"><b>{esc(short)}</b><em>{d.day}.</em><i>0</i></a>')
        sections.append(f'<section id="d{i}" class="day"><h2>{esc(title)}{sub}<span class="count"></span></h2>'
                        f'<ul>{"".join(card(e) for e in rows)}</ul>'
                        f'<p class="none" hidden>Ingen treff</p></section>')
    venues = sorted({e["venue"] for e in events})
    now = datetime.now(OSLO).strftime("%d.%m.%Y kl. %H:%M")
    warn = f'<p class="warn">Kunne ikke hente: {esc(", ".join(failed))}</p>' if failed else ""
    return TEMPLATE.format(pills="".join(pills), sections="\n".join(sections),
                           warn=warn, now=now, total=len(events), venues=len(venues))


TEMPLATE = """<!doctype html>
<html lang="nb"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Oslo1Week</title>
<style>
:root{{--bg:#f6f5f2;--card:#fff;--fg:#17171a;--mute:#6b6b73;--line:#e6e4df;--accent:#17171a;
--musikk:#7c3aed;--humor:#d97706;--scene:#0d9488;--annet:#6b7280;--thumb:#ecebe6}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f0f12;--card:#18181c;--fg:#f0f0f2;--mute:#9a9aa3;--line:#2a2a30;
--accent:#f0f0f2;--musikk:#a78bfa;--humor:#fbbf24;--scene:#2dd4bf;--annet:#9ca3af;--thumb:#232329}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,sans-serif}}
a{{color:inherit;text-decoration:none}}
header{{position:sticky;top:0;z-index:10;background:color-mix(in srgb,var(--bg) 92%,transparent);
backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}}
.wrap{{max-width:760px;margin:0 auto;padding:0 16px}}
.top{{display:flex;align-items:baseline;justify-content:space-between;padding:14px 0 8px}}
h1{{font-size:20px;margin:0;letter-spacing:-.01em}}
.stat{{color:var(--mute);font-size:12px}}
.days{{display:flex;gap:4px;padding:4px 0 10px}}
.pill{{flex:1 1 0;min-width:0;display:flex;flex-direction:column;align-items:center;padding:6px 2px;
border-radius:12px;background:var(--card);border:1px solid var(--line);font-size:12px}}
.pill b{{font-weight:600;white-space:nowrap}} .pill em{{font-style:normal;font-size:11px;color:var(--mute)}}
.pill i{{font-style:normal;color:var(--mute);font-size:11px}}
.pill.active em{{color:inherit;opacity:.7}}
.pill.active{{background:var(--accent);color:var(--bg);border-color:var(--accent)}}
.pill.active i{{color:inherit;opacity:.7}}
.tools{{display:flex;flex-wrap:wrap;gap:8px;padding:0 0 12px}}
input{{flex:1;font:inherit;font-size:13px;padding:6px 10px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--fg);min-width:0}}
main{{max-width:760px;margin:0 auto;padding:8px 16px 64px}}
.day{{scroll-margin-top:170px;margin-top:24px}}
h2{{font-size:18px;margin:0 0 10px;display:flex;align-items:baseline;gap:8px}}
.sub{{font-size:13px;font-weight:400;color:var(--mute)}}
.count{{margin-left:auto;font-size:12px;font-weight:400;color:var(--mute)}}
ul{{list-style:none;margin:0;padding:0;display:grid;gap:8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden;
border-left:4px solid var(--c,var(--annet))}}
.g-musikk{{--c:var(--musikk)}} .g-humor{{--c:var(--humor)}} .g-scene{{--c:var(--scene)}}
.card a{{display:grid;grid-template-columns:48px 56px 1fr;gap:12px;align-items:center;padding:10px 12px}}
.card a:hover{{background:color-mix(in srgb,var(--c) 7%,var(--card))}}
.time{{font-variant-numeric:tabular-nums;font-weight:600;font-size:14px;text-align:right}}
.thumb{{width:56px;height:56px;border-radius:10px;background:var(--thumb);overflow:hidden;display:block}}
.thumb img{{width:100%;height:100%;object-fit:cover;display:block}}
.body{{display:flex;flex-direction:column;gap:2px;min-width:0}}
.title{{font-weight:600;overflow-wrap:anywhere}}
.meta{{color:var(--mute);font-size:13px}}
.badge{{align-self:flex-start;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;
padding:1px 7px;border-radius:99px;background:#fee2e2;color:#b91c1c}}
.badge-utsatt{{background:#fef3c7;color:#92400e}}
.card.past{{opacity:.45}}
.none{{color:var(--mute);margin:4px 2px}}
.warn{{background:#fef3c7;color:#92400e;border-radius:10px;padding:8px 12px;font-size:13px}}
footer{{margin-top:40px;color:var(--mute);font-size:12px}}
@media(max-width:480px){{.pill i{{display:none}}.card a{{grid-template-columns:40px 48px 1fr;gap:10px}}.thumb{{width:48px;height:48px}}}}
</style></head><body>
<header><div class="wrap">
<div class="top"><h1>Oslo1Week</h1><span class="stat" id="stat">{total} arrangementer · {venues} scener</span></div>
<nav class="days">{pills}</nav>
<div class="tools"><input id="q" type="search" placeholder="Søk …"></div>
</div></header>
<main>
{warn}
{sections}
<footer>Oppdatert {now}</footer>
</main>
<script>
const $=(s,r=document)=>[...r.querySelectorAll(s)];
function apply(){{
  const q=$("#q")[0].value.trim().toLowerCase();let total=0;
  $(".day").forEach(day=>{{
    let n=0;
    $(".card",day).forEach(c=>{{
      const ok=!q||c.dataset.q.includes(q);
      c.hidden=!ok;if(ok)n++;
    }});
    total+=n;
    day.querySelector(".none").hidden=n>0;
    day.querySelector(".count").textContent=n+(n===1?" arrangement":" arrangementer");
    document.querySelector('.pill[data-day="'+day.id+'"] i').textContent=n;
  }});
  $("#stat")[0].textContent=total+" arrangementer";
}}
$("#q")[0].oninput=apply;
const now=Date.now();
$(".card").forEach(c=>{{const t=+c.dataset.ts;if(t&&t+2*36e5<now)c.classList.add("past")}});
const io=new IntersectionObserver(es=>es.forEach(e=>{{if(e.isIntersecting){{
  $(".pill").forEach(p=>p.classList.toggle("active",p.dataset.day===e.target.id))}}}}),{{rootMargin:"-30% 0px -60% 0px"}});
$(".day").forEach(d=>io.observe(d));
apply();
</script></body></html>"""


def main():
    today = datetime.now(OSLO).date()
    last = today + timedelta(days=DAYS_AHEAD)
    log = RunLog()

    def task(src):
        name, fn = src[0], src[1]
        t0 = time.time()
        log.update(name, state="running", started=t0)
        try:
            got = fn()
        except Exception as exc:
            log.update(name, state="error", duration=time.time() - t0, error=repr(exc)[:200])
            raise
        inside = [e for e in got if today <= day_of(e) <= last]
        log.update(name, state="ok", duration=time.time() - t0, fetched=len(got), inwindow=len(inside))
        return got, inside

    events, failed = [], []
    with ThreadPoolExecutor(len(SOURCES)) as ex:
        futs = [(s[0], ex.submit(task, s)) for s in SOURCES]
    for name, fut in futs:
        try:
            got, inside = fut.result()
            print(f"{name:20} {len(got):4} hentet, {len(inside):3} i perioden")
            events += inside
        except Exception as exc:
            print(f"{name:20} FEIL: {exc!r}", file=sys.stderr)
            failed.append(name)
    OUT.write_text(render(events, failed, today), encoding="utf-8")
    log.finish(events, failed)
    print(f"Skrev {OUT} ({len(events)} arrangementer)")


if __name__ == "__main__":
    main()
