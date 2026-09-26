# changelog

## unreleased
- the first version: a local page for a Starlink dish and its router — status, the 15-minute graphs, the obstruction
  map, wifi clients, an aim helper for dishes without motors, and a few controls behind confirms.
- `wire`: a protobuf codec driven by the schema each device sends by reflection; `grpcweb`: a keep-alive gRPC-web
  client, reflection (v1alpha, then v1) and `Device.call`.
- the page: vanilla html, css and js with no outside requests; dark with a light scheme; polls only while visible;
  inline svg graphs, a canvas obstruction map, controls behind a `<dialog>` confirm, a raw json drawer.
