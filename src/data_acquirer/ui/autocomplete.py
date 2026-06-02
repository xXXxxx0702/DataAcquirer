"""An Entry with type-ahead autocompletion backed by a popup listbox.

Used as the in-cell editor for the point-name column: the user types part of a
point name and a dropdown shows matching points discovered from the InfluxDB
catalog. Choosing a suggestion fires ``on_commit(name, measurement)`` so the
caller can also auto-fill the measurement/type.

The popup is a borderless ``Toplevel`` positioned under the entry, so it works
even though the entry is itself an overlay placed on top of a Treeview cell.
Selection is dispatched via ``after_idle`` so widgets are never destroyed from
inside their own event handler.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Mapping, Optional, Sequence


class AutocompleteEntry(ttk.Entry):
    def __init__(
        self,
        master,
        completions: Sequence[str] = (),
        *,
        measurement_of: Optional[Mapping[str, str]] = None,
        on_commit: Optional[Callable[[str, Optional[str]], None]] = None,
        max_results: int = 12,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self._all = list(completions)
        self._measurement_of = dict(measurement_of or {})
        self._on_commit = on_commit
        self._max = max_results

        self._popup: Optional[tk.Toplevel] = None
        self._listbox: Optional[tk.Listbox] = None
        self._committed = False

        self.bind("<KeyRelease>", self._on_keyrelease)
        self.bind("<FocusOut>", self._on_focusout)
        self.bind("<Return>", self._on_return)
        self.bind("<Escape>", self._on_escape)
        self.bind("<Down>", self._focus_listbox)

    # ------------------------------------------------------------------ #
    # Matching
    # ------------------------------------------------------------------ #
    def _matches(self, text: str) -> list[str]:
        text = text.strip().lower()
        if not text:
            return self._all[: self._max]
        starts = [c for c in self._all if c.lower().startswith(text)]
        seen = set(starts)
        contains = [c for c in self._all if text in c.lower() and c not in seen]
        return (starts + contains)[: self._max]

    # ------------------------------------------------------------------ #
    # Popup management
    # ------------------------------------------------------------------ #
    def _show_popup(self, items: list[str]) -> None:
        if not items:
            self._hide_popup()
            return
        if self._popup is None:
            self._popup = tk.Toplevel(self)
            self._popup.wm_overrideredirect(True)
            self._listbox = tk.Listbox(self._popup, activestyle="dotbox", exportselection=False)
            self._listbox.pack(fill="both", expand=True)
            self._listbox.bind("<ButtonRelease-1>", lambda e: self.after_idle(self._choose_active))
            self._listbox.bind("<Return>", lambda e: self.after_idle(self._choose_active))
            self._listbox.bind("<Escape>", self._on_escape)

        self._listbox.delete(0, "end")
        for it in items:
            self._listbox.insert("end", it)
        self._listbox.configure(height=min(len(items), 8))

        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        self._popup.wm_geometry(f"{max(self.winfo_width(), 200)}x{self._listbox.winfo_reqheight()}+{x}+{y}")
        self._popup.deiconify()
        self._popup.lift()

    def _hide_popup(self) -> None:
        if self._popup is not None:
            self._popup.withdraw()

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    _NAV_KEYS = {"Up", "Down", "Return", "Escape", "Tab"}

    def _on_keyrelease(self, event) -> None:
        if event.keysym in self._NAV_KEYS:
            return
        self._show_popup(self._matches(self.get()))

    def _focus_listbox(self, event):
        if self._listbox is not None and self._popup is not None and self._listbox.size():
            self._listbox.focus_set()
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(0)
            self._listbox.activate(0)
        return "break"

    def _choose_active(self) -> None:
        if self._listbox is None:
            return
        sel = self._listbox.curselection()
        index = sel[0] if sel else (self._listbox.index("active") if self._listbox.size() else None)
        if index is None:
            return
        value = self._listbox.get(index)
        self.delete(0, "end")
        self.insert(0, value)
        self._commit(value)

    def _on_return(self, event):
        if self._popup is not None and self._popup.winfo_viewable() and self._listbox and self._listbox.size():
            self._choose_active()
        else:
            self._commit(self.get())
        return "break"

    def _on_escape(self, event):
        # Cancel the suggestion popup but keep editing; if already hidden, commit.
        if self._popup is not None and self._popup.winfo_viewable():
            self._hide_popup()
            self.focus_set()
            return "break"
        self._commit(self.get())
        return "break"

    def _on_focusout(self, event):
        # Defer: a click on the popup listbox should win over the focus-out commit.
        self.after(150, self._focusout_commit)

    def _focusout_commit(self) -> None:
        if self._committed:
            return
        # If focus moved into our own popup listbox, don't commit yet.
        if self._listbox is not None and self.focus_get() is self._listbox:
            return
        self._commit(self.get())

    # ------------------------------------------------------------------ #
    def _commit(self, value: str) -> None:
        if self._committed:
            return
        self._committed = True
        self._hide_popup()
        measurement = self._measurement_of.get(value)
        if self._on_commit is not None:
            self._on_commit(value, measurement)

    # ------------------------------------------------------------------ #
    def destroy(self) -> None:  # also tear down the popup Toplevel
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None
            self._listbox = None
        super().destroy()
