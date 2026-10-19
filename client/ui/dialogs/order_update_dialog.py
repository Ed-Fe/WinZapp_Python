"""An order the seller sent or updated: its items and what it says.

The seller's order details and each later status arrive as one message
(core/commerce.py, ORDER_UPDATE_MESSAGE_TYPE) that already carries everything
shown here, so nothing is asked of the server. Read only: the one answer
WhatsApp Web gives the buyer (confirming an order to be paid off-site by card,
WAWebBizSendOrderAction.sendOrderPaymentMethodUpdate with "confirm") is not
offered here.
"""

import wx

from core import commerce
from ui.dialogs.labelled_control import add_labelled


class OrderUpdateDialog(wx.Dialog):
    def __init__(self, parent, main_window, msg, chat_name):
        i18n = main_window.i18n
        super().__init__(parent, title=i18n.t("commerce_order_title").format(name=chat_name),
                         size=(560, 460), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        labels = commerce.order_update_item_labels(msg, i18n)
        self._items = None
        if labels:
            self._items = add_labelled(self, sizer, i18n.t("commerce_order_items"),
                                       lambda: wx.ListBox(self, choices=labels, style=wx.LB_SINGLE), 2)
            self._items.SetSelection(0)
        self._details = add_labelled(self, sizer, i18n.t("commerce_order_details"), lambda: wx.TextCtrl(
            self, value=commerce.order_update_description(msg, i18n),
            style=wx.TE_READONLY | wx.TE_MULTILINE), 1)
        close = wx.Button(self, wx.ID_CANCEL, label=i18n.t("close"))
        sizer.Add(close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)
        self.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CANCEL)
        (self._items or self._details).SetFocus()
        self.CentreOnParent()
