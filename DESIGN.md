# localdish — design

This is the contract the code is built to. When code and this file disagree, one of them is a bug: fix it or fix this.

## goals and limits

- **Standard library only**, Python ≥ 3.9. No runtime dependencies, no build step, no network beyond the dish and router.
- **Local only**: one process, one page, `127.0.0.1`.
- **Gentle with the gear**: one keep-alive connection per device, a fixed cadence, single-flight. The page never
  triggers a device call except through an explicit button.
- **Ship nothing of Starlink's**: schemas are fetched from each device at runtime by gRPC server reflection.
- **Everything visible**: every field a device reports is reachable from the page (raw drawer), not only the ones
  we chose to chart.

Non-goals for 0.1: history kept on disk, remote access, more than one dish, a PyPI release.

## what the devices look like (measured, 2026-09)

- **Dish:** `192.168.100.1`. gRPC on `:9200`, **gRPC-web on `:9201`** (plain HTTP/1.1 POST). One RPC does everything:
  `POST /SpaceX.API.Device.Device/Handle`. Its body is a `SpaceX.API.Device.Request` whose one-of field names the
  operation, and it returns a `SpaceX.API.Device.Response` whose one-of carries the answer.
- **Router:** gRPC on `:9000`, gRPC-web on **`:9001`**, at the LAN gateway. The factory default is `192.168.1.1`, but
  users change it; one Mini measured at `192.168.2.1`.
- **Reflection over gRPC-web works:** `POST /grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo` with a
  `ServerReflectionRequest{file_containing_symbol: "SpaceX.API.Device.Device"}` returns every file the service needs
  in one reply: 19 files, ~155–167 KB, ~0.1 s.
- **Dish and router have different schemas** (e.g. api_version 42 vs 126 on one Mini). Fetch per device.
- **Errors can be "trailers-only":** an HTTP 200 with an empty body, where the status is in the HTTP headers
  (`Grpc-Status: 7`, `Grpc-Message: GetLocation requests disabled due to policy`). Otherwise the status is in a
  trailer frame (flag `0x80`) at the end of the body: `grpc-status: 0\r\n`. Header names vary in case; read both
  places, case-insensitively.
- **Time bases differ:** `dish_get_history.outages[].start_timestamp_ns` is **GPS time** (ns since 1980-01-06, no leap
  seconds: unix = gps + 315964800 − 18). `event_log.*timestamp_ns` and the router's `utc_ns` are **Unix** ns.
  `software_update_stats.reboot_scheduled_utc_time` is Unix seconds.
- **Location is off by default:** `get_location` → status 7 (PERMISSION_DENIED) until `location_request_mode` is `LOCAL`.
- **The history ring:** `dish_get_history` arrays are 900 samples (1 per second). `current` is a running sample
  counter; the newest sample is at index `(current − 1) % n`, and the oldest valid one is at `current % n` once
  `current ≥ n`.
- **gRPC-web framing:** each message is `flag(1) + length(4, big-endian) + bytes`. Request flag is `0x00`.
  Headers: `content-type: application/grpc-web+proto`, `x-grpc-web: 1`.

## layout

```
localdish.py              launcher for a clone (sys.path + localdish.cli.main)
localdish/
  __init__.py             __version__
  wire.py                 protobuf codec driven by a descriptor set (J1)
  grpcweb.py              gRPC-web client, reflection, Device (J1)
  device.py               read table, control table, request building, availability (J4)
  explain.py              pure functions: headline, alerts, aim, outages, ring, facts (J4)
  poller.py               cadence, single-flight, caches, events (J2)
  server.py               HTTP server, API, guards, static files (J2)
  cli.py                  argument parsing, router discovery, --demo wiring (J2)
  demo.py                 FakeDevice over the recorded fixtures (J2)
  static/index.html app.js app.css   the page (J3)
  demo/mini.json          recorded, scrubbed Starlink Mini (see "fixtures")
tests/                    unittest; offline; fixtures in tests/fixtures/
```

## decoded values: the one convention everything uses

`wire.Schema.decode()` returns plain dicts that survive a JSON round trip unchanged:

| protobuf | decoded as |
|---|---|
| any integer type (incl. 64-bit, sint, fixed) | `int` (never a string) |
| `double` | `float` as is |
| `float` | the shortest decimal that round-trips to the same float32 (`27.45`, not `27.450000762939453`) |
| `bool` / `string` | `bool` / `str` (UTF-8, `errors="replace"`) |
| `bytes` | standard base64 `str` with padding |
| enum | its value **name**; an unknown number stays an `int` |
| message | `dict` |
| repeated | `list` (accept packed and unpacked on the wire) |
| map | `dict` with **string** keys (int keys stringified) |
| field not on the wire | **absent** — no defaults are filled in; consumers use `.get(key, default)` |
| field number not in the schema | key `"#<number>"`: varint → int, fixed32/64 → int, length-delimited → base64 str; a repeat becomes a list |

A decoded `Response` drops its top-level `id`. The fixtures were produced by a reference decoder with exactly these
rules, so they are the golden shape.

## modules

### wire.py (J1)

```python
class Schema:
    @classmethod
    def from_files(cls, files: list[bytes]) -> "Schema"      # serialized FileDescriptorProto blobs (reflection output)
    def message_names(self) -> list[str]                      # full names, e.g. "SpaceX.API.Device.Response"
    def fields(self, message: str) -> dict[str, "Field"]      # by field name
    def enum_values(self, enum: str) -> dict[str, int]
    def decode(self, message: str, data: bytes) -> dict
    def encode(self, message: str, value: dict) -> bytes
```

- `descriptor.proto` field numbers are hard-coded (they are fixed by Google): FileDescriptorProto name 1, package 2,
  dependency 3, message_type 4, enum_type 5; DescriptorProto name 1, field 2, nested_type 3, enum_type 4, options 7
  (MessageOptions.map_entry 7), oneof_decl 8; FieldDescriptorProto name 1, number 3, label 4, type 5, type_name 6,
  oneof_index 9; EnumDescriptorProto name 1, value 2; EnumValueDescriptorProto name 1, number 2.
- `encode` takes field names, enum names or ints, nested dicts, lists; **an empty dict still emits its field**
  (`{"reboot": {}}` → tag + length 0, which is how an operation is selected). Unknown names raise `ValueError`.
- Pure Python; no I/O.

### grpcweb.py (J1)

```python
class GrpcError(Exception):
    code: int; name: str; message: str          # name: "PERMISSION_DENIED", "UNIMPLEMENTED", …

class Client:                                   # one keep-alive http.client.HTTPConnection, thread-safe (a lock)
    def __init__(self, host: str, port: int, timeout: float = 5.0)
    def unary(self, path: str, message: bytes, timeout: float | None = None) -> bytes   # one request message → one response message
    def close(self) -> None

def fetch_schema(client: Client) -> list[bytes]  # reflection; v1alpha then v1; follows missing deps with file_by_filename

class Device:
    def __init__(self, host: str, port: int, *, timeout: float = 5.0)
    def call(self, op: str, fields: dict | None = None, *, timeout: float | None = None) -> dict
        # Request{op: fields or {}} → decoded Response minus "id". The schema is fetched on first use and again after
        # the device has been unreachable ≥ 60 s (it may have rebooted into new firmware).
    @property
    def schema(self) -> "Schema | None"
    def close(self) -> None
```

- Any transport error (refused, reset, timeout, bad framing) **closes the connection** and raises `OSError` /
  `TimeoutError`. The next `call` reconnects. **No retries inside the client**: the caller's cadence is the only clock.
- A non-zero grpc-status raises `GrpcError`. The response `status` field inside a successful body is left to callers.
- Timeouts: default 5 s; callers pass 20 s for history and the obstruction map.

**Settled while building (wire piece):**
- `Field` (from `Schema.fields()`) has `name, number, kind` (descriptor.proto type names), `type_name` (full name, no
  leading dot), `repeated, map, oneof, packed`. Unknown message or enum names raise KeyError. Encode raises ValueError.
- Decode follows protobuf merge rules: a repeated singular message merges, the last oneof member wins, the last map key
  wins. A map entry with no key or value gets the default, the only default ever filled in. A known number with the wrong
  wire type and a field whose type is missing from the schema both become `"#n"`. Malformed input raises ValueError.
- **NaN is kept by the decoder** (the reference keeps it too). The fixtures contain NaN, so **the server turns
  non-finite floats into null** and dumps with `allow_nan=False`.
- Client: before reusing an idle keep-alive, if the socket reads as EOF, reconnect first. That isn't a retry, because
  nothing was sent. If a reply has no grpc-status anywhere, HTTP 200 counts as OK; any other HTTP status maps through
  gRPC's table (404 → UNIMPLEMENTED, 503 → UNAVAILABLE). `grpc-message` is percent-decoded.
- `Device.schema_loaded` (a time.time() stamp) changes on each (re)load; the poller compares it to log the event. A
  GrpcError counts as "answered" for the 60 s unreachable clock. A failed refetch keeps the old schema.
- Measured: history decode 15 ms (55 KB), map 2 ms, reflection parse 13–15 ms. A dish held one keep-alive across 6 s gaps.

### device.py (J4)

```python
READS: dict[str, tuple[str, str, str]]
    # name → (target "dish"|"router", request op, response key), e.g.
    # "dish_status": ("dish", "get_status", "dish_get_status"), "router_status": ("router", "get_status", "wifi_get_status")
def read(dev, name: str) -> dict                    # dev.call(...)[response key], {} if absent

class Control(NamedTuple):
    name: str; label: str; group: str               # group: "restart" | "settings" | "maintenance" | "tests"
    target: str; op: str; confirm: str              # confirm: the question the page asks, lowercase, plain
    params: list[dict]                              # [{"name","type","choices"?,"min"?,"max"?,"label"}]
CONTROLS: list[Control]
def build(name: str, params: dict) -> tuple[str, str, dict]     # → (target, op, request fields); ValueError on bad params
def available(name: str, state: dict) -> tuple[bool, str | None] # state = the cache the poller holds; reason when not
def current(name: str, state: dict) -> dict                      # the control's current values, e.g. {"mode": "ALWAYS_OFF"}
```

Controls for 0.1 (requests verified against a Mini's schema; each one's live effect is verified with the owner present):

| name | group | target → request | params |
|---|---|---|---|
| `restart` | restart | dish → `reboot {}` | — |
| `snow_melt` | settings | dish → `dish_set_config {dish_config: {snow_melt_mode, apply_snow_melt_mode: true}}` | `mode`: AUTO / ALWAYS_ON / ALWAYS_OFF |
| `power_save` | settings | dish → `dish_set_config {dish_config: {power_save_mode, power_save_start_minutes, power_save_duration_minutes, apply_…: true ×3}}` | `enabled` bool, `start_minutes` 0–1439, `duration_minutes` 1–1440 |
| `share_location` | settings | dish → `dish_set_config {dish_config: {location_request_mode: LOCAL/NONE, apply_location_request_mode: true}}` | `share` bool |
| `clear_obstructions` | maintenance | dish → `dish_clear_obstruction_map {}` | — |
| `stow` | maintenance | dish → `dish_stow {unstow}` | `unstow` bool; only when `has_actuators` is `HAS_ACTUATORS_YES` |
| `speedtest` | tests | router → `start_speedtest {}`, then `get_speedtest_status {}` | — |
| `ping` | tests | router → `get_ping {}` | — |

Schedule minutes are in the dish's local day (a Mini reported update hour 3 and scheduled its reboot at 03:54 local).
The page shows them as clock times.

### explain.py (J4), pure functions of decoded dicts

```python
def ring(history: dict) -> dict        # {"n", "current", "count", "latency_ms", "drop", "down_bps", "up_bps", "power_w"} oldest→newest, valid samples only
def outages(history: dict, now_unix: float) -> list[dict]   # newest first: {"cause", "cause_text", "start_unix", "ago_s", "duration_s", "did_switch"} (GPS → Unix)
def headline(status: dict | None, error: str | None) -> dict  # {"text", "tone": "ok"|"warn"|"bad", "detail"}
def alerts(status: dict) -> list[dict]                     # [{"name", "text", "tone"}] (not "key": the server strips keys named like credentials) every true alert, unknown ones worded from the key
def aim(status: dict) -> dict | None                       # dishes without motors; see below
def facts(state: dict) -> list[list]                       # [[label, value_text], …] device, firmware, service, update, config …
def explain(state: dict, now_unix: float) -> dict          # {"headline", "alerts", "aim", "facts"} for /api/state
```

**aim**: from `alignment_stats`. Azimuth is compass degrees and may be negative (−7 = 353). The turn is the shortest
signed difference `want − now` in (−180, 180]. Positive means clockwise seen from above, which is "to the right,
standing behind the dish". Elevation: positive `want − now` means tilt up, toward the sky. Returns
`{"az_now", "az_want", "el_now", "el_want", "turn_deg", "tilt_deg", "ok": |turn| ≤ 5 and |tilt| ≤ 3, "text": […],
"confidence": attitude_estimation_state, "uncertainty_deg"}`. When the filter hasn't converged, say so instead of
giving directions.

**Holding the aim** (found live on an obstructed Mini): while the dish searches, its status drops the `desired_boresight_*`
fields. The poller keeps the last status that had them (`state["dish"]["aim_status"]`, `aim_age_s`), and `explain` shows
that reading when the live one has none, with `held_s` and a line "as of N ago — the dish isn't saying right now".

**Settled while building (knowledge piece):**
- `available` / `current` / `facts` / `explain` read the `/api/state` shape: `state["dish"]["status"|"config"|"device_info"]`,
  `state["router"]` (None = no router), `state["localdish"][device]["reachable"|"error"]`, and `state["running"]["speedtest"]`.
  `config` and `device_info` are accepted wrapped or unwrapped.
- **A missing field means its proto3 default**, because the decoder fills none in. In a config that was read, a missing
  `snow_melt_mode` is AUTO, a missing `power_save_mode` is off, and a missing `location_request_mode` is NONE. With no
  config read at all, `current()` is `{}`.
- Params are strict: all required, no extras, real bools and ints. Types are `bool`, `int` (min/max) and `choice` (choices).
- `available` also says "the dish/router is not answering" and "a speed test is running". The UNIMPLEMENTED mark is the
  server's.
- Outages carry `cause_text`. **NaN becomes None** in the ring and must be null in every JSON body (the router's ping
  reports `latencyMs: NaN` for dropped targets), so the server serializes with `allow_nan=False` after cleaning.
- `aim` gives compass bearings 0–360 and returns None for dishes with motors. When unconverged, turn, tilt and ok are None
  and there is one line of text.
- Headline order: unreachable → connecting → disabled → outage (booting / searching / obstructed / sleeping / stowed /
  too hot / other) → no internet → degraded → online ("19 ms to starlink"). A pending update adds "restarts ~hh:mm" to the
  detail. **Clock times use this computer's local time**: the dish's `utc_offset_s` ignores daylight saving.
- **The headline flickers at 1 Hz** on an obstructed dish (a Mini showed 76 drop transitions in 15 min). So the poller
  logs an event only when the headline **tone** changes, and not more than once every 30 s for the same tone pair.

### poller.py (J2)

One thread per device; the page only reads caches. Cadence (the defaults; a constant table at the top of the file):

| what | while a page is watching | otherwise |
|---|---|---|
| dish `get_status` | every 1 s | every 30 s |
| dish `get_history` | every 5 s | — |
| dish `dish_get_obstruction_map` | every 60 s; refresh button, ≥ 10 s apart | — |
| dish `get_device_info`, `dish_get_config` | at start, every 5 min, and right after a control | same |
| dish `get_diagnostics` | every 60 s | — |
| dish `get_location` | every 5 min if the last answer wasn't PERMISSION_DENIED; else only on refresh | — |
| router `get_status` + `wifi_get_clients` | together, every 30 s | — |
| router `get_device_info` | at start, every 5 min | same |
| router `get_speedtest_status` | every 1 s while a test runs, ≤ 90 s | — |

"Watching" means a `GET /api/state` arrived in the last 10 s. Calls to one device are serialized (one in flight).
A failed call records the error and waits for its next slot. Nothing is retried early. The poller counts calls per
`device:op` (total and last 60 s) for `/api/stats`, and keeps an event list (≤ 200): reachability changes,
state changes (headline text), controls pressed and their results, and schema (re)loads.

### server.py (J2)

`ThreadingHTTPServer` on `127.0.0.1:8686` by default.

- **Host guard:** the `Host` header must be `127.0.0.1:<port>`, `localhost:<port>` or `[::1]:<port>`, else 403.
- **POST guard:** requires `X-Localdish: 1` and `Content-Type: application/json`, else 403. No CORS headers, ever.
- **Secret stripping:** before any JSON leaves, drop keys matching `(?i)(password|passphrase|psk|secret|token|key)$`,
  at any depth.
- Static files come from `localdish/static/` via `importlib.resources`, with `Cache-Control: no-store`.

API (JSON, UTF-8):

```
GET  /api/state
{
  "localdish": {"version", "now": unix_s, "demo": bool,
                "dish":   {"host", "reachable": bool, "error": str|null, "age_s": float|null, "schema": bool},
                "router": {…same…} | null},
  "dish":   {"status": {}|null, "device_info": {}|null, "config": {}|null, "diagnostics": {}|null,
             "location": {}|null, "location_error": str|null},
  "router": {"status": {}|null, "clients": []|null, "device_info": {}|null, "ping": {}|null} | null,
  "explain": {"headline", "alerts", "aim", "facts"},                      # explain.explain()
  "controls": [{"name","label","group","confirm","params","available","reason","current"}],
  "running": {"speedtest": {"started": unix_s, "status": {}|null, "done": bool} | null},
  "events": [{"t": unix_s, "kind": "dish"|"router"|"control"|"app", "text"}]   # oldest→newest, last 50
}
GET  /api/history      {"age_s", "error", "ring": explain.ring(), "outages": explain.outages(), "event_log": {}}
GET  /api/obstruction  {"age_s", "error", "map": dish_get_obstruction_map body}     # num_rows, num_cols, snr[], min_elevation_deg, max_theta_deg, map_reference_frame
GET  /api/stats        {"calls": {"dish:get_status": {"total", "last_60s"}, …}, "watching": bool}
POST /api/control/<name>   body {"params": {…}}  → 200 {"ok": true, "result": {…}, "text"} | 400/403/409/502 {"ok": false, "error"}
POST /api/refresh/<obstruction|info|router|location>  → 200 {"ok": true} | 429 {"ok": false, "retry_after_s"}
```

A control for an unavailable action returns 409. A device error returns 502 with the grpc message. An
UNIMPLEMENTED answer marks that control unavailable for the rest of the process.

**Settled while building (serve piece):**
- "—" in the "otherwise" column means not polled while nobody watches: history, map, diagnostics, location **and the
  router batch**. router `get_device_info` still runs every 5 min. When a page starts watching, everything due runs at once.
- While a device is unreachable, only its primary job runs (dish status, or the router batch). An OSError inside a batch
  stops the rest of that batch. Intervals count from the last attempt, success or failure.
- The poller calls `dev.call` itself (it needs per-job timeouts) and takes the one-of answer. A non-zero `status.code`
  inside a Response body is raised as GrpcError. An op missing from this firmware's Request schema is UNIMPLEMENTED (which
  retires a control). Other codec errors become INTERNAL "could not read the <device>'s reply to <op>".
- Refresh rate limits count the job's own scheduled runs too (a map refresh 3 s after a fetch → 429, retry 7).
  `refresh/info` includes router info. After any dish control, info is forced; after `share_location`, location is too.
- `running.speedtest` = `{"started", "status", "done", "error"}`. The router's status shape is
  `{status: {running, id, up: {throughputs_mbps[], err}, down: {…}}}`. Done when running goes false after being seen true,
  or after 5 s never seen running, or at 90 s.
- `reachable` is false before the first answer. Error text is lowercase ("connection refused", "timed out").
- Static files are served at `/`, `/<name>` and `/static/<name>`: plain names only, no dotfiles, no `..`. Every reply
  carries `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. A missing Host
  header is refused.
- If explain raises, /api/state still answers, with a "localdish could not explain this" headline.
- Headline events: on a tone change, at most once per 30 s for the same pair; a flip that reverts inside the window stays
  silent.

### cli.py and demo.py (J2)

```
localdish [--port 8686] [--dish 192.168.100.1] [--router auto|ADDRESS|none] [--demo] [--version]
```

- `--router auto`: the default gateway from `/proc/net/route` on Linux, else `route -n get default` on macOS, else
  `ipconfig` on Windows. Then try `192.168.1.1`. Use the first whose `:9001` accepts a TCP connection within 1 s.
  If none does, run without a router.
- `--demo`: `demo.FakeDevice` answers `call(op)` from `demo/mini.json` (`{"dish": {op: Response}, "router": {…},
  "errors": {device: {op: {"code", "message"}}}}`), raising `GrpcError` for recorded errors and UNIMPLEMENTED for the
  rest. Controls in demo mode succeed without doing anything and log "demo: …". Live values may drift slightly so the
  page looks alive.
- Prints one line, e.g. `localdish 0.1.0 — http://127.0.0.1:8686 (dish 192.168.100.1, router 192.168.1.1)`, and logs
  events to stderr.

### the page (J3): static/index.html, app.js, app.css

Vanilla JS and CSS, no framework, no external requests, system fonts. Dark by default (`prefers-color-scheme`
light supported). Works at phone width. Polls `/api/state` every 1 s while `document.visibilityState === "visible"`,
`/api/history` every 5 s, and `/api/obstruction` every 60 s. It stops when hidden.

Sections, top to bottom:
1. **header:** the headline pill (`explain.headline`), "refreshed hh:mm:ss", the version, and a demo badge.
2. **live cards:** PoP latency, download / upload now, obstructed %, uptime, power (the ring's last `power_w`),
   ethernet, SNR, GPS.
3. **aim:** shown when `explain.aim` is non-null. A small top-down SVG with now and want arrows, the text lines, and
   an "ok" state.
4. **alerts.**
5. **graphs (15 min):** latency & loss, throughput & power. Inline SVG polylines with axes and the current value
   printed.
6. **outages:** newest first, cause, when and how long.
7. **obstruction map:** canvas, polar: centre = zenith, edge = `max_theta_deg`, snr −1 = no data.
8. **wifi clients:** name, band, signal, and time connected, sorted by signal.
9. **controls:** grouped, each with an in-page confirm dialog (`<dialog>`, never `window.confirm`). The result or
   error is shown inline, and a busy state lasts until the reply arrives.
10. **events.**
11. **facts:** `explain.facts`, then a **raw** drawer with the full JSON of dish status / config / device info /
    diagnostics / router status / clients / ping.

Interface text is lowercase except proper names. Numbers carry units. Staleness shows: if `age_s` > 5, the card dims
and says how old it is.

## fixtures

`localdish/demo/mini.json` (a Starlink Mini, firmware 2026.05) and `tests/fixtures/standard.json` (a standard
dish's status, config and device info) were recorded with read-only calls and decoded with the rules above. They
were then **scrubbed**: device and router ids, account shard, client and lease names, MACs, IPs (mapped into
`192.168.1.x`, `100.64.0.x`, `2001:db8::`), SSIDs, BSSIDs and domains are replaced, and credential-named keys are
dropped. Raw captures never enter the repository.

## testing

`python3 -m unittest discover -s tests -q` runs offline in seconds and never touches a real device. Live checks are
manual and read-only unless the owner is present; they respect the cadence table (never a second poller against a
dish that already has one).
