"""Main application window."""

from __future__ import annotations

import calendar
import datetime
import os
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from ..bookmarks import BookmarkStore, ServerBookmark
from ..config import AcquireConfig
from ..paths import BOOKMARKS_PATH, LAST_SESSION_PATH, PRESETS_DIR
from .points_table import PointsTable
from .worker import (
    CancelledMsg,
    CatalogMsg,
    CatalogWorker,
    ConnectionTestWorker,
    DoneMsg,
    ErrorMsg,
    LogMsg,
    ProgressMsg,
    PullWorker,
)

_POLL_MS = 100

# Fields that define the connection; changing any of them triggers a re-test.
CONNECTION_KEYS = ("host", "port", "username", "password", "database")

# Time quick-range buttons: (label, unit, amount). unit is "days" or "months".
RECENT_RANGES = (
    ("近1天", "days", 1),
    ("近2天", "days", 2),
    ("近7天", "days", 7),
    ("近14天", "days", 14),
    ("近30天", "days", 30),
    ("近3月", "months", 3),
    ("近6月", "months", 6),
    ("近1年", "months", 12),
)
_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_CUSTOM_HOURS_DEFAULT = 24
_CUSTOM_HOURS_MAX = 24 * 365
_NUMBER_SPINBOX_STYLE = "Comfort.TSpinbox"


def _subtract_months(dt: datetime.datetime, months: int) -> datetime.datetime:
    """Return dt shifted back by N calendar months, clamping the day to the
    target month's last valid day (e.g. Mar 31 - 1 month -> Feb 28/29)."""
    month_index = dt.month - 1 - months
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.replace(year=year, month=month, day=min(dt.day, last_day))


class DateTimePicker(ttk.Frame):
    """Compact date/time selector backed by a formatted ``StringVar``.

    Each component remains directly editable while also supporting the native
    spinbox arrows and mouse wheel.  The public value stays compatible with
    the existing configuration format (``YYYY-MM-DD HH:MM:SS``).
    """

    def __init__(self, parent, variable: tk.StringVar) -> None:
        super().__init__(parent)
        self.variable = variable
        self._syncing = False
        self._parts = {
            "year": tk.StringVar(),
            "month": tk.StringVar(),
            "day": tk.StringVar(),
            "hour": tk.StringVar(),
            "minute": tk.StringVar(),
            "second": tk.StringVar(),
        }
        self._spinboxes: dict[str, ttk.Spinbox] = {}

        ttk.Label(self, text="日期").pack(side="left", padx=(0, 4))
        self._add_part("year", 1, 9999, 7)
        ttk.Label(self, text="-").pack(side="left")
        self._add_part("month", 1, 12, 5)
        ttk.Label(self, text="-").pack(side="left")
        self._add_part("day", 1, 31, 5)

        ttk.Label(self, text="时间").pack(side="left", padx=(12, 4))
        self._add_part("hour", 0, 23, 5)
        ttk.Label(self, text=":").pack(side="left")
        self._add_part("minute", 0, 59, 5)
        ttk.Label(self, text=":").pack(side="left")
        self._add_part("second", 0, 59, 5)

        self.variable.trace_add("write", self._sync_from_value)
        if not self.variable.get():
            self.variable.set(datetime.datetime.now().strftime(_TIME_FMT))
        else:
            self._sync_from_value()

    def _add_part(self, name: str, minimum: int, maximum: int, width: int) -> None:
        spinbox = ttk.Spinbox(
            self,
            from_=minimum,
            to=maximum,
            width=width,
            textvariable=self._parts[name],
            justify="center",
            wrap=True,
            command=self._sync_to_value,
            style=_NUMBER_SPINBOX_STYLE,
        )
        spinbox.pack(side="left")
        self._spinboxes[name] = spinbox
        spinbox.bind("<FocusOut>", self._sync_to_value)
        spinbox.bind("<Return>", self._sync_to_value)
        spinbox.bind(
            "<MouseWheel>",
            lambda event, n=name, lo=minimum, hi=maximum: self._on_mousewheel(
                event, n, lo, hi
            ),
        )

    def _sync_from_value(self, *_args) -> None:
        if self._syncing:
            return
        try:
            value = datetime.datetime.strptime(self.variable.get(), _TIME_FMT)
        except (TypeError, ValueError):
            return

        self._syncing = True
        try:
            values = {
                "year": f"{value.year:04d}",
                "month": f"{value.month:02d}",
                "day": f"{value.day:02d}",
                "hour": f"{value.hour:02d}",
                "minute": f"{value.minute:02d}",
                "second": f"{value.second:02d}",
            }
            for name, text in values.items():
                self._parts[name].set(text)
        finally:
            self._syncing = False

    def _sync_to_value(self, _event=None) -> None:
        if self._syncing:
            return
        try:
            year = min(9999, max(1, int(self._parts["year"].get())))
            month = min(12, max(1, int(self._parts["month"].get())))
            max_day = calendar.monthrange(year, month)[1]
            day = min(max_day, max(1, int(self._parts["day"].get())))
            hour = min(23, max(0, int(self._parts["hour"].get())))
            minute = min(59, max(0, int(self._parts["minute"].get())))
            second = min(59, max(0, int(self._parts["second"].get())))
        except (TypeError, ValueError):
            self._sync_from_value()
            return

        value = datetime.datetime(year, month, day, hour, minute, second)
        self.variable.set(value.strftime(_TIME_FMT))
        self._sync_from_value()

    def _on_mousewheel(
        self, event, name: str, minimum: int, maximum: int
    ) -> str:
        try:
            current = int(self._parts[name].get())
        except (TypeError, ValueError):
            current = minimum
        step = 1 if event.delta > 0 else -1
        if current + step > maximum:
            current = minimum
        elif current + step < minimum:
            current = maximum
        else:
            current += step
        self._parts[name].set(str(current))
        self._sync_to_value()
        return "break"


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.pack(fill="both", expand=True)

        self._pull_worker: PullWorker | None = None
        self._conn_worker: ConnectionTestWorker | None = None
        self._catalog_worker: CatalogWorker | None = None

        self.bookmarks = BookmarkStore(BOOKMARKS_PATH)
        self._conn_silent = False  # current test suppresses popups?
        self._catalog_silent = False  # current catalog load suppresses popups?
        self._last_conn_signature: tuple | None = None  # last connection we tested
        self._retest_after_id: str | None = None  # debounce handle
        self._conn_pending: dict | None = None  # test requested while one is running
        self._catalog_pending: bool | None = None  # reload requested while loading
        self._catalog_conn_sig: tuple | None = None  # connection the load belongs to

        self._startup_note = ""
        self._build_widgets()
        self._load_into_widgets(self._load_startup_config())
        if self._startup_note:
            self._append_log(self._startup_note)

        # Persist the session on close, and auto-test the (restored) connection.
        master.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, lambda: self._start_conn_test(silent=True, force=True))

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        self.vars: dict[str, tk.Variable] = {}
        self.entries: dict[str, ttk.Entry] = {}
        # The Vista spinbox arrows consume part of the text area.  Explicit
        # inner padding keeps digits clear of the arrows at Windows DPI scales.
        ttk.Style(self).configure(_NUMBER_SPINBOX_STYLE, padding=(4, 1, 4, 1))

        # --- Connection ---
        conn = ttk.LabelFrame(self, text="连接配置", padding=8)
        conn.pack(fill="x")
        self._add_entry(conn, "host", "地址 (host)", 0, 0, width=18)
        self._add_entry(conn, "port", "端口 (port)", 0, 2, width=8)
        self._add_entry(conn, "database", "数据库 (database)", 0, 4, width=24)
        self._add_entry(conn, "username", "用户名", 1, 0, width=18)
        self._add_entry(conn, "password", "密码", 1, 2, width=18, show="*")
        ttk.Button(conn, text="测试连接", command=self.on_test_connection).grid(
            row=1, column=4, sticky="e", padx=4, pady=2
        )
        self.conn_status = ttk.Label(conn, text="●  未连接", foreground="#999")
        self.conn_status.grid(row=1, column=5, sticky="w", padx=4)

        # Server bookmarks row.
        ttk.Label(conn, text="服务器书签").grid(
            row=2, column=0, sticky="e", padx=(4, 2), pady=(6, 2)
        )
        self.bookmark_var = tk.StringVar()
        self.bookmark_combo = ttk.Combobox(
            conn, textvariable=self.bookmark_var, state="readonly", width=28
        )
        self.bookmark_combo.grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 2))
        self.bookmark_combo.bind("<<ComboboxSelected>>", self._on_bookmark_selected)
        ttk.Button(conn, text="保存为书签…", command=self.on_save_bookmark).grid(
            row=2, column=3, sticky="w", padx=4, pady=(6, 2)
        )
        ttk.Button(conn, text="删除书签", command=self.on_delete_bookmark).grid(
            row=2, column=4, sticky="w", pady=(6, 2)
        )
        self._refresh_bookmarks()

        # Re-test the connection automatically when a connection field changes.
        for key in CONNECTION_KEYS:
            self.entries[key].bind("<FocusOut>", self._on_conn_field_changed)
            self.entries[key].bind("<Return>", self._on_conn_field_changed)

        # --- Time window & behaviour ---
        tf = ttk.LabelFrame(self, text="时间范围与行为", padding=8)
        tf.pack(fill="x", pady=(8, 0))
        self.time_pickers: dict[str, DateTimePicker] = {}
        for row, (key, label) in enumerate(
            (("start_time", "起始时间"), ("end_time", "终止时间"))
        ):
            ttk.Label(tf, text=label).grid(
                row=row, column=0, sticky="e", padx=(4, 8), pady=3
            )
            var = tk.StringVar()
            self.vars[key] = var
            picker = DateTimePicker(tf, var)
            picker.grid(row=row, column=1, columnspan=7, sticky="w", pady=3)
            self.time_pickers[key] = picker

        self._add_entry(tf, "chunk_hours", "分段(小时)", 2, 0, width=8)
        self._add_entry(tf, "utc_offset_hours", "UTC偏移(小时)", 2, 2, width=8)
        self._add_entry(tf, "value_field", "取值字段", 2, 4, width=12)
        self._add_entry(tf, "measure_tag", "点位标签", 2, 6, width=14)

        quick = ttk.Frame(tf)
        quick.grid(row=3, column=0, columnspan=8, sticky="w", pady=(7, 0))
        ttk.Label(quick, text="快捷范围：").grid(
            row=0, column=0, rowspan=2, sticky="ne", pady=3
        )

        quick_hours_days = ttk.Frame(quick)
        quick_hours_days.grid(row=0, column=1, sticky="w")
        self.recent_hours_var = tk.StringVar(value=str(_CUSTOM_HOURS_DEFAULT))
        ttk.Label(quick_hours_days, text="近").pack(side="left", padx=(2, 0))
        self.recent_hours_spinbox = ttk.Spinbox(
            quick_hours_days,
            from_=1,
            to=_CUSTOM_HOURS_MAX,
            width=7,
            textvariable=self.recent_hours_var,
            justify="center",
            wrap=True,
            style=_NUMBER_SPINBOX_STYLE,
        )
        self.recent_hours_spinbox.pack(side="left")
        self.recent_hours_spinbox.bind("<MouseWheel>", self._on_recent_hours_wheel)
        self.recent_hours_spinbox.bind("<Return>", self._apply_recent_hours)
        ttk.Label(quick_hours_days, text="小时").pack(side="left", padx=(2, 4))
        ttk.Button(
            quick_hours_days,
            text="应用",
            width=5,
            command=self._apply_recent_hours,
        ).pack(side="left", padx=(0, 6))

        for label, unit, amount in RECENT_RANGES[:5]:
            ttk.Button(
                quick_hours_days,
                text=label,
                width=7,
                command=lambda u=unit, n=amount: self._set_recent_range(u, n),
            ).pack(side="left", padx=2)

        quick_calendar = ttk.Frame(quick)
        quick_calendar.grid(row=1, column=1, sticky="w", pady=(3, 0))
        for label, unit, amount in RECENT_RANGES[5:]:
            ttk.Button(
                quick_calendar,
                text=label,
                width=7,
                command=lambda u=unit, n=amount: self._set_recent_range(u, n),
            ).pack(side="left", padx=2)
        ttk.Label(
            quick_calendar,
            text="（结束=当前时间，开始=往前推对应时长）",
            foreground="#666",
        ).pack(side="left", padx=6)

        # --- Points ---
        pf = ttk.LabelFrame(self, text="点位列表", padding=8)
        pf.pack(fill="both", expand=True, pady=(8, 0))

        catbar = ttk.Frame(pf)
        catbar.pack(fill="x", pady=(0, 6))
        self.catalog_btn = ttk.Button(
            catbar, text="刷新点位目录", command=self.on_load_catalog
        )
        self.catalog_btn.pack(side="left")
        self.catalog_label = ttk.Label(
            catbar, text="连接成功后将自动加载点位目录以启用联想", foreground="#666"
        )
        self.catalog_label.pack(side="left", padx=8)

        self.points_table = PointsTable(pf)
        self.points_table.pack(fill="both", expand=True)

        # --- Output ---
        of = ttk.LabelFrame(self, text="输出", padding=8)
        of.pack(fill="x", pady=(8, 0))
        self._add_entry(of, "output_path", "CSV 文件", 0, 0, width=52)
        ttk.Button(of, text="浏览…", command=self.on_browse_output).grid(
            row=0, column=2, padx=4
        )

        # --- Actions ---
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(8, 0))
        self.start_btn = ttk.Button(actions, text="开始拉取", command=self.on_start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            actions, text="取消", command=self.on_cancel, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(actions, text="保存配置…", command=self.on_save_config).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="载入配置…", command=self.on_load_config).pack(
            side="left"
        )

        self.progress = ttk.Progressbar(actions, mode="determinate", length=240)
        self.progress.pack(side="right")
        self.progress_label = ttk.Label(actions, text="")
        self.progress_label.pack(side="right", padx=8)

        # --- Log ---
        lf = ttk.LabelFrame(self, text="运行日志", padding=8)
        lf.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(lf, height=10, wrap="word", state="disabled")
        log_sb = ttk.Scrollbar(lf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")

    def _add_entry(
        self, parent, key, label, row, col, width=20, show=None
    ) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(
            row=row, column=col, sticky="e", padx=(4, 2), pady=2
        )
        var = tk.StringVar()
        self.vars[key] = var
        entry = ttk.Entry(parent, textvariable=var, width=width, show=show)
        entry.grid(row=row, column=col + 1, sticky="w", padx=(0, 8), pady=2)
        self.entries[key] = entry
        return entry

    # ------------------------------------------------------------------ #
    # Config <-> widgets
    # ------------------------------------------------------------------ #
    def _default_config(self) -> AcquireConfig:
        cfg = AcquireConfig()
        # A couple of sample points so the table isn't empty on first launch.
        from ..config import PointSpec

        cfg.points = [
            PointSpec("B5_ZQWD", "Float", "主汽温度"),
            PointSpec("B5_YJJW_AUTO", "Bool", "一级减温水自动投入标志"),
        ]
        return cfg

    def _load_startup_config(self) -> AcquireConfig:
        """Restore the config saved at last shutdown; fall back to defaults."""
        if LAST_SESSION_PATH.exists():
            try:
                cfg = AcquireConfig.load(LAST_SESSION_PATH)
                self._startup_note = f"已恢复上次会话配置: {LAST_SESSION_PATH}"
                return cfg
            except Exception as exc:
                self._startup_note = f"无法读取上次会话配置 ({exc})，使用默认配置"
        else:
            self._startup_note = "首次启动，使用默认配置"
        return self._default_config()

    def _on_close(self) -> None:
        """Persist the current config so the next launch restores it."""
        if self._pull_worker and self._pull_worker.running:
            if not messagebox.askyesno(
                "退出确认",
                "数据拉取仍在进行中，此时退出会中断拉取，"
                "输出文件可能不完整。\n\n确定要退出吗？",
            ):
                return
        try:
            self._config_from_widgets().save(LAST_SESSION_PATH)
        except Exception:
            pass  # never block shutdown on a save failure
        self.master.destroy()

    def _load_into_widgets(self, cfg: AcquireConfig) -> None:
        for key, var in self.vars.items():
            var.set(str(getattr(cfg, key)))
        self.points_table.set_points(cfg.points)

    def _config_from_widgets(self) -> AcquireConfig:
        def as_int(key, default):
            try:
                return int(str(self.vars[key].get()).strip())
            except ValueError:
                return default

        return AcquireConfig(
            host=self.vars["host"].get().strip(),
            port=as_int("port", 8086),
            username=self.vars["username"].get(),
            password=self.vars["password"].get(),
            database=self.vars["database"].get().strip(),
            start_time=self.vars["start_time"].get().strip(),
            end_time=self.vars["end_time"].get().strip(),
            chunk_hours=as_int("chunk_hours", 24),
            utc_offset_hours=as_int("utc_offset_hours", 8),
            value_field=self.vars["value_field"].get().strip() or "value",
            measure_tag=self.vars["measure_tag"].get().strip() or "measurePoint",
            points=self.points_table.get_points(),
            output_path=self.vars["output_path"].get().strip(),
        )

    # ------------------------------------------------------------------ #
    # Logging helpers
    # ------------------------------------------------------------------ #
    def _append_log(self, text: str) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{ts}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Button handlers
    # ------------------------------------------------------------------ #
    def _recent_hours(self) -> int | None:
        try:
            hours = int(self.recent_hours_var.get())
        except (TypeError, ValueError):
            self.master.bell()
            return None
        hours = min(_CUSTOM_HOURS_MAX, max(1, hours))
        self.recent_hours_var.set(str(hours))
        return hours

    def _on_recent_hours_wheel(self, event) -> str:
        hours = self._recent_hours() or _CUSTOM_HOURS_DEFAULT
        step = 1 if event.delta > 0 else -1
        hours = min(_CUSTOM_HOURS_MAX, max(1, hours + step))
        self.recent_hours_var.set(str(hours))
        return "break"

    def _apply_recent_hours(self, _event=None) -> None:
        hours = self._recent_hours()
        if hours is not None:
            self._set_recent_range("hours", hours)

    def _set_recent_range(self, unit: str, amount: int) -> None:
        now = datetime.datetime.now()
        if unit == "months":
            start = _subtract_months(now, amount)
            desc = f"近 {amount} 个月" if amount < 12 else f"近 {amount // 12} 年"
        elif unit == "hours":
            start = now - datetime.timedelta(hours=amount)
            desc = f"近 {amount} 小时"
        else:
            start = now - datetime.timedelta(days=amount)
            desc = f"近 {amount} 天"
        self.vars["end_time"].set(now.strftime(_TIME_FMT))
        self.vars["start_time"].set(start.strftime(_TIME_FMT))
        self._append_log(
            f"时间范围已设为{desc}: {self.vars['start_time'].get()} ~ {self.vars['end_time'].get()}"
        )

    # ------------------------------------------------------------------ #
    # Server bookmarks
    # ------------------------------------------------------------------ #
    def _refresh_bookmarks(self, select: str | None = None) -> None:
        names = self.bookmarks.names()
        self.bookmark_combo.configure(values=names)
        if select and select in names:
            self.bookmark_var.set(select)
        elif self.bookmark_var.get() not in names:
            self.bookmark_var.set("")

    def _on_bookmark_selected(self, _event=None) -> None:
        bm = self.bookmarks.get(self.bookmark_var.get())
        if bm is None:
            return
        self.vars["host"].set(bm.host)
        self.vars["port"].set(str(bm.port))
        self.vars["username"].set(bm.username)
        self.vars["password"].set(bm.password)
        self.vars["database"].set(bm.database)
        self._append_log(f"已切换到服务器书签: {bm.name}")
        self._start_conn_test(silent=True, force=True)

    def on_save_bookmark(self) -> None:
        default = (
            self.bookmark_var.get()
            or f"{self.vars['host'].get()}:{self.vars['port'].get()}"
        )
        name = simpledialog.askstring(
            "保存为书签", "书签名称：", initialvalue=default, parent=self
        )
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if self.bookmarks.get(name) and not messagebox.askyesno(
            "覆盖书签", f"书签 “{name}” 已存在，是否覆盖？"
        ):
            return

        def as_int(key, default):
            try:
                return int(str(self.vars[key].get()).strip())
            except ValueError:
                return default

        self.bookmarks.upsert(
            ServerBookmark(
                name=name,
                host=self.vars["host"].get().strip(),
                port=as_int("port", 8086),
                username=self.vars["username"].get(),
                password=self.vars["password"].get(),
                database=self.vars["database"].get().strip(),
            )
        )
        self._refresh_bookmarks(select=name)
        self._append_log(f"已保存服务器书签: {name}")

    def on_delete_bookmark(self) -> None:
        name = self.bookmark_var.get()
        if not name:
            return
        if messagebox.askyesno("删除书签", f"确定删除书签 “{name}” 吗？"):
            self.bookmarks.remove(name)
            self._refresh_bookmarks()
            self._append_log(f"已删除服务器书签: {name}")

    def on_browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择输出 CSV 文件",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=Path(self.vars["output_path"].get()).name or "data.csv",
        )
        if path:
            self.vars["output_path"].set(path)

    def _reveal_in_explorer(self, output_path: str) -> None:
        """Open the output folder, selecting the exported file when possible."""
        try:
            target = Path(output_path).resolve()
            folder = target.parent if target.parent.exists() else target
            if sys.platform.startswith("win"):
                if target.exists():
                    subprocess.run(["explorer", "/select,", str(target)])
                else:
                    os.startfile(str(folder))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)])
            else:
                subprocess.run(["xdg-open", str(folder)])
        except Exception as exc:  # opening the folder is best-effort
            self._append_log(f"打开输出文件夹失败: {exc}")

    def _connection_signature(self) -> tuple:
        return tuple(str(self.vars[k].get()).strip() for k in CONNECTION_KEYS)

    def on_test_connection(self) -> None:
        # Manual button: always test, and show popups on failure.
        self._start_conn_test(silent=False, force=True)

    def _on_conn_field_changed(self, _event=None) -> None:
        # Debounce: a focus-out followed quickly by another shouldn't fire twice.
        if self._retest_after_id is not None:
            self.after_cancel(self._retest_after_id)
        self._retest_after_id = self.after(400, self._auto_retest)

    def _auto_retest(self) -> None:
        self._retest_after_id = None
        if self._connection_signature() == self._last_conn_signature:
            return  # connection unchanged since last test
        self._start_conn_test(silent=True, force=True)

    def _start_conn_test(self, *, silent: bool, force: bool = False) -> None:
        signature = self._connection_signature()
        if signature != self._last_conn_signature:
            # Server changed: the old autocomplete catalog no longer applies.
            self._clear_catalog()
        if self._conn_worker and self._conn_worker.running:
            # Busy testing: remember the request and run it once finished.
            self._conn_pending = {"silent": silent, "force": force}
            return
        if not force and signature == self._last_conn_signature:
            return
        self._last_conn_signature = signature
        self._conn_silent = silent
        cfg = self._config_from_widgets()
        self.conn_status.configure(text="●  连接中…", foreground="#c08000")
        self._append_log(f"正在测试连接 {cfg.host}:{cfg.port} / {cfg.database} …")
        self._conn_worker = ConnectionTestWorker(cfg)
        self._conn_worker.start()
        self.after(_POLL_MS, self._poll_conn)

    def _clear_catalog(self) -> None:
        """Drop the autocomplete catalog (it belongs to the previous server)."""
        if self.points_table.catalog_size:
            self.points_table.set_catalog({})
            self._append_log("连接配置已更改，已清空旧的点位目录")
        self.catalog_label.configure(
            text="连接成功后将自动加载点位目录以启用联想"
        )

    def on_load_catalog(self, *, silent: bool = False) -> None:
        if self._catalog_worker and self._catalog_worker.running:
            # Busy loading: remember the request and rerun it once finished.
            self._catalog_pending = silent
            return
        cfg = self._config_from_widgets()
        self._catalog_silent = silent
        self._catalog_conn_sig = self._connection_signature()
        self.catalog_btn.configure(state="disabled")
        self.catalog_label.configure(text="正在加载点位目录…")
        self._append_log(f"正在从 {cfg.host}:{cfg.port}/{cfg.database} 加载点位目录…")
        self._catalog_worker = CatalogWorker(cfg)
        self._catalog_worker.start()
        self.after(_POLL_MS, self._poll_catalog)

    def on_start(self) -> None:
        if self._pull_worker and self._pull_worker.running:
            return
        cfg = self._config_from_widgets()
        errors = cfg.validate()
        if errors:
            messagebox.showerror("配置无效", "\n".join(errors))
            return

        self.progress.configure(value=0, maximum=100)
        self.progress_label.configure(text="")
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")

        self._pull_worker = PullWorker(cfg)
        self._pull_worker.start()
        self.after(_POLL_MS, self._poll_pull)

    def on_cancel(self) -> None:
        if self._pull_worker and self._pull_worker.running:
            self._pull_worker.cancel()
            self._append_log("已请求取消，正在停止…")

    def on_save_config(self) -> None:
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="保存配置",
            defaultextension=".json",
            initialdir=str(PRESETS_DIR),
            filetypes=[("JSON 配置", "*.json")],
        )
        if not path:
            return
        try:
            self._config_from_widgets().save(path)
            self._append_log(f"配置已保存: {path}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def on_load_config(self) -> None:
        path = filedialog.askopenfilename(
            title="载入配置",
            initialdir=str(PRESETS_DIR) if PRESETS_DIR.exists() else ".",
            filetypes=[("JSON 配置", "*.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            cfg = AcquireConfig.load(path)
            self._load_into_widgets(cfg)
            self._append_log(f"配置已载入: {path}")
            # 载入后自动测试连接（成功则自动刷新点位目录、启用联想）
            self._start_conn_test(silent=True, force=True)
        except Exception as exc:
            messagebox.showerror("载入失败", str(exc))

    # ------------------------------------------------------------------ #
    # Queue polling
    # ------------------------------------------------------------------ #
    def _drain(self, worker):
        msgs = []
        while not worker.queue.empty():
            msgs.append(worker.queue.get_nowait())
        return msgs

    def _poll_conn(self) -> None:
        worker = self._conn_worker
        if worker is None:
            return
        for msg in self._drain(worker):
            if isinstance(msg, LogMsg):
                self._append_log(msg.text)
            elif isinstance(msg, DoneMsg):
                if self._conn_pending is not None:
                    continue  # stale result: a newer test is queued
                # Connection OK -> auto-load the catalog to enable autocomplete.
                self.conn_status.configure(text="●  已连接", foreground="#2e7d32")
                self.on_load_catalog(silent=True)
            elif isinstance(msg, ErrorMsg):
                self._append_log(msg.text)
                if self._conn_pending is not None:
                    continue  # stale result: a newer test is queued
                self.conn_status.configure(text="●  连接失败", foreground="#c62828")
                if not self._conn_silent:
                    messagebox.showerror("连接失败", msg.text)
        if worker.running:
            self.after(_POLL_MS, self._poll_conn)
        elif self._conn_pending is not None:
            pending, self._conn_pending = self._conn_pending, None
            self._start_conn_test(**pending)

    def _poll_catalog(self) -> None:
        worker = self._catalog_worker
        if worker is None:
            return
        for msg in self._drain(worker):
            if isinstance(msg, LogMsg):
                self._append_log(msg.text)
            elif isinstance(msg, CatalogMsg):
                if (
                    self._catalog_pending is not None
                    or self._catalog_conn_sig != self._connection_signature()
                ):
                    self._append_log("点位目录结果已过期（连接已更改），忽略")
                    continue
                self.points_table.set_catalog(msg.catalog)
                self.catalog_label.configure(
                    text=f"已加载 {len(msg.catalog)} 个点位，输入时可自动联想匹配"
                )
            elif isinstance(msg, ErrorMsg):
                self._append_log(msg.text)
                if self._catalog_pending is not None:
                    continue  # stale failure: a newer reload is queued
                self.catalog_label.configure(text="点位目录加载失败")
                if not self._catalog_silent:
                    messagebox.showerror("加载失败", msg.text)
        if worker.running:
            self.after(_POLL_MS, self._poll_catalog)
        else:
            self.catalog_btn.configure(state="normal")
            if self._catalog_pending is not None:
                silent, self._catalog_pending = self._catalog_pending, None
                self.on_load_catalog(silent=silent)

    def _poll_pull(self) -> None:
        worker = self._pull_worker
        if worker is None:
            return
        finished = False
        for msg in self._drain(worker):
            if isinstance(msg, LogMsg):
                self._append_log(msg.text)
            elif isinstance(msg, ProgressMsg):
                self.progress.configure(maximum=msg.total, value=msg.done)
                self.progress_label.configure(text=f"{msg.done}/{msg.total}")
            elif isinstance(msg, DoneMsg):
                self._append_log(f"✔ 拉取完成: {msg.rows} 行 -> {msg.output_path}")
                messagebox.showinfo(
                    "完成",
                    f"已保存 {msg.rows} 行到\n{msg.output_path}\n\n点击确定打开输出文件夹。",
                )
                self._reveal_in_explorer(msg.output_path)
                finished = True
            elif isinstance(msg, CancelledMsg):
                self._append_log("已取消。")
                finished = True
            elif isinstance(msg, ErrorMsg):
                self._append_log(f"✘ {msg.text}")
                messagebox.showerror("拉取失败", msg.text)
                finished = True

        if worker.running:
            self.after(_POLL_MS, self._poll_pull)
        elif finished or not worker.running:
            self.start_btn.configure(state="normal")
            self.cancel_btn.configure(state="disabled")


def _enable_windows_dpi_awareness() -> None:
    """Opt in to DPI awareness so Tk renders crisply on HiDPI displays.

    Must run before the Tk root window is created. No-op outside Windows.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system-DPI aware
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()  # pre-Win8.1 fallback
    except Exception:
        pass  # cosmetic only — never block startup


def main() -> None:
    _enable_windows_dpi_awareness()
    root = tk.Tk()

    # Match Tk's point-based font sizing to the real DPI, and scale the
    # default window geometry accordingly (capped to the visible screen).
    scale = 1.0
    try:
        dpi = root.winfo_fpixels("1i")
        if dpi > 0:
            scale = max(1.0, dpi / 96.0)
            root.tk.call("tk", "scaling", dpi / 72.0)
    except tk.TclError:
        pass
    width = min(int(980 * scale), root.winfo_screenwidth() - 40)
    height = min(int(880 * scale), root.winfo_screenheight() - 80)

    root.title("DataAcquirer — InfluxDB 数据拉取工具")
    root.geometry(f"{width}x{height}")
    try:
        ttk.Style().theme_use("vista")  # nicer on Windows; falls back if absent
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
