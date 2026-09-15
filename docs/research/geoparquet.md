# GeoParquet for the feature store

GeoParquet 1.1 with a covering bounding-box column is the right container for
the OCS feature store, and it needs four write options set explicitly to
deliver on that. The single most important one is `schema_version="1.1.0"`:
without it the file declares itself 1.0.0 while carrying a 1.1 `covering` key,
which is a file that reads fine today and is not valid against either version.

## Writing

```python
frame.to_parquet(
    path,
    write_covering_bbox=True,
    schema_version="1.1.0",
    geometry_encoding="WKB",
    compression="zstd",
    row_group_size=65_536,
)
```

`write_covering_bbox=True` adds a `bbox` column typed
`struct<xmin: double, ymin: double, xmax: double, ymax: double>` and records it
under the `covering` key in the file metadata. Parquet keeps per-row-group
statistics for those four child fields, and that is what makes spatial pruning
possible at all.

Rows should be sorted along a Hilbert curve before writing. Row-group pruning
only helps when rows that are near each other in space are near each other in
the file; unsorted rows produce row groups whose bounding boxes all overlap the
query and none of which can be skipped. GeoPandas exposes
`frame.hilbert_distance()` for the sort key.

WKB rather than GeoArrow. The GeoParquet 2.0 release candidate drops the
GeoArrow encodings in favour of the native Parquet `GEOMETRY` logical type, so
GeoArrow encoding in a 1.1 file is a dead end. WKB is the encoding that
survives into 2.0 as the interchange form.

## Reading

```python
frame = geopandas.read_parquet(
    path,
    bbox=(xmin, ymin, xmax, ymax),
    columns=["id", "name"],
    filters=[("path", "=", "/abc/def")],
    filesystem=parquet_filesystem,
)
```

Three properties of `bbox=` decide the design around it:

- It requires the covering column. A file written without
  `write_covering_bbox=True` raises rather than falling back to a scan, so the
  covering column is a hard write-side contract, not an optimisation.
- It prunes row groups using Parquet statistics. Work avoided is proportional
  to how well the Hilbert sort clustered the data.
- It is envelope-only. A row whose bounding box intersects the query window is
  returned even if its geometry does not. Results must be re-filtered with
  `frame.geometry.intersects(window)` before they are returned to a caller, or
  a horseshoe-shaped polygon will match a query aimed at the notch.

`filters=` is standard Parquet predicate pushdown in DNF form and composes with
`bbox=`. `columns=` reduces the read, but the identity column and the geometry
column must always be added back regardless of what the caller asked for.

`read_parquet` accepts a `pyarrow.BufferReader`, which means a backend that has
no filesystem — the in-memory one — can still serve the same read path by
fetching the object bytes through obstore and wrapping them.

## Remote files

For S3, `pyarrow.fs.S3FileSystem` is the filesystem to pass:

```python
pyarrow.fs.S3FileSystem(
    access_key=...,
    secret_key=...,
    endpoint_override="localhost:9000",
    scheme="http",
    region="us-east-1",
)
```

`endpoint_override` takes `host:port` with no scheme — the scheme is a separate
argument. Setting `endpoint_override` switches the client to path-style
addressing automatically, which is what rustfs and most self-hosted S3-compatible
stores need.

DuckDB's spatial extension reads GeoParquet from S3 and is a reasonable
downstream consumer, but it only pushes spatial predicates down when the query
names the bbox struct fields explicitly. A `ST_Intersects` call alone reads
every row group.

## Identity contract

From CLIM-1068: feature identity comes from `properties[id_property]`, chosen
at write time and recorded with the collection. A null id or a duplicate id
fails the write loudly and names the offending features. This is a deliberate
rejection of the alternative — synthesising ids, or keeping the last row of a
duplicate pair — because a feature store whose ids are not stable cannot
support the DHIS2 export path, which keys its location column on that id
(`plugins/processes/aggregate_spatial.py:185`).

Two pieces of feedback for CLIM-1067's schema. `primary_geometry` should be a
*column name*, identifying which column is the geometry when a frame has more
than one; it is not a place to record what kind of geometry the collection
holds. That belongs in a separate `geometry_types` field carrying the set of
types actually present, because real administrative-boundary collections mix
`Polygon` and `MultiPolygon` and a single-valued field forces a lie.

## Compared with what OCS does now

`openeo/jobs.py:1585` writes `gdf.to_parquet(path)` with no arguments at all:
schema version defaults, no covering column, no compression choice, no row
group sizing, no sort. It is a job result file rather than a stored dataset, so
this is not a bug in its own context — but it is not a feature store, and it
cannot be pruned, queried by bounding box, versioned or published.

## To be measured

The row-group hit rate on the test fixture — what fraction of row groups a
representative bounding-box query actually reads, with and without the Hilbert
sort — is not yet measured. The fixture in this repo is small enough that the
number will be dominated by fixed costs; a meaningful measurement needs a
collection large enough to span many row groups, and that measurement is
outstanding.
