"""Credential loading for concurrer.

**Two distinct principals. Do not conflate them.**

    UFL_USERNAME / UFL_PASSWORD
        The UF (GatorLink) identity, authenticated at the **identity provider**.
        Used at hop 2, the Shibboleth form. Bare id, no domain.

    CONCUR_USERNAME
        The **Concur account**, entered at Concur's own identity-first field at
        hop 1. Concur uses it to look up which company the account belongs to
        and therefore which IdP to bounce you to.

These are NOT aliases of each other. An earlier revision of this file treated
`CONCUR_USERNAME` as an alias for `UFL_USERNAME`, on the assumption that the
same principal signs both. The operator confirmed the Concur username differs
from the UF username, which makes an alias actively wrong: it would submit a
UF id into Concur's company lookup, get no match, and silently bounce back to
the sign-in page — which is exactly what happened on 2026-09-21.

No controlling terminal exists in this box (`/dev/tty` -> ENXIO), so the
interactive-prompt tier is unavailable by construction. Everything resolves
from a file or the environment and never prints a secret.

Precedence, highest first, each field resolved independently:

    1. explicit secrets file   --secrets-file PATH
    2. environment variable
    3. default secrets file    ~/.config/concur/secrets.env

There is deliberately **no password flag**, following canvasser: argv is not
private (shell history, world-readable /proc/<pid>/cmdline). The operator
places the file themselves so the value never transits chat or logs.
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

#: The UF identity-provider credential. Bare GatorLink id, e.g. "erthworm".
UFL_USERNAME_VAR = "UFL_USERNAME"
UFL_PASSWORD_VAR = "UFL_PASSWORD"

#: The Concur account identity. Separate principal; see the module docstring.
CONCUR_USERNAME_VAR = "CONCUR_USERNAME"

#: Used only for the derived fallback when CONCUR_USERNAME is unset.
UF_EMAIL_DOMAIN = "ufl.edu"

STATE_DIR = Path(os.environ.get("CONCUR_STATE_DIR", Path.home() / ".config" / "concur"))
DEFAULT_SECRETS = STATE_DIR / "secrets.env"


class ConfigError(RuntimeError):
    """Required configuration missing or unusable."""


@dataclass(frozen=True)
class Credentials:
    """The three values, each with the source it came from.

    Carrying sources lets a failed login say *which* mechanism supplied *which*
    value, without ever printing a secret.
    """

    concur_username: str
    ufl_username: str
    ufl_password: str
    sources: dict[str, str]

    def report(self) -> str:
        """Human-readable, secret-free summary for logging."""
        return (
            f"CONCUR_USERNAME={self.concur_username} (from {self.sources['concur_username']}); "
            f"UFL_USERNAME={self.ufl_username} (from {self.sources['ufl_username']}); "
            f"UFL_PASSWORD from {self.sources['ufl_password']}"
        )


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines, ignoring comments and blanks."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def warn_if_world_readable(path: Path) -> None:
    """Advisory, not fatal: a secrets file others can read."""
    if not path.exists():
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(
            f"WARNING: {path} is readable beyond its owner "
            f"(mode {stat.S_IMODE(mode):04o}). Run: chmod 600 {path}",
            file=sys.stderr,
        )


def resolve(
    *,
    concur_username: str | None = None,
    ufl_username: str | None = None,
    secrets_file: Path | None = None,
) -> Credentials:
    """Resolve all three values by precedence. Never logs a secret."""
    explicit: dict[str, str] = {}
    if secrets_file is not None:
        if not secrets_file.exists():
            raise ConfigError(f"--secrets-file {secrets_file} does not exist.")
        if secrets_file.is_dir():
            raise ConfigError(f"--secrets-file {secrets_file} is a directory.")
        warn_if_world_readable(secrets_file)
        explicit = parse_env_file(secrets_file)

    warn_if_world_readable(DEFAULT_SECRETS)
    default_values = parse_env_file(DEFAULT_SECRETS)

    def one(field: str, var: str, cli: str | None) -> tuple[str, str]:
        if cli:
            return cli, "command line"
        for values, label in (
            (explicit, f"--secrets-file ({secrets_file})"),
            (os.environ, f"${var}"),
            (default_values, str(DEFAULT_SECRETS)),
        ):
            if values.get(var):
                return values[var], label
        raise ConfigError(
            f"No {field} found (looked for ${var}). This box has no terminal to "
            f"prompt on, so it must come from a file or the environment.\n"
            f"  Add to {DEFAULT_SECRETS} (chmod 600):\n"
            f"    {var}=..."
        )

    uu, uu_src = one("UF username", UFL_USERNAME_VAR, ufl_username)
    up, up_src = one("UF password", UFL_PASSWORD_VAR, None)

    cu, cu_src = None, None
    try:
        cu, cu_src = one("Concur username", CONCUR_USERNAME_VAR, concur_username)
    except ConfigError:
        # Fall back to the email form of the UF id. Loud about it, because the
        # two principals are genuinely distinct: this holds only while the
        # Concur account happens to be the UF id at the ufl.edu domain, and a
        # silent derivation would hide the moment that stops being true.
        cu = f"{uu}@{UF_EMAIL_DOMAIN}"
        cu_src = (
            f"derived from UFL_USERNAME as {cu} — set {CONCUR_USERNAME_VAR} "
            f"explicitly if the Concur account differs"
        )
        print(
            f"NOTE: {CONCUR_USERNAME_VAR} not set; using {cu} derived from "
            f"{UFL_USERNAME_VAR}.",
            file=sys.stderr,
        )

    return Credentials(
        concur_username=cu,
        ufl_username=uu,
        ufl_password=up,
        sources={
            "concur_username": cu_src,
            "ufl_username": uu_src,
            "ufl_password": up_src,
        },
    )
