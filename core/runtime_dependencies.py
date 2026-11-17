"""Installed package versions reachable from explicit requirements in this interpreter."""
from importlib import metadata
import re


def installed_versions(requirements_text: str) -> list[tuple[str, str]]:
    """Return the installed PEP 508 closure. Callers expand -r files and omit -c constraints."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    roots = (re.sub(r"(?:^|\s+)#.*", "", raw).strip() for raw in requirements_text.splitlines())
    pending = [(Requirement(line), ("",)) for line in roots if line]
    versions, visited = {}, set()
    while pending:
        requirement, parent_extras = pending.pop()
        if requirement.marker and not any(requirement.marker.evaluate({"extra": extra}) for extra in parent_extras):
            continue
        name = canonicalize_name(requirement.name)
        extras = tuple(sorted(requirement.extras)) or ("",)
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        distribution = metadata.distribution(name)
        versions[name] = distribution.version
        pending.extend((Requirement(raw), extras) for raw in distribution.requires or ())
    return sorted(versions.items())
