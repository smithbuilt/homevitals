from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class EufyConfig:
    email: str
    password: str
    customer_id: str | None = None


@dataclass
class GarminConfig:
    email: str
    password: str


@dataclass
class StravaConfig:
    client_id: str
    client_secret: str


@dataclass
class ZwiftConfig:
    email: str
    password: str


@dataclass
class OmronConfig:
    email: str                       # the OMRON connect login
    password: str                    # from the vault, key "<name>:omron"
    country: str                     # two-letter code of the country the account was created in, upper case
    server: str | None = None        # None, "na", "eu", or an https:// address without a trailing slash
    user_number: int | None = None   # None = every reading in the account; 1 or 2 = that user slot on the monitor


@dataclass
class UserConfig:
    name: str
    eufy: EufyConfig | None = None    # optional: a person may not use the scale
    garmin: GarminConfig | None = None
    strava: StravaConfig | None = None
    zwift: ZwiftConfig | None = None
    omron: OmronConfig | None = None  # a blood pressure source; readings go to this person's Garmin


# Countries whose OMRON connect accounts live on OMRON's older servers, which
# this build does not talk to. omron_client
# imports this; it lives here so config never imports the client.
OMRON_V1_COUNTRIES = frozenset(
    "AU BD ID MM MY NZ PH SG TH VN HK JP KR TW IN AR BO BR CL CO CR DO EC GT HN MX NI PA PE PY SV UY VE".split()
)


@dataclass
class AppConfig:
    users: list[UserConfig]


def _interpolate_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(
                f"Environment variable '{var_name}' referenced in config is not set."
            )
        return env_value

    return re.sub(r"\$\{(\w+)}", replacer, value)


def _walk_and_interpolate(obj: dict | list | str) -> dict | list | str:
    """Recursively interpolate env vars in all string values."""
    if isinstance(obj, str):
        return _interpolate_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _walk_and_interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_interpolate(item) for item in obj]
    return obj


def _get_password(user_name: str, service: str, email: str, yaml_password: str | None) -> str:
    """Resolve password: credential store first, then YAML fallback."""
    from homevitals.credentials import get_password

    key = f"{user_name}:{service}"
    stored = get_password(key)
    if stored:
        return stored

    if yaml_password:
        return yaml_password

    raise ValueError(
        f"No {service} password found for user '{user_name}'. "
        f"Run: homevitals --update-password"
    )


def _get_strava_secret(user_name: str, yaml_secret: str | None) -> str:
    """Resolve the Strava API client secret: credential store first, then the
    YAML fallback for configs not yet migrated.

    Its own function rather than _get_password's `service` argument because
    the fix is a different command: --update-password prompts for the Eufy and
    Garmin account passwords, never for a Strava app secret.
    """
    from homevitals.credentials import get_password

    stored = get_password(f"{user_name}:strava")
    if stored:
        return stored

    if yaml_secret:
        return yaml_secret

    raise ValueError(
        f"No Strava client secret found for user '{user_name}'. "
        f"Run: homevitals --setup-strava"
    )


def _section_value(user: dict, section: str, key: str):
    value = user.get(section)
    return value.get(key) if isinstance(value, dict) else None


def _two_letters(value) -> str | None:
    text = str(value).strip().upper() if isinstance(value, str) else ""
    return text if re.fullmatch(r"[A-Z]{2}", text) else None


def validate_omron_section(name: str, section, path: Path | str) -> None:
    """Check one person's omron section. Pure: never reads the vault."""
    email = section.get("email") if isinstance(section, dict) else None
    if not isinstance(email, str) or not email.strip():
        raise ValueError(
            f"User '{name}' has an omron section without an email. Add omron.email in {path}, "
            "or connect the monitor again in the HomeVitals window (Edit person)."
        )
    if "@" not in email:
        raise ValueError(
            f"User '{name}' has an omron.email that is not an email address. "
            f"Fix it in {path} or via Edit person in the HomeVitals window."
        )
    country = _two_letters(section.get("country"))
    if country is None:
        raise ValueError(
            f"User '{name}' has no valid omron.country. Set the two-letter code of the country the OMRON connect "
            f"account was created in (for example CA), in {path} or via Edit person in the HomeVitals window."
        )
    server = section.get("server")
    if server is not None and server != "":
        ok = isinstance(server, str) and (
            server.strip().lower() in ("na", "eu") or re.fullmatch(r"https://\S+", server.strip()) is not None
        )
        if not ok:
            raise ValueError(f"User '{name}' has an invalid omron.server. Use na, eu, or a full https:// address.")
    elif country in OMRON_V1_COUNTRIES:
        raise ValueError(
            f"User '{name}': OMRON connect accounts created in {country} use OMRON's older server system, which "
            "this build does not support. If the account was created in another country, change omron.country; "
            "otherwise see the README."
        )
    user_number = section.get("user_number")
    if user_number is not None and _omron_user_number(user_number) is None:
        raise ValueError(
            f"User '{name}' has an invalid omron.user_number. Use a whole number from 1 to {MAX_OMRON_USER_NUMBER} "
            "(the user on the monitor), or remove it to sync every reading in the account."
        )


# Monitors have one user, two, or more; OMRON numbers them from 1. 99 is only a sanity limit.
MAX_OMRON_USER_NUMBER = 99


def _omron_user_number(value) -> int | None:
    """The monitor user as an int (1..MAX_OMRON_USER_NUMBER), or None if the value isn't one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        return None
    return number if 1 <= number <= MAX_OMRON_USER_NUMBER else None


def _normalise_omron_server(value) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if text.lower() in ("na", "eu"):
        return text.lower()
    return text.rstrip("/")


def _has_email(section) -> bool:
    return isinstance(section, dict) and isinstance(section.get("email"), str) and bool(section["email"].strip())


def scale_ready(user: UserConfig) -> bool:
    """The scale step can run: a scale and somewhere to send it. A single legacy user without a
    profile is ready too (the AmbiguousProfileError path handles that, as upstream)."""
    return user.eufy is not None and any((user.garmin, user.strava, user.zwift))


def bp_ready(user: UserConfig) -> bool:
    """The blood pressure step can run: a monitor and Garmin (readings can only go to Garmin)."""
    return user.omron is not None and user.garmin is not None


def nothing_connected(user: UserConfig) -> bool:
    return not any((user.eufy, user.garmin, user.omron, user.strava, user.zwift))


def missing_for_sync(user: UserConfig) -> list[str]:
    """What to connect before anything can sync for this person, in plain words ([] when something can)."""
    if scale_ready(user) or bp_ready(user):
        return []
    missing = []
    if user.garmin is None:
        missing.append("Garmin")
    if user.eufy is None and user.omron is None:
        missing.append("the scale or the blood pressure monitor")
    return missing


def validate_household_users(raw_users: list[dict], path: Path | str) -> None:
    """Household rules on the raw users list. Raises ValueError naming the user.

    Pure: never reads the vault. load_config calls it before resolving any
    password, and the GUI calls it on raw YAML. With more than one person,
    every person must be linked to their own Eufy profile and their own Garmin
    account, so nobody's weigh-ins can reach the wrong Garmin account.
    """
    names: list[str] = []
    seen: dict[str, str] = {}
    for u in raw_users:
        name = str((u or {}).get("name") or "").strip() if isinstance(u, dict) else ""
        if not name:
            raise ValueError(f"Every user needs a name. Fix the entry without one in {path}.")
        if name.lower() in seen:
            raise ValueError(f"User names must be unique: '{name}' appears more than once in {path}.")
        seen[name.lower()] = name
        names.append(name)
        # Every account section is optional, but one that is there must be well formed.
        if "eufy" in u and not _has_email(u["eufy"]):
            raise ValueError(
                f"User '{name}' has an eufy section without an email. Fix eufy.email in {path}, or open the "
                "person in the HomeVitals window and connect the scale again."
            )
        if "garmin" in u and not _has_email(u["garmin"]):
            raise ValueError(
                f"User '{name}' has a garmin section without an email. Fix garmin.email in {path}, or open the "
                "person in the HomeVitals window and connect Garmin again."
            )
        if "omron" in u:
            validate_omron_section(name, u["omron"], path)

    if len(raw_users) <= 1:
        return

    profile_owner: dict[str, str] = {}
    garmin_owner: dict[str, str] = {}
    omron_owner: dict[str, str] = {}
    for name, u in zip(names, raw_users, strict=True):
        customer_id = _section_value(u, "eufy", "customer_id")
        has_profile = customer_id is not None and str(customer_id).strip() != ""
        # The kids rule: with more than one person, everyone who uses the scale owns a profile.
        if "eufy" in u and not has_profile:
            raise ValueError(
                f"User '{name}' has no eufy.customer_id. With more than one person, everyone who uses the scale "
                "must be linked to their own Eufy profile so nobody's weigh-ins reach the wrong Garmin account. "
                "Open the person in the HomeVitals window and choose their scale profile, "
                f"or set eufy.customer_id in {path}."
            )
        if has_profile:
            customer_id = str(customer_id).strip()
            if customer_id in profile_owner:
                raise ValueError(
                    f"Users '{profile_owner[customer_id]}' and '{name}' are linked to the same Eufy profile "
                    f"(...{customer_id[-4:]}). Each person needs their own profile. "
                    "Open one of them in the HomeVitals window and change their scale profile."
                )
            profile_owner[customer_id] = name

        garmin_email = _section_value(u, "garmin", "email")
        if garmin_email:
            key = str(garmin_email).strip().lower()
            if key in garmin_owner:
                raise ValueError(
                    f"Users '{garmin_owner[key]}' and '{name}' both use the Garmin account {key}. "
                    "Each person needs their own Garmin account."
                )
            garmin_owner[key] = name

        omron_email = _section_value(u, "omron", "email")
        if omron_email:
            key = str(omron_email).strip().lower()
            if key in omron_owner:
                raise ValueError(
                    f"Users '{omron_owner[key]}' and '{name}' both use the OMRON connect account {key}. Each person "
                    "needs their own OMRON connect account so nobody's readings reach the wrong Garmin account."
                )
            omron_owner[key] = name

        for section in ("strava", "zwift"):
            if section in u:
                raise ValueError(
                    f"User '{name}' has a '{section}' section. With more than one person, this build "
                    f"syncs to Garmin only; remove the {section} section from {path}."
                )


def load_config(path: Path) -> AppConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    # An empty file parses to None and a stray top-level list parses to a list;
    # both used to reach raw.get("users") and die on AttributeError, and a
    # missing or empty users list died on a bare KeyError. None of those name
    # the file or say what to do about it.
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), list) or not raw["users"]:
        raise ValueError(
            f"No users found in {path}. The file is empty or malformed. "
            f"Restore it from a backup, or delete it and run homevitals to set up again."
        )

    raw = _walk_and_interpolate(raw)

    # Before any vault read, so a bad household config never prompts for passwords.
    validate_household_users(raw["users"], path)

    users = []
    for u in raw["users"]:
        name = str(u["name"]).strip()

        garmin = None
        if "garmin" in u:
            garmin = GarminConfig(
                email=u["garmin"]["email"],
                password=_get_password(name, "garmin", u["garmin"]["email"], u["garmin"].get("password")),
            )

        strava = None
        if "strava" in u:
            strava = StravaConfig(
                client_id=str(u["strava"]["client_id"]),
                client_secret=_get_strava_secret(name, u["strava"].get("client_secret")),
            )

        zwift = None
        if "zwift" in u:
            zwift = ZwiftConfig(
                email=u["zwift"]["email"],
                password=_get_password(name, "zwift", u["zwift"]["email"], u["zwift"].get("password")),
            )

        omron = None
        if "omron" in u:
            # A blood pressure source, not a target: readings can only go to this person's Garmin.
            o = u["omron"]
            omron = OmronConfig(
                email=str(o["email"]).strip(),
                password=_get_password(name, "omron", o["email"], o.get("password")),
                country=str(o["country"]).strip().upper(),
                server=_normalise_omron_server(o.get("server")),
                user_number=_omron_user_number(o["user_number"]) if o.get("user_number") is not None else None,
            )

        # No "must have a sync target" rule: a partly set up person loads fine and is
        # simply not ready (scale_ready / bp_ready decide that at sync time).
        eufy = None
        if "eufy" in u:
            e = u["eufy"]
            eufy = EufyConfig(
                email=e["email"],
                password=_get_password(name, "eufy", e["email"], e.get("password")),
                customer_id=str(e["customer_id"]).strip() if e.get("customer_id") is not None else None,
            )

        users.append(UserConfig(
            name=name,
            eufy=eufy,
            garmin=garmin,
            strava=strava,
            zwift=zwift,
            omron=omron,
        ))

    return AppConfig(users=users)
