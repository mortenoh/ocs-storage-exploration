# API walkthrough

Every endpoint of the service, exercised with `curl` in the order a dataset
actually lives: create, append, query, publish, roll back, read as STAC, list,
delete. The responses below were captured from a real run against a scratch data
directory, so the snapshot identifiers and the temporary paths will differ but
nothing else will.

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
       "timestep_count": 6, "start_time": "2020-01-01T00:00:00Z", "step": "month",
       "license": "CC-BY-4.0", "attribution": "Open Climate Service"}' | jq -c .
```

```json
{"dataset_identifier":"temperature-demo","snapshot_identifier":"RK4P9ETMDPFEA8ZGVR30",
 "timestep_count":6,"variables":["temperature"],"published":false}
```

Every field has a default, so `-d '{}'` creates a 16 by 32 global coverage of
three monthly steps. Add `"publish": true` to publish the snapshot in the same
request, and `"overwrite": true` to replace an existing dataset; without it a
second create of the same identifier answers 409. `license` takes an SPDX
identifier, an SPDX expression or `proprietary` and refuses free text;
`attribution` is free text. Both land on the record and reach the
[STAC collection](../concepts/stac-catalog.md), and an overwrite or an append
that does not name them keeps what the record already held.

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
       "publish": true, "license": "proprietary", "attribution": "Statistics Norway",
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

## The STAC catalog

Everything above is also readable as STAC, without writing anything: a
collection is a projection of the record, so there is no second index to keep in
sync. The landing page names the conformance classes, the collections endpoint
and one child per advertised dataset.

```bash
curl -s $BASE/stac | jq -c '{id, title, conformsTo}'
curl -s $BASE/stac | jq -c '[.links[] | {rel, href}]'
```

```json
{"id":"ocs-storage-exploration","title":"OCS storage exploration",
 "conformsTo":["https://api.stacspec.org/v1.0.0/core","https://api.stacspec.org/v1.0.0/collections"]}
[{"rel":"self","href":"http://127.0.0.1:8765/stac"},
 {"rel":"root","href":"http://127.0.0.1:8765/stac"},
 {"rel":"data","href":"http://127.0.0.1:8765/stac/collections"},
 {"rel":"child","href":"http://127.0.0.1:8765/stac/collections/districts-demo"},
 {"rel":"child","href":"http://127.0.0.1:8765/stac/collections/temperature-demo"}]
```

```bash
curl -s $BASE/stac/collections | jq -c '[.collections[] | {id, type: ."ocs:item_type", license}]'
curl -s $BASE/stac/collections/temperature-demo | jq -c '{id, dims: ."cube:dimensions", vars: ."cube:variables"}'
curl -s $BASE/stac/collections/temperature-demo | jq -c '{assets, providers}'
```

```json
[{"id":"districts-demo","type":"feature","license":"proprietary"},
 {"id":"temperature-demo","type":"coverage","license":"CC-BY-4.0"}]
{"id":"temperature-demo",
 "dims":{"x":{"type":"spatial","axis":"x","extent":[-180.0,180.0],"reference_system":4326},
         "y":{"type":"spatial","axis":"y","extent":[-90.0,90.0],"reference_system":4326},
         "t":{"type":"temporal","extent":["2020-01-01T00:00:00Z","2020-09-01T00:00:00Z"]}},
 "vars":{"temperature":{"dimensions":["t","y","x"],"type":"data"}}}
{"assets":{"icechunk":{"href":"file:///tmp/ocs-demo/ocs/raster/temperature-demo",
                       "type":"application/vnd.zarr; version=3","title":"Icechunk repository",
                       "icechunk:branch":"published","roles":["data"]},
           "api":{"href":"http://127.0.0.1:8765/api/v1/raster/temperature-demo/query",
                  "type":"application/json","title":"Raster query endpoint","roles":["metadata"]}},
 "providers":[{"name":"Open Climate Service","roles":["producer","licensor"]}]}
```

A feature collection describes the Parquet it advertises, read from that file's
footer rather than from the record: the collection was rolled back to version 1
above, so it reports the three rows that version holds even though the newest
write has one.

```bash
curl -s $BASE/stac/collections/districts-demo \
  | jq -c '{version: ."ocs:version", rows: ."table:row_count", geometry: ."table:primary_geometry",
            columns: ."table:columns"}'
curl -s $BASE/stac/collections/districts-demo | jq -c '.assets.data'
```

```json
{"version":1,"rows":3,"geometry":"geometry",
 "columns":[{"name":"geometry","type":"binary"},{"name":"id","type":"string"},
            {"name":"level","type":"int64"},{"name":"path","type":"string"},
            {"name":"bbox","type":"struct<xmin: double, ymin: double, xmax: double, ymax: double>"}]}
{"href":"file:///tmp/ocs-demo/ocs/vector/districts-demo/versions/v00001/data.parquet",
 "type":"application/x-parquet","title":"GeoParquet data","roles":["data"]}
```

Only published datasets are advertised. A dataset created without
`"publish": true` is absent from the listing and answers 404 until
`published_only=false` asks for it:

```bash
curl -s -X POST $BASE/api/v1/raster/draft-demo -H 'content-type: application/json' -d '{}' > /dev/null
curl -s -o /dev/null -w '%{http_code}\n' $BASE/stac/collections/draft-demo
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/stac/collections/draft-demo?published_only=false"
curl -s $BASE/stac/collections/absent | jq -c .
curl -s -o /dev/null -X DELETE $BASE/api/v1/datasets/draft-demo
```

```text
404
200
```

```json
{"error":"DatasetNotFoundError","detail":"no dataset record for 'absent'"}
```

[The STAC catalog](../concepts/stac-catalog.md) explains what each kind
advertises, why the media types are the ones they are, and where the coverage
temporal extent still tracks the record rather than the published snapshot.

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

## The same walkthrough against rustfs

Nothing above changes on S3. The only difference is which backend the service
was started with, which is the point of the whole design.

```bash
make docker-run-s3      # rustfs plus the service on the s3 backend, in the foreground
export BASE=http://127.0.0.1:8001
```

`docker-run-s3` runs the `s3` compose profile in the foreground, so Ctrl-C stops
both containers; `make docker-down` cleans up a stack that was interrupted. The
service is published on 8001 so it never collides with a local `make run` or
with the filesystem profile on 8000. To run it outside Docker instead, point the
same settings at the endpoint by hand:

```bash
docker compose up -d --wait rustfs
OCS_STORAGE_BACKEND=s3 \
OCS_STORAGE_S3__BUCKET=ocs-storage-exploration \
OCS_STORAGE_S3__ENDPOINT_URL=http://127.0.0.1:9000 \
OCS_STORAGE_S3__REGION=us-east-1 \
OCS_STORAGE_S3__ACCESS_KEY_ID=rustfsadmin \
OCS_STORAGE_S3__SECRET_ACCESS_KEY=rustfsadmin \
OCS_STORAGE_S3__ALLOW_HTTP=true \
OCS_STORAGE_S3__FORCE_PATH_STYLE=true \
  uv run uvicorn ocs_storage_exploration.main:create_app --factory --port 8765
```

`GET /health` and `GET /api/v1/backends` report the active scheme, and the
backend description names the endpoint but never a secret:

```json
{"status":"ok","version":"0.1.0","backend":"s3"}
{"scheme":"s3","root":"ocs-storage-exploration","base_prefix":"ocs","available":true,
 "supports_parquet_filesystem":true,
 "details":{"bucket":"ocs-storage-exploration","region":"us-east-1",
            "endpoint_url":"http://rustfs:9000","addressing_style":"path",
            "allow_http":"true","anonymous":"false","has_credentials":"true"}}
```

Creating and publishing one coverage and one collection through exactly the
calls above leaves this in the bucket:

```text
ocs/catalog/datasets/districts-demo.json
ocs/catalog/datasets/temperature-demo.json
ocs/raster/temperature-demo/repo
ocs/raster/temperature-demo/snapshots/1CECHNKREP0F1RSTCMT0
ocs/raster/temperature-demo/snapshots/36X40SW4JZCK9PQV43N0
ocs/raster/temperature-demo/transactions/1CECHNKREP0F1RSTCMT0
ocs/raster/temperature-demo/transactions/36X40SW4JZCK9PQV43N0
ocs/raster/temperature-demo/manifests/12JK7D21GHC6HYNM63H0
ocs/raster/temperature-demo/chunks/QA9BX124QHESED1QEAEG
ocs/vector/districts-demo/current.json
ocs/vector/districts-demo/versions/v00001/data.parquet
```

That is the layout
[backends and key layout](../concepts/backends-and-layout.md) specifies, keys
rather than directories, with the Icechunk repository laid out by Icechunk
itself below its own prefix. The record addresses come back as
`s3://ocs-storage-exploration/ocs/vector/districts-demo` rather than a path, so
a record read on another host still points at the same bytes.

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
refusing an oversized read, 422 a contract or identity violation, and 501 a
backend refusing an operation it cannot support.
