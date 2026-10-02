"""homevitals --doctor: one command that diagnoses the whole setup and prints
the exact fix for anything wrong.

Every check is wrapped so nothing here can raise past _run_doctor - a
diagnostic tool that crashes is worse than useless. Checks 4-10 run even
when earlier ones warn; checks 2-10 are skipped (with a single explanatory
line) when check 1 (config) fails, since nothing else can load without it.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homevitals import credentials, platform_support
from homevitals.cli import updater
from homevitals.config import load_config
from homevitals.credentials import active_store_label
from homevitals.eufy_client import EufyClient
from homevitals.garmin_client import GarminClient
from homevitals.omron_client import OmronClient
from homevitals.state import SyncState
from homevitals.strava_client import StravaClient
from homevitals.zwift_client import ZwiftClient

_LABEL_WIDTH = 14


def _line(status: str, label: str, detail: str, fix: str | None = None, width: int = _LABEL_WIDTH) -> str:
    text = f"{status:<5} {label:<{width}} {detail}"
    if fix:
        text += f"   fix: {fix}"
    return text


def _labeller(name: str, multi: bool) -> Callable[[str], str]:
    """With more than one person, prefix each per-person label with their name."""
    if multi:
        return lambda label: f"{name}: {label}"
    return lambda label: label


def _run_doctor(config_path: Path, db_path: Path) -> int:
    # Rows are collected first and printed at the end, so labels with a
    # person's name in front still line up.
    rows: list[tuple[str, str, str, str | None] | str] = []
    fail_count = 0
    warn_count = 0

    def report(status: str, label: str, detail: str, fix: str | None = None) -> None:
        nonlocal fail_count, warn_count
        rows.append((status, label, detail, fix))
        if status == "FAIL":
            fail_count += 1
        elif status == "WARN":
            warn_count += 1

    def flush() -> None:
        width = max([_LABEL_WIDTH] + [len(r[1]) for r in rows if isinstance(r, tuple)])
        print("\n".join(r if isinstance(r, str) else _line(*r, width=width) for r in rows))

    # 1. config
    if not config_path.exists():
        report("FAIL", "config", "no config", "homevitals")
        rows.append("skip  remaining checks - no config to load")
        flush()
        print("")
        print(f"{fail_count} problem(s) found.")
        return 1

    try:
        config = load_config(config_path)
    except Exception as e:
        report("FAIL", "config", str(e))
        rows.append("skip  remaining checks - config failed to load")
        flush()
        print("")
        print(f"{fail_count} problem(s) found.")
        return 1

    n = len(config.users)
    targets = sorted({t for u in config.users for t in ("garmin", "strava", "zwift") if getattr(u, t)})
    report("PASS", "config", f"valid ({n} user{'s' if n != 1 else ''}, targets: {', '.join(targets) or 'none'})")
    try:
        # Old one-person installs kept unkeyed login tokens; with two or more
        # people this does nothing.
        credentials.migrate_legacy_tokens(config.users)
    except Exception as e:
        report("WARN", "config", f"could not move old login tokens ({type(e).__name__})")

    # Shared: keychain
    _check_keychain(report)

    # Per person: profile, eufy token, garmin/strava/zwift, eufy cloud, state db
    multi = n > 1
    for user in config.users:
        label = _labeller(user.name, multi)
        _check_setup(report, user, label)
        eufy_client = None
        if user.eufy:
            _check_profile(report, user, label)
            eufy_client = _check_eufy_token(report, user, label)
        if user.garmin:
            _check_garmin_session(report, user, label)
        if user.omron:
            _check_omron(report, user, label, multi)
        if user.strava:
            _check_strava_token(report, user, label)
        if user.zwift:
            _check_zwift_session(report, user, label)
        _check_eufy_cloud(report, eufy_client, label)
        _check_state_db(report, db_path, user, label)

    # Shared: scheduled sync agent (where the platform manages one), version
    agent = platform_support.agent_status()
    if agent is not None:
        report(agent["status"], agent["label"], agent["detail"], agent["fix"])
    _check_version(report)

    flush()
    print("")
    if fail_count == 0 and warn_count == 0:
        print("All checks passed.")
        return 0
    if fail_count == 0:
        # Warnings alone exit 0; saying "problems found" here would contradict
        # the exit code and read as a failure.
        print(f"{warn_count} warning(s), nothing blocking.")
        return 0
    print(f"{fail_count} problem(s) found.")
    return 1


def _same(label: str) -> str:
    return label


def _check_profile(report, user, label: Callable[[str], str] = _same) -> None:
    try:
        if user.eufy.customer_id:
            short = user.eufy.customer_id[-4:]
            report("PASS", label("profile"), f"selected (...{short})")
        else:
            report(
                "WARN", label("profile"),
                "not set (fine for single-profile accounts)",
                "homevitals --select-profile",
            )
    except Exception as e:
        report("FAIL", label("profile"), str(e))


def _check_keychain(report) -> None:
    try:
        # A credentials file next to an active keychain is a stray unmarked
        # file being ignored: it holds a stale copy of secrets that nothing
        # reads or updates, so surface it instead of staying silent.
        if credentials._active_backend() == "keychain" and credentials.CRED_FILE.exists():
            report(
                "WARN", "keychain",
                "keychain active; unused credentials file at ~/.homevitals/credentials.json",
                "homevitals --use-file-store (adopt it) or delete the file",
            )
            return
        report("PASS", "keychain", active_store_label())
    except Exception as e:
        report("PASS", "keychain", f"file store (no keychain prompts) ({e})")


def _check_eufy_token(report, user, label: Callable[[str], str] = _same):
    eufy_client = EufyClient(user.eufy)
    try:
        status = eufy_client.token_status()
        state = status.get("state")
        if state == "valid":
            report("PASS", label("eufy token"), f"valid, {status['days_remaining']}d remaining")
        elif state == "expired":
            report("WARN", label("eufy token"), "expired (next sync re-logs-in with the stored password)")
        else:
            report("WARN", label("eufy token"), "no token (next sync re-logs-in with the stored password)")
    except Exception as e:
        report("FAIL", label("eufy token"), str(e))
    return eufy_client


def _check_garmin_session(report, user, label: Callable[[str], str] = _same) -> None:
    client = None
    try:
        client = GarminClient(user.garmin)
        client.authenticate(allow_interactive=False)
        client.check_connection()
        report("PASS", label("garmin session"), "connected (live check)")
    except Exception as e:
        _report_connection_error(report, label("garmin session"), e)
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


def _check_setup(report, user, label: Callable[[str], str] = _same) -> None:
    """A WARN line for someone who isn't set up enough to sync anything yet. The window is the fix."""
    from homevitals.config import missing_for_sync, nothing_connected

    missing = missing_for_sync(user)
    if not missing:
        return
    state = "not set up yet" if nothing_connected(user) else "not fully set up"
    report("WARN", label("setup"), f"{state}: connect {' and '.join(missing)} in the HomeVitals window")


def _check_omron(report, user, label: Callable[[str], str] = _same, multi: bool = False) -> None:
    """Log in to OMRON connect and count the last 30 days of readings. Counts only, never values."""
    from homevitals.bp_sync import safe_error_text

    client = None
    try:
        client = OmronClient(user.omron)
        client.authenticate()
        n = len(client.fetch_readings(datetime.now(timezone.utc) - timedelta(days=30)))
        if n:
            report("PASS", label("omron"), f"connected; {n} reading{'s' if n != 1 else ''} in the last 30 days")
        else:
            report("WARN", label("omron"), "connected; no readings in the last 30 days (open the OMRON connect app "
                                           "on the phone so it picks up readings from the monitor)")
    except Exception as e:
        msg = safe_error_text(e)
        fix = None
        if "--update-password" in msg:
            fix = "homevitals --update-password" + (f" --user {user.name}" if multi else "")
        report("FAIL", label("omron"), msg, fix)
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


def _check_strava_token(report, user, label: Callable[[str], str] = _same) -> None:
    client = None
    try:
        client = StravaClient(user.strava)
        client.authenticate()
        client.check_connection()
        report("PASS", label("strava token"), "connected (live check)")
    except Exception as e:
        _report_connection_error(report, label("strava token"), e)
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


def _check_zwift_session(report, user, label: Callable[[str], str] = _same) -> None:
    client = None
    try:
        client = ZwiftClient(user.zwift)
        client.authenticate()
        client.check_connection()
        report("PASS", label("zwift session"), "connected (live read check)")
    except Exception as e:
        _report_connection_error(report, label("zwift session"), e)
    finally:
        if client is not None:
            with suppress(Exception):
                client.close()


def _report_connection_error(report, label: str, error: Exception) -> None:
    msg = str(error)
    # Network and server failures should not tell the user to reset a login.
    fix = next((f"homevitals {hint}" for hint in (
        "--update-password", "--reauth garmin", "--setup-strava", "--reauth zwift",
    ) if hint in msg), None)
    report("FAIL", label, msg, fix)


def _check_eufy_cloud(report, eufy_client, label: Callable[[str], str] = _same) -> None:
    if eufy_client is None:
        return
    try:
        eufy_client.authenticate()
        window_start = int(time.time()) - 30 * 86400
        measurements = eufy_client.fetch_measurements(after_timestamp=window_start)
        if not measurements:
            report("WARN", label("eufy cloud"), "no weigh-ins in the last 30 days")
            return
        newest = max(m.timestamp for m in measurements)
        now = datetime.now(timezone.utc)
        ts = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
        age = now - ts
        days = age.days
        hours = int(age.total_seconds() / 3600)
        if days >= 2:
            report(
                "WARN", label("eufy cloud"),
                f"last weigh-in {days}d ago; if you weighed since, open the Eufy app",
            )
        elif hours >= 1:
            report("PASS", label("eufy cloud"), f"last weigh-in {hours}h ago")
        else:
            report("PASS", label("eufy cloud"), "last weigh-in just now")
    except Exception as e:
        msg = str(e)
        fix = None
        if "--update-password" in msg:
            fix = "homevitals --update-password"
        report("FAIL", label("eufy cloud"), msg, fix)
    finally:
        try:
            eufy_client.close()
        except Exception:
            pass


def _check_state_db(report, db_path: Path, user, label: Callable[[str], str] = _same) -> None:
    try:
        state = SyncState(db_path)
    except Exception as e:
        report("FAIL", label("state db"), str(e))
        return

    try:
        ts = state.get_latest_sync_timestamp(user.name)
        if ts is None:
            report("PASS", label("state db"), "never synced")
        else:
            last_sync = datetime.fromtimestamp(ts, tz=timezone.utc)
            ago = datetime.now(timezone.utc) - last_sync
            days = ago.days
            hours = int(ago.total_seconds() / 3600)
            if days > 0:
                report("PASS", label("state db"), f"last sync {days}d ago")
            else:
                report("PASS", label("state db"), f"last sync {hours}h ago")
    except Exception as e:
        report("FAIL", label("state db"), str(e))
    finally:
        try:
            state.close()
        except Exception:
            pass


def _check_version(report) -> None:
    try:
        from homevitals import __version__
        if updater._self_update_disabled():
            report("PASS", "version", f"{__version__} (update with: uv tool upgrade homevitals)")
            return
        latest = updater._latest_pypi_version()
        if latest is None:
            report("WARN", "version", "could not check")
            return
        if latest == __version__:
            report("PASS", "version", f"{__version__} (up to date)")
            return
        report(
            "WARN", "version",
            f"{__version__} installed, {latest} available",
            "homevitals --update",
        )
    except Exception as e:
        report("WARN", "version", f"could not check ({e})")
