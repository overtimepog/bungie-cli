#!/usr/bin/env python3
"""bungie.py - a thin, extensible CLI over the Bungie.net Platform API.

Reuses the proven auth / api / manifest plumbing in vault_clean.py (same folder,
same app credentials, shared token.json + manifest_items.json caches). The CLI
chdir's into its own folder so those caches resolve no matter where you run it.

Commands:
  login                       force/refresh the OAuth login
  whoami                      current Bungie user + Destiny memberships
  chars                       your characters (class, light, playtime)
  inv [--char ID] [--vault] [--search TERM] [--type weapon|armor]
                              list instanced items with names + power
  item <name|hash> [-n N]     search the manifest for an item definition
  source <name|hash> [-n N]   where an item drops (collectible source string)
  lock <instanceId>           lock an item      (unlock: same with unlock)
  unlock <instanceId>
  transfer <instanceId> --to vault|char [--char ID] [--count N]
  equip <instanceId> --char ID
  postmaster [--char ID] [--pull <instanceId>] [--count N]
  godrolls [--wishlist SRC] [--search TERM] [--missing]
                              cross your owned weapons against the DIM voltron
                              wishlist: which guns you own ARE god rolls (default),
                              or --missing = own the gun, not the roll
  vault ...                   delegate to vault_clean.py (its own flags)
  raw <path> [--post] [--body JSON]
                              call ANY /Platform endpoint (covers the whole API)
  selftest                    offline checks, no network

Per-command help: python bungie.py <command> -h
"""
import argparse, json, os, subprocess, sys
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)                 # so token.json / manifest_items.json are the folder's
import vault_clean as vc       # reuse get_token, api, load_manifest, constants

IDX_F     = "manifest_index.json"
COLL_F    = "collectibles.json"
SRC_F     = "sources.json"
VOLTRON   = "https://raw.githubusercontent.com/48klocs/dim-wish-list-sources/master/voltron.txt"
VAULT_B   = 138197802          # bucketHash for the vault
POST_B     = 215593132         # bucketHash for a character's postmaster
WEAPON, ARMOR = 3, 2
CLASSES   = {0: "Titan", 1: "Hunter", 2: "Warlock", 3: "?"}
TIERS     = {2: "Basic", 3: "Common", 4: "Rare", 5: "Legendary", 6: "Exotic"}


# ===== shared helpers ====================================================
def membership(token):
    """Return (membershipType, membershipId) for the primary Destiny account."""
    me   = vc.api("/User/GetMembershipsForCurrentUser/", token)
    mems = me["destinyMemberships"]
    mem  = next((m for m in mems if str(m["membershipId"]) == str(me.get("primaryMembershipId"))),
                mems[0])
    return mem["membershipType"], mem["membershipId"]


def characters(token, mtype, mid):
    prof = vc.api(f"/Destiny2/{mtype}/Profile/{mid}/?components=200", token)
    return prof["characters"]["data"]


def first_char(token, mtype, mid):
    return next(iter(characters(token, mtype, mid)))


def index():
    """Slim {hash: {name, type, tier}} map. Built once from the 214MB manifest,
    then cached small so name lookups are cheap.
    ponytail: JSON scan; move to the SQLite manifest if lookups get hot."""
    if os.path.exists(IDX_F):
        return json.load(open(IDX_F, encoding="utf-8"))
    defs = vc.load_manifest()
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
    m    = vc.api("/Destiny2/Manifest/")            # public, no token needed
    path = m["jsonWorldComponentContentPaths"]["en"]["DestinyCollectibleDefinition"]
    print("downloading collectible defs (~once)...")
    c    = requests.get(vc.ROOT + path).json()
    json.dump(c, open(COLL_F, "w", encoding="utf-8"))
    return c


def sources():
    """Slim {itemHash: sourceString}. Built once from full manifest + collectibles."""
    if os.path.exists(SRC_F):
        return json.load(open(SRC_F, encoding="utf-8"))
    defs, coll = vc.load_manifest(), collectibles()
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


def all_items(token, mtype, mid, comps="102,201,205,300"):
    """Yield (item_dict, owner) across vault + every character inv/equipment.
    owner is 'vault' or a characterId. Returns (list, instances_comp)."""
    prof = vc.api(f"/Destiny2/{mtype}/Profile/{mid}/?components={comps}", token)
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


# ===== commands ==========================================================
def cmd_login(a):
    vc.get_token()
    print("logged in; token cached in token.json")


def cmd_whoami(a):
    token = vc.get_token()
    me = vc.api("/User/GetMembershipsForCurrentUser/", token)
    u  = me.get("bungieNetUser", {})
    print(f"{u.get('uniqueName', u.get('displayName','?'))}  (membershipId {u.get('membershipId','?')})")
    for m in me["destinyMemberships"]:
        star = " *primary" if str(m["membershipId"]) == str(me.get("primaryMembershipId")) else ""
        print(f"  type {m['membershipType']}  id {m['membershipId']}  {m.get('displayName','')}{star}")


def cmd_chars(a):
    token = vc.get_token()
    mtype, mid = membership(token)
    for cid, c in characters(token, mtype, mid).items():
        print(f"{cid}  {CLASSES.get(c['classType'],'?'):7}  light {c.get('light','?'):>4}  "
              f"{int(c.get('minutesPlayedTotal',0))//60}h played  last {c.get('dateLastPlayed','')[:10]}")


def cmd_inv(a):
    token = vc.get_token()
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
        d     = inst.get(iid, {})
        level = d.get("itemLevel", 0)
        gt    = d.get("gearTier", 0)
        rows.append((info["name"], level, gt, TIERS.get(info["tier"], "?"),
                     "vault" if owner == "vault" else owner[-6:], iid))
    rows.sort(key=lambda r: (-r[1], r[0]))
    for name, level, gt, tier, loc, iid in rows:
        print(f"T{gt} L{level:<3}  {tier:9}  {name:38.38}  {loc:>6}  {iid}")
    print(f"\n{len(rows)} items")


def cmd_item(a):
    idx = index()
    hits = search_defs(idx, a.query, a.n)
    for h, v in hits:
        print(f"{h:>12}  {TIERS.get(v['tier'],'?'):9}  type {v['type']:>2}  {v['name']}")
    if not hits:
        print("no match")


def _set_lock(token, state, iid):
    mtype, mid = membership(token)
    char = first_char(token, mtype, mid)
    vc.api("/Destiny2/Actions/Items/SetLockState/", token, "POST",
           {"state": state, "itemId": iid, "characterId": char, "membershipType": mtype})
    print(f"{'locked' if state else 'unlocked'} {iid}")


def cmd_lock(a):   _set_lock(vc.get_token(), True,  a.instance)
def cmd_unlock(a): _set_lock(vc.get_token(), False, a.instance)


def cmd_transfer(a):
    token = vc.get_token()
    mtype, mid = membership(token)
    ih, owner = find_instance(token, mtype, mid, a.instance)
    if a.to == "vault":
        if owner == "vault":
            return print("already in vault")
        cid, to_vault = owner, True
    else:
        cid, to_vault = a.char or first_char(token, mtype, mid), False
    vc.api("/Destiny2/Actions/Items/TransferItem/", token, "POST",
           {"itemReferenceHash": ih, "stackSize": a.count, "transferToVault": to_vault,
            "itemId": a.instance, "characterId": cid, "membershipType": mtype})
    print(f"transferred {a.instance} -> {a.to}")
    # ponytail: char->char must hop through the vault; run twice if that's what you need.


def cmd_equip(a):
    token = vc.get_token()
    mtype, _ = membership(token)
    vc.api("/Destiny2/Actions/Items/EquipItem/", token, "POST",
           {"itemId": a.instance, "characterId": a.char, "membershipType": mtype})
    print(f"equipped {a.instance} on {a.char}")


def cmd_postmaster(a):
    token = vc.get_token()
    mtype, mid = membership(token)
    idx = index()
    if a.pull:
        ih, owner = find_instance(token, mtype, mid, a.pull)
        vc.api("/Destiny2/Actions/Items/PullFromPostmaster/", token, "POST",
               {"itemReferenceHash": ih, "stackSize": a.count, "itemId": a.pull,
                "characterId": owner, "membershipType": mtype})
        return print(f"pulled {a.pull}")
    items, inst = all_items(token, mtype, mid, comps="201,300")
    for it, owner in items:
        if it.get("bucketHash") != POST_B:            continue
        if a.char and owner != a.char:                continue
        info = idx.get(str(it["itemHash"]), {"name": f"hash {it['itemHash']}"})
        iid  = it.get("itemInstanceId", "-")
        qty  = it.get("quantity", 1)
        print(f"{owner[-6:]}  x{qty:<3}  {info['name']:38.38}  {iid}")


def cmd_source(a):
    idx, srcs = index(), sources()
    seen = {}                                   # name -> best source (a real one wins over blank)
    for h, v in search_defs(idx, a.query, a.n * 3):
        s = srcs.get(h, "")
        if v["name"] not in seen or (s and not seen[v["name"]]):
            seen[v["name"]] = s
    for name, s in list(seen.items())[:a.n]:
        print(f"{name:28.28}  {s or '(no collectible source - world drop / vendor)'}")
    if not seen:
        print("no match")


def _perk_names(defs, hashes, cap=4):
    names = [defs.get(str(h), {}).get("displayProperties", {}).get("name", "") for h in hashes]
    names = [n for n in names if n]
    return ", ".join(names[:cap]) + (" ..." if len(names) > cap else "")


def _matched(it, wl):
    """The wishlist perk-set this instance satisfies, or None. Mirrors vault_clean.matches_wishlist."""
    for req in wl.get(it["hash"], ()):
        if req <= it["perks"]:
            return req
    for req in wl.get(vc.WILDCARD, ()):
        if req and req <= it["perks"]:
            return req
    return None


def cmd_godrolls(a):
    from collections import defaultdict
    token = vc.get_token()
    wl    = vc.load_wishlist(a.wishlist)
    print(f"wishlist: {len(wl)} entries")
    defs  = vc.load_manifest()
    srcs  = sources() if a.source else {}
    items, _, _ = vc.build_inventory(token, defs)
    guns  = [it for it in items if it["itemType"] == WEAPON
             and (not a.search or a.search.lower() in it["name"].lower())]
    byhash = defaultdict(list)
    for it in guns:
        byhash[it["hash"]].append(it)

    hits = miss = 0
    for h, grp in sorted(byhash.items(), key=lambda kv: kv[1][0]["name"].lower()):
        name    = grp[0]["name"]
        matched = [(it, _matched(it, wl)) for it in grp]
        good    = [(it, req) for it, req in matched if req is not None or it.get("crafted")]
        if a.missing:
            if good or h not in wl:
                continue
            wants = " | ".join(_perk_names(defs, s) for s in wl[h][:3] if s) or "(any roll)"
            src   = f"\n   [{srcs[str(h)]}]" if a.source and str(h) in srcs else ""
            print(f"{name:34.34} x{len(grp)}{src}\n   wants: {wants}")
            miss += 1
            continue
        if not good:
            continue
        src = f"   [{srcs[str(h)]}]" if a.source and str(h) in srcs else ""
        print(f"{name:34.34} x{len(grp)} owned, {len(good)} god roll{src}")
        for it, req in good:
            tag  = "crafted" if (it.get("crafted") and req is None) else _perk_names(defs, req or [])
            meta = f"T{it['gearTier']} L{it['level']}"
            print(f"   {meta:>7}  {it['location']:>5}  {it['instanceId']}  {tag}")
            hits += 1
    print(f"\n{'missing ' + str(miss) if a.missing else str(hits) + ' god-roll instances'}")


def cmd_vault(a):
    subprocess.run([sys.executable, os.path.join(HERE, "vault_clean.py"), *a.rest], cwd=HERE)


def cmd_raw(a):
    token  = vc.get_token()
    body   = json.loads(a.body) if a.body else None
    method = "POST" if (a.post or body) else "GET"
    out    = vc.api(a.path, token, method, body)
    text   = json.dumps(out, indent=2)
    print(text[:20000] + ("\n... (truncated)" if len(text) > 20000 else ""))


def cmd_selftest(a):
    idx = {"1": {"name": "Fatebringer", "type": WEAPON, "tier": 5},
           "2": {"name": "Fate of All Fools", "type": WEAPON, "tier": 6},
           "3": {"name": "Chromatic Fire", "type": ARMOR, "tier": 6}}
    assert [h for h, _ in search_defs(idx, "fate")] == ["1", "2"], "substring+ranking"
    assert [h for h, _ in search_defs(idx, "2")] == ["2"], "exact-hash lookup"
    assert search_defs(idx, "nope") == [], "no match"
    assert CLASSES[1] == "Hunter" and TIERS[6] == "Exotic"
    wl = {5: [frozenset({11, 22})], vc.WILDCARD: [frozenset({99})]}
    assert _matched({"hash": 5, "perks": frozenset({11, 22, 33})}, wl) == frozenset({11, 22})
    assert _matched({"hash": 5, "perks": frozenset({11})}, wl) is None
    assert _matched({"hash": 7, "perks": frozenset({99})}, wl) == frozenset({99})
    print("selftest ok")


# ===== dispatch ==========================================================
def main():
    ap  = argparse.ArgumentParser(description="CLI over the Bungie.net Platform API")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("whoami").set_defaults(fn=cmd_whoami)
    sub.add_parser("chars").set_defaults(fn=cmd_chars)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)

    p = sub.add_parser("inv"); p.set_defaults(fn=cmd_inv)
    p.add_argument("--char"); p.add_argument("--vault", action="store_true")
    p.add_argument("--search"); p.add_argument("--type", choices=["weapon", "armor"])

    p = sub.add_parser("item"); p.set_defaults(fn=cmd_item)
    p.add_argument("query"); p.add_argument("-n", type=int, default=15)

    p = sub.add_parser("source"); p.set_defaults(fn=cmd_source)
    p.add_argument("query"); p.add_argument("-n", type=int, default=10)

    for name, fn in (("lock", cmd_lock), ("unlock", cmd_unlock)):
        p = sub.add_parser(name); p.set_defaults(fn=fn); p.add_argument("instance")

    p = sub.add_parser("transfer"); p.set_defaults(fn=cmd_transfer)
    p.add_argument("instance"); p.add_argument("--to", choices=["vault", "char"], required=True)
    p.add_argument("--char"); p.add_argument("--count", type=int, default=1)

    p = sub.add_parser("equip"); p.set_defaults(fn=cmd_equip)
    p.add_argument("instance"); p.add_argument("--char", required=True)

    p = sub.add_parser("postmaster"); p.set_defaults(fn=cmd_postmaster)
    p.add_argument("--char"); p.add_argument("--pull"); p.add_argument("--count", type=int, default=1)

    p = sub.add_parser("godrolls"); p.set_defaults(fn=cmd_godrolls)
    p.add_argument("--wishlist", default=VOLTRON); p.add_argument("--search")
    p.add_argument("--missing", action="store_true"); p.add_argument("--source", action="store_true")

    p = sub.add_parser("vault"); p.set_defaults(fn=cmd_vault)
    p.add_argument("rest", nargs=argparse.REMAINDER)

    p = sub.add_parser("raw"); p.set_defaults(fn=cmd_raw)
    p.add_argument("path"); p.add_argument("--post", action="store_true"); p.add_argument("--body")

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
