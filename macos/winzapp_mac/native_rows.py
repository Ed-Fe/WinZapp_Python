"""Native Mac rows for MacListCtrl: VO-Space and VoiceOver actions.

wx.ListBox on macOS is a cell-based NSTableView (wxNSTableView). Its rows
are opaque cells with only AXConfirm/AXShowMenu, so VO-Space does nothing
WinZapp can see and there is nowhere to hang VoiceOver actions.

This module makes the tables owned by MacListCtrl *view-based*: each row is
a WZRowCell (an NSTableCellView) that

  * speaks the row text as its label;
  * answers AXPress (VO-Space) by activating the row, exactly as Enter or a
    double-click does (EVT_LIST_ITEM_ACTIVATED);
  * answers AXShowMenu (VO-Shift-M) with the row's real context menu;
  * offers that same context menu's items as VoiceOver custom actions
    (VO-Command-Space), built on demand from WinZapp's own
    EVT_CONTEXT_MENU handler — so new menu items upstream appear as actions
    with no Mac-side change.

How: wx's own methods look up their "super" implementation by walking the
class chain, so giving a wx view a subclass (isa-swizzling) loops forever.
Instead two methods are added to wxNSTableView itself, and they only change
behaviour for tables registered here; every other wx list box keeps its
stock cell-based behaviour.
"""

import logging
import weakref

import objc
import wx
from AppKit import (
    NSAccessibilityCustomAction,
    NSMakeRect,
    NSTableCellView,
    NSTextField,
)

_VIEW_FOR = b"tableView:viewForTableColumn:row:"
_owners = {}  # NSTableView pointer -> weakref(MacListCtrl)
_installed = False
_collecting = []  # stack of capture lists while building menus silently


# ---------------------------------------------------------------- menus ----

def _flatten(menu, prefix=""):
    items = []
    for item in menu.GetMenuItems():
        if item.IsSeparator():
            continue
        label = item.GetItemLabelText().strip()
        if not label:
            continue
        sub = item.GetSubMenu()
        if sub is not None:
            items.extend(_flatten(sub, f"{prefix}{label}: "))
            continue
        if not item.IsEnabled():
            continue
        items.append((prefix + label, item.GetId()))
    return items


def _patched_popup(self, menu, *args, **kwargs):
    if _collecting:
        # Read the items now: WinZapp Destroy()s most menus the moment
        # PopupMenu() returns, which while capturing is immediately.
        _collecting[-1].append((self, _flatten(menu)))
        return True
    return _orig_popup(self, menu, *args, **kwargs)


_orig_popup = wx.Window.PopupMenu


_orig_bind = wx.EvtHandler.Bind
_recording = []  # stack of lists of Bind() calls made while building a menu


def _patched_bind(self, event, handler, source=None, id=wx.ID_ANY, id2=wx.ID_ANY):
    if _recording:
        _recording[-1].append((self, event, handler, source, id, id2))
    return _orig_bind(self, event, handler, source, id, id2)


def _unbind_all(binds):
    for obj, event, handler, source, id_, id2 in binds:
        try:
            obj.Unbind(event, source=source, id=id_, id2=id2, handler=handler)
        except Exception:
            pass


def _build_menu(ctrl, binds):
    """Run the control's EVT_CONTEXT_MENU handler with PopupMenu captured.
    Every Bind() it makes is appended to *binds* so the caller can undo
    them — WinZapp binds a fresh EVT_MENU handler per item each time a menu
    is built, and VoiceOver asks for a row's actions on every landing.
    Returns (window, [(label, id)]) or (None, None)."""
    captured = []
    _collecting.append(captured)
    _recording.append(binds)
    try:
        evt = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU, ctrl.GetId(), wx.DefaultPosition)
        evt.SetEventObject(ctrl)
        ctrl.GetEventHandler().ProcessEvent(evt)
    except Exception:
        logging.debug("[mac-rows] building context menu failed", exc_info=True)
    finally:
        _recording.pop()
        _collecting.pop()
    return captured[-1] if captured else (None, None)


def menu_actions(ctrl):
    binds = []
    win, items = _build_menu(ctrl, binds)
    _unbind_all(binds)
    return [label for label, _id in items or []]


def run_menu_action(ctrl, wanted):
    binds = []
    win, items = _build_menu(ctrl, binds)
    try:
        for label, item_id in items or []:
            if label == wanted:
                # Handlers are bound on the window by item id, so the menu
                # itself need not exist any more to dispatch to them.
                evt = wx.CommandEvent(wx.wxEVT_MENU, item_id)
                evt.SetEventObject(win)
                win.GetEventHandler().ProcessEvent(evt)
                return True
    finally:
        _unbind_all(binds)
    return False


# ----------------------------------------------------------------- rows ----

class WZRowCell(NSTableCellView):
    def initWithFrame_(self, frame):
        self = objc.super(WZRowCell, self).initWithFrame_(frame)
        if self is None:
            return None
        self._wz_row = -1
        self._wz_table = None
        return self

    def _ctrl(self):
        ref = _owners.get(objc.pyobjc_id(self._wz_table)) if self._wz_table is not None else None
        return ref() if ref else None

    def isAccessibilityElement(self):
        # The table's own per-cell AX element already forwards label, press,
        # show-menu and custom actions to this view; also exposing the view
        # nested a second "cell" inside every row.
        return False

    def accessibilityRole(self):
        return "AXCell"

    def accessibilityLabel(self):
        tf = self.textField()
        return tf.stringValue() if tf is not None else ""

    def _select_me(self, ctrl):
        if ctrl.GetFocusedItem() != self._wz_row:
            ctrl.Focus(self._wz_row)

    def accessibilityPerformPress(self):
        ctrl = self._ctrl()
        if ctrl is None or self._wz_row < 0:
            return False
        self._select_me(ctrl)
        wx.CallAfter(ctrl.activate_row, self._wz_row)
        return True

    def accessibilityPerformShowMenu(self):
        ctrl = self._ctrl()
        if ctrl is None or self._wz_row < 0:
            return False
        self._select_me(ctrl)
        evt = wx.ContextMenuEvent(wx.wxEVT_CONTEXT_MENU, ctrl.GetId(), wx.DefaultPosition)
        evt.SetEventObject(ctrl)
        wx.CallAfter(ctrl.GetEventHandler().ProcessEvent, evt)
        return True

    def accessibilityCustomActions(self):
        ctrl = self._ctrl()
        # Only the row the keyboard is on: WinZapp's menu handlers act on
        # the selected row, and VoiceOver's cursor follows it.
        if ctrl is None or self._wz_row < 0 or ctrl.GetFocusedItem() != self._wz_row:
            return []
        # AppKit asks twice per lookup (the table's cell element forwards
        # to this view and also rolls up its descendants' actions); handing
        # back the very same action objects lets it merge them instead of
        # listing every action twice.
        import time
        cached = getattr(self, "_wz_actions", None)
        if cached and cached[0] == self._wz_row and time.monotonic() - cached[1] < 1.0:
            return cached[2]
        try:
            labels = menu_actions(ctrl)
        except Exception:
            return []
        actions = [
            NSAccessibilityCustomAction.alloc().initWithName_target_selector_(
                label, self, b"wzPerformAction:")
            for label in labels
        ]
        self._wz_actions = (self._wz_row, time.monotonic(), actions)
        return actions

    @objc.typedSelector(b"Z@:@")
    def wzPerformAction_(self, action):
        ctrl = self._ctrl()
        if ctrl is None:
            return False
        name = str(action.name())
        wx.CallAfter(run_menu_action, ctrl, name)
        return True


def _view_for(self, table_view, column, row):
    view = table_view.makeViewWithIdentifier_owner_("wzrow", self)
    if view is None:
        view = WZRowCell.alloc().initWithFrame_(NSMakeRect(0, 0, 200, 17))
        tf = NSTextField.labelWithString_("")
        tf.setAccessibilityElement_(False)
        tf.setLineBreakMode_(4)  # NSLineBreakByTruncatingTail
        tf.setTranslatesAutoresizingMaskIntoConstraints_(True)
        tf.setAutoresizingMask_(2)  # width sizable
        tf.setFrame_(NSMakeRect(2, 0, 196, 17))
        view.addSubview_(tf)
        view.setTextField_(tf)
        view.setIdentifier_("wzrow")
    try:
        value = self.dataSource().tableView_objectValueForTableColumn_row_(table_view, column, row)
    except Exception:
        value = ""
    view.textField().setStringValue_("" if value is None else str(value))
    view._wz_row = row
    view._wz_table = table_view
    return view


def _responds(self, sel):
    if sel == _VIEW_FOR or sel == _VIEW_FOR.decode():
        return objc.pyobjc_id(self) in _owners
    return objc.super(objc.lookUpClass("wxNSTableView"), self).respondsToSelector_(sel)


def _prepared_cell(self, column, row):
    # wx still calls this cell-based API; on a view-based table AppKit logs
    # an error for it. Hand back the column's data cell primed with the
    # row's value, which is what the cell-based table would have returned.
    if objc.pyobjc_id(self) not in _owners:
        return objc.super(objc.lookUpClass("wxNSTableView"), self).preparedCellAtColumn_row_(column, row)
    try:
        col = self.tableColumns()[column]
        cell = col.dataCellForRow_(row).copy()
        cell.setObjectValue_(self.dataSource().tableView_objectValueForTableColumn_row_(self, col, row))
        return cell
    except Exception:
        return None


def _install():
    global _installed
    if _installed:
        return
    table_cls = objc.lookUpClass("wxNSTableView")
    objc.classAddMethods(table_cls, [
        objc.selector(_view_for, selector=_VIEW_FOR, signature=b"@@:@@q"),
        objc.selector(_responds, selector=b"respondsToSelector:", signature=b"Z@::"),
        objc.selector(_prepared_cell, selector=b"preparedCellAtColumn:row:", signature=b"@@:qq"),
    ])
    wx.Window.PopupMenu = _patched_popup
    wx.EvtHandler.Bind = _patched_bind
    _installed = True


def adopt(ctrl):
    """Switch a MacListCtrl's native table to WZRowCell rows."""
    try:
        _install()
        scroll = objc.objc_object(c_void_p=ctrl.GetHandle())
        table = scroll.documentView()
        if table is None or table.className() != "wxNSTableView":
            return
        _owners[objc.pyobjc_id(table)] = weakref.ref(ctrl)
        delegate = table.delegate()
        table.setDelegate_(None)
        table.setDelegate_(delegate)
        table.reloadData()
        ptr = objc.pyobjc_id(table)
        ctrl.Bind(wx.EVT_WINDOW_DESTROY,
                  lambda e, p=ptr: (_owners.pop(p, None), e.Skip()))
    except Exception:
        logging.exception("[mac-rows] could not make native rows")
