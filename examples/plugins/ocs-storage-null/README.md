# ocs-storage-null

An external plugin shipped as its own package, showing that the storage
exploration service discovers a third-party scheme through the
`ocs_storage_exploration.plugins` entry-point group and `importlib.metadata`,
with no change to the host.

The `null` scheme it adds describes itself but refuses every operation, so the
example stays about the plugin seam rather than about storage. A real plugin
returns a working `StorageBackend` from `storage_backend` instead.

The package is deliberately not part of the host's uv workspace and is not
installed by `make install`: installing it would add `null` to every
`GET /api/v1/backends` response of a development checkout. The tests in
`tests/test_plugins.py` register it in process and stub
`importlib.metadata.entry_points` to prove the discovery path.

To try it for real, install it into the host environment and start the service:

```bash
uv pip install -e examples/plugins/ocs-storage-null
curl -s http://127.0.0.1:8000/api/v1/backends | jq '.items[].scheme'
```
