# bungie-cli

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)
![Platform: Bungie.net API](https://img.shields.io/badge/API-Bungie.net-orange.svg)
![Game: Destiny 2](https://img.shields.io/badge/game-Destiny%202-lightgrey.svg)

A single, self-contained command-line interface over the [Bungie.net Platform API](https://bungie-net.github.io/) for Destiny 2 — one tool an AI agent (or a human) can drive to inspect and act on an account: characters, inventory, item/source lookup, god-roll triage against DIM wishlists, meta-only vault triage with keeper-locking, transfers/locks/equips, and a raw passthrough to every endpoint.

Everything lives in **`bungie.py`** — no other modules to install or run.

## Setup

```bash
pip install -r requirements.txt
cp bungie_secrets.example.py bungie_secrets.py   # then fill in your keys
```

Get an API key + OAuth `client_id` at <https://www.bungie.net/en/Application> (Create New App, redirect URL `https://localhost:7777/callback`, scopes: read inventory + move/equip/destroy). Put them in `bungie_secrets.py` (gitignored) or export `BUNGIE_API_KEY` / `BUNGIE_CLIENT_ID` / `BUNGIE_CLIENT_SECRET`.

First run opens a Bungie approval page; with `cryptography` installed it auto-captures the login (accept the one-time self-signed-cert warning). The token is cached in `token.json` and auto-refreshed.

## Commands

```
python bungie.py whoami | chars
python bungie.py inv [--char ID] [--vault] [--search TERM] [--type weapon|armor]
python bungie.py item <name|hash> [-n N]        # manifest lookup
python bungie.py source <name|hash> [-n N]      # where an item drops
python bungie.py godrolls [--wishlist SRC] [--search TERM] [--missing] [--source]
python bungie.py lock|unlock <instanceId>
python bungie.py transfer <instanceId> --to vault|char [--char ID] [--count N]
python bungie.py equip <instanceId> --char ID
python bungie.py postmaster [--char ID] [--pull <instanceId>]
python bungie.py vault [--wishlist <url|file>] [--lock-keepers]  # triage -> dismantle_list.csv
python bungie.py raw /Any/Platform/Endpoint/ [--post] [--body '{...}']
python bungie.py selftest                       # offline checks
```

## Examples

```bash
# who am I / my characters
python bungie.py whoami
python bungie.py chars

# find a weapon's hash, then where it drops
python bungie.py item "fatebringer"
python bungie.py source "fatebringer"

# browse inventory
python bungie.py inv --vault --type weapon           # weapons in the vault
python bungie.py inv --search "hung jury"            # anything matching a name
python bungie.py inv --char 2305843010616664795      # one character's gear

# god rolls you own, and the ones you still need (with drop sources)
python bungie.py godrolls --source
python bungie.py godrolls --missing --source

# move / equip / protect a specific item (use an instanceId from `inv`)
python bungie.py transfer 6917530071249430515 --to char --char 2305843010616664795
python bungie.py equip 6917530071249430515 --char 2305843010616664795
python bungie.py lock 6917530071249430515

# empty the postmaster
python bungie.py postmaster                          # list
python bungie.py postmaster --pull 6917530071249430515

# vault triage: mark junk vs keepers, lock every keeper, write a shard list
python bungie.py vault \
  --wishlist https://raw.githubusercontent.com/48klocs/dim-wish-list-sources/master/voltron.txt \
  --lock-keepers

# hit any endpoint the CLI doesn't wrap
python bungie.py raw /Destiny2/3/Profile/4611686018499245344/?components=200
```

### God rolls

`godrolls` cross-references the weapons you own against a [DIM voltron wishlist](https://github.com/48klocs/dim-wish-list-sources) (the default) and resolves the winning perks to names:

```bash
python bungie.py godrolls --source          # guns you own that ARE god rolls + where they drop
python bungie.py godrolls --missing --source # own the gun, not the roll (the chase list)
```

### raw

The escape hatch — any endpoint not wrapped as a subcommand is one command away:

```bash
python bungie.py raw /Destiny2/Manifest/
```

## JSON output (for agents)

Most commands take `--json` for machine-readable output. Read commands emit their
data; action commands (`lock`, `unlock`, `transfer`, `equip`, `vault`) emit a JSON
ack of what changed. In JSON mode all progress/status lines are suppressed, so
stdout is pure JSON you can pipe straight into a parser.

```bash
python bungie.py whoami --json
```
```json
{
  "name": "Guardian#1234",
  "membershipId": "24705785",
  "memberships": [
    { "type": 3, "id": "4611686018499245344", "displayName": "Guardian", "primary": true }
  ]
}
```

```bash
python bungie.py godrolls --search maahes --json
```
```json
[
  {
    "name": "Maahes HC4",
    "hash": 1246793994,
    "owned": 3,
    "godRolls": 3,
    "source": "Source: Open Legendary engrams and earn faction rank-up packages.",
    "instances": [
      {
        "instanceId": "6917530071249430515",
        "gearTier": 0,
        "level": 1,
        "location": "vault",
        "crafted": false,
        "perks": ["Corkscrew Rifling", "Golden Tricorn", "Unrelenting", "Appended Mag"]
      }
    ]
  }
]
```

`godrolls --missing --json` instead returns `{ "name", "hash", "owned", "source", "wants": [[perk, ...], ...] }` per weapon.

Action acks:

```bash
python bungie.py lock 6917530071249430515 --json     # {"locked": "6917530071249430515"}
python bungie.py transfer 6917... --to vault --json   # {"transferred": "6917...", "to": "vault"}
python bungie.py vault --lock-keepers --json
```
```json
{ "total": 216, "keep": 184, "junk": 32, "csv": "dismantle_list.csv",
  "lockedKeepers": 184, "lockFailures": [] }
```

A typical agent loop: read state (`inv --json` / `godrolls --json`), decide, act
(`transfer`/`lock` with `--json`), and check the ack.

## Notes

- Caches (`manifest_items.json` ~214 MB, `manifest_index.json`, `collectibles.json`, `sources.json`) are built on first use and gitignored.
- The Bungie API has **no destroy-item endpoint**, so dismantling stays manual; `vault --lock-keepers` locks every keeper first, so a manual dismantle spree can't delete a god roll.
- Weapon "power" was flattened in the 2026 gear-tier system; the CLI shows `gearTier` + `itemLevel` instead.

## License

MIT
