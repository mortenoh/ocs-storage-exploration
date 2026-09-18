# STAC catalog

A STAC Collection is a description of a dataset in a vocabulary other people's
clients already read. So the catalog is a **projection** rather than a second
copy of anything — nothing is written when a collection is requested, and there
is no index to keep in sync.

What it projects is the stored dataset, not the catalog record. The record
contributes identity alone: the identifier, the title and the storage key the
asset and link hrefs are built from. Everything a client would
act on — the extents, the variables, the coordinate reference system, the row
count and the columns — is read from the version being advertised:
`RasterRepository.describe` for a coverage, the version metadata sidecar and the
Parquet footer for a collection. The record tracks the newest write, so
projecting it would advertise a time axis or a row count that the published
bytes do not have.

`storage/stac.py` holds the projection and `api/stac.py` serves it:

| Endpoint | What it answers |
| --- | --- |
| `GET /stac` | The landing page: a STAC `Catalog` with `conformsTo`, a `data` link to the collections endpoint and one `child` link per dataset |
| `GET /stac/collections` | `{"collections": [...], "links": [...]}`, every advertised record projected |
| `GET /stac/collections/{id}` | One collection, 404 when the identifier is unknown or nothing is published |

All three take `?published_only=` and default it to `true`.

## Why only published versions are listed

A draft is a version that exists but that nobody has promised. Publication in
this model is a pointer move — an Icechunk branch reset for a coverage, an
etag-conditional pointer object for a collection — and the pointer is the only
thing that says which bytes a reader is meant to see. A catalog that advertised
drafts would hand out an href whose contents change under the client on the next
write, and a rollback would silently retract data the catalog had already
promised. So the default is `published_only=true`, and a draft-only dataset
answers 404 from `/stac/collections/{id}` rather than appearing as a collection
nobody can rely on.

`published_only=false` exists for the operator looking at what is staged. It
advertises the draft with the same shape, describing the `main` branch for a
coverage and the newest written version for a collection.

## The base URL

Every link and every API asset href is absolute, built from
`str(request.base_url).rstrip("/")`. Starlette derives `base_url` from the ASGI
scope's root path, so a service mounted behind a prefix answers hrefs carrying
that prefix without any configuration. Nothing is read from a proxy header.

## What a coverage advertises

A coverage is a data cube, so the collection carries the
[datacube extension](https://stac-extensions.github.io/datacube/v2.2.0/schema.json):

- `cube:dimensions` with one entry per cube dimension — the two spatial axes
  with their native `extent` and a `reference_system` (the EPSG code when the
  CRS has one, WKT2 otherwise), and the temporal axis with the time coordinate
  of the advertised snapshot. The keys are the dimension names the cube was
  written with, `t`, `y` and `x` by default.
- `cube:variables`, one `{"dimensions": ["t", "y", "x"], "type": "data"}` entry
  per data variable the snapshot holds.

`extent.spatial` is always WGS84, as STAC requires, so a grid written in another
frame is reprojected with pyproj's `transform_bounds`. `cube:dimensions` keeps
the native frame, which is what `reference_system` is for: the two never
disagree because one says where the cube is and the other says what it is
written on.

The projection opens the advertised snapshot once and reads its dimension names,
its projection code, its coordinates and its time axis from the store itself. A
store that is locked, missing or corrupt costs the accuracy of those fields and
nothing else: the collection falls back to what the record holds rather than
failing.

Assets:

- `icechunk`, whose href is the record's storage key resolved against the
  backend that is serving, and whose media type is
  `application/vnd.zarr; version=3`. There is no registered media type for an
  Icechunk repository, and inventing `application/vnd.zarr+icechunk` would be a
  string no client matches; the repository is a Zarr v3 store, so it is
  advertised as one. The string is byte-identical to the one OCS publishes,
  because stac-js compares media types as literals rather than parsing their
  parameters. Which branch to open is a separate `icechunk:branch` field:
  `published` for a published coverage, and `main` for a draft-only one, which
  has no published branch a reader could open. A draft asset also pins the
  advertised snapshot as `ocs:snapshot_identifier`, so it stays unambiguous when
  `main` moves on; the field is omitted when no snapshot could be read.
- `api`, pointing at `{base_url}/api/v1/raster/{id}/query` with role `metadata`,
  for a client that wants a summary rather than the bytes. The endpoint defaults
  to the published branch, so a draft-only coverage carries its selector in the
  href: `?snapshot_identifier=...`, or `?version=draft` when there is no snapshot
  to pin.

The advertised snapshot is on the collection as `ocs:snapshot_identifier` — the
one the `published` branch points at, or the head of `main` for a draft — and the
item type as `ocs:item_type`.

```json
{
  "type": "Collection",
  "id": "temperature-demo",
  "stac_version": "1.1.0",
  "description": "Icechunk-backed GeoZarr coverage on a 8 by 16 grid in EPSG:4326, holding 3 timesteps of temperature.",
  "title": "Synthetic temperature",
  "stac_extensions": ["https://stac-extensions.github.io/datacube/v2.2.0/schema.json"],
  "cube:dimensions": {
    "x": {"type": "spatial", "axis": "x", "extent": [-180.0, 180.0], "reference_system": 4326},
    "y": {"type": "spatial", "axis": "y", "extent": [-90.0, 90.0], "reference_system": 4326},
    "t": {"type": "temporal", "extent": ["2020-01-01T00:00:00Z", "2020-03-01T00:00:00Z"]}
  },
  "cube:variables": {"temperature": {"dimensions": ["t", "y", "x"], "type": "data"}},
  "ocs:item_type": "coverage",
  "ocs:snapshot_identifier": "995B0EPQKW8F556KJV2G",
  "extent": {
    "spatial": {"bbox": [[-180.0, -90.0, 180.0, 90.0]]},
    "temporal": {"interval": [["2020-01-01T00:00:00Z", "2020-03-01T00:00:00Z"]]}
  },
  "license": "CC-BY-4.0",
  "providers": [{"name": "Open Climate Service", "roles": ["producer", "licensor"]}],
  "assets": {
    "icechunk": {
      "href": "s3://ocs-storage-exploration/ocs/raster/temperature-demo/9c5e7d2b1a3f4e6c8b0d2f4a6c8e0b2d",
      "type": "application/vnd.zarr; version=3",
      "title": "Icechunk repository",
      "icechunk:branch": "published",
      "roles": ["data"]
    },
    "api": {
      "href": "http://127.0.0.1:8000/api/v1/raster/temperature-demo/query",
      "type": "application/json",
      "title": "Raster query endpoint",
      "roles": ["metadata"]
    }
  },
  "links": [
    {"rel": "self", "href": "http://127.0.0.1:8000/stac/collections/temperature-demo", "type": "application/json"},
    {"rel": "root", "href": "http://127.0.0.1:8000/stac", "type": "application/json"},
    {"rel": "parent", "href": "http://127.0.0.1:8000/stac", "type": "application/json"}
  ]
}
```

## What a feature collection advertises

A vector collection is a table, so it carries the
[table extension](https://stac-extensions.github.io/table/v1.2.0/schema.json):

- `table:primary_geometry` from the metadata sidecar of the advertised version.
- `table:row_count` and `table:columns` from the Parquet footer of that version.
  Only the footer is read, never a row group, so the cost is one sidecar read and
  one footer read per collection. The GeoParquet covering `bbox` struct and the
  WKB geometry column appear as they are in the file, because the point of the
  field is to describe the file a client is about to open.

Both come from the published version rather than from the record on purpose. The
record's feature detail tracks the newest write, so a collection rolled back to
an older version — or one whose newest draft was written in another frame — would
otherwise advertise a count, a CRS and a geometry list the published file does
not have.

`extent.temporal` is `[[null, null]]`: a feature collection in this model has no
time axis, and inventing one from the record's timestamps would advertise
metadata as data. `extent.spatial` is the envelope the advertised version's
sidecar records, reprojected to WGS84 when that version was written in another
frame.

Assets:

- `data`, the GeoParquet object of the advertised version, at
  `{storage_key}/versions/vNNNNN/data.parquet` resolved against the backend that
  is serving. The `storage_key` of the record carries the generation the
  collection was created into, so the href names the live data rather than a
  prefix a collection of the same name once used. Its media type is
  `application/x-parquet` (pystac's `MediaType.PARQUET`) and its role is `data`. The
  version directory name is the same one the pointer object names, so the href
  is stable until the pointer moves.
- `api`, pointing at `{base_url}/api/v1/vector/{id}/features` with role
  `metadata`. As with a coverage, a draft-only collection carries its selector in
  the href, `?version=` the advertised version, because the endpoint otherwise
  answers the published version it does not have.

The advertised version is on the collection as `ocs:version`.

```json
{
  "type": "Collection",
  "id": "districts-demo",
  "stac_version": "1.1.0",
  "description": "GeoParquet feature collection of 2 features in EPSG:4326, identified by id and holding Point, Polygon.",
  "title": "Demo districts",
  "stac_extensions": ["https://stac-extensions.github.io/table/v1.2.0/schema.json"],
  "table:row_count": 2,
  "table:primary_geometry": "geometry",
  "table:columns": [
    {"name": "geometry", "type": "binary"},
    {"name": "id", "type": "string"},
    {"name": "level", "type": "int64"},
    {"name": "bbox", "type": "struct<xmin: double, ymin: double, xmax: double, ymax: double>"}
  ],
  "ocs:item_type": "feature",
  "ocs:version": 1,
  "extent": {
    "spatial": {"bbox": [[5.3, 59.8, 10.9, 60.4]]},
    "temporal": {"interval": [[null, null]]}
  },
  "license": "proprietary",
  "providers": [{"name": "Statistics Norway", "roles": ["producer", "licensor"]}],
  "assets": {
    "data": {
      "href": "s3://ocs-storage-exploration/ocs/vector/districts-demo/6b1f0c8d4a2e4f1b9c3d5e7a8b0c2d4e/versions/v00001/data.parquet",
      "type": "application/x-parquet",
      "title": "GeoParquet data",
      "roles": ["data"]
    },
    "api": {
      "href": "http://127.0.0.1:8000/api/v1/vector/districts-demo/features",
      "type": "application/json",
      "title": "Feature query endpoint",
      "roles": ["metadata"]
    }
  },
  "links": [
    {"rel": "self", "href": "http://127.0.0.1:8000/stac/collections/districts-demo", "type": "application/json"},
    {"rel": "root", "href": "http://127.0.0.1:8000/stac", "type": "application/json"},
    {"rel": "parent", "href": "http://127.0.0.1:8000/stac", "type": "application/json"}
  ]
}
```

## Media types, in one place

| What | Media type | Why |
| --- | --- | --- |
| Icechunk repository | `application/vnd.zarr; version=3` | No registered Icechunk type exists; the repository holds a Zarr v3 store and clients match this string literally |
| GeoParquet object | `application/x-parquet` | pystac's `MediaType.PARQUET`, the value the table extension's own examples use |
| Every API endpoint and link | `application/json` | They answer JSON |

## Licence and attribution

`license` is an SPDX identifier, an SPDX expression or `proprietary`, validated
loosely enough to accept `CC-BY-4.0`, `Apache-2.0 OR MIT` and `proprietary`
while refusing a sentence of prose. `attribution` is free text.

Both belong to the version being advertised rather than to the record. A feature
collection reads them from the metadata sidecar of the version it advertises and
a coverage from the metadata of the Icechunk commit that wrote the snapshot, so
a draft written under other terms cannot change what the published version
promises, and a rollback takes the earlier terms back with it; the record is the
fallback only when no version or snapshot can be read at all. A create declares
the terms its own commit carries, and an append carries forward the terms of the
snapshot it extends.

`license` maps onto the collection's `license` field, falling back to `other`
when none is declared — STAC 1.1 requires the field and `other` is its
spelling for "not an SPDX identifier". `attribution` becomes a single entry in
`providers` with the roles `producer` and `licensor`, because attribution under
CC-BY is a licence condition rather than a courtesy, and `providers` is the only
collection-level field STAC defines for naming the party that has to be
credited.

Both are threaded through `POST /api/v1/raster/{id}` and
`POST /api/v1/vector/{id}`, and both survive a rewrite: an overwrite or an
append that does not name them keeps what the earlier record held.

## What is still record-shaped

Identity only: the identifier, the title and the storage key the asset and link
hrefs are built from. Nothing a client would compute against comes from the
record while the store can be read, and when it cannot, the record is the
documented fallback rather than the source.

## Validation

The suite checks the structure rather than the schemas: every collection is
round-tripped through `pystac.Collection.from_dict`, which rejects a document
pystac cannot read back. `pystac.validation.validate_dict` is skipped, with the
reason in the skip marker: it needs the `jsonschema` extra and fetches every
schema over HTTP, and a test suite that fails when a schema host is slow is
worse than no test.

The schemas were checked once by hand, against the documents these endpoints
produced:

```bash
uvx --with stac-validator stac-validator validate coverage-collection.json
uvx --with stac-validator stac-validator validate feature-collection.json
uvx --with stac-validator stac-validator validate landing.json
```

The landing page and the feature collection validate against the STAC 1.1.0
Catalog and Collection schemas and the table extension. The coverage collection
validates against the Collection schema, and against the datacube extension
schema once its `$ref` to `https://proj.org/schemas/v0.4/projjson.schema.json`
is stubbed — that URL is dead, so no document using datacube v2.2.0 can be
validated end to end by a networked validator today. The failure is upstream,
not in the projection.
