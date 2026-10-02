"""Tests for the window's logic (homevitals/gui_logic.py). No tkinter, no network.

"""
from __future__ import annotations

import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from homevitals import gui_logic
from homevitals.credentials import get_password, get_token, store_password, store_token
from homevitals.eufy_client import EufyProfile
from homevitals.gui_logic import CheckResult, Person

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _household(tmp_path: Path, fixtures, passwords: bool = True) -> Path:
    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), path)
    if passwords:
        for name in ("Chris", "Jane"):
            store_password(f"{name}:eufy", "pw")
            store_password(f"{name}:garmin", "pw")
    return path


def _people() -> list[Person]:
    return [
        Person("Chris", "scale@example.com", "adult-a@example.com", "adult-a-0001"),
        Person("Jane", "scale@example.com", "adult-b@example.com", "adult-b-0002"),
    ]


def _profile(cid: str, kg: float, day: int) -> EufyProfile:
    return EufyProfile(customer_id=cid, last_measured=datetime(2026, 9, day, 7, tzinfo=timezone.utc), last_weight_kg=kg)


def _users(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())["users"]


# ---------------------------------------------------------------------------
# Module shape
# ---------------------------------------------------------------------------


def test_module_does_not_import_tkinter():
    import ast
    tree = ast.parse(Path(gui_logic.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "tkinter" not in imported
    assert "_tkinter" not in imported


# ---------------------------------------------------------------------------
# Reading people
# ---------------------------------------------------------------------------


def test_read_people_from_household_fixture(fixtures):
    people = gui_logic.read_people(fixtures.path("config_household.yaml"))
    assert people == _people()


def test_read_people_missing_file_is_empty(tmp_path):
    assert gui_logic.read_people(tmp_path / "nope.yaml") == []


@pytest.mark.parametrize("content", ["", "users: [", "- just a list", "users: 5"])
def test_read_people_malformed_yaml_is_empty(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    assert gui_logic.read_people(path) == []


def test_read_people_never_exposes_inline_passwords(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [{"name": "default",
                                          "eufy": {"email": "e@example.com", "password": "Inline-Secret-1"},
                                          "garmin": {"email": "g@example.com", "password": "Inline-Secret-2"}}]}))
    people = gui_logic.read_people(path)
    assert "Inline-Secret" not in repr(people)
    assert people[0].garmin_email == "g@example.com"


def test_read_people_marks_omron_presence(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [
        {"name": "Chris", "eufy": {"email": "e@example.com", "customer_id": "a"}, "garmin": {"email": "a@example.com"},
         "omron": {"email": "a@example.com", "country": "CA"}},
        {"name": "Jane", "eufy": {"email": "e@example.com", "customer_id": "b"}, "garmin": {"email": "b@example.com"}},
    ]}))
    assert [p.has_omron for p in gui_logic.read_people(path)] == [True, False]


def test_config_problem_translates_missing_customer_id(fixtures):
    text = gui_logic.config_problem(fixtures.path("config_household_missing_customer_id.yaml"))
    assert text == "Jane isn't linked to a scale profile yet. Click Fix problems and choose their profile."


def test_config_problem_none_for_valid_config(fixtures, tmp_path):
    assert gui_logic.config_problem(fixtures.path("config_household.yaml")) is None
    assert gui_logic.config_problem(tmp_path / "missing.yaml") is None


# ---------------------------------------------------------------------------
# Add person: validation
# ---------------------------------------------------------------------------


def test_validate_name_blank():
    assert gui_logic.validate_name("  ", []) == "Please type a name."


def test_validate_name_too_long():
    assert gui_logic.validate_name("x" * 41, []) == "Keep the name under 40 characters."


def test_validate_name_duplicate_case_insensitive():
    assert gui_logic.validate_name("chris", _people()) == (
        "Someone called 'chris' is already set up. Pick a different name."
    )


def test_validate_name_ok():
    assert gui_logic.validate_name("Sam", _people()) is None


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "@example.com", "a@.", "a b@example.com"])
def test_validate_email_rejects_garbage(email):
    assert gui_logic.validate_email(email) == "That doesn't look like an email address."


def test_validate_email_ok():
    assert gui_logic.validate_email(" adult-a@example.com ") is None






def test_profile_choices_marks_linked_profiles_and_formats_kg_and_lb():
    profiles = [_profile("adult-a-0001", 80.0, 30), _profile("kid-c-0003", 30.4, 29)]
    choices = gui_logic.profile_choices(profiles, _people())
    assert [c.customer_id for c in choices] == ["adult-a-0001", "kid-c-0003"]
    assert choices[0].label == "Profile ...0001 - 80.0 kg (176.4 lb), last weigh-in 2026-09-30"
    assert choices[0].linked_to == "Chris"
    assert choices[1].linked_to is None


def test_validate_profile_choice_rejects_linked_and_none():
    assert gui_logic.validate_profile_choice(None, _people()) == "Pick the profile that belongs to this person."
    assert gui_logic.validate_profile_choice("adult-b-0002", _people()) == "That profile is already linked to Jane."
    assert gui_logic.validate_profile_choice("new-0005", _people()) is None


def test_validate_garmin_email_rejects_another_persons_account():
    assert gui_logic.validate_garmin_email("Adult-A@example.com", _people()) == (
        "Chris already syncs to that Garmin account. Each person needs their own."
    )
    assert gui_logic.validate_garmin_email("bad", _people()) == "That doesn't look like an email address."
    assert gui_logic.validate_garmin_email("new@example.com", _people()) is None


# ---------------------------------------------------------------------------
# Add person: saving
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Remove person
# ---------------------------------------------------------------------------


def test_removal_confirmation_text():
    assert gui_logic.removal_confirmation_text("Jane") == (
        "Remove Jane? Their Garmin data stays in Garmin; this only stops syncing."
    )


def test_remove_person_deletes_passwords_and_keyed_garmin_token(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    store_password("Jane:omron", "x")
    store_token("garmin:adult-b@example.com", {"di_token": "b"})
    store_token("garmin:adult-a@example.com", {"di_token": "a"})
    result = gui_logic.remove_person(path, "Jane")
    assert result.removed and not result.last_person
    assert [u["name"] for u in _users(path)] == ["Chris"]
    for key in ("Jane:eufy", "Jane:garmin", "Jane:omron"):
        assert get_password(key) is None
    assert get_password("Chris:garmin") == "pw"
    assert get_token("garmin:adult-b@example.com") is None
    assert get_token("garmin:adult-a@example.com") == {"di_token": "a"}


def test_remove_person_keeps_shared_eufy_token(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    store_token("eufy:scale@example.com", {"access_token": "t"})
    result = gui_logic.remove_person(path, "Jane")
    assert result.eufy_token_deleted is False
    assert get_token("eufy:scale@example.com") == {"access_token": "t"}


def test_remove_person_deletes_eufy_token_when_last_user_of_that_login(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [
        {"name": "Chris", "eufy": {"email": "scale@example.com", "customer_id": "a"}, "garmin": {"email": "a@example.com"}},
        {"name": "Sam", "eufy": {"email": "other@example.com", "customer_id": "b"}, "garmin": {"email": "b@example.com"}},
    ]}))
    store_token("eufy:other@example.com", {"access_token": "t"})
    result = gui_logic.remove_person(path, "Sam")
    assert result.eufy_token_deleted is True
    assert get_token("eufy:other@example.com") is None


def test_remove_last_person_deletes_config_and_turns_off_autosync(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [{"name": "Chris", "eufy": {"email": "scale@example.com"},
                                          "garmin": {"email": "a@example.com"}}]}))
    with patch("homevitals.gui_logic.platform_support.uninstall_agent") as mock_uninstall:
        result = gui_logic.remove_person(path, "Chris")
    assert result.last_person is True
    assert not path.exists()
    mock_uninstall.assert_called_once()


def test_remove_person_leaves_state_db_alone(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    db = tmp_path / "state.db"
    db.write_bytes(b"history")
    gui_logic.remove_person(path, "Jane")
    assert db.read_bytes() == b"history"


def test_remove_unknown_person_changes_nothing(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    before = path.read_text()
    assert gui_logic.remove_person(path, "Sam").removed is False
    assert path.read_text() == before


# ---------------------------------------------------------------------------
# Sync now
# ---------------------------------------------------------------------------


def test_sync_command_uses_module_run_and_headless():
    cmd = gui_logic.sync_command()
    assert cmd[1:] == ["-m", "homevitals", "--headless"]


def test_sync_command_swaps_pythonw_for_python(tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    python = tmp_path / "python.exe"
    pythonw.write_text("")
    python.write_text("")
    with patch.object(sys, "executable", str(pythonw)):
        assert gui_logic.sync_command()[0] == str(python)
    python.unlink()
    with patch.object(sys, "executable", str(pythonw)):
        assert gui_logic.sync_command()[0] == str(pythonw)


def test_sync_environment_forces_utf8():
    env = gui_logic.sync_environment()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def _t(line: str) -> str | None:
    return gui_logic.translate_line(line, _people())


def test_translate_hides_debug_and_tracebacks():
    assert _t("2026-10-01 10:00:00,123 DEBUG homevitals.sync: details") is None
    assert _t("Traceback (most recent call last):") is None
    assert _t('  File "C:\\x\\sync.py", line 5, in sync_user') is None
    assert _t("    ^^^^^^") is None
    assert _t("RuntimeError: boom") is None


def test_translate_strips_log_prefix_from_warnings():
    assert _t("2026-10-01 10:00:00,123 WARNING homevitals.x: Something odd happened") == "Something odd happened"


def test_translate_lock_held():
    assert _t("Another homevitals run is in progress; skipping.") == (
        "A sync is already running (probably the automatic one). Try again in a minute."
    )


def test_translate_syncs_completed_plural_and_singular():
    assert _t("Syncs completed: Garmin 2.") == "Synced 2 weigh-ins to Garmin."
    assert _t("Syncs completed: Garmin 1.") == "Synced 1 weigh-in to Garmin."


def test_translate_no_new_measurements_multi_and_single():
    assert _t("No new measurements for 2 profiles.") == "Nothing new to sync for anyone."
    assert _t("No new measurements | last sync: 2h ago | Garmin connected") == "Nothing new to sync."
    assert gui_logic.line_means_nothing_new("No new measurements for 2 profiles.")
    # One person with nothing new doesn't mean the whole run had nothing (the other may have synced).
    assert not gui_logic.line_means_nothing_new("Jane: nothing new.")
    assert not gui_logic.line_means_nothing_new("Chris: synced 2 weigh-ins to Garmin.")


def test_translate_open_eufy_app_hint():
    line = ("If you weighed in recently and it isn't here, open the Eufy app so it uploads to the cloud, "
            "then run homevitals again.")
    assert _t(line) == "If someone weighed in recently, open the Eufy app on the phone first, then sync again."


def test_translate_per_user_lines_pass_through():
    assert _t("Chris: synced 2 weigh-ins to Garmin.") == "Chris: synced 2 weigh-ins to Garmin."
    assert _t("Jane: nothing new.") == "Jane: nothing new to sync."


def test_translate_per_user_line_with_failure_clause():
    line = "Jane: nothing new. Garmin failed - Garmin wants an MFA code. Run: homevitals --reauth garmin"
    assert _t(line) == "Jane: nothing new to sync. Garmin needs Jane to log in again. Click Fix problems."


def test_translate_reauth_names_the_person():
    line = ("2026-10-01 10:00:00,123 ERROR homevitals.sync: Authentication failed for Jane/garmin: "
            "Garmin wants an MFA code and no one is here to type it. Run: homevitals --reauth garmin")
    assert _t(line) == "Garmin needs Jane to log in again. Click Fix problems."
    assert _t("Run: homevitals --reauth garmin") == "Garmin needs someone to log in again. Click Fix problems."


def test_translate_update_password_names_service():
    assert _t("Chris: failed - Eufy login failed: bad. If you changed your Eufy password, run: "
              "homevitals --update-password") == (
        "Chris's Eufy password is wrong or has changed. Click Fix problems and enter the new one."
    )
    assert _t("Jane: failed - Garmin rejected the email or password. Run: homevitals --update-password") == (
        "Jane's Garmin password is wrong or has changed. Click Fix problems and enter the new one."
    )
    assert _t("Garmin rejected the email or password. Run: homevitals --update-password") == (
        "Someone's Garmin password is wrong or has changed. Click Fix problems and enter the new one."
    )


def test_translate_select_profile():
    assert _t("No data synced for Chris. Choose a profile with: homevitals --select-profile") == (
        "Chris isn't linked to a scale profile yet. Click Fix problems and choose their profile."
    )


def test_translate_playwright():
    assert _t("Garmin's browser fallback needs the optional Playwright extra") == (
        "Garmin's direct login didn't work this time (Garmin sometimes blocks repeated logins). "
        "Wait an hour, then click Fix problems and log in again."
    )


def test_translate_rate_limit():
    assert _t("Chris: failed - 429 Too Many Requests") == "Garmin is asking us to slow down. Wait an hour and try again."


def test_translate_network_retry():
    assert _t("2026-10-01 10:00:00,123 WARNING homevitals.cli.app: Network unavailable; retrying scheduled sync "
              "once in 60s") == "No internet connection right now. Trying once more in a minute..."


def test_translate_transient_network():
    assert _t("Chris: failed - [Errno 11001] getaddrinfo failed") == (
        "Couldn't reach the internet. Check the connection and try again."
    )


def test_translate_sync_failed_for_names():
    assert _t("Sync failed for: Chris/garmin, Jane. Run with --verbose for details.") == (
        "Sync didn't finish for Chris (Garmin), Jane. See the lines above."
    )


def test_translate_could_not_start_uses_translate_error():
    assert _t("homevitals could not start: User 'Jane' has no eufy.customer_id. With more than one person...") == (
        "Couldn't start the sync: Jane isn't linked to a scale profile yet. Click Fix problems and choose their profile."
    )


def test_translate_garmin_existing_dates():
    assert _t("Garmin already has a weigh-in dated 2026-09-30.") == (
        "Garmin already had a weigh-in for 2026-09-30, so that one was skipped."
    )
    assert _t("Garmin already has weigh-ins for 2 dates.") == (
        "Garmin already had some of these weigh-ins, so they were skipped."
    )


def test_translate_no_config():
    assert _t("No config found. Run homevitals in a terminal to set up.") == "Nobody is set up yet. Click Add person first."


def test_translate_unknown_line_passes_through():
    assert _t("Something new from upstream.") == "Something new from upstream."


def test_translate_blank_line_is_hidden():
    assert _t("   ") is None


def test_finish_message_variants():
    assert gui_logic.finish_message(0, False) == "Done."
    assert gui_logic.finish_message(0, True).startswith("Done. Nothing new to sync. If someone weighed in recently, "
                                                        "open the Eufy app on their phone first")
    assert gui_logic.finish_message(1, False) == (
        "Sync finished with problems. Read the lines above. Fix problems can repair logins and passwords."
    )


@pytest.mark.parametrize("text,expected", [
    ("User 'Jane' has no eufy.customer_id. With more ...", "Jane isn't linked to a scale profile yet. Click Fix problems and choose their profile."),
    ("User names must be unique: 'Chris' appears", "Two people have the same name in the settings file. Remove one of them."),
    ("Users 'A' and 'B' both use the Garmin account x", "Two people are set up with the same Garmin account. Each person needs their own."),
    ("Users 'A' and 'B' are linked to the same Eufy profile (...0001)", "Two people are linked to the same scale profile. Remove one and add them again."),
    ("No garmin password found for user 'Jane'. Run: homevitals --update-password", "Jane's Garmin password is missing. Click Fix problems to enter it."),
    ("No users found in x. The file is empty or malformed.", "The settings file is empty or damaged. Remove it and add people again."),
    ("The system keychain could not be read (locked)", "Windows couldn't open the saved passwords. Sign out and back in to Windows, then try again."),
    ("Something else entirely", "Something else entirely"),
])
def test_translate_error_each_rule(text, expected):
    assert gui_logic.translate_error(text) == expected


# ---------------------------------------------------------------------------
# Fix problems
# ---------------------------------------------------------------------------


def _profiles_client(customer_ids):
    client = MagicMock()
    client.list_profiles.return_value = [_profile(c, 70.0, 30) for c in customer_ids]
    return client


def _run(path, eufy_client=None, garmin_clients=None, installed=True):
    eufy_client = eufy_client or _profiles_client(["adult-a-0001", "adult-b-0002", "kid-c-0003"])
    garmin_factory = MagicMock(side_effect=garmin_clients) if garmin_clients else MagicMock()
    seen: list[CheckResult] = []
    with patch("homevitals.gui_logic.EufyClient", return_value=eufy_client) as eufy_ctor, \
         patch("homevitals.gui_logic.GarminClient", garmin_factory), \
         patch("homevitals.gui_logic.platform_support.agent_installed", return_value=installed):
        results = gui_logic.run_checks(path, seen.append)
    return results, seen, eufy_ctor, garmin_factory


def test_run_checks_reports_autosync_profiles_and_garmin_ok(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    results, _, _, garmin = _run(path)
    assert all(r.ok for r in results), results
    texts = [r.text for r in results]
    assert texts[0] == "Automatic sync is on (every 4 hours)."
    assert "Eufy login works and Chris's scale profile is there." in texts
    assert "Eufy login works and Jane's scale profile is there." in texts
    assert "Garmin login for Chris works." in texts
    assert "Garmin login for Jane works." in texts
    for call in garmin.return_value.authenticate.call_args_list:
        assert call.kwargs == {"allow_interactive": False}
    assert garmin.return_value.close.call_count == 2


def test_run_checks_logs_in_to_eufy_once_per_shared_login(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    _, _, eufy_ctor, _ = _run(path)
    assert eufy_ctor.call_count == 1
    eufy_ctor.return_value.close.assert_called_once()


def test_run_checks_autosync_off_offers_turn_on(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    results, *_ = _run(path, installed=False)
    assert results[0] == CheckResult(False, "Automatic sync is off.", None, "turn_on_autosync")


def test_run_checks_missing_profile_offers_choose_profile(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [{"name": "default", "eufy": {"email": "scale@example.com"},
                                          "garmin": {"email": "a@example.com"}}]}))
    store_password("default:eufy", "pw")
    store_password("default:garmin", "pw")
    results, *_ = _run(path)
    match = [r for r in results if r.action == "choose_profile"]
    assert match == [CheckResult(False, "default isn't linked to a scale profile yet.", "default", "choose_profile")]


def test_run_checks_profile_not_on_account(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    results, *_ = _run(path, eufy_client=_profiles_client(["adult-a-0001"]))
    jane = [r for r in results if r.person == "Jane" and "profile" in r.text]
    assert jane[0].ok is False
    assert jane[0].text.startswith("Jane's scale profile was not found on the Eufy account.")


def test_run_checks_garmin_mfa_needed_offers_login_again(fixtures, tmp_path):
    from homevitals.sync import PermanentSyncError
    path = _household(tmp_path, fixtures)
    chris, jane = MagicMock(), MagicMock()
    jane.authenticate.side_effect = PermanentSyncError(
        "Garmin wants an MFA code and no one is here to type it. Run: homevitals --reauth garmin")
    results, *_ = _run(path, garmin_clients=[chris, jane])
    assert CheckResult(False, "Garmin needs Jane to log in again.", "Jane", "garmin_login") in results
    jane.close.assert_called_once()


def test_run_checks_garmin_bad_password_offers_change_password(fixtures, tmp_path):
    from homevitals.sync import PermanentSyncError
    path = _household(tmp_path, fixtures)
    chris = MagicMock()
    chris.authenticate.side_effect = PermanentSyncError(
        "Garmin rejected the email or password. Run: homevitals --update-password")
    results, *_ = _run(path, garmin_clients=[chris, MagicMock()])
    assert CheckResult(False, "Garmin says the password for Chris is wrong.", "Chris", "change_garmin_password") in results


def test_run_checks_eufy_bad_password_offers_change_password(fixtures, tmp_path):
    from homevitals.sync import PermanentSyncError
    path = _household(tmp_path, fixtures)
    eufy = MagicMock()
    eufy.authenticate.side_effect = PermanentSyncError(
        "Eufy login failed: wrong. If you changed your Eufy password, run: homevitals --update-password")
    results, *_ = _run(path, eufy_client=eufy)
    for name in ("Chris", "Jane"):
        assert CheckResult(False, f"The Eufy password for {name} is wrong or has changed.", name,
                           "change_eufy_password") in results


def test_run_checks_network_error_has_no_action(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    eufy = MagicMock()
    eufy.authenticate.side_effect = OSError("[Errno 11001] getaddrinfo failed")
    results, *_ = _run(path, eufy_client=eufy)
    eufy_rows = [r for r in results if "Eufy" in r.text]
    assert eufy_rows and all(r.action is None and not r.ok for r in eufy_rows)
    assert eufy_rows[0].text == "Couldn't reach Eufy. Check the internet connection and try again."


def test_run_checks_config_error_is_one_failed_result(fixtures, tmp_path):
    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_missing_customer_id.yaml"), path)
    results, *_ = _run(path)
    assert results == [CheckResult(False, "Jane isn't linked to a scale profile yet. Click Fix problems and choose their profile.")]


def test_run_checks_unexpected_exception_becomes_failed_result(fixtures, tmp_path, caplog):
    path = _household(tmp_path, fixtures)
    with patch("homevitals.gui_logic.load_config", side_effect=KeyError("weird")):
        results = gui_logic.run_checks(path, lambda r: None)
    assert results == [CheckResult(False, "Couldn't finish the checks. Details are in the log file.")]
    assert "weird" in caplog.text


def test_run_checks_streams_results_in_order(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    results, seen, *_ = _run(path)
    assert seen == results


def test_run_checks_migrates_legacy_tokens(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    with patch("homevitals.gui_logic.migrate_legacy_tokens") as migrate:
        _run(path)
    assert [u.name for u in migrate.call_args.args[0]] == ["Chris", "Jane"]


@pytest.mark.parametrize("message,expected_text,expected_action", [
    ("Garmin wants an MFA code. Run: homevitals --reauth garmin", "Garmin needs Chris to log in again.", "garmin_login"),
    ("Garmin rejected the email or password. Run: homevitals --update-password",
     "Garmin says the password for Chris is wrong.", "change_garmin_password"),
    ("HTTP 429 from Garmin", "Garmin is asking us to slow down. Wait an hour and try again.", None),
    ("Garmin's browser fallback needs the optional Playwright extra",
     "Garmin's direct login didn't work this time (Garmin sometimes blocks repeated logins). Wait an hour, "
     "then try Log in again.", "garmin_login"),
    ("[Errno 11001] getaddrinfo failed", "Couldn't reach Garmin. Check the internet connection and try again.", None),
    ("something odd", "Garmin didn't answer as expected for Chris. Details are in the log file.", None),
])
def test_classify_garmin_error_table(message, expected_text, expected_action):
    result = gui_logic.classify_garmin_error(RuntimeError(message), "Chris")
    assert (result.ok, result.text, result.person, result.action) == (False, expected_text, "Chris", expected_action)


def test_classify_garmin_error_cancelled_login_is_not_called_a_wrong_password():
    from homevitals.sync import PermanentSyncError
    exc = PermanentSyncError("Garmin login cancelled. If the MFA email never arrived, the stored password is "
                             "likely wrong; run: homevitals --update-password")
    result = gui_logic.classify_garmin_error(exc, "Jane")
    assert result.text == ("Garmin login was cancelled. If the code email never arrived, the password is "
                           "probably wrong.")
    assert result.action == "garmin_login"


def test_classify_garmin_error_cancelled_and_too_many_requests_types():
    from garminconnect import GarminConnectTooManyRequestsError

    from homevitals.garmin_auth import GarminLoginCancelled
    assert gui_logic.classify_garmin_error(GarminLoginCancelled("x"), "Jane").action == "garmin_login"
    assert gui_logic.classify_garmin_error(GarminConnectTooManyRequestsError("x"), "Jane").text == (
        "Garmin is asking us to slow down. Wait an hour and try again."
    )


@pytest.mark.parametrize("message,expected_text,expected_action", [
    ("Eufy login failed: bad. run: homevitals --update-password",
     "The Eufy password for Chris is wrong or has changed.", "change_eufy_password"),
    ("connection reset by peer", "Couldn't reach Eufy. Check the internet connection and try again.", None),
    ("odd", "Eufy didn't answer as expected for Chris. Details are in the log file.", None),
])
def test_classify_eufy_error_table(message, expected_text, expected_action):
    result = gui_logic.classify_eufy_error(RuntimeError(message), "Chris")
    assert (result.ok, result.text, result.person, result.action) == (False, expected_text, "Chris", expected_action)


def test_garmin_login_again_uses_force_reauth(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    with patch("homevitals.gui_logic.GarminAuth") as auth:
        gui_logic.garmin_login_again(path, "Jane")
    assert auth.call_args.args == ("adult-b@example.com", "pw")
    auth.return_value.force_reauth.assert_called_once()


def test_choose_profile_saves_to_named_user(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [{"name": "default", "eufy": {"email": "scale@example.com"},
                                          "garmin": {"email": "a@example.com"}}]}))
    gui_logic.choose_profile(path, "default", "adult-a-0001")
    assert _users(path)[0]["eufy"]["customer_id"] == "adult-a-0001"


def test_choose_profile_rejects_a_linked_profile(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    with pytest.raises(ValueError, match="already linked to Chris"):
        gui_logic.choose_profile(path, "Jane", "adult-a-0001")


# ---------------------------------------------------------------------------
# MFA bridge
# ---------------------------------------------------------------------------


def test_mfa_bridge_returns_code_from_main_thread():
    bridge = gui_logic.MfaBridge(post=lambda fn: fn(), ask=lambda: " 123456 ")
    assert bridge.prompt() == " 123456 "


def test_mfa_bridge_cancel_returns_empty():
    bridge = gui_logic.MfaBridge(post=lambda fn: fn(), ask=lambda: None)
    assert bridge.prompt() == ""


def test_mfa_bridge_timeout_returns_empty():
    bridge = gui_logic.MfaBridge(post=lambda fn: None, ask=lambda: "never", timeout=0.05)
    assert bridge.prompt() == ""


def test_mfa_bridge_works_across_threads():
    pending = []
    bridge = gui_logic.MfaBridge(post=pending.append, ask=lambda: "654321", timeout=5)
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("code", bridge.prompt()))
    worker.start()
    while not pending:
        threading.Event().wait(0.01)
    pending[0]()   # the "main thread" runs the posted dialog
    worker.join(5)
    assert out["code"] == "654321"


def test_mfa_bridge_ask_exception_returns_empty():
    def boom():
        raise RuntimeError("dialog failed")
    bridge = gui_logic.MfaBridge(post=lambda fn: fn(), ask=boom)
    assert bridge.prompt() == ""


# ---------------------------------------------------------------------------
# Desktop shortcut
# ---------------------------------------------------------------------------


def test_shortcut_plan_prefers_gui_launcher_then_pythonw(tmp_path):
    launcher = tmp_path / "homevitals-gui.exe"
    launcher.write_text("")
    with patch("homevitals.gui_logic.shutil.which", return_value=str(launcher)):
        plan = gui_logic.shortcut_plan()
    assert plan.target == str(launcher) and plan.arguments == ""
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("")
    launcher.unlink()
    with patch("homevitals.gui_logic.shutil.which", return_value=None), \
         patch.object(sys, "executable", str(tmp_path / "python.exe")):
        plan = gui_logic.shortcut_plan()
    assert plan.target == str(pythonw)
    assert plan.arguments == "-m homevitals.gui"


def test_shortcut_plan_none_when_nothing_found(tmp_path):
    with patch("homevitals.gui_logic.shutil.which", return_value=None), \
         patch.object(sys, "executable", str(tmp_path / "python.exe")):
        assert gui_logic.shortcut_plan() is None


def test_shortcut_script_escapes_single_quotes_and_uses_desktop_folder():
    plan = gui_logic.ShortcutPlan(target="C:\\Users\\O'Neil\\gui.exe", arguments="", working_dir="C:\\Users\\O'Neil")
    script = gui_logic.shortcut_script(plan)
    assert "[Environment]::GetFolderPath('Desktop')" in script
    assert "C:\\Users\\O''Neil\\gui.exe" in script
    assert "HomeVitals.lnk" in script
    assert script.rstrip().endswith("$s.Save()\nWrite-Output $s.FullName")


def test_translate_unknown_person_failure_hides_the_raw_error():
    assert _t("Chris: failed - KeyError 'dateWeightList' from https://x/y?token=abc") == (
        "Chris: sync failed. Details are in the log file."
    )
    assert _t("Jane: nothing new. Garmin failed - weird 500 body") == (
        "Jane: nothing new to sync. Garmin failed. Details are in the log file."
    )


def test_run_checks_locked_keychain_gets_the_plain_sentence(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    with patch("homevitals.gui_logic.load_config",
               side_effect=RuntimeError("The system keychain could not be read (locked).")):
        results = gui_logic.run_checks(path, lambda r: None)
    assert results == [CheckResult(False, "Windows couldn't open the saved passwords. Sign out and back in to "
                                          "Windows, then try again.")]


# ---------------------------------------------------------------------------
# Part 4: blood pressure in the window's logic (WP19)
# ---------------------------------------------------------------------------

OMRON_PW = "Falcon-Dune-7731!"


def _omron_household(tmp_path: Path, fixtures) -> Path:
    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_omron.yaml"), path)
    for name in ("Chris", "Jane"):
        for service in ("eufy", "garmin", "omron"):
            store_password(f"{name}:{service}", "pw")
    return path


def test_country_choices_default_canada_first_codes_unique_and_all_two_letters():
    codes = [c for c, _ in gui_logic.OMRON_COUNTRY_CHOICES]
    assert codes[0] == "CA" == gui_logic.DEFAULT_OMRON_COUNTRY
    assert len(codes) == len(set(codes))
    assert all(len(c) == 2 and c.isalpha() and c.isupper() for c in codes)
    assert "QA" in codes and "US" in codes and "GB" in codes


def test_country_label_roundtrip():
    assert gui_logic.country_label("CA") == "Canada (CA)"
    assert gui_logic.country_code_from_label("Canada (CA)") == "CA"
    assert gui_logic.country_code_from_label("qatar") == "QA"
    assert gui_logic.country_code_from_label("ca") == "CA"
    assert gui_logic.country_code_from_label(" xx ") == "XX"
    assert gui_logic.country_name("QA") == "Qatar"
    assert gui_logic.country_name("zz") == "ZZ"


def test_validate_omron_country_rules():
    assert gui_logic.validate_omron_country("") == "Pick the country the OMRON connect account was created in."
    assert gui_logic.validate_omron_country("CAN") == "Pick the country the OMRON connect account was created in."
    assert gui_logic.validate_omron_country("Canada") is None
    assert gui_logic.validate_omron_country("QA") is None
    assert gui_logic.validate_omron_country("JP") == (
        "OMRON connect accounts created in JP use OMRON's older servers, which this build does not support yet.")


def test_validate_omron_email_rejects_another_persons_account_and_honours_exclude(fixtures):
    people = gui_logic.read_people(fixtures.path("config_household_omron.yaml"))
    assert gui_logic.validate_omron_email("Adult-B@example.com", people) == (
        "Jane already syncs blood pressure from that OMRON connect account. Each person needs their own.")
    assert gui_logic.validate_omron_email("adult-b@example.com", people, exclude="Jane") is None
    assert gui_logic.validate_omron_email("nope", people) == "That doesn't look like an email address."


def test_read_people_includes_omron_email_and_country_never_password(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [{"name": "Chris", "eufy": {"email": "e@example.com", "customer_id": "a"},
                                          "garmin": {"email": "g@example.com"},
                                          "omron": {"email": " O@example.com ", "country": "qa",
                                                    "password": "Inline-Omron-Secret"}}]}))
    (person,) = gui_logic.read_people(path)
    assert (person.has_omron, person.omron_email, person.omron_country) == (True, "O@example.com", "QA")
    assert "Inline-Omron-Secret" not in repr(person)


def test_check_omron_login_forces_fresh_login_checks_connection_and_closes():
    with patch("homevitals.gui_logic.OmronClient") as client_cls:
        gui_logic.check_omron_login("o@example.com", "pw", "QA")
    config = client_cls.call_args.args[0]
    assert (config.email, config.password, config.country) == ("o@example.com", "pw", "QA")
    client = client_cls.return_value
    client.authenticate.assert_called_once_with(force_login=True)
    client.check_connection.assert_called_once()
    client.close.assert_called_once()


def test_check_omron_login_closes_on_failure():
    with patch("homevitals.gui_logic.OmronClient") as client_cls:
        client_cls.return_value.authenticate.side_effect = RuntimeError("no")
        with pytest.raises(RuntimeError):
            gui_logic.check_omron_login("o@example.com", "pw", "CA")
    client_cls.return_value.close.assert_called_once()


def test_connect_omron_writes_password_then_section(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    gui_logic.connect_omron(path, "Jane", "jane-omron@example.com", OMRON_PW, "qa")
    assert get_password("Jane:omron") == OMRON_PW
    assert _users(path)[1]["omron"] == {"email": "jane-omron@example.com", "country": "QA"}
    assert OMRON_PW not in path.read_text()


def test_connect_omron_rolls_back_password_on_write_failure(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    with patch("homevitals.cli.shared._write_config", side_effect=OSError("disk full")), pytest.raises(OSError):
        gui_logic.connect_omron(path, "Jane", "jane-omron@example.com", OMRON_PW, "CA")
    assert get_password("Jane:omron") is None


def test_connect_omron_rejects_account_used_by_another_person(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    with pytest.raises(ValueError, match="both use the OMRON connect account"):
        gui_logic.connect_omron(path, "Jane", "adult-a@example.com", "pw", "CA")
    assert get_password("Jane:omron") == "pw"


def test_connect_omron_replaces_existing_details(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    gui_logic.connect_omron(path, "Jane", "jane-new@example.com", "new-pw", "GB")
    assert _users(path)[1]["omron"] == {"email": "jane-new@example.com", "country": "GB"}
    assert get_password("Jane:omron") == "new-pw"


def test_disconnect_omron_removes_section_password_and_token(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    store_token("omron:adult-b@example.com", {"access_token": "t"})
    assert gui_logic.disconnect_omron(path, "Jane") is True
    assert "omron" not in _users(path)[1]
    assert get_password("Jane:omron") is None
    assert get_token("omron:adult-b@example.com") is None
    assert get_password("Chris:omron") == "pw"


def _shared_omron_config(path):
    # Hand-edited: two people on one OMRON account (load_config would refuse this; disconnect must still be safe).
    path.write_text(yaml.dump({"users": [
        {"name": "Chris", "eufy": {"email": "s@example.com", "customer_id": "a"}, "garmin": {"email": "a@example.com"},
         "omron": {"email": "shared@example.com", "country": "CA"}},
        {"name": "Jane", "eufy": {"email": "s@example.com", "customer_id": "b"}, "garmin": {"email": "b@example.com"},
         "omron": {"email": "SHARED@example.com", "country": "CA"}},
    ]}))


def test_disconnect_omron_keeps_token_used_by_someone_else(tmp_path):
    path = tmp_path / "config.yaml"
    _shared_omron_config(path)
    store_token("omron:shared@example.com", {"access_token": "t"})
    assert gui_logic.disconnect_omron(path, "Chris") is True
    assert get_token("omron:shared@example.com") == {"access_token": "t"}
    assert "omron" in _users(path)[1]


def test_disconnect_omron_deletes_token_when_nobody_else_uses_it(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [
        {"name": "Chris", "eufy": {"email": "s@example.com", "customer_id": "a"}, "garmin": {"email": "a@example.com"},
         "omron": {"email": "solo@example.com", "country": "CA"}},
    ]}))
    store_token("omron:solo@example.com", {"access_token": "t"})
    assert gui_logic.disconnect_omron(path, "Chris") is True
    assert get_token("omron:solo@example.com") is None


def test_connect_omron_restores_previous_password_on_write_failure(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    with patch("homevitals.cli.shared._write_config", side_effect=OSError("disk full")), pytest.raises(OSError):
        gui_logic.connect_omron(path, "Jane", "jane-new@example.com", "new-pw", "GB")
    assert get_password("Jane:omron") == "pw"


def test_connect_omron_deletes_old_token_when_email_changes(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    store_token("omron:adult-b@example.com", {"access_token": "old"})
    gui_logic.connect_omron(path, "Jane", "jane-new@example.com", "new-pw", "GB")
    assert get_token("omron:adult-b@example.com") is None


def test_connect_omron_keeps_token_when_email_is_unchanged(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    store_token("omron:adult-b@example.com", {"access_token": "same"})
    gui_logic.connect_omron(path, "Jane", "Adult-B@example.com", "new-pw", "QA")
    assert get_token("omron:adult-b@example.com") == {"access_token": "same"}


def test_disconnect_omron_noop_when_not_connected(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    before = path.read_text()
    assert gui_logic.disconnect_omron(path, "Jane") is False
    assert path.read_text() == before


def test_disconnect_omron_leaves_state_db_alone(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    db = tmp_path / "state.db"
    db.write_bytes(b"history")
    gui_logic.disconnect_omron(path, "Jane")
    assert db.read_bytes() == b"history"


def test_omron_disconnect_confirmation_text():
    assert gui_logic.omron_disconnect_confirmation_text("Jane") == (
        "Stop syncing blood pressure for Jane? Their readings stay in Garmin and in the OMRON connect app; this only "
        "stops new readings from syncing.")


def test_remove_person_deletes_omron_token_when_unused(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    store_token("omron:adult-b@example.com", {"access_token": "t"})
    store_token("omron:adult-a@example.com", {"access_token": "a"})
    gui_logic.remove_person(path, "Jane")
    assert get_token("omron:adult-b@example.com") is None
    assert get_token("omron:adult-a@example.com") == {"access_token": "a"}
    assert get_password("Jane:omron") is None


@pytest.mark.parametrize("exc,expected_text,expected_action", [
    ("login", gui_logic.omron_login_rejected_text("Chris"), "change_omron_password"),
    (OSError("[Errno 11001] getaddrinfo failed"),
     "Couldn't reach OMRON connect. Check the internet connection and try again.", None),
    ("api", "OMRON connect accepted the password but refused to hand over readings for Chris. Details are in the "
            "log file.", None),
    (RuntimeError("odd SENTINEL"), "OMRON connect didn't answer as expected for Chris. Details are in the log file.",
     None),
])
def test_classify_omron_error_table(exc, expected_text, expected_action):
    from homevitals.omron_client import OmronApiError, OmronLoginError
    if exc == "login":
        exc = OmronLoginError(f"OMRON connect rejected the login for x (country CA) {OMRON_PW}")
    elif exc == "api":
        exc = OmronApiError("OMRON connect refused the request (HTTP 403) at h even after logging in again.")
    result = gui_logic.classify_omron_error(exc, "Chris")
    assert (result.ok, result.text, result.person, result.action) == (False, expected_text, "Chris", expected_action)
    assert OMRON_PW not in result.text and "SENTINEL" not in result.text


def test_run_checks_includes_omron_row_ok_and_failure(fixtures, tmp_path):
    from homevitals.omron_client import OmronLoginError
    path = _omron_household(tmp_path, fixtures)
    chris, jane = MagicMock(), MagicMock()
    jane.authenticate.side_effect = OmronLoginError("OMRON connect rejected the login for x")
    with patch("homevitals.gui_logic.OmronClient", side_effect=[chris, jane]) as ctor:
        results, *_ = _run(path)
    assert CheckResult(True, "OMRON connect login works for Chris.", "Chris") in results
    assert [r for r in results if r.person == "Jane" and r.action == "change_omron_password"]
    assert [c.args[0].email for c in ctor.call_args_list] == ["adult-a@example.com", "adult-b@example.com"]
    chris.check_connection.assert_called_once()
    jane.close.assert_called_once()


def test_run_checks_skips_omron_for_people_without_it(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    with patch("homevitals.gui_logic.OmronClient") as ctor:
        _run(path)
    ctor.assert_not_called()


def test_run_checks_omron_failure_does_not_hide_garmin_results(fixtures, tmp_path):
    path = _omron_household(tmp_path, fixtures)
    with patch("homevitals.gui_logic.OmronClient", side_effect=RuntimeError("boom")):
        results, *_ = _run(path)
    assert "Garmin login for Chris works." in [r.text for r in results]
    assert "Garmin login for Jane works." in [r.text for r in results]


def test_translate_bp_failed_login_names_person():
    assert _t("Chris: blood pressure failed - OMRON connect rejected the login for x (country CA). Check ...") == (
        gui_logic.omron_login_rejected_text("Chris") + " Click Fix problems.")


def test_translate_bp_failed_reauth_and_password():
    assert _t("Jane: blood pressure failed - Garmin wants an MFA code. Run: homevitals --reauth garmin") == (
        "Garmin needs Jane to log in again. Click Fix problems.")
    assert _t("Jane: blood pressure failed - Garmin rejected the email or password. Run: homevitals --update-password") \
        == "Jane's Garmin password is wrong or has changed. Click Fix problems and enter the new one."
    assert _t("Jane: blood pressure failed - OMRON connect refused the request (HTTP 403) at h even after logging in "
              "again. Run: homevitals --update-password") == (
        "Jane's blood pressure sync didn't finish. Details are in the log file.")


def test_translate_bp_failed_transient():
    assert _t("Chris: blood pressure failed - [Errno 11001] getaddrinfo failed") == (
        "Couldn't reach OMRON connect for Chris's blood pressure. Check the internet connection and try again.")


def test_translate_bp_failed_other():
    assert _t("Chris: blood pressure failed - Garmin upload failed (API Error 400)") == (
        "Chris's blood pressure sync didn't finish. Details are in the log file.")
    assert _t("Chris: blood pressure failed - Garmin is asking us to slow down (HTTP 429). Wait an hour.") == (
        "Garmin is asking us to slow down. Wait an hour and try again.")


def test_translate_bp_detail_lines_pass_through():
    for line in ("Chris: blood pressure - no new readings. Open the OMRON connect app on the phone so it picks up "
                 "readings from the monitor, then sync again.",
                 "Chris: blood pressure - skipped 1 reading with values outside the range Garmin accepts "
                 "(nothing was changed or rounded).",
                 "Chris: blood pressure - Garmin already had 2 readings, so they were skipped."):
        assert _t(line) == line


def test_translate_synced_bp_line_passes_through():
    for line in ("Chris: synced 2 weigh-ins and 1 blood pressure reading to Garmin.",
                 "Jane: synced 1 blood pressure reading to Garmin."):
        assert _t(line) == line


def test_translate_syncs_completed_with_blood_pressure_note():
    assert _t("Syncs completed: Garmin 2. Blood pressure: 1 reading.") == (
        "Synced 2 weigh-ins and 1 blood pressure reading to Garmin.")
    assert _t("Syncs completed: Garmin 1. Blood pressure: 3 readings.") == (
        "Synced 1 weigh-in and 3 blood pressure readings to Garmin.")


def test_translate_syncs_completed_blood_pressure_only():
    assert _t("Syncs completed: blood pressure 1.") == "Synced 1 blood pressure reading to Garmin."
    assert _t("Syncs completed: blood pressure 4.") == "Synced 4 blood pressure readings to Garmin."


def test_translate_sync_failed_for_renders_bp_target():
    assert _t("Sync failed for: Chris/garmin_bp, Jane/garmin. Run with --verbose for details.") == (
        "Sync didn't finish for Chris (blood pressure), Jane (Garmin). See the lines above.")


def test_translate_dry_run_bp_passes_through():
    line = "[DRY RUN] Would upload 2 blood pressure readings for Chris."
    assert _t(line) == line


@pytest.mark.parametrize("text,expected", [
    ("User 'Chris' has an omron section without an email. Add ...",
     "Chris's blood pressure monitor settings are incomplete. Use Edit person to connect it again."),
    ("User 'Chris' has an omron.email that is not an email address.",
     "Chris's blood pressure monitor settings are incomplete. Use Edit person to connect it again."),
    ("User 'Jane' has no valid omron.country. Set ...",
     "Jane's OMRON connect country is missing or wrong. Use Edit person and pick the country the account was "
     "created in."),
    ("User 'Jane': OMRON connect accounts created in JP use OMRON's older server system, which ...",
     "Jane's OMRON connect account was created in a country this build can't sync from yet. Use Edit person to "
     "check the country, or see the readme."),
    ("Users 'Chris' and 'Jane' both use the OMRON connect account x. Each ...",
     "Two people are set up with the same OMRON connect account. Each person needs their own."),
    ("User 'Jane' has an invalid omron.server. Use na, eu ...",
     "Jane's blood pressure settings have an invalid advanced option (server or user number). See the readme."),
    ("User 'Jane' has an invalid omron.user_number. Use a whole number from 1 to 99 ...",
     "Jane's blood pressure settings have an invalid advanced option (server or user number). See the readme."),
    ("No omron password found for user 'Jane'. Run: homevitals --update-password",
     "Jane's OMRON connect password is missing. Click Fix problems to enter it."),
])
def test_translate_error_omron_rules(text, expected):
    assert gui_logic.translate_error(text) == expected


def test_translate_hides_upstream_strava_notice():
    # Upstream's one-time "New in homevitals: Strava support!" doesn't apply to a household install.
    assert _t("New in homevitals: Strava support! Run homevitals --setup-strava to connect.") is None


def test_sync_command_can_add_older_bp_readings():
    cmd = gui_logic.sync_command(older_bp_readings=True)
    assert cmd[1:] == ["-m", "homevitals", "--headless", "--bp-backfill-days", str(gui_logic.OLDER_BP_READINGS_DAYS)]
    assert gui_logic.OLDER_BP_READINGS_DAYS >= 3650


# ---------------------------------------------------------------------------
# Logo and "open when Windows starts" (owner request, 2026-10-02)
# ---------------------------------------------------------------------------

def test_app_icon_ships_as_a_real_ico_and_png():
    ico = gui_logic.icon_path()
    assert ico is not None and ico.name == "app_icon.ico"
    data = ico.read_bytes()
    assert data[:4] == b"\0\0\1\0"                 # ICO header
    assert int.from_bytes(data[4:6], "little") >= 5   # several sizes
    assert (ico.parent / "app_icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_icons_are_packaged_with_the_wheel():
    import tomllib
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert "assets/*" in pyproject["tool"]["setuptools"]["package-data"]["homevitals"]


def test_shortcut_script_sets_the_icon_and_the_folder(tmp_path):
    plan = gui_logic.ShortcutPlan(target=r"C:\x\gui.exe", arguments="", working_dir=r"C:\x")
    script = gui_logic.shortcut_script(plan, folder="Startup", icon=Path(r"C:\i\app_icon.ico"))
    assert "[Environment]::GetFolderPath('Startup')" in script
    assert r"$s.IconLocation = 'C:\i\app_icon.ico,0'" in script
    desktop = gui_logic.shortcut_script(plan)
    assert "GetFolderPath('Desktop')" in desktop and "IconLocation" not in desktop


def test_startup_shortcut_path_and_state(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = gui_logic.startup_shortcut_path()
    assert path == tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "HomeVitals.lnk"
    assert gui_logic.startup_enabled() is False
    path.parent.mkdir(parents=True)
    path.write_bytes(b"lnk")
    assert gui_logic.startup_enabled() is True
    gui_logic.disable_startup()
    assert not path.exists()
    gui_logic.disable_startup()       # already off: no error


# ---------------------------------------------------------------------------
# The Person window's logic
# ---------------------------------------------------------------------------

def _partial(tmp_path: Path, fixtures) -> Path:
    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_partial.yaml"), path)
    for name, services in {"Chris": ("eufy", "garmin", "omron"), "Jane": ("garmin",), "Sam": ("eufy",)}.items():
        for service in services:
            store_password(f"{name}:{service}", f"{name}-{service}-pw")
    return path


def _by_name(path: Path) -> dict[str, dict]:
    return {u["name"]: u for u in _users(path)}


def test_person_flags_has_eufy_has_garmin(fixtures):
    people = {p.name: p for p in gui_logic.read_people(fixtures.path("config_household_partial.yaml"))}
    assert (people["Chris"].has_eufy, people["Chris"].has_garmin) == (True, True)
    assert (people["Jane"].has_eufy, people["Jane"].has_garmin) == (False, True)
    assert (people["Sam"].has_eufy, people["Sam"].has_garmin) == (True, False)


def test_read_people_without_eufy_section_gives_none_email(fixtures):
    jane = gui_logic.read_people(fixtures.path("config_household_partial.yaml"))[1]
    assert jane.eufy_email is None and jane.customer_id is None


def test_person_row_cells_for_each_state():
    full = Person("Chris", "scale@example.com", "adult-a@example.com", "adult-a-0001", True, "o@example.com", "GB")
    assert gui_logic.person_row(full) == ("Chris", "adult-a@example.com", "...0001", "on")
    garmin_only = Person("Jane", None, "adult-b@example.com", None)
    assert gui_logic.person_row(garmin_only) == ("Jane", "adult-b@example.com", "not connected", "off")
    no_profile = Person("Sam", "scale@example.com", None, None)
    assert gui_logic.person_row(no_profile) == ("Sam", "not connected", "profile not chosen", "off")


@pytest.mark.parametrize("person,service,expected", [
    (None, "garmin", "Not connected."),
    (Person("C", "s@example.com", "g@example.com", "abc-0001"), "garmin", "Connected as g@example.com."),
    (Person("C", None, None, None), "garmin", "Not connected."),
    (Person("C", "s@example.com", None, "abc-0001"), "eufy", "Connected as s@example.com, profile ...0001."),
    (Person("C", "s@example.com", None, None), "eufy", "Connected as s@example.com, no profile chosen yet."),
    (Person("C", None, None, None), "eufy", "Not connected."),
    (Person("C", None, None, None, True, "o@example.com", "GB"), "omron", "Connected as o@example.com (United Kingdom)."),
    (Person("C", None, None, None), "omron", "Not connected."),
])
def test_account_status_sentences(person, service, expected):
    assert gui_logic.account_status(person, service) == expected


def test_check_garmin_login_uses_force_reauth():
    with patch("homevitals.gui_logic.GarminAuth") as auth:
        gui_logic.check_garmin_login(" b@example.com ", "pw")
    assert auth.call_args.args == ("b@example.com", "pw")
    auth.return_value.force_reauth.assert_called_once()


def test_check_eufy_login_fresh_uses_fresh_login_and_saved_uses_authenticate():
    with patch("homevitals.gui_logic.EufyClient") as client_cls:
        client = client_cls.return_value
        client.list_profiles.return_value = ["p"]
        assert gui_logic.check_eufy_login("s@example.com", "pw", fresh=True) == ["p"]
        client._fresh_login.assert_called_once()
        client.authenticate.assert_not_called()
        gui_logic.check_eufy_login("s@example.com", "pw", fresh=False)
        client.authenticate.assert_called_once()
        assert client.close.call_count == 2
        client.list_profiles.side_effect = RuntimeError("down")
        with pytest.raises(RuntimeError):
            gui_logic.check_eufy_login("s@example.com", "pw", fresh=True)
        assert client.close.call_count == 3


def test_connect_garmin_creates_person_on_first_connect(tmp_path):
    path = tmp_path / "sub" / "config.yaml"
    gui_logic.connect_garmin(path, "Jane", " adult-b@example.com ", "jane-pw")
    assert _users(path) == [{"name": "Jane", "garmin": {"email": "adult-b@example.com"}}]
    assert get_password("Jane:garmin") == "jane-pw"
    assert "jane-pw" not in path.read_text()


def test_connect_garmin_adds_section_to_existing_person(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    gui_logic.connect_garmin(path, "Sam", "sam@example.com", "sam-pw")
    sam = _by_name(path)["Sam"]
    assert sam["garmin"] == {"email": "sam@example.com"} and sam["eufy"]["customer_id"] == "adult-b-0002"


def test_connect_garmin_replaces_email_and_deletes_old_token(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    store_token("garmin:adult-b@example.com", {"di_token": "old"})
    gui_logic.connect_garmin(path, "Jane", "jane-new@example.com", "new-pw")
    assert _by_name(path)["Jane"]["garmin"] == {"email": "jane-new@example.com"}
    assert get_token("garmin:adult-b@example.com") is None
    assert get_password("Jane:garmin") == "new-pw"


def test_connect_garmin_rolls_back_password_on_write_failure(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    with patch("homevitals.cli.shared._write_config", side_effect=OSError("disk full")), pytest.raises(OSError):
        gui_logic.connect_garmin(path, "Jane", "adult-b@example.com", "new-pw")
    assert get_password("Jane:garmin") == "Jane-garmin-pw"
    with patch("homevitals.cli.shared._write_config", side_effect=OSError("disk full")), pytest.raises(OSError):
        gui_logic.connect_garmin(path, "Kim", "kim@example.com", "kim-pw")
    assert get_password("Kim:garmin") is None


@pytest.mark.parametrize("name,match", [(" ", "Every user needs a name"), ("chris", "User names must be unique")])
def test_connect_garmin_rejects_blank_and_duplicate_names(fixtures, tmp_path, name, match):
    path = _partial(tmp_path, fixtures)
    before = path.read_text()
    with pytest.raises(ValueError, match=match):
        gui_logic.connect_garmin(path, name, "new@example.com", "pw")
    assert path.read_text() == before
    assert get_password(f"{name}:garmin") is None


def test_connect_scale_writes_email_and_customer_id_together(tmp_path):
    path = tmp_path / "config.yaml"
    gui_logic.connect_garmin(path, "Jane", "adult-b@example.com", "pw")
    gui_logic.connect_scale(path, "Jane", "scale@example.com", "eufy-pw", "adult-b-0002")
    assert _by_name(path)["Jane"]["eufy"] == {"email": "scale@example.com", "customer_id": "adult-b-0002"}


def test_connect_scale_requires_customer_id_even_for_the_first_person(tmp_path):
    path = tmp_path / "config.yaml"
    with pytest.raises(ValueError, match="Pick the profile"):
        gui_logic.connect_scale(path, "Jane", "scale@example.com", "pw", "  ")
    assert not path.exists()
    assert get_password("Jane:eufy") is None


def test_connect_scale_updates_everyone_sharing_the_eufy_login(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    names = gui_logic.connect_scale(path, "Jane", "SCALE@example.com", "new-eufy", "adult-c-0009")
    assert names[0] == "Jane" and set(names) == {"Jane", "Chris", "Sam"}
    for name in ("Jane", "Chris", "Sam"):
        assert get_password(f"{name}:eufy") == "new-eufy"


def test_connect_scale_creates_person_when_first_account(tmp_path):
    path = tmp_path / "config.yaml"
    assert gui_logic.connect_scale(path, "Sam", "scale@example.com", "pw", "adult-b-0002") == ["Sam"]
    assert _users(path) == [{"name": "Sam", "eufy": {"email": "scale@example.com", "customer_id": "adult-b-0002"}}]


def test_connect_scale_rolls_back_every_password_on_write_failure(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    with patch("homevitals.cli.shared._write_config", side_effect=OSError("disk full")), pytest.raises(OSError):
        gui_logic.connect_scale(path, "Jane", "scale@example.com", "new-eufy", "adult-c-0009")
    assert get_password("Chris:eufy") == "Chris-eufy-pw"
    assert get_password("Sam:eufy") == "Sam-eufy-pw"
    assert get_password("Jane:eufy") is None


def test_connect_scale_deletes_old_eufy_token_only_when_unused(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    store_token("eufy:scale@example.com", {"access_token": "shared"})
    gui_logic.connect_scale(path, "Sam", "other@example.com", "pw", "adult-b-0002")
    assert get_token("eufy:scale@example.com") == {"access_token": "shared"}     # Chris still uses it
    gui_logic.connect_scale(path, "Chris", "other@example.com", "pw", "adult-a-0001")
    assert get_token("eufy:scale@example.com") is None                           # nobody does now


def test_connect_omron_creates_person_when_first_account(tmp_path):
    path = tmp_path / "config.yaml"
    gui_logic.connect_omron(path, "Kim", "kim@example.com", "pw", "gb")
    assert _users(path) == [{"name": "Kim", "omron": {"email": "kim@example.com", "country": "GB"}}]


def test_connect_omron_keeps_advanced_settings_when_the_email_is_unchanged(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({"users": [{"name": "Kim", "garmin": {"email": "k@example.com"},
                                          "omron": {"email": "kim@example.com", "country": "QA", "server": "eu"}}]}))
    gui_logic.connect_omron(path, "Kim", "kim@example.com", "new-pw", "QA")
    assert _by_name(path)["Kim"]["omron"] == {"email": "kim@example.com", "country": "QA", "server": "eu"}


def test_disconnect_garmin_removes_section_password_and_token(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    store_token("garmin:adult-b@example.com", {"di_token": "t"})
    assert gui_logic.disconnect_garmin(path, "Jane") is True
    assert _by_name(path)["Jane"] == {"name": "Jane"}
    assert get_password("Jane:garmin") is None
    assert get_token("garmin:adult-b@example.com") is None


def test_disconnect_scale_removes_section_and_password_keeps_shared_token(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    store_token("eufy:scale@example.com", {"access_token": "shared"})
    assert gui_logic.disconnect_scale(path, "Sam") is True
    assert "eufy" not in _by_name(path)["Sam"]
    assert get_password("Sam:eufy") is None
    assert get_token("eufy:scale@example.com") == {"access_token": "shared"}


def test_disconnect_scale_deletes_token_when_last_user_of_that_login(tmp_path):
    path = tmp_path / "config.yaml"
    gui_logic.connect_scale(path, "Sam", "solo@example.com", "pw", "adult-b-0002")
    store_token("eufy:solo@example.com", {"access_token": "t"})
    gui_logic.disconnect_scale(path, "Sam")
    assert get_token("eufy:solo@example.com") is None


def test_disconnect_last_account_leaves_name_only_person(tmp_path):
    path = tmp_path / "config.yaml"
    gui_logic.connect_garmin(path, "Jane", "b@example.com", "pw")
    gui_logic.disconnect_garmin(path, "Jane")
    assert _users(path) == [{"name": "Jane"}]


def test_disconnect_garmin_and_scale_noop_when_not_connected(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    before = path.read_text()
    assert gui_logic.disconnect_garmin(path, "Sam") is False
    assert gui_logic.disconnect_scale(path, "Jane") is False
    assert gui_logic.disconnect_garmin(path, "Nobody") is False
    assert path.read_text() == before


def test_disconnect_leaves_state_db_alone(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    db = tmp_path / "state.db"
    db.write_bytes(b"history")
    gui_logic.disconnect_garmin(path, "Chris")
    gui_logic.disconnect_scale(path, "Chris")
    assert db.read_bytes() == b"history"


def test_disconnect_confirmation_texts():
    assert gui_logic.garmin_disconnect_confirmation_text("Jane") == (
        "Disconnect Garmin for Jane? Nothing already in Garmin is removed. Syncing for Jane stops until Garmin is "
        "connected again.")
    assert gui_logic.scale_disconnect_confirmation_text("Jane").startswith("Disconnect the scale for Jane?")
    assert "profile becomes free to link again" in gui_logic.scale_disconnect_confirmation_text("Jane")


def test_scale_login_again_uses_fresh_login_and_reports_profile_presence(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    with patch("homevitals.gui_logic.EufyClient") as client_cls:
        client = client_cls.return_value
        client.list_profiles.return_value = [_profile("adult-a-0001", 80.0, 30)]
        result = gui_logic.scale_login_again(path, "Chris")
        assert result.ok and client._fresh_login.called
        result = gui_logic.scale_login_again(path, "Sam")
        assert not result.ok and "was not found" in result.text
        # A scale login without a chosen profile (an old one-person install): offer Choose profile.
        legacy = tmp_path / "legacy.yaml"
        legacy.write_text(yaml.safe_dump({"users": [{"name": "Chris", "eufy": {"email": "scale@example.com"},
                                                     "garmin": {"email": "adult-a@example.com"}}]}),
                          encoding="utf-8")
        store_password("Chris:eufy", "pw")
        store_password("Chris:garmin", "pw")
        result = gui_logic.scale_login_again(legacy, "Chris")
        assert not result.ok and result.action == "choose_profile"
    with pytest.raises(ValueError, match="No person named 'Jane' with a scale connected"):
        gui_logic.scale_login_again(path, "Jane")


def test_omron_login_again_forces_login_and_checks_connection(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    with patch("homevitals.gui_logic.OmronClient") as client_cls:
        result = gui_logic.omron_login_again(path, "Chris")
    client = client_cls.return_value
    client.authenticate.assert_called_once_with(force_login=True)
    client.check_connection.assert_called_once()
    client.close.assert_called_once()
    assert result == gui_logic.omron_ok_result("Chris")
    with pytest.raises(ValueError, match="with a blood pressure monitor connected"):
        gui_logic.omron_login_again(path, "Jane")


def test_list_profiles_for_person_greys_out_other_peoples_profiles(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    with patch("homevitals.gui_logic.EufyClient") as client_cls:
        client_cls.return_value.list_profiles.return_value = [_profile("adult-a-0001", 80.0, 30),
                                                              _profile("adult-b-0002", 61.9, 29),
                                                              _profile("kid-c-0003", 30.4, 28)]
        choices = gui_logic.list_profiles_for_person(path, "Chris")
    assert [(c.customer_id, c.linked_to) for c in choices] == [("adult-a-0001", None), ("adult-b-0002", "Sam"),
                                                                ("kid-c-0003", None)]
    client_cls.return_value.close.assert_called_once()


def test_not_ready_result_texts():
    from homevitals.config import GarminConfig, UserConfig
    jane = UserConfig(name="Jane", eufy=None, garmin=GarminConfig("b@example.com", "pw"))
    assert gui_logic.not_ready_result(jane) == CheckResult(
        False, "Jane isn't fully set up yet: connect the scale or the blood pressure monitor so there is something "
               "to sync.", "Jane", "open_person")
    kim = UserConfig(name="Kim", eufy=None)
    assert gui_logic.not_ready_result(kim).text == (
        "Kim isn't set up yet: connect Garmin and the scale or the blood pressure monitor.")


def test_run_checks_adds_not_ready_row_with_open_person_action(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    with patch("homevitals.gui_logic.OmronClient"):
        results, *_ = _run(path)
    rows = [r for r in results if r.action == "open_person"]
    assert [r.person for r in rows] == ["Jane", "Sam"]


def test_run_checks_skips_eufy_checks_for_people_without_scale(fixtures, tmp_path):
    path = _partial(tmp_path, fixtures)
    with patch("homevitals.gui_logic.OmronClient"):
        results, _, eufy_ctor, garmin = _run(path)
    assert eufy_ctor.call_count == 1                 # one shared Eufy login (Chris and Sam)
    assert not [r for r in results if r.person == "Jane" and "Eufy" in r.text]
    assert [c.args[0].email for c in garmin.call_args_list] == ["adult-a@example.com", "adult-b@example.com"]


def test_run_checks_ready_household_has_no_not_ready_rows(fixtures, tmp_path):
    path = _household(tmp_path, fixtures)
    results, *_ = _run(path)
    assert not [r for r in results if r.action == "open_person"]


def test_translate_not_ready_lines_pass_through_and_are_not_nothing_new():
    for line in ("Jane: not fully set up yet (connect the scale or the blood pressure monitor).",
                 "Kim: not set up yet (connect Garmin and the scale or the blood pressure monitor)."):
        people = [Person("Jane", None, "b@example.com", None), Person("Kim", None, None, None)]
        assert gui_logic.translate_line(line, people) == line
        assert gui_logic.line_means_nothing_new(line) is False


def test_translate_no_new_measurements_singular_profile():
    assert _t("No new measurements for 1 profile.") == "Nothing new to sync for anyone."


def test_translate_error_incomplete_sections():
    assert gui_logic.translate_error("User 'Jane' has an eufy section without an email. Fix ...") == (
        "Jane's scale settings are incomplete. Open Jane in the window and connect the scale again.")
    assert gui_logic.translate_error("User 'Jane' has a garmin section without an email. Fix ...") == (
        "Jane's Garmin settings are incomplete. Open Jane in the window and connect Garmin again.")


def test_config_problem_none_for_partial_household(fixtures):
    assert gui_logic.config_problem(fixtures.path("config_household_partial.yaml")) is None


def test_remove_last_person_deletes_legacy_tokens_and_files(tmp_path):
    from homevitals.cli import shared
    path = tmp_path / "config.yaml"
    gui_logic.connect_garmin(path, "Jane", "b@example.com", "pw")
    store_token("garmin", {"di_token": "legacy"})
    store_token("eufy", {"access_token": "legacy"})
    shared.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("session.json", "eufy_token.json"):
        (shared.DATA_DIR / name).write_text("{}")
    with patch("homevitals.gui_logic.platform_support.uninstall_agent"):
        gui_logic.remove_person(path, "Jane")
    assert get_token("garmin") is None and get_token("eufy") is None
    assert not (shared.DATA_DIR / "session.json").exists() and not (shared.DATA_DIR / "eufy_token.json").exists()


# ---------------------------------------------------------------------------
# Progress symbols in the people list (owner request, 2026-10-02)
# ---------------------------------------------------------------------------


def test_parse_progress_reads_only_well_formed_markers():
    assert gui_logic.parse_progress("@progress\tChris\tscale\tstart") == gui_logic.ProgressEvent("Chris", "scale", "start")
    assert gui_logic.parse_progress("@progress\tMary Ann\tbp\tdone\t3\r\n") == \
        gui_logic.ProgressEvent("Mary Ann", "bp", "done", 3)
    for line in ("Chris: synced 2 weigh-ins to Garmin.", "@progress\tChris\tweight\tstart",
                 "@progress\tChris\tscale\tmaybe", "@progress Chris scale start"):
        assert gui_logic.parse_progress(line) is None


def test_progress_markers_are_never_shown_as_text():
    assert gui_logic.translate_line("@progress\tChris\tscale\tdone\t2", _people()) is None
    assert not gui_logic.line_means_nothing_new("@progress\tChris\tscale\tdone\t0")


def test_sync_plan_waits_only_for_what_can_sync(fixtures, tmp_path):
    import shutil
    path = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_partial.yaml"), path)
    plan = gui_logic.sync_plan(gui_logic.read_people(path))
    # Chris: scale and monitor; Jane: Garmin only (nothing to sync); Sam: no Garmin.
    assert plan == {("Chris", "scale"): ("waiting", None), ("Chris", "bp"): ("waiting", None)}


def test_progress_cells_through_a_run():
    chris = gui_logic.Person("Chris", "scale@example.com", "a@example.com", "adult-a-0001", True, "a@example.com", "GB")
    progress = gui_logic.sync_plan([chris])
    assert gui_logic.person_row(chris, progress)[2:] == ("...0001   ⋯ waiting", "on   ⋯ waiting")
    gui_logic.apply_progress(progress, gui_logic.ProgressEvent("Chris", "scale", "start"))
    assert gui_logic.progress_running(progress)
    frames = {gui_logic.person_row(chris, progress, frame)[2] for frame in range(4)}
    assert frames == {f"...0001   {c} syncing" for c in gui_logic.SPINNER}
    gui_logic.apply_progress(progress, gui_logic.ProgressEvent("Chris", "scale", "done", 2))
    gui_logic.apply_progress(progress, gui_logic.ProgressEvent("Chris", "bp", "start"))
    gui_logic.apply_progress(progress, gui_logic.ProgressEvent("Chris", "bp", "done", 0))
    assert gui_logic.person_row(chris, progress)[2:] == ("...0001   ✓ 2 new", "on   ✓ up to date")
    assert not gui_logic.progress_running(progress)
    gui_logic.apply_progress(progress, gui_logic.ProgressEvent("Chris", "bp", "failed"))
    assert gui_logic.person_row(chris, progress)[3] == "on   ✗ failed"
    # Without progress the cells are the plain ones.
    assert gui_logic.person_row(chris) == ("Chris", "a@example.com", "...0001", "on")


def test_finish_progress_drops_steps_the_run_never_reached():
    progress = {("Chris", "scale"): ("done", 1), ("Chris", "bp"): ("syncing", None), ("Jane", "scale"): ("waiting", None)}
    gui_logic.finish_progress(progress)
    assert progress == {("Chris", "scale"): ("done", 1)}


def test_sync_environment_streams_output_with_progress_markers():
    env = gui_logic.sync_environment()
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["EUFY_SYNC_PROGRESS"] == "1"


def test_row_state_picks_the_most_important_state():
    progress = {("Chris", "scale"): ("done", 1), ("Chris", "bp"): ("syncing", None),
                ("Jane", "scale"): ("failed", None), ("Jane", "bp"): ("done", 0), ("Sam", "scale"): ("done", 0)}
    assert gui_logic.row_state(progress, "Chris") == "syncing"
    assert gui_logic.row_state(progress, "Jane") == "failed"
    assert gui_logic.row_state(progress, "Sam") == "done"
    assert gui_logic.row_state(progress, "Nobody") is None


# ---------------------------------------------------------------------------
# The rename to HomeVitals (household.5)
# ---------------------------------------------------------------------------


def test_only_the_new_launchers_are_installed():
    import tomllib
    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "homevitals"
    assert data["project"]["scripts"] == {"homevitals": "homevitals.cli:main"}
    assert data["project"]["gui-scripts"] == {"homevitals-gui": "homevitals.gui:main"}
    assert gui_logic.SHORTCUT_NAME == "HomeVitals"


def test_shortcut_plan_uses_a_full_path_even_when_which_answers_a_relative_one(tmp_path, monkeypatch):
    launcher = tmp_path / "homevitals-gui.EXE"
    launcher.write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gui_logic.shutil, "which", lambda name: ".\\homevitals-gui.EXE")
    plan = gui_logic.shortcut_plan()
    assert Path(plan.target).is_absolute() and Path(plan.target) == launcher.resolve()
    assert Path(plan.working_dir) == tmp_path.resolve()


def test_startup_box_sees_the_old_startup_shortcut_and_removes_both(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = gui_logic.startup_shortcut_path().parent
    folder.mkdir(parents=True)
    old = folder / "Household scale sync.lnk"
    old.write_bytes(b"")
    assert gui_logic.startup_enabled()
    gui_logic.disable_startup()
    assert not old.exists() and not gui_logic.startup_enabled()


def test_shortcut_retarget_script_keeps_the_file_and_changes_the_target():
    plan = gui_logic.ShortcutPlan(r"C:\bin\homevitals-gui.exe", "", r"C:\bin")
    lnk = Path(r"C:\Pinned\Household scale sync.lnk")
    script = gui_logic.shortcut_retarget_script(lnk, plan, Path(r"C:\i\app_icon.ico"))
    assert "CreateShortcut('C:\\Pinned\\Household scale sync.lnk')" in script
    assert "$s.TargetPath = 'C:\\bin\\homevitals-gui.exe'" in script
    assert "IconLocation = 'C:\\i\\app_icon.ico,0'" in script


def test_omron_login_rejected_text_asks_for_email_password_and_the_sign_up_country():
    text = gui_logic.omron_login_rejected_text("Chris", "CA")
    assert text.startswith("OMRON connect didn't accept Chris's login. Check three things: the email, the password, "
                           "and the country.")
    assert "the one chosen when this OMRON connect account was first created" in text
    assert text.endswith("You picked Canada.")
    assert "You picked" not in gui_logic.omron_login_rejected_text("Chris")
