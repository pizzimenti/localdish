# changelog

## unreleased
- settings laid out like the Starlink app: a **sleep schedule** with sleep and wake as clock times and a 24-hour dial
  whose handles you drag (5-minute steps, or the arrow keys); **snow melt** as automatic / pre-heat / off; save stays
  greyed until something changes. location sharing moves to an advanced row that says what it does.
- restart and install update live in the software card, clear in the obstruction map, speed test and ping in a
  **tests** card that shows the speed test's down and up. a control the page doesn't know still shows, under "other".
- the facts list the dish's coordinates when it shares them.
- a software section: dish and router firmware, the update in words, and **install update now** when one is waiting
  (a restart, as the Starlink app does it).
- the power-save schedule is read as UTC minutes (checked against the Starlink app) and shown on your own clock.
- the first version: a local page for a Starlink dish and its router — status, the 15-minute graphs, the obstruction
  map, wifi clients, an aim helper for dishes without motors, and a few controls behind confirms.
- `wire`: a protobuf codec driven by the schema each device sends by reflection; `grpcweb`: a keep-alive gRPC-web
  client, reflection (v1alpha, then v1) and `Device.call`.
- the page: vanilla html, css and js with no outside requests; dark with a light scheme; polls only while visible;
  inline svg graphs, a canvas obstruction map, controls behind a `<dialog>` confirm, a raw json drawer; a ping-test
  table; stow only on dishes with motors.
- every reply carries a strict Content-Security-Policy (no inline script or style, only this server).
- `--capture DIR`: reads a dish and router once, read-only and spaced, scrubs ids, names, MACs, SSIDs, domains,
  addresses and credential-named keys, and writes one JSON file in the fixture format for bug reports.
