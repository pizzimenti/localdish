# changelog

## unreleased
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
