# API walkthrough

Every endpoint of the service, exercised with `curl` in the order a dataset
actually lives: create, append, query, publish, roll back, list, delete. The
responses below were captured from a real run against a scratch data directory,
so the snapshot identifiers will differ but nothing else will.

Start the service on a throwaway directory:

```bash
OCS_STORAGE_DATA_DIRECTORY=/tmp/ocs-demo uv run uvicorn ocs_storage_exploration.main:create_app \
  --factory --port 8765
export BASE=http://127.0.0.1:8765
```

`jq` is only used to keep the output readable. Where a dataset was written,
[inspecting the data](inspecting-the-data.md) shows how to open the same bytes
without the API.

## Health and backends

```bash
curl -s $BASE/health | jq -c .
curl -s $BASE/api/v1/backends | jq -c '.items[] | {scheme, available}'
```

```json
{"status":"ok","version":"0.1.0","backend":"file"}
{"scheme":"file","available":true}
{"scheme":"memory","available":false}
{"scheme":"s3","available":false}
```

The active backend is listed first with its root and base prefix; the other
registered schemes are reported as inactive without building a client, so no
credential is read to answer this request. A backend description never contains
a secret.

## Create a coverage

The server generates the data: the request describes the grid, the variable and
the time axis rather than carrying any bytes.

```bash
curl -s -X POST $BASE/api/v1/raster/temperature-demo \
  -H 'content-type: application/json' \
  -d '{"title": "Synthetic temperature", "shape": [32, 64], "variable": "temperature",
       "timestep_count": 6, "start_time": "2020-01-01T00:00:00Z", "step": "month"}' | jq -c .
```

```json
{"dataset_identifier":"temperature-demo","snapshot_identifier":"RK4P9ETMDPFEA8ZGVR30",
 "timestep_count":6,"variables":["temperature"],"published":false}
```

Every field has a default, so `-d '{}'` creates a 16 by 32 global coverage of
three monthly steps. Add `"publish": true` to publish the snapshot in the same
request, and `"overwrite": true` to replace an existing dataset; without it a
second create of the same identifier answers 409.

## Append timesteps

The append continues the time axis with the calendar step the grid recorded, so
the request only says how many steps to add.

```bash
curl -s -X POST $BASE/api/v1/raster/temperature-demo/append \
  -H 'content-type: application/json' -d '{"timestep_count": 3, "seed": 7}' | jq -c .
```

```json
{"dataset_identifier":"temperature-demo","snapshot_identifier":"8FMG55R5397CKBMH99D0",
 "timestep_count":9,"variables":["temperature"],"published":false}
```

## Query a window

```bash
curl -s "$BASE/api/v1/raster/temperature-demo/query?variable=temperature" | jq -c .
curl -s "$BASE/api/v1/raster/temperature-demo/query?bbox=0,50,30,70&start=2020-02-01T00:00:00&end=2020-04-01T00:00:00" | jq -c .
```

```json
{"dataset_identifier":"temperature-demo","variable":"temperature",
 "bbox":{"minimum_x":-180.0,"minimum_y":-90.0,"maximum_x":180.0,"maximum_y":90.0},
 "crs":"EPSG:4326","snapshot_identifier":"8FMG55R5397CKBMH99D0","timestep_count":9,
 "cell_count":18432,"minimum":-1.0094642639160156,"maximum":6.00623083114624,"mean":2.0000004504533937}
{"dataset_identifier":"temperature-demo","variable":"temperature",
 "bbox":{"minimum_x":0.0,"minimum_y":50.625,"maximum_x":28.125,"maximum_y":67.5},
 "crs":"EPSG:4326","snapshot_identifier":"8FMG55R5397CKBMH99D0","timestep_count":3,
 "cell_count":45,"minimum":1.7261766195297241,"maximum":3.916940689086914,"mean":2.8190076298183864}
```

The answered `bbox` is the envelope of the cells that were actually read, grown
by half a cell, which is why it snaps outwards to the grid. A window that reads
more than `OCS_STORAGE_MAX_QUERY_CELL_COUNT` cells is refused with 413 rather
than truncated, and a window that selects nothing is refused rather than
answered with nulls.

## Publish, list versions, roll back

```bash
curl -s -X POST $BASE/api/v1/raster/temperature-demo/publish | jq -c .
curl -s "$BASE/api/v1/raster/temperature-demo/versions?limit=5" | jq -c '.items[]'
```

```json
{"dataset_identifier":"temperature-demo","item_type":"coverage","published":true,"changed":true,
 "snapshot_identifier":"8FMG55R5397CKBMH99D0","version":null,
 "previous_snapshot_identifier":null,"previous_version":null}
{"snapshot_identifier":"8FMG55R5397CKBMH99D0","message":"append","written_at":"2026-09-15T17:15:18.166688Z","is_published":true}
{"snapshot_identifier":"RK4P9ETMDPFEA8ZGVR30","message":"initial write","written_at":"2026-09-15T17:15:18.134216Z","is_published":false}
{"snapshot_identifier":"1CECHNKREP0F1RSTCMT0","message":"Repository initialized","written_at":"2026-09-15T17:15:18.120458Z","is_published":false}
```

Publishing the same snapshot twice answers `"changed": false` instead of
failing. A rollback is the same call with an older snapshot:

```bash
FIRST=$(curl -s "$BASE/api/v1/raster/temperature-demo/versions" \
  | jq -r '[.items[] | select(.message == "initial write")][0].snapshot_identifier')
curl -s -X POST $BASE/api/v1/raster/temperature-demo/publish \
  -H 'content-type: application/json' -d "{\"snapshot_identifier\": \"$FIRST\"}" | jq -c .
curl -s "$BASE/api/v1/raster/temperature-demo/query?version=published" | jq -c '{timestep_count}'
curl -s "$BASE/api/v1/raster/temperature-demo/query?version=draft" | jq -c '{timestep_count}'
```

```json
{"dataset_identifier":"temperature-demo","item_type":"coverage","published":true,"changed":true,
 "snapshot_identifier":"RK4P9ETMDPFEA8ZGVR30","version":null,
 "previous_snapshot_identifier":"8FMG55R5397CKBMH99D0","previous_version":null}
{"timestep_count":6}
{"timestep_count":9}
```

`version=published` follows the published pointer and `version=draft` reads the
main branch; `snapshot_identifier=...` pins one snapshot regardless of either.

## Create a collection

A vector collection is written from a GeoJSON FeatureCollection. The columns a
later query may filter on have to be declared up front.

```bash
curl -s -X POST $BASE/api/v1/vector/districts-demo \
  -H 'content-type: application/json' \
  -d '{"title": "Demo districts", "identifier_property": "id", "selectable_columns": ["level", "path"],
       "publish": true,
       "feature_collection": {"type": "FeatureCollection", "features": [
         {"type": "Feature", "properties": {"id": "oslo", "level": 2, "path": "/root/no/oslo"},
          "geometry": {"type": "Polygon", "coordinates": [[[10.6,59.8],[10.9,59.8],[10.9,60.0],[10.6,60.0],[10.6,59.8]]]}},
         {"type": "Feature", "properties": {"id": "bergen", "level": 2, "path": "/root/no/bergen"},
          "geometry": {"type": "Polygon", "coordinates": [[[5.2,60.3],[5.5,60.3],[5.5,60.5],[5.2,60.5],[5.2,60.3]]]}},
         {"type": "Feature", "properties": {"id": "tromso", "level": 3, "path": "/root/no/tromso"},
          "geometry": {"type": "Point", "coordinates": [18.96, 69.65]}}]}}' | jq -c .
```

```json
{"dataset_identifier":"districts-demo","version":1,"feature_count":3,"published":true}
```

A null or repeated identifier is refused with 422 and the offending value is
named in the response detail.

## Read features

```bash
curl -s "$BASE/api/v1/vector/districts-demo/features?bbox=5,59,11,61" \
  | jq -c '{number_returned, number_matched, version, crs, truncated, ids: [.features[].properties.id]}'
curl -s "$BASE/api/v1/vector/districts-demo/features?where=level:3&columns=level" \
  | jq -c '{number_returned, properties: .features[0].properties}'
curl -s "$BASE/api/v1/vector/districts-demo/features?where=path:/root/no/*&limit=2" \
  | jq -c '{number_returned, number_matched, truncated}'
```

```json
{"number_returned":2,"number_matched":2,"version":1,"crs":"EPSG:4326","truncated":false,"ids":["bergen","oslo"]}
{"number_returned":1,"properties":{"level":3,"id":"tromso"}}
{"number_returned":2,"number_matched":null,"truncated":true}
```

Features are answered in the coordinate reference system the collection was
written in, which the response names in `crs`; they are never silently
reprojected. `bbox-crs` reprojects the query window instead, so
`?bbox=556597,8180387,1224514,8625823&bbox-crs=EPSG:3857` is the same window in
Web Mercator and answers the same two features. The window is densified before
it is reprojected, so a large box does not cut a corner off the curve. `columns` keeps the identifier property and the
geometry whatever else it names, `where` may be repeated for several columns,
and a trailing asterisk makes a clause a prefix match. When a `limit` truncates
the page, `number_matched` is null because the total was never counted, and an
unqualified read of a collection larger than
`OCS_STORAGE_MAX_UNQUALIFIED_FEATURE_COUNT` is refused with 413.

## Publish a version and roll back

Writing the same identifier again adds a version; it never overwrites one.

```bash
curl -s -X POST $BASE/api/v1/vector/districts-demo \
  -H 'content-type: application/json' \
  -d '{"identifier_property": "id", "selectable_columns": ["level"],
       "feature_collection": {"type": "FeatureCollection", "features": [
         {"type": "Feature", "properties": {"id": "oslo", "level": 2},
          "geometry": {"type": "Point", "coordinates": [10.75, 59.91]}}]}}' | jq -c .
curl -s -X POST $BASE/api/v1/vector/districts-demo/publish \
  -H 'content-type: application/json' -d '{"version": 2}' | jq -c .
curl -s "$BASE/api/v1/vector/districts-demo/features?bbox=-180,-90,180,90" | jq -c '{version, number_returned}'
curl -s -X POST $BASE/api/v1/vector/districts-demo/publish \
  -H 'content-type: application/json' -d '{"version": 1}' | jq -c .
curl -s "$BASE/api/v1/vector/districts-demo/features?bbox=-180,-90,180,90" | jq -c '{version, number_returned}'
```

```json
{"dataset_identifier":"districts-demo","version":2,"feature_count":1,"published":false}
{"dataset_identifier":"districts-demo","item_type":"feature","published":true,"changed":true,
 "snapshot_identifier":null,"version":2,"previous_snapshot_identifier":null,"previous_version":1}
{"version":2,"number_returned":1}
{"dataset_identifier":"districts-demo","item_type":"feature","published":true,"changed":true,
 "snapshot_identifier":null,"version":1,"previous_snapshot_identifier":null,"previous_version":2}
{"version":1,"number_returned":3}
```

`POST /publish` with no body publishes the newest version. A coverage is
published by `snapshot_identifier` and a collection by `version`; naming both,
or naming the selector of the other item type, is refused with 422.

## List and delete

```bash
curl -s $BASE/api/v1/datasets \
  | jq -c '.items[] | {dataset_identifier, item_type, storage_format, published: .publication.published}'
curl -s "$BASE/api/v1/datasets?item_type=feature" | jq -c '[.items[].dataset_identifier]'
curl -s $BASE/api/v1/datasets/temperature-demo | jq -c '{title, item_type, timestep_count, temporal}'
```

```json
{"dataset_identifier":"districts-demo","item_type":"feature","storage_format":"geoparquet","published":true}
{"dataset_identifier":"temperature-demo","item_type":"coverage","storage_format":"icechunk","published":true}
["districts-demo"]
{"title":"Synthetic temperature","item_type":"coverage","timestep_count":9,
 "temporal":{"start":"2020-01-01T00:00:00","end":"2020-09-01T00:00:00"}}
```

One delete endpoint serves both item types: the record names the item type and
the service routes to the engine that owns the bytes.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE $BASE/api/v1/datasets/temperature-demo
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE $BASE/api/v1/datasets/districts-demo
curl -s -o /dev/null -w '%{http_code}\n' $BASE/api/v1/datasets/temperature-demo
```

```text
204
204
404
```

The record is deleted before the bytes, so a failure halfway leaves orphan
objects that a prefix listing finds rather than a record that points at nothing.

## Error shape

Every failure is answered by one exception handler in the same shape, with the
status code the error class declares:

```bash
curl -s $BASE/api/v1/datasets/absent | jq -c .
```

```json
{"error":"DatasetNotFoundError","detail":"no dataset record for 'absent'"}
```

400 is a malformed address, bounding box or clause, 404 a missing dataset or
snapshot, 409 an existing identifier or a lost compare-and-swap, 413 a guard
refusing an oversized read, 422 a contract or identity violation, and 501 the
S3 backend saying it is not implemented yet.
