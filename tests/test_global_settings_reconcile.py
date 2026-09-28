"""The install-wide settings are reconciled with global/app.json by difference.

`MainWindow._persist_global_settings()` runs on every `save_settings()`, and
most saves have nothing to do with settings (an audio speed, a recent
reaction, the session token). It used to copy every key of
`app_settings._GENERAL_GLOBAL` / `_CONNECTION_GLOBAL` from this account's copy
into the shared file each time — a copy as old as the account's startup — so
the last account to save *anything* won every install-wide setting at once.
Reproduced by review with the real methods and two windows on one app.json:
A chooses "keep both open", B stores something unrelated, the shared file says
"single" again, and A's Settings dialog shows its choice undone.

Now a key is written only when it differs from what this account last took
from the shared file (`_global_settings_snapshot`); any other key is pulled.

Two halves of the same bug are covered below as well: a pull must not
overwrite a change the Settings dialog made to the local copy while the save
was running, and the dialog's OK writes back only the install-wide controls
the user changed in it — writing an untouched one read as this account's
change and undid the other account's.

Two stub windows share one `AppSettings(tmp_path)` and carry the real methods
under the real attribute names (`_app_settings` with the underscore,
`_save_lock`, `global_dir`), and save through the real `save_settings()`.
"""

import ast
import inspect
import json
import textwrap
import threading
from unittest.mock import Mock

import pytest

from app_settings import _CONNECTION_GLOBAL, _GENERAL_GLOBAL, AppSettings
from core.settings_transfer import build_export
from main import MainWindow
from main_window import settings as settings_module
from ui.dialogs.settings_dialog import SettingsDialog


@pytest.fixture(autouse=True)
def _account_dir(tmp_path, monkeypatch):
    """settings.json goes to a scratch folder, never to the real data dir."""
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.setattr(settings_module, "data_path",
                        lambda *parts: str(account.joinpath(*parts)))


class _Window:
    def __init__(self, global_dir, general=None, connection=None):
        self.settings = {"general": dict(general or {}),
                         "connection": dict(connection or {})}
        self.global_dir = str(global_dir)
        self._save_lock = threading.Lock()
        # Raised from inside save_settings()'s own except: a save that failed
        # must fail the test, not pass it on whatever the file already held.
        self.error_sound = Mock(play=Mock(side_effect=AssertionError("save failed")))
        self.i18n = Mock()
        self.app_name = "WinZapp"
        self.apply_settings_live = Mock()

    _apply_global_settings = MainWindow._apply_global_settings
    _persist_global_settings = MainWindow._persist_global_settings
    save_settings = MainWindow.save_settings
    _save_settings_locked = MainWindow._save_settings_locked
    import_settings_from_file = MainWindow.import_settings_from_file


class _Radio:
    def __init__(self, value=False):
        self.value = value

    def SetValue(self, value):
        self.value = value

    def GetValue(self):
        return self.value


class _Dialog:
    """SettingsDialog carrying only the two radio buttons and the window."""

    def __init__(self, main_window):
        self.main_window = main_window
        self._switch_behavior_single_rb = _Radio()
        self._switch_behavior_keep_open_rb = _Radio()

    _install_wide_settings = SettingsDialog._install_wide_settings
    _load_switch_behavior = SettingsDialog._load_switch_behavior
    _apply_switch_behavior = SettingsDialog._apply_switch_behavior
    _global_control_changed = SettingsDialog._global_control_changed


def _started(global_dir, **sections):
    window = _Window(global_dir, **sections)
    window._apply_global_settings()
    return window


def _stored(global_dir, key):
    with open(AppSettings(str(global_dir))._path, encoding="utf-8") as f:
        return json.load(f).get(key)


def _choose(window, behavior):
    """What OK on the Settings dialog does with the account-switch choice."""
    dialog = _Dialog(window)
    dialog._load_switch_behavior()
    getattr(dialog, f"_switch_behavior_{behavior}_rb").SetValue(True)
    if behavior == "keep_open":
        dialog._switch_behavior_single_rb.SetValue(False)
    else:
        dialog._switch_behavior_keep_open_rb.SetValue(False)
    dialog._apply_switch_behavior()
    window.save_settings()


def _unrelated_save(window):
    """One of the dozens of saves that change no setting at all."""
    window.settings.setdefault("audio_playback", {})["audio_default_speed"] = 1.5
    window.save_settings()


class TestAnUnrelatedSaveDoesNotUndoAnotherAccount:
    def test_the_choice_survives_the_other_account_saving(self, tmp_path):
        a = _started(tmp_path, general={"switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})

        _choose(a, "keep_open")
        assert _stored(tmp_path, "switch_behavior") == "keep_open"

        _unrelated_save(b)

        assert _stored(tmp_path, "switch_behavior") == "keep_open"
        dialog = _Dialog(a)
        dialog._load_switch_behavior()
        assert dialog._switch_behavior_keep_open_rb.GetValue() is True
        assert dialog._switch_behavior_single_rb.GetValue() is False

    def test_every_global_key_is_covered_not_only_the_switch(self, tmp_path):
        """The fix is in the mirroring, so the tray icon and the connection
        block are protected the same way."""
        a = _started(tmp_path)
        b = _started(tmp_path)

        a.settings["general"]["show_tray_icon"] = False
        a.settings["connection"]["wpp_server"] = "http://192.0.2.10"
        a.save_settings()
        _unrelated_save(b)

        assert _stored(tmp_path, "show_tray_icon") is False
        assert _stored(tmp_path, "wpp_server") == "http://192.0.2.10"


class TestTheOtherAccountSeesTheChange:
    def test_its_next_save_pulls_the_shared_value(self, tmp_path):
        a = _started(tmp_path, general={"switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})

        _choose(a, "keep_open")
        _unrelated_save(b)

        assert b.settings["general"]["switch_behavior"] == "keep_open"
        assert b._global_settings_snapshot["switch_behavior"] == "keep_open"

    def test_and_can_then_change_it_back(self, tmp_path):
        """Pulled into the snapshot too, so B choosing the value it started
        with is still read as a change once B has seen A's."""
        a = _started(tmp_path, general={"switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})
        _choose(a, "keep_open")
        _unrelated_save(b)

        b.settings["general"]["switch_behavior"] = "single"
        b.save_settings()

        assert _stored(tmp_path, "switch_behavior") == "single"


class TestTwoExplicitChangesOfTheSameKey:
    def test_the_later_one_wins(self, tmp_path):
        a = _started(tmp_path, general={"switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})

        _choose(a, "keep_open")
        _choose(b, "single")

        assert _stored(tmp_path, "switch_behavior") == "single"
        _unrelated_save(a)
        assert _stored(tmp_path, "switch_behavior") == "single"
        assert a.settings["general"]["switch_behavior"] == "single"

    def test_the_later_one_wins_through_the_local_copy_alone(self, tmp_path):
        """Not only through the dialog's direct write: a changed local value
        is written on that account's next save."""
        a = _started(tmp_path)
        b = _started(tmp_path)

        a.settings["general"]["show_tray_icon"] = False
        a.save_settings()
        b.settings["general"]["updates_enabled"] = False
        b.save_settings()
        b.settings["general"]["show_tray_icon"] = True
        b.save_settings()

        assert _stored(tmp_path, "show_tray_icon") is True
        assert _stored(tmp_path, "updates_enabled") is False


class TestASettingsImportIsWritten:
    def _export(self, tmp_path, general):
        path = tmp_path / "export.json"
        path.write_text(json.dumps(build_export({"general": general})), encoding="utf-8")
        return str(path)

    def test_the_imported_value_reaches_the_shared_file(self, tmp_path):
        a = _started(tmp_path, general={"switch_behavior": "single"})

        error, applied = a.import_settings_from_file(
            self._export(tmp_path, {"switch_behavior": "keep_open"}))

        assert (error, applied) == ("", 1)
        assert _stored(tmp_path, "switch_behavior") == "keep_open"
        a.apply_settings_live.assert_called_once()

    def test_even_when_it_equals_the_value_this_account_started_with(self, tmp_path):
        """By difference alone the import would read as "unchanged" here and
        the other account's value would be pulled over it. An import is an
        explicit choice, so it is written."""
        a = _started(tmp_path, general={"switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})
        _choose(b, "keep_open")

        a.import_settings_from_file(self._export(tmp_path, {"switch_behavior": "single"}))

        assert _stored(tmp_path, "switch_behavior") == "single"
        assert a.settings["general"]["switch_behavior"] == "single"

    def test_a_key_the_file_does_not_carry_is_not_forced(self, tmp_path):
        a = _started(tmp_path, general={"switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})
        _choose(b, "keep_open")

        a.import_settings_from_file(self._export(tmp_path, {"notifications_enabled": False}))

        assert _stored(tmp_path, "switch_behavior") == "keep_open"


class TestFirstRun:
    def test_no_shared_file_is_seeded_from_the_account(self, tmp_path):
        _started(tmp_path, general={"switch_behavior": "keep_open",
                                    "first_run": False})

        assert _stored(tmp_path, "switch_behavior") == "keep_open"
        assert _stored(tmp_path, "first_run") is False

    def test_a_key_the_shared_file_lacks_is_seeded_on_save(self, tmp_path):
        """Every save used to write every key, so the file ends up complete
        exactly as before; there was nobody's choice there to overwrite."""
        window = _started(tmp_path)

        window.save_settings()

        stored = json.loads(open(AppSettings(str(tmp_path))._path, encoding="utf-8").read())
        assert stored["wpp_api_key"] == window.settings["connection"]["wpp_api_key"]
        assert stored["switch_behavior"] == "single"

    def test_a_new_account_takes_the_install_wide_values(self, tmp_path):
        a = _started(tmp_path, general={"switch_behavior": "single"})
        _choose(a, "keep_open")

        fresh = _started(tmp_path, general={"switch_behavior": "single"})
        _unrelated_save(fresh)

        assert fresh.settings["general"]["switch_behavior"] == "keep_open"
        assert _stored(tmp_path, "switch_behavior") == "keep_open"


class TestWithoutABaseline:
    def test_a_half_started_window_never_writes_a_stale_copy(self, tmp_path):
        """_apply_global_settings() failing after it set _app_settings leaves
        no snapshot. With no baseline a stale copy and a choice look the
        same, so the shared value is kept rather than overwritten."""
        app = AppSettings(str(tmp_path))
        app.set("switch_behavior", "keep_open")
        window = _Window(tmp_path, general={"switch_behavior": "single"})
        window._app_settings = app

        window.save_settings()

        assert _stored(tmp_path, "switch_behavior") == "keep_open"
        assert window.settings["general"]["switch_behavior"] == "keep_open"


def _while_the_file_is_read(window, change):
    """Run `change` inside window's next app.all(): after the comparison, before
    the pull — where the Settings dialog's OK, which writes self.settings without
    `_save_lock`, can land during a background save."""
    app = window._app_settings
    real_all = app.all
    pending = [change]

    def _all():
        result = real_all()
        if pending:
            pending.pop()()
        return result

    app.all = _all


class TestAChangeMadeDuringTheSaveSurvives:
    """Reproduced by review with the write interleaved inside app.all(): the
    background save compared `language`, found it equal to the snapshot and
    read the file; OK set "en-US" meanwhile; the pull wrote "pt-BR" back over
    it, and OK's own save then found "equal to the snapshot" and wrote
    nothing. The choice was gone without a word."""

    def test_the_pull_leaves_it_and_the_next_save_writes_it(self, tmp_path):
        a = _started(tmp_path, general={"language": "pt-BR"})

        _while_the_file_is_read(
            a, lambda: a.settings["general"].__setitem__("language", "en-US"))
        _unrelated_save(a)

        assert a.settings["general"]["language"] == "en-US"
        assert _stored(tmp_path, "language") == "pt-BR"  # not written yet...

        a.save_settings()  # ...OK's own save, right after

        assert _stored(tmp_path, "language") == "en-US"
        assert a.settings["general"]["language"] == "en-US"

    def test_the_other_keys_are_still_pulled(self, tmp_path):
        a = _started(tmp_path, general={"language": "pt-BR", "switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})
        _choose(b, "keep_open")

        _while_the_file_is_read(
            a, lambda: a.settings["general"].__setitem__("language", "en-US"))
        _unrelated_save(a)

        assert a.settings["general"]["switch_behavior"] == "keep_open"
        assert a._global_settings_snapshot["switch_behavior"] == "keep_open"

    def test_even_when_it_is_the_value_the_file_holds(self, tmp_path):
        """The snapshot keeps the value compared, not the file's. From the
        file it would read "unchanged" whenever the new value and the file
        agree — here B set "en-US" and A's OK chose "en-US" during the save —
        and the next save would pull whatever the file says by then instead
        of writing the choice."""
        a = _started(tmp_path, general={"language": "pt-BR"})
        b = _started(tmp_path, general={"language": "pt-BR"})
        b.settings["general"]["language"] = "en-US"
        b.save_settings()

        _while_the_file_is_read(
            a, lambda: a.settings["general"].__setitem__("language", "en-US"))
        _unrelated_save(a)

        assert a.settings["general"]["language"] == "en-US"
        assert a._global_settings_snapshot["language"] == "pt-BR"
        # Another account changes it before A's OK saves: A's choice is
        # written then, like any change saved after another account's — the
        # later write wins.
        AppSettings(str(tmp_path)).set("language", "es-ES")
        a.save_settings()
        assert _stored(tmp_path, "language") == "en-US"

    def test_a_change_back_during_the_save_of_the_first(self, tmp_path):
        """The written branch: A's save writes "keep_open" while OK sets
        "single" back. The snapshot becomes "keep_open", so "single" is a
        change on the next save and is written."""
        a = _started(tmp_path, general={"switch_behavior": "single"})
        a.settings["general"]["switch_behavior"] = "keep_open"

        _while_the_file_is_read(
            a, lambda: a.settings["general"].__setitem__("switch_behavior", "single"))
        a.save_settings()
        assert _stored(tmp_path, "switch_behavior") == "keep_open"
        assert a.settings["general"]["switch_behavior"] == "single"

        a.save_settings()

        assert _stored(tmp_path, "switch_behavior") == "single"



# ── The Settings dialog writes back only what was changed in it ─────────────
# Reproduced by review: OK wrote every install-wide control back, touched or
# not. (i) A's dialog opens on "single", B chooses "keep_open", A presses OK
# without touching the radio: app.json says "single" again, no save needed in
# between. (ii) The General/connection keys the same way, through a
# background save between the opening and OK. And an untouched OK in A
# switched A's language, because B had changed it.

_LANGS = ["pt-BR", "en-US", "es-ES"]

#: Global keys the dialog has no control for, and why. Together with
#: _global_control_values() they must cover _GENERAL_GLOBAL/_CONNECTION_GLOBAL.
NOT_EDITED_BY_THE_DIALOG = {
    "first_run": "set by the first-run questions",
    "hotkey_first_run_asked": "set by the first-run questions",
    "api_type_first_run_asked": "set by the first-run questions",
    "autostart": ("the box mirrors the Windows Run entry and is applied through "
                  "MainWindow._apply_autostart() only when it differs from it"),
}

#: A value other than the default for each key the dialog edits.
_ANOTHER_VALUE = {
    "language": "es-ES",
    "updates_enabled": False,
    "alpha_updates_enabled": True,
    "show_tray_icon": False,
    "switch_behavior": "keep_open",
    "wpp_custom_api": True,
    "wpp_server": "http://192.0.2.10",
    "wpp_ws_server": "ws://192.0.2.10",
    "wpp_api_key": "outra-chave",
}


class _Combo:
    def __init__(self):
        self.selection = -1

    def SetSelection(self, index):
        self.selection = index

    def GetSelection(self):
        return self.selection


class _SettingsDialog(_Dialog):
    """SettingsDialog with its install-wide controls only, driving the real
    load and apply halves."""

    def __init__(self, main_window):
        super().__init__(main_window)
        self._lang_codes = list(_LANGS)
        self._lang_combo = _Combo()
        self._updates_check = _Radio()
        self._alpha_updates_check = _Radio()
        self._tray_icon_check = _Radio()
        self._custom_api_check = _Radio()
        self._server_field = _Radio("")
        self._ws_server_field = _Radio("")
        self._api_key_field = _Radio("")

    _global_control_values = SettingsDialog._global_control_values
    _load_install_wide_values = SettingsDialog._load_install_wide_values
    _apply_install_wide_values = SettingsDialog._apply_install_wide_values


def _opened(window):
    window.i18n.language = window.settings["general"].get("language") or "pt-BR"
    window.tray_icon = None
    window._init_tray = Mock()
    dialog = _SettingsDialog(window)
    dialog._load_install_wide_values()
    return dialog


def _ok(dialog):
    """What OK (or Apply) does with the install-wide half, then its save.
    Returns whether the language changed."""
    language_changed = dialog._apply_install_wide_values()
    dialog.main_window.save_settings()
    return language_changed


def _section_of(key):
    return "connection" if key in _CONNECTION_GLOBAL else "general"


def _change_elsewhere(window, key, value):
    window.settings[_section_of(key)][key] = value
    window.save_settings()


def _set_control(dialog, key, value):
    if key == "language":
        dialog._lang_combo.SetSelection(_LANGS.index(value))
    elif key == "switch_behavior":
        dialog._switch_behavior_keep_open_rb.SetValue(value == "keep_open")
        dialog._switch_behavior_single_rb.SetValue(value != "keep_open")
    else:
        control = {
            "updates_enabled": dialog._updates_check,
            "alpha_updates_enabled": dialog._alpha_updates_check,
            "show_tray_icon": dialog._tray_icon_check,
            "wpp_custom_api": dialog._custom_api_check,
            "wpp_server": dialog._server_field,
            "wpp_ws_server": dialog._ws_server_field,
            "wpp_api_key": dialog._api_key_field,
        }[key]
        control.SetValue(value)


def _written_key(node):
    """KEY of `<...settings...>[KEY] = ...` or `<...settings...>.set(KEY, ...)`."""
    if (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
            and "settings" in ast.unparse(node.targets[0].value)):
        return node.targets[0].slice.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "set" and node.args
            and isinstance(node.args[0], ast.Constant)
            and "settings" in ast.unparse(node.func.value)):
        return node.args[0].value
    return None


def _under_guard(node, parents, key):
    """Inside `if self._global_control_changed(KEY, ...):`, same KEY."""
    while node in parents:
        node = parents[node]
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                and ast.unparse(node.test.func) == "self._global_control_changed"
                and isinstance(node.test.args[0], ast.Constant)
                and node.test.args[0].value == key):
            return True
    return False


class TestEveryInstallWideControlIsKnown:
    def test_the_dialog_edits_global_keys_and_the_rest_are_named(self, tmp_path):
        every = set(_GENERAL_GLOBAL) | set(_CONNECTION_GLOBAL)
        edited = set(_opened(_started(tmp_path))._global_control_values())

        assert edited <= every
        assert every - edited == set(NOT_EDITED_BY_THE_DIALOG)
        assert set(_ANOTHER_VALUE) == edited

    def test_every_global_write_on_ok_is_behind_the_check(self):
        """By source, for _apply_values() itself, which no stub can run: a
        global key written there unguarded would bring the bug back."""
        every = set(_GENERAL_GLOBAL) | set(_CONNECTION_GLOBAL)
        guarded, unguarded, port = set(), [], []
        for name in ("_apply_values", "_apply_install_wide_values", "_apply_switch_behavior"):
            tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(SettingsDialog, name))))
            parents = {child: node for node in ast.walk(tree)
                       for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                key = _written_key(node)
                if key == "wpp_port":
                    port.append(_under_guard(node, parents, key))
                if key not in every:
                    continue
                if _under_guard(node, parents, key):
                    guarded.add(key)
                else:
                    unguarded.append(f"{name}: {key}")

        assert unguarded == []
        assert guarded == set(_ANOTHER_VALUE)
        # The port is this account's own, not install-wide: written every
        # time, as before.
        assert port == [False]


class TestAnUntouchedDialogDoesNotUndoAnotherAccount:
    def test_the_account_switch_choice(self, tmp_path):
        """(i): no save in between — the radio went straight to the file."""
        a = _started(tmp_path, general={"switch_behavior": "single"})
        b = _started(tmp_path, general={"switch_behavior": "single"})
        dialog = _opened(a)

        _choose(b, "keep_open")
        _ok(dialog)

        assert _stored(tmp_path, "switch_behavior") == "keep_open"
        assert a.settings["general"]["switch_behavior"] == "keep_open"

    @pytest.mark.parametrize("key", sorted(_ANOTHER_VALUE))
    def test_any_key_through_a_background_save(self, tmp_path, key):
        """(ii): the background save pulls B's value into A's copy and
        snapshot, so OK writing the control back read as A's change."""
        a = _started(tmp_path)
        b = _started(tmp_path)
        dialog = _opened(a)

        _change_elsewhere(b, key, _ANOTHER_VALUE[key])
        _unrelated_save(a)
        _ok(dialog)

        assert _stored(tmp_path, key) == _ANOTHER_VALUE[key]
        assert a.settings[_section_of(key)][key] == _ANOTHER_VALUE[key]

    @pytest.mark.parametrize("key", sorted(_ANOTHER_VALUE))
    def test_any_key_with_no_save_in_between(self, tmp_path, key):
        a = _started(tmp_path)
        b = _started(tmp_path)
        dialog = _opened(a)

        _change_elsewhere(b, key, _ANOTHER_VALUE[key])
        _ok(dialog)

        assert _stored(tmp_path, key) == _ANOTHER_VALUE[key]

    def test_nor_switches_this_windows_language(self, tmp_path):
        """B changed it, A's copy pulled it, A's dialog shows it: OK without
        touching the combo is not A choosing a language."""
        a = _started(tmp_path, general={"language": "pt-BR"})
        b = _started(tmp_path, general={"language": "pt-BR"})
        _change_elsewhere(b, "language", "en-US")
        _unrelated_save(a)
        dialog = _opened(a)
        a.i18n.language = "pt-BR"  # what A is still showing

        assert _ok(dialog) is False
        assert _stored(tmp_path, "language") == "en-US"

    def test_nor_touches_the_tray_icon(self, tmp_path):
        a = _started(tmp_path, general={"show_tray_icon": False})
        b = _started(tmp_path, general={"show_tray_icon": False})
        dialog = _opened(a)

        _change_elsewhere(b, "show_tray_icon", True)
        _unrelated_save(a)
        _ok(dialog)

        a._init_tray.assert_not_called()


class TestAChangeMadeInTheDialogIsWritten:
    @pytest.mark.parametrize("key", sorted(_ANOTHER_VALUE))
    def test_every_control(self, tmp_path, key):
        a = _started(tmp_path)
        dialog = _opened(a)

        _set_control(dialog, key, _ANOTHER_VALUE[key])
        _ok(dialog)

        assert _stored(tmp_path, key) == _ANOTHER_VALUE[key]
        assert a.settings[_section_of(key)][key] == _ANOTHER_VALUE[key]

    def test_and_wins_over_an_earlier_change_elsewhere(self, tmp_path):
        a = _started(tmp_path)
        b = _started(tmp_path)
        dialog = _opened(a)

        _change_elsewhere(b, "wpp_server", "http://192.0.2.10")
        _unrelated_save(a)
        dialog._server_field.SetValue("http://192.0.2.20")
        _ok(dialog)
        _unrelated_save(b)

        assert _stored(tmp_path, "wpp_server") == "http://192.0.2.20"
        assert b.settings["connection"]["wpp_server"] == "http://192.0.2.20"
        assert a.wpp_server == "http://192.0.2.20"

    def test_a_language_chosen_here_switches_the_window(self, tmp_path):
        a = _started(tmp_path, general={"language": "pt-BR"})
        b = _started(tmp_path, general={"language": "pt-BR"})
        dialog = _opened(a)

        _change_elsewhere(b, "language", "en-US")
        _unrelated_save(a)
        dialog._lang_combo.SetSelection(_LANGS.index("es-ES"))

        assert _ok(dialog) is True
        assert _stored(tmp_path, "language") == "es-ES"

    def test_the_tray_icon_follows_a_change_made_here(self, tmp_path):
        a = _started(tmp_path, general={"show_tray_icon": False})
        dialog = _opened(a)

        dialog._tray_icon_check.SetValue(True)
        _ok(dialog)

        a._init_tray.assert_called_once()

    def test_ok_after_apply_does_not_write_it_again(self, tmp_path):
        """What Apply wrote is the new baseline: OK right after, untouched
        since, is not a second choice over B's change in between."""
        a = _started(tmp_path)
        b = _started(tmp_path)
        dialog = _opened(a)
        dialog._server_field.SetValue("http://192.0.2.20")
        dialog._lang_combo.SetSelection(_LANGS.index("en-US"))
        assert _ok(dialog) is True  # Apply

        _change_elsewhere(b, "wpp_server", "http://192.0.2.30")
        assert _ok(dialog) is False  # OK

        assert _stored(tmp_path, "wpp_server") == "http://192.0.2.30"
