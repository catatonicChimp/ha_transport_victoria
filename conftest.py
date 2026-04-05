"""Root conftest.py — stubs out homeassistant for pure unit tests.

The importer, time_utils, repository, and rate_limiter modules only depend on
stdlib + the local custom_components package. They do NOT need the real HA
runtime. We inject minimal stubs so imports resolve without installing HA.

Each stub module uses __getattr__ to return a no-op sentinel for any name
that hasn't been explicitly set, so we don't have to enumerate every symbol
that gets imported at module level.
"""

from __future__ import annotations

import sys
import types


class _Stub:
    """Universal stub: callable, iterable, usable as base class, returns itself."""
    def __init__(self, *a, **kw): pass
    def __call__(self, *a, **kw): return _Stub()
    def __getattr__(self, name): return _Stub()
    def __iter__(self): return iter([])
    def __class_getitem__(cls, item): return cls
    def __init_subclass__(cls, **kw): pass


def _stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: _Stub()  # type: ignore[method-assign]
    sys.modules[name] = mod
    return mod


# Register all HA sub-packages we need to stub
_HA_MODULES = [
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.event",
    "homeassistant.helpers.selector",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.util",
    "homeassistant.util.loop",
    "aiohttp",
    "voluptuous",
    "google",
    "google.transit",
    "google.transit.gtfs_realtime_pb2",
]

for _mod_name in _HA_MODULES:
    _stub_module(_mod_name)

# Set concrete names that our code uses in isinstance / raise / inherit contexts

_ce = sys.modules["homeassistant.config_entries"]
_ce.ConfigEntry = _Stub  # type: ignore[attr-defined]
_ce.ConfigFlow = _Stub  # type: ignore[attr-defined]
_ce.ConfigFlowResult = _Stub  # type: ignore[attr-defined]
_ce.OptionsFlow = _Stub  # type: ignore[attr-defined]

_core = sys.modules["homeassistant.core"]
_core.HomeAssistant = _Stub  # type: ignore[attr-defined]

_exc = sys.modules["homeassistant.exceptions"]
_exc.ConfigEntryAuthFailed = Exception  # type: ignore[attr-defined]
_exc.ConfigEntryNotReady = Exception  # type: ignore[attr-defined]

class _DataUpdateCoordinator:
    def __init__(self, *a, **kw): pass
    def __class_getitem__(cls, item): return cls

_coord = sys.modules["homeassistant.helpers.update_coordinator"]
_coord.DataUpdateCoordinator = _DataUpdateCoordinator  # type: ignore[attr-defined]
_coord.UpdateFailed = Exception  # type: ignore[attr-defined]

_aio = sys.modules["aiohttp"]
_aio.ClientSession = _Stub  # type: ignore[attr-defined]
_aio.ClientError = Exception  # type: ignore[attr-defined]
_aio.ClientResponseError = Exception  # type: ignore[attr-defined]
_aio.ClientTimeout = _Stub  # type: ignore[attr-defined]

_vol = sys.modules["voluptuous"]
_vol.Schema = _Stub  # type: ignore[attr-defined]
_vol.Required = _Stub  # type: ignore[attr-defined]
_vol.Optional = _Stub  # type: ignore[attr-defined]
