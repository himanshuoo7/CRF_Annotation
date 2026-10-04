"""Study-design adapters.

Importing this package registers every built-in adapter. Add support for a new
EDC by writing a module here that calls `register()`, or — with no code at all —
by dropping a `*.profile.json` into a profile directory and calling
`load_profiles()`.
"""

from __future__ import annotations

from .base import (
    DesignSource,
    identify,
    load_study_design,
    merge,
    register,
    registered,
)

from . import odm_xml, rave_als, rave_json
from .profile import Profile, ProfileSource, load_profiles

# Importing each adapter module registers it with the registry. Naming them
# here makes that side effect explicit rather than looking like a dead import.
_BUILTIN_ADAPTERS = (rave_json, rave_als, odm_xml)

__all__ = [
    "DesignSource",
    "Profile",
    "ProfileSource",
    "identify",
    "load_profiles",
    "load_study_design",
    "merge",
    "register",
    "registered",
    "describe",
]


def describe() -> str:
    """One line per registered adapter — useful in logs and `--list-sources`."""
    return "\n".join(
        f"  {adapter.name:<32} {', '.join(adapter.extensions)}"
        for adapter in registered()
    )
