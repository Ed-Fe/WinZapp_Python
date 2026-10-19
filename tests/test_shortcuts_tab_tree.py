"""Settings > Shortcuts: a tree of categories and a search field.

Reported: every row of the list said its category before the command, so the
same word was heard again and again. The list is now a tree: the category is
said once, on its own row, and opens with the right arrow. The search field
above it filters by category, command or keys, and Down arrow goes into the
results, as in the settings' own search.
"""

from types import SimpleNamespace

import wx

from core.keyboard_shortcuts import Shortcut
from ui.dialogs.shortcuts_tab import ShortcutsTabMixin, shortcut_groups

A = Shortcut("main.a", "main", "a", "label_a", 0, 0)
B = Shortcut("main.b", "main", "b", "label_b", 0, 0)
C = Shortcut("status.c", "status", "c", "label_c", 0, 0)
D = Shortcut("status_list.d", "status_list", "d", "label_d", 0, 0)
CATEGORY = {"main": "Navigation", "status": "Status", "status_list": "Status"}
TEXT = {A: "Open chats: Alt+1", B: "Ábrir áudio: Ctrl+P", C: "Reply: Ctrl+R", D: "Delete: Delete"}


def _groups(query=""):
    return shortcut_groups([A, B, C, D], lambda s: CATEGORY[s.scope], lambda s: TEXT[s], query)


def test_each_category_once_in_the_order_it_first_appears():
    assert [(category, [s for s, _t in items]) for category, items in _groups()] == [
        ("Navigation", [A, B]), ("Status", [C, D])]


def test_the_rows_say_the_command_and_keys_without_the_category():
    [(_category, items), _] = _groups()
    assert items[0] == (A, "Open chats: Alt+1")


def test_the_search_keeps_only_matching_commands_and_their_categories():
    assert _groups("reply") == [("Status", [(C, "Reply: Ctrl+R")])]
    assert _groups("ctrl") == [("Navigation", [(B, TEXT[B])]), ("Status", [(C, TEXT[C])])]


def test_the_search_ignores_case_and_accents_and_needs_every_word():
    assert [s for _c, items in _groups("ABRIR audio") for s, _t in items] == [B]
    assert _groups("status delete") == [("Status", [(D, TEXT[D])])]
    assert _groups("reply alt") == []


class _Tree:
    """Enough of a wx.TreeCtrl for the key handlers."""

    def __init__(self, first_item, previous_ok):
        self.first_item = first_item
        self.previous_ok = previous_ok
        self.selected = None
        self.focused = False

    def GetSelection(self):
        return SimpleNamespace(IsOk=lambda: True)

    def GetPrevVisible(self, item):
        return SimpleNamespace(IsOk=lambda: self.previous_ok)

    def EnsureVisible(self, item):
        pass

    def SelectItem(self, item):
        self.selected = item

    def SetFocus(self):
        self.focused = True


class _Tab:
    for _name in ("_on_shortcut_search_key", "_on_shortcut_tree_key"):
        locals()[_name] = getattr(ShortcutsTabMixin, _name)
    del _name

    def __init__(self, first_item="first", previous_ok=True):
        self._shortcut_list = _Tree(first_item, previous_ok)
        self._shortcut_search = SimpleNamespace(SetFocus=self._search_focused)
        self.search_focused = False
        self._first = first_item

    def _first_shortcut_item(self):
        return self._first

    def _search_focused(self):
        self.search_focused = True


class _Key:
    def __init__(self, code):
        self.code = code
        self.skipped = False

    def GetKeyCode(self):
        return self.code

    def Skip(self):
        self.skipped = True


def test_down_from_the_search_goes_to_the_first_command():
    tab, key = _Tab(), _Key(wx.WXK_DOWN)
    tab._on_shortcut_search_key(key)
    assert tab._shortcut_list.selected == "first" and tab._shortcut_list.focused
    assert not key.skipped


def test_down_with_no_result_is_left_to_the_field():
    tab, key = _Tab(first_item=None), _Key(wx.WXK_DOWN)
    tab._on_shortcut_search_key(key)
    assert key.skipped and not tab._shortcut_list.focused


def test_up_from_the_first_row_goes_back_to_the_search():
    tab, key = _Tab(previous_ok=False), _Key(wx.WXK_UP)
    tab._on_shortcut_tree_key(key)
    assert tab.search_focused and not key.skipped


def test_up_from_another_row_moves_in_the_tree():
    tab, key = _Tab(previous_ok=True), _Key(wx.WXK_UP)
    tab._on_shortcut_tree_key(key)
    assert key.skipped and not tab.search_focused
