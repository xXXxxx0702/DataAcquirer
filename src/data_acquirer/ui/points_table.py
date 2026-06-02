"""An editable table (ttk.Treeview) for managing the list of measure points.

Supports:
  * toggling the "enabled" flag by clicking the first column;
  * double-click in-place editing of name / measurement / note
    (measurement uses a combobox of the common types);
  * add / remove / duplicate rows;
  * bulk paste from clipboard (one ``name,measurement,note`` per line).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List

from ..config import PointSpec
from .autocomplete import AutocompleteEntry

MEASUREMENT_CHOICES = ("Float", "Double", "Bool", "Int", "String")
_COLUMNS = ("enabled", "name", "measurement", "note")
_HEADINGS = {"enabled": "启用", "name": "点位 (measurePoint)", "measurement": "类型/measurement", "note": "备注"}
_WIDTHS = {"enabled": 50, "name": 240, "measurement": 150, "note": 260}


class PointsTable(ttk.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._editor: tk.Widget | None = None
        self._catalog: dict[str, str] = {}      # point name -> measurement
        self._catalog_names: list[str] = []

        self.tree = ttk.Treeview(self, columns=_COLUMNS, show="headings", height=10)
        for col in _COLUMNS:
            self.tree.heading(col, text=_HEADINGS[col])
            self.tree.column(col, width=_WIDTHS[col], anchor="center" if col == "enabled" else "w")

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        toolbar = ttk.Frame(self)
        toolbar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Button(toolbar, text="添加", command=self.add_row).pack(side="left")
        ttk.Button(toolbar, text="复制选中", command=self.duplicate_selected).pack(side="left", padx=4)
        ttk.Button(toolbar, text="删除选中", command=self.remove_selected).pack(side="left")
        ttk.Button(toolbar, text="从剪贴板粘贴", command=self.paste_from_clipboard).pack(side="left", padx=4)
        ttk.Label(
            toolbar,
            text="（双击点位单元格可输入并联想匹配；点击“启用”列切换；粘贴格式：点位,类型,备注）",
            foreground="#666",
        ).pack(side="left", padx=8)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)

    # ------------------------------------------------------------------ #
    # Data <-> rows
    # ------------------------------------------------------------------ #
    def set_catalog(self, catalog: dict[str, str]) -> None:
        """Provide the ``{point: measurement}`` catalog for type-ahead matching."""
        self._catalog = dict(catalog)
        self._catalog_names = sorted(catalog.keys())

    @property
    def catalog_size(self) -> int:
        return len(self._catalog)

    def set_points(self, points: List[PointSpec]) -> None:
        self.tree.delete(*self.tree.get_children())
        for p in points:
            self._insert(p)

    def get_points(self) -> List[PointSpec]:
        points: List[PointSpec] = []
        for item in self.tree.get_children():
            enabled, name, measurement, note = self.tree.item(item, "values")
            points.append(
                PointSpec(
                    name=str(name).strip(),
                    measurement=str(measurement).strip() or "Float",
                    note=str(note),
                    enabled=(enabled == "✔"),
                )
            )
        return points

    def _insert(self, p: PointSpec) -> str:
        return self.tree.insert(
            "", "end",
            values=("✔" if p.enabled else "", p.name, p.measurement, p.note),
        )

    # ------------------------------------------------------------------ #
    # Toolbar actions
    # ------------------------------------------------------------------ #
    def add_row(self) -> None:
        item = self._insert(PointSpec(name="", measurement="Float"))
        self.tree.selection_set(item)
        self.tree.see(item)

    def duplicate_selected(self) -> None:
        for item in self.tree.selection():
            enabled, name, measurement, note = self.tree.item(item, "values")
            self.tree.insert("", "end", values=(enabled, name, measurement, note))

    def remove_selected(self) -> None:
        for item in self.tree.selection():
            self.tree.delete(item)

    def paste_from_clipboard(self) -> None:
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [c.strip() for c in line.replace("\t", ",").split(",")]
            name = parts[0]
            measurement = parts[1] if len(parts) > 1 and parts[1] else "Float"
            note = parts[2] if len(parts) > 2 else ""
            if name:
                self._insert(PointSpec(name=name, measurement=measurement, note=note))

    # ------------------------------------------------------------------ #
    # Inline editing
    # ------------------------------------------------------------------ #
    def _on_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        column = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if column == "#1":  # the "enabled" column -> toggle
            values = list(self.tree.item(item, "values"))
            values[0] = "" if values[0] == "✔" else "✔"
            self.tree.item(item, values=values)

    def _on_double_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        column = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item or column == "#1":  # don't text-edit the toggle column
            return
        self._open_editor(item, column)

    def _open_editor(self, item: str, column: str) -> None:
        self._close_editor()
        col_index = int(column[1:]) - 1
        x, y, w, h = self.tree.bbox(item, column)
        col_name = _COLUMNS[col_index]
        value = self.tree.item(item, "values")[col_index]

        if col_name == "name":
            # Type-ahead editor: choosing a suggestion also fills the measurement.
            editor = AutocompleteEntry(
                self.tree,
                self._catalog_names,
                measurement_of=self._catalog,
                on_commit=lambda name, meas: self._commit_name(item, name, meas),
            )
            editor.insert(0, value)
            editor.place(x=x, y=y, width=w, height=h)
            editor.focus_set()
            editor.select_range(0, "end")
            self._editor = editor
            return

        if col_name == "measurement":
            editor = ttk.Combobox(self.tree, values=MEASUREMENT_CHOICES)
            editor.set(value)
        else:
            editor = ttk.Entry(self.tree)
            editor.insert(0, value)

        editor.place(x=x, y=y, width=w, height=h)
        editor.focus_set()
        if isinstance(editor, ttk.Entry):
            editor.select_range(0, "end")

        def commit(_event=None):
            new_value = editor.get()
            values = list(self.tree.item(item, "values"))
            values[col_index] = new_value
            self.tree.item(item, values=values)
            self._close_editor()

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", lambda e: self._close_editor())
        self._editor = editor

    def _commit_name(self, item: str, name: str, measurement: str | None) -> None:
        """Write a chosen/typed point name; auto-fill measurement if known."""
        if not self.tree.exists(item):
            self._close_editor()
            return
        values = list(self.tree.item(item, "values"))
        values[1] = name
        if measurement:  # came from the catalog -> sync the type column
            values[2] = measurement
        self.tree.item(item, values=values)
        self._close_editor()

    def _close_editor(self) -> None:
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None
