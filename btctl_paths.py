"""Shared fail-closed host-path policy for managed Unraid operations."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping, Protocol

from btctl_core import ConfigError


TEMPLATE_ROOT = Path("/boot/config/plugins/dockerMan/templates-user")
STORAGE_ROOT = Path("/mnt")
# Top-level /mnt entries that are never the pool behind an exclusive share.
_RESERVED_STORAGE_ROOTS = frozenset(
    {"user", "user0", "disks", "remotes", "addons", "rootshare"}
)


class UnraidPathConfig(Protocol):
    install_profile: str
    state_dir: str
    data_dir: str
    backup_dir: str
    unraid_template_dir: str


def validate_mount_path(path: Path, label: str) -> Path:
    value = str(path)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise ConfigError(f"{label} must be one absolute non-root path")
    if any(character in value for character in (",", "\n", "\r", "\t", "\0")):
        raise ConfigError(f"{label} contains a character unsupported by Docker mount")
    return path


def _validate_existing_components(path: Path) -> None:
    current = Path(path.anchor)
    missing = False
    for component in path.parts[1:]:
        current /= component
        if missing:
            continue
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing = True
            continue
        except OSError as exc:
            raise ConfigError("managed path components could not be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ConfigError("managed path must not contain a symbolic link")
        if current != path and not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError("managed path parent must be a real directory")
        if current == path and not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError("managed path must be a directory when it already exists")


def _exclusive_share_pool_directory(share: Path, root: Path, label: str) -> Path:
    """Return the pool directory behind one Unraid exclusive-share link.

    Unraid publishes an exclusive share as the relative link
    ``/mnt/user/<share> -> ../<pool>/<share>``. Any other link shape is
    rejected so that only that root-managed alias is followed.
    """
    try:
        target = os.readlink(share)
    except OSError as exc:
        raise ConfigError(f"{label} Unraid share link could not be inspected") from exc
    parts = Path(target).parts
    if (
        len(parts) != 3
        or target != "/".join(parts)
        or parts[0] != ".."
        or parts[1] in {".", ".."} | _RESERVED_STORAGE_ROOTS
        or parts[2] != share.name
    ):
        raise ConfigError(
            f"{label} Unraid share must be one real directory or exclusive-share link"
        )
    pool_share = root / parts[1] / parts[2]
    for directory in (pool_share.parent, pool_share):
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise ConfigError(
                f"{label} Unraid exclusive share must resolve to one pool directory"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError(
                f"{label} Unraid exclusive share must resolve to one pool directory"
            )
    return pool_share


def host_storage_path(
    path: Path,
    *,
    storage_root: Path | None = None,
    label: str = "managed path",
) -> Path:
    """Map a path below an Unraid exclusive-share link to its pool path.

    Every other path is returned unchanged. Docker binds and the operator keep
    using the configured ``/mnt/user`` path; host-side identity, overlap and
    mount-source checks use the returned pool path.
    """
    candidate = Path(path)
    root = Path(storage_root or STORAGE_ROOT)
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return candidate
    if len(relative.parts) < 2 or relative.parts[0] != "user":
        return candidate
    share = root / "user" / relative.parts[1]
    if not share.is_symlink():
        return candidate
    pool_share = _exclusive_share_pool_directory(share, root, label)
    return pool_share.joinpath(*relative.parts[2:])


def validate_storage_path(
    path: Path,
    label: str,
    *,
    storage_root: Path | None = None,
) -> Path:
    candidate = validate_mount_path(Path(path), label)
    root = validate_mount_path(Path(storage_root or STORAGE_ROOT), "Unraid storage root")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ConfigError(f"{label} must be under {root}") from exc
    if not relative.parts:
        raise ConfigError(f"{label} must be below an existing Unraid share or pool root")
    if relative.parts[0] in {"user", "user0"}:
        if len(relative.parts) < 3:
            raise ConfigError(f"{label} must be below an existing Unraid share")
        storage_boundary = root / relative.parts[0] / relative.parts[1]
        boundary_description = "share"
    else:
        if len(relative.parts) < 2:
            raise ConfigError(
                f"{label} must be below an existing Unraid share or pool root"
            )
        storage_boundary = root / relative.parts[0]
        boundary_description = "pool root"
    try:
        metadata = storage_boundary.lstat()
    except FileNotFoundError as exc:
        raise ConfigError(
            f"{label} must be below an existing Unraid {boundary_description}"
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"{label} Unraid {boundary_description} could not be inspected"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) and relative.parts[0] == "user":
        pool_share = _exclusive_share_pool_directory(storage_boundary, root, label)
        _validate_existing_components(pool_share.joinpath(*relative.parts[2:]))
        return candidate
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ConfigError(
            f"{label} Unraid {boundary_description} must be one real directory"
        )
    _validate_existing_components(candidate)
    return candidate


def validate_template_path(path: Path) -> Path:
    candidate = validate_mount_path(path, "BT_UNRAID_TEMPLATE_DIR")
    if candidate != TEMPLATE_ROOT:
        raise ConfigError(
            "BT_UNRAID_TEMPLATE_DIR must be exactly "
            "/boot/config/plugins/dockerMan/templates-user"
        )
    _validate_existing_components(candidate)
    return candidate


def storage_minimum(path: Path) -> Path:
    relative = path.relative_to(STORAGE_ROOT)
    if relative.parts[0] in {"user", "user0"}:
        return STORAGE_ROOT / relative.parts[0] / relative.parts[1]
    return STORAGE_ROOT / relative.parts[0]


def paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def validate_unraid_config_paths(
    config: UnraidPathConfig,
    values: Mapping[str, str],
    repository: Path,
) -> None:
    if config.install_profile != "unraid":
        return
    paths = {
        "state": validate_storage_path(Path(config.state_dir), "BT_STATE_DIR"),
        "data": validate_storage_path(Path(config.data_dir), "BT_DATA_DIR"),
        "backup": validate_storage_path(Path(config.backup_dir), "BT_BACKUP_DIR"),
        "template": validate_template_path(Path(config.unraid_template_dir)),
    }
    legacy = values.get("BT_LEGACY_DATA_DIR", "")
    if legacy:
        paths["legacy"] = validate_storage_path(
            Path(legacy), "BT_LEGACY_DATA_DIR"
        )
    # Compare pool paths so an exclusive-share alias cannot hide an overlap.
    checkout = host_storage_path(validate_mount_path(repository, "repository"))
    for label, managed in paths.items():
        if paths_overlap(checkout, host_storage_path(managed)):
            raise ConfigError(f"{label} path must not overlap the Git checkout")
