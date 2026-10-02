"""The homevitals command line entry point and sync driver."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from time import sleep

from homevitals import platform_support
from homevitals.cli import doctor, maintenance, profiles, setup, shared, status, updater
from homevitals.config import bp_ready, missing_for_sync, nothing_connected, scale_ready
from homevitals.reporting import SyncReport, blood_pressure_note, garmin_existing_note, update_counts_summary

logger = logging.getLogger("homevitals")
NETWORK_RETRY_DELAY = 60
_REAUTH_TARGETS = frozenset(("garmin", "strava", "zwift"))


def _target_label(total_counts: dict[str, int]) -> str:
    """Human label for the targets that received data, e.g. "Garmin and Strava"."""
    names = [n.capitalize() for n in total_counts if total_counts[n] > 0]
    if len(names) < 3:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _tally_run(user, counts: dict[str, int], errors: dict[str, str], total_counts: dict[str, int], failures: list) -> None:
    """Fold one sync_user result into the run totals."""
    for target_name, count in counts.items():
        total_counts[target_name] = total_counts.get(target_name, 0) + count
    for target_name, err in errors.items():
        failures.append((f"{user.name}/{target_name}", err))
    logger.info("User %s: synced %s", user.name, counts)


def _run_blood_pressure(user, state, args, backfill: int | None, report: SyncReport, failures: list):
    """Run one person's Omron -> Garmin step. Never raises: a failure is recorded and reported."""
    if not bp_ready(user) or args.target not in (None, "garmin"):
        return None
    from homevitals import bp_sync
    try:
        result = bp_sync.sync_blood_pressure(user, state, headless=args.headless, dry_run=args.dry_run,
                                             backfill_days=backfill)
    except Exception as e:
        text = bp_sync.safe_error_text(e)
        logger.error("Blood pressure sync failed for %s: %s", user.name, text)
        logger.debug("Blood pressure traceback", exc_info=True)
        failures.append((f"{user.name}/garmin_bp", text))
        return bp_sync.BpSyncResult(error=text)
    if result is None:
        return None
    if not args.dry_run:
        report.bp_uploaded += result.uploaded
    if result.error:
        failures.append((f"{user.name}/garmin_bp", result.error))
    return result


def _known_failure_targets(failures: list, marker: str) -> set[str]:
    """Return allowlisted target names from structured per-target failures."""
    targets = set()
    for name, error in failures:
        if marker not in error or "/" not in name:
            continue
        target = name.rsplit("/", 1)[1]
        if target in _REAUTH_TARGETS:
            targets.add(target)
    return targets


def _reauth_repair_command(failures: list) -> str:
    marked_failures = [(name, error) for name, error in failures if "--reauth" in error]
    targets = _known_failure_targets(failures, "--reauth")
    if len(marked_failures) == 1 and len(targets) == 1:
        return f"homevitals --reauth {next(iter(targets))}"
    return "homevitals --reauth"


def _password_failure_title(failures: list) -> str:
    marked_failures = [
        (name, error) for name, error in failures if "--update-password" in error
    ]
    targets = _known_failure_targets(failures, "--update-password")
    if len(marked_failures) == 1 and len(targets) == 1:
        return f"{shared.APP_NAME}: {next(iter(targets)).capitalize()} login failed"
    if any("changed your Eufy password" in error for _, error in failures):
        return f"{shared.APP_NAME}: Eufy login failed"
    return f"{shared.APP_NAME}: login failed"


def _sync_with_network_retry(user, state, **kwargs):
    """Give scheduled network failures one delayed retry, keeping partial counts."""
    from homevitals.network import is_transient_network_error
    from homevitals.sync import sync_user

    can_retry = kwargs.get("headless", False) and not kwargs.get("dry_run", False)
    totals: dict[str, int] = {}
    for attempt in range(2):
        try:
            counts, errors = sync_user(user, state, **kwargs)
        except Exception as e:
            if not (can_retry and attempt == 0 and is_transient_network_error(str(e))):
                if any(totals.values()):
                    return totals, {"sync": str(e)}
                raise
        else:
            for target, count in counts.items():
                totals[target] = totals.get(target, 0) + count
            if not (
                can_retry and attempt == 0 and errors
                and all(is_transient_network_error(err) for err in errors.values())
            ):
                return totals, errors
        logger.warning("Network unavailable; retrying scheduled sync once in %ds", NETWORK_RETRY_DELAY)
        sleep(NETWORK_RETRY_DELAY)


def main() -> None:
    try:
        _main()
    except KeyboardInterrupt:
        # Ctrl+C at any prompt (MFA code, passwords, confirmations) should
        # read as a cancel, not dump a traceback.
        print("\nCancelled.")
        sys.exit(130)


def _main() -> None:
    import argparse

    # Before anything reads the settings or creates the data folder. --version and --help
    # only print: they never touch the data folder or the saved passwords.
    if not {"--version", "-V", "--help", "-h"} & set(sys.argv[1:]):
        from homevitals import migrate
        migrate.move_from_old_names()

    from homevitals import __version__

    parser = argparse.ArgumentParser(
        prog="homevitals",
        description="Sync Eufy smart scale data to Garmin Connect, Strava, and Zwift",
    )
    parser.add_argument("--version", "-V", action="version", version=f"homevitals {__version__}")
    parser.add_argument("--status", action="store_true", help="Show sync status and token health")
    parser.add_argument("--doctor", action="store_true", help="Check the whole setup and print fixes for anything wrong")
    parser.add_argument("--reauth", nargs="?", const="all", default=None, metavar="TARGET",
                        choices=("all", "garmin", "strava", "zwift"),
                        help="Re-authenticate (optionally: garmin, strava, or zwift)")
    parser.add_argument("--setup-strava", action="store_true", help="Connect Strava to your account")
    parser.add_argument("--setup-zwift", action="store_true", help="Connect experimental Zwift weight sync")
    parser.add_argument("--disconnect-zwift", action="store_true", help="Disconnect Zwift weight sync")
    parser.add_argument("--target", choices=("garmin", "strava", "zwift"), default=None,
                        help="Sync only one configured target")
    parser.add_argument("--select-profile", action="store_true", help="Choose which Eufy profile to sync")
    parser.add_argument("--update-password", action="store_true", help="Change stored passwords")
    parser.add_argument("--user", default=None, metavar="NAME",
                        help="Which person --update-password or --reauth applies to")
    parser.add_argument("--update", action="store_true", help="Update homevitals to the latest version")
    parser.add_argument("--history", nargs="?", const=14, type=int, default=None, metavar="N",
                        help="Show recent sync history, last N entries (default: 14)")
    parser.add_argument("--backfill-days", type=int, default=None, help="Sync last N days")
    parser.add_argument("--bp-backfill-days", type=int, default=None, metavar="N",
                        help="Blood pressure only: look back N days in OMRON connect")
    parser.add_argument("--repair-days", type=int, default=None,
                        help="Re-sync the last N days even if records are already marked as synced")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--headless", action="store_true", help="Never prompt; log back in on its own if the session died (for scheduled runs)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed sync logs")
    parser.add_argument("--install-agent", action="store_true", help="Set up automatic sync every 4 hours")
    parser.add_argument("--uninstall-agent", action="store_true", help="Remove automatic sync")
    parser.add_argument("--uninstall", action="store_true", help="Remove all data, tokens, and automatic sync")
    parser.add_argument("--use-file-store", action="store_true", help="Store credentials in a 0o600 file instead of the keychain (no keychain prompts)")
    parser.add_argument("--use-keychain", action="store_true", help="Move credentials back into the system keychain")
    parser.add_argument("--config", type=Path, default=None, help="Config path (default: ~/.homevitals/config.yaml)")
    parser.add_argument("--db", type=Path, default=None, help="Database path (default: ~/.homevitals/state.db)")
    args = parser.parse_args()

    # Both set the fetch window, from opposite intents (fill in what is
    # missing vs. re-send what is there), so honoring one silently would be a
    # coin flip on which the user meant.
    if args.repair_days is not None and args.backfill_days is not None:
        parser.error("--repair-days and --backfill-days cannot be used together")

    config_path = args.config or shared.DEFAULT_CONFIG
    db_path = args.db or shared.DEFAULT_DB

    # Configure logging before any command dispatch. Every path that can reach
    # a Garmin login (setup, --update-password, --reauth, sync) needs the
    # garminconnect logger quieted, or its per-strategy 429 warnings print
    # through logging's last-resort handler and look like errors.
    shared._configure_logging(args.verbose)

    # Handle doctor - must report even on a fresh install, never launch the
    # first-run wizard, and never traceback.
    if args.doctor:
        from homevitals.cli import lock
        # Live checks can refresh tokens, so they share the sync lock.
        with lock.single_instance(require_lock=True) as acquired:
            if not acquired:
                print("Another sync or diagnostic is running. Retry --doctor when it finishes.")
                sys.exit(1)
            sys.exit(doctor._run_doctor(config_path, db_path))

    # Handle full uninstall
    if args.uninstall:
        maintenance._uninstall(shared.DATA_DIR, config_path=config_path, db_path=db_path)
        return

    # Handle credential store mode switches
    if args.use_file_store:
        from homevitals import credentials
        try:
            credentials.use_file_store()
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)
        print("Credentials moved to a 0o600 file (~/.homevitals/credentials.json). No more keychain prompts.")
        sys.exit(0)

    if args.use_keychain:
        from homevitals import credentials
        try:
            credentials.use_keychain_store()
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)
        print("Credentials moved into the system keychain.")
        sys.exit(0)

    # Handle Launch Agent install/uninstall. Installing needs a config first:
    # a scheduled agent on an unconfigured machine would just fail (and
    # notify) every few hours.
    if args.install_agent:
        if not config_path.exists():
            print("No config found. Run homevitals first to set up.")
            sys.exit(1)
        maintenance._install_launch_agent()
        return

    if args.uninstall_agent:
        maintenance._uninstall_launch_agent()
        return

    # Handle self-update
    if args.update:
        updater._self_update()
        return

    # Handle Strava setup
    if args.setup_strava:
        setup._setup_strava(config_path)
        return

    if args.setup_zwift:
        from homevitals.cli import lock
        with lock.single_instance(require_lock=True) as acquired:
            if not acquired:
                print("Another homevitals run is in progress. Retry --setup-zwift when it finishes.")
                sys.exit(1)
            setup._setup_zwift(config_path)
        return

    if args.disconnect_zwift:
        from homevitals.cli import lock
        with lock.single_instance(require_lock=True) as acquired:
            if not acquired:
                print("Another homevitals run is in progress. Retry --disconnect-zwift when it finishes.")
                sys.exit(1)
            maintenance._disconnect_zwift(config_path)
        return

    # Handle profile selection
    if args.select_profile:
        profiles._select_profile(config_path)
        return

    # Handle password update
    if args.update_password:
        from homevitals.cli import lock
        with lock.single_instance(require_lock=True) as acquired:
            if not acquired:
                print("Another homevitals run is in progress. Retry --update-password when it finishes.")
                sys.exit(1)
            maintenance._update_password(config_path, user_name=args.user)
        return

    # Handle reauth
    if args.reauth is not None:
        target = None if args.reauth == "all" else args.reauth
        from homevitals.cli import lock
        with lock.single_instance(require_lock=True) as acquired:
            if not acquired:
                print("Another homevitals run is in progress. Retry --reauth when it finishes.")
                sys.exit(1)
            maintenance._reauth(config_path, force=True, target=target, user_name=args.user)
        return

    # --status/--history are read-only inspection commands - on a fresh
    # install they must refuse cleanly rather than dropping the user into
    # the interactive setup wizard (which prints "Running first sync ..."
    # that a --status/--history invocation never actually runs).
    if (args.status or args.history is not None) and not config_path.exists():
        print("No config found. Run homevitals first to set up.")
        sys.exit(1)

    # First-run setup if no config exists
    first_run = not config_path.exists()
    if first_run and args.headless:
        msg = "No config found. Run homevitals in a terminal to set up."
        print(msg)
        platform_support.notify(shared.APP_NAME, msg)
        sys.exit(1)

    try:
        if first_run:
            setup._first_run_setup(config_path)
        else:
            # Migrate existing plaintext passwords to keychain (one-time)
            setup._migrate_config_passwords(config_path)
            # One-time upgrade notice for users coming from eufy-garmin-sync
            setup._show_upgrade_notice()

        # Load config (passwords resolved from keychain or YAML fallback)
        from homevitals.config import load_config
        config = load_config(config_path)
        # Old one-person installs kept unkeyed login tokens; move them once.
        # With two or more people this does nothing (never guess whose they were).
        from homevitals.credentials import migrate_legacy_tokens
        migrate_legacy_tokens(config.users)
    except SystemExit:
        raise
    except Exception as e:
        msg = f"homevitals could not start: {e}"
        print(msg)
        platform_support.notify(f"{shared.APP_NAME}: sync failed", str(e)[:200])
        sys.exit(1)

    has_garmin = any(u.garmin for u in config.users)

    if args.target and not any(getattr(u, args.target, None) for u in config.users):
        print(f"Target '{args.target}' is not configured. Check your config.")
        sys.exit(1)

    # Handle status
    if args.status:
        from homevitals.state import SyncState
        try:
            state = SyncState(db_path)
        except Exception as e:
            print(f"Could not read sync state: {e}")
            sys.exit(1)
        status._show_status(state, config.users)
        state.close()
        return

    # Handle history
    if args.history is not None:
        from homevitals.state import SyncState
        try:
            state = SyncState(db_path)
        except Exception as e:
            print(f"Could not read sync state: {e}")
            sys.exit(1)
        status._show_history(state, config.users, limit=args.history)
        state.close()
        return

    # Run sync
    from homevitals.cli import lock
    from homevitals.eufy_client import AmbiguousProfileError
    from homevitals.state import SyncState

    # Sync and live diagnostics share the lock because both refresh tokens.
    # A manual
    # run landing on top of the 4-hourly scheduled one would upload the same
    # measurement twice, and both runs refreshing the Strava token can leave
    # the loser saving a token the rotation already killed. An overlap is a
    # normal event, not a failure, so the skipped run exits 0 - a non-zero
    # exit here would fire the failure toast every time the two met.
    with lock.single_instance() as acquired:
        if not acquired:
            print("Another homevitals run is in progress; skipping.")
            return

        updater._check_for_updates()

        backfill = args.backfill_days
        if first_run and backfill is None:
            backfill = 7

        try:
            state = SyncState(db_path)
        except Exception as e:
            msg = f"homevitals could not start: {e}"
            print(msg)
            platform_support.notify(f"{shared.APP_NAME}: sync failed", str(e)[:200])
            sys.exit(1)

        try:
            total_counts: dict[str, int] = {}
            report = SyncReport(multiple_users=len(config.users) > 1)
            failures = []
            not_ready: list = []
            for user in config.users:
                # The scale step and the blood pressure step each have their own
                # try/except: a failure in one never stops the other.
                counts: dict[str, int] = {}
                errors: dict[str, str] = {}
                scale_failed: str | None = None
                run_scale = scale_ready(user)
                if run_scale:
                    status._progress(user.name, "scale", "start")
                    try:
                        counts, errors = _sync_with_network_retry(user, state, backfill_days=backfill, repair_days=args.repair_days, headless=args.headless, dry_run=args.dry_run, target=args.target, report=report)
                        _tally_run(user, counts, errors, total_counts, failures)
                    except AmbiguousProfileError as e:
                        interactive = not args.headless and sys.stdin.isatty()
                        if interactive:
                            # _prompt_profile_choice prints the list and asks for a pick.
                            customer_id = profiles._prompt_profile_choice(e.profiles)
                            profiles._save_customer_id(config_path, customer_id, user.name)
                            user.eufy.customer_id = customer_id
                            print("Saved. Syncing your profile now...")
                            try:
                                counts, errors = _sync_with_network_retry(user, state, backfill_days=backfill, repair_days=args.repair_days, headless=args.headless, dry_run=args.dry_run, target=args.target, report=report)
                                _tally_run(user, counts, errors, total_counts, failures)
                            except Exception as retry_error:
                                logger.exception("Failed to sync user %s after profile selection", user.name)
                                failures.append((user.name, str(retry_error)))
                                scale_failed = str(retry_error)
                        else:
                            print("")
                            print("Multiple profiles were found on this Eufy account:")
                            for i, p in enumerate(e.profiles, 1):
                                print(profiles._format_profile(p, i))
                            print("")
                            print(f"No data synced for {user.name}. Choose a profile with: homevitals --select-profile")
                            failures.append((user.name, "multiple Eufy profiles; run homevitals --select-profile"))
                            scale_failed = "multiple Eufy profiles; run homevitals --select-profile"
                    except Exception as e:
                        logger.exception("Failed to sync user %s", user.name)
                        failures.append((user.name, str(e)))
                        scale_failed = str(e)

                if run_scale:
                    if scale_failed is not None or errors:
                        status._progress(user.name, "scale", "failed")
                    else:
                        status._progress(user.name, "scale", "done", sum(counts.values()))
                bp_started = bp_ready(user) and args.target in (None, "garmin")
                if bp_started:
                    status._progress(user.name, "bp", "start")
                bp = _run_blood_pressure(user, state, args, args.bp_backfill_days or backfill, report, failures)
                if bp_started:
                    if bp is not None and bp.error:
                        status._progress(user.name, "bp", "failed")
                    else:
                        status._progress(user.name, "bp", "done", bp.uploaded if bp is not None else 0)
                # Say how each person did (the window shows these lines). A single
                # person without a monitor keeps upstream's output exactly.
                if not run_scale and not bp_ready(user):
                    # Partly set up: nothing to sync yet. Informational, never a failure.
                    print(status._user_not_ready_line(user.name, missing_for_sync(user), nothing_connected(user)))
                    not_ready.append(user)
                elif args.dry_run:
                    if bp is not None:
                        n = bp.uploaded
                        print(f"[DRY RUN] Would upload {n} blood pressure reading{'s' if n != 1 else ''} "
                              f"for {user.name}.")
                elif len(config.users) > 1 or user.omron is not None:
                    if scale_failed is not None:
                        print(status._user_failure_line(user.name, scale_failed))
                    else:
                        print(status._user_summary_line(user.name, counts, errors,
                                                        bp_uploaded=bp.uploaded if bp else 0))
                    for line in status._bp_detail_lines(user.name, bp):
                        print(line)

            total = sum(total_counts.values())

            if args.dry_run:
                if total > 0:
                    print(f"[DRY RUN] {update_counts_summary(total_counts, planned=True)}")
                elif failures:
                    print("[DRY RUN] No syncs planned.")
                else:
                    print("[DRY RUN] Would sync 0 measurements. Nothing new to sync.")
                if failures:
                    fail_names = ", ".join(name for name, _ in failures)
                    print(f"Could not check: {fail_names}. Run with --verbose for details.")
                sys.exit(1 if failures else 0)

            if failures:
                from homevitals.cli import failure_notify
                reauth_needed = any("--reauth" in err for _, err in failures)
                password_needed = any(
                    "--update-password" in err or "changed your Eufy password" in err
                    for _, err in failures
                )
                multiple_profiles = any("multiple Eufy profiles" in err for _, err in failures)
                all_transient = all(failure_notify.is_transient_network_error(err) for _, err in failures)
                completed = ""
                if total > 0:
                    completed = f" Synced to {_target_label(total_counts)}."
                completed += garmin_existing_note(report)
                if reauth_needed:
                    command = _reauth_repair_command(failures)
                    platform_support.notify(
                        f"{shared.APP_NAME}: re-login needed",
                        (completed.strip() + " " if completed else "") + f"Run: {command}",
                        command=command,
                    )
                    failure_notify.clear_network_failures()
                elif password_needed:
                    platform_support.notify(
                        _password_failure_title(failures),
                        (completed.strip() + " " if completed else "")
                        + "Run: homevitals --update-password",
                        command="homevitals --update-password",
                    )
                    failure_notify.clear_network_failures()
                elif multiple_profiles:
                    platform_support.notify(
                        f"{shared.APP_NAME}: choose your profile",
                        (completed.strip() + " " if completed else "")
                        + "Run: homevitals --select-profile",
                        command="homevitals --select-profile",
                    )
                    failure_notify.clear_network_failures()
                elif all_transient and args.headless and not args.dry_run:
                    # A scheduled run that only hit network trouble. Stay quiet - the
                    # next run retries - unless several have failed in a row, which
                    # points at a real outage worth one heads-up.
                    count, hours = failure_notify.record_network_failure()
                    if failure_notify.should_escalate(count):
                        platform_support.notify(
                            f"{shared.APP_NAME}: network still down",
                            f"Sync has hit network errors for ~{round(hours)}h ({count} runs). "
                            f"Pending measurements will retry automatically.{completed}",
                        )
                else:
                    fail_msg = "; ".join(f"{name}: {err[:80]}" for name, err in failures)
                    platform_support.notify(f"{shared.APP_NAME}: sync failed", fail_msg + completed)
                    failure_notify.clear_network_failures()
                logger.error("Sync failed for: %s", "; ".join(f"{n}: {e[:80]}" for n, e in failures))
            elif not args.dry_run:
                # A clean run means the network is back; let a future outage
                # escalate from scratch.
                from homevitals.cli import failure_notify
                failure_notify.clear_network_failures()

            if total > 0 and not failures:
                target_label = _target_label(total_counts)
                platform_support.notify(
                    shared.APP_NAME,
                    f"Synced to {target_label}." + blood_pressure_note(report) + garmin_existing_note(report),
                )
            elif report.bp_uploaded > 0 and not failures:
                n = report.bp_uploaded
                platform_support.notify(shared.APP_NAME, f"Synced {n} blood pressure reading{'s' if n != 1 else ''} to Garmin.")

            if first_run:
                if failures:
                    if total > 0:
                        print("")
                        print(update_counts_summary(total_counts) + garmin_existing_note(report))
                    elif garmin_existing_note(report):
                        print("")
                        print(garmin_existing_note(report).strip())
                    print("")
                    print("First sync failed. Fix the issue above, then run homevitals again.")
                else:
                    if total > 0:
                        print("")
                        print(update_counts_summary(total_counts) + garmin_existing_note(report))
                    elif garmin_existing_note(report):
                        print("")
                        print(garmin_existing_note(report).strip())
                    maintenance._offer_launch_agent()
                    print("")
                    apps = []
                    if has_garmin:
                        apps.append("Garmin Connect")
                    if any(u.strava for u in config.users):
                        apps.append("Strava")
                    if any(u.zwift for u in config.users):
                        apps.append("Zwift")
                    print(f"You're all set! Check the {' and '.join(apps)} app to see your data.")
            elif not args.verbose:
                ready_users = [u for u in config.users if u not in not_ready]
                status._print_summary(total_counts, failures, state, ready_users, report)

            sys.exit(1 if failures else 0)

        finally:
            state.close()


if __name__ == "__main__":
    main()
