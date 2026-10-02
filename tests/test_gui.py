"""Smoke tests for the tkinter window (homevitals/gui.py).

The window's logic is tested in test_gui_logic.py. These only check that the
window opens, shows the right state and closes. Each one skips when there is
no screen.
"""
from __future__ import annotations

import gc
import shutil

import pytest

tk = pytest.importorskip("tkinter")

from tkinter import ttk  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from homevitals import garmin_auth, gui  # noqa: E402


@pytest.fixture(scope="session")
def _tk_root():
    """One Tk for the whole run. Creating and destroying Tk over and over on
    Windows sometimes fails to read Tk's own library files, so the smoke tests
    share one and each gets its own child window. A few tries before skipping."""
    import time
    error = None
    for _ in range(5):
        try:
            r = tk.Tk()
            break
        except tk.TclError as e:
            error = e
            time.sleep(0.3)
    else:
        pytest.skip(f"no display ({error})")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def root(_tk_root, monkeypatch):
    # The autosync box asks the platform; keep the smoke tests off schtasks.
    monkeypatch.setattr("homevitals.gui.platform_support.agent_installed", lambda: False)
    # Old windows are freed here, on the main thread. Freed by a worker thread instead, each Tk
    # variable waits a second for a main loop the tests never run, and the handler tests time out.
    gc.collect()
    top = tk.Toplevel(_tk_root)
    top.withdraw()
    yield top
    try:
        top.destroy()
    except tk.TclError:
        pass
    gc.collect()


def test_window_opens_and_closes(root, tmp_path):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    root.update()
    assert app.root is root


def test_empty_state_disables_sync_and_fix_buttons(root, tmp_path):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    root.update()
    assert app.people == []
    for button in (app.sync_button, app.remove_button, app.fix_button):
        assert str(button["state"]) == "disabled"
    assert str(app.add_button["state"]) == "normal"
    assert "Nobody is set up yet" in app.empty_label["text"]


def test_people_listed_from_fixture_config(root, tmp_path, fixtures):
    config = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household.yaml"), config)
    app = gui.App(root, config_path=config)
    root.update()
    rows = [app.tree.item(i)["values"] for i in app.tree.get_children()]
    assert [r[0] for r in rows] == ["Chris", "Jane"]
    assert rows[0][1] == "adult-a@example.com"
    assert rows[0][2] == "...0001"
    assert str(app.sync_button["state"]) == "normal"


def test_install_mfa_bridge_sets_override_and_reset_fixture_clears_it(root, tmp_path):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    gui.install_mfa_bridge(app)
    assert garmin_auth.MFA_PROMPT_OVERRIDE is not None
    assert garmin_auth.MFA_PROMPT_OVERRIDE.__self__.__class__.__name__ == "MfaBridge"


def test_say_appends_to_message_area(root, tmp_path):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    app.say("Hello there.")
    assert "Hello there." in app.messages.get("1.0", "end")


def test_post_runs_callable_on_the_main_loop(root, tmp_path):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    seen = []
    app.post(lambda: seen.append("ran"))
    app._drain_queue()
    assert seen == ["ran"]


def test_report_callback_exception_logs_and_says_generic_text(caplog):
    said = []
    try:
        raise RuntimeError("secret detail")
    except RuntimeError as e:
        gui.report_callback_exception(type(e), e, e.__traceback__, say=said.append)
    assert said == ["Something went wrong. Details were saved to the log file (~/.homevitals/sync.log)."]
    assert "secret detail" in caplog.text


def test_gui_main_is_the_launcher_entry_point():
    import tomllib
    from pathlib import Path
    pyproject = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["gui-scripts"]["homevitals-gui"] == "homevitals.gui:main"
    assert callable(gui.main)


# ---------------------------------------------------------------------------
# Part 4: blood pressure in the window (WP20)
# ---------------------------------------------------------------------------


def test_people_list_shows_blood_pressure_column(root, tmp_path, fixtures):
    config = tmp_path / "config.yaml"
    shutil.copy(fixtures.path("config_household_omron.yaml"), config)
    app = gui.App(root, config_path=config)
    rows = [app.tree.item(i)["values"] for i in app.tree.get_children()]
    assert [r[3] for r in rows] == ["on", "on"]
    shutil.copy(fixtures.path("config_household.yaml"), config)
    app.refresh_people()
    rows = [app.tree.item(i)["values"] for i in app.tree.get_children()]
    assert [r[3] for r in rows] == ["off", "off"]


def test_people_list_survives_duplicate_names(root, tmp_path):
    import yaml
    config = tmp_path / "config.yaml"
    user = {"name": "Chris", "eufy": {"email": "e@example.com", "customer_id": "a"}, "garmin": {"email": "a@example.com"}}
    config.write_text(yaml.dump({"users": [user, dict(user, garmin={"email": "b@example.com"})]}))
    app = gui.App(root, config_path=config)
    assert len(app.tree.get_children()) == 2
    assert "same name" in app.messages.get("1.0", "end")


def test_edit_person_button_disabled_in_empty_state(root, tmp_path):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    assert str(app.edit_button["state"]) == "disabled"






def test_omron_form_defaults_to_canada_and_parses_typed_code(root):
    form = gui.OmronForm(root)
    assert form.country.get() == "Canada (CA)"
    form.email_var.set(" adult-a@example.com ")
    form.password_var.set("pw")
    assert form.values() == ("adult-a@example.com", "pw", "CA")
    form.country.set("qa")
    assert form.values()[2] == "QA"
    assert form.validate([], None) is None
    form.password_var.set("")
    assert form.validate([], None) == "Type the OMRON connect password."
    form.prefill("o@example.com", "GB")
    assert form.email_var.get() == "o@example.com" and form.country.get() == "United Kingdom (GB)"
    assert form.password_var.get() == ""


def test_closing_the_window_is_logged(root, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO)
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    app.on_close()
    assert "Window closed by the user" in caplog.text


def test_crash_log_records_hard_crashes(tmp_path, monkeypatch):
    import faulthandler
    monkeypatch.setattr("homevitals.cli.shared.DATA_DIR", tmp_path)
    try:
        gui.enable_crash_log()
        assert faulthandler.is_enabled()
        assert (tmp_path / "crash.log").exists()
    finally:
        faulthandler.disable()




# ---------------------------------------------------------------------------
# Logo and "open when Windows starts" (owner request, 2026-10-02)
# ---------------------------------------------------------------------------


def test_window_uses_the_app_icon(root, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(root, "iconbitmap", lambda *a, **kw: seen.append(kw.get("default") or (a[0] if a else None)))
    gui.App(root, config_path=tmp_path / "config.yaml")
    assert seen and str(seen[0]).endswith("app_icon.ico")


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows only")
def test_startup_box_creates_and_removes_the_startup_shortcut(root, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    made = []
    monkeypatch.setattr(gui, "create_shortcut", lambda plan, folder: made.append(folder))
    monkeypatch.setattr(gui.gui_logic, "shortcut_plan", lambda: gui.gui_logic.ShortcutPlan("t.exe", "", "."))
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    assert app.startup_var.get() is False
    app.startup_var.set(True)
    app.on_startup_toggle()
    app._drain_until_idle()
    assert made == ["Startup"]
    path = gui.gui_logic.startup_shortcut_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"lnk")
    app.startup_var.set(False)
    app.on_startup_toggle()
    assert not path.exists()


def test_desktop_shortcut_uses_the_logo(monkeypatch):
    scripts = []

    class Done:
        returncode = 0
        stderr = ""
        stdout = "C:\\Desk\\Household scale sync.lnk\r\n"

    def fake_run(cmd, **kwargs):
        import base64
        scripts.append(base64.b64decode(cmd[-1]).decode("utf-16-le"))
        return Done()

    tagged = []
    monkeypatch.setattr(gui.subprocess, "run", fake_run)
    monkeypatch.setattr("homevitals.win_appid.set_shortcut_app_id", tagged.append)
    gui.create_shortcut(gui.gui_logic.ShortcutPlan("t.exe", "", "."), "Desktop")
    assert "GetFolderPath('Desktop')" in scripts[0]
    assert "app_icon.ico,0" in scripts[0]
    # The new shortcut shares the window's taskbar button.
    assert tagged == ["C:\\Desk\\Household scale sync.lnk"]


# ---------------------------------------------------------------------------
# Running in the background (tray icon) (owner request, 2026-10-02)
# ---------------------------------------------------------------------------


class _FakeTray:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_close_hides_to_the_tray_when_there_is_one(root, tmp_path, caplog, monkeypatch):
    import logging
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(gui.platform_support, "notify", lambda *a, **kw: None)
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    app.tray = _FakeTray()
    app.on_close()
    assert root.winfo_exists()
    assert root.state() == "withdrawn"
    assert "hidden to the tray" in caplog.text
    app.show_window()
    assert root.state() == "normal"


def test_quit_from_the_tray_stops_it_and_closes(root, tmp_path, caplog, monkeypatch):
    import logging
    caplog.set_level(logging.INFO)
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    fake = _FakeTray()
    app.tray = fake
    destroyed = []
    monkeypatch.setattr(root, "destroy", lambda: destroyed.append(True))
    app.quit()
    assert fake.stopped and destroyed
    assert "closed by the user" in caplog.text


def test_background_flag():
    assert gui.wants_background(["homevitals-gui", "--background"]) is True
    assert gui.wants_background(["homevitals-gui"]) is False


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows only")
def test_startup_shortcut_starts_in_the_background(root, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    made = []
    monkeypatch.setattr(gui, "create_shortcut", lambda plan, folder: made.append((plan.arguments, folder)))
    monkeypatch.setattr(gui.gui_logic, "shortcut_plan", lambda: gui.gui_logic.ShortcutPlan("t.exe", "", "."))
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    app.startup_var.set(True)
    app.on_startup_toggle()
    app._drain_until_idle()
    assert made == [("--background", "Startup")]


# ---------------------------------------------------------------------------
# Part 5: the Person window (WP26)
# ---------------------------------------------------------------------------


def _app_with(root, tmp_path, fixtures, name="config_household_omron.yaml"):
    config = tmp_path / "config.yaml"
    shutil.copy(fixtures.path(name), config)
    return gui.App(root, config_path=config)


def test_person_dialog_new_person_has_name_entry_and_three_sections_not_connected(root, tmp_path):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    dialog = gui.PersonDialog(app, None)
    root.update()
    assert dialog.title() == "Add person"
    assert isinstance(dialog.name_widget, ttk.Entry)
    assert list(dialog.sections) == ["garmin", "eufy", "omron"]
    for section in dialog.sections.values():
        assert section.status_label["text"] == "Not connected."
        assert section.mode == "connect"
        assert set(section.buttons) == {"Connect"}
    dialog.close()


def test_person_dialog_existing_person_locks_name_and_shows_statuses(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures)
    dialog = gui.PersonDialog(app, app.people[0])
    root.update()
    assert dialog.title() == "Edit Chris"
    assert isinstance(dialog.name_widget, ttk.Label)
    assert [s.status_label["text"] for s in dialog.sections.values()] == [
        "Connected as adult-a@example.com.",
        "Connected as scale@example.com, profile ...0001.",
        "Connected as adult-a@example.com (Canada).",
    ]
    for section in dialog.sections.values():
        assert {"Log in again", "Change password", "Change account", "Disconnect"} <= set(section.buttons)
    dialog.close()


def test_person_dialog_legacy_scale_offers_choose_profile(root, tmp_path):
    import yaml
    config = tmp_path / "config.yaml"
    config.write_text(yaml.dump({"users": [{"name": "Chris", "eufy": {"email": "scale@example.com"},
                                            "garmin": {"email": "adult-a@example.com"}}]}))
    app = gui.App(root, config_path=config)
    dialog = gui.PersonDialog(app, app.people[0])
    eufy = dialog.sections["eufy"]
    assert eufy.status_label["text"] == "Connected as scale@example.com, no profile chosen yet."
    assert set(eufy.buttons) == {"Choose profile", "Change password", "Change account", "Disconnect"}
    dialog.close()


def test_person_dialog_blood_pressure_offers_older_readings_only_when_connected(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures)
    dialog = gui.PersonDialog(app, app.people[0])
    bp = dialog.sections["omron"]
    assert "Bring in older readings" in bp.buttons
    started = []
    app.start_sync = lambda **kw: started.append(kw)
    with patch("homevitals.gui.messagebox.askyesno", return_value=True):
        bp.on_older_readings()
    assert started == [{"older_bp_readings": True}]
    assert not dialog.winfo_exists()
    shutil.copy(fixtures.path("config_household.yaml"), app.config_path)
    app.refresh_people()
    other = gui.PersonDialog(app, app.people[0])
    assert set(other.sections["omron"].buttons) == {"Connect"}
    other.close()


def test_person_dialog_connect_garmin_saves_only_after_login_works(root, tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    app = gui.App(root, config_path=config)
    dialog = gui.PersonDialog(app, None)
    garmin = dialog.sections["garmin"]
    dialog.name_var.set("Jane")
    garmin.email_var.set("adult-b@example.com")
    garmin.password_var.set("pw-b")
    connects = []
    real_connect = gui.gui_logic.connect_garmin
    monkeypatch.setattr(gui.gui_logic, "connect_garmin", lambda *a: (connects.append(a[1:3]), real_connect(*a)))
    error = RuntimeError("Garmin rejected the email or password")

    def failing(email, password):
        raise error

    monkeypatch.setattr(gui.gui_logic, "check_garmin_login", failing)
    garmin.on_connect()
    app._drain_until_idle()
    assert connects == []
    assert not config.exists()
    assert garmin.message_label["text"] == gui.gui_logic.classify_garmin_error(error, "Jane").text
    assert not dialog.working and not dialog.changed
    assert dialog.title() == "Add person"

    monkeypatch.setattr(gui.gui_logic, "check_garmin_login", lambda email, password: None)
    garmin.on_connect()
    app._drain_until_idle()
    assert connects == [("Jane", "adult-b@example.com")]
    assert config.exists()
    assert [p.name for p in app.people] == ["Jane"]
    assert dialog.title() == "Edit Jane"
    assert isinstance(dialog.name_widget, ttk.Label)
    assert dialog.changed
    assert garmin.status_label["text"] == "Connected as adult-b@example.com."
    assert garmin.password_var.get() == ""
    assert "Garmin is connected for Jane." in app.messages.get("1.0", "end")
    assert dialog.sections["eufy"].mode == "connect"
    assert dialog.sections["omron"].mode == "connect"
    dialog.close()


def test_person_dialog_needs_a_name_before_connecting(root, tmp_path, monkeypatch):
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    dialog = gui.PersonDialog(app, None)
    called = []
    monkeypatch.setattr(gui.gui_logic, "check_garmin_login", lambda *a: called.append(a))
    garmin = dialog.sections["garmin"]
    garmin.email_var.set("adult-b@example.com")
    garmin.password_var.set("pw-b")
    garmin.on_connect()
    assert garmin.message_label["text"] == "Please type a name."
    dialog.name_var.set("Jane")
    garmin.password_var.set("")
    garmin.on_connect()
    assert garmin.message_label["text"] == "Please type the Garmin password."
    assert called == [] and not dialog.working
    dialog.close()


def test_person_dialog_refused_save_shows_plain_sentence(root, tmp_path, fixtures, monkeypatch):
    app = _app_with(root, tmp_path, fixtures, "config_household_partial.yaml")
    dialog = gui.PersonDialog(app, next(p for p in app.people if p.name == "Sam"))
    monkeypatch.setattr(gui.gui_logic, "check_garmin_login", lambda *a: None)

    def refuse(*a):
        raise ValueError("User 'Sam' and user 'Chris' both use the Garmin account x@example.com.")

    monkeypatch.setattr(gui.gui_logic, "connect_garmin", refuse)
    garmin = dialog.sections["garmin"]
    garmin.email_var.set("sam@example.com")
    garmin.password_var.set("pw")
    garmin.on_connect()
    app._drain_until_idle()
    assert garmin.message_label["text"] == ("Two people are set up with the same Garmin account. "
                                            "Each person needs their own.")
    assert not dialog.changed
    dialog.close()


def test_person_dialog_save_waits_while_a_sync_runs(root, tmp_path, monkeypatch):
    import contextlib
    app = gui.App(root, config_path=tmp_path / "config.yaml")
    dialog = gui.PersonDialog(app, None)
    monkeypatch.setattr(gui.gui_logic, "check_garmin_login", lambda *a: None)
    monkeypatch.setattr(gui.lock, "single_instance", lambda **kw: contextlib.nullcontext(False))
    dialog.name_var.set("Jane")
    garmin = dialog.sections["garmin"]
    garmin.email_var.set("adult-b@example.com")
    garmin.password_var.set("pw-b")
    garmin.on_connect()
    app._drain_until_idle()
    assert garmin.message_label["text"] == gui.SYNC_RUNNING
    assert not (tmp_path / "config.yaml").exists()
    dialog.close()


def test_person_dialog_scale_connect_picks_profile_and_saves(root, tmp_path, fixtures, monkeypatch):
    from datetime import datetime

    from homevitals.credentials import get_password, store_password
    from homevitals.eufy_client import EufyProfile
    app = _app_with(root, tmp_path, fixtures, "config_household_partial.yaml")
    store_password("Chris:eufy", "chris-eufy")
    jane = next(p for p in app.people if p.name == "Jane")
    dialog = gui.PersonDialog(app, jane)
    eufy = dialog.sections["eufy"]
    assert eufy.mode == "connect"
    # Her own Eufy account by default: nothing of anyone else's is filled in.
    assert eufy.email_var.get() == ""
    eufy.email_var.set("jane-eufy@example.com")
    eufy.password_var.set("jane-eufy-pw")
    logins = []
    profiles = [EufyProfile("jane-0003", datetime(2026, 9, 29), 60.0)]
    monkeypatch.setattr(gui.gui_logic, "check_eufy_login",
                        lambda email, pw, fresh: (logins.append((email, pw, fresh)), profiles)[1])
    eufy.on_connect()
    app._drain_until_idle()
    assert logins == [("jane-eufy@example.com", "jane-eufy-pw", True)]
    assert eufy.mode == "picker"
    assert eufy.message_label["text"] == ""     # the "Logging in..." line is gone
    assert [(c.customer_id, c.linked_to) for c in eufy.choices] == [("jane-0003", None)]
    eufy.picker.var.set("jane-0003")
    eufy.on_save_profile()
    app._drain_until_idle()
    jane = next(p for p in app.people if p.name == "Jane")
    assert jane.customer_id == "jane-0003" and jane.eufy_email == "jane-eufy@example.com"
    assert get_password("Jane:eufy") == "jane-eufy-pw"
    assert get_password("Chris:eufy") == "chris-eufy"       # someone else's login is never touched
    assert eufy.status_label["text"] == "Connected as jane-eufy@example.com, profile ...0003."
    assert eufy.draft is None
    text = app.messages.get("1.0", "end")
    assert "The scale is connected for Jane (profile ...0003)." in text
    assert "also updated" not in text
    dialog.close()


def test_scale_box_never_uses_someone_elses_saved_eufy_password(root, tmp_path, fixtures, monkeypatch):
    from homevitals.credentials import store_password
    app = _app_with(root, tmp_path, fixtures, "config_household_partial.yaml")
    store_password("Chris:eufy", "chris-eufy")
    dialog = gui.PersonDialog(app, next(p for p in app.people if p.name == "Jane"))
    eufy = dialog.sections["eufy"]
    assert not hasattr(eufy, "reuse_var") and not hasattr(eufy, "reuse_box")
    called = []
    monkeypatch.setattr(gui.gui_logic, "check_eufy_login", lambda *a, **kw: called.append(a))
    # Typing Chris's Eufy email is not enough to open his account: its password must be typed too.
    eufy.email_var.set("scale@example.com")
    eufy.on_connect()
    assert eufy.message_label["text"] == "Please type the Eufy password."
    assert called == []
    assert not hasattr(gui.gui_logic, "saved_eufy_password")
    dialog.close()


def test_person_dialog_scale_password_change_keeps_profile_without_picker(root, tmp_path, fixtures, monkeypatch):
    from datetime import datetime

    from homevitals.credentials import get_password
    from homevitals.eufy_client import EufyProfile
    app = _app_with(root, tmp_path, fixtures)
    dialog = gui.PersonDialog(app, app.people[0])
    eufy = dialog.sections["eufy"]
    eufy.show_form("password")
    eufy.password_var.set("new-eufy")
    fresh_flags = []
    monkeypatch.setattr(gui.gui_logic, "check_eufy_login", lambda email, pw, fresh: (
        fresh_flags.append(fresh), [EufyProfile("adult-a-0001", datetime(2026, 9, 30), 80.0)])[1])
    eufy.on_connect()
    app._drain_until_idle()
    assert fresh_flags == [True]
    assert get_password("Chris:eufy") == "new-eufy" and get_password("Jane:eufy") == "new-eufy"
    text = app.messages.get("1.0", "end")
    assert "The scale is connected for Chris (profile ...0001)." in text
    assert "The Eufy password was also updated for Jane (same Eufy login)." in text
    assert "new-eufy" not in text
    dialog.close()


def test_person_dialog_password_mode_locks_identity_fields(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures)
    dialog = gui.PersonDialog(app, app.people[0])
    for service, email in (("garmin", "adult-a@example.com"), ("eufy", "scale@example.com")):
        section = dialog.sections[service]
        section.show_form("password")
        assert str(section.email_entry["state"]) == "readonly"
        assert section.email_var.get() == email
        assert set(section.buttons) == {"Connect", "Cancel"}
    bp = dialog.sections["omron"]
    bp.show_form("password")
    assert str(bp.form.email_entry["state"]) == "readonly"
    assert str(bp.form.country["state"]) == "disabled"
    assert bp.form.values()[0] == "adult-a@example.com" and bp.form.values()[2] == "CA"
    bp.show_form("account")
    assert str(bp.form.email_entry["state"]) == "normal"
    bp.form.password_var.set("typed")
    bp.on_cancel()
    assert bp.mode == "connected" and bp.form is None
    dialog.close()


def test_person_dialog_start_runs_login_again(root, tmp_path, fixtures, monkeypatch):
    app = _app_with(root, tmp_path, fixtures)
    calls = []
    monkeypatch.setattr(gui.gui_logic, "garmin_login_again", lambda path, name: calls.append(name))
    dialog = gui.PersonDialog(app, app.people[0], start=("garmin", "login_again"))
    root.update()
    app._drain_until_idle()
    assert calls == ["Chris"]
    assert app.current_login_email == "adult-a@example.com"
    assert dialog.sections["garmin"].message_label["text"] == "Garmin login for Chris is working again."
    dialog.close()


def test_person_dialog_start_opens_password_form(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures)
    dialog = gui.PersonDialog(app, app.people[1], start=("omron", "password"))
    root.update()
    assert dialog.sections["omron"].mode == "password"
    dialog.close()


def test_person_dialog_close_reports_changed_to_callback(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures)
    seen = []
    dialog = gui.PersonDialog(app, app.people[0], on_close=seen.append)
    dialog.close()
    assert seen == [False]
    second = gui.PersonDialog(app, app.people[0], on_close=seen.append)
    with patch("homevitals.gui.messagebox.askyesno", return_value=True):
        second.sections["omron"].on_disconnect()
    assert second.changed
    assert second.sections["omron"].mode == "connect"
    assert "Blood pressure syncing is off for Chris." in app.messages.get("1.0", "end")
    second.close()
    assert seen == [False, True]


def test_person_dialog_disconnect_cancelled_changes_nothing(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures)
    dialog = gui.PersonDialog(app, app.people[0])
    with patch("homevitals.gui.messagebox.askyesno", return_value=False):
        dialog.sections["garmin"].on_disconnect()
    assert not dialog.changed
    assert app.people[0].garmin_email == "adult-a@example.com"
    dialog.close()


def test_fix_problems_person_actions_open_the_person_dialog(root, tmp_path, fixtures, monkeypatch):
    from homevitals.gui_logic import CheckResult
    app = _app_with(root, tmp_path, fixtures)
    monkeypatch.setattr(gui.FixProblemsDialog, "run", lambda self: None)
    opened = MagicMock()
    monkeypatch.setattr(gui, "PersonDialog", opened)
    fix = gui.FixProblemsDialog(app)
    label = ttk.Label(fix.rows)
    fix.on_action(CheckResult(False, "x", "Chris", "garmin_login"), label)
    assert opened.call_args.args[1].name == "Chris"
    assert opened.call_args.kwargs["start"] == ("garmin", "login_again")
    assert opened.call_args.kwargs["on_close"] == fix._after_person_dialog
    for action, start in (("change_eufy_password", ("eufy", "password")), ("choose_profile", ("eufy", "choose_profile")),
                          ("change_omron_password", ("omron", "password")), ("open_person", None)):
        fix.on_action(CheckResult(False, "x", "Jane", action), label)
        assert opened.call_args.kwargs["start"] == start
    assert fix.ACTION_LABELS["open_person"] == "Open"
    runs = []
    monkeypatch.setattr(gui.FixProblemsDialog, "run", lambda self: runs.append(1))
    fix._after_person_dialog(False)
    fix._after_person_dialog(True)
    assert runs == [1]
    fix.close()


def test_people_list_shows_not_connected_cells(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures, "config_household_partial.yaml")
    rows = [tuple(app.tree.item(i)["values"]) for i in app.tree.get_children()]
    assert rows == [("Chris", "adult-a@example.com", "...0001", "on"),
                    ("Jane", "adult-b@example.com", "not connected", "off"),
                    ("Sam", "not connected", "...0002", "off")]
    assert app.tree.bind("<Double-1>")


def test_old_dialogs_are_gone():
    for name in ("AddPersonDialog", "EditPersonDialog", "ChooseProfileDialog"):
        assert not hasattr(gui, name)


def test_closing_mid_login_reports_only_a_real_save(root, tmp_path, fixtures, monkeypatch):
    from datetime import datetime

    from homevitals.credentials import get_password
    from homevitals.eufy_client import EufyProfile
    app = _app_with(root, tmp_path, fixtures)
    dialog = gui.PersonDialog(app, app.people[0])
    eufy = dialog.sections["eufy"]
    eufy.show_form("password")
    eufy.password_var.set("new-eufy")
    # Chris's profile is gone from the account, so this login ends at the picker and saves nothing.
    monkeypatch.setattr(gui.gui_logic, "check_eufy_login",
                        lambda email, pw, fresh: [EufyProfile("adult-c-0003", datetime(2026, 9, 29), 60.0)])
    eufy.on_connect()
    dialog.close()
    app._drain_until_idle()
    assert "The scale is connected" not in app.messages.get("1.0", "end")
    assert get_password("Chris:eufy") is None


# ---------------------------------------------------------------------------
# Progress symbols in the people list (owner request, 2026-10-02)
# ---------------------------------------------------------------------------


def test_sync_now_draws_progress_symbols_and_keeps_markers_out_of_messages(root, tmp_path, fixtures, monkeypatch):
    import queue
    import threading
    import time

    app = _app_with(root, tmp_path, fixtures)
    release = threading.Event()
    lines = ["@progress\tChris\tscale\tstart\n", "@progress\tChris\tscale\tdone\t2\n",
             "@progress\tChris\tbp\tstart\n", "@progress\tChris\tbp\tdone\t0\n",
             "Chris: synced 2 weigh-ins to Garmin.\n",
             "@progress\tJane\tscale\tstart\n", "@progress\tJane\tscale\tfailed\n",
             "@progress\tJane\tbp\tstart\n"]

    class FakeProc:
        def __init__(self):
            def out():
                yield from lines[:1]
                release.wait(5)          # the test looks at the list mid-run
                yield from lines[1:]
            self.stdout = out()

        def wait(self):
            return 1

    monkeypatch.setattr(gui.subprocess, "Popen", lambda *a, **kw: FakeProc())

    def pump(until, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            try:
                app._queue.get(timeout=0.05)()
            except queue.Empty:
                pass

    def cells():
        return {app.tree.item(i)["values"][0]: tuple(app.tree.item(i)["values"][2:])
                for i in app.tree.get_children()}

    app.start_sync()
    assert cells()["Jane"] == ("...0002   ⋯ waiting", "on   ⋯ waiting")
    pump(lambda: app.progress.get(("Chris", "scale"), ("",))[0] == "syncing")
    assert cells()["Chris"][0].endswith(" syncing")
    release.set()
    pump(lambda: not app.busy)
    assert cells()["Chris"] == ("...0001   ✓ 2 new", "on   ✓ up to date")
    # Jane's monitor step never finished (the run stopped): its symbol goes.
    assert cells()["Jane"] == ("...0002   ✗ failed", "on")
    text = app.messages.get("1.0", "end")
    assert "@progress" not in text
    assert "synced 2 weigh-ins" in text
    tags = {app.tree.item(i)["values"][0]: tuple(app.tree.item(i)["tags"]) for i in app.tree.get_children()}
    assert tags == {"Chris": ("done",), "Jane": ("failed",)}
    # A refresh of the list (for example after editing someone) keeps the results.
    app.refresh_people()
    assert cells()["Chris"] == ("...0001   ✓ 2 new", "on   ✓ up to date")


def test_password_boxes_have_a_show_button_and_start_hidden(root, tmp_path, fixtures):
    app = _app_with(root, tmp_path, fixtures, "config_household_partial.yaml")
    dialog = gui.PersonDialog(app, None)
    for service in ("garmin", "eufy", "omron"):
        entry = dialog.sections[service].password_entry
        assert str(entry.cget("show")) == "*"
        entry.show_button.invoke()
        assert str(entry.cget("show")) == "" and entry.show_button["text"] == "Hide"
        entry.show_button.invoke()
        assert str(entry.cget("show")) == "*" and entry.show_button["text"] == "Show"
    # Revealed, then the form is redrawn: hidden again.
    garmin = dialog.sections["garmin"]
    garmin.password_entry.show_button.invoke()
    garmin.render()
    assert str(garmin.password_entry.cget("show")) == "*"
    dialog.close()



@pytest.mark.parametrize("old", ["Health Sync for Garmin", "Household scale sync"])
def test_old_shortcuts_move_to_the_new_name(tmp_path, monkeypatch, old):
    from homevitals import win_appid
    desktop, startup, pinned = tmp_path / "Desktop", tmp_path / "Startup", tmp_path / "User Pinned" / "TaskBar"
    for folder in (desktop, startup, pinned):
        folder.mkdir(parents=True)
        (folder / f"{old}.lnk").write_bytes(b"")
    monkeypatch.setattr(win_appid, "our_shortcuts", lambda name: [
        f / f"{name}.lnk" for f in (desktop, startup, pinned) if (f / f"{name}.lnk").exists()])
    monkeypatch.setattr(gui.gui_logic, "startup_shortcut_path", lambda: startup / "HomeVitals.lnk")
    made, scripts, tagged = [], [], []
    monkeypatch.setattr(gui, "create_shortcut", lambda plan, folder: made.append((folder, plan.arguments)))
    monkeypatch.setattr(gui, "_run_shortcut_script", scripts.append)
    monkeypatch.setattr(win_appid, "set_shortcut_app_id", lambda lnk: tagged.append(lnk) or True)
    moved = []
    monkeypatch.setattr(gui.platform_support, "migrate_agent", lambda: moved.append(1) or True)
    gui.move_to_new_name(gui.gui_logic.ShortcutPlan(r"C:\bin\homevitals-gui.exe", "", r"C:\bin"))
    # Desktop and Startup are made again under the new name (Startup still starts in the tray).
    assert sorted(made) == [("Desktop", ""), ("Startup", "--background")]
    assert not (desktop / f"{old}.lnk").exists()
    assert not (startup / f"{old}.lnk").exists()
    # The pinned copy keeps its file (so the pin stays); the repair step checks where it points.
    assert (pinned / f"{old}.lnk").exists()
    assert scripts == [] and tagged == []
    assert moved == [1]


def test_window_title_is_the_new_name(root, tmp_path):
    gui.App(root, config_path=tmp_path / "config.yaml")
    assert root.title() == "HomeVitals"


def test_start_up_fixes_shortcuts_that_point_at_the_wrong_program(tmp_path, monkeypatch):
    from homevitals import win_appid
    good, bad = tmp_path / "HomeVitals.lnk", tmp_path / "HomeVitals (2).lnk"
    monkeypatch.setattr(win_appid, "our_shortcuts", lambda name: [good, bad])
    plan = gui.gui_logic.ShortcutPlan(r"C:\Users\x\.local\bin\homevitals-gui.exe", "", r"C:\Users\x\.local\bin")

    class Done:
        returncode = 0
        stdout = f"{good}|C:\\Users\\x\\.local\\bin\\homevitals-gui.exe\r\n{bad}|C:\\homevitals-gui.EXE\r\n"

    monkeypatch.setattr(gui.subprocess, "run", lambda *a, **kw: Done())
    scripts, tagged = [], []
    monkeypatch.setattr(gui, "_run_shortcut_script", scripts.append)
    monkeypatch.setattr(win_appid, "set_shortcut_app_id", lambda lnk: tagged.append(str(lnk)) or True)
    monkeypatch.setattr(gui.platform_support, "repair_agent", lambda: False)
    gui.repair_shortcuts_and_task(plan)
    # Only the broken one is rewritten, in place, keeping its arguments (so --background stays).
    assert len(scripts) == 1
    assert str(bad) in scripts[0] and "homevitals-gui.exe" in scripts[0]
    assert "$s.Arguments" not in scripts[0]
    assert tagged == [str(bad)]


def test_omron_wrong_country_message_names_the_country_tried(root, tmp_path, fixtures, monkeypatch):
    from homevitals.omron_client import OmronLoginError
    app = _app_with(root, tmp_path, fixtures, "config_household_partial.yaml")
    dialog = gui.PersonDialog(app, next(p for p in app.people if p.name == "Jane"))
    bp = dialog.sections["omron"]

    def rejected(email, password, country):
        raise OmronLoginError("OMRON connect rejected the login for x")

    monkeypatch.setattr(gui.gui_logic, "check_omron_login", rejected)
    bp.form.email_var.set("jane-omron@example.com")
    bp.form.password_var.set("pw")
    bp.form.country.set("Canada (CA)")
    bp.on_connect()
    app._drain_until_idle()
    text = bp.message_label["text"]
    assert "Check three things: the email, the password, and the country." in text
    assert text.endswith("You picked Canada.")
    dialog.close()


def test_garmin_code_box_is_styled_and_returns_the_code_or_none(root):
    gui.theme.apply(root)
    dialog = gui.CodeDialog(root, "adult-b@example.com")
    assert dialog.title() == "Garmin security code"
    assert str(dialog.ok_button.cget("style")) == "Primary.TButton"
    dialog.code_var.set("  123456 ")
    dialog.on_ok()
    assert dialog.result == "123456" and not dialog.winfo_exists()
    again = gui.CodeDialog(root, "")
    again.on_cancel()
    assert again.result is None


def test_old_named_pin_is_rewritten_only_when_it_points_elsewhere(tmp_path, monkeypatch):
    from homevitals import win_appid
    pinned = tmp_path / "User Pinned" / "TaskBar"
    pinned.mkdir(parents=True)
    old_pin = pinned / "Household scale sync.lnk"
    old_pin.write_bytes(b"")
    desktop_old = tmp_path / "Household scale sync.lnk"
    monkeypatch.setattr(win_appid, "our_shortcuts", lambda name: [old_pin, desktop_old] if name == "Household scale sync" else [])
    plan = gui.gui_logic.ShortcutPlan(r"C:\bin\homevitals-gui.exe", "", r"C:\bin")
    asked = []

    def fake_run(cmd, **kw):
        import base64
        asked.append(base64.b64decode(cmd[-1]).decode("utf-16-le"))
        return type("Done", (), {"returncode": 0, "stdout": f"{old_pin}|C:\\bin\\homevitals-gui.exe\r\n"})()

    monkeypatch.setattr(gui.subprocess, "run", fake_run)
    scripts = []
    monkeypatch.setattr(gui, "_run_shortcut_script", scripts.append)
    monkeypatch.setattr(gui.platform_support, "repair_agent", lambda: False)
    gui.repair_shortcuts_and_task(plan)
    # Only the pinned old-named copy is checked (desktop ones get the new name instead), and it's already right.
    assert len(asked) == 1 and str(old_pin) in asked[0] and str(desktop_old) not in asked[0]
    assert scripts == []
