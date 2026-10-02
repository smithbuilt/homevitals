"""Password updates, re-auth, and the scheduled-sync agent lifecycle."""
from __future__ import annotations

import getpass
import shutil
import sys
from pathlib import Path

import yaml

from homevitals import install, platform_support
from homevitals.cli import shared
from homevitals.prompt import PROMPT_TIMEOUT_SECONDS, input_with_timeout


def _pick_user(config: dict, user_name: str | None, command: str) -> dict:
    """The raw user a credential command applies to.

    One user: that user. A name given: the match (or exit 1). Several users and
    no name: ask at a terminal, otherwise exit 1 pointing at the window, so a
    command can never quietly change the wrong person's login.
    """
    users = config.get("users") or []
    if user_name is not None:
        wanted = user_name.strip().lower()
        for u in users:
            if str(u.get("name", "")).strip().lower() == wanted:
                return u
        print(f"No person named '{user_name}' in the config.")
        sys.exit(1)
    if len(users) == 1:
        return users[0]
    if not sys.stdin.isatty():
        print("This install has more than one person. Use the HomeVitals window, "
              f"or run: homevitals {command} --user NAME")
        sys.exit(1)
    for i, u in enumerate(users, 1):
        print(f"  {i}. {u.get('name', 'default')}")
    while True:
        answer = input_with_timeout(f"Which person? [1-{len(users)}] ", PROMPT_TIMEOUT_SECONDS)
        if answer is None:
            print("")
            print("No answer after 5 minutes; nothing changed.")
            sys.exit(1)
        if answer.strip().isdigit() and 1 <= int(answer.strip()) <= len(users):
            return users[int(answer.strip()) - 1]
        print(f"Type a number from 1 to {len(users)}.")


def _same_email(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.strip().lower() == b.strip().lower()


def _store_password_for_email(raw_users: list[dict], service: str, email: str, password: str) -> list[str]:
    """Store the password for every person whose {service}.email matches.

    The household's adults share one Eufy login, so a changed Eufy password
    must reach all of them. Returns the names updated.
    """
    from homevitals.credentials import store_password

    updated = []
    for u in raw_users:
        if _same_email((u.get(service) or {}).get("email"), email):
            name = u.get("name", "default")
            store_password(f"{name}:{service}", password)
            updated.append(name)
    return updated


_LEGACY_TOKEN_FILES = {"eufy": "eufy_token.json", "garmin": "session.json"}


def _clear_tokens_for_user(raw_user: dict, services: tuple[str, ...] = ("eufy", "garmin", "omron")) -> None:
    """Delete this person's keyed tokens, plus any old unkeyed name and file, for each service.

    OMRON connect never had unkeyed tokens, so only the keyed one goes.
    """
    from homevitals.credentials import account_token_name, delete_token

    for service in services:
        email = (raw_user.get(service) or {}).get("email")
        if not email:
            continue
        delete_token(account_token_name(service, email))
        if service not in _LEGACY_TOKEN_FILES:
            continue
        delete_token(service)
        legacy_file = shared.DATA_DIR / _LEGACY_TOKEN_FILES[service]
        if legacy_file.exists():
            legacy_file.unlink()


def _update_password(config_path: Path, user_name: str | None = None) -> None:
    """Update stored passwords."""
    if not config_path.exists():
        print("No config found. Run homevitals first to set up.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    user = _pick_user(config, user_name, "--update-password")
    user_name = user.get("name", "default")
    if not any(k in user for k in ("eufy", "garmin", "zwift", "omron")):
        print(f"{user_name} has no accounts connected yet. Connect them in the HomeVitals window.")
        return

    print("Press Enter to keep current password.")
    print("")

    eufy_pw = getpass.getpass("New Eufy password: ") if "eufy" in user else ""
    garmin_pw = getpass.getpass("New Garmin password: ") if "garmin" in user else ""
    zwift_pw = getpass.getpass("New Zwift password: ") if "zwift" in user else ""
    omron_pw = getpass.getpass("New OMRON connect password: ") if "omron" in user else ""

    if not eufy_pw and not garmin_pw and not zwift_pw and not omron_pw:
        print("No changes made.")
        return

    from homevitals.credentials import delete_token, store_password

    if eufy_pw:
        # One Eufy login can be shared by several people: update all of them.
        _store_password_for_email(config["users"], "eufy", user["eufy"]["email"], eufy_pw)

    if garmin_pw:
        store_password(f"{user_name}:garmin", garmin_pw)
    if zwift_pw:
        store_password(f"{user_name}:zwift", zwift_pw)
    if omron_pw:
        store_password(f"{user_name}:omron", omron_pw)

    # Clear cached tokens for changed services
    if eufy_pw:
        _clear_tokens_for_user(user, ("eufy",))
    if garmin_pw:
        _clear_tokens_for_user(user, ("garmin",))
    if omron_pw:
        _clear_tokens_for_user(user, ("omron",))
    if zwift_pw:
        delete_token("zwift")

    changed = []
    if eufy_pw:
        changed.append("Eufy")
    if garmin_pw:
        changed.append("Garmin")
    if zwift_pw:
        changed.append("Zwift")
    if omron_pw:
        changed.append("OMRON connect")
    print(f"{' and '.join(changed)} password{'s' if len(changed) > 1 else ''} updated.")
    if omron_pw:
        print("OMRON connect password updated - the next sync logs in with it.")

    if garmin_pw:
        print("Garmin password changed - re-authenticating...")
        _reauth(config_path, config, target="garmin", user_name=user_name)
    if zwift_pw:
        print("Zwift password changed - re-authenticating...")
        _reauth(config_path, config, target="zwift", user_name=user_name)


def _reauth(config_path: Path, config: dict | None = None, force: bool = False, target: str | None = None,
            user_name: str | None = None) -> None:
    """Force re-authentication for a specific target or all targets."""
    if config is None:
        if not config_path.exists():
            print("No config found. Run homevitals first to set up.")
            sys.exit(1)
        with open(config_path) as f:
            config = yaml.safe_load(f)

    user = _pick_user(config, user_name, "--reauth")
    user_name = user.get("name", "default")

    do_garmin = (target is None or target == "garmin") and "garmin" in user
    do_strava = (target is None or target == "strava") and "strava" in user
    do_zwift = (target is None or target == "zwift") and "zwift" in user

    if target and not do_garmin and not do_strava and not do_zwift:
        print(f"Target '{target}' is not configured. Check your config.")
        return

    if do_garmin:
        from homevitals.config import _get_password
        from homevitals.garmin_auth import GarminAuth

        garmin_email = user["garmin"]["email"]
        garmin_pw = _get_password(user_name, "garmin", garmin_email, user["garmin"].get("password"))
        auth = GarminAuth(garmin_email, garmin_pw)

        if force:
            status = auth.token_status()
            if status["state"] == "valid":
                if not sys.stdin.isatty():
                    # Honor the documented default (No) when there's no one
                    # to answer the prompt, instead of silently proceeding
                    # as "yes" and destroying a valid token.
                    print("Garmin re-auth skipped (already connected; run interactively to force).")
                    do_garmin = False
                else:
                    print("Garmin is already connected. Re-authenticate anyway? [y/N] ", end="", flush=True)
                    answer = input_with_timeout("", PROMPT_TIMEOUT_SECONDS)
                    if answer is None:
                        # This prompt is reached from a failure toast, so the
                        # window can sit open with nobody reading it. Waiting
                        # forever once kept the process (and its lock on the
                        # tool venv) alive for hours; the default is No anyway.
                        print("")
                        print("No answer after 5 minutes; keeping the current Garmin login.")
                        do_garmin = False
                    elif not answer.strip().lower().startswith("y"):
                        print("Garmin re-auth skipped.")
                        do_garmin = False

        if do_garmin:
            from homevitals.sync import PermanentSyncError
            try:
                auth.force_reauth()
                print("Done - Garmin tokens saved.")
            except PermanentSyncError as e:
                print(str(e))
                sys.exit(1)

    if do_strava:
        from homevitals.config import StravaConfig, _get_strava_secret
        from homevitals.strava_client import authorize_strava
        strava_cfg = StravaConfig(
            client_id=str(user["strava"]["client_id"]),
            client_secret=_get_strava_secret(user_name, user["strava"].get("client_secret")),
        )
        try:
            authorize_strava(strava_cfg)
        except (RuntimeError, OSError) as e:
            print(str(e))
            print("Retry with: homevitals --reauth strava")
            sys.exit(1)
        print("Done - Strava tokens saved.")

    if do_zwift:
        from homevitals.config import ZwiftConfig, _get_password
        from homevitals.zwift_client import ZwiftClient

        zwift_email = user["zwift"]["email"]
        zwift_cfg = ZwiftConfig(
            email=zwift_email,
            password=_get_password(user_name, "zwift", zwift_email, user["zwift"].get("password")),
        )
        client = ZwiftClient(zwift_cfg)
        try:
            client.authenticate(force=True)
            client.check_connection()
        except Exception as e:
            print(f"Zwift re-authentication failed: {e}")
            print("Retry with: homevitals --reauth zwift")
            sys.exit(1)
        finally:
            client.close()
        print("Done - Zwift token saved.")


def _disconnect_zwift(config_path: Path) -> None:
    """Remove only Zwift configuration and credentials."""
    if not config_path.exists():
        print("No config found. Run homevitals first to set up.")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    user = config["users"][0]
    if "zwift" not in user:
        print("Zwift is not configured.")
        return

    user_name = user.get("name", "default")
    del user["zwift"]
    shared._write_config(config_path, config)

    from homevitals.credentials import delete_password, delete_token
    delete_password(f"{user_name}:zwift")
    delete_token("zwift")
    delete_token("zwift_probe")
    print("Zwift disconnected. Other sync targets are unchanged.")


def _install_launch_agent() -> None:
    """Install the scheduled-sync agent for the current platform."""
    platform_support.install_agent()


def _offer_launch_agent() -> None:
    """Offer to install the scheduled-sync agent after first-run setup."""
    platform_support.offer_agent()


def _uninstall_launch_agent() -> None:
    """Remove the scheduled-sync agent for the current platform."""
    platform_support.uninstall_agent()


def _uninstall(data_dir: Path, config_path: Path | None = None, db_path: Path | None = None) -> None:
    """Remove all homevitals data: Launch Agent, config, tokens, state DB.

    config_path/db_path default to the standard files under data_dir, but a
    custom --config/--db location (outside data_dir) is also deleted so
    --uninstall does not leave those files behind.
    """
    if not sys.stdin.isatty():
        print("Error: --uninstall requires an interactive terminal.")
        sys.exit(1)

    print("This will remove:")
    print(f"  - All saved credentials and tokens in {data_dir}/")
    print("  - Keychain entries for homevitals")
    print("  - Sync history database")
    if platform_support.agent_installed():
        print("  - Automatic sync")
    print("")

    answer = input("Are you sure? [y/N] ").strip()
    if not answer.lower().startswith("y"):
        print("Cancelled.")
        return

    default_config_path = data_dir / "config.yaml"
    default_db_path = data_dir / "state.db"
    config_path = config_path or default_config_path
    db_path = db_path or default_db_path

    # Offer to keep state DB so reinstalls don't duplicate measurements
    keep_db = False
    if db_path.exists():
        print("")
        keep_answer = input("Keep sync history? Prevents duplicates if you reinstall later. [Y/n] ").strip()
        keep_db = not keep_answer.lower().startswith("n")

    # Stop and remove the scheduled-sync agent, where the platform manages one.
    if platform_support.agent_installed():
        platform_support.purge_agent()

    # Clear keychain entries for every user named in the config
    raw_users: list[dict] = [{"name": "default"}]
    if config_path.exists():
        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            users = [u for u in raw.get("users", []) if isinstance(u, dict)]
            if users:
                raw_users = users
        except Exception:
            pass

    # Clear the keychain vault. On a file-backend machine this gate skips the
    # deletes, which is safe only because credentials.json lives inside data_dir
    # and is erased by the rmtree below; keep them together if CRED_FILE ever
    # moves outside ~/.homevitals.
    from homevitals.credentials import _keyring_available, account_token_name, delete_password, delete_token
    if _keyring_available():
        # Best-effort: a locked keychain makes the vault read raise, and a
        # half-finished uninstall that leaves the data dir behind (the rmtree
        # is below) plus a raw traceback is worse than skipping this. The
        # rmtree still erases a file-backed vault under data_dir.
        try:
            for user in raw_users:
                name = user.get("name", "default")
                # "strava" here is the API app's client secret, not an account
                # password; it moved into the vault alongside the other two.
                # "omron" is reserved for the blood pressure sync.
                for suffix in ["eufy", "garmin", "strava", "zwift", "omron"]:
                    delete_password(f"{name}:{suffix}")
                for service in ("garmin", "eufy", "omron"):
                    email = (user.get(service) or {}).get("email")
                    if email:
                        delete_token(account_token_name(service, email))
            delete_token("eufy")
            delete_token("garmin")
            delete_token("strava")
            delete_token("zwift")
            delete_token("zwift_probe")
        except Exception:
            print("Note: could not clear keychain entries (the keychain may be locked).")

    # Remove data directory. A kept DB at a custom --db path lives outside
    # data_dir, so only the default location needs the selective sweep.
    preserve_default_db = keep_db and db_path == default_db_path and db_path.exists()
    if data_dir.exists():
        if not preserve_default_db:
            shutil.rmtree(data_dir)
        else:
            for item in data_dir.iterdir():
                if item.name == "state.db":
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

    # A custom --config/--db path lives outside data_dir, so it survives the
    # rmtree above and must be removed explicitly.
    if config_path != default_config_path and config_path.exists():
        config_path.unlink()
    if db_path != default_db_path and not keep_db and db_path.exists():
        db_path.unlink()

    print("")
    if keep_db:
        print(f"Removed all homevitals data (sync history kept in {db_path}).")
    else:
        print("Removed all homevitals data.")

    print(f"To remove the package itself, run: {install.uninstall_command()}")
