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

## Notes

- Caches (`manifest_items.json` ~214 MB, `manifest_index.json`, `collectibles.json`, `sources.json`) are built on first use and gitignored.
- The Bungie API has **no destroy-item endpoint**, so dismantling stays manual; `vault --lock-keepers` locks every keeper first, so a manual dismantle spree can't delete a god roll.
- Weapon "power" was flattened in the 2026 gear-tier system; the CLI shows `gearTier` + `itemLevel` instead.

## License

MIT
