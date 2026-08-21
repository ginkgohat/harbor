"""Config file read/write for Harbor."""

import logging
import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "harbor"
CONFIG_PATH = CONFIG_DIR / "config.toml"

# Top-level keys recognized by save_config.  Anything else in the config
# dict is preserved silently (unknown keys would be dropped on save), so we
# log a warning to make the loss observable.
_KNOWN_KEYS = {"port", "min_depth", "max_depth", "roots"}


def load_config(path):
    """Load a TOML config file, returning a dict or None."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError:
        return None


def save_config(path, config):
    """Write a config dict to a TOML file.

    Only the keys Harbor knows about (port, min_depth, max_depth, roots)
    are written back.  Any other top-level keys are dropped — a warning
    is logged so this is not entirely silent.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    unknown = set(config.keys()) - _KNOWN_KEYS
    if unknown:
        logger.warning(
            "save_config: dropping unrecognized top-level keys: %s",
            ", ".join(sorted(unknown)),
        )

    lines = []

    # Write scalar settings first (must come before [[roots]] in TOML)
    for key in ("port", "min_depth", "max_depth"):
        if key in config:
            lines.append(f"{key} = {config[key]}")
    if any(k in config for k in ("port", "min_depth", "max_depth")):
        lines.append("")

    # Write [[roots]] entries
    for root in config.get("roots", []):
        lines.append("[[roots]]")
        lines.append(f'path = {_toml_str(root["path"])}')
        if "label" in root:
            lines.append(f'label = {_toml_str(root["label"])}')
        lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))


def _toml_str(s):
    """Escape a string for TOML."""
    # Basic TOML string escaping
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def resolve_roots(cli_paths, config):
    """Resolve the list of (path, label) roots from CLI + config.

    Priority: CLI paths > config file [[roots]] > cwd.
    """
    if cli_paths:
        return [(p, os.path.basename(os.path.realpath(os.path.expanduser(p)))) for p in cli_paths]

    if config and "roots" in config:
        roots = []
        for entry in config["roots"]:
            path = entry["path"]
            expanded = os.path.realpath(os.path.expanduser(path))
            label = entry.get("label", os.path.basename(expanded))
            roots.append((path, label))
        return roots

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
