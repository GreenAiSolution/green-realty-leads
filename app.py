"""Green Realty — Phoenix instant home-value lead funnel.

A homeowner types an address. We pull the parcel from Maricopa County's open
assessor layer (no key, no MLS), price it against the last 12 months of real
closed sales in the same ZIP, and show a value range. The full report (the
comparable sales) is behind a name + phone + email form. That form is the lead.

Pure standard library so it deploys anywhere Python runs. State is one SQLite
file at $DATA_DIR (a Railway volume in production).

Env:
  PORT            Railway sets this
  DATA_DIR        where leads.db lives (default ./data)
  ADMIN_KEY       required to view /leads
  RESEND_API_KEY  optional — emails each new lead to NOTIFY_EMAIL
  NOTIFY_EMAIL    default jadengreen808@gmail.com
  FROM_EMAIL      default leads@greenaidigital.com (must be a Resend-verified domain)
  LICENSE_STATUS  'pending' (default) or 'licensed' — controls the disclosure copy
  SITE_NAME       default 'Green Realty'
"""
from __future__ import annotations
import csv, datetime, html, io, json, os, re, sqlite3, ssl, statistics, threading, time
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(HERE, "data"))
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "jadengreen808@gmail.com")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "leads@greenaidigital.com")
LICENSE_STATUS = os.environ.get("LICENSE_STATUS", "pending")
SITE_NAME = os.environ.get("SITE_NAME", "Green Realty")

COUNTY = ("https://gis.mcassessor.maricopa.gov/arcgis/rest/services"
          "/Parcels/MapServer/0/query")
FIELDS = ("APN,OWNER_NAME,PHYSICAL_STREET_NUM,PHYSICAL_STREET_DIR,PHYSICAL_STREET_NAME,"
          "PHYSICAL_STREET_TYPE,PHYSICAL_CITY,PHYSICAL_ZIP,PHYSICAL_ADDRESS,MAIL_ADDRESS,"
          "MAIL_ZIP,SALE_DATE,SALE_PRICE,DEED_DATE,LIVING_SPACE,LAND_SIZE,CONST_YEAR,"
          "SUBNAME,PUC,FCV_CUR,LATITUDE,LONGITUDE")
MIN_ARMS_LENGTH = 25_000
COMP_CACHE_SECONDS = 6 * 3600

# --------------------------------------------------------------------- TLS
def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # macOS dev box: /etc/ssl/cert.pem lacks Sectigo roots the county chain needs.
    if os.uname().sysname == "Darwin":
        try:
            import subprocess
            pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                                  "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                 capture_output=True, text=True, timeout=10).stdout
            if pem:
                ctx.load_verify_locations(cadata=pem)
        except Exception:
            pass
    return ctx
SSL_CTX = _ssl_context()

def http_get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "green-realty-leads/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()

def http_post_json(url: str, body: dict, headers: dict, timeout: int = 20) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return json.loads(r.read() or b"{}")

# ------------------------------------------------------------------ county
def _num(v):
    if v in (None, ""):
        return None
    t = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None

def _date(a: dict) -> str | None:
    d = a.get("DEED_DATE")
    if d:
        try:
            return datetime.datetime.utcfromtimestamp(float(d) / 1000).date().isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    s = str(a.get("SALE_DATE") or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return None

def county_query(where: str, limit: int = 1000, order: str | None = None) -> list[dict]:
    params = {"where": where, "f": "json", "returnGeometry": "false",
              "outFields": FIELDS, "resultRecordCount": str(limit)}
    if order:
        params["orderByFields"] = order
    data = json.loads(http_get(COUNTY + "?" + urllib.parse.urlencode(params)))
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "county query rejected"))
    return [f.get("attributes", {}) for f in data.get("features", [])]

STREET_TYPES = {"STREET": "ST", "AVENUE": "AVE", "AVE.": "AVE", "ROAD": "RD", "DRIVE": "DR",
                "LANE": "LN", "COURT": "CT", "PLACE": "PL", "CIRCLE": "CIR", "BOULEVARD": "BLVD",
                "WAY": "WAY", "TRAIL": "TRL", "PARKWAY": "PKWY", "TERRACE": "TER", "LOOP": "LOOP"}
DIRS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "N": "N", "S": "S", "E": "E", "W": "W"}

def parse_address(text: str) -> dict:
    """'4525 E Cactus Rd, Phoenix, AZ 85032' -> {num, dir, name, zip}"""
    t = re.sub(r"[.,]", " ", text.upper())
    zip_m = re.search(r"\b(85\d{3})\b", t)
    zipc = zip_m.group(1) if zip_m else None
    t = re.sub(r"\b(AZ|ARIZONA|PHOENIX|SCOTTSDALE|MESA|TEMPE|CHANDLER|GILBERT|GLENDALE|PEORIA|"
               r"SURPRISE|GOODYEAR|AVONDALE|BUCKEYE|QUEEN CREEK|CAVE CREEK|FOUNTAIN HILLS|"
               r"PARADISE VALLEY|LITCHFIELD PARK|EL MIRAGE|TOLLESON|SUN CITY|SUN CITY WEST|"
               r"ANTHEM|LAVEEN|AHWATUKEE|85\d{3})\b", " ", t)
    toks = [x for x in t.split() if x]
    if not toks or not toks[0].isdigit():
        raise ValueError("Start with the house number, like: 4525 E Cactus Rd 85032")
    num = toks.pop(0)
    d = None
    if toks and toks[0] in DIRS:
        d = DIRS[toks.pop(0)]
    if toks and (toks[-1] in STREET_TYPES or toks[-1] in STREET_TYPES.values()):
        toks.pop()
    if not toks:
        raise ValueError("Add the street name, like: 4525 E Cactus Rd 85032")
    return {"num": num, "dir": d, "name": " ".join(toks), "zip": zipc}

def find_parcel(text: str) -> dict | None:
    p = parse_address(text)
    where = f"PHYSICAL_STREET_NUM = '{p['num']}' AND UPPER(PHYSICAL_STREET_NAME) LIKE '{p['name'].replace(chr(39), chr(39)*2)}%'"
    if p["dir"]:
        where += f" AND PHYSICAL_STREET_DIR = '{p['dir']}'"
    if p["zip"]:
        where += f" AND PHYSICAL_ZIP = '{p['zip']}'"
    rows = county_query(where, limit=5)
    if not rows and p["dir"]:      # try without the direction — people often get it wrong
        rows = county_query(where.replace(f" AND PHYSICAL_STREET_DIR = '{p['dir']}'", ""), limit=5)
    if not rows:
        return None
    rows.sort(key=lambda a: 0 if str(a.get("PUC", "")).startswith("01") else 1)
    return rows[0]

def _title(s: str) -> str:
    t = s.title()
    t = re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), t)   # 36Th -> 36th
    return re.sub(r"\b(N|S|E|W|Ne|Nw|Se|Sw)\b", lambda m: m.group(1).upper(), t)

def shape(a: dict) -> dict:
    sqft = _num(a.get("LIVING_SPACE")); lot = _num(a.get("LAND_SIZE")); yr = _num(a.get("CONST_YEAR"))
    return {
        "apn": str(a.get("APN") or "").strip(),
        "address": _title(re.split(r"\s{2,}", str(a.get("PHYSICAL_ADDRESS") or ""))[0]),
        "city": str(a.get("PHYSICAL_CITY") or "").title(), "zip": str(a.get("PHYSICAL_ZIP") or "").strip()[:5],
        "sqft": int(sqft) if sqft and 200 <= sqft <= 40000 else None,
        "lot_sqft": int(lot) if lot and lot > 0 else None,
        "year_built": int(yr) if yr and 1850 <= yr <= 2100 else None,
        "subdivision": str(a.get("SUBNAME") or "").title() or None,
        "last_sale_price": _num(a.get("SALE_PRICE")), "last_sale_date": _date(a),
        "assessor_fcv": _num(a.get("FCV_CUR")),
        "absentee": bool(a.get("MAIL_ZIP")) and str(a.get("MAIL_ZIP") or "")[:5] != str(a.get("PHYSICAL_ZIP") or "")[:5],
        "lat": _num(a.get("LATITUDE")), "lon": _num(a.get("LONGITUDE")),
    }

_comp_cache: dict[str, tuple[float, list[dict]]] = {}
_comp_lock = threading.Lock()

def zip_sales(zipc: str, months: int = 12) -> list[dict]:
    with _comp_lock:
        hit = _comp_cache.get(zipc)
        if hit and time.time() - hit[0] < COMP_CACHE_SECONDS:
            return hit[1]
    since = datetime.date.today() - datetime.timedelta(days=int(months * 30.44))
    where = (f"PHYSICAL_ZIP = '{zipc}' AND PUC LIKE '01%' AND SALE_PRICE IS NOT NULL "
             f"AND DEED_DATE > DATE '{since.isoformat()}'")
    rows = county_query(where, limit=1000, order="DEED_DATE DESC")
    sales = []
    for a in rows:
        s = shape(a)
        if s["last_sale_price"] and s["last_sale_price"] >= MIN_ARMS_LENGTH and s["sqft"] and s["last_sale_date"]:
            sales.append(s)
    with _comp_lock:
        _comp_cache[zipc] = (time.time(), sales)
    return sales

def estimate(subject: dict) -> dict:
    """Median $/sqft of similar recent sales in the ZIP, times the subject's sqft."""
    if not subject.get("sqft") or not subject.get("zip"):
        return {"ok": False, "reason": "The county has no living-area figure for this parcel, so we can't price it automatically."}
    sales = zip_sales(subject["zip"])
    sq = subject["sqft"]
    band = [s for s in sales if 0.75 * sq <= s["sqft"] <= 1.25 * sq]
    if len(band) < 5:
        band = [s for s in sales if 0.6 * sq <= s["sqft"] <= 1.5 * sq]
    if len(band) < 3:
        return {"ok": False, "reason": f"Fewer than three comparable sales closed in {subject['zip']} in the last 12 months."}
    ppsf = sorted(s["last_sale_price"] / s["sqft"] for s in band)
    # trim 10% each tail to drop family transfers and outliers
    k = max(1, len(ppsf) // 10)
    core = ppsf[k:-k] if len(ppsf) > 2 * k + 2 else ppsf
    med = statistics.median(core)
    point = med * sq
    # rank comps by closeness in sqft, then recency; keep 5
    band.sort(key=lambda s: (abs(s["sqft"] - sq) / sq, s["last_sale_date"]), reverse=False)
    comps = [s for s in band if s.get("apn") != subject.get("apn")][:5]
    return {"ok": True, "point": round(point, -3), "low": round(point * 0.94, -3), "high": round(point * 1.06, -3),
            "ppsf": round(med), "n_sales": len(band), "n_zip_sales": len(sales), "comps": comps}

# ------------------------------------------------------------------- store
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "leads.db")
_db_lock = threading.Lock()

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con

with db() as _c:
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS leads (
      id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
      name TEXT, phone TEXT, email TEXT, intent TEXT, timeline TEXT, consent INTEGER,
      address TEXT, apn TEXT, zip TEXT, sqft INTEGER, year_built INTEGER,
      estimate_low REAL, estimate_point REAL, estimate_high REAL,
      source TEXT, ip TEXT, user_agent TEXT, notified INTEGER DEFAULT 0, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS lookups (
      id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, query TEXT, found INTEGER, zip TEXT, source TEXT, ip TEXT
    );""")

def log_lookup(q: str, found: bool, zipc: str | None, source: str, ip: str):
    with _db_lock, db() as c:
        c.execute("INSERT INTO lookups(created_at,query,found,zip,source,ip) VALUES(?,?,?,?,?,?)",
                  (datetime.datetime.utcnow().isoformat(timespec="seconds"), q[:200], int(found), zipc, source[:100], ip))

def save_lead(d: dict, ip: str, ua: str) -> int:
    with _db_lock, db() as c:
        cur = c.execute("""INSERT INTO leads(created_at,name,phone,email,intent,timeline,consent,address,apn,zip,sqft,
            year_built,estimate_low,estimate_point,estimate_high,source,ip,user_agent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.datetime.utcnow().isoformat(timespec="seconds"), d.get("name"), d.get("phone"), d.get("email"),
             d.get("intent"), d.get("timeline"), int(bool(d.get("consent"))), d.get("address"), d.get("apn"), d.get("zip"),
             d.get("sqft"), d.get("year_built"), d.get("estimate_low"), d.get("estimate_point"), d.get("estimate_high"),
             (d.get("source") or "direct")[:100], ip, ua[:200]))
        return cur.lastrowid

def notify(lead_id: int, d: dict):
    if not RESEND_API_KEY:
        return
    est = f"${d.get('estimate_low', 0):,.0f} – ${d.get('estimate_high', 0):,.0f}" if d.get("estimate_point") else "n/a"
    text = (f"New home-value lead #{lead_id}\n\n{d.get('name')}\n{d.get('phone')}\n{d.get('email')}\n\n"
            f"{d.get('address')}  (ZIP {d.get('zip')}, {d.get('sqft')} sqft, built {d.get('year_built')})\n"
            f"Estimate: {est}\nIntent: {d.get('intent')}   Timeline: {d.get('timeline')}\n"
            f"Source: {d.get('source') or 'direct'}\nConsent to call/text: {'yes' if d.get('consent') else 'NO'}\n")
    try:
        http_post_json("https://api.resend.com/emails",
                       {"from": f"{SITE_NAME} Leads <{FROM_EMAIL}>", "to": [NOTIFY_EMAIL],
                        "subject": f"LEAD: {d.get('name')} — {d.get('address')} ({d.get('intent')})", "text": text},
                       {"Authorization": f"Bearer {RESEND_API_KEY}"})
        with _db_lock, db() as c:
            c.execute("UPDATE leads SET notified=1 WHERE id=?", (lead_id,))
    except Exception as e:  # never let a notification failure lose the lead
        print("notify failed:", e, flush=True)

# --------------------------------------------------------------------- http
def read_file(name: str) -> str:
    with open(os.path.join(HERE, "templates", name), encoding="utf-8") as f:
        return f.read()

DISCLOSURE = {
    "pending": (f"{SITE_NAME} is an independent home-value service run by Jaden Green, who is completing Arizona "
                "real-estate licensing. Estimates are computed from Maricopa County public records and are not an "
                "appraisal or a broker price opinion. Not affiliated with Maricopa County."),
    "licensed": (f"{SITE_NAME} — Jaden Green, Arizona licensed real estate salesperson. Estimates are computed from "
                 "Maricopa County public records and are not an appraisal. Not affiliated with Maricopa County."),
}

def client_ip(h) -> str:
    return (h.headers.get("X-Forwarded-For", "").split(",")[0].strip() or h.client_address[0])

class Handler(BaseHTTPRequestHandler):
    server_version = "green-realty-leads/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, obj: dict):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            page = (read_file("index.html").replace("{{SITE_NAME}}", html.escape(SITE_NAME))
                    .replace("{{DISCLOSURE}}", html.escape(DISCLOSURE.get(LICENSE_STATUS, DISCLOSURE["pending"])))
                    .replace("{{YEAR}}", str(datetime.date.today().year)))
            return self.send(200, page.encode())
        if u.path == "/health":
            return self.send_json(200, {"ok": True, "db": os.path.exists(DB), "resend": bool(RESEND_API_KEY), "license": LICENSE_STATUS})
        if u.path == "/leads":
            if not ADMIN_KEY or q.get("key", [""])[0] != ADMIN_KEY:
                return self.send(403, b"forbidden", "text/plain")
            with db() as c:
                rows = c.execute("SELECT * FROM leads ORDER BY id DESC LIMIT 500").fetchall()
                n_look = c.execute("SELECT COUNT(*) FROM lookups").fetchone()[0]
                n_found = c.execute("SELECT COUNT(*) FROM lookups WHERE found=1").fetchone()[0]
                by_src = c.execute("SELECT COALESCE(source,'direct') s, COUNT(*) n FROM leads GROUP BY s ORDER BY n DESC").fetchall()
            if q.get("format", [""])[0] == "csv":
                out = io.StringIO(); w = csv.writer(out)
                w.writerow(rows[0].keys() if rows else ["id"])
                for r in rows: w.writerow(list(r))
                return self.send(200, out.getvalue().encode(), "text/csv", {"Content-Disposition": "attachment; filename=leads.csv"})
            trs = "".join(
                f"<tr><td>{r['id']}</td><td>{html.escape(r['created_at'])}</td><td><b>{html.escape(r['name'] or '')}</b><br>"
                f"<a href='tel:{html.escape(r['phone'] or '')}'>{html.escape(r['phone'] or '')}</a><br>"
                f"<a href='mailto:{html.escape(r['email'] or '')}'>{html.escape(r['email'] or '')}</a></td>"
                f"<td>{html.escape(r['address'] or '')}<br><small>{r['sqft'] or '?'} sqft · built {r['year_built'] or '?'}</small></td>"
                f"<td>${(r['estimate_low'] or 0):,.0f} – ${(r['estimate_high'] or 0):,.0f}</td>"
                f"<td>{html.escape(r['intent'] or '')}<br><small>{html.escape(r['timeline'] or '')}</small></td>"
                f"<td>{html.escape(r['source'] or 'direct')}</td><td>{'✓' if r['consent'] else '✗'}</td></tr>"
                for r in rows)
            srcs = " · ".join(f"{html.escape(s['s'])} {s['n']}" for s in by_src) or "none yet"
            page = (read_file("leads.html").replace("{{ROWS}}", trs).replace("{{N_LEADS}}", str(len(rows)))
                    .replace("{{N_LOOKUPS}}", str(n_look)).replace("{{N_FOUND}}", str(n_found))
                    .replace("{{SOURCES}}", srcs).replace("{{KEY}}", html.escape(ADMIN_KEY)).replace("{{SITE_NAME}}", html.escape(SITE_NAME)))
            return self.send(200, page.encode(), extra={"Cache-Control": "no-store"})
        return self.send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        ip = client_ip(self)
        try:
            d = self.body_json()
        except Exception:
            return self.send_json(400, {"ok": False, "error": "bad json"})
        if u.path == "/api/lookup":
            q = str(d.get("address") or "").strip()[:200]
            src = str(d.get("source") or "direct")[:100]
            if not q:
                return self.send_json(400, {"ok": False, "error": "Type an address."})
            try:
                a = find_parcel(q)
            except ValueError as e:
                return self.send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                print("county error:", e, flush=True)
                return self.send_json(502, {"ok": False, "error": "The county records service didn't answer. Try again in a minute."})
            if not a:
                log_lookup(q, False, None, src, ip)
                return self.send_json(404, {"ok": False, "error": "We couldn't find that address in Maricopa County records. Check the house number and street, and add the ZIP."})
            s = shape(a)
            log_lookup(q, True, s["zip"], src, ip)
            try:
                est = estimate(s)
            except Exception as e:
                print("estimate error:", e, flush=True)
                est = {"ok": False, "reason": "Sales data for this ZIP is temporarily unavailable."}
            public = {k: v for k, v in s.items() if k not in ("lat", "lon", "absentee")}
            teaser = {k: v for k, v in est.items() if k != "comps"}      # comps are the paid-for part
            return self.send_json(200, {"ok": True, "subject": public, "estimate": teaser})
        if u.path == "/api/lead":
            name = str(d.get("name") or "").strip()[:100]
            phone = re.sub(r"[^0-9+]", "", str(d.get("phone") or ""))[:20]
            email = str(d.get("email") or "").strip()[:120]
            if len(name) < 2 or len(phone) < 10 or "@" not in email:
                return self.send_json(400, {"ok": False, "error": "Name, a 10-digit phone, and a real email are needed to send the report."})
            if not d.get("consent"):
                return self.send_json(400, {"ok": False, "error": "Please tick the consent box so we can reach you."})
            lead = {"name": name, "phone": phone, "email": email, "intent": str(d.get("intent") or "")[:40],
                    "timeline": str(d.get("timeline") or "")[:40], "consent": True,
                    "address": str(d.get("address") or "")[:200], "apn": str(d.get("apn") or "")[:20],
                    "zip": str(d.get("zip") or "")[:5], "sqft": d.get("sqft"), "year_built": d.get("year_built"),
                    "estimate_low": d.get("estimate_low"), "estimate_point": d.get("estimate_point"),
                    "estimate_high": d.get("estimate_high"), "source": str(d.get("source") or "direct")}
            lead_id = save_lead(lead, ip, self.headers.get("User-Agent", ""))
            threading.Thread(target=notify, args=(lead_id, lead), daemon=True).start()
            comps = []
            try:
                if lead["zip"] and lead["sqft"]:
                    est = estimate({"zip": lead["zip"], "sqft": int(lead["sqft"]), "apn": lead["apn"]})
                    comps = est.get("comps", []) if est.get("ok") else []
            except Exception as e:
                print("comps error:", e, flush=True)
            comps = [{"address": c["address"], "sqft": c["sqft"], "price": c["last_sale_price"], "date": c["last_sale_date"],
                      "year_built": c["year_built"], "ppsf": round(c["last_sale_price"] / c["sqft"])} for c in comps]
            return self.send_json(200, {"ok": True, "lead_id": lead_id, "comps": comps})
        return self.send_json(404, {"ok": False, "error": "not found"})

def main():
    port = int(os.environ.get("PORT", "8195"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"{SITE_NAME} leads on :{port}  db={DB}  resend={'on' if RESEND_API_KEY else 'off'}  license={LICENSE_STATUS}", flush=True)
    srv.serve_forever()

if __name__ == "__main__":
    main()
