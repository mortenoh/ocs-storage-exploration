# Samples

Real files the service is exercised against, so the storage model is tested by
data that exists rather than by synthetic cubes and sample features.

The three files committed here are small and are in git. Everything `make
samples` downloads lands in `downloaded/`, which is gitignored: it is fetched
once, while online, and read offline from then on.

## Committed here

| File | What it is | Source | Licence |
| --- | --- | --- | --- |
| `sierra_leone_districts.geojson` | 13 DHIS2 organisation units, polygons, with `id`, `name`, `level` and `parentName` | Open Climate Service test data (`tests/data/`) | BSD-3-Clause, as the Open Climate Service repository |
| `ne_110m_lakes.geojson` | 25 lakes at 1:110m, polygons, with `id`, `name` and `featureclass` | Natural Earth, via the Open Climate Service test data | Public domain (Natural Earth terms of use) |
| `sle_pop_2026_CN_1km_R2025A_UA_v1.tif` | WorldPop constrained population of Sierra Leone for 2026, 1 km, a 370 by 364 GeoTIFF in EPSG:4326 with nodata -99999 | WorldPop, via the Open Climate Service test data | CC BY 4.0, attribution "WorldPop, University of Southampton" |

## Downloaded by `make samples`

| Path | What it is | Source | Licence |
| --- | --- | --- | --- |
| `downloaded/chirps3/chirps3-YYYY-MM-DD.tif` | 14 days of CHIRPS v3.0 final daily rainfall, 1 January to 14 January 2024, clipped to Sierra Leone: 63 by 69 cells, EPSG:4326, nodata -9999 | Climate Hazards Center, UC Santa Barbara | Public domain; catalogues list CHIRPS as CC0-1.0. Cite the Climate Hazards Center |
| `downloaded/geoboundaries-sle-adm2.geojson` | 14 Sierra Leone ADM2 areas with `shapeID`, `shapeName`, `shapeGroup` and `shapeType` | geoBoundaries release `gbOpen`, pinned to commit `9469f09` | CC BY 4.0, attribution "geoBoundaries" |

The global CHIRPS file for one day is about 30 MB. `scripts/fetch_samples.py`
opens it over HTTPS as a cloud optimised GeoTIFF and clips to the Sierra Leone
bounding box, so only the window travels and each file written here is a few
kilobytes. The script is idempotent: it skips a day that is already on disk.

## Using them

- `make samples` fetches what is missing, online, once.
- `make demo` ingests all of it into the configured backend, offline.
- `make docker-run-file` and `make docker-run-s3` run the same ingest as a
  one-shot seed container, with this directory bind mounted read-only, so the
  service comes up with all five datasets in it.
- [The real data guide](../docs/guides/real-data.md) explains what to look at afterwards.
