"""The household window: homevitals-gui.

Drawing only. What each button does lives in gui_logic.py, where it is tested
without a screen.

Rules here: tkinter is touched from the main thread only (workers hand results
back through App.post); no print (pythonw has no console); every handler is
wrapped so a surprise is logged and shown as one plain sentence, never a
traceback; nothing typed into a password box is ever logged or shown.
"""
from __future__ import annotations

import base64
import contextlib
import functools
import io
import logging
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, TypeVar

from homevitals import garmin_auth, gui_logic, platform_support, theme
from homevitals.cli import lock, shared

logger = logging.getLogger(__name__)

T = TypeVar("T")

TITLE = shared.APP_NAME
GENERIC_ERROR = "Something went wrong. Details were saved to the log file (~/.homevitals/sync.log)."
SYNC_RUNNING = "A sync is running right now. Wait for it to finish, then try again."
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PAD = 8
LABEL_COLUMN = 215      # the label column of every account form, so the boxes line up across sections


def configure_gui_logging() -> None:
    """Log to ~/.homevitals/sync.log only. pythonw has no console to log to."""
    shared.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(shared.LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("garminconnect").setLevel(logging.ERROR)


def report_callback_exception(exc_type, exc, tb, say: Callable[[str], None] | None = None) -> None:
    """Installed as root.report_callback_exception: details to the log, one plain sentence to the window."""
    logger.error("Unexpected error in the window", exc_info=(exc_type, exc, tb))
    if say is not None:
        say(GENERIC_ERROR)


def _guarded(method: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a button handler so a surprise is logged and shown as the generic sentence."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            logger.exception("Window action %s failed", method.__name__)
            app = self if isinstance(self, App) else getattr(self, "app", None)
            if app is not None:
                app.say(GENERIC_ERROR)
    return wrapper


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk, config_path: Path | None = None) -> None:
        self.root = root
        self.config_path = Path(config_path) if config_path is not None else shared.DEFAULT_CONFIG
        self.people: list[gui_logic.Person] = []
        self.current_login_email = ""
        self.busy = False
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()

        self._jobs = 0
        # Progress symbols in the people list: (name, "scale" | "bp") -> (state, count).
        self.progress: dict[tuple[str, str], tuple[str, int | None]] = {}
        self._spin_frame = 0
        self._spinning = False
        self.tray = None                 # set by main() when the tray icon is showing
        self._told_about_tray = False
        root.title(TITLE)
        root.minsize(780, 540)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        icon = gui_logic.icon_path()
        if icon is not None:
            # "default" gives every dialog the logo too; Windows uses it for the taskbar button.
            with contextlib.suppress(tk.TclError):
                root.iconbitmap(default=str(icon))
        root.report_callback_exception = lambda *a: report_callback_exception(*a, say=self.say)
        with contextlib.suppress(tk.TclError):
            import tkinter.font as tkfont
            tkfont.nametofont("TkDefaultFont").configure(size=10)
            tkfont.nametofont("TkTextFont").configure(size=10)
        theme.apply(root)
        root.configure(background=theme.BG)
        png = gui_logic.icon_png_path()
        theme.Header(root, TITLE, f"Weigh-ins and blood pressure for everyone at home  ·  {shared.TAGLINE}",
                     str(png) if png else None).pack(fill="x")

        outer = ttk.Frame(root, padding=(PAD * 2, PAD * 2, PAD * 2, PAD * 2))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="People who sync to Garmin", style="Title.TLabel").pack(anchor="w")
        self.list_frame = ttk.Frame(outer, style="Panel.TFrame", padding=(3, 3))
        self.list_frame.pack(fill="both", expand=False, pady=(PAD // 2, PAD + 2))
        self.tree = ttk.Treeview(self.list_frame, columns=("name", "garmin", "profile", "bp"), show="headings",
                                 height=5, selectmode="browse")
        self._row_names: dict[str, str] = {}
        for column, heading, width in (("name", "Name", 110), ("garmin", "Garmin account", 220),
                                       ("profile", "Scale profile", 170), ("bp", "Blood pressure", 150)):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor="w")
        # Row tints while a sync runs and after it: blue = working, green = done, red = something failed.
        for tag, colour in (("syncing", "#DCEBF8"), ("waiting", "#EEF3F7"), ("done", "#DDF3EE"),
                            ("failed", "#FBE3E1")):
            self.tree.tag_configure(tag, background=colour)
        self.empty_label = ttk.Label(
            self.list_frame, text="Nobody is set up yet. Click Add person to add the first person.",
            padding=(PAD, PAD * 2), style="Panel.TLabel")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.sync_button = ttk.Button(buttons, text="Sync now", command=self.on_sync_now, style="Primary.TButton")
        self.add_button = ttk.Button(buttons, text="Add person", command=self.on_add_person)
        self.edit_button = ttk.Button(buttons, text="Edit person", command=self.on_edit_person)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.remove_button = ttk.Button(buttons, text="Remove person", command=self.on_remove_person,
                                        style="Danger.TButton")
        self.fix_button = ttk.Button(buttons, text="Fix problems", command=self.on_fix_problems)
        for b in (self.sync_button, self.add_button, self.edit_button, self.remove_button, self.fix_button):
            b.pack(side="left", padx=(0, PAD))
        if sys.platform == "win32":
            ttk.Button(buttons, text="Add desktop shortcut",
                       command=self.on_desktop_shortcut).pack(side="right")

        extras = ttk.Frame(outer)
        extras.pack(fill="x", pady=(PAD, 0))
        self.autosync_var = tk.BooleanVar(value=self._agent_installed())
        ttk.Checkbutton(extras, text="Automatic sync (every 4 hours)", variable=self.autosync_var,
                        command=self.on_autosync_toggle).pack(side="left")
        self.startup_var = tk.BooleanVar(value=gui_logic.startup_enabled())
        if sys.platform == "win32":
            startup = ttk.Frame(outer)
            startup.pack(fill="x")
            ttk.Checkbutton(startup, text="Open this window when Windows starts", variable=self.startup_var,
                            command=self.on_startup_toggle).pack(side="left")

        ttk.Label(outer, text="Messages", style="Title.TLabel").pack(anchor="w", pady=(PAD * 2, 0))
        text_frame = ttk.Frame(outer, style="Panel.TFrame", padding=(4, 4))
        text_frame.pack(fill="both", expand=True, pady=(PAD // 2, 0))
        self.messages = tk.Text(text_frame, height=10, wrap="word", state="disabled", relief="flat",
                                borderwidth=0, highlightthickness=0, font="TkDefaultFont", padx=8, pady=6,
                                background=theme.PANEL, foreground=theme.TEXT)
        scroll = ttk.Scrollbar(text_frame, command=self.messages.yview)
        self.messages.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.messages.pack(side="left", fill="both", expand=True)

        self.refresh_people()
        problem = gui_logic.config_problem(self.config_path)
        if problem:
            self.say(problem)
        root.after(100, self._drain_queue)

    # -- plumbing ----------------------------------------------------------

    def _agent_installed(self) -> bool:
        try:
            return bool(platform_support.agent_installed())
        except Exception:
            logger.exception("Could not read the automatic sync state")
            return False

    def on_close(self) -> None:
        """The window's X. With a tray icon it only hides the window; the program keeps running."""
        if self.tray is not None:
            self.root.withdraw()
            logger.info("Window hidden to the tray")
            if not self._told_about_tray:
                self._told_about_tray = True
                with contextlib.suppress(Exception):
                    platform_support.notify(TITLE, "Still running. Click its icon by the clock to open it, "
                                                   "or right-click the icon and choose Quit.")
            return
        self.quit()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        with contextlib.suppress(tk.TclError):
            self.root.focus_force()

    def quit(self) -> None:
        # Logged so a window that vanished can be told apart from one closed on purpose.
        logger.info("Window closed by the user%s", " while a sync was running" if self.busy else "")
        if self.tray is not None:
            with contextlib.suppress(Exception):
                self.tray.stop()
            self.tray = None
        self.root.destroy()

    def post(self, fn: Callable[[], None]) -> None:
        """Thread-safe: run fn on the main thread soon."""
        self._queue.put(fn)

    def _drain_queue(self) -> None:
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                logger.exception("Window update failed")
                self.say(GENERIC_ERROR)
        with contextlib.suppress(tk.TclError):
            self.root.after(100, self._drain_queue)

    def run_in_background(self, work: Callable[[], T],
                          on_done: Callable[[T | None, BaseException | None], None]) -> None:
        """Run work on a worker thread; on_done gets (result, None) or (None, error) on the main thread."""
        self._jobs += 1

        def finish(result, error) -> None:
            self._jobs -= 1
            on_done(result, error)

        def runner() -> None:
            try:
                result = work()
            except BaseException as e:  # noqa: BLE001 - handed back to the main thread
                self.post(lambda e=e: finish(None, e))
                return
            self.post(lambda: finish(result, None))
        threading.Thread(target=runner, daemon=True).start()

    def _drain_until_idle(self, timeout: float = 10.0) -> None:
        """Run posted callbacks until no background job is left (for tests)."""
        import time
        deadline = time.monotonic() + timeout
        while self._jobs and time.monotonic() < deadline:
            try:
                fn = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            fn()

    def say(self, text: str) -> None:
        """Add one line to the message area (main thread only)."""
        self.messages.configure(state="normal")
        self.messages.insert("end", text + "\n")
        self.messages.see("end")
        self.messages.configure(state="disabled")

    def refresh_people(self) -> None:
        self.people = gui_logic.read_people(self.config_path)
        self.tree.delete(*self.tree.get_children())
        # Tk picks the row ids, so a hand-edited file with two identical names still shows both rows.
        self._row_names = {}
        for person in self.people:
            iid = self.tree.insert("", "end", values=gui_logic.person_row(person, self.progress, self._spin_frame),
                                   tags=self._row_tags(person.name))
            self._row_names[iid] = person.name
        if self.people:
            self.empty_label.pack_forget()
            self.tree.pack(fill="both", expand=True)
        else:
            self.tree.pack_forget()
            self.empty_label.pack(anchor="w")
        self._update_buttons()

    def _update_buttons(self) -> None:
        has_people = bool(self.people)
        self.sync_button.configure(state="normal" if has_people and not self.busy else "disabled")
        self.fix_button.configure(state="normal" if has_people and not self.busy else "disabled")
        self.remove_button.configure(state="normal" if has_people else "disabled")
        self.edit_button.configure(state="normal" if has_people else "disabled")

    def _selected_name(self) -> str | None:
        selection = self.tree.selection()
        if selection:
            return self._row_names.get(selection[0])
        if len(self.people) == 1:
            return self.people[0].name
        return None

    @_guarded
    def on_edit_person(self) -> None:
        name = self._selected_name()
        person = next((p for p in self.people if p.name == name), None)
        if person is None:
            self.say("Click a person in the list first, then click Edit person.")
            return
        PersonDialog(self, person)

    def _on_double_click(self, event: tk.Event) -> None:
        # Only a double-click on a row; one on the headings or the empty space below does nothing.
        if self.tree.identify_row(event.y):
            self.on_edit_person()

    # -- Sync now ----------------------------------------------------------

    @_guarded
    def on_sync_now(self) -> None:
        self.start_sync()

    def start_sync(self, older_bp_readings: bool = False) -> None:
        if self.busy:
            self.say("A sync is already running. Wait for it to finish, then try again.")
            return
        self.busy = True
        self._update_buttons()
        self.say("Bringing in older blood pressure readings... (this can take a few minutes)"
                 if older_bp_readings else "Syncing...")
        people = list(self.people)
        self.progress = gui_logic.sync_plan(people)
        self.draw_progress()
        try:
            proc = subprocess.Popen(
                gui_logic.sync_command(older_bp_readings=older_bp_readings), env=gui_logic.sync_environment(), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW,
            )
        except Exception:
            logger.exception("Could not start the sync")
            self.busy = False
            self._update_buttons()
            self.say("Couldn't start the sync. Details are in the log file.")
            return

        def read_output() -> None:
            saw_nothing_new = False
            try:
                for raw in proc.stdout:
                    line = raw.rstrip("\r\n")
                    event = gui_logic.parse_progress(line)
                    if event is not None:
                        self.post(lambda event=event: self._on_progress(event))
                        continue
                    logger.info("sync: %s", line)
                    saw_nothing_new = saw_nothing_new or gui_logic.line_means_nothing_new(line)
                    text = gui_logic.translate_line(line, people)
                    if text:
                        self.post(lambda text=text: self.say(text))
                code = proc.wait()
            except Exception:
                logger.exception("Reading the sync output failed")
                code = 1
            self.post(lambda: self._sync_finished(code, saw_nothing_new))

        threading.Thread(target=read_output, daemon=True).start()

    def _on_progress(self, event: gui_logic.ProgressEvent) -> None:
        gui_logic.apply_progress(self.progress, event)
        self.draw_progress()
        if gui_logic.progress_running(self.progress) and not self._spinning:
            self._spinning = True
            self.root.after(200, self._spin)

    def _spin(self) -> None:
        """Turn the "syncing" spinner while any step is running."""
        if not gui_logic.progress_running(self.progress):
            self._spinning = False
            return
        self._spin_frame += 1
        self.draw_progress()
        with contextlib.suppress(tk.TclError):
            self.root.after(200, self._spin)

    def draw_progress(self) -> None:
        """Redraw the people list cells with the current progress symbols (no re-read of the settings)."""
        people = {p.name: p for p in self.people}
        for iid, name in self._row_names.items():
            person = people.get(name)
            if person is not None and self.tree.exists(iid):
                self.tree.item(iid, values=gui_logic.person_row(person, self.progress, self._spin_frame),
                               tags=self._row_tags(name))

    def _row_tags(self, name: str) -> tuple[str, ...]:
        state = gui_logic.row_state(self.progress, name)
        return (state,) if state else ()

    def _sync_finished(self, code: int, saw_nothing_new: bool) -> None:
        gui_logic.finish_progress(self.progress)
        self.draw_progress()
        self.busy = False
        self._update_buttons()
        self.say(gui_logic.finish_message(code, saw_nothing_new))

    # -- Add / remove ------------------------------------------------------

    @_guarded
    def on_add_person(self) -> None:
        PersonDialog(self, None)

    @_guarded
    def on_remove_person(self) -> None:
        name = self._selected_name()
        if name is None:
            self.say("Click a person in the list first, then click Remove person.")
            return
        if not messagebox.askyesno(TITLE, gui_logic.removal_confirmation_text(name), parent=self.root):
            return
        with lock.single_instance(require_lock=True) as acquired:
            if not acquired:
                self.say(SYNC_RUNNING)
                return
            result = gui_logic.remove_person(self.config_path, name)
        if not result.removed:
            self.say(f"Couldn't find {name} in the settings file.")
        elif result.last_person:
            self.say(f"Removed {name}. Nobody is set up now, so automatic sync was turned off.")
            self.autosync_var.set(self._agent_installed())
        else:
            self.say(f"Removed {name}.")
        self.refresh_people()

    # -- Fix problems ------------------------------------------------------

    @_guarded
    def on_fix_problems(self) -> None:
        FixProblemsDialog(self)

    # -- Automatic sync and shortcut ---------------------------------------

    @_guarded
    def on_autosync_toggle(self) -> None:
        self.set_autosync(self.autosync_var.get())

    def set_autosync(self, wanted: bool, on_done: Callable[[bool], None] | None = None) -> None:
        def work() -> tuple[bool, str]:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                if wanted:
                    platform_support.install_agent()
                else:
                    platform_support.uninstall_agent()
            return bool(platform_support.agent_installed()), out.getvalue()

        def done(result, error) -> None:
            if error is not None:
                logger.error("Changing automatic sync failed", exc_info=error)
                installed = self._agent_installed()
                captured = ""
            else:
                installed, captured = result
            for line in captured.splitlines():
                text = gui_logic.translate_line(line, self.people)
                if text:
                    self.say(text)
            self.autosync_var.set(installed)
            if installed != wanted:
                self.say("Couldn't change automatic sync. Details are in the log file.")
            if on_done is not None:
                on_done(installed)

        self.run_in_background(work, done)

    @_guarded
    def on_desktop_shortcut(self) -> None:
        plan = gui_logic.shortcut_plan()
        if plan is None:
            logger.error("Desktop shortcut: no homevitals-gui launcher or pythonw.exe found")
            self.say("Couldn't find the program to link to. Details are in the log file.")
            return

        def done(_result, error) -> None:
            if error is not None:
                logger.error("Desktop shortcut failed", exc_info=error)
                self.say("Couldn't make the shortcut. Details are in the log file.")
            else:
                self.say(f"A shortcut called '{gui_logic.SHORTCUT_NAME}' is on your desktop.")

        self.run_in_background(lambda: create_shortcut(plan, "Desktop"), done)

    @_guarded
    def on_startup_toggle(self) -> None:
        if not self.startup_var.get():
            gui_logic.disable_startup()
            self.say("The window won't open by itself when Windows starts.")
            return
        plan = gui_logic.shortcut_plan()
        if plan is None:
            logger.error("Startup shortcut: no homevitals-gui launcher or pythonw.exe found")
            self.startup_var.set(False)
            self.say("Couldn't find the program to link to. Details are in the log file.")
            return

        def done(_result, error) -> None:
            if error is not None:
                logger.error("Startup shortcut failed", exc_info=error)
                self.startup_var.set(False)
                self.say("Couldn't set the window to open with Windows. Details are in the log file.")
            else:
                self.say("The window will open by itself when Windows starts.")

        # From the Startup folder it opens quietly in the tray, without popping the window up.
        startup_plan = gui_logic.ShortcutPlan(plan.target, f"{plan.arguments} --background".strip(), plan.working_dir)
        self.run_in_background(lambda: create_shortcut(startup_plan, "Startup"), done)


# ---------------------------------------------------------------------------
# Shared widgets
# ---------------------------------------------------------------------------


def _password_entry(parent: tk.Misc, var: tk.StringVar) -> ttk.Entry:
    return ttk.Entry(parent, textvariable=var, show="*", width=40)


def _show_button(parent: tk.Misc, entry: ttk.Entry) -> ttk.Button:
    """A Show / Hide button for a password box. It only ever reveals what was typed into
    this box (the window never fills in a saved password); a redrawn form starts hidden."""
    button = ttk.Button(parent, text="Show", width=6)

    def toggle() -> None:
        hidden = str(entry.cget("show")) == "*"
        entry.configure(show="" if hidden else "*")
        button.configure(text="Hide" if hidden else "Show")

    button.configure(command=toggle)
    entry.show_button = button      # for tests
    return button


class ProfilePicker(ttk.Frame):
    """Radio buttons for the Eufy profiles; profiles already linked to someone are greyed out."""

    def __init__(self, parent: tk.Misc, choices: list[gui_logic.ProfileChoice]) -> None:
        super().__init__(parent)
        self.var = tk.StringVar(value="")
        for choice in choices:
            label = choice.label + (f"  (linked to {choice.linked_to})" if choice.linked_to else "")
            ttk.Radiobutton(self, text=label, value=choice.customer_id, variable=self.var,
                            state="disabled" if choice.linked_to else "normal").pack(anchor="w", pady=2)
        ttk.Label(self, wraplength=480, justify="left", text=(
            "Profiles not linked to anyone (for example the kids) are never synced anywhere. "
            "Pick yours by the weight and date; the newest weigh-in is at the top.")).pack(anchor="w", pady=(PAD, 0))

    @property
    def selected(self) -> str | None:
        return self.var.get() or None


class OmronForm(ttk.Frame):
    """OMRON connect email, password and country. The password is never prefilled or shown."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.email_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.columnconfigure(0, minsize=LABEL_COLUMN)
        for row, label in enumerate(("OMRON connect email", "OMRON connect password",
                                     "Country the account was created in")):
            ttk.Label(self, text=label).grid(row=row, column=0, sticky="w", padx=(0, PAD), pady=2)
        self.email_entry = ttk.Entry(self, textvariable=self.email_var, width=40)
        self.email_entry.grid(row=0, column=1, sticky="w", pady=2)
        self.password_entry = _password_entry(self, self.password_var)
        self.password_entry.grid(row=1, column=1, sticky="w", pady=2)
        _show_button(self, self.password_entry).grid(row=1, column=2, sticky="w", padx=(PAD // 2, 0), pady=2)
        self.country = ttk.Combobox(self, width=37,
                                    values=[gui_logic.country_label(c) for c, _ in gui_logic.OMRON_COUNTRY_CHOICES])
        self.country.set(gui_logic.country_label(gui_logic.DEFAULT_OMRON_COUNTRY))
        self.country.grid(row=2, column=1, sticky="w", pady=2)

    def lock_identity(self, locked: bool) -> None:
        """Change password: the email and country stay as they are; only the password can be typed."""
        self.email_entry.configure(state="readonly" if locked else "normal")
        self.country.configure(state="disabled" if locked else "normal")

    def values(self) -> tuple[str, str, str]:
        return (self.email_var.get().strip(), self.password_var.get(),
                gui_logic.country_code_from_label(self.country.get()))

    def prefill(self, email: str | None, country: str | None) -> None:
        self.email_var.set(email or "")
        self.password_var.set("")
        self.country.set(gui_logic.country_label(country or gui_logic.DEFAULT_OMRON_COUNTRY))

    def validate(self, people: list[gui_logic.Person], exclude: str | None) -> str | None:
        email, password, country = self.values()
        problem = gui_logic.validate_omron_email(email, people, exclude=exclude) or \
            gui_logic.validate_omron_country(country)
        if problem:
            return problem
        if not password:
            return "Type the OMRON connect password."
        return None

    def clear_password(self) -> None:
        self.password_var.set("")


class CodeDialog(tk.Toplevel):
    """The Garmin security code box, in the app's look. result is the code, or None on Cancel."""

    def __init__(self, parent: tk.Misc, email: str) -> None:
        super().__init__(parent)
        self.result: str | None = None
        self.title("Garmin security code")
        self.configure(background=theme.BG)
        self.resizable(False, False)
        if parent.winfo_viewable():
            self.transient(parent)
        body = ttk.Frame(self, padding=(PAD * 3, PAD * 2, PAD * 3, PAD * 2))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Type the code Garmin emailed you", style="Title.TLabel").pack(anchor="w")
        who = email or "your Garmin email address"
        ttk.Label(body, text=f"Garmin sent a security code to {who}.", wraplength=380, justify="left").pack(
            anchor="w", pady=(PAD // 2, PAD))
        self.code_var = tk.StringVar()
        self.entry = ttk.Entry(body, textvariable=self.code_var, width=18, font=("Segoe UI", 14), justify="center")
        self.entry.pack(anchor="w", pady=(0, PAD))
        ttk.Label(body, text="If no email arrives within a minute, the password is probably wrong: click Cancel "
                             "and check it.", wraplength=380, justify="left", style="Muted.TLabel").pack(anchor="w")
        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(PAD * 2, 0))
        self.ok_button = ttk.Button(buttons, text="OK", style="Primary.TButton", command=self.on_ok)
        self.ok_button.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self.on_cancel).pack(side="right", padx=(0, PAD))
        self.bind("<Return>", lambda _e: self.on_ok())
        self.bind("<Escape>", lambda _e: self.on_cancel())
        self.protocol("WM_DELETE_WINDOW", self.on_cancel)
        # In front of the Person window that asked, and the only thing that takes clicks until answered.
        self.lift()
        with contextlib.suppress(tk.TclError):
            self.attributes("-topmost", True)
            self.after(300, lambda: self.winfo_exists() and self.attributes("-topmost", False))
            self.grab_set()
        self.entry.focus_set()

    def on_ok(self) -> None:
        self.result = self.code_var.get().strip() or None
        self.destroy()

    def on_cancel(self) -> None:
        self.result = None
        self.destroy()


def ask_mfa_code(parent: tk.Misc, email: str) -> str | None:
    """The Garmin code box, on the main thread. Returns the code, or None on Cancel."""
    dialog = CodeDialog(parent, email)
    parent.wait_window(dialog)
    return dialog.result


def install_mfa_bridge(app: App) -> None:
    """Route Garmin's code request to a pop-up on the main thread."""
    bridge = gui_logic.MfaBridge(app.post, lambda: ask_mfa_code(app.root, app.current_login_email))
    garmin_auth.MFA_PROMPT_OVERRIDE = bridge.prompt


def create_shortcut(plan: gui_logic.ShortcutPlan, folder: str = "Desktop") -> None:
    """Make the shortcut, with the app's logo, in "Desktop" or "Startup" (this user's special folders)."""
    _run_shortcut_script(gui_logic.shortcut_script(plan, folder=folder, icon=gui_logic.icon_path()))


def _run_shortcut_script(script: str) -> None:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
        capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(f"PowerShell exit {result.returncode}: {result.stderr.strip()[:500]}")
    made = (result.stdout or "").strip().splitlines()
    if made:
        # Same taskbar ID as the window, so the shortcut and the running window are one button.
        from homevitals import win_appid
        win_appid.set_shortcut_app_id(made[-1].strip())


def apply_taskbar_identity(app: App) -> None:
    """One taskbar button for the window, its shortcuts and its pinned icon (Windows only).

    The window gets relaunch details (pinning its button then pins something that
    starts the program), and shortcuts made by an older version get the window's
    taskbar ID, once, in the background.
    """
    if sys.platform != "win32":
        return
    from homevitals import win_appid

    plan = gui_logic.shortcut_plan()
    if plan is not None:
        app.root.update_idletasks()
        command = f'"{plan.target}" {plan.arguments}'.strip()
        win_appid.set_window_identity(int(app.root.wm_frame(), 16), command, TITLE, gui_logic.icon_path())

    def fix_old_shortcuts() -> None:
        try:
            move_to_new_name(plan)
        except Exception:
            logger.exception("Moving shortcuts or automatic sync to the new name failed")
        try:
            repair_shortcuts_and_task(plan)
        except Exception:
            logger.exception("Checking the shortcuts and automatic sync failed")
        for lnk in win_appid.our_shortcuts(gui_logic.SHORTCUT_NAME):
            if win_appid.read_shortcut_app_id(lnk) != win_appid.APP_ID and win_appid.set_shortcut_app_id(lnk):
                logger.info("Gave the shortcut %s the window's taskbar ID", lnk)

    threading.Thread(target=fix_old_shortcuts, daemon=True).start()


def repair_shortcuts_and_task(plan: gui_logic.ShortcutPlan | None) -> None:
    """Our shortcuts all point at this launcher, and automatic sync runs this homevitals.

    Earlier versions could save a relative path (".\\homevitals-gui.EXE") that points
    nowhere; a moved install leaves old paths behind. Each start checks and fixes them,
    keeping each shortcut's file (so a pin stays) and its arguments (--background).
    """
    from homevitals import win_appid

    if plan is not None:
        # Pinned copies keep the old name (renaming the file would undo the pin), so they're checked too.
        shortcuts = win_appid.our_shortcuts(gui_logic.SHORTCUT_NAME) + [
            lnk for old in gui_logic.OLD_SHORTCUT_NAMES for lnk in win_appid.our_shortcuts(old)
            if "User Pinned" in lnk.parts]
        if shortcuts:
            encoded = base64.b64encode(gui_logic.shortcut_targets_script(shortcuts).encode("utf-16-le")).decode()
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
            for line in (result.stdout or "").splitlines():
                path, _, target = line.strip().partition("|")
                if path and target.lower() != plan.target.lower():
                    _run_shortcut_script(gui_logic.shortcut_retarget_script(Path(path), plan, gui_logic.icon_path(),
                                                                            keep_arguments=True))
                    win_appid.set_shortcut_app_id(path)
                    logger.info("Fixed the shortcut %s (it pointed at %s)", path, target or "nothing")
    if platform_support.repair_agent():
        logger.info("Automatic sync registered again with the right program path")


def move_to_new_name(plan: gui_logic.ShortcutPlan | None) -> None:
    """Shortcuts and automatic sync from before the rename (household.4 and older), moved over once.

    Desktop and Startup shortcuts are made again under the new name (the Startup one
    still starts in the tray) and the old ones removed. The pinned taskbar copy keeps
    its file, so the pin stays, and is pointed at the new launcher. Automatic sync is
    registered again so it runs homevitals.
    """
    from homevitals import win_appid

    if plan is not None:
        for lnk in [lnk for old in gui_logic.OLD_SHORTCUT_NAMES for lnk in win_appid.our_shortcuts(old)]:
            if "User Pinned" in lnk.parts:
                continue        # kept under its old name; repair_shortcuts_and_task points it here when needed
            startup = lnk.parent == gui_logic.startup_shortcut_path().parent
            new_plan = gui_logic.ShortcutPlan(plan.target, f"{plan.arguments} --background".strip(),
                                              plan.working_dir) if startup else plan
            create_shortcut(new_plan, "Startup" if startup else "Desktop")
            lnk.unlink()
            logger.info("Renamed the %s shortcut to '%s'", "Startup" if startup else "desktop",
                        gui_logic.SHORTCUT_NAME)
    if platform_support.migrate_agent():
        logger.info("Automatic sync moved to the new name")


class _Dialog(tk.Toplevel):
    def __init__(self, app: App, title: str) -> None:
        super().__init__(app.root)
        self.app = app
        self.title(title)
        self.configure(background=theme.BG)
        self.transient(app.root)
        self.resizable(True, False)
        self.body = ttk.Frame(self, padding=PAD * 2)
        self.body.pack(fill="both", expand=True)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def close(self) -> None:
        self.destroy()


# ---------------------------------------------------------------------------
# The Person window (add or edit one person)
# ---------------------------------------------------------------------------


class _Refused(Exception):
    """A save refused by the household rules (our own words, safe to show through translate_error)."""


def _refusals(fn: Callable[[], T]) -> T:
    """Run a gui_logic save; its ValueError is our own message, so mark it for translate_error."""
    try:
        return fn()
    except ValueError as e:
        raise _Refused(str(e)) from e


def _login_again(fn: Callable[[], T]) -> T:
    """Run a *_login_again call; only its own "No person named" refusal is our wording."""
    try:
        return fn()
    except ValueError as e:
        if str(e).startswith("No person named"):
            raise _Refused(str(e)) from e
        raise


def _grid_field(parent: tk.Misc, row: int, label: str, var: tk.StringVar, secret: bool = False) -> ttk.Entry:
    parent.columnconfigure(0, minsize=LABEL_COLUMN)
    ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, PAD), pady=2)
    entry = _password_entry(parent, var) if secret else ttk.Entry(parent, textvariable=var, width=40)
    entry.grid(row=row, column=1, sticky="w", pady=2)
    if secret:
        _show_button(parent, entry).grid(row=row, column=2, sticky="w", padx=(PAD // 2, 0), pady=2)
    return entry


class PersonDialog(_Dialog):
    """Add or edit one person: a name and one box per account. Each account connects on its own."""

    SERVICES = ("garmin", "eufy", "omron")
    TITLES = {"garmin": "Garmin", "eufy": "Scale (Eufy)", "omron": "Blood pressure (OMRON connect)"}
    INTROS = {
        "garmin": "This person's own Garmin Connect account. Weigh-ins and blood pressure readings go here.",
        "eufy": "The Eufy account this person's weigh-ins go to, and which profile on the scale is theirs.",
        "omron": "The OMRON connect account on this person's phone, and the country it was created in.",
    }

    def __init__(self, app: App, person: gui_logic.Person | None, start: tuple[str, str] | None = None,
                 on_close: Callable[[bool], None] | None = None) -> None:
        super().__init__(app, f"Edit {person.name}" if person is not None else "Add person")
        self.person = person
        self.on_close = on_close
        self.working = False
        self.changed = False
        self.name_var = tk.StringVar()
        self.name_row = ttk.Frame(self.body)
        self.name_row.pack(fill="x")
        self.name_widget: ttk.Entry | ttk.Label
        self._draw_name()
        self.sections: dict[str, AccountSection] = {}
        for service in self.SERVICES:
            section = AccountSection(self, service)
            section.pack(fill="x", pady=(PAD, 0))
            self.sections[service] = section
            section.render()
        ttk.Button(self.body, text="Close", command=self.close).pack(anchor="e", pady=(PAD * 2, 0))
        if person is None:
            self.name_widget.focus_set()
        if start is not None:
            self.after_idle(lambda: self._start(start))

    def _draw_name(self) -> None:
        for child in self.name_row.winfo_children():
            child.destroy()
        line = ttk.Frame(self.name_row)
        line.pack(fill="x")
        ttk.Label(line, text="Name").pack(side="left", padx=(0, PAD))
        if self.person is None:
            self.name_widget = ttk.Entry(line, textvariable=self.name_var, width=30)
            note = ("Type a name, then connect the accounts you have. You can connect the rest later: open the "
                    "person again from the list.")
        else:
            self.name_widget = ttk.Label(line, text=self.person.name, style="Name.TLabel")
            note = "To rename someone, remove them and add them again. Their Garmin data is not affected."
        self.name_widget.pack(side="left")
        ttk.Label(self.name_row, text=note, wraplength=560, justify="left", style="Muted.TLabel").pack(
            anchor="w", pady=(2, 0))

    @_guarded
    def _start(self, start: tuple[str, str]) -> None:
        if not self.winfo_exists():
            return
        service, what = start
        section = self.sections.get(service)
        if section is None or not section.connected():
            return
        if what == "login_again":
            section.on_login_again()
        elif what == "password":
            section.show_form("password")
        elif what == "choose_profile":
            section.on_choose_profile()

    def current_name(self) -> str:
        return self.person.name if self.person is not None else self.name_var.get().strip()

    def guard_name(self) -> str | None:
        if self.person is not None:
            return None
        return gui_logic.validate_name(self.current_name(), self.app.people)

    def others(self) -> list[gui_logic.Person]:
        name = self.current_name().lower()
        return [p for p in self.app.people if p.name.lower() != name]

    def begin_work(self, section: AccountSection, text: str) -> None:
        self.working = True
        for s in self.sections.values():
            s.set_busy(True)
        if isinstance(self.name_widget, ttk.Entry):
            self.name_widget.configure(state="disabled")
        section.set_status(text)

    def end_work(self, section: AccountSection, problem: str | None = None) -> None:
        self.working = False
        for s in self.sections.values():
            s.set_busy(False)
        if isinstance(self.name_widget, ttk.Entry):
            self.name_widget.configure(state="normal")
        section.set_status(problem or "", problem=bool(problem))

    def after_change(self, message: str, section: AccountSection | None = None) -> None:
        """An account was saved or removed: refresh the main list and redraw every box from the settings file."""
        name = self.current_name()
        self.app.say(message)
        self.app.refresh_people()
        self.changed = True
        was_new = self.person is None
        self.person = next((p for p in self.app.people if p.name == name), None) or \
            next((p for p in self.app.people if p.name.lower() == name.lower()), None)
        if was_new and self.person is not None:
            self.title(f"Edit {self.person.name}")
            self._draw_name()
        for s in self.sections.values():
            s.render()
        if section is not None:
            section.set_status(message)

    def close(self) -> None:
        for section in self.sections.values() if hasattr(self, "sections") else ():
            section.clear_secrets()
        self.name_var.set("")
        callback, self.on_close = self.on_close, None
        super().close()
        if callback is not None:
            callback(self.changed)


class AccountSection(ttk.Frame):
    """One account inside the Person window: its status, then buttons, a form or the profile picker."""

    PROGRESS = {"garmin": "Logging in to Garmin... (this can take a minute)", "eufy": "Logging in to Eufy...",
                "omron": "Checking the OMRON connect login..."}
    PASSWORD_HEADINGS = {
        "garmin": "Type the new Garmin password. It is checked with a real login before it is saved (Garmin may "
                  "email a code).",
        "eufy": "Type the new Eufy password. It is checked with a real login before it is saved. Everyone who "
                "shares this Eufy login gets the new password too.",
        "omron": "Type the new OMRON connect password. It is checked with a real login before it is saved.",
    }
    OLDER_READINGS_TEXT = ("Bring in older blood pressure readings? This looks back up to 10 years in OMRON connect "
                           "for everyone with a monitor connected. Readings already in Garmin are skipped, so nothing "
                           "is sent twice. It can take a few minutes.")

    def __init__(self, dialog: PersonDialog, service: str) -> None:
        super().__init__(dialog.body, style="Card.TFrame", padding=(PAD + 6, PAD + 2))
        ttk.Label(self, text=dialog.TITLES[service], style="Title.TLabel").pack(anchor="w")
        self.dialog = dialog
        self.app = dialog.app
        self.service = service
        self.mode = ""
        self.buttons: dict[str, ttk.Button] = {}
        self.email_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.email_entry: ttk.Entry | None = None
        self.password_entry: ttk.Entry | None = None
        self.form: OmronForm | None = None
        self.picker: ProfilePicker | None = None
        self.choices: list[gui_logic.ProfileChoice] = []
        self.picker_purpose = ""
        self.draft: tuple[str, str] | None = None     # (email, password) between the Eufy login and Save profile
        self._tried_country: str | None = None         # the OMRON country of the last login attempt
        ttk.Label(self, text=dialog.INTROS[service], wraplength=560, justify="left", style="Muted.TLabel").pack(
            anchor="w")
        self.status_label = ttk.Label(self, text="", wraplength=560, justify="left")
        self.status_label.pack(anchor="w", pady=(PAD // 2, 0))
        self.content = ttk.Frame(self)
        self.content.pack(fill="x", pady=(PAD // 2, 0))
        self.message_label = ttk.Label(self, text="", wraplength=560, justify="left")   # shown only when it says something

    # -- views ----------------------------------------------------------------

    def connected(self) -> bool:
        p = self.dialog.person
        if p is None:
            return False
        return {"garmin": p.has_garmin, "eufy": p.has_eufy, "omron": p.has_omron}[self.service]

    def _clear(self) -> None:
        self.clear_secrets()
        self.form = None
        self.picker = None
        self.email_entry = None
        self.password_entry = None
        self.buttons = {}
        for child in self.content.winfo_children():
            child.destroy()

    BUTTON_STYLES = {"Connect": "Primary.TButton", "Save profile": "Primary.TButton", "Disconnect": "Danger.TButton"}

    def _button(self, parent: tk.Misc, text: str, command: Callable[[], None]) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, style=self.BUTTON_STYLES.get(text, "TButton"))
        self.buttons[text] = button
        return button

    def render(self) -> None:
        """The section's resting view, drawn from dialog.person."""
        self._clear()
        self.set_status("")
        self.status_label.configure(text=gui_logic.account_status(self.dialog.person, self.service))
        if not self.connected():
            self.show_form("connect")
            return
        row = ttk.Frame(self.content)
        row.pack(fill="x")
        if self.service == "eufy" and not self.dialog.person.customer_id:
            self.mode = "no_profile"
            self._button(row, "Choose profile", self.on_choose_profile).pack(side="left", padx=(0, PAD))
        else:
            self.mode = "connected"
            self._button(row, "Log in again", self.on_login_again).pack(side="left", padx=(0, PAD))
        self._button(row, "Change password", self.on_change_password).pack(side="left", padx=(0, PAD))
        self._button(row, "Change account", self.on_change_account).pack(side="left", padx=(0, PAD))
        self._button(row, "Disconnect", self.on_disconnect).pack(side="left")
        if self.service == "omron":
            older = ttk.Frame(self.content)
            older.pack(fill="x", pady=(PAD, 0))
            self._button(older, "Bring in older readings", self.on_older_readings).pack(side="left")
            ttk.Label(older, text="Each sync looks back a few weeks. This looks back up to 10 years, once.",
                      wraplength=360, justify="left").pack(side="left", padx=(PAD, 0))
        self.set_busy(self.dialog.working)

    def show_form(self, mode: str) -> None:
        """mode: "connect" (nothing connected yet), "password" (same account, new password), "account" (any)."""
        self._clear()
        self.mode = mode
        person = self.dialog.person
        if mode == "password":
            ttk.Label(self.content, text=self.PASSWORD_HEADINGS[self.service], wraplength=560,
                      justify="left").pack(anchor="w", pady=(0, PAD // 2))
        if self.service == "omron":
            self.form = OmronForm(self.content)
            if mode != "connect" and person is not None:
                self.form.prefill(person.omron_email, person.omron_country)
            self.form.lock_identity(mode == "password")
            self.form.pack(anchor="w", fill="x")
            self.password_entry = self.form.password_entry
        else:
            fields = ttk.Frame(self.content)
            fields.pack(anchor="w", fill="x")
            label = "Garmin" if self.service == "garmin" else "Eufy"
            if mode != "connect" and person is not None:
                self.email_var.set((person.garmin_email if self.service == "garmin" else person.eufy_email) or "")
            else:
                # Never someone else's login: people sharing a scale may keep their weight to themselves.
                self.email_var.set("")
            self.email_entry = _grid_field(fields, 0, f"{label} email", self.email_var)
            self.password_entry = _grid_field(fields, 1, f"{label} password", self.password_var, secret=True)
            if mode == "password":
                self.email_entry.configure(state="readonly")
        row = ttk.Frame(self.content)
        row.pack(fill="x", pady=(PAD // 2, 0))
        self._button(row, "Connect", self.on_connect).pack(side="left")
        if mode != "connect":
            self._button(row, "Cancel", self.on_cancel).pack(side="left", padx=(PAD, 0))
        self.set_busy(self.dialog.working)
        if mode == "password" and self.password_entry is not None:
            self.password_entry.focus_set()

    def show_picker(self, choices: list[gui_logic.ProfileChoice], preselect: str | None, purpose: str) -> None:
        """purpose: "connect" (after a Eufy login; the draft holds the login) or "choose" (profile only)."""
        draft = self.draft
        self._clear()
        self.draft = draft
        self.mode = "picker"
        self.picker_purpose = purpose
        self.choices = choices
        self.picker = ProfilePicker(self.content, choices)
        if preselect and any(c.customer_id == preselect and not c.linked_to for c in choices):
            self.picker.var.set(preselect)
        self.picker.pack(anchor="w", fill="x")
        row = ttk.Frame(self.content)
        row.pack(fill="x", pady=(PAD // 2, 0))
        self._button(row, "Save profile", self.on_save_profile).pack(side="left")
        self._button(row, "Cancel", self.on_cancel).pack(side="left", padx=(PAD, 0))
        self.set_busy(self.dialog.working)

    def set_status(self, text: str, problem: bool = False) -> None:
        self.message_label.configure(text=text, foreground=theme.DANGER if problem else theme.TEXT)
        if text:
            self.message_label.pack(anchor="w", pady=(PAD // 2, 0))
        else:
            self.message_label.pack_forget()

    def set_busy(self, busy: bool) -> None:
        for button in self.buttons.values():
            button.configure(state="disabled" if busy else "normal")

    def clear_secrets(self) -> None:
        self.password_var.set("")
        if self.form is not None:
            self.form.clear_password()
        self.draft = None

    # -- running one job --------------------------------------------------------

    def _run(self, progress: str, work: Callable[[], Any], on_success: Callable[[Any], None],
             what: str, saved_message: str | Callable[[Any], str | None] | None = None) -> None:
        """Run work on a worker; show the plain sentence on failure, call on_success on the main thread.

        saved_message: for a save, what the main window says if this window was closed meanwhile
        (a callable gets the result and returns None when nothing was saved).
        """
        name = self.dialog.current_name()
        self.dialog.begin_work(self, progress)

        def done(result, error) -> None:
            if not self.winfo_exists():
                # The window was closed meanwhile. A save that already went through still updates the list.
                message = saved_message(result) if callable(saved_message) else saved_message
                if error is None and result is not False and message:
                    self.app.say(message)
                    self.app.refresh_people()
                return
            if error is not None:
                self.dialog.end_work(self, self._plain_error(error, name, what))
                return
            if result is False:
                self.dialog.end_work(self, SYNC_RUNNING)
                return
            self.dialog.end_work(self)
            on_success(result)

        self.app.run_in_background(work, done)

    def _plain_error(self, error: BaseException, name: str, what: str) -> str:
        if isinstance(error, _Refused):
            logger.warning("Person window: %s for %s was refused: %s", what, name, error)
            return gui_logic.translate_error(str(error))
        if self.service == "omron":
            # 6.3: OMRON failures are logged by type only.
            logger.error("Person window: %s for %s failed (%s)", what, name, type(error).__name__)
            result = gui_logic.classify_omron_error(error, name)
            if result.action == "change_omron_password":
                # Name the country that was tried: a wrong one fails exactly like a wrong password.
                return gui_logic.omron_login_rejected_text(name, self._tried_country)
            return result.text
        logger.error("Person window: %s for %s failed", what, name, exc_info=error)
        classify = gui_logic.classify_garmin_error if self.service == "garmin" else gui_logic.classify_eufy_error
        return classify(error, name).text

    def _locked(self, fn: Callable[[], T], saving: bool = True) -> Callable[[], T | bool]:
        """Wrap a job so it only runs while no sync is running; False means a sync held the lock.

        saving: fn is a gui_logic save, whose ValueError is our own refusal (see _refusals).
        """
        def work():
            with lock.single_instance(require_lock=True) as acquired:
                if not acquired:
                    return False
                return _refusals(fn) if saving else fn()
        return work

    # -- handlers -------------------------------------------------------------

    @_guarded
    def on_connect(self) -> None:
        if self.dialog.working:
            return
        {"garmin": self._connect_garmin, "eufy": self._connect_eufy, "omron": self._connect_omron}[self.service]()

    def _connect_garmin(self) -> None:
        email = self.email_var.get().strip()
        password = self.password_var.get()
        problem = self.dialog.guard_name() or gui_logic.validate_garmin_email(email, self.dialog.others())
        if not problem and not password:
            problem = "Please type the Garmin password."
        if problem:
            self.set_status(problem, problem=True)
            return
        name = self.dialog.current_name()
        config_path = self.app.config_path
        self.app.current_login_email = email
        save = self._locked(lambda: gui_logic.connect_garmin(config_path, name, email, password) or True)

        def work():
            gui_logic.check_garmin_login(email, password)
            return save()

        message = f"Garmin is connected for {name}."
        self._run(self.PROGRESS["garmin"], work, lambda _r: self.dialog.after_change(message, self),
                  "Garmin connect", message)

    def _connect_eufy(self) -> None:
        email = self.email_var.get().strip()
        problem = self.dialog.guard_name() or gui_logic.validate_email(email)
        # Always the typed password, checked by a real login: nobody can open another person's
        # Eufy account (and see its profiles' weights) with a password saved for them.
        password = self.password_var.get()
        if not problem and not password:
            problem = "Please type the Eufy password."
        if problem:
            self.set_status(problem, problem=True)
            return
        name = self.dialog.current_name()
        config_path = self.app.config_path
        person = self.dialog.person
        keep = person.customer_id if (self.mode == "password" and person is not None) else None
        preselect = person.customer_id if self.connected() else None
        others = self.dialog.others()

        def work():
            profiles = gui_logic.check_eufy_login(email, password, fresh=True)
            if not profiles:
                return ("none", None)
            if keep and keep in {p.customer_id for p in profiles}:
                names = self._locked(lambda: gui_logic.connect_scale(config_path, name, email, password, keep))()
                return ("saved", names) if names is not False else False
            return ("pick", profiles)

        def success(result) -> None:
            kind, value = result
            if kind == "none":
                self.set_status("No weigh-ins found on this Eufy account yet. Step on the scale, open the Eufy app "
                                "on your phone, then try again.", problem=True)
            elif kind == "saved":
                self._scale_saved(name, keep, value)
            else:
                self.draft = (email, password)
                self.show_picker(gui_logic.profile_choices(value, others), preselect, "connect")

        # A password change saves in the same job only when the profile is still there; a login
        # that ends at the picker (or finds nothing) has saved nothing.
        saved_text = f"The scale is connected for {name} (profile ...{keep[-4:]})." if keep else None
        self._run(self.PROGRESS["eufy"], work, success, "Eufy connect",
                  lambda result: saved_text if result and result[0] == "saved" else None)

    def _scale_saved(self, name: str, customer_id: str, names: list[str]) -> None:
        self.dialog.after_change(f"The scale is connected for {name} (profile ...{customer_id[-4:]}).", self)
        also = [n for n in names if n != name]
        if also:
            self.app.say(f"The Eufy password was also updated for {', '.join(also)} (same Eufy login).")

    def _connect_omron(self) -> None:
        form = self.form
        problem = self.dialog.guard_name() or (form.validate(self.dialog.others(), exclude=None) if form else
                                               "Type the OMRON connect password.")
        if problem:
            self.set_status(problem, problem=True)
            return
        email, password, country = form.values()
        self._tried_country = country
        name = self.dialog.current_name()
        config_path = self.app.config_path
        save = self._locked(lambda: gui_logic.connect_omron(config_path, name, email, password, country) or True)

        def work():
            gui_logic.check_omron_login(email, password, country)
            return save()

        message = f"Blood pressure syncing is on for {name}."
        self._run(self.PROGRESS["omron"], work, lambda _r: self.dialog.after_change(message, self),
                  "OMRON connect", message)

    @_guarded
    def on_save_profile(self) -> None:
        if self.dialog.working or self.picker is None:
            return
        chosen = self.picker.selected
        problem = gui_logic.validate_profile_choice(chosen, self.dialog.others())
        if problem:
            self.set_status(problem, problem=True)
            return
        name = self.dialog.current_name()
        config_path = self.app.config_path
        if self.picker_purpose == "connect" and self.draft is not None:
            email, password = self.draft
            work = self._locked(lambda: gui_logic.connect_scale(config_path, name, email, password, chosen))
            self._run("Saving...", work, lambda names: self._scale_saved(name, chosen, names), "Save profile",
                      f"The scale is connected for {name} (profile ...{chosen[-4:]}).")
        else:
            work = self._locked(lambda: gui_logic.choose_profile(config_path, name, chosen) or True)
            message = f"Saved {name}'s scale profile (...{chosen[-4:]})."
            self._run("Saving...", work, lambda _r: self.dialog.after_change(message, self), "Save profile", message)

    @_guarded
    def on_login_again(self) -> None:
        if self.dialog.working:
            return
        name = self.dialog.current_name()
        config_path = self.app.config_path
        if self.service == "garmin":
            self.app.current_login_email = self.dialog.person.garmin_email or ""
            work = self._locked(lambda: _login_again(lambda: gui_logic.garmin_login_again(config_path, name)) or True,
                                saving=False)
            self._run(self.PROGRESS["garmin"], work,
                      lambda _r: self.set_status(f"Garmin login for {name} is working again."), "Log in again")
            return
        if self.service == "omron":
            self._tried_country = self.dialog.person.omron_country
        call = gui_logic.scale_login_again if self.service == "eufy" else gui_logic.omron_login_again
        work = self._locked(lambda: _login_again(lambda: call(config_path, name)), saving=False)
        self._run(self.PROGRESS[self.service], work,
                  lambda result: self.set_status(result.text, problem=not result.ok), "Log in again")

    @_guarded
    def on_choose_profile(self) -> None:
        if self.dialog.working:
            return
        name = self.dialog.current_name()
        config_path = self.app.config_path
        self._run(self.PROGRESS["eufy"], lambda: _login_again(
            lambda: gui_logic.list_profiles_for_person(config_path, name)),
            lambda choices: self.show_picker(choices, None, "choose"), "Choose profile")

    @_guarded
    def on_change_password(self) -> None:
        if not self.dialog.working:
            self.show_form("password")

    @_guarded
    def on_change_account(self) -> None:
        if not self.dialog.working:
            self.show_form("account")

    @_guarded
    def on_cancel(self) -> None:
        if not self.dialog.working:
            self.render()

    @_guarded
    def on_disconnect(self) -> None:
        if self.dialog.working:
            return
        name = self.dialog.current_name()
        text = {"garmin": gui_logic.garmin_disconnect_confirmation_text,
                "eufy": gui_logic.scale_disconnect_confirmation_text,
                "omron": gui_logic.omron_disconnect_confirmation_text}[self.service](name)
        if not messagebox.askyesno(TITLE, text, parent=self.dialog):
            return
        with lock.single_instance(require_lock=True) as acquired:
            if not acquired:
                self.set_status(SYNC_RUNNING, problem=True)
                return
            {"garmin": gui_logic.disconnect_garmin, "eufy": gui_logic.disconnect_scale,
             "omron": gui_logic.disconnect_omron}[self.service](self.app.config_path, name)
        message = {"garmin": f"Garmin is disconnected for {name}.",
                   "eufy": f"The scale is disconnected for {name}.",
                   "omron": f"Blood pressure syncing is off for {name}."}[self.service]
        self.dialog.after_change(message, self)

    @_guarded
    def on_older_readings(self) -> None:
        if self.dialog.working:
            return
        if not messagebox.askyesno(TITLE, self.OLDER_READINGS_TEXT, parent=self.dialog):
            return
        self.dialog.close()
        self.app.start_sync(older_bp_readings=True)


# ---------------------------------------------------------------------------
# Fix problems
# ---------------------------------------------------------------------------


class FixProblemsDialog(_Dialog):
    ACTION_LABELS = {
        "garmin_login": "Log in again",
        "change_eufy_password": "Change password",
        "change_garmin_password": "Change password",
        "change_omron_password": "Change password",
        "turn_on_autosync": "Turn on",
        "choose_profile": "Choose profile",
        "open_person": "Open",
    }
    START_FOR_ACTION: dict[str, tuple[str, str] | None] = {
        "garmin_login": ("garmin", "login_again"),
        "change_garmin_password": ("garmin", "password"),
        "change_eufy_password": ("eufy", "password"),
        "change_omron_password": ("omron", "password"),
        "choose_profile": ("eufy", "choose_profile"),
        "open_person": None,
    }

    def __init__(self, app: App) -> None:
        super().__init__(app, "Fix problems")
        self.geometry("620x380")
        self.rows = ttk.Frame(self.body)
        self.rows.pack(fill="both", expand=True)
        bottom = ttk.Frame(self.body)
        bottom.pack(fill="x", pady=(PAD * 2, 0))
        ttk.Button(bottom, text="Close", command=self.close).pack(side="right")
        self.again_button = ttk.Button(bottom, text="Check again", command=self.run)
        self.again_button.pack(side="right", padx=(0, PAD))
        self.run()

    def _clear(self) -> None:
        for child in self.rows.winfo_children():
            child.destroy()

    def note(self, text: str) -> None:
        ttk.Label(self.rows, text=text, wraplength=560, justify="left").pack(anchor="w", pady=2)

    def add_row(self, result: gui_logic.CheckResult) -> None:
        if not self.winfo_exists():
            return
        row = ttk.Frame(self.rows)
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="✓" if result.ok else "✗", width=2,
                  foreground="#2a7" if result.ok else "#a33").pack(side="left", anchor="n")
        label = ttk.Label(row, text=result.text, wraplength=430, justify="left")
        label.pack(side="left", fill="x", expand=True)
        if result.action:
            ttk.Button(row, text=self.ACTION_LABELS.get(result.action, "Fix"),
                       command=lambda: self.on_action(result, label)).pack(side="right")

    @_guarded
    def run(self) -> None:
        self._clear()
        self.again_button.configure(state="disabled")
        self.note("Checking... (this takes a few seconds)")

        def work():
            with lock.single_instance(require_lock=True) as acquired:
                if not acquired:
                    return None
                self.app.post(self._clear)
                return gui_logic.run_checks(self.app.config_path,
                                            lambda r: self.app.post(lambda r=r: self.add_row(r)))

        def done(results, error) -> None:
            if not self.winfo_exists():
                return
            self.again_button.configure(state="normal")
            if error is not None:
                logger.error("Fix problems failed", exc_info=error)
                self.note("Couldn't finish the checks. Details are in the log file.")
            elif results is None:
                self._clear()
                self.note("A sync is running right now. Try again in a minute.")
            elif all(r.ok for r in results):
                self.note("Everything looks fine.")

        self.app.run_in_background(work, done)

    @_guarded
    def on_action(self, result: gui_logic.CheckResult, label: ttk.Label) -> None:
        if result.action == "turn_on_autosync":
            label.configure(text="Turning on automatic sync...")
            self.app.set_autosync(True, lambda on: label.configure(
                text="Automatic sync is on (every 4 hours)." if on else "Couldn't turn on automatic sync."))
            return
        person = next((p for p in self.app.people if p.name == result.person), None)
        if person is None:
            label.configure(text=f"Couldn't find {result.person} in the settings file.")
            return
        PersonDialog(self.app, person, start=self.START_FOR_ACTION.get(result.action),
                     on_close=self._after_person_dialog)

    def _after_person_dialog(self, changed: bool) -> None:
        if changed and self.winfo_exists():
            self.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_crash_file = None


def enable_crash_log() -> None:
    """Write the details of a hard crash (one Python can't catch) to ~/.homevitals/crash.log.

    pythonw has no console, so without this a crash leaves no trace at all.
    """
    import faulthandler

    global _crash_file
    shared.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _crash_file = open(shared.DATA_DIR / "crash.log", "a", encoding="utf-8")  # noqa: SIM115 - kept open on purpose
    faulthandler.enable(file=_crash_file, all_threads=True)


def wants_background(argv: list[str]) -> bool:
    """--background (used by the Startup-folder shortcut) starts quietly in the tray."""
    return "--background" in argv[1:]


def _start_tray(app: App) -> None:
    """Show the logo by the clock. If that fails, the window simply behaves like a normal window."""
    from homevitals import tray as tray_module

    icon = gui_logic.icon_path()
    if icon is None or sys.platform != "win32":
        return
    icon_obj = tray_module.TrayIcon(str(icon), TITLE, {
        "open": lambda: app.post(app.show_window),
        "sync": lambda: app.post(app.on_sync_now),
        "quit": lambda: app.post(app.quit),
    })
    if icon_obj.start():
        app.tray = icon_obj


def main() -> None:
    """Entry point for homevitals-gui."""
    try:
        # Before the log file (which creates the new data folder) and before the settings are read.
        from homevitals import migrate
        moved = migrate.move_from_old_names()
        configure_gui_logging()
        for line in moved:
            logger.info("Moved to the HomeVitals name: %s", line)
        with contextlib.suppress(Exception):
            enable_crash_log()
        from homevitals import __version__, tray
        if tray.signal_running_instance():
            # Already running (probably in the tray): that copy opens its window instead.
            logger.info("Window already running; asked it to open")
            return
        logger.info("Window opened (homevitals %s)", __version__)
        from homevitals.cli import setup
        if shared.DEFAULT_CONFIG.exists():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    setup._migrate_config_passwords(shared.DEFAULT_CONFIG)
            except Exception:
                # A damaged settings file: the window opens anyway and says so in plain words.
                logger.exception("Could not move passwords out of the settings file")
        if sys.platform == "win32":
            # Its own taskbar identity, so Windows shows our logo instead of grouping it under Python.
            with contextlib.suppress(Exception):
                import ctypes

                from homevitals.win_appid import APP_ID
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        root = tk.Tk()
        app = App(root)
        install_mfa_bridge(app)
        _start_tray(app)
        with contextlib.suppress(Exception):
            apply_taskbar_identity(app)
        if app.tray is not None and wants_background(sys.argv):
            root.withdraw()
            logger.info("Started in the tray")
    except Exception:
        logger.exception("The window could not start")
        with contextlib.suppress(Exception):
            messagebox.showerror(TITLE, "The window could not start. Details were saved to the log file "
                                        "(~/.homevitals/sync.log).")
        return
    root.mainloop()
    logger.info("Window stopped")


if __name__ == "__main__":
    main()
