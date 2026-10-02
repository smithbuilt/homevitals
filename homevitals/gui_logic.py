"""Everything the household window does, as plain functions with no drawing code.

gui.py draws the window and calls these. Keeping them apart means the logic is
tested directly, without a screen.

Rules for this module: never log a form field or a password, and never put an
exception's raw text into a message meant for the window. Details go to the
log file through logger.exception.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from garminconnect import GarminConnectTooManyRequestsError

from homevitals import platform_support
from homevitals.config import OMRON_V1_COUNTRIES, EufyConfig, OmronConfig, load_config, validate_household_users
from homevitals.credentials import (
    account_token_name,
    delete_password,
    delete_token,
    get_password,
    migrate_legacy_tokens,
    store_password,
)
from homevitals.eufy_client import EufyClient, EufyProfile
from homevitals.garmin_auth import GarminAuth, GarminLoginCancelled
from homevitals.garmin_client import GarminClient
from homevitals.network import is_transient_network_error
from homevitals.omron_client import OmronClient, OmronLoginError
from homevitals.prompt import PROMPT_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Person:
    name: str
    eufy_email: str | None              # None when the person has no scale connected
    garmin_email: str | None
    customer_id: str | None
    has_omron: bool = False
    omron_email: str | None = None
    omron_country: str | None = None

    @property
    def has_eufy(self) -> bool:
        return bool(self.eufy_email)

    @property
    def has_garmin(self) -> bool:
        return bool(self.garmin_email)


def person_row(person: Person, progress: dict[tuple[str, str], tuple[str, int | None]] | None = None,
               frame: int = 0) -> tuple[str, str, str, str]:
    """The four cells of the people list: name, Garmin account, scale profile, blood pressure.

    progress (from a running or finished sync) adds a symbol after the scale and
    blood pressure cells; frame turns the "syncing" spinner.
    """
    if person.customer_id:
        scale = f"...{person.customer_id[-4:]}"
    elif person.has_eufy:
        scale = "profile not chosen"
    else:
        scale = "not connected"
    bp = "on" if person.has_omron else "off"
    progress = progress or {}
    scale = _with_progress(scale, progress.get((person.name, "scale")), frame)
    bp = _with_progress(bp, progress.get((person.name, "bp")), frame)
    return (person.name, person.garmin_email or "not connected", scale, bp)


# ---------------------------------------------------------------------------
# Progress symbols in the people list (owner request, 2026-10-02)
# ---------------------------------------------------------------------------

SPINNER = "◐◓◑◒"


@dataclass(frozen=True)
class ProgressEvent:
    name: str
    step: str                 # "scale" | "bp"
    state: str                # "start" | "done" | "failed"
    count: int | None = None  # how many were sent (done only); never a weight or a reading


def parse_progress(line: str) -> ProgressEvent | None:
    """A progress marker printed by the sync for the window, or None for any other line."""
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) < 4 or parts[0] != "@progress" or parts[2] not in ("scale", "bp") \
            or parts[3] not in ("start", "done", "failed"):
        return None
    count = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
    return ProgressEvent(parts[1], parts[2], parts[3], count)


def sync_plan(people: list[Person]) -> dict[tuple[str, str], tuple[str, int | None]]:
    """What a sync is about to do: every connected scale and monitor starts as "waiting"."""
    plan: dict[tuple[str, str], tuple[str, int | None]] = {}
    for p in people:
        if p.has_garmin and p.has_eufy:
            plan[(p.name, "scale")] = ("waiting", None)
        if p.has_garmin and p.has_omron:
            plan[(p.name, "bp")] = ("waiting", None)
    return plan


def apply_progress(progress: dict[tuple[str, str], tuple[str, int | None]], event: ProgressEvent) -> None:
    key = (event.name, event.step)
    if event.state == "start":
        progress[key] = ("syncing", None)
    elif event.state == "done":
        progress[key] = ("done", event.count or 0)
    else:
        progress[key] = ("failed", None)


def finish_progress(progress: dict[tuple[str, str], tuple[str, int | None]]) -> None:
    """The sync ended: anything it never got to (it stopped early) loses its symbol."""
    for key in [k for k, (state, _) in progress.items() if state in ("waiting", "syncing")]:
        del progress[key]


def row_state(progress: dict[tuple[str, str], tuple[str, int | None]], name: str) -> str | None:
    """One word for a person's whole row (it sets the row's tint): failed, syncing, waiting, done or None."""
    states = {state for (who, _), (state, _) in progress.items() if who == name}
    for state in ("failed", "syncing", "waiting", "done"):
        if state in states:
            return state
    return None


def progress_running(progress: dict[tuple[str, str], tuple[str, int | None]]) -> bool:
    return any(state == "syncing" for state, _ in progress.values())


def _with_progress(text: str, entry: tuple[str, int | None] | None, frame: int) -> str:
    if entry is None:
        return text
    state, count = entry
    if state == "waiting":
        return f"{text}   ⋯ waiting"
    if state == "syncing":
        return f"{text}   {SPINNER[frame % len(SPINNER)]} syncing"
    if state == "failed":
        return f"{text}   ✗ failed"
    return f"{text}   ✓ {count} new" if count else f"{text}   ✓ up to date"


def account_status(person: Person | None, service: str) -> str:
    """The status line of one account section in the Person window."""
    if person is None:
        return "Not connected."
    if service == "garmin":
        return f"Connected as {person.garmin_email}." if person.has_garmin else "Not connected."
    if service == "eufy":
        if not person.has_eufy:
            return "Not connected."
        if person.customer_id:
            return f"Connected as {person.eufy_email}, profile ...{person.customer_id[-4:]}."
        return f"Connected as {person.eufy_email}, no profile chosen yet."
    if person.has_omron and person.omron_email:
        return f"Connected as {person.omron_email} ({country_name(person.omron_country or '')})."
    return "Not connected."


def _raw_users(config_path: Path) -> list[dict]:
    """The users list straight from config.yaml, or [] if the file is missing or damaged."""
    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), list):
        return []
    return [u for u in raw["users"] if isinstance(u, dict)]


def _section(user: dict, name: str) -> dict:
    value = user.get(name)
    return value if isinstance(value, dict) else {}


def read_people(config_path: Path) -> list[Person]:
    """Who is set up, read from the raw YAML. Never reads or returns passwords."""
    people = []
    for u in _raw_users(config_path):
        customer_id = _section(u, "eufy").get("customer_id")
        garmin_email = _section(u, "garmin").get("email")
        omron = _section(u, "omron")
        people.append(Person(
            name=str(u.get("name") or "").strip(),
            eufy_email=str(_section(u, "eufy").get("email") or "").strip() or None,
            garmin_email=str(garmin_email) if garmin_email else None,
            customer_id=str(customer_id).strip() if customer_id is not None else None,
            has_omron=isinstance(u.get("omron"), dict),
            omron_email=str(omron.get("email") or "").strip() or None,
            omron_country=str(omron.get("country") or "").strip().upper() or None,
        ))
    return people


def config_problem(config_path: Path) -> str | None:
    """A plain sentence if the settings file breaks a household rule, else None."""
    path = Path(config_path)
    if not path.exists():
        return None
    users = _raw_users(path)
    if not users:
        return translate_error("No users found. The file is empty or malformed.")
    try:
        validate_household_users(users, path)
    except ValueError as e:
        return translate_error(str(e))
    return None


def _same_email(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


# ---------------------------------------------------------------------------
# Add person
# ---------------------------------------------------------------------------


def validate_name(name: str, people: list[Person]) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Please type a name."
    if len(name) > 40:
        return "Keep the name under 40 characters."
    if any(p.name.lower() == name.lower() for p in people):
        return f"Someone called '{name}' is already set up. Pick a different name."
    return None


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_email(email: str) -> str | None:
    if not _EMAIL.match((email or "").strip()):
        return "That doesn't look like an email address."
    return None




@dataclass(frozen=True)
class ProfileChoice:
    customer_id: str
    label: str
    linked_to: str | None


def profile_label(profile: EufyProfile) -> str:
    lb = profile.last_weight_kg * 2.20462
    when = profile.last_measured.strftime("%Y-%m-%d")
    name = profile.name or f"Profile ...{profile.customer_id[-4:]}"
    return f"{name} - {profile.last_weight_kg:.1f} kg ({lb:.1f} lb), last weigh-in {when}"


def profile_choices(profiles: list[EufyProfile], people: list[Person]) -> list[ProfileChoice]:
    """One choice per Eufy profile, newest weigh-in first, marking the ones already linked."""
    owners = {p.customer_id: p.name for p in people if p.customer_id}
    return [ProfileChoice(p.customer_id, profile_label(p), owners.get(p.customer_id)) for p in profiles]


def validate_profile_choice(customer_id: str | None, people: list[Person]) -> str | None:
    if not customer_id:
        return "Pick the profile that belongs to this person."
    for p in people:
        if p.customer_id == customer_id:
            return f"That profile is already linked to {p.name}."
    return None


def validate_garmin_email(email: str, people: list[Person]) -> str | None:
    problem = validate_email(email)
    if problem:
        return problem
    for p in people:
        if _same_email(p.garmin_email, email):
            return f"{p.name} already syncs to that Garmin account. Each person needs their own."
    return None


# ---------------------------------------------------------------------------
# Remove person
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemovalResult:
    removed: bool
    eufy_token_deleted: bool
    last_person: bool


def removal_confirmation_text(name: str) -> str:
    return f"Remove {name}? Their Garmin data stays in Garmin; this only stops syncing."


def remove_person(config_path: Path, name: str) -> RemovalResult:
    """Stop syncing one person. Their Garmin data and the sync history are left alone.

    The config is rewritten first and the secrets removed after, so a failed
    write never leaves a person in the list without their passwords. The Eufy
    token is shared by everyone on that Eufy login, so it is only removed when
    nobody left uses it. With nobody left, the settings file goes and automatic
    sync is turned off (otherwise the 4-hourly run would report an error forever).
    """
    from homevitals.cli import shared

    path = Path(config_path)
    users = _raw_users(path)
    target = next((u for u in users if str(u.get("name", "")).strip() == name.strip()), None)
    if target is None:
        return RemovalResult(False, False, False)
    remaining = [u for u in users if u is not target]

    last_person = not remaining
    if last_person:
        path.unlink()
    else:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        config["users"] = remaining
        shared._write_config(path, config)

    raw_name = str(target.get("name", "")).strip()
    for suffix in ("eufy", "garmin", "omron"):
        delete_password(f"{raw_name}:{suffix}")
    garmin_email = _section(target, "garmin").get("email")
    if garmin_email:
        delete_token(account_token_name("garmin", garmin_email))
    eufy_email = _section(target, "eufy").get("email")
    eufy_token_deleted = False
    if eufy_email and not any(_same_email(_section(u, "eufy").get("email"), eufy_email) for u in remaining):
        delete_token(account_token_name("eufy", eufy_email))
        eufy_token_deleted = True
    omron_email = _section(target, "omron").get("email")
    if omron_email and not any(_same_email(_section(u, "omron").get("email"), omron_email) for u in remaining):
        delete_token(account_token_name("omron", omron_email))

    if last_person:
        # Nobody left: the old unkeyed one-person tokens go too, so the next person
        # added can never inherit a previous owner's session.
        from homevitals.cli import shared
        delete_token("garmin")
        delete_token("eufy")
        for legacy in ("session.json", "eufy_token.json"):
            with contextlib.suppress(FileNotFoundError):
                (shared.DATA_DIR / legacy).unlink()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                platform_support.uninstall_agent()
        except Exception:
            logger.exception("Could not turn off automatic sync after removing the last person")
    logger.info("Removed person %s", raw_name)
    return RemovalResult(True, eufy_token_deleted, last_person)


# ---------------------------------------------------------------------------
# Sync now
# ---------------------------------------------------------------------------


# "Bring in older readings" looks this far back in OMRON connect (about 10 years).
OLDER_BP_READINGS_DAYS = 3650


def sync_command(older_bp_readings: bool = False) -> list[str]:
    """Run the command-line sync as a child process, so the run lock still works.

    older_bp_readings asks OMRON connect for up to 10 years of blood pressure
    readings instead of the usual window; the scale step is unchanged.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            exe = console
    cmd = [str(exe), "-m", "homevitals", "--headless"]
    if older_bp_readings:
        cmd += ["--bp-backfill-days", str(OLDER_BP_READINGS_DAYS)]
    return cmd


def sync_environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Lines arrive as they happen (not in one block at the end), with the progress markers.
    env["PYTHONUNBUFFERED"] = "1"
    env["EUFY_SYNC_PROGRESS"] = "1"
    return env


_LOG_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} (\w+) [\w.]+: (.*)$")
_TRACEBACK_LINE = re.compile(r"^(Traceback \(most recent call last\)|  File \"|\s+\^|[A-Za-z_][\w.]*(Error|Exception): )")
_PER_PERSON = re.compile(r"^(synced .*?\.|nothing new\.)(?: (\w+) failed - (.*))?$")

PLAYWRIGHT_TEXT = ("Garmin's direct login didn't work this time (Garmin sometimes blocks repeated logins). "
                   "Wait an hour, then click Fix problems and log in again.")
SLOW_DOWN_TEXT = "Garmin is asking us to slow down. Wait an hour and try again."
LOCK_HELD_TEXT = "A sync is already running (probably the automatic one). Try again in a minute."


def _person_prefix(line: str, people: list[Person]) -> tuple[str, str] | None:
    """("Chris", rest) when the line starts with "Chris: " for a known person."""
    for p in sorted(people, key=lambda p: len(p.name), reverse=True):
        if p.name and line.startswith(f"{p.name}: "):
            return p.name, line[len(p.name) + 2:]
    return None


def _mentioned_person(line: str, people: list[Person]) -> str | None:
    for p in people:
        if p.name and (f"{p.name}/" in line or f"for {p.name}" in line or f"user '{p.name}'" in line):
            return p.name
    return None


def _translate_message(text: str, name: str | None) -> str | None:
    """Plain words for one problem message, or None when no rule knows it."""
    if "--reauth" in text:
        return f"Garmin needs {name or 'someone'} to log in again. Click Fix problems."
    if "--update-password" in text or "changed your Eufy password" in text or "rejected the email or password" in text:
        service = "Eufy" if "Eufy" in text else "Garmin"
        return f"{name or 'Someone'}'s {service} password is wrong or has changed. Click Fix problems and enter the new one."
    if "--select-profile" in text or "multiple Eufy profiles" in text:
        return f"{name or 'Someone'} isn't linked to a scale profile yet. Click Fix problems and choose their profile."
    if "Playwright" in text or "browser fallback" in text:
        return PLAYWRIGHT_TEXT
    if "429" in text or "Too Many Requests" in text or "rate limit" in text.lower():
        return SLOW_DOWN_TEXT
    if "Network unavailable; retrying" in text:
        return "No internet connection right now. Trying once more in a minute..."
    if is_transient_network_error(text):
        return "Couldn't reach the internet. Check the connection and try again."
    return None


def omron_login_rejected_text(name: str | None = None, country: str | None = None) -> str:
    """What to check when OMRON connect refuses a login: the country matters as much as the password."""
    who = f"{name}'s login" if name else "this login"
    picked = f" You picked {country_name(country)}." if country else ""
    return (f"OMRON connect didn't accept {who}. Check three things: the email, the password, and the country. "
            "The country must be the one chosen when this OMRON connect account was first created (the OMRON "
            f"connect app asks for it at sign-up), because each country uses its own OMRON server.{picked}")


def _translate_bp_failure(error: str, name: str) -> str:
    """Plain words for "{name}: blood pressure failed - {error}" (the error is already safe text)."""
    if "OMRON connect rejected the login" in error:
        return f"{omron_login_rejected_text(name)} Click Fix problems."
    if "OMRON connect" in error:
        if is_transient_network_error(error):
            return (f"Couldn't reach OMRON connect for {name}'s blood pressure. Check the internet connection and "
                    "try again.")
        return f"{name}'s blood pressure sync didn't finish. Details are in the log file."
    if "--reauth" in error:
        return f"Garmin needs {name} to log in again. Click Fix problems."
    if "--update-password" in error:
        return f"{name}'s Garmin password is wrong or has changed. Click Fix problems and enter the new one."
    if "429" in error or "slow down" in error:
        return SLOW_DOWN_TEXT
    if is_transient_network_error(error):
        return (f"Couldn't reach OMRON connect for {name}'s blood pressure. Check the internet connection and "
                "try again.")
    return f"{name}'s blood pressure sync didn't finish. Details are in the log file."


def line_means_nothing_new(line: str) -> bool:
    """True for the CLI lines that mean nothing at all was synced (the finish message then adds a hint).

    A per-person "Jane: nothing new." doesn't count: someone else may have synced in the same run.
    The CLI prints "No new measurements..." exactly when nobody got anything.
    """
    text = line.strip()
    m = _LOG_PREFIX.match(text)
    if m:
        text = m.group(2)
    return text.startswith("No new measurements")


def translate_line(line: str, people: list[Person]) -> str | None:
    """Plain-language text for one line of sync output, or None to hide it.

    Hidden lines (debug output, tracebacks) still reach the log file, because
    the window logs every raw line before translating it.
    """
    text = line.rstrip("\r\n")
    if text.startswith("@progress\t"):
        return None     # drawn as symbols in the people list, never shown as text
    if not text.strip():
        return None
    m = _LOG_PREFIX.match(text)
    if m:
        if m.group(1) == "DEBUG":
            return None
        text = m.group(2)
    if _TRACEBACK_LINE.match(text):
        return None
    if text.startswith("New in homevitals:"):
        return None      # upstream's one-time Strava notice; this build syncs to Garmin only
    if "Another homevitals run is in progress" in text:
        return LOCK_HELD_TEXT
    if text.startswith("No config found"):
        return "Nobody is set up yet. Click Add person first."
    m = re.match(r"^homevitals could not start: (.+)$", text)
    if m:
        return "Couldn't start the sync: " + translate_error(m.group(1))
    m = re.match(r"^Syncs completed: Garmin (\d+)\.(?: Blood pressure: (\d+) readings?\.)?", text)
    if m:
        n = int(m.group(1))
        weigh_ins = f"{n} weigh-in{'s' if n != 1 else ''}"
        if m.group(2):
            bp = int(m.group(2))
            return f"Synced {weigh_ins} and {bp} blood pressure reading{'s' if bp != 1 else ''} to Garmin."
        return f"Synced {weigh_ins} to Garmin."
    m = re.match(r"^Syncs completed: blood pressure (\d+)\.", text)
    if m:
        bp = int(m.group(1))
        return f"Synced {bp} blood pressure reading{'s' if bp != 1 else ''} to Garmin."
    if re.match(r"^No new measurements for \d+ profiles?\.", text):
        return "Nothing new to sync for anyone."
    if text.startswith("No new measurements"):
        return "Nothing new to sync."
    if "open the Eufy app so it uploads to the cloud" in text:
        return "If someone weighed in recently, open the Eufy app on the phone first, then sync again."

    prefixed = _person_prefix(text, people)
    if prefixed:
        name, rest = prefixed
        if rest.startswith("blood pressure failed - "):
            return _translate_bp_failure(rest[len("blood pressure failed - "):], name)
        if rest.startswith("blood pressure - "):
            return text          # already plain: hint, skipped readings
        if rest.startswith(("not fully set up yet", "not set up yet")):
            return text          # a partly set up person: already plain, and not a failure
        m = _PER_PERSON.match(rest)
        if m:
            head = f"{name}: {m.group(1)}" if m.group(1).startswith("synced") else f"{name}: nothing new to sync."
            if m.group(2):
                # An unknown error stays in the log; raw error text never reaches the window.
                error = m.group(3)
                head += " " + (_translate_message(error, name) or f"{m.group(2)} failed. Details are in the log file.")
            return head
        if rest.startswith("failed - "):
            return (_translate_message(rest[len("failed - "):], name)
                    or f"{name}: sync failed. Details are in the log file.")

    translated = _translate_message(text, _mentioned_person(text, people))
    if translated:
        return translated
    m = re.match(r"^Sync failed for: (.+)\. Run with --verbose", text)
    if m:
        parts = []
        for item in m.group(1).split(", "):
            who, _, target = item.partition("/")
            label = "blood pressure" if target == "garmin_bp" else target.capitalize()
            parts.append(f"{who} ({label})" if target else who)
        return f"Sync didn't finish for {', '.join(parts)}. See the lines above."
    m = re.search(r"Garmin already has a weigh-in dated (\S+?)\.?$", text)
    if m:
        return f"Garmin already had a weigh-in for {m.group(1)}, so that one was skipped."
    if "Garmin skipped existing weigh-ins" in text or "already has weigh-ins for" in text:
        return "Garmin already had some of these weigh-ins, so they were skipped."
    return text


def finish_message(exit_code: int, saw_nothing_new: bool) -> str:
    if exit_code == 0 and not saw_nothing_new:
        return "Done."
    if exit_code == 0:
        return ("Done. Nothing new to sync. If someone weighed in recently, open the Eufy app on their phone "
                "first (the scale sends weigh-ins through the app), then click Sync now again.")
    return "Sync finished with problems. Read the lines above. Fix problems can repair logins and passwords."


def translate_error(text: str) -> str:
    """Config and start-up errors in plain words (first match wins)."""
    who = re.search(r"User '(.+?)'", text)
    name = who.group(1) if who else "Someone"
    if "omron section without an email" in text or "omron.email that is not an email address" in text:
        return f"{name}'s blood pressure monitor settings are incomplete. Use Edit person to connect it again."
    if "no valid omron.country" in text:
        return (f"{name}'s OMRON connect country is missing or wrong. Use Edit person and pick the country the "
                "account was created in.")
    if "use OMRON's older server system" in text:
        return (f"{name}'s OMRON connect account was created in a country this build can't sync from yet. Use Edit "
                "person to check the country, or see the readme.")
    if "both use the OMRON connect account" in text:
        return "Two people are set up with the same OMRON connect account. Each person needs their own."
    if "invalid omron.server" in text or "invalid omron.user_number" in text:
        return (f"{name}'s blood pressure settings have an invalid advanced option (server or user number). "
                "See the readme.")
    m = re.search(r"No omron password found for user '(.+?)'", text)
    if m:
        return f"{m.group(1)}'s OMRON connect password is missing. Click Fix problems to enter it."
    if "has an eufy section without an email" in text:
        return f"{name}'s scale settings are incomplete. Open {name} in the window and connect the scale again."
    if "has a garmin section without an email" in text:
        return f"{name}'s Garmin settings are incomplete. Open {name} in the window and connect Garmin again."
    m = re.search(r"User '(.+?)' has no eufy\.customer_id", text)
    if m:
        return f"{m.group(1)} isn't linked to a scale profile yet. Click Fix problems and choose their profile."
    if "User names must be unique" in text:
        return "Two people have the same name in the settings file. Remove one of them."
    if "both use the Garmin account" in text:
        return "Two people are set up with the same Garmin account. Each person needs their own."
    if "linked to the same Eufy profile" in text:
        return "Two people are linked to the same scale profile. Remove one and add them again."
    m = re.search(r"No (\w+) password found for user '(.+?)'", text)
    if m:
        return f"{m.group(2)}'s {m.group(1).capitalize()} password is missing. Click Fix problems to enter it."
    if "No users found" in text or "empty or malformed" in text:
        return "The settings file is empty or damaged. Remove it and add people again."
    if "keychain could not be read" in text:
        return "Windows couldn't open the saved passwords. Sign out and back in to Windows, then try again."
    return text


# ---------------------------------------------------------------------------
# Fix problems
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    text: str
    person: str | None = None
    action: str | None = None   # "garmin_login" | "change_eufy_password" | "change_garmin_password" |
                                # "change_omron_password" | "turn_on_autosync" | "choose_profile" |
                                # "open_person" | None


def autosync_result(installed: bool) -> CheckResult:
    if installed:
        return CheckResult(True, "Automatic sync is on (every 4 hours).")
    return CheckResult(False, "Automatic sync is off.", None, "turn_on_autosync")


def eufy_profile_result(person: Any, present: bool) -> CheckResult:
    name = person.name
    customer_id = getattr(person, "customer_id", None)
    if customer_id is None and getattr(person, "eufy", None) is not None:
        customer_id = person.eufy.customer_id
    if not customer_id:
        return CheckResult(False, f"{name} isn't linked to a scale profile yet.", name, "choose_profile")
    if present:
        return CheckResult(True, f"Eufy login works and {name}'s scale profile is there.", name)
    return CheckResult(False, f"{name}'s scale profile was not found on the Eufy account. It may have been deleted "
                              f"in the Eufy app. Remove {name} and add them again.", name)


def classify_eufy_error(exc: BaseException, name: str) -> CheckResult:
    text = str(exc)
    if "--update-password" in text or "Eufy login failed" in text:
        return CheckResult(False, f"The Eufy password for {name} is wrong or has changed.", name, "change_eufy_password")
    if is_transient_network_error(text):
        return CheckResult(False, "Couldn't reach Eufy. Check the internet connection and try again.", name)
    return CheckResult(False, f"Eufy didn't answer as expected for {name}. Details are in the log file.", name)


def classify_garmin_error(exc: BaseException, name: str) -> CheckResult:
    text = str(exc)
    if "Garmin login cancelled" in text:
        return CheckResult(False, "Garmin login was cancelled. If the code email never arrived, the password is "
                                  "probably wrong.", name, "garmin_login")
    if isinstance(exc, GarminLoginCancelled) or "--reauth" in text:
        return CheckResult(False, f"Garmin needs {name} to log in again.", name, "garmin_login")
    if "--update-password" in text or "rejected the email or password" in text:
        return CheckResult(False, f"Garmin says the password for {name} is wrong.", name, "change_garmin_password")
    if isinstance(exc, GarminConnectTooManyRequestsError) or "429" in text:
        return CheckResult(False, SLOW_DOWN_TEXT, name)
    if "Playwright" in text or "browser fallback" in text:
        return CheckResult(False, "Garmin's direct login didn't work this time (Garmin sometimes blocks repeated "
                                  "logins). Wait an hour, then try Log in again.", name, "garmin_login")
    if is_transient_network_error(text):
        return CheckResult(False, "Couldn't reach Garmin. Check the internet connection and try again.", name)
    return CheckResult(False, f"Garmin didn't answer as expected for {name}. Details are in the log file.", name)


def run_checks(config_path: Path, on_result: Callable[[CheckResult], None]) -> list[CheckResult]:
    """Check everyone's logins and profiles. Runs in a worker thread.

    The caller holds the run lock. Each result goes to on_result as soon as it
    is known, and all of them are returned. Nothing propagates: an unexpected
    error becomes one failed row, with the details in the log file.
    """
    results: list[CheckResult] = []

    def emit(result: CheckResult) -> None:
        results.append(result)
        on_result(result)

    try:
        try:
            config = load_config(Path(config_path))
        except (ValueError, RuntimeError) as e:
            # RuntimeError: the Windows password store couldn't be opened.
            emit(CheckResult(False, translate_error(str(e))))
            return results
        migrate_legacy_tokens(config.users)

        emit(autosync_result(platform_support.agent_installed()))

        accounts: dict[str, list] = {}
        for user in config.users:
            if user.eufy is not None:
                accounts.setdefault(user.eufy.email.strip().lower(), []).append(user)
        for users in accounts.values():
            client = EufyClient(users[0].eufy)
            try:
                client.authenticate()
                present = {p.customer_id for p in client.list_profiles()}
            except Exception as e:
                logger.exception("Fix problems: Eufy check failed")
                for user in users:
                    emit(classify_eufy_error(e, user.name))
                continue
            finally:
                with contextlib.suppress(Exception):
                    client.close()
            for user in users:
                emit(eufy_profile_result(user, user.eufy.customer_id in present))

        for user in config.users:
            if not user.garmin:
                continue
            garmin = GarminClient(user.garmin)
            try:
                garmin.authenticate(allow_interactive=False)
                garmin.check_connection()
                emit(CheckResult(True, f"Garmin login for {user.name} works.", user.name))
            except Exception as e:
                logger.exception("Fix problems: Garmin check failed for %s", user.name)
                emit(classify_garmin_error(e, user.name))
            finally:
                with contextlib.suppress(Exception):
                    garmin.close()

        for user in config.users:
            if not user.omron:
                continue
            omron = None
            try:
                omron = OmronClient(user.omron)
                omron.authenticate()
                omron.check_connection()
                emit(omron_ok_result(user.name))
            except Exception as e:
                logger.error("Fix problems: OMRON connect check failed for %s (%s)", user.name, type(e).__name__)
                emit(classify_omron_error(e, user.name))
            finally:
                if omron is not None:
                    with contextlib.suppress(Exception):
                        omron.close()

        from homevitals.config import missing_for_sync
        for user in config.users:
            if missing_for_sync(user):
                emit(not_ready_result(user))
    except Exception:
        logger.exception("Fix problems: the checks stopped unexpectedly")
        emit(CheckResult(False, "Couldn't finish the checks. Details are in the log file."))
    return results


# ---------------------------------------------------------------------------
# Blood pressure monitor (OMRON connect)
# ---------------------------------------------------------------------------

DEFAULT_OMRON_COUNTRY = "CA"
OMRON_COUNTRY_CHOICES: list[tuple[str, str]] = [
    ("CA", "Canada"),
    ("AT", "Austria"), ("BH", "Bahrain"), ("BE", "Belgium"), ("BG", "Bulgaria"), ("HR", "Croatia"), ("CY", "Cyprus"),
    ("CZ", "Czechia"), ("DK", "Denmark"), ("EE", "Estonia"), ("FI", "Finland"), ("FR", "France"), ("DE", "Germany"),
    ("GR", "Greece"), ("HU", "Hungary"), ("IE", "Ireland"), ("IT", "Italy"), ("KW", "Kuwait"), ("LV", "Latvia"),
    ("LT", "Lithuania"), ("LU", "Luxembourg"), ("MT", "Malta"), ("NL", "Netherlands"), ("OM", "Oman"), ("PL", "Poland"),
    ("PT", "Portugal"), ("QA", "Qatar"), ("RO", "Romania"), ("SA", "Saudi Arabia"), ("SK", "Slovakia"),
    ("SI", "Slovenia"), ("ES", "Spain"), ("SE", "Sweden"), ("CH", "Switzerland"), ("AE", "United Arab Emirates"),
    ("GB", "United Kingdom"), ("US", "United States"),
]
_COUNTRY_NAMES = dict(OMRON_COUNTRY_CHOICES)


def country_name(code: str) -> str:
    code = (code or "").strip().upper()
    return _COUNTRY_NAMES.get(code, code)


def country_label(code: str) -> str:
    """What the country box shows, e.g. "Canada (CA)"."""
    code = (code or "").strip().upper()
    return f"{country_name(code)} ({code})"


def country_code_from_label(text: str) -> str:
    """"Canada (CA)" -> "CA"; "ca" -> "CA"; "Canada" -> "CA"; anything else upper-cased."""
    value = (text or "").strip()
    m = re.search(r"\(([A-Za-z]{2})\)\s*$", value)
    if m:
        return m.group(1).upper()
    for code, name in OMRON_COUNTRY_CHOICES:
        if name.lower() == value.lower():
            return code
    return value.upper()


def validate_omron_country(code: str) -> str | None:
    code = country_code_from_label(code)
    if not re.fullmatch(r"[A-Z]{2}", code):
        return "Pick the country the OMRON connect account was created in."
    if code in OMRON_V1_COUNTRIES:
        return (f"OMRON connect accounts created in {country_name(code)} use OMRON's older servers, which this build "
                "does not support yet.")
    return None


def validate_omron_email(email: str, people: list[Person], exclude: str | None = None) -> str | None:
    problem = validate_email(email)
    if problem:
        return problem
    for p in people:
        if p.name != exclude and _same_email(p.omron_email, email):
            return f"{p.name} already syncs blood pressure from that OMRON connect account. Each person needs their own."
    return None


def _find_user(users: list[dict], name: str) -> dict | None:
    return next((u for u in users if str(u.get("name", "")).strip() == name.strip()), None)


def _write_users(config_path: Path, users: list[dict]) -> None:
    """Rewrite config.yaml with this users list, keeping any other top-level settings."""
    from homevitals.cli import shared

    path = Path(config_path)
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except yaml.YAMLError:
        config = {}
    if not isinstance(config, dict):
        config = {}
    config["users"] = users
    shared._write_config(path, config)


def _connect_section(config_path: Path, name: str, service: str, section: dict, password: str,
                     shared_login: bool = False) -> list[str]:
    """Save one account for one person, all or nothing. The login was already checked by the caller.

    Creates the person (their first account) when they aren't in the file yet.
    The household rules run on the would-be list before anything is written.
    With shared_login (the scale), everyone on that same login gets the
    password. Returns the names whose password was written, this person first.
    """
    path = Path(config_path)
    name = name.strip()
    users = _raw_users(path)
    target = _find_user(users, name)
    old = _section(target, service) if target is not None else {}
    if old and _same_email(old.get("email"), section.get("email")):
        # Same account: keep any advanced settings typed by hand (for example omron.server).
        section = {**{k: v for k, v in old.items() if k != "password"}, **section}
    if target is None:
        updated = {"name": name, service: section}
        would_be = [*users, updated]
    else:
        updated = dict(target, **{service: section})
        would_be = [updated if u is target else u for u in users]
    validate_household_users(would_be, path)

    names = [name]
    if shared_login:
        names += [str(u.get("name", "")).strip() for u in would_be
                  if u is not updated and _same_email(_section(u, service).get("email"), section.get("email"))]
    keys = [f"{n}:{service}" for n in names]
    previous = {key: get_password(key) for key in keys}
    for key in keys:
        store_password(key, password)
    try:
        _write_users(path, would_be)
    except BaseException:
        for key, value in previous.items():
            if value is None:
                delete_password(key)
            else:
                store_password(key, value)
        raise

    old_email = old.get("email")
    if old_email and not _same_email(old_email, section.get("email")):
        if not any(u is not updated and _same_email(_section(u, service).get("email"), old_email) for u in would_be):
            delete_token(account_token_name(service, old_email))
    logger.info("Connected %s for %s", service, name)
    return names


def _disconnect_section(config_path: Path, name: str, service: str) -> bool:
    """Remove one account from one person. The person stays (maybe with just a name); state.db is untouched."""
    path = Path(config_path)
    users = _raw_users(path)
    target = _find_user(users, name)
    if target is None or service not in target:
        return False
    email = _section(target, service).get("email")
    _write_users(path, [{k: v for k, v in u.items() if k != service} if u is target else u for u in users])
    delete_password(f"{str(target.get('name', '')).strip()}:{service}")
    if email and not any(u is not target and _same_email(_section(u, service).get("email"), email) for u in users):
        delete_token(account_token_name(service, email))
    logger.info("Disconnected %s for %s", service, name)
    return True


def check_garmin_login(email: str, password: str) -> None:
    """Log in to Garmin with these details (the code box appears if Garmin asks). Worker thread.

    The fresh session is saved for that Garmin account; no password or setting is written here.
    """
    GarminAuth(email.strip(), password).force_reauth()


def check_eufy_login(email: str, password: str, *, fresh: bool) -> list[EufyProfile]:
    """Log in to Eufy and list the scale's profiles. Worker thread.

    fresh: a typed password is checked by a real login (a saved shared session would hide a typo);
    otherwise the saved password of someone on the same Eufy login is used.
    """
    client = EufyClient(EufyConfig(email=email.strip(), password=password))
    try:
        if fresh:
            client._fresh_login()
        else:
            client.authenticate()
        return client.list_profiles()
    finally:
        client.close()


def check_omron_login(email: str, password: str, country: str) -> None:
    """Log in to OMRON connect with these details and read one day. Raises if anything fails.

    Runs in a worker thread. The session is saved for that account, which is
    harmless if the dialog is then cancelled.
    """
    client = OmronClient(OmronConfig(email=email.strip(), password=password, country=country.strip().upper()))
    try:
        client.authenticate(force_login=True)
        client.check_connection()
    finally:
        client.close()


def connect_garmin(config_path: Path, name: str, email: str, password: str) -> None:
    """Save this person's Garmin account (after check_garmin_login worked). Creates the person if new."""
    _connect_section(config_path, name, "garmin", {"email": email.strip()}, password)


def connect_scale(config_path: Path, name: str, email: str, password: str, customer_id: str) -> list[str]:
    """Save this person's scale login and profile together. Returns everyone whose Eufy password was updated."""
    if not (customer_id or "").strip():
        # The kids rule needs two people to fire in the config; even the first person must own a profile.
        raise ValueError("Pick the profile that belongs to this person.")
    return _connect_section(config_path, name, "eufy", {"email": email.strip(), "customer_id": customer_id.strip()},
                            password, shared_login=True)


def connect_omron(config_path: Path, name: str, email: str, password: str, country: str) -> None:
    """Save this person's OMRON connect account (after check_omron_login worked). Creates the person if new."""
    _connect_section(config_path, name, "omron", {"email": email.strip(), "country": country.strip().upper()},
                     password)


def disconnect_garmin(config_path: Path, name: str) -> bool:
    return _disconnect_section(config_path, name, "garmin")


def disconnect_scale(config_path: Path, name: str) -> bool:
    """Drops the scale login and the profile (which becomes free to link to someone else)."""
    return _disconnect_section(config_path, name, "eufy")


def disconnect_omron(config_path: Path, name: str) -> bool:
    """Turn off blood pressure for one person. Their readings stay in Garmin; the sync history is untouched."""
    return _disconnect_section(config_path, name, "omron")


def garmin_disconnect_confirmation_text(name: str) -> str:
    return (f"Disconnect Garmin for {name}? Nothing already in Garmin is removed. Syncing for {name} stops until "
            "Garmin is connected again.")


def scale_disconnect_confirmation_text(name: str) -> str:
    return (f"Disconnect the scale for {name}? Their weigh-ins already in Garmin stay there; this only stops new "
            "weigh-ins from syncing. Their scale profile becomes free to link again.")


def omron_disconnect_confirmation_text(name: str) -> str:
    return (f"Stop syncing blood pressure for {name}? Their readings stay in Garmin and in the OMRON connect app; "
            "this only stops new readings from syncing.")


def classify_omron_error(exc: BaseException, name: str) -> CheckResult:
    text = str(exc)
    if isinstance(exc, OmronLoginError) or "OMRON connect rejected the login" in text:
        return CheckResult(False, omron_login_rejected_text(name), name, "change_omron_password")
    if is_transient_network_error(text):
        return CheckResult(False, "Couldn't reach OMRON connect. Check the internet connection and try again.", name)
    if "even after logging in again" in text:
        return CheckResult(False, f"OMRON connect accepted the password but refused to hand over readings for {name}. "
                                  "Details are in the log file.", name)
    return CheckResult(False, f"OMRON connect didn't answer as expected for {name}. Details are in the log file.", name)


def omron_ok_result(person_name: str) -> CheckResult:
    return CheckResult(True, f"OMRON connect login works for {person_name}.", person_name)


def garmin_login_again(config_path: Path, name: str) -> None:
    """Log this person into Garmin again (the code pop-up appears if Garmin asks)."""
    config = load_config(Path(config_path))
    user = next((u for u in config.users if u.name == name.strip()), None)
    if user is None or user.garmin is None:
        raise ValueError(f"No person named '{name}' with a Garmin account.")
    GarminAuth(user.garmin.email, user.garmin.password).force_reauth()


def _user_from_config(config_path: Path, name: str):
    config = load_config(Path(config_path))
    return next((u for u in config.users if u.name == name.strip()), None)


def scale_login_again(config_path: Path, name: str) -> CheckResult:
    """A fresh Eufy login with the saved password, then check this person's profile is still there."""
    user = _user_from_config(config_path, name)
    if user is None or user.eufy is None:
        raise ValueError(f"No person named '{name}' with a scale connected.")
    client = EufyClient(user.eufy)
    try:
        client._fresh_login()       # proves the saved password; authenticate() would accept an old session
        present = {p.customer_id for p in client.list_profiles()}
    finally:
        client.close()
    return eufy_profile_result(user, user.eufy.customer_id in present)


def omron_login_again(config_path: Path, name: str) -> CheckResult:
    """A fresh OMRON connect login with the saved password."""
    user = _user_from_config(config_path, name)
    if user is None or user.omron is None:
        raise ValueError(f"No person named '{name}' with a blood pressure monitor connected.")
    client = OmronClient(user.omron)
    try:
        client.authenticate(force_login=True)
        client.check_connection()
    finally:
        client.close()
    return omron_ok_result(user.name)


def list_profiles_for_person(config_path: Path, name: str) -> list[ProfileChoice]:
    """The scale's profiles for Choose profile, with other people's profiles marked as linked."""
    user = _user_from_config(config_path, name)
    if user is None or user.eufy is None:
        raise ValueError(f"No person named '{name}' with a scale connected.")
    client = EufyClient(user.eufy)
    try:
        client.authenticate()
        profiles = client.list_profiles()
    finally:
        client.close()
    return profile_choices(profiles, [p for p in read_people(config_path) if p.name != name.strip()])


def not_ready_result(user) -> CheckResult:
    from homevitals.config import missing_for_sync, nothing_connected

    what = " and ".join(missing_for_sync(user))
    if nothing_connected(user):
        return CheckResult(False, f"{user.name} isn't set up yet: connect {what}.", user.name, "open_person")
    return CheckResult(False, f"{user.name} isn't fully set up yet: connect {what} so there is something to sync.",
                       user.name, "open_person")


def choose_profile(config_path: Path, name: str, customer_id: str) -> None:
    from homevitals.cli import profiles

    others = [p for p in read_people(config_path) if p.name != name.strip()]
    problem = validate_profile_choice(customer_id, others)
    if problem:
        raise ValueError(problem)
    profiles._save_customer_id(Path(config_path), customer_id, name)


class MfaBridge:
    """Lets a worker thread ask the main thread for the Garmin code. No tkinter here.

    post runs a callable on the main thread (the window's queue); ask shows the
    code box there and returns the code, or None for Cancel.
    """

    def __init__(self, post: Callable[[Callable[[], None]], None], ask: Callable[[], str | None],
                 timeout: float = PROMPT_TIMEOUT_SECONDS) -> None:
        self._post = post
        self._ask = ask
        self._timeout = timeout

    def prompt(self) -> str:
        """Worker side. Returns the code, or "" when cancelled or nobody answered in time."""
        done = threading.Event()
        answer: dict[str, str | None] = {}

        def ask_on_main_thread() -> None:
            try:
                answer["code"] = self._ask()
            except Exception:
                logger.exception("The Garmin code box failed")
            finally:
                done.set()

        self._post(ask_on_main_thread)
        if not done.wait(self._timeout):
            return ""
        return answer.get("code") or ""


# ---------------------------------------------------------------------------
# Desktop shortcut (Windows only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShortcutPlan:
    target: str
    arguments: str
    working_dir: str


def shortcut_plan() -> ShortcutPlan | None:
    """What the desktop shortcut should open: the homevitals-gui launcher, else pythonw."""
    found = shutil.which(GUI_LAUNCHER)
    if found and Path(found).exists():
        # Always a full path: started from the launcher's own folder, which() answers ".\\homevitals-gui.EXE",
        # and a shortcut to that points nowhere.
        found = os.path.abspath(found)
        return ShortcutPlan(found, "", str(Path(found).parent))
    beside = Path(sys.executable).with_name(f"{GUI_LAUNCHER}.exe")
    if beside.exists():
        return ShortcutPlan(str(beside), "", str(beside.parent))
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return ShortcutPlan(str(pythonw), "-m homevitals.gui", str(Path.home()))
    return None


SHORTCUT_NAME = "HomeVitals"
# Names shortcuts had in earlier versions ("Health Sync for Garmin": household.5-9;
# "Household scale sync": household.4 and older). They move to SHORTCUT_NAME on first start.
OLD_SHORTCUT_NAMES = ("Health Sync for Garmin", "Household scale sync")
GUI_LAUNCHER = "homevitals-gui"


# Other apps that already get these readings from Garmin Connect, so nothing needs setting
# up in HomeVitals for them. Checked against each service's own help pages and developer
# posts (2026-10): Runalyze imports weight and blood pressure from Garmin; intervals.icu
# and TrainingPeaks import weight but not blood pressure; Strava imports activities only.
OTHER_APPS_NOTE_TITLE = "Using Runalyze, intervals.icu or TrainingPeaks? There's nothing to set up here."
OTHER_APPS_NOTE = ("Link them to Garmin Connect in their own settings and they pick up these readings from Garmin: "
                   "Runalyze gets weight and blood pressure; intervals.icu and TrainingPeaks get weight (not blood "
                   "pressure). Strava takes activities from Garmin, but not weight or blood pressure.")


def icon_png_path() -> Path | None:
    """The logo as a PNG (for the window's header band)."""
    path = Path(__file__).with_name("assets") / "app_icon.png"
    return path if path.exists() else None


def icon_path() -> Path | None:
    """The app's logo as a Windows .ico, shipped inside the package (None if it's missing)."""
    path = Path(__file__).with_name("assets") / "app_icon.ico"
    return path if path.exists() else None


def shortcut_script(plan: ShortcutPlan, name: str = SHORTCUT_NAME, folder: str = "Desktop",
                    icon: Path | None = None) -> str:
    """PowerShell that makes a shortcut in a special folder ("Desktop" or "Startup").

    GetFolderPath follows the real location, so a Desktop moved to another
    drive or into OneDrive still works.
    """
    def q(value: str) -> str:
        return value.replace("'", "''")

    lines = [
        f"$d = [Environment]::GetFolderPath('{q(folder)}')",
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '{q(name)}.lnk'))",
        f"$s.TargetPath = '{q(plan.target)}'",
        f"$s.Arguments = '{q(plan.arguments)}'",
        f"$s.WorkingDirectory = '{q(plan.working_dir)}'",
    ]
    if icon is not None:
        lines.append(f"$s.IconLocation = '{q(str(icon))},0'")
    lines.append("$s.Save()")
    lines.append("Write-Output $s.FullName")      # where it went, so the taskbar ID can be added after
    return "\n".join(lines)


def startup_shortcut_path() -> Path:
    """Where the "open when Windows starts" shortcut lives: this user's Startup folder."""
    base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"{SHORTCUT_NAME}.lnk"


def startup_enabled() -> bool:
    return any(startup_shortcut_path().with_name(f"{name}.lnk").exists()
               for name in (SHORTCUT_NAME, *OLD_SHORTCUT_NAMES))


def disable_startup() -> None:
    for path in [startup_shortcut_path().with_name(f"{name}.lnk") for name in (SHORTCUT_NAME, *OLD_SHORTCUT_NAMES)]:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def shortcut_retarget_script(lnk: Path, plan: ShortcutPlan, icon: Path | None = None,
                             keep_arguments: bool = False) -> str:
    """PowerShell that points an existing shortcut at plan, keeping its file (used for the
    pinned taskbar copy: renaming that file would undo the pin)."""
    def q(value: str) -> str:
        return value.replace("'", "''")

    lines = [
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{q(str(lnk))}')",
        f"$s.TargetPath = '{q(plan.target)}'",
        f"$s.WorkingDirectory = '{q(plan.working_dir)}'",
    ]
    if not keep_arguments:
        lines.append(f"$s.Arguments = '{q(plan.arguments)}'")
    if icon is not None:
        lines.append(f"$s.IconLocation = '{q(str(icon))},0'")
    lines.append("$s.Save()")
    lines.append("Write-Output $s.FullName")
    return "\n".join(lines)


def shortcut_targets_script(paths: list[Path]) -> str:
    """PowerShell that prints "path|target" for each shortcut (to find ones pointing somewhere wrong)."""
    def q(value: str) -> str:
        return value.replace("'", "''")

    lines = ["$sh = New-Object -ComObject WScript.Shell"]
    for path in paths:
        lines.append(f"$s = $sh.CreateShortcut('{q(str(path))}'); Write-Output ('{q(str(path))}|' + $s.TargetPath)")
    return "\n".join(lines)
