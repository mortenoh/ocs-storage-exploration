# Plugin framework

Backends are plugins, and the machinery is
[pluginkit](https://winterop-com.github.io/pluginkit). This page covers the
framework itself; the backend contract is in
[writing a backend plugin](backends-and-layout.md#writing-a-backend-plugin).

## What pluginkit is

pluginkit is a small, strictly typed, generics-first plugin framework for Python
3.13 and newer. It is hook-style, like pluggy: the host declares extension
points as specs, plugins implement them as extensions, a manager dispatches one
call to every implementation. Zero runtime dependencies, a `py.typed` marker,
and external plugins found through the standard library's
`importlib.metadata`.

The difference from pluggy is the typing. `caller(spec)` hands back a caller
whose result type is derived from the spec and its dispatch mode - `list[R]`
collecting, `R | None` for `firstresult`, `R` for a pipeline - so
`backend_for_scheme` receives a `StorageBackend | None` with no cast and no
hand-written annotation. Only the calling side is statically linked: an
implementation's annotations are its own, and registration validates argument
names at runtime.

pluginkit is our own framework, and its own documentation says to prefer pluggy
for anything you ship. That is good advice, and this is an exploration. Every
caller here is checked by strict mypy and pyright, which is what pluginkit buys
and pluggy's `Any` does not; an async manager is in the box for the day a
backend must await; and the surface in use is four methods, so a swap to pluggy
stays confined to `storage/plugins.py` and `storage/backends/__init__.py`.

## The vocabulary

Markers are bound to a project name, which namespaces the attribute they stamp
on what they decorate:

```python
extension_point = ExtensionPoint(PROJECT_NAME)
extension = Extension(PROJECT_NAME)
```

An extension point is the declaration: a name, a signature and a dispatch
mode; its body never runs.

```python
class StorageBackendSpecs:
    @staticmethod
    @extension_point(firstresult=True)
    def storage_backend(settings: Settings, scheme: str) -> StorageBackend | None:
        """Build the backend serving the scheme, or None when this plugin does not provide it."""
```

The `@staticmethod` is load-bearing: without it pyright reads the first
parameter as `self` and reports `Type of parameter "settings" must be a
supertype of its class`. A spec is always a `@staticmethod` over
`@extension_point`.

An extension is one plugin's answer, matched by method name:

```python
class FilesystemBackendPlugin:
    @extension
    def storage_backend(self, settings: Settings, scheme: str) -> StorageBackend | None:
        if scheme != StorageScheme.FILE:
            return None
        return FilesystemStorageBackend.from_settings(settings)
```

A plugin is any object carrying extensions; no base class. The manager learns
the specs, accepts plugins and dispatches:

```python
plugin_manager = PluginManager(PROJECT_NAME)
plugin_manager.add_extension_points(StorageBackendSpecs)
plugin_manager.register(FilesystemBackendPlugin(), name="filesystem")
plugin_manager.load_entrypoints(ENTRY_POINT_GROUP)

backend = plugin_manager.caller(StorageBackendSpecs.storage_backend)(settings=settings, scheme=scheme)
```

## Dispatch modes

- **Collecting** is the default: every implementation runs and the non-`None`
  results come back as a list. `storage_schemes` is collecting, so
  `provided_schemes` flattens one list per plugin into a sorted, deduplicated
  tuple.
- **firstresult** stops at the first implementation returning non-`None`.
  `storage_backend` and `storage_backend_description` are both `firstresult`,
  and a plugin abstains from a scheme it does not own by returning `None`.
- **Pipeline** threads its first argument through the implementations, each
  transforming the previous value - a fold, or a middleware chain. Nothing here
  has that shape; a key-rewriting chain would.
- **Wrappers** (`@extension(wrapper=True)`) are generators running around every
  other implementation of a hook, seeing exceptions thrown back in: timing,
  cleanup. **Historic** hooks replay their call to plugins registered
  afterwards, for a startup event a late loader must still hear. Neither is
  used.

Precedence falls out of `firstresult` plus ordering. pluginkit calls
same-priority implementations in registration order, first registered first
(pluggy is the reverse), and `build_plugin_manager` registers the built-ins
before `load_entrypoints` runs, so a plugin claiming `file` is asked after
`FilesystemBackendPlugin` has answered and never displaces it. A deployment
wanting the opposite changes no plugin code: register the external plugin first,
or `set_blocked("filesystem")`, which unregisters that name and refuses it
afterwards.

## Discovery

External plugins arrive through the `ocs_storage_exploration.plugins`
entry-point group, declaring themselves in their own `pyproject.toml` as
`examples/plugins/ocs-storage-null/` does:

```toml
[project.entry-points."ocs_storage_exploration.plugins"]
null = "ocs_storage_null:plugin"
```

The value resolves to the plugin object and the key becomes its registered name.
`build_plugin_manager` calls `load_entrypoints` without `ignore_errors`, so a
plugin failing to import or register raises `PluginValidationError` and the
process does not start: a service coming up quietly missing a scheme is worse
than one that refuses to start.
`load_entrypoints_report` is the resilient alternative if that has to change.

Membership is decided at service construction, not at settings parse.
`Settings.backend` is a `SchemeName`, validated for shape only, because the
legal schemes are whatever the installed plugins provide.
`StorageService.from_settings` asks the manager through `backend_for_scheme`,
the one place an unclaimed scheme becomes `BackendNotSupportedError`.

## The async manager

pluginkit also ships `AsyncPluginManager`: identical registration, validation
and ordering, with callers that are coroutines awaiting coroutine
implementations (plain ones too). This service stays synchronous even though
`AsyncStorageService` awaits everything downstream, because no backend hook
performs I/O - `storage_backend` constructs a client,
`storage_backend_description` reports static facts, neither opens a connection.
The day a plugin must fetch a token or probe an endpoint to build its backend,
the change is `PluginManager` to `AsyncPluginManager` and an `await` at the two
call sites in `storage/plugins.py`.

## Testing plugins

`tests/test_plugins.py` exercises the seam two ways, neither installing
anything. In-process registration against an isolated manager -
`build_plugin_manager(load_entry_points=False)`, `register`, then
`StorageService.from_settings(settings, plugin_manager)` - covers contested and
refused schemes. Discovery is covered by monkeypatching
`pluginkit.manager.entry_points` with a stub returning a hand-built
`EntryPoint`, proving `load_entrypoints` registers a distribution the checkout
never installed.

## Compared to the alternatives

| | pluginkit | pluggy | The removed registry |
| --- | --- | --- | --- |
| Typing | callers typed from the spec, clean under strict mypy and pyright | hook calls return `Any` | typed factories keyed on the scheme enum, closed to new schemes |
| Discovery | `load_entrypoints` over `importlib.metadata` | the same | a dotted path imported by hand |
| Async | `AsyncPluginManager` included | needs `apluggy` | none |
| Dependencies | none | none | none, but the code was ours |
| Maintenance | ours, small and young | battle tested by pytest and tox | ours, growing a feature per question asked |
