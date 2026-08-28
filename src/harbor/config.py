"""Config file read/write for Harbor."""

import logging
import os
import shutil
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

import tomli_w
from platformdirs import user_config_path

logger = logging.getLogger(__name__)

# Cross-platform config dir (e.g. ~/.config/harbor on Linux/macOS,
# %APPDATA%/harbor on Windows).  Uses appauthor=False so the path is
# just the app name, no extra "ginkgohat" subdirectory.
CONFIG_DIR = Path(user_config_path("harbor", appauthor=False))
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Legacy path used before platformdirs (hardcoded to ~/.config/harbor).
# On Linux/macOS this happens to match the platformdirs result, but on
# Windows they differ.  We migrate the legacy file on startup if present.
_LEGACY_CONFIG_DIR = Path.home() / ".config" / "harbor"
_LEGACY_CONFIG_PATH = _LEGACY_CONFIG_DIR / "config.toml"

# Top-level keys recognized by save_config.  Anything else in the config
# dict is preserved silently (unknown keys would be dropped on save), so we
# log a warning to make the loss observable.
_KNOWN_KEYS = {"port", "min_depth", "max_depth", "roots"}


def load_config(path):
    """Load a TOML config file, returning a dict or None.

    If *path* is the default CONFIG_PATH and the file doesn't exist, we
    check for a legacy config at ~/.config/harbor/config.toml and migrate
    it to the new platform-specific location.
    """
    path = Path(path)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        # Auto-migrate from legacy path if the requested path is the default
        if path.resolve() == CONFIG_PATH.resolve() and _LEGACY_CONFIG_PATH.is_file():
            logger.info(
                "Migrating config from %s to %s",
                _LEGACY_CONFIG_PATH, CONFIG_PATH,
            )
            migrate_legacy_config()
            return load_config(path)
        return None
    except tomllib.TOMLDecodeError:
        return None


def migrate_legacy_config():
    """Migrate the legacy ~/.config/harbor config to the platformdirs path.

    The old file is kept as ``config.toml.bak`` in the legacy directory
    so the user can roll back if something goes wrong.
    """
    if not _LEGACY_CONFIG_PATH.is_file():
        return
    # Back up the old file
    backup = _LEGACY_CONFIG_PATH.with_suffix(".bak")
    shutil.copy2(_LEGACY_CONFIG_PATH, backup)
    logger.info("Legacy config backed up at %s", backup)
    # Move to new location
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(_LEGACY_CONFIG_PATH), str(CONFIG_PATH))
    logger.info("Config migrated to %s", CONFIG_PATH)


def save_config(path, config):
    """Write a config dict to a TOML file using tomli_w.

    Only the keys Harbor knows about (port, min_depth, max_depth, roots)
    are written back.  Any other top-level keys are dropped — a warning
    is logged so this is not entirely silent.

    Uses tomli_w for correct round-trip escaping (backslashes, quotes,
    multi-line strings, etc.) instead of the previous hand-rolled serializer.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    unknown = set(config.keys()) - _KNOWN_KEYS
    if unknown:
        logger.warning(
            "save_config: dropping unrecognized top-level keys: %s",
            ", ".join(sorted(unknown)),
        )

    # Build a clean dict with only recognized keys, preserving the order
    # that makes the config file readable (scalars first, then roots table).
    clean: dict = {}
    for key in ("port", "min_depth", "max_depth"):
        if key in config:
            clean[key] = config[key]
    if "roots" in config:
        clean["roots"] = [
            {k: v for k, v in root.items() if k in ("path", "label")}
            for root in config["roots"]
        ]

    with open(path, "wb") as f:
        tomli_w.dump(clean, f)



def resolve_roots(cli_paths, config):
    """Resolve the list of (path, label) roots from CLI args.

    - If CLI paths are given, use them.
    - Otherwise, use the current working directory.

    Rationale: ``harbor`` with no arguments should scan the current
    directory — this matches user intuition (like ``ls`` or ``code .``)
    and the README documentation.  Config-file roots are no longer
    consulted by default; users who want persistent roots can pass them
    explicitly on the command line or via a shell alias.
    """
    if cli_paths:
        return [(p, os.path.basename(os.path.realpath(os.path.expanduser(p)))) for p in cli_paths]

    return [(os.getcwd(), os.path.basename(os.getcwd()))]


def resolve_setting(cli_val, env_var, config_key, config, default):
    """Resolve a setting: CLI > env > config file > default."""
    if cli_val is not None:
        return cli_val
    if env_var in os.environ:
        return type(default)(os.environ[env_var])
    if config and config_key in config:
        return config[config_key]
    return default
