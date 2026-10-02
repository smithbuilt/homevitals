"""Sync status, history, and the end-of-run summary line."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from homevitals.reporting import SyncReport, blood_pressure_note, garmin_existing_note, update_counts_summary


def _ascii(text: str) -> str:
    """The GUI reads these lines through a pipe; keep them plain ASCII."""
    return text.replace("→", "->").encode("ascii", "replace").decode("ascii")


def _user_summary_line(name: str, counts: dict[str, int], errors: dict[str, str], bp_uploaded: int = 0) -> str:
    """One line per person, printed when more than one person is set up (or someone has a monitor).

    "Chris: synced 2 weigh-ins and 1 blood pressure reading to Garmin." /
    "Chris: nothing new.", plus a "<Target> failed - <error>" clause for each
    target that failed.
    """
    synced = [(target, n) for target, n in counts.items() if n]
    bp = f"{bp_uploaded} blood pressure reading{'s' if bp_uploaded != 1 else ''}" if bp_uploaded else ""
    if len(synced) == 1 and synced[0][0] == "garmin" and bp:
        n = synced[0][1]
        line = f"{name}: synced {n} weigh-in{'s' if n != 1 else ''} and {bp} to Garmin."
    elif synced:
        parts = [f"{n} weigh-in{'s' if n != 1 else ''} to {target.capitalize()}" for target, n in synced]
        if bp:
            parts.append(f"{bp} to Garmin")
        line = f"{name}: synced {' and '.join(parts)}."
    elif bp:
        line = f"{name}: synced {bp} to Garmin."
    else:
        line = f"{name}: nothing new."
    for target, error in errors.items():
        line += f" {target.capitalize()} failed - {error[:160]}"
    return _ascii(line)


# The window sets this so a sync tells it when each person's steps start and finish
# (it draws progress symbols from these lines and never shows them).
PROGRESS_ENV = "EUFY_SYNC_PROGRESS"
PROGRESS_PREFIX = "@progress"


def _progress(name: str, step: str, state: str, count: int | None = None) -> None:
    """One machine line for the window: step "scale" or "bp"; state "start", "done" or "failed".

    Only printed when the window asked for it. It carries how many items were
    sent, never a weight or a reading.
    """
    if os.environ.get(PROGRESS_ENV) != "1":
        return
    fields = [PROGRESS_PREFIX, name.replace("\t", " "), step, state]
    if count is not None:
        fields.append(str(count))
    print("\t".join(fields), flush=True)


def _user_not_ready_line(name: str, missing: list[str], nothing: bool) -> str:
    """The line for someone who isn't set up enough to sync anything yet (not a failure)."""
    what = " and ".join(missing)
    if nothing:
        return _ascii(f"{name}: not set up yet (connect {what}).")
    return _ascii(f"{name}: not fully set up yet (connect {what}).")


def _user_failure_line(name: str, error: str) -> str:
    """The line for a person whose whole sync failed."""
    return _ascii(f"{name}: failed - {error[:160]}")


def _readings(n: int) -> str:
    return f"{n} reading{'s' if n != 1 else ''}"


def _bp_detail_lines(name: str, result) -> list[str]:
    """Extra lines about one person's blood pressure step (counts only, never values)."""
    if result is None:
        return []
    lines = []
    if result.error:
        lines.append(f"{name}: blood pressure failed - {result.error[:160]}")
    if result.skipped_out_of_range:
        lines.append(f"{name}: blood pressure - skipped {_readings(result.skipped_out_of_range)} with values outside "
                     "the range Garmin accepts (nothing was changed or rounded).")
    if result.skipped_in_garmin:
        n = result.skipped_in_garmin
        lines.append(f"{name}: blood pressure - Garmin already had {_readings(n)}, so "
                     f"{'they were' if n != 1 else 'it was'} skipped.")
    if not (result.uploaded or result.skipped_in_garmin or result.skipped_out_of_range or result.error):
        lines.append(f"{name}: blood pressure - no new readings. Open the OMRON connect app on the phone so it "
                     "picks up readings from the monitor, then sync again.")
    return [_ascii(line) for line in lines]


def _zwift_token_status(config) -> dict:
    from homevitals.zwift_client import ZwiftClient
    client = ZwiftClient(config)
    try:
        return client.token_status()
    finally:
        client.close()


def _print_summary(
    total_counts: dict[str, int], failures: list, state, users: list,
    report: SyncReport | None = None,
) -> None:
    """Print a single-line sync summary."""
    if failures:
        if any(total_counts.values()):
            print(update_counts_summary(total_counts) + garmin_existing_note(report))
        elif garmin_existing_note(report):
            print(garmin_existing_note(report).strip())
        fail_names = ", ".join(name for name, _ in failures)
        print(f"Sync failed for: {fail_names}. Run with --verbose for details.")
        return

    if not users:
        return      # nobody was ready to sync; their own lines already said so

    total = sum(total_counts.values())
    if total > 0:
        print(update_counts_summary(total_counts) + blood_pressure_note(report) + garmin_existing_note(report))
        return

    if report is not None and report.bp_uploaded > 0:
        print(f"Syncs completed: blood pressure {report.bp_uploaded}.")
        return

    if garmin_existing_note(report):
        print(garmin_existing_note(report).strip())
        return

    if report is not None and report.multiple_users:
        print(f"No new measurements for {len(users)} profile{'s' if len(users) != 1 else ''}.")
        return

    # No-op sync - build an informative one-liner
    parts = ["No new measurements"]

    user = users[0]
    ts = state.get_latest_sync_timestamp(user.name)
    if ts:
        last_sync = datetime.fromtimestamp(ts, tz=timezone.utc)
        ago = datetime.now(timezone.utc) - last_sync
        days = ago.days
        hours = int(ago.total_seconds() / 3600) % 24
        if days > 0:
            parts.append(f"last sync: {days}d ago")
        else:
            parts.append(f"last sync: {hours}h ago")

    from homevitals.eufy_client import EufyClient
    eufy_status = EufyClient(user.eufy).token_status() if user.eufy is not None else {"state": "none"}
    if eufy_status["state"] == "none":
        pass        # no scale connected for this person
    elif eufy_status["state"] == "expired":
        parts.append("Eufy token EXPIRED (will re-login on next sync)")
    elif eufy_status["state"] == "no_token":
        parts.append("Eufy: no token")
    elif eufy_status["days_remaining"] is not None:
        parts.append(f"Eufy token valid {eufy_status['days_remaining']}d")

    if user.garmin:
        from homevitals.garmin_auth import GarminAuth
        status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
        if status["state"] == "valid":
            parts.append("Garmin connected")
        else:
            parts.append("Garmin not connected")

    if user.strava:
        from homevitals.strava_client import StravaClient
        strava_status = StravaClient(user.strava).token_status()
        if strava_status["state"] == "expired":
            parts.append("Strava token EXPIRED")
        elif strava_status["state"] == "no_session":
            parts.append("Strava: not authorized")
        elif strava_status["state"] == "refresh_needed":
            parts.append("Strava token refresh pending")
        else:
            parts.append("Strava connected")

    if user.zwift:
        zwift_status = _zwift_token_status(user.zwift)
        if zwift_status["state"] in ("valid", "refresh_needed"):
            parts.append("Zwift connected")
        else:
            parts.append("Zwift not connected")

    print(" | ".join(parts))
    if user.eufy is not None:
        print(
            "If you weighed in recently and it isn't here, open the Eufy app so it "
            "uploads to the cloud, then run homevitals again."
        )


def _show_status(state, users: list) -> None:
    """Print detailed sync status for all users."""
    for user in users:
        print(f"\n{'=' * 40}")
        print(f"User: {user.name}")
        print(f"{'=' * 40}")

        # Last sync info
        ts = state.get_latest_sync_timestamp(user.name)
        if ts:
            last_sync = datetime.fromtimestamp(ts, tz=timezone.utc)
            ago = datetime.now(timezone.utc) - last_sync
            hours = int(ago.total_seconds() / 3600)
            print(f"Last synced measurement: {last_sync.strftime('%Y-%m-%d %H:%M UTC')} ({hours}h ago)")
        else:
            print("Last synced measurement: never")

        # Eufy token health
        from homevitals.eufy_client import EufyClient
        if user.eufy is None:
            print("Eufy auth: not configured (no scale connected)")
            eufy_status = {"state": "none"}
        else:
            eufy_status = EufyClient(user.eufy).token_status()
        if eufy_status["state"] == "none":
            pass
        elif eufy_status["state"] == "expired":
            print("Eufy auth: token expired (will re-login automatically on next sync)")
        elif eufy_status["state"] == "no_token":
            print("Eufy auth: no saved token - will login on next sync")
        else:
            print(f"Eufy auth: valid ({eufy_status['days_remaining']} days remaining)")

        # Garmin token health
        if user.garmin:
            from homevitals.garmin_auth import GarminAuth
            status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
            if status["state"] == "valid":
                print("Garmin auth: valid (auto-refreshes; re-login only if it expires)")
            else:
                print("Garmin auth: not connected - run: homevitals --reauth garmin")

        # Strava token health
        if user.strava:
            from homevitals.strava_client import StravaClient
            strava_status = StravaClient(user.strava).token_status()
            if strava_status["state"] == "expired":
                print("Strava auth: EXPIRED - re-authorize with --reauth strava")
            elif strava_status["state"] == "no_session":
                print("Strava auth: not authorized - run --setup-strava")
            elif strava_status["state"] == "refresh_needed":
                print("Strava auth: access token expired, will refresh on next sync")
            else:
                print("Strava auth: valid (refresh token active)")

        if user.zwift:
            zwift_status = _zwift_token_status(user.zwift)
            if zwift_status["state"] == "valid":
                print("Zwift auth: valid")
            elif zwift_status["state"] == "expired":
                print("Zwift auth: expired - run: homevitals --reauth zwift")
            elif zwift_status["state"] == "refresh_needed":
                print("Zwift auth: connected (refreshes on next sync)")
            else:
                print("Zwift auth: not connected - run: homevitals --reauth zwift")


def _show_history(state, users: list, limit: int = 14) -> None:
    """Print recent sync history as a table."""
    KG_TO_LB = 2.20462

    multi = len(users) > 1
    for user in users:
        if multi:
            print(f"\n{user.name}")
        history = state.get_history(user.name, limit=limit)
        if not history:
            print("No sync history yet.")
            continue

        # Determine which targets are in the data
        all_targets = set()
        for entry in history:
            all_targets.update(entry["targets"])
        target_cols = sorted(all_targets)

        # Header
        header = f"{'Date':<12} {'Weight':<22}"
        for t in target_cols:
            header += f" {t.capitalize():<8}"
        print(header)
        print("-" * len(header))

        # Rows
        for entry in history:
            ts = entry["timestamp"]
            date_str = ts[:10] if len(ts) >= 10 else ts
            kg = entry["weight_kg"]
            lb = kg * KG_TO_LB
            weight_str = f"{kg:.2f} kg ({lb:.1f} lb)"

            row = f"{date_str:<12} {weight_str:<22}"
            for t in target_cols:
                mark = "✓" if t in entry["targets"] else "-"
                row += f" {mark:<8}"
            print(row)

        print("")
        print("Weight from Eufy cloud API. May differ from your scale display by up to ~0.5 lb.")
