#!/usr/bin/env python3
"""bungie.py - a single, self-contained CLI over the Bungie.net Platform API.

One tool an agent (or a human) can drive to inspect and act on a user's Destiny 2
account: characters, inventory, item/source lookup, god-roll matching vs DIM
wishlists, vault triage with keeper-locking, transfers/locks/equips, and a raw
passthrough to any endpoint. Chdir's into its own folder so caches resolve
regardless of the working directory.

Credentials come from a local (gitignored) bungie_secrets.py or env vars
(BUNGIE_API_KEY / BUNGIE_CLIENT_ID / BUNGIE_CLIENT_SECRET). Token is cached in
token.json and auto-refreshed. Manifest + collectible tables cache on first use.

Commands:
  login                       force/refresh the OAuth login
  whoami                      current Bungie user + Destiny memberships
  chars                       your characters (class, light, playtime)
  inv [--char ID] [--vault] [--search TERM] [--type weapon|armor]
                              list instanced items (gearTier + itemLevel)
  item <name|hash> [-n N]     search the manifest for an item definition
  source <name|hash> [-n N]   where an item drops (collectible source string)
  lock <instanceId> / unlock <instanceId>
  transfer <instanceId> --to vault|char [--char ID] [--count N]
  equip <instanceId> --char ID
  postmaster [--char ID] [--pull <instanceId>] [--count N]
  godrolls [--wishlist SRC] [--search TERM] [--missing] [--source]
                              cross your owned weapons against a DIM wishlist
  vault [--wishlist SRC] [--lock-keepers]
                              meta-only vault triage -> dismantle_list.csv;
                              --lock-keepers locks every KEEP so a manual
                              dismantle spree can't delete a god roll
  raw <path> [--post] [--body JSON]
                              call ANY /Platform endpoint (covers the whole API)
  selftest                    offline checks, no network

Read commands (whoami, chars, inv, item, source, postmaster, godrolls) take
--json for machine-readable output an agent can parse.

Per-command help: python bungie.py <command> -h
"""
import argparse, json, os, sys, time, webbrowser
from collections import defaultdict
from urllib.parse import urlencode, urlparse, parse_qs
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)                 # so token.json / manifest caches are the folder's

# ===== credentials (local bungie_secrets.py, else env; never commit real keys) =====
try:
    from bungie_secrets import API_KEY, CLIENT_ID, CLIENT_SECRET
except ImportError:
    API_KEY       = os.environ.get("BUNGIE_API_KEY", "")
    CLIENT_ID     = os.environ.get("BUNGIE_CLIENT_ID", "")
    CLIENT_SECRET = os.environ.get("BUNGIE_CLIENT_SECRET", "")

BASE         = "https://www.bungie.net/Platform"
ROOT         = "https://www.bungie.net"
REDIRECT_URL = "https://localhost:7777/callback"
TOKENF, MANIF = "token.json", "manifest_items.json"
IDX_F, COLL_F, SRC_F = "manifest_index.json", "collectibles.json", "sources.json"
VOLTRON = "https://raw.githubusercontent.com/48klocs/dim-wish-list-sources/master/voltron.txt"

# vault triage tuning (ruthless meta-only defaults)
KEEP_PER_HASH   = 1     # copies kept of each non-wishlist gun/armor hash
MIN_ARMOR_TOTAL = 60    # legendary armor under this total stat = junk

ARMOR_STATS = (2996146975, 392767087, 1943323491,    # mob, res, rec
               1735777505, 144602215, 4244567218)     # dis, int, str
WEAPON, ARMOR = 3, 2
WILDCARD  = -69420             # DIM "applies to all items" item id
VAULT_B   = 138197802         # bucketHash for the vault
POST_B    = 215593132         # bucketHash for a character's postmaster
CLASSES   = {0: "Titan", 1: "Hunter", 2: "Warlock", 3: "?"}
TIERS     = {2: "Basic", 3: "Common", 4: "Rare", 5: "Legendary", 6: "Exotic"}


# ===== auth =============================================================
def _save_token(t):
    t["expires_at"] = time.time() + t.get("expires_in", 3600) - 60
    json.dump(t, open(TOKENF, "w"))


def _exchange(data):
    # Confidential app: HTTP Basic auth. Public app (no secret): client_id in body.
    auth = (CLIENT_ID, CLIENT_SECRET) if CLIENT_SECRET else None
    if not CLIENT_SECRET:
        data = {**data, "client_id": CLIENT_ID}
    r = requests.post(f"{BASE}/App/OAuth/Token/", data=data, auth=auth,
                      headers={"Content-Type": "application/x-www-form-urlencoded"})
    r.raise_for_status()
    t = r.json(); _save_token(t); return t


def _gen_cert():
    """Self-signed localhost cert to serve the https redirect. Needs cryptography."""
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
    """Local https server on the redirect port; return the OAuth code, or None."""
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


# ===== manifest + caches ================================================
def load_manifest():
    if os.path.exists(MANIF):
        return json.load(open(MANIF))
    print("downloading item manifest (~once)...")
    m    = api("/Destiny2/Manifest/")
    path = m["jsonWorldComponentContentPaths"]["en"]["DestinyInventoryItemDefinition"]
    defs = requests.get(ROOT + path).json()
    json.dump(defs, open(MANIF, "w"))
    return defs


def index():
    """Slim {hash: {name, type, tier}} map. Built once from the manifest, cached.
    ponytail: JSON scan; move to the SQLite manifest if lookups get hot."""
    if os.path.exists(IDX_F):
        return json.load(open(IDX_F, encoding="utf-8"))
    defs = load_manifest()
    idx  = {h: {"name": d["displayProperties"]["name"],
                "type": d.get("itemType", 0),
                "tier": d.get("inventory", {}).get("tierType", 0)}
           for h, d in defs.items()
           if d.get("displayProperties", {}).get("name")}
    json.dump(idx, open(IDX_F, "w", encoding="utf-8"))
    return idx


def collectibles():
    if os.path.exists(COLL_F):
        return json.load(open(COLL_F, encoding="utf-8"))
    m    = api("/Destiny2/Manifest/")               # public, no token needed
    path = m["jsonWorldComponentContentPaths"]["en"]["DestinyCollectibleDefinition"]
    print("downloading collectible defs (~once)...")
    c    = requests.get(ROOT + path).json()
    json.dump(c, open(COLL_F, "w", encoding="utf-8"))
    return c


def sources():
    """Slim {itemHash: sourceString}. Built once from full manifest + collectibles."""
    if os.path.exists(SRC_F):
        return json.load(open(SRC_F, encoding="utf-8"))
    defs, coll = load_manifest(), collectibles()
    out = {}
    for h, d in defs.items():
        ch = d.get("collectibleHash")
        if ch:
            s = coll.get(str(ch), {}).get("sourceString", "").strip()
            if s:
                out[h] = s
    json.dump(out, open(SRC_F, "w", encoding="utf-8"))
    return out


def search_defs(idx, term, n=15):
    """Pure: item defs whose name contains `term` (or an exact hash), best-fit first."""
    if term.isdigit() and term in idx:
        return [(term, idx[term])]
    t    = term.lower()
    hits = [(h, v) for h, v in idx.items() if t in v["name"].lower()]
    hits.sort(key=lambda x: (len(x[1]["name"]), x[1]["name"].lower()))
    return hits[:n]


# ===== wishlist (DIM voltron format) ====================================
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


def matched_set(it, wl):
    """The wishlist perk-set this instance satisfies, or None."""
    for req in wl.get(it["hash"], ()):                 # empty req (item-only line) matches any roll
        if req <= it["perks"]:
            return req
    for req in wl.get(WILDCARD, ()):                   # wildcard must name perks
        if req and req <= it["perks"]:
            return req
    return None


# ===== vault triage rules (pure, testable, no network) ==================
def classify(items, wishlist=None, keep_per_hash=KEEP_PER_HASH, min_armor_total=MIN_ARMOR_TOTAL):
    wl_on = wishlist is not None
    for it in items:                                   # crafted weapons are protected like wishlist rolls
        it["verdict"], it["reason"] = "keep", ""
        it["wl"] = it.get("crafted", False) or (matched_set(it, wishlist) is not None if wl_on else False)

    for it in items:                                   # greens + blues
        if it["tierType"] <= 4:
            it["verdict"], it["reason"] = "junk", "rare or below"

    for it in items:                                   # weak legendary armor (wishlist match spared)
        if (it["verdict"] == "keep" and not it["wl"] and it["itemType"] == ARMOR
                and it["tierType"] == 5 and it["total"] < min_armor_total):
            it["verdict"], it["reason"] = "junk", f"armor total {it['total']} < {min_armor_total}"

    if wl_on:                                           # meta gate: non-wishlist LEGENDARY weapons are junk
        for it in items:                               # exotics are build-defining -> never gated
            if (it["verdict"] == "keep" and it["itemType"] == WEAPON
                    and it["tierType"] == 5 and not it["wl"]):
                it["verdict"], it["reason"] = "junk", "not a wishlist roll"

    groups = defaultdict(list)                          # dupes among survivors (wishlist rolls exempt)
    for it in items:
        if it["verdict"] == "keep" and not it["wl"] and it["itemType"] in (WEAPON, ARMOR):
            groups[it["hash"]].append(it)
    for grp in groups.values():                         # keep the best copy (gearTier > level > power)
        grp.sort(key=lambda x: (x.get("gearTier", 0), x.get("level", 0), x.get("power", 0)), reverse=True)
        for extra in grp[keep_per_hash:]:
            extra["verdict"], extra["reason"] = "junk", "duplicate (lower tier)"
    return items


# ===== account / inventory ==============================================
def membership(token):
    """(membershipType, membershipId) for the primary Destiny account."""
    me   = api("/User/GetMembershipsForCurrentUser/", token)
    mems = me["destinyMemberships"]
    mem  = next((m for m in mems if str(m["membershipId"]) == str(me.get("primaryMembershipId"))),
                mems[0])
    return mem["membershipType"], mem["membershipId"]


def characters(token, mtype, mid):
    prof = api(f"/Destiny2/{mtype}/Profile/{mid}/?components=200", token)
    return prof["characters"]["data"]


def first_char(token, mtype, mid):
    return next(iter(characters(token, mtype, mid)))


def all_items(token, mtype, mid, comps="102,201,205,300"):
    """(list of (item, owner), instances_comp). owner is 'vault' or a characterId."""
    prof = api(f"/Destiny2/{mtype}/Profile/{mid}/?components={comps}", token)
    inst = prof.get("itemComponents", {}).get("instances", {}).get("data", {})
    out  = [(it, "vault") for it in prof["profileInventory"]["data"]["items"]]
    for cid, inv in prof.get("characterInventories", {}).get("data", {}).items():
        out += [(it, cid) for it in inv["items"]]
    for cid, eq in prof.get("characterEquipment", {}).get("data", {}).items():
        out += [(it, cid) for it in eq["items"]]
    return out, inst


def find_instance(token, mtype, mid, iid):
    """Locate one instanced item -> (itemHash, owner). owner is 'vault' or characterId."""
    items, _ = all_items(token, mtype, mid, comps="102,201,205")
    for it, owner in items:
        if str(it.get("itemInstanceId")) == str(iid):
            return it["itemHash"], owner
    sys.exit(f"instance {iid} not found in your inventory")


def build_inventory(token, defs):
    """Weapons + armor across vault and characters, with perks/stats resolved.
    Returns (items, membershipType, char_id). char_id = any character, for locking."""
    mtype, mid = membership(token)
    prof = api(f"/Destiny2/{mtype}/Profile/{mid}/?components=102,200,201,300,304,305", token)
    char_id = next(iter(prof["characters"]["data"]))
    comp = prof.get("itemComponents", {})
    inst = comp.get("instances", {}).get("data", {})
    stat = comp.get("stats", {}).get("data", {})
    sock = comp.get("sockets", {}).get("data", {})

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
        d_inst = inst.get(iid, {})
        out.append(dict(
            hash=it["itemHash"],
            name=d["displayProperties"]["name"],
            itemType=d["itemType"],
            tierType=d.get("inventory", {}).get("tierType", 0),
            power=d_inst.get("primaryStat", {}).get("value", 0),
            gearTier=d_inst.get("gearTier", 0),          # 2026 Frontiers quality tier (0-5)
            level=d_inst.get("itemLevel", 0),            # weapon power was flattened; itemLevel is meaningful
            total=total,
            perks=perks,
            crafted=bool(it.get("state", 0) & 8),        # DestinyItemState.Crafted
            instanceId=iid,
            owner=owner,                                 # character id to lock this item through
            location="vault" if it.get("bucketHash") == VAULT_B else "char",
        ))
    return out, mtype, char_id


def set_lock(token, mtype, char, iid, state):
    api("/Destiny2/Actions/Items/SetLockState/", token, "POST",
        {"state": state, "itemId": iid, "characterId": char, "membershipType": mtype})


def _perk_list(defs, hashes):
    return [defs.get(str(h), {}).get("displayProperties", {}).get("name", "") or str(h) for h in hashes]


def _perk_names(defs, hashes, cap=4):
    names = [n for n in _perk_list(defs, hashes) if n]
    return ", ".join(names[:cap]) + (" ..." if len(names) > cap else "")


def _emit(data):
    print(json.dumps(data, indent=2, default=str))


# ===== commands =========================================================
def cmd_login(a):
    get_token()
    print("logged in; token cached in token.json")


def cmd_whoami(a):
    token = get_token()
    me = api("/User/GetMembershipsForCurrentUser/", token)
    u  = me.get("bungieNetUser", {})
    prim = str(me.get("primaryMembershipId"))
    data = {"name": u.get("uniqueName", u.get("displayName")),
            "membershipId": u.get("membershipId"),
            "memberships": [{"type": m["membershipType"], "id": m["membershipId"],
                             "displayName": m.get("displayName"),
                             "primary": str(m["membershipId"]) == prim}
                            for m in me["destinyMemberships"]]}
    if a.json:
        return _emit(data)
    print(f"{data['name']}  (membershipId {data['membershipId']})")
    for m in data["memberships"]:
        print(f"  type {m['type']}  id {m['id']}  {m['displayName'] or ''}{' *primary' if m['primary'] else ''}")


def cmd_chars(a):
    token = get_token()
    mtype, mid = membership(token)
    data = [{"id": cid, "class": CLASSES.get(c["classType"], "?"), "light": c.get("light"),
             "minutesPlayed": int(c.get("minutesPlayedTotal", 0)),
             "lastPlayed": c.get("dateLastPlayed", "")[:10]}
            for cid, c in characters(token, mtype, mid).items()]
    if a.json:
        return _emit(data)
    for c in data:
        print(f"{c['id']}  {c['class']:7}  light {c['light']:>4}  "
              f"{c['minutesPlayed']//60}h played  last {c['lastPlayed']}")


def cmd_inv(a):
    token = get_token()
    mtype, mid = membership(token)
    idx = index()
    items, inst = all_items(token, mtype, mid)
    rows = []
    for it, owner in items:
        iid = it.get("itemInstanceId")
        if not iid:
            continue
        info = idx.get(str(it["itemHash"]))
        if not info:
            continue
        if a.vault and owner != "vault":            continue
        if a.char  and owner != a.char:             continue
        if a.type == "weapon" and info["type"] != WEAPON: continue
        if a.type == "armor"  and info["type"] != ARMOR:  continue
        if a.search and a.search.lower() not in info["name"].lower(): continue
        d = inst.get(iid, {})
        rows.append({"name": info["name"], "hash": it["itemHash"], "instanceId": iid,
                     "level": d.get("itemLevel", 0), "gearTier": d.get("gearTier", 0),
                     "rarity": TIERS.get(info["tier"], "?"),
                     "location": "vault" if owner == "vault" else owner})
    rows.sort(key=lambda r: (-r["level"], r["name"]))
    if a.json:
        return _emit(rows)
    for r in rows:
        loc = "vault" if r["location"] == "vault" else r["location"][-6:]
        print(f"T{r['gearTier']} L{r['level']:<3}  {r['rarity']:9}  {r['name']:38.38}  {loc:>6}  {r['instanceId']}")
    print(f"\n{len(rows)} items")


def cmd_item(a):
    idx = index()
    hits = search_defs(idx, a.query, a.n)
    if a.json:
        return _emit([{"hash": h, "name": v["name"], "type": v["type"],
                       "rarity": TIERS.get(v["tier"], "?")} for h, v in hits])
    for h, v in hits:
        print(f"{h:>12}  {TIERS.get(v['tier'],'?'):9}  type {v['type']:>2}  {v['name']}")
    if not hits:
        print("no match")


def cmd_source(a):
    idx, srcs = index(), sources()
    seen = {}                                   # name -> best source (a real one wins over blank)
    for h, v in search_defs(idx, a.query, a.n * 3):
        s = srcs.get(h, "")
        if v["name"] not in seen or (s and not seen[v["name"]]):
            seen[v["name"]] = s
    picked = list(seen.items())[:a.n]
    if a.json:
        return _emit([{"name": name, "source": s or None} for name, s in picked])
    for name, s in picked:
        print(f"{name:28.28}  {s or '(no collectible source - world drop / vendor)'}")
    if not seen:
        print("no match")


def cmd_lock(a):
    token = get_token(); mtype, mid = membership(token)
    set_lock(token, mtype, first_char(token, mtype, mid), a.instance, True)
    print(f"locked {a.instance}")


def cmd_unlock(a):
    token = get_token(); mtype, mid = membership(token)
    set_lock(token, mtype, first_char(token, mtype, mid), a.instance, False)
    print(f"unlocked {a.instance}")


def cmd_transfer(a):
    token = get_token()
    mtype, mid = membership(token)
    ih, owner = find_instance(token, mtype, mid, a.instance)
    if a.to == "vault":
        if owner == "vault":
            return print("already in vault")
        cid, to_vault = owner, True
    else:
        cid, to_vault = a.char or first_char(token, mtype, mid), False
    api("/Destiny2/Actions/Items/TransferItem/", token, "POST",
        {"itemReferenceHash": ih, "stackSize": a.count, "transferToVault": to_vault,
         "itemId": a.instance, "characterId": cid, "membershipType": mtype})
    print(f"transferred {a.instance} -> {a.to}")
    # ponytail: char->char must hop through the vault; run twice if that's what you need.


def cmd_equip(a):
    token = get_token()
    mtype, _ = membership(token)
    api("/Destiny2/Actions/Items/EquipItem/", token, "POST",
        {"itemId": a.instance, "characterId": a.char, "membershipType": mtype})
    print(f"equipped {a.instance} on {a.char}")


def cmd_postmaster(a):
    token = get_token()
    mtype, mid = membership(token)
    idx = index()
    if a.pull:
        ih, owner = find_instance(token, mtype, mid, a.pull)
        api("/Destiny2/Actions/Items/PullFromPostmaster/", token, "POST",
            {"itemReferenceHash": ih, "stackSize": a.count, "itemId": a.pull,
             "characterId": owner, "membershipType": mtype})
        return _emit({"pulled": a.pull}) if a.json else print(f"pulled {a.pull}")
    items, _ = all_items(token, mtype, mid, comps="201,300")
    data = []
    for it, owner in items:
        if it.get("bucketHash") != POST_B:            continue
        if a.char and owner != a.char:                continue
        info = idx.get(str(it["itemHash"]), {"name": f"hash {it['itemHash']}"})
        data.append({"owner": owner, "name": info["name"], "quantity": it.get("quantity", 1),
                     "instanceId": it.get("itemInstanceId")})
    if a.json:
        return _emit(data)
    for d in data:
        print(f"{d['owner'][-6:]}  x{d['quantity']:<3}  {d['name']:38.38}  {d['instanceId'] or '-'}")


def cmd_godrolls(a):
    token = get_token()
    wl    = load_wishlist(a.wishlist)
    if not a.json:
        print(f"wishlist: {len(wl)} entries")
    defs  = load_manifest()
    srcs  = sources() if (a.source or a.json) else {}
    items, _, _ = build_inventory(token, defs)
    guns  = [it for it in items if it["itemType"] == WEAPON
             and (not a.search or a.search.lower() in it["name"].lower())]
    byhash = defaultdict(list)
    for it in guns:
        byhash[it["hash"]].append(it)

    data = []
    for h, grp in sorted(byhash.items(), key=lambda kv: kv[1][0]["name"].lower()):
        name = grp[0]["name"]
        good = [(it, req) for it, req in ((it, matched_set(it, wl)) for it in grp)
                if req is not None or it.get("crafted")]
        src  = srcs.get(str(h))
        if a.missing:
            if good or h not in wl:
                continue
            data.append({"name": name, "hash": h, "owned": len(grp), "source": src,
                         "wants": [_perk_list(defs, s) for s in wl[h] if s]})
        else:
            if not good:
                continue
            data.append({"name": name, "hash": h, "owned": len(grp), "godRolls": len(good), "source": src,
                         "instances": [{"instanceId": it["instanceId"], "gearTier": it["gearTier"],
                                        "level": it["level"], "location": it["location"],
                                        "crafted": bool(it.get("crafted") and req is None),
                                        "perks": _perk_list(defs, req or [])} for it, req in good]})
    if a.json:
        return _emit(data)

    hits = 0
    for d in data:
        if a.missing:
            srct  = f"\n   [{d['source']}]" if a.source and d["source"] else ""
            wants = " | ".join(", ".join(w[:4]) for w in d["wants"][:3]) or "(any roll)"
            print(f"{d['name']:34.34} x{d['owned']}{srct}\n   wants: {wants}")
        else:
            srct = f"   [{d['source']}]" if a.source and d["source"] else ""
            print(f"{d['name']:34.34} x{d['owned']} owned, {d['godRolls']} god roll{srct}")
            for ins in d["instances"]:
                tag = "crafted" if ins["crafted"] else \
                    ", ".join(ins["perks"][:4]) + (" ..." if len(ins["perks"]) > 4 else "")
                print(f"   T{ins['gearTier']} L{ins['level']:<3}  {ins['location']:>5}  {ins['instanceId']}  {tag}")
                hits += 1
    print(f"\n{'missing ' + str(len(data)) if a.missing else str(hits) + ' god-roll instances'}")


def cmd_vault(a):
    """Meta-only triage -> dismantle_list.csv; optionally lock every keeper."""
    wishlist = load_wishlist(a.wishlist) if a.wishlist else None
    if wishlist is not None:
        print(f"wishlist: {len(wishlist)} entries")
    token = get_token()
    defs  = load_manifest()
    items, mtype, _ = build_inventory(token, defs)
    classify(items, wishlist=wishlist)
    junk = [it for it in items if it["verdict"] == "junk"]
    keep = [it for it in items if it["verdict"] == "keep"]
    junk.sort(key=lambda x: (x["itemType"], x["reason"], -x["level"]))

    with open("dismantle_list.csv", "w", encoding="utf-8") as f:
        f.write("name,type,gearTier,level,total,location,reason\n")
        for it in junk:
            t = "weapon" if it["itemType"] == WEAPON else "armor"
            f.write(f"\"{it['name']}\",{t},{it['gearTier']},{it['level']},"
                    f"{it['total']},{it['location']},\"{it['reason']}\"\n")

    print(f"\n{len(items)} instanced items  |  KEEP {len(keep)}  |  JUNK {len(junk)}")
    print("wrote dismantle_list.csv")

    if a.lock_keepers:
        print(f"locking {len(keep)} keepers...")
        for i, it in enumerate(keep, 1):
            try:
                set_lock(token, mtype, it["owner"], it["instanceId"], True)
            except Exception as e:
                print(f"  lock failed {it['name']}: {e}")
            time.sleep(0.15)            # ponytail: fixed throttle; raise if Bungie 429s
            if i % 25 == 0:
                print(f"  {i}/{len(keep)}")
        print("keepers locked. Now shard the dismantle_list safely.")


def cmd_raw(a):
    token  = get_token()
    body   = json.loads(a.body) if a.body else None
    method = "POST" if (a.post or body) else "GET"
    out    = api(a.path, token, method, body)
    text   = json.dumps(out, indent=2)
    print(text[:20000] + ("\n... (truncated)" if len(text) > 20000 else ""))


def cmd_selftest(a):
    # --- CLI helpers: search + wishlist matching ---
    idx = {"1": {"name": "Fatebringer", "type": WEAPON, "tier": 5},
           "2": {"name": "Fate of All Fools", "type": WEAPON, "tier": 6},
           "3": {"name": "Chromatic Fire", "type": ARMOR, "tier": 6}}
    assert [h for h, _ in search_defs(idx, "fate")] == ["1", "2"], "substring+ranking"
    assert [h for h, _ in search_defs(idx, "2")] == ["2"], "exact-hash lookup"
    assert search_defs(idx, "nope") == [], "no match"
    assert CLASSES[1] == "Hunter" and TIERS[6] == "Exotic"
    wl = {5: [frozenset({11, 22})], WILDCARD: [frozenset({99})]}
    assert matched_set({"hash": 5, "perks": frozenset({11, 22, 33})}, wl) == frozenset({11, 22})
    assert matched_set({"hash": 5, "perks": frozenset({11})}, wl) is None
    assert matched_set({"hash": 7, "perks": frozenset({99})}, wl) == frozenset({99})

    # --- vault triage rules ---
    base = lambda **k: {"total": 0, "power": 0, "level": 0, "gearTier": 0, "perks": frozenset(), **k}
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

    wl2 = {100: [frozenset({11, 22})], WILDCARD: [frozenset({99})]}
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
    v = {x["instanceId"]: x["verdict"] for x in classify(items, wishlist=wl2)}
    assert v == dict(g="keep", h="keep", i="junk", j="keep", k="junk", l="junk", m="keep", n="keep"), v
    print("selftest ok")


# ===== dispatch =========================================================
def main():
    ap  = argparse.ArgumentParser(description="Single CLI over the Bungie.net Platform API")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    p = sub.add_parser("whoami"); p.set_defaults(fn=cmd_whoami); p.add_argument("--json", action="store_true")
    p = sub.add_parser("chars");  p.set_defaults(fn=cmd_chars);  p.add_argument("--json", action="store_true")

    p = sub.add_parser("inv"); p.set_defaults(fn=cmd_inv)
    p.add_argument("--char"); p.add_argument("--vault", action="store_true")
    p.add_argument("--search"); p.add_argument("--type", choices=["weapon", "armor"])
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("item"); p.set_defaults(fn=cmd_item)
    p.add_argument("query"); p.add_argument("-n", type=int, default=15); p.add_argument("--json", action="store_true")

    p = sub.add_parser("source"); p.set_defaults(fn=cmd_source)
    p.add_argument("query"); p.add_argument("-n", type=int, default=10); p.add_argument("--json", action="store_true")

    for name, fn in (("lock", cmd_lock), ("unlock", cmd_unlock)):
        p = sub.add_parser(name); p.set_defaults(fn=fn); p.add_argument("instance")

    p = sub.add_parser("transfer"); p.set_defaults(fn=cmd_transfer)
    p.add_argument("instance"); p.add_argument("--to", choices=["vault", "char"], required=True)
    p.add_argument("--char"); p.add_argument("--count", type=int, default=1)

    p = sub.add_parser("equip"); p.set_defaults(fn=cmd_equip)
    p.add_argument("instance"); p.add_argument("--char", required=True)

    p = sub.add_parser("postmaster"); p.set_defaults(fn=cmd_postmaster)
    p.add_argument("--char"); p.add_argument("--pull"); p.add_argument("--count", type=int, default=1)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("godrolls"); p.set_defaults(fn=cmd_godrolls)
    p.add_argument("--wishlist", default=VOLTRON); p.add_argument("--search")
    p.add_argument("--missing", action="store_true"); p.add_argument("--source", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("vault"); p.set_defaults(fn=cmd_vault)
    p.add_argument("--wishlist", help="DIM voltron file/URL; enables the meta gate")
    p.add_argument("--lock-keepers", action="store_true")

    p = sub.add_parser("raw"); p.set_defaults(fn=cmd_raw)
    p.add_argument("path"); p.add_argument("--post", action="store_true"); p.add_argument("--body")

    a = ap.parse_args()
    if a.cmd != "selftest" and not API_KEY:
        sys.exit("No API key. Copy bungie_secrets.example.py -> bungie_secrets.py and fill it in "
                 "(or set BUNGIE_API_KEY / BUNGIE_CLIENT_ID).")
    a.fn(a)


if __name__ == "__main__":
    main()
