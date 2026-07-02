#!/usr/bin/env python3
"""
vault_clean.py - ruthless meta-only Destiny 2 vault triage via the Bungie API.

What it does (the only things the API allows):
  - Pulls your full vault + character inventories with live perks.
  - Classifies junk: greens/blues, low-stat legendary armor, duplicates, and
    -- if a wishlist is loaded -- any weapon that is NOT a wishlist god roll.
  - Writes dismantle_list.csv  -> what to shard in-game or in DIM.
  - --lock-keepers : locks every KEEP item so a fast manual dismantle spree
    physically cannot delete a god roll.

The API has NO destroy-item endpoint, so dismantling stays manual. This script
makes that manual pass safe and fast.

ONE-TIME SETUP
  1. https://www.bungie.net/en/Application -> Create New App.
       OAuth Client Type: Confidential
       Redirect URL:      https://localhost:7777/callback
       Scopes:            Read your Destiny inventory + Move/equip/destroy items
  2. Put your keys in a local (gitignored) bungie_secrets.py -- copy
     bungie_secrets.example.py -- or export BUNGIE_API_KEY / BUNGIE_CLIENT_ID
     / BUNGIE_CLIENT_SECRET in your environment. Never commit real keys.
  3. pip install requests
     pip install cryptography      # optional: enables one-click login page
  4. python vault_clean.py --wishlist voltron.txt
     python vault_clean.py --wishlist https://raw.githubusercontent.com/48klocs/dim-wish-list-sources/master/voltron.txt
     python vault_clean.py --wishlist voltron.txt --lock-keepers
     python vault_clean.py --selftest      # no network, checks the rules

LOGIN
  First run opens a Bungie approval page. With cryptography installed it lands
  on a local HTTPS page that auto-captures the login (click through the browser's
  one-time self-signed-cert warning). Without it, copy the code from the URL and
  paste it. Token is cached + auto-refreshed after that.
"""
import argparse, json, os, sys, time, webbrowser
from collections import defaultdict
from urllib.parse import urlencode, urlparse, parse_qs
import requests

# Credentials load from a local (gitignored) bungie_secrets.py, else env vars.
# Real keys must NEVER be committed -- see bungie_secrets.example.py.
try:
    from bungie_secrets import API_KEY, CLIENT_ID, CLIENT_SECRET
except ImportError:
    API_KEY       = os.environ.get("BUNGIE_API_KEY", "")
    CLIENT_ID     = os.environ.get("BUNGIE_CLIENT_ID", "")
    CLIENT_SECRET = os.environ.get("BUNGIE_CLIENT_SECRET", "")
REDIRECT_URL  = "https://localhost:7777/callback"

BASE   = "https://www.bungie.net/Platform"
ROOT   = "https://www.bungie.net"
TOKENF = "token.json"
MANIF  = "manifest_items.json"

# ---- tuning (ruthless meta-only defaults) -------------------------------
KEEP_PER_HASH   = 1     # copies kept of each non-wishlist gun/armor hash
MIN_ARMOR_TOTAL = 60    # legendary armor under this total stat = junk
# -------------------------------------------------------------------------

ARMOR_STATS = (2996146975, 392767087, 1943323491,    # mob, res, rec
               1735777505, 144602215, 4244567218)     # dis, int, str
WEAPON, ARMOR = 3, 2
WILDCARD = -69420       # DIM "applies to all items" item id


# ===== wishlist (DIM voltron format) =====================================
def load_wishlist(src):
    """src = file path or URL. Returns {item_hash: [frozenset(required perks), ...]}."""
    text = requests.get(src).text if src.startswith("http") else open(src, encoding="utf-8").read()
    wl = defaultdict(list)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("dimwishlist:"):
            continue
        params = dict(p.split("=", 1) for p in line[len("dimwishlist:"):].split("&") if "=" in p)
        try:
            ih = int(params.get("item", "0"))
        except ValueError:
            continue
        if ih < 0 and ih != WILDCARD:
            continue                                   # negative = trash roll; we only whitelist
        perks = frozenset(int(x) for x in params.get("perks", "").split(",") if x.strip().lstrip("-").isdigit())
        wl[ih].append(perks)
    return dict(wl)


def matches_wishlist(it, wl):
    for req in wl.get(it["hash"], ()):                 # empty req (item-only line) matches any roll
        if req <= it["perks"]:
            return True
    for req in wl.get(WILDCARD, ()):                   # wildcard must name perks, else it'd match everything
        if req and req <= it["perks"]:
            return True
    return False


# ===== the rules (pure, testable, no network) ============================
def classify(items, wishlist=None, keep_per_hash=KEEP_PER_HASH, min_armor_total=MIN_ARMOR_TOTAL):
    wl_on = wishlist is not None
    for it in items:                                    # crafted weapons are protected like wishlist rolls
        it["verdict"], it["reason"] = "keep", ""
        it["wl"] = it.get("crafted", False) or (matches_wishlist(it, wishlist) if wl_on else False)

    for it in items:                                   # greens + blues
        if it["tierType"] <= 4:
            it["verdict"], it["reason"] = "junk", "rare or below"

    for it in items:                                   # weak legendary armor (wishlist match is spared)
        if (it["verdict"] == "keep" and not it["wl"] and it["itemType"] == ARMOR
                and it["tierType"] == 5 and it["total"] < min_armor_total):
            it["verdict"], it["reason"] = "junk", f"armor total {it['total']} < {min_armor_total}"

    if wl_on:                                           # meta gate: non-wishlist LEGENDARY weapons are junk
        for it in items:                                # exotics are build-defining -> never gated, kept below
            if (it["verdict"] == "keep" and it["itemType"] == WEAPON
                    and it["tierType"] == 5 and not it["wl"]):
                it["verdict"], it["reason"] = "junk", "not a wishlist roll"

    groups = defaultdict(list)                          # dupes among survivors (wishlist rolls exempt)
    for it in items:
        if it["verdict"] == "keep" and not it["wl"] and it["itemType"] in (WEAPON, ARMOR):
            groups[it["hash"]].append(it)
    for grp in groups.values():
        grp.sort(key=lambda x: x["power"], reverse=True)
        for extra in grp[keep_per_hash:]:
            extra["verdict"], extra["reason"] = "junk", "duplicate (lower power)"
    return items


def selftest():
    base = lambda **k: {"total": 0, "power": 0, "perks": frozenset(), **k}
    # --- no wishlist: dupe + stat rules ---
    items = [
        base(hash=1, name="Blue",  itemType=WEAPON, tierType=4, power=1800, instanceId="a"),
        base(hash=2, name="Dupe+", itemType=WEAPON, tierType=5, power=2000, instanceId="b"),
        base(hash=2, name="Dupe-", itemType=WEAPON, tierType=5, power=1900, instanceId="c"),
        base(hash=3, name="LowArm", itemType=ARMOR, tierType=5, power=2000, total=55, instanceId="d"),
        base(hash=4, name="OKArm",  itemType=ARMOR, tierType=5, power=2000, total=66, instanceId="e"),
        base(hash=5, name="Exotic", itemType=ARMOR, tierType=6, power=2000, total=58, instanceId="f"),
    ]
    v = {x["instanceId"]: x["verdict"] for x in classify(items)}
    assert v == dict(a="junk", b="keep", c="junk", d="junk", e="keep", f="keep"), v

    # --- with wishlist: meta gate ---
    wl = {100: [frozenset({11, 22})], WILDCARD: [frozenset({99})]}
    items = [
        base(hash=100, name="GodRoll", itemType=WEAPON, tierType=5, perks=frozenset({11, 22, 33}), instanceId="g"),
        base(hash=100, name="GodDupe", itemType=WEAPON, tierType=5, perks=frozenset({11, 22}),     instanceId="h"),
        base(hash=100, name="BadRoll", itemType=WEAPON, tierType=5, perks=frozenset({11}),         instanceId="i"),
        base(hash=200, name="Wildcard", itemType=WEAPON, tierType=5, perks=frozenset({99}),        instanceId="j"),
        base(hash=300, name="NoMatch", itemType=WEAPON, tierType=5, perks=frozenset(),             instanceId="k"),
        base(hash=400, name="Blue",    itemType=WEAPON, tierType=4, perks=frozenset({11, 22}),     instanceId="l"),
        base(hash=500, name="Exotic",  itemType=WEAPON, tierType=6, perks=frozenset(),             instanceId="m"),
        base(hash=600, name="Crafted", itemType=WEAPON, tierType=5, perks=frozenset(), crafted=True, instanceId="n"),
    ]
    v = {x["instanceId"]: x["verdict"] for x in classify(items, wishlist=wl)}
    assert v == dict(g="keep", h="keep", i="junk", j="keep", k="junk", l="junk", m="keep", n="keep"), v
    print("selftest ok")


# ===== auth ==============================================================
def _save_token(t):
    t["expires_at"] = time.time() + t.get("expires_in", 3600) - 60
    json.dump(t, open(TOKENF, "w"))


def _exchange(data):
    # Confidential app: HTTP Basic auth. Public app (no secret): send client_id in the body.
    auth = (CLIENT_ID, CLIENT_SECRET) if CLIENT_SECRET else None
    if not CLIENT_SECRET:
        data = {**data, "client_id": CLIENT_ID}
    r = requests.post(f"{BASE}/App/OAuth/Token/", data=data, auth=auth,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    r.raise_for_status()
    t = r.json(); _save_token(t); return t


def _gen_cert():
    """Self-signed localhost cert so the https redirect can be served. Needs cryptography."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime, tempfile
    key  = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, "wb").write(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return cp, kp


def _capture_code():
    """Run a local https server on the redirect port; return the OAuth code, or None if unavailable."""
    try:
        cp, kp = _gen_cert()
    except ImportError:
        return None
    import ssl
    from http.server import BaseHTTPRequestHandler, HTTPServer
    box = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if "code" in q:
                box["code"] = q["code"][0]
                self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
                self.wfile.write(b"<h2>Login complete. Close this tab and return to the terminal.</h2>")
            else:
                self.send_response(204); self.end_headers()

        def log_message(self, *a): pass

    port = urlparse(REDIRECT_URL).port or 443
    srv  = HTTPServer(("localhost", port), H)
    ctx  = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(cp, kp)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"Waiting for login on {REDIRECT_URL} (accept the browser's cert warning)...")
    while "code" not in box:
        srv.handle_request()
    return box["code"]


def get_token():
    if os.path.exists(TOKENF):
        t = json.load(open(TOKENF))
        if t.get("expires_at", 0) > time.time():
            return t["access_token"]
        if t.get("refresh_token"):
            try:
                return _exchange({"grant_type": "refresh_token",
                                  "refresh_token": t["refresh_token"]})["access_token"]
            except Exception:
                pass
    url = "https://www.bungie.net/en/OAuth/Authorize?" + urlencode(
        {"client_id": CLIENT_ID, "response_type": "code"})
    print("\nApprove access here:\n", url, "\n")
    try: webbrowser.open(url)
    except Exception: pass
    code = _capture_code()
    if not code:
        raw  = input("Paste the code (or full localhost URL): ").strip()
        code = parse_qs(urlparse(raw).query).get("code", [raw])[0]
    return _exchange({"grant_type": "authorization_code", "code": code})["access_token"]


def api(path, token=None, method="GET", json_body=None):
    h = {"X-API-Key": API_KEY}
    if token: h["Authorization"] = f"Bearer {token}"
    r = requests.request(method, f"{BASE}{path}", headers=h, json=json_body)
    r.raise_for_status()
    body = r.json()
    if body.get("ErrorCode", 1) != 1:
        raise RuntimeError(f"{body.get('ErrorStatus')}: {body.get('Message')}")
    return body["Response"]


# ===== manifest (item names / tier / type) ==============================
def load_manifest():
    if os.path.exists(MANIF):
        return json.load(open(MANIF))
    print("Downloading item manifest (~once)...")
    m    = api("/Destiny2/Manifest/")
    path = m["jsonWorldComponentContentPaths"]["en"]["DestinyInventoryItemDefinition"]
    defs = requests.get(ROOT + path).json()
    json.dump(defs, open(MANIF, "w"))
    return defs


# ===== inventory pull ====================================================
def build_inventory(token, defs):
    me  = api("/User/GetMembershipsForCurrentUser/", token)
    mem = next((m for m in me["destinyMemberships"]
                if str(m["membershipId"]) == str(me.get("primaryMembershipId"))),
               me["destinyMemberships"][0])
    mtype, mid = mem["membershipType"], mem["membershipId"]

    prof = api(f"/Destiny2/{mtype}/Profile/{mid}/?components=102,200,201,300,304,305", token)
    char_id = next(iter(prof["characters"]["data"]))           # any char, for locking
    comp = prof.get("itemComponents", {})
    inst = comp.get("instances", {}).get("data", {})
    stat = comp.get("stats", {}).get("data", {})
    sock = comp.get("sockets", {}).get("data", {})

    # pair each item with the character id it must be locked through (vault -> any char)
    raw = [(it, char_id) for it in prof["profileInventory"]["data"]["items"]]
    for cid, items in prof["characterInventories"]["data"].items():
        raw += [(it, cid) for it in items["items"]]

    out = []
    for it, owner in raw:
        iid = it.get("itemInstanceId")
        if not iid:
            continue
        d = defs.get(str(it["itemHash"]))
        if not d or d.get("itemType", 0) not in (WEAPON, ARMOR):
            continue
        total = sum(s["value"] for h, s in stat.get(iid, {}).get("stats", {}).items()
                    if int(h) in ARMOR_STATS)
        perks = frozenset(s["plugHash"] for s in sock.get(iid, {}).get("sockets", [])
                          if s.get("plugHash"))
        out.append(dict(
            hash=it["itemHash"],
            name=d["displayProperties"]["name"],
            itemType=d["itemType"],
            tierType=d.get("inventory", {}).get("tierType", 0),
            power=inst.get(iid, {}).get("primaryStat", {}).get("value", 0),
            gearTier=inst.get(iid, {}).get("gearTier", 0),   # 2026 Frontiers quality tier (0-5)
            level=inst.get(iid, {}).get("itemLevel", 0),     # weapon "power" was flattened; itemLevel is meaningful
            total=total,
            perks=perks,
            crafted=bool(it.get("state", 0) & 8),       # DestinyItemState.Crafted
            instanceId=iid,
            owner=owner,                                 # character id to lock this item through
            location="vault" if it.get("bucketHash") == 138197802 else "char",
        ))
    return out, mtype, char_id


def lock(token, mtype, char_id, instance_id):
    api("/Destiny2/Actions/Items/SetLockState/", token, "POST", {
        "state": True, "itemId": instance_id,
        "characterId": char_id, "membershipType": mtype})


# ===== main ==============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wishlist", help="DIM voltron file path or URL; enables the meta gate")
    ap.add_argument("--lock-keepers", action="store_true",
                    help="lock every KEEP item so a manual dismantle spree can't delete it")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not (CLIENT_ID and API_KEY):
        sys.exit("Fill CLIENT_ID / CLIENT_SECRET / API_KEY at the top first.")

    wishlist = None
    if a.wishlist:
        wishlist = load_wishlist(a.wishlist)
        print(f"Wishlist: {len(wishlist)} item entries loaded")

    token = get_token()
    defs  = load_manifest()
    items, mtype, char_id = build_inventory(token, defs)
    classify(items, wishlist=wishlist)

    junk = [it for it in items if it["verdict"] == "junk"]
    keep = [it for it in items if it["verdict"] == "keep"]
    junk.sort(key=lambda x: (x["itemType"], x["reason"], -x["power"]))

    with open("dismantle_list.csv", "w", encoding="utf-8") as f:
        f.write("name,type,power,total,location,reason\n")
        for it in junk:
            t = "weapon" if it["itemType"] == WEAPON else "armor"
            f.write(f"\"{it['name']}\",{t},{it['power']},{it['total']},{it['location']},\"{it['reason']}\"\n")

    print(f"\n{len(items)} instanced items  |  KEEP {len(keep)}  |  JUNK {len(junk)}")
    print("Wrote dismantle_list.csv")

    if a.lock_keepers:
        print(f"Locking {len(keep)} keepers...")
        for i, it in enumerate(keep, 1):
            try:
                lock(token, mtype, it["owner"], it["instanceId"])
            except Exception as e:
                print(f"  lock failed {it['name']}: {e}")
            time.sleep(0.15)            # ponytail: fixed throttle; raise if Bungie 429s
            if i % 25 == 0:
                print(f"  {i}/{len(keep)}")
        print("Keepers locked. Now shard the dismantle_list safely.")


if __name__ == "__main__":
    main()
