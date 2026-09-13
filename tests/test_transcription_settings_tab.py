"""The Transcrição tab: what it says out loud, and what it must not break.

Four failures this pins, each of which is silent in a different way.

* **A tab inserted in the middle renumbers every other one.** The settings
  dialog addresses its pages by hardcoded index — `_notebook.SetSelection(8)`
  here, `SetSelection(4)` in main.py — and `_refresh_dialog_labels()` retitles
  them by position. Appending is the one position that shifts nothing, but the
  new index still owes that enumeration a line of its own: without it the tab
  keeps its old caption after a language change, and nothing fails.

* **A combobox item is one accessibility object.** The screen reader reads the
  whole string and nothing else, so "medium", "equilibrado", how big it is and
  whether it is already downloaded all have to be inside that one line — and a
  placeholder a locale forgot to fill leaves a literal `{size}` to be read out.

* **The same warning, every single time the dialog opens — or never.**
  `resolve()` reports a substituted value and records nothing;
  `sanitize_section()` is what makes the report stop. Calling them in the wrong
  order sanitizes first and the user is never told at all; calling only the
  first tells them again tomorrow. And calling either of them from
  `_load_transcription_values()`, which runs on every open of the dialog
  whatever tab the user came for, spends the warning on a tab that was never
  put on screen: the value is rewritten, the sentence is never delivered, and
  the choice is gone with no cue of any kind. So the tab writes back only what
  it actually presented.

* **Seconds of driver I/O to open Ctrl+, .** `device.probe_hardware()` imports
  ctranslate2, asks NVML and LoadLibrary's cuBLAS. On the path of building the
  dialog that blocks the wx thread before there is a window for a screen reader
  to announce, and nothing on the tab needed the answer at that point — the
  substitutions do not depend on the probe at all.

* **The CUDA libraries silently forgotten between sessions.** The folder a
  previous run downloaded into is on no loader search path when this process
  starts, so `register_installed_runtime()` has to run at startup and before
  the first device decision, or a user who paid for 550 MB is back on the
  processor with nothing in the log saying why.

SettingsDialog itself is never constructed here: it is a wx.Dialog, which the
suite may not put on the desktop at all (see
tests/test_no_desktop_visible_windows.py). The tab is built by binding its own
unbound methods onto a stub and handing them conftest's off-screen frame — the
house pattern, and the reason the page builder takes its parent as an argument.
"""

import ast
import json
import pathlib
import re

import pytest
import wx

from app_paths import resource_path
from core.transcription import backend as backend_module
from core.transcription import cuda_runtime, device, model_catalog, model_store
from core.transcription import preferences
from ui.dialogs import settings_dialog
from ui.dialogs.settings_dialog import SettingsDialog

from tests.conftest import hidden_frame

REPO = pathlib.Path(__file__).resolve().parent.parent
SETTINGS_DIALOG_SOURCE = (
    REPO / "client" / "ui" / "dialogs" / "settings_dialog.py"
).read_text(encoding="utf-8")


def _load(name):
    with open(resource_path("languages", f"{name}.json"), "r", encoding="utf-8") as f:
        return json.load(f)


LOCALES = sorted(_load("language_map"))

# A machine with plenty of RAM and no graphics card — the common case, and the
# one where every "automatic" has an unambiguous answer.
_CPU_ONLY = device.HardwareProbe(total_ram_mb=16_384, available_ram_mb=12_288)


class _I18n:
    """The real translation table, so the assertions are about real strings."""

    def __init__(self, locale="pt-BR"):
        self.language = locale
        self._table = _load(locale)

    def t(self, key):
        return self._table.get(key, key)


class _AppSettings:
    """app_settings, minus the file. Raises for a non-global key like it does."""

    def __init__(self, models_dir=""):
        self._values = {preferences.MODELS_DIR_SETTING: models_dir}

    def get(self, key):
        if key not in self._values:
            raise KeyError(key)
        return self._values[key]

    def set(self, key, value):
        if key != preferences.MODELS_DIR_SETTING:
            raise KeyError(key)
        self._values[key] = value


class _SpeakOutput:
    """MainWindow.speak_output, minus accessible_output2. Records what was said.

    Deliberately spelled `output()` and nothing else: that is the single funnel
    every announcement in the app goes through, and it is what makes the two
    Settings > Acessibilidade toggles apply. A tab that reached for `Auto()`
    itself would speak over a user who asked for silence.
    """

    def __init__(self):
        self.spoken = []

    def output(self, text, interrupt=False):
        self.spoken.append(text)


class _MainWindow:
    def __init__(self, settings=None, locale="pt-BR", app_settings=None):
        self.settings = settings if settings is not None else {}
        self.i18n = _I18n(locale)
        # `_app_settings`, with the underscore, because that is the only name
        # MainWindow ever writes (main.py, _apply_global_settings()). A stub
        # spelling it without one is how the tab shipped reading an attribute
        # production does not have, with six green tests over it.
        self._app_settings = app_settings
        self.speak_output = _SpeakOutput()
        self.saves = 0

    def save_settings(self):
        self.saves += 1


class _TabOwner:
    """Stand-in for SettingsDialog carrying only what the tab touches."""

    def __init__(self, main_window):
        self.main_window = main_window
        self.dirtied = 0

    def _mark_dirty(self, event=None):
        # Same guard as SettingsDialog._mark_dirty: populating the controls
        # while the dialog opens is not an edit. Without it the stub would count
        # what production deliberately ignores, and a dirtied == 0 assertion
        # could only ever be written for code that never runs at load time.
        if getattr(self, "_loading_values", False):
            return
        self.dirtied += 1

    _build_transcription_page = SettingsDialog._build_transcription_page
    _transcription_app_settings = SettingsDialog._transcription_app_settings
    _stored_transcription_models_dir = SettingsDialog._stored_transcription_models_dir
    _show_transcription_models_dir = SettingsDialog._show_transcription_models_dir
    _populate_transcription_model_choices = (
        SettingsDialog._populate_transcription_model_choices
    )
    _populate_transcription_language_choices = (
        SettingsDialog._populate_transcription_language_choices
    )
    _populate_transcription_backend_choices = (
        SettingsDialog._populate_transcription_backend_choices
    )
    _selected_transcription_model = SettingsDialog._selected_transcription_model
    _selected_transcription_language = SettingsDialog._selected_transcription_language
    _selected_transcription_backend = SettingsDialog._selected_transcription_backend
    _select_transcription_model = SettingsDialog._select_transcription_model
    _select_transcription_language = SettingsDialog._select_transcription_language
    _select_transcription_backend = SettingsDialog._select_transcription_backend
    # Genuinely static on the dialog — rewrapped, or they would be handed the
    # stub as their first argument.
    _selected_id = staticmethod(SettingsDialog._selected_id)
    _select_id = staticmethod(SettingsDialog._select_id)
    _sync_transcription_language_controls = (
        SettingsDialog._sync_transcription_language_controls
    )
    _selected_transcription_device_preference = (
        SettingsDialog._selected_transcription_device_preference
    )
    _show_transcription_substitutions = SettingsDialog._show_transcription_substitutions
    _show_transcription_hardware_notices = (
        SettingsDialog._show_transcription_hardware_notices
    )
    _transcription_hardware_notice_keys = (
        SettingsDialog._transcription_hardware_notice_keys
    )
    _render_transcription_substitutions = (
        SettingsDialog._render_transcription_substitutions
    )
    _show_transcription_cuda_status = SettingsDialog._show_transcription_cuda_status
    _refresh_transcription_models = SettingsDialog._refresh_transcription_models
    _load_transcription_values = SettingsDialog._load_transcription_values
    _enter_transcription_page = SettingsDialog._enter_transcription_page
    _transcription_setting_may_be_written = (
        SettingsDialog._transcription_setting_may_be_written
    )
    _apply_transcription_values = SettingsDialog._apply_transcription_values
    _refresh_transcription_labels = SettingsDialog._refresh_transcription_labels
    _on_transcription_detect_language_toggle = (
        SettingsDialog._on_transcription_detect_language_toggle
    )
    _on_transcription_device_change = SettingsDialog._on_transcription_device_change
    _on_browse_transcription_models_dir = (
        SettingsDialog._on_browse_transcription_models_dir
    )


@pytest.fixture
def no_hardware_probe(monkeypatch):
    """Answer the machine's questions from the test instead of the machine.

    probe_hardware() loads NVML and cuda_runtime.installation_state() reads the
    install-wide folder — on the developer's own machine, which is exactly what
    device.py's docstring says a decision must never depend on.
    """
    monkeypatch.setattr(
        settings_dialog.transcription_device, "probe_hardware", lambda: _CPU_ONLY
    )
    monkeypatch.setattr(
        settings_dialog.cuda_runtime,
        "installation_state",
        lambda directory=None: cuda_runtime.RuntimeState(cuda_runtime.STATE_ABSENT, ()),
    )


@pytest.fixture
def tab(wx_app, tmp_path, no_hardware_probe):
    """A built Transcrição tab, on an off-screen parent, with an empty folder."""
    frame = hidden_frame()
    owner = _TabOwner(_MainWindow(app_settings=_AppSettings(str(tmp_path))))
    owner._transcription_page = owner._build_transcription_page(frame)
    # The dialog binds EVT_TEXT to _mark_dirty at dialog level and relies on
    # command-event propagation to catch every text control. Without the same
    # binding here, a field written with SetValue() — which fires EVT_TEXT even
    # for identical text — dirties the real dialog and nothing in this suite
    # can see it. That is exactly how merely arriving on the tab came to show
    # the Apply button.
    frame.Bind(wx.EVT_TEXT, owner._mark_dirty)
    try:
        yield owner
    finally:
        frame.Destroy()


class TestLookingIsNotEditing:
    """Arriving on the tab, or redrawing it, must not make Apply appear.

    The warning field, the CUDA status line and the folder field only *show*
    something. Written with SetValue() they fired EVT_TEXT, the dialog routed it
    to _mark_dirty, and a user who arrowed onto "Transcrição" and changed
    nothing got an Apply button and an extra tab stop — the behaviour
    _mark_dirty was introduced to remove.
    """

    def _open(self, tab):
        # As _load_values() does it in the real dialog: under the load guard.
        tab._loading_values = True
        try:
            tab._load_transcription_values()
        finally:
            tab._loading_values = False
        tab.dirtied = 0

    def test_arriving_on_the_tab_changes_nothing(self, tab):
        self._open(tab)
        tab._enter_transcription_page()
        assert tab.dirtied == 0

    def test_redrawing_what_the_folder_holds_changes_nothing(self, tab):
        self._open(tab)
        tab._enter_transcription_page()
        tab._refresh_transcription_models()
        assert tab.dirtied == 0

    def test_the_binding_can_see_a_real_edit(self, tab):
        """Guards the two tests above from passing because nothing is wired."""
        self._open(tab)
        tab._transcription_models_dir_field.SetValue("typed by hand")
        assert tab.dirtied == 1


class TestTheTabIsAppendedAtTheEnd:
    """Inserting one in the middle renumbers every hardcoded index there is."""

    def _add_pages(self):
        return re.findall(
            r"AddPage\(\s*[^,]+,\s*i18n\.t\(\"([a-z_]+)\"\)\)", SETTINGS_DIALOG_SOURCE
        )

    def _page_texts(self):
        return [
            (int(index), key)
            for index, key in re.findall(
                r"SetPageText\((\d+), i18n\.t\(\"([a-z_]+)\"\)\)",
                SETTINGS_DIALOG_SOURCE,
            )
        ]

    def test_the_transcription_tab_is_the_last_one_added(self):
        assert self._add_pages()[-1] == "tab_transcription"

    def test_the_retranslation_enumerates_every_page_at_its_own_index(self):
        """Missing line = a tab caption that never follows a language change."""
        added = self._add_pages()
        assert [key for _index, key in self._page_texts()] == added
        assert [index for index, _key in self._page_texts()] == list(range(len(added)))

    def test_no_hardcoded_page_selection_reaches_the_new_tab(self):
        """Every SetSelection() in this file names a page below the new one, so
        appending could not have moved what any of them points at."""
        selections = [
            int(index)
            for index in re.findall(
                r"_notebook\.SetSelection\((\d+)\)", SETTINGS_DIALOG_SOURCE
            )
        ]
        assert selections, "the hardcoded selections are what this guards"
        assert max(selections) < len(self._add_pages()) - 1


class TestTheStartupRegistrationIsWired:
    """Nothing else puts the downloaded CUDA folder on the loader's path."""

    @staticmethod
    def _main_window_init():
        source = (REPO / "client" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        return item
        raise AssertionError("MainWindow.__init__ not found")

    @staticmethod
    def _call_lines(function, name):
        return [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == name
        ]

    def test_it_is_called_from_main_window_init(self):
        init = self._main_window_init()
        assert self._call_lines(init, "register_installed_runtime")

    def test_it_runs_after_the_settings_are_loaded(self):
        """After load_settings(), so a configured folder is already readable."""
        init = self._main_window_init()
        assert min(self._call_lines(init, "register_installed_runtime")) > min(
            self._call_lines(init, "load_settings")
        )

    def test_it_runs_before_the_first_device_decision(self):
        """A guard for a call that does not exist yet, and says so.

        Registering after the probe answers the old question: the probe decides
        by loading the libraries, so a directory added afterwards is one the
        answer already ignored. `MainWindow.__init__` reaches neither
        `probe_hardware()` nor `resolve_device()` today — the transcription tab
        takes its own probe on first visit — so `probes` is empty and this
        assertion is vacuous *by design*. It is here to fail the day something
        in `__init__` starts asking, which is the only day it can be got wrong;
        an `assert probes` would fail today instead, for nothing.
        """
        init = self._main_window_init()
        registered = min(self._call_lines(init, "register_installed_runtime"))
        probes = self._call_lines(init, "probe_hardware") + self._call_lines(
            init, "resolve_device"
        )
        assert all(registered < line for line in probes)


class TestTheModelChoicesReadAsOneSentence:
    """A combobox item is a single accessibility object: everything the user
    needs to choose between two models has to be inside that one string."""

    @staticmethod
    def _model(model_id):
        return model_catalog.get_model(model_id)

    def test_an_installed_model_says_so_and_quotes_its_disk_size(self):
        i18n = _I18n()
        label = settings_dialog._transcription_model_choice_label(
            i18n, self._model("medium"), model_store.InstallState(model_store.STATE_INSTALLED)
        )
        assert label.startswith("medium: ")
        assert i18n.t("transcription_size_balanced") in label
        assert "1,4 GB" in label
        assert "instalado" in label

    def test_a_model_that_is_not_here_quotes_the_download_instead(self):
        i18n = _I18n()
        label = settings_dialog._transcription_model_choice_label(
            i18n, self._model("tiny"), model_store.InstallState(model_store.STATE_ABSENT)
        )
        assert "75 MB" in label
        assert "não instalado" in label

    def test_an_interrupted_download_is_neither_of_those(self):
        i18n = _I18n()
        states = {
            state: settings_dialog._transcription_model_choice_label(
                i18n, self._model("small"), model_store.InstallState(state)
            )
            for state in (
                model_store.STATE_INSTALLED,
                model_store.STATE_ABSENT,
                model_store.STATE_INCOMPLETE,
            )
        }
        assert len(set(states.values())) == 3

    def test_a_folder_that_could_not_be_measured_reads_as_not_installed(self):
        """None is what a caller with no answer passes, and telling the user a
        model is installed on that basis is the one wrong answer."""
        i18n = _I18n()
        assert settings_dialog._transcription_model_choice_label(
            i18n, self._model("base"), None
        ) == settings_dialog._transcription_model_choice_label(
            i18n, self._model("base"), model_store.InstallState(model_store.STATE_ABSENT)
        )

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_locale_leaves_a_placeholder_to_be_read_out(self, locale):
        i18n = _I18n(locale)
        for model in model_catalog.list_models():
            for state in (model_store.STATE_INSTALLED, model_store.STATE_ABSENT,
                          model_store.STATE_INCOMPLETE):
                label = settings_dialog._transcription_model_choice_label(
                    i18n, model, model_store.InstallState(state)
                )
                assert "{" not in label and "}" not in label, (locale, model.id)
                assert model.id in label

    def test_the_size_uses_the_locale_decimal_separator(self):
        assert settings_dialog._format_transcription_size(_I18n("pt-BR"), 1_610_612_736) \
            == "1,5 GB"
        assert settings_dialog._format_transcription_size(_I18n("en-US"), 1_610_612_736) \
            == "1.5 GB"

    def test_the_combobox_offers_automatic_first_and_then_the_catalogue(self, tab):
        combo = tab._transcription_model_combo
        assert combo.GetString(0) == tab.main_window.i18n.t("transcription_option_auto")
        assert tab._transcription_model_ids == [preferences.AUTO] + [
            model.id for model in model_catalog.list_models()
        ]
        # Nothing was downloaded into the fixture's folder, so every entry has
        # to say so — a list that claims otherwise sends the user to a run that
        # fails with MODEL_NOT_INSTALLED.
        for index in range(1, combo.GetCount()):
            assert "não instalado" in combo.GetString(index)

    def test_an_installed_model_shows_as_installed_in_the_list(
        self, tab, monkeypatch
    ):
        monkeypatch.setattr(
            settings_dialog.model_store,
            "installation_state",
            lambda root, model: model_store.InstallState(
                model_store.STATE_INSTALLED if model.id == "small"
                else model_store.STATE_ABSENT
            ),
        )
        tab._populate_transcription_model_choices()
        index = tab._transcription_model_ids.index("small")
        assert "instalado" in tab._transcription_model_combo.GetString(index)
        assert "não instalado" not in tab._transcription_model_combo.GetString(index)

    def test_rebuilding_the_list_keeps_the_selection(self, tab):
        tab._select_transcription_model("large-v3")
        tab._populate_transcription_model_choices()
        assert tab._selected_transcription_model() == "large-v3"


class TestTheLanguageControls:
    """The checkbox and the list answer two different questions — which is why
    they are two controls, and why one has to drive the other."""

    def test_the_list_starts_with_the_interface_language_sentinel(self, tab):
        assert tab._transcription_language_codes[0] == preferences.LANGUAGE_INTERFACE
        assert tab._transcription_language_combo.GetString(0) == tab.main_window.i18n.t(
            "transcription_language_interface"
        )

    def test_the_endonyms_are_offered_with_the_apps_own_language_first(self, tab):
        assert tab._transcription_language_codes[1] == "pt"
        assert tab._transcription_language_combo.GetString(1) == "português"

    def test_detecting_automatically_disables_the_list(self, tab):
        tab._transcription_detect_language_check.SetValue(True)
        tab._sync_transcription_language_controls()
        assert not tab._transcription_language_combo.IsEnabled()
        assert not tab._transcription_language_label.IsEnabled()

    def test_turning_detection_off_enables_it_again(self, tab):
        tab._transcription_detect_language_check.SetValue(True)
        tab._sync_transcription_language_controls()
        tab._transcription_detect_language_check.SetValue(False)
        tab._sync_transcription_language_controls()
        assert tab._transcription_language_combo.IsEnabled()
        assert tab._transcription_language_label.IsEnabled()

    def test_the_checkbox_handler_syncs_and_lets_the_event_through(self, tab):
        """The dialog-level _mark_dirty() only fires on events a control
        handler passes on with Skip()."""
        skipped = []

        class _Event:
            def Skip(self):
                skipped.append(True)

        tab._transcription_detect_language_check.SetValue(True)
        tab._on_transcription_detect_language_toggle(_Event())
        assert not tab._transcription_language_combo.IsEnabled()
        assert skipped == [True]


class TestEverySettingIsReadBackAndWritten:
    def test_the_defaults_load_as_the_automatic_positions(self, tab):
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == preferences.AUTO
        assert tab._transcription_device_radio.GetSelection() == 0
        assert tab._transcription_detect_language_check.GetValue() is True
        assert tab._selected_transcription_language() == preferences.LANGUAGE_INTERFACE

    def test_a_stored_choice_is_selected_when_the_tab_opens(self, tab):
        tab.main_window.settings["transcription"] = {
            "model": "large-v3-turbo",
            "device": device.PREFERENCE_CPU,
            "language": "pl",
            "auto_detect_language": False,
        }
        tab._load_transcription_values()
        assert tab._selected_transcription_model() == "large-v3-turbo"
        assert tab._transcription_device_radio.GetSelection() == 2
        assert tab._selected_transcription_language() == "pl"
        assert tab._transcription_detect_language_check.GetValue() is False
        assert tab._transcription_language_combo.IsEnabled()

    def test_what_the_user_picks_is_what_reaches_settings_json(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("small")
        tab._transcription_device_radio.SetSelection(1)
        tab._transcription_detect_language_check.SetValue(False)
        tab._select_transcription_language("es")
        tab._apply_transcription_values()

        section = tab.main_window.settings["transcription"]
        assert section["model"] == "small"
        assert section["device"] == device.PREFERENCE_CUDA
        assert section["auto_detect_language"] is False
        assert section["language"] == "es"

    def test_the_language_survives_detection_being_turned_back_on(self, tab):
        """It is a preference, not an instruction: preferred_language() answers
        it whatever the checkbox says, so dropping it would lose a choice the
        user has no way of getting back."""
        tab._load_transcription_values()
        tab._select_transcription_language("pl")
        tab._transcription_detect_language_check.SetValue(True)
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["language"] == "pl"

    def test_a_round_trip_through_the_tab_changes_nothing_on_its_own(self, tab):
        stored = {
            "backend": preferences.AUTO,
            "model": "base",
            "device": device.PREFERENCE_CUDA,
            "language": "fr",
            "auto_detect_language": False,
        }
        tab.main_window.settings["transcription"] = dict(stored)
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"] == stored

    def test_the_backend_key_is_left_alone_while_there_is_nothing_to_pick(self, tab):
        """One backend means there is nothing to choose, and a combobox with a
        single entry is a tab stop that answers nothing — so the tab neither
        offers the setting nor writes it, and a value stored by a build that
        did offer it survives untouched."""
        assert tab._transcription_backend_combo is None
        stored = backend_module.BACKEND_FASTER_WHISPER
        tab.main_window.settings["transcription"] = {"backend": stored}
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["backend"] == stored


class TestTheModelsFolder:
    def test_the_field_shows_the_resolved_folder(self, tab, tmp_path):
        tab._load_transcription_values()
        assert tab._transcription_models_dir_field.GetValue() == str(tmp_path)

    def test_an_empty_setting_resolves_to_the_default_folder(self, wx_app, no_hardware_probe):
        frame = hidden_frame()
        try:
            owner = _TabOwner(_MainWindow(app_settings=_AppSettings("")))
            owner._transcription_page = owner._build_transcription_page(frame)
            assert owner._transcription_models_dir == ""
            assert owner._transcription_models_dir_field.GetValue() == (
                model_store.default_models_dir()
            )
        finally:
            frame.Destroy()

    def test_the_field_is_not_typed_into(self, tab):
        """Writing the resolved path back would freeze a data folder that is
        meant to be copyable to another machine."""
        assert not tab._transcription_models_dir_field.IsEditable()

    def test_a_changed_folder_is_written_install_wide(self, tab, tmp_path):
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "elsewhere")
        tab._apply_transcription_values()
        assert tab.main_window._app_settings.get(preferences.MODELS_DIR_SETTING) == str(
            tmp_path / "elsewhere"
        )
        # And never into this account's own settings.json — the files are
        # shared by every account.
        assert preferences.MODELS_DIR_SETTING not in tab.main_window.settings.get(
            "transcription", {}
        )

    def test_an_unchanged_folder_is_not_rewritten(self, tab, tmp_path):
        written = []
        tab.main_window._app_settings.set = lambda key, value: written.append(key)
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert written == []

    def test_a_window_without_app_settings_still_opens_the_tab(
        self, wx_app, no_hardware_probe
    ):
        """account-less/legacy windows: the tab must open, on the default."""
        frame = hidden_frame()
        try:
            owner = _TabOwner(_MainWindow(app_settings=None))
            owner._transcription_page = owner._build_transcription_page(frame)
            owner._load_transcription_values()
            owner._apply_transcription_values()
            assert owner._transcription_models_dir == ""
        finally:
            frame.Destroy()


class TestTheSubstitutionWarningIsSaidOnceAndOnce:
    def test_a_retired_model_is_reported_when_the_tab_opens(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert tab._transcription_substituted_field.GetValue() == tab.main_window.i18n.t(
            "transcription_substituted_model"
        )
        assert tab._transcription_substituted_field.IsShown()

    def test_the_stored_value_itself_is_never_read_out(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert "whisper-from-2019" not in tab._transcription_substituted_field.GetValue()

    def test_it_is_spoken_when_the_tab_is_actually_selected(self, tab):
        """A read-only field on a tab nobody selected is a cue for nobody, and
        under NVDA it is not one even with the tab open until focus reaches it.
        Through speak_output, never a second accessible_output2 of our own."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert tab.main_window.speak_output.spoken == []

        tab._enter_transcription_page()
        assert tab.main_window.speak_output.spoken == [
            tab.main_window.i18n.t("transcription_substituted_model")
        ]

    def test_it_is_spoken_once_per_opening_however_often_the_tab_is_revisited(
        self, tab
    ):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._enter_transcription_page()
        tab._enter_transcription_page()
        tab._enter_transcription_page()
        assert len(tab.main_window.speak_output.spoken) == 1

    def test_nothing_is_spoken_when_there_was_nothing_to_replace(self, tab):
        tab.main_window.settings["transcription"] = dict(preferences.DEFAULTS)
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.speak_output.spoken == []

    def test_opening_the_dialog_without_visiting_the_tab_rewrites_nothing(self, tab):
        """The bug this pins: the user opens Configurações to change the
        interface language, the notebook is on page 0, and the warning is drawn
        and consumed on a tab nobody saw. sanitize_section() is what consumes
        it, so it may not run until the tab has been on screen."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        assert tab.main_window.saves == 0
        assert tab.main_window.settings["transcription"]["model"] == "whisper-from-2019"

    def test_the_second_opening_says_nothing(self, tab):
        """sanitize_section() rewrote the dead value, which is the whole point
        of calling it after resolve() rather than instead of it — and after the
        sentence has actually been delivered."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.saves == 1

        tab._transcription_page_seen = False
        tab._load_transcription_values()
        assert tab._transcription_substituted_field.GetValue() == ""
        assert not tab._transcription_substituted_field.IsShown()
        tab._enter_transcription_page()
        assert tab.main_window.saves == 1

    def test_a_settings_file_with_nothing_wrong_is_never_saved_on_open(self, tab):
        tab.main_window.settings["transcription"] = dict(preferences.DEFAULTS)
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab.main_window.saves == 0
        assert not tab._transcription_substituted_field.IsShown()

    def test_a_model_that_is_merely_not_downloaded_is_not_a_substitution(self, tab):
        """MODEL_NOT_INSTALLED is an offer to download, not a value that was
        replaced — warning about it would be a warning about nothing."""
        tab.main_window.settings["transcription"] = {"model": "large-v3"}
        tab._load_transcription_values()
        assert tab._transcription_substituted_field.GetValue() == ""
        assert tab._selected_transcription_model() == "large-v3"


class TestTheTabWritesBackOnlyWhatItShowed:
    """An OK pressed from another tab must not consume a warning that tab never
    delivered. The controls show `resolve()`'s *replacement*, so writing them
    back is the silent swap preferences.py exists to prevent — one step worse
    than the original, because now it is on disk."""

    def test_a_substituted_model_survives_an_ok_from_another_tab(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        # The combobox has already fallen back to "Automático" — that is the
        # value that must not reach settings.json unannounced.
        assert tab._selected_transcription_model() == preferences.AUTO

        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["model"] == "whisper-from-2019"

    def test_the_same_ok_after_visiting_the_tab_does_write_it(self, tab):
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._enter_transcription_page()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["model"] == preferences.AUTO

    def test_a_substituted_language_survives_it_too(self, tab):
        tab.main_window.settings["transcription"] = {
            "language": "klingon", "auto_detect_language": False,
        }
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["language"] == "klingon"

    def test_a_substituted_device_survives_it_too(self, tab):
        tab.main_window.settings["transcription"] = {"device": "quantum"}
        tab._load_transcription_values()
        tab._apply_transcription_values()
        assert tab.main_window.settings["transcription"]["device"] == "quantum"

    def test_everything_else_is_written_from_any_tab(self, tab):
        """The gate is only ever about a substituted value: every other control
        is a faithful copy of what is stored, and a dialog that stopped writing
        those would break Apply for the user who did use the tab."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab._transcription_detect_language_check.SetValue(False)
        tab._select_transcription_language("es")
        tab._apply_transcription_values()

        section = tab.main_window.settings["transcription"]
        assert section["auto_detect_language"] is False
        assert section["language"] == "es"
        assert section["device"] == device.PREFERENCE_AUTO


class TestTheCudaStatusLine:
    """Four situations, and cuda_runtime tells apart the two that share a
    state precisely so this line can say which."""

    def test_absent_says_what_the_download_costs(self):
        i18n = _I18n()
        text = settings_dialog._transcription_cuda_status_text(
            i18n, cuda_runtime.RuntimeState(cuda_runtime.STATE_ABSENT, ("cublas64_12.dll",))
        )
        assert text.startswith(i18n.t("transcription_cuda_runtime_absent").split(".")[0])
        assert settings_dialog._format_transcription_size(
            i18n, cuda_runtime.WHEEL_BYTES
        ) in text

    def test_installed_says_so(self):
        i18n = _I18n()
        assert settings_dialog._transcription_cuda_status_text(
            i18n, cuda_runtime.RuntimeState(cuda_runtime.STATE_INSTALLED, ())
        ) == i18n.t("transcription_cuda_runtime_installed")

    def test_an_earlier_pin_asks_for_an_update_not_for_a_repair(self):
        i18n = _I18n()
        assert settings_dialog._transcription_cuda_status_text(
            i18n,
            cuda_runtime.RuntimeState(cuda_runtime.STATE_INCOMPLETE, (), "12.0.0.0"),
        ) == i18n.t(cuda_runtime.OUTDATED_I18N_KEY)

    def test_missing_files_win_over_the_version(self):
        """Both signals are present at once when a download of the *old* pin
        was interrupted, and "finish the download" describes that directory."""
        i18n = _I18n()
        assert settings_dialog._transcription_cuda_status_text(
            i18n,
            cuda_runtime.RuntimeState(
                cuda_runtime.STATE_INCOMPLETE, ("cublas64_12.dll",), "12.0.0.0"
            ),
        ) == i18n.t("transcription_cuda_runtime_incomplete")

    def test_the_line_is_on_the_tab_and_not_typed_into(self, tab):
        tab._load_transcription_values()
        assert tab._transcription_cuda_field.GetValue() == (
            settings_dialog._transcription_cuda_status_text(
                tab.main_window.i18n,
                cuda_runtime.RuntimeState(cuda_runtime.STATE_ABSENT, ()),
            )
        )
        assert not tab._transcription_cuda_field.IsEditable()

    @pytest.mark.parametrize("locale", LOCALES)
    def test_no_locale_leaves_a_placeholder_in_it(self, locale):
        i18n = _I18n(locale)
        for state in (cuda_runtime.STATE_ABSENT, cuda_runtime.STATE_INCOMPLETE,
                      cuda_runtime.STATE_INSTALLED):
            text = settings_dialog._transcription_cuda_status_text(
                i18n, cuda_runtime.RuntimeState(state, ())
            )
            assert text and "{" not in text and "}" not in text, (locale, state)


class TestOpeningTheDialogProbesNoHardware:
    """probe_hardware() imports ctranslate2, asks NVML and LoadLibrary's cuBLAS
    — seconds on a machine with a card, on the wx thread, before there is a
    window for the screen reader to announce."""

    def test_the_hardware_is_not_probed_while_the_tab_is_only_being_built(
        self, wx_app, tmp_path, monkeypatch
    ):
        probes = []
        monkeypatch.setattr(
            settings_dialog.transcription_device,
            "probe_hardware",
            lambda: probes.append(True) or _CPU_ONLY,
        )
        monkeypatch.setattr(
            settings_dialog.cuda_runtime,
            "installation_state",
            lambda directory=None: cuda_runtime.RuntimeState(
                cuda_runtime.STATE_ABSENT, ()
            ),
        )
        frame = hidden_frame()
        try:
            owner = _TabOwner(_MainWindow(app_settings=_AppSettings(str(tmp_path))))
            owner._transcription_page = owner._build_transcription_page(frame)
            owner._load_transcription_values()
            assert probes == []

            owner._enter_transcription_page()
            assert probes == [True]
        finally:
            frame.Destroy()

    def test_the_models_folder_is_not_listed_while_the_tab_is_only_being_built(
        self, tab, monkeypatch
    ):
        """models_folder() walks the folder, and _load_transcription_values()
        threw the result away — the model list is measured per model by the
        populate helper against the same folder."""
        listings = []
        monkeypatch.setattr(
            settings_dialog.transcription_preferences,
            "models_folder",
            lambda stored=None: listings.append(stored) or (str(stored or ""), ()),
        )
        tab._load_transcription_values()
        assert listings == []

        tab._enter_transcription_page()
        assert len(listings) == 1

    def test_the_substitutions_are_the_same_with_or_without_a_probe(self, tab):
        """What the tab reads off resolve() is `.substitutions` and nothing
        else, and no substitution is decided against the probe — which is what
        makes handing it an empty one safe rather than merely cheap."""
        settings = {"transcription": {
            "model": "whisper-from-2019",
            "device": "quantum",
            "language": "klingon",
            "auto_detect_language": False,
        }}
        with_probe = preferences.resolve(settings, _CPU_ONLY, ("small",), "pt-BR")
        without = preferences.resolve(settings, device.HardwareProbe(), (), "pt-BR")
        assert with_probe.substitutions == without.substitutions


class TestWhatThisMachineHasToSay:
    """Two sentences that only a measurement can produce, in the same field as
    the substitutions — one warning area, one tab stop."""

    @staticmethod
    def _pick_the_graphics_card(tab):
        tab._transcription_device_radio.SetSelection(
            settings_dialog._TRANSCRIPTION_DEVICE_PREFERENCES.index(
                device.PREFERENCE_CUDA
            )
        )

    @staticmethod
    def _fallback_sentence(i18n, probe):
        """What device.py itself says about asking this machine for the card.

        Read off resolve_device() rather than named here, because which of the
        two reasons applies is device.py's own distinction: an explicit request
        on a machine with no card at all answers CUDA_UNAVAILABLE, while
        NO_CUDA_FOUND is the one "automatic" gets. Pinning the literal key
        would be this test deciding that instead.
        """
        _device_id, reason = device.resolve_device(device.PREFERENCE_CUDA, probe)
        assert reason in (device.REASON_NO_CUDA_FOUND,
                          device.REASON_CUDA_UNAVAILABLE)
        return i18n.t(device.device_reason_i18n_key(reason))

    def test_asking_for_a_card_this_computer_does_not_have_says_so(self, tab):
        """Without this the CUDA line below invites a half-gigabyte download
        that could not help, and nothing anywhere says the transcription would
        run on the processor regardless."""
        tab._load_transcription_values()
        tab._enter_transcription_page()
        self._pick_the_graphics_card(tab)
        tab._show_transcription_hardware_notices()
        assert self._fallback_sentence(tab.main_window.i18n, _CPU_ONLY) in (
            tab._transcription_substituted_field.GetValue()
        )
        assert tab._transcription_substituted_field.IsShown()

    def test_the_automatic_position_is_not_a_disappointed_expectation(self, tab):
        """The same machine, the same fallback — but nobody asked for the card,
        so there is nothing to report. resolve_device() draws the same line."""
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._transcription_device_radio.GetSelection() == 0
        assert tab._transcription_hardware_keys == []

    def test_the_radio_handler_refreshes_it_and_lets_the_event_through(self, tab):
        """Skip(), or the dialog-level EVT_RADIOBOX never fires and the Apply
        button stays hidden for the one control on this tab that is a RadioBox."""
        skipped = []

        class _Event:
            def Skip(self):
                skipped.append(True)

        tab._load_transcription_values()
        tab._enter_transcription_page()
        self._pick_the_graphics_card(tab)
        tab._on_transcription_device_change(_Event())
        assert skipped == [True]
        assert self._fallback_sentence(tab.main_window.i18n, _CPU_ONLY) in (
            tab._transcription_substituted_field.GetValue()
        )

    def test_a_machine_that_could_not_be_measured_says_which_of_the_two_it_is(
        self, tab, monkeypatch
    ):
        """"Nothing fits" and "nothing could be measured" ask for different
        things — a smaller model against a download — and an empty combobox
        showing "Automático" says neither."""
        monkeypatch.setattr(
            settings_dialog.transcription_device,
            "probe_hardware",
            device.HardwareProbe,
        )
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._transcription_substituted_field.GetValue() == tab.main_window.i18n.t(
            preferences.MODEL_NONE_I18N_KEYS[preferences.MODEL_NONE_UNMEASURED]
        )

    def test_nothing_fitting_reads_differently_from_nothing_measured(self, tab):
        tiny_machine = device.HardwareProbe(total_ram_mb=512, available_ram_mb=64)
        tab._load_transcription_values()
        tab._transcription_probe = tiny_machine
        tab._show_transcription_hardware_notices()
        assert tab._transcription_substituted_field.GetValue() == tab.main_window.i18n.t(
            preferences.MODEL_NONE_I18N_KEYS[preferences.MODEL_NONE_NOTHING_FITS]
        )

    def test_a_machine_with_room_says_nothing_at_all(self, tab):
        tab._load_transcription_values()
        tab._enter_transcription_page()
        assert tab._transcription_hardware_keys == []
        assert not tab._transcription_substituted_field.IsShown()

    def test_nothing_is_claimed_before_anything_was_measured(self, tab):
        """No probe means no measurement, not a measurement of zero."""
        tab._load_transcription_values()
        tab._show_transcription_hardware_notices()
        assert tab._transcription_hardware_keys == []

    def test_a_selection_the_radio_cannot_have_is_not_read_as_the_processor(
        self, tab
    ):
        """wx.NOT_FOUND is -1 and would index the preference tuple from the end.
        Unreachable in a RadioBox; the guard is what keeps it from being silent
        if the control is ever swapped."""
        tab._load_transcription_values()
        tab._transcription_device_radio.GetSelection = lambda: wx.NOT_FOUND
        assert tab._selected_transcription_device_preference() == device.PREFERENCE_AUTO


class TestTheInstallWideFolderReachesTheAttributeMainWindowActuallyHas:
    """The bug six green tests covered: the tab read `main_window.app_settings`
    and MainWindow only ever writes `_app_settings`, so the folder the user
    chose in Procurar was never stored and never read back — no message, no log,
    the field simply back on the default next time."""

    @staticmethod
    def _self_attributes_assigned_in(path) -> set:
        source = (REPO / path).read_text(encoding="utf-8")
        assigned = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    assigned.add(target.attr)
        return assigned

    def test_main_py_stores_it_under_the_underscored_name_and_no_other(self):
        assigned = self._self_attributes_assigned_in("client/main.py")
        assert "_app_settings" in assigned
        assert "app_settings" not in assigned, (
            "main.py grew a second spelling — the transcription tab reads "
            "_app_settings and has no fallback the way switch_behavior does"
        )

    def test_the_tab_asks_for_exactly_that_name(self):
        """Read off the accessor's own source, so renaming the attribute in
        main.py without renaming it here fails rather than silently answering
        "the default folder" forever."""
        tree = ast.parse(SETTINGS_DIALOG_SOURCE)
        accessor = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_transcription_app_settings"
        )
        names = [
            node.value for node in ast.walk(accessor)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.endswith("app_settings")
        ]
        assert names == ["_app_settings"]

    def test_no_transcription_method_reaches_for_the_bare_spelling(self):
        """The two pre-existing switch_behavior call sites keep theirs — they
        survive on a settings["general"] fallback this key does not have."""
        tree = ast.parse(SETTINGS_DIALOG_SOURCE)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and "transcription" in node.name):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and inner.value == "app_settings":
                    raise AssertionError(
                        f"{node.name} names the attribute MainWindow never sets"
                    )

    def test_the_folder_chosen_is_the_folder_read_back(self, tab, tmp_path):
        """End to end over the stub, with the attribute spelled as production
        spells it: what Procurar recorded reaches app.json and comes back."""
        tab._load_transcription_values()
        tab._transcription_models_dir = str(tmp_path / "models")
        tab._apply_transcription_values()
        assert tab._stored_transcription_models_dir() == str(tmp_path / "models")


class TestTheTabIsEnteredThroughTheNotebook:
    """Nothing else calls _enter_transcription_page(), and everything the tab
    measures, speaks and consumes is behind it."""

    def test_the_notebook_page_change_is_bound(self):
        assert "wx.EVT_NOTEBOOK_PAGE_CHANGED" in SETTINGS_DIALOG_SOURCE
        assert "self._on_settings_page_changed" in SETTINGS_DIALOG_SOURCE

    def test_the_handler_enters_the_tab_and_lets_the_event_through(self):
        tree = ast.parse(SETTINGS_DIALOG_SOURCE)
        handler = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_on_settings_page_changed"
        )
        called = {
            node.func.attr for node in ast.walk(handler)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "_enter_transcription_page" in called
        assert "Skip" in called


class TestRetranslation:
    def test_the_labels_and_both_lists_follow_a_language_change(self, tab):
        tab._load_transcription_values()
        tab._select_transcription_model("medium")
        tab.main_window.i18n = _I18n("pl")

        tab._refresh_transcription_labels()

        assert tab._transcription_model_label.GetLabel() == tab.main_window.i18n.t(
            "transcription_model_label"
        )
        assert tab._selected_transcription_model() == "medium"
        # The list is rebuilt rather than relabelled, because "the app's own
        # language" is what decides which endonym comes first.
        assert tab._transcription_language_codes[1] == "pl"
        assert tab._transcription_language_combo.GetString(1) == "polski"

    def test_the_warning_is_said_again_in_the_new_language(self, tab):
        """The keys are kept rather than the sentences, so a warning does not
        stay on screen in the language the user just left."""
        tab.main_window.settings["transcription"] = {"model": "whisper-from-2019"}
        tab._load_transcription_values()
        tab.main_window.i18n = _I18n("pl")

        tab._refresh_transcription_labels()

        assert tab._transcription_substituted_field.GetValue() == _I18n("pl").t(
            "transcription_substituted_model"
        )


class TestTheMnemonicsOnThisTab:
    """That the keys exist at all is not checked here any more: every label on
    this tab is asked for as a literal `i18n.t("...")`, which is exactly what
    tests/test_i18n_keys_exist.py scans for against all five locales, and the
    three `transcription_model_choice_*` keys — which are not literals — are
    already covered by test_no_locale_leaves_a_placeholder_to_be_read_out
    above, since a missing one renders as its own key name and no longer
    contains the model id."""

    #: Every label on the tab, plus the three buttons that are on screen
    #: whichever tab is showing. Leaving those three out is what let &Aviso sit
    #: on top of &Aplicar and, worse, &Onde on top of the dialog's own &OK.
    LABELLED = (
        "transcription_substituted_label",
        "transcription_model_label",
        "transcription_device_label",
        "transcription_language_label",
        "transcription_language_detect",
        "transcription_backend_label",
        "transcription_models_dir_label",
        "transcription_models_dir_browse_btn",
        "transcription_cuda_runtime_label",
        "ok",
        "cancel",
        "apply",
    )

    @staticmethod
    def _mnemonic(value):
        for index, char in enumerate(value):
            if char == "&" and index + 1 < len(value) and value[index + 1] != "&":
                return value[index + 1].casefold()
        return None

    @pytest.mark.parametrize("locale", LOCALES)
    def test_they_do_not_collide(self, locale):
        """Two controls sharing an Alt key means one of them can never be
        reached with it — and which one is undefined."""
        table = _load(locale)
        used = [
            self._mnemonic(table[key]) for key in self.LABELLED
            if self._mnemonic(table[key]) is not None
        ]
        assert len(used) == len(set(used)), f"{locale}: repeated mnemonics in {used}"

    @pytest.mark.parametrize("locale", LOCALES)
    def test_every_control_on_the_tab_has_one(self, locale):
        """A control with no mnemonic is one a keyboard user can only reach by
        tabbing past everything above it. transcription_language_detect had
        none in any of the five."""
        table = _load(locale)
        without = sorted(
            key for key in self.LABELLED if self._mnemonic(table[key]) is None
        )
        assert without == [], f"{locale}: no Alt key for {without}"


