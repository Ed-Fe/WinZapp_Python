"""A MacListCtrl write changes one native row instead of wx reloading the
whole table (native_rows.change / reload_plan). No window, no wx.App."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path[:0] = [os.path.join(ROOT, "macos"), os.path.join(ROOT, "client")]

from winzapp_mac import native_rows as nr  # noqa: E402


class _Ctrl:
    _wz_table_ptr = 1234


def plan(kind, row, table_rows, data_rows, done=False, owned=True, columns=1):
    return nr.reload_plan([kind, row, done], owned, columns, table_rows, data_rows)


def test_a_text_change_sets_that_row_only():
    assert plan(nr.ROW, 7, 20, 20) == (nr.ROW, 7)


def test_an_insert_adds_one_row_when_the_data_has_exactly_one_more():
    assert plan(nr.INSERT, 0, 20, 21) == (nr.INSERT, 0)
    assert plan(nr.INSERT, 20, 20, 21) == (nr.INSERT, 20)      # appended at the end


def test_a_delete_removes_one_row_when_the_data_has_exactly_one_fewer():
    assert plan(nr.DELETE, 19, 20, 19) == (nr.DELETE, 19)


def test_the_reloads_after_the_change_in_the_same_write_do_nothing():
    # Insert's UpdateLineToEnd reloads again right after ListInsert.
    assert plan(nr.INSERT, 3, 21, 21, done=True) == ("none",)


def test_anything_that_does_not_line_up_is_a_full_reload():
    assert plan(nr.INSERT, 0, 20, 22) == ("full",)             # two rows appeared
    assert plan(nr.DELETE, 0, 20, 20) == ("full",)             # nothing was removed
    assert plan(nr.ROW, 7, 20, 21) == ("full",)                # count changed under a text write
    assert plan(nr.ROW, 25, 20, 20) == ("full",)               # row out of range
    assert plan(nr.INSERT, 3, 20, 22, done=True) == ("full",)  # count moved again after the change
    assert plan(nr.ROW, 7, 20, 20, owned=False) == ("full",)   # not a MacListCtrl table
    assert plan(nr.ROW, 7, 20, 20, columns=2) == ("full",)
    assert nr.reload_plan(None, True, 1, 20, 20) == ("full",)  # no write in progress: Clear(), Set()


def test_change_marks_the_table_only_inside_the_block_even_when_it_raises():
    with nr.change(_Ctrl(), nr.INSERT, 3) as c:
        assert nr._pending == {1234: c.entry} and c.entry == [nr.INSERT, 3, False]
    assert nr._pending == {}
    try:
        with nr.row_only(_Ctrl(), 3):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert nr._pending == {}


def test_a_control_that_was_never_adopted_marks_nothing():
    with nr.change(object(), nr.DELETE, 3):
        assert nr._pending == {}
