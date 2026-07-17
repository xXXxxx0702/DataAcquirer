"""An editable table (ttk.Treeview) for managing the list of measure points.

Supports:
  * toggling the "enabled" flag by clicking the first column;
  * double-click in-place editing of name / measurement / note
    (measurement uses a combobox of the common types);
  * live, non-destructive filtering by point name / measurement / note;
  * enabled-point count and alternating row colours;
  * add / remove / duplicate rows;
  * bulk paste from clipboard (one ``name,measurement,note`` per line).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
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
        self._editor_commit = None
        self._editing_enabled = True
        self._action_buttons: list[ttk.Button] = []
        self._catalog: dict[str, str] = {}      # point name -> measurement
        self._catalog_names: list[str] = []
        # ``Treeview.detach`` lets filtering hide rows without deleting them.
        # Keep the canonical item order separately because detached rows are not
        # included in ``tree.get_children()``.
        self._items: list[str] = []
        self._search_var = tk.StringVar()
        self._summary_var = tk.StringVar(value="已选择 0 个点位 · 共 0 个")

        self.tree = ttk.Treeview(self, columns=_COLUMNS, show="headings", height=10)
        for col in _COLUMNS:
            self.tree.heading(col, text=_HEADINGS[col])
            self.tree.column(col, width=_WIDTHS[col], anchor="center" if col == "enabled" else "w")

        search_bar = ttk.Frame(self)
        search_bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(search_bar, text="搜索点位").pack(side="left")
        self.search_entry = ttk.Entry(
            search_bar,
            textvariable=self._search_var,
            width=28,
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(6, 4))
        ttk.Button(
            search_bar,
            text="清除",
            width=5,
            command=lambda: self._search_var.set(""),
        ).pack(side="left")
        ttk.Label(
            search_bar,
            textvariable=self._summary_var,
            foreground="#1967a3",
        ).pack(side="right", padx=(12, 0))

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        # Treeview headings are part of the widget and therefore remain fixed
        # while only its rows move under the vertical scrollbar.
        self.tree.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        hsb.grid(row=2, column=0, sticky="ew")

        toolbar = ttk.Frame(self)
        toolbar.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        actions = (
            ("添加", self.add_row, {}),
            ("复制选中", self.duplicate_selected, {"padx": 4}),
            ("删除选中", self.remove_selected, {}),
            ("清空", self.clear, {"padx": 4}),
            ("从剪贴板粘贴", self.paste_from_clipboard, {}),
        )
        for text, command, pack_options in actions:
            button = ttk.Button(toolbar, text=text, command=command)
            button.pack(side="left", **pack_options)
            self._action_buttons.append(button)
        ttk.Label(
            self,
            text="双击单元格编辑；点击“启用”切换；粘贴格式：点位,类型,备注",
            foreground="#666",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.tree.tag_configure("even", background="#ffffff")
        self.tree.tag_configure("odd", background="#f3f6f9")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self._search_var.trace_add("write", self._apply_filter)

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

    def set_points(self, points: List[PointSpec], *, notify: bool = True) -> None:
        self._close_editor()
        if self._search_var.get():
            self._search_var.set("")
        if self._items:
            self.tree.delete(*[item for item in self._items if self.tree.exists(item)])
        self._items.clear()
        for p in points:
            self._insert(p, refresh=False)
        self._apply_filter()
        if notify:
            self._notify_points_changed()

    def get_points(self) -> List[PointSpec]:
        points: List[PointSpec] = []
        for item in self._items:
            if not self.tree.exists(item):
                continue
            enabled, name, measurement, note = self.tree.item(item, "values")
            points.append(
                PointSpec(
                    name=str(name).strip(),
                    measurement=str(measurement).strip(),
                    note=str(note),
                    enabled=(enabled == "✔"),
                )
            )
        return points

    def commit_pending_edit(self) -> None:
        """Commit the active cell editor before a save or pull action."""
        commit = self._editor_commit
        if commit is not None:
            commit()

    def set_editable(self, editable: bool) -> None:
        """Enable or lock point mutations while keeping search/scroll available."""
        if not editable:
            self.commit_pending_edit()
        self._editing_enabled = editable
        for button in self._action_buttons:
            button.configure(state="normal" if editable else "disabled")

    def _insert(self, p: PointSpec, *, refresh: bool = True) -> str:
        item = self.tree.insert(
            "", "end",
            values=("✔" if p.enabled else "", p.name, p.measurement, p.note),
        )
        self._items.append(item)
        if refresh:
            self._apply_filter()
        return item

    def _apply_filter(self, *_args) -> None:
        """Show matching rows without deleting or reordering the underlying data."""
        # Typing in the search box moves focus away from an active cell.  Save
        # that edit before filtering instead of silently discarding it.
        if self._editor_commit is not None:
            self.commit_pending_edit()
            return  # the commit performs a fresh filter after closing the editor
        query = self._search_var.get().strip().casefold()
        tokens = query.split()

        # Moving every matching row to the end in canonical order also restores
        # the original order after a filter is cleared.
        for item in self._items:
            if not self.tree.exists(item):
                continue
            values = self.tree.item(item, "values")
            searchable = " ".join(str(value) for value in values[1:4]).casefold()
            if all(token in searchable for token in tokens):
                self.tree.move(item, "", "end")
            else:
                self.tree.detach(item)

        self._refresh_row_styles()
        self._refresh_summary()

    def _refresh_row_styles(self) -> None:
        """Apply zebra stripes according to the current visible row order."""
        for index, item in enumerate(self.tree.get_children()):
            self.tree.item(item, tags=("even" if index % 2 == 0 else "odd",))

    def _refresh_summary(self) -> None:
        total = 0
        enabled = 0
        for item in self._items:
            if not self.tree.exists(item):
                continue
            total += 1
            values = self.tree.item(item, "values")
            enabled += bool(values and values[0] == "✔")

        visible = len(self.tree.get_children())
        if self._search_var.get().strip():
            suffix = f"显示 {visible}/{total}"
        else:
            suffix = f"共 {total} 个"
        self._summary_var.set(f"已选择 {enabled} 个点位 · {suffix}")

    def _notify_points_changed(self) -> None:
        """Notify consumers once after a user operation changes point data."""
        self.event_generate("<<PointsChanged>>", when="tail")

    # ------------------------------------------------------------------ #
    # Toolbar actions
    # ------------------------------------------------------------------ #
    def add_row(self) -> None:
        if not self._editing_enabled:
            return
        self.commit_pending_edit()
        # A blank row cannot match an active query; clear it so the newly added
        # row is immediately visible and editable.
        if self._search_var.get():
            self._search_var.set("")
        item = self._insert(PointSpec(name="", measurement="Float"))
        self.tree.selection_set(item)
        self.tree.see(item)
        self._notify_points_changed()

    def duplicate_selected(self) -> None:
        if not self._editing_enabled:
            return
        self.commit_pending_edit()
        selected = self.tree.selection()
        for item in selected:
            enabled, name, measurement, note = self.tree.item(item, "values")
            self._insert(
                PointSpec(
                    name=str(name),
                    measurement=str(measurement),
                    note=str(note),
                    enabled=(enabled == "✔"),
                ),
                refresh=False,
            )
        self._apply_filter()
        if selected:
            self._notify_points_changed()

    def remove_selected(self) -> None:
        if not self._editing_enabled:
            return
        self.commit_pending_edit()
        selected = set(self.tree.selection())
        for item in selected:
            self.tree.delete(item)
        self._items = [item for item in self._items if item not in selected]
        self._apply_filter()
        if selected:
            self._notify_points_changed()

    def clear(self) -> None:
        if not self._editing_enabled:
            return
        self.commit_pending_edit()
        items = [item for item in self._items if self.tree.exists(item)]
        if not items:
            return
        if messagebox.askyesno("清空点位", f"确定要清空全部 {len(items)} 个点位吗？"):
            self._close_editor()
            self.tree.delete(*items)
            self._items.clear()
            self._apply_filter()
            self._notify_points_changed()

    def paste_from_clipboard(self) -> None:
        if not self._editing_enabled:
            return
        self.commit_pending_edit()
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        inserted = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [c.strip() for c in line.replace("\t", ",").split(",")]
            name = parts[0]
            measurement = parts[1] if len(parts) > 1 and parts[1] else "Float"
            note = parts[2] if len(parts) > 2 else ""
            if name:
                self._insert(
                    PointSpec(name=name, measurement=measurement, note=note),
                    refresh=False,
                )
                inserted = True
        self._apply_filter()
        if inserted:
            self._notify_points_changed()

    # ------------------------------------------------------------------ #
    # Inline editing
    # ------------------------------------------------------------------ #
    def _on_click(self, event) -> None:
        if not self._editing_enabled:
            return
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
            self._refresh_summary()
            self._notify_points_changed()

    def _on_double_click(self, event) -> None:
        if not self._editing_enabled:
            return
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
            self._editor_commit = lambda: editor._commit(editor.get())
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
            if self._editor is not editor or not self.tree.exists(item):
                return
            new_value = editor.get()
            values = list(self.tree.item(item, "values"))
            changed = str(values[col_index]) != new_value
            values[col_index] = new_value
            self.tree.item(item, values=values)
            self._close_editor()
            self._apply_filter()
            if changed:
                self._notify_points_changed()

        editor.bind("<Return>", commit)
        editor.bind("<FocusOut>", commit)
        editor.bind("<Escape>", lambda e: self._close_editor())
        self._editor = editor
        self._editor_commit = commit

    def _commit_name(self, item: str, name: str, measurement: str | None) -> None:
        """Write a chosen/typed point name; auto-fill measurement if known."""
        if not self.tree.exists(item):
            self._close_editor()
            return
        values = list(self.tree.item(item, "values"))
        changed = str(values[1]) != name
        values[1] = name
        if measurement:  # came from the catalog -> sync the type column
            changed = changed or str(values[2]) != measurement
            values[2] = measurement
        self.tree.item(item, values=values)
        self._close_editor()
        self._apply_filter()
        if changed:
            self._notify_points_changed()

    def _close_editor(self) -> None:
        editor, self._editor = self._editor, None
        self._editor_commit = None
        if editor is not None:
            editor.destroy()
