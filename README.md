# localdish

A small web page for your Starlink dish and router that runs on your own computer, talks straight to the dish over
your local network, and keeps working when the internet doesn't. Handy when your phone (and the Starlink app) isn't with you, you're
somewhere with a Mini and a laptop, or you just want to see what the dish is actually saying.

- **Nothing to install.** Python 3.9 or newer and its standard library. No packages, no build step, no accounts.
- **Local only.** The page is served on `127.0.0.1` for this computer alone. It fetches nothing from the internet —
  no fonts, no scripts, no analytics.
- **Gentle.** One connection to the dish and one to the router, at a cadence the gear is happy with: live status once a
  second while the page is open, far less while it's closed. Anything that makes the dish do work happens only when you
  press a button.

> localdish is an independent project. It is not made by, affiliated with, or endorsed by SpaceX or Starlink.

## quick start

```sh
git clone https://github.com/pizzimenti/localdish
cd localdish
python3 localdish.py
```

Then open <http://127.0.0.1:8686>. Your computer must be on the dish's network (its wifi, or a cable to its router).

No dish nearby? `python3 localdish.py --demo` serves a recorded, anonymised Starlink Mini so you can look around.

Prefer a command on your path? With [pipx](https://pipx.pypa.io): `pipx install git+https://github.com/pizzimenti/localdish`,
then `localdish`.

## what you get

- **live:** latency to Starlink's network, download and upload right now, obstruction, uptime, power draw, ethernet
  speed, GPS, alerts, software-update state, and a plain-English headline ("online", "searching for satellites",
  "obstructed", …).
- **the last 15 minutes:** latency and packet loss, throughput and power, every outage with its cause.
- **obstruction map:** what the dish has seen of the sky.
- **aim helper:** for dishes without motors (the Mini, for example), where the dish points now against where it
  wants to point: "turn it about 11° to the left". While the dish is searching the sky it keeps showing the last
  reading, marked with its age.
- **wifi clients:** who is on the router, their signal and band.
- **ping:** the router's latency and loss to well-known services around the world.
- **everything else:** every field the dish and router report, one click away as raw data.

## controls

Each is a button with a confirmation, and one press sends exactly one request.

- **restart** the dish. On a Mini the whole unit restarts, wifi included — expect about two minutes offline.
- **snow melt** mode: automatic, always on, or off.
- **power-save schedule** (sleep): on or off, start time and length.
- **share location** with this computer (off by default on the dish).
- **clear the obstruction map** so the dish re-learns the sky.
- **stow / unstow**, only on dishes with motors.
- **speed test** and **ping** from the router.

A control the dish or router doesn't support is hidden once it says so.

## options

```
python3 localdish.py [--port 8686] [--dish 192.168.100.1] [--router auto|ADDRESS|none] [--demo] [--capture DIR]
```

- `--router auto` (the default) looks for the Starlink router at your default gateway, then `192.168.1.1`.
- If you use your own router with Starlink in bypass mode, the dish is still at `192.168.100.1`; use `--router none`.

## how it works

The dish and router speak gRPC, and also gRPC-web — the same protocol over plain HTTP. localdish uses gRPC-web
with Python's own `http.client`. To know what the messages mean, it asks each device for its own message definitions
through gRPC server reflection when it starts. So it always matches the firmware in front of it, and this repository
contains no Starlink code or protocol files. See [DESIGN.md](DESIGN.md) for the details.

### report a problem / share your dish

`python3 localdish.py --capture DIR` reads your dish once (device info, status, the 15-minute history, the obstruction
map, config, diagnostics and location, 2 s apart) and your router once (device info, status, wifi clients, ping),
then writes one JSON file to `DIR` and exits. It only asks: it changes nothing and starts no test. Before writing, it
scrubs what identifies you. Device, router and account ids, client names, SSIDs, MACs and domains are replaced. LAN,
private and IPv6 addresses are mapped to documentation ranges, and anything named like a password, key or token is
dropped. It prints how many of each kind it replaced, never the values. The file has the same format as
`localdish/demo/mini.json`, so it can go into an issue or become a fixture. Look it over before you share it.

## safety

- The server listens on `127.0.0.1` only, answers only to `localhost` / `127.0.0.1` in the `Host` header, and
  refuses control requests that lack its own header. Another website open in your browser can't restart your dish.
- Fields that look like credentials (passwords, keys, tokens) are removed before anything reaches the page.
- The page never asks the dish for anything on its own. It reads what localdish's single poller has already fetched.

## license

MIT — see [LICENSE](LICENSE).
