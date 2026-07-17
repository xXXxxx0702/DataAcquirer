"""Main application window."""

from __future__ import annotations

import calendar
import datetime
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from ..bookmarks import BookmarkStore, ServerBookmark
from ..config import AcquireConfig
from ..history import RunHistoryStore, RunRecord
from ..paths import (
    BOOKMARKS_PATH,
    LAST_SESSION_PATH,
    PRESETS_DIR,
    RUN_HISTORY_PATH,
)
from .points_table import PointsTable
from .widgets import ToolTip
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
    SegmentMsg,
)

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
APP_ICON_PNG = ASSETS_DIR / "app_icon.png"
APP_ICON_ICO = ASSETS_DIR / "app_icon.ico"
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
_WINDOWS_APP_ID = "DataAcquirer.InfluxDB.Client"

FIELD_TOOLTIPS = {
    "host": "InfluxDB 服务器的 IP 地址或主机名，例如 192.168.1.20。",
    "port": "InfluxDB HTTP API 端口，必须是 1–65535 之间的整数。",
    "database": "要查询的 InfluxDB 数据库名称。",
    "username": "InfluxDB 登录用户名；服务器未启用认证时可以留空。",
    "password": "InfluxDB 登录密码。运行历史会脱敏；预设和服务器书签仍会保存连接凭据。",
    "chunk_hours": "每次查询覆盖的小时数。数据量大时可适当减小，以便失败重试。",
    "utc_offset_hours": "本地时间相对 UTC 的小时偏移，例如中国标准时间填写 8。",
    "value_field": "InfluxDB measurement 中保存数值的字段名，通常为 value。",
    "measure_tag": "用于区分点位的 tag 名称，通常为 measurePoint。",
    "output_path": "CSV 输出路径。目录不存在时程序会自动创建。",
}


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
        spinbox.bind("<KeyRelease>", self._emit_edited, add="+")
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
            self._emit_edited()
            return

        value = datetime.datetime(year, month, day, hour, minute, second)
        self.variable.set(value.strftime(_TIME_FMT))
        self._sync_from_value()

    def _emit_edited(self, _event=None) -> None:
        """Tell the owning form to validate while a component is being typed."""
        self.event_generate("<<DateTimeEdited>>", when="tail")

    def parsed_value(self) -> datetime.datetime | None:
        """Return the value currently visible in the six component editors."""
        try:
            year = int(self._parts["year"].get())
            month = int(self._parts["month"].get())
            day = int(self._parts["day"].get())
            hour = int(self._parts["hour"].get())
            minute = int(self._parts["minute"].get())
            second = int(self._parts["second"].get())
            return datetime.datetime(year, month, day, hour, minute, second)
        except (TypeError, ValueError):
            return None

    def set_invalid(self, invalid: bool) -> None:
        style = "Invalid.Comfort.TSpinbox" if invalid else _NUMBER_SPINBOX_STYLE
        for spinbox in self._spinboxes.values():
            spinbox.configure(style=style)

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
        self.run_history = RunHistoryStore(RUN_HISTORY_PATH)
        self._last_conn_signature: tuple | None = None  # last connection we tested
        self._verified_conn_signature: tuple | None = None
        self._retest_after_id: str | None = None  # debounce handle
        self._conn_pending: dict | None = None  # test requested while one is running
        self._catalog_pending: bool | None = None  # reload requested while loading
        self._catalog_conn_sig: tuple | None = None  # connection the load belongs to

        self._loading_widgets = False
        self._validation_after_id: str | None = None
        self._field_errors: dict[str, str] = {}
        self._log_expanded = False
        self._pull_started_monotonic: float | None = None
        self._pull_started_at = ""
        self._pull_progress_after_id: str | None = None
        self._pull_active = False
        self._progress_done = 0
        self._progress_total = 0
        self._progress_phase = "idle"
        self._current_segment = 0
        self._current_window = ""
        self._active_pull_config: AcquireConfig | None = None
        self._last_completed_config: AcquireConfig | None = None
        self._history_window: tk.Toplevel | None = None
        self._history_tree: ttk.Treeview | None = None
        self._history_records_by_item: dict[str, RunRecord] = {}
        self._history_task_buttons: list[ttk.Button] = []
        self._run_lock_widgets: list[tuple[tk.Widget, str]] = []
        self._notice_output_path = ""

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
        self._tooltips: list[ToolTip] = []

        # The Vista spinbox arrows consume part of the text area.  Explicit
        # inner padding keeps digits clear of the arrows at Windows DPI scales.
        style = ttk.Style(self)
        style.configure(_NUMBER_SPINBOX_STYLE, padding=(4, 1, 4, 1))
        style.configure(
            "Invalid.TEntry",
            fieldbackground="#fff1f1",
            foreground="#9b1c1c",
        )
        style.configure(
            "Invalid.Comfort.TSpinbox",
            fieldbackground="#fff1f1",
            foreground="#9b1c1c",
            padding=(4, 1, 4, 1),
        )

        # --- Top toolbar ---
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 8))
        self.new_task_btn = ttk.Button(
            toolbar, text="＋ 新建任务", command=self.on_new_task
        )
        self.new_task_btn.pack(side="left")
        self.save_preset_btn = ttk.Button(
            toolbar, text="保存预设", command=self.on_save_config
        )
        self.save_preset_btn.pack(side="left", padx=(6, 0))
        self.load_preset_btn = ttk.Button(
            toolbar, text="加载预设", command=self.on_load_config
        )
        self.load_preset_btn.pack(side="left", padx=(6, 0))
        self.history_btn = ttk.Button(
            toolbar, text="历史记录", command=self.on_show_history
        )
        self.history_btn.pack(side="left", padx=(6, 0))

        self.conn_status = tk.Label(
            toolbar,
            text="●  未连接",
            background="#eef2f6",
            foreground="#59636e",
            padx=12,
            pady=4,
            font=("Segoe UI", 9, "bold"),
        )
        self.conn_status.pack(side="right")
        ttk.Label(toolbar, text="连接状态").pack(side="right", padx=(0, 8))

        # --- Two-column workspace ---
        workspace = ttk.Panedwindow(self, orient="horizontal")
        workspace.pack(fill="both", expand=True)
        left = ttk.Frame(workspace, padding=(0, 0, 5, 0))
        right = ttk.Frame(workspace, padding=(5, 0, 0, 0))
        workspace.add(left, weight=6)
        workspace.add(right, weight=7)

        # --- Connection ---
        conn = ttk.LabelFrame(left, text="连接配置", padding=8)
        conn.pack(fill="x")
        self._add_entry(conn, "host", "地址 (host)", 0, 0, width=18)
        self._add_entry(conn, "port", "端口 (port)", 0, 2, width=8)
        database_entry = self._add_entry(
            conn, "database", "数据库", 1, 0, width=34
        )
        database_entry.grid_configure(columnspan=3, sticky="ew")
        self._add_entry(conn, "username", "用户名", 2, 0, width=18)
        self._add_entry(conn, "password", "密码", 2, 2, width=18, show="*")
        conn.columnconfigure(1, weight=1)

        # Server bookmarks row.
        ttk.Label(conn, text="服务器书签").grid(
            row=3, column=0, sticky="e", padx=(4, 2), pady=(6, 2)
        )
        self.bookmark_var = tk.StringVar()
        self.bookmark_combo = ttk.Combobox(
            conn, textvariable=self.bookmark_var, state="readonly", width=28
        )
        self.bookmark_combo.grid(
            row=3, column=1, columnspan=3, sticky="ew", pady=(6, 2)
        )
        self.bookmark_combo.bind("<<ComboboxSelected>>", self._on_bookmark_selected)
        connection_actions = ttk.Frame(conn)
        connection_actions.grid(
            row=4, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        ttk.Button(
            connection_actions, text="测试连接", command=self.on_test_connection
        ).pack(side="left")
        ttk.Button(
            connection_actions, text="保存为书签…", command=self.on_save_bookmark
        ).pack(side="left", padx=4)
        ttk.Button(
            connection_actions, text="删除书签", command=self.on_delete_bookmark
        ).pack(side="left")
        self.connection_hint = ttk.Label(
            connection_actions, text="", foreground="#7a5a00"
        )
        self.connection_hint.pack(side="left", padx=8)
        self._refresh_bookmarks()

        # Re-test the connection automatically when a connection field changes.
        for key in CONNECTION_KEYS:
            self.entries[key].bind("<FocusOut>", self._on_conn_field_changed)
            self.entries[key].bind("<Return>", self._on_conn_field_changed)

        # --- Time window & behaviour ---
        tf = ttk.LabelFrame(left, text="时间范围与行为", padding=8)
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
            picker.bind(
                "<<DateTimeEdited>>",
                lambda _event, field=key: self._on_field_edited(field),
            )

        self._add_entry(tf, "chunk_hours", "分段(小时)", 2, 0, width=8)
        self._add_entry(tf, "utc_offset_hours", "UTC偏移(小时)", 2, 2, width=8)
        self._add_entry(tf, "value_field", "取值字段", 3, 0, width=15)
        self._add_entry(tf, "measure_tag", "点位标签", 3, 2, width=17)
        self.entries["measure_tag"].bind(
            "<FocusOut>", self._on_measure_tag_changed, add="+"
        )
        self.entries["measure_tag"].bind(
            "<Return>", self._on_measure_tag_changed, add="+"
        )

        quick = ttk.Frame(tf)
        quick.grid(row=4, column=0, columnspan=8, sticky="w", pady=(7, 0))
        ttk.Label(quick, text="快捷范围：").grid(
            row=0, column=0, rowspan=3, sticky="ne", pady=3
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

        for label, unit, amount in RECENT_RANGES[:3]:
            ttk.Button(
                quick_hours_days,
                text=label,
                width=6,
                command=lambda u=unit, n=amount: self._set_recent_range(u, n),
            ).pack(side="left", padx=2)

        quick_calendar = ttk.Frame(quick)
        quick_calendar.grid(row=1, column=1, sticky="w", pady=(3, 0))
        for label, unit, amount in RECENT_RANGES[3:]:
            ttk.Button(
                quick_calendar,
                text=label,
                width=6,
                command=lambda u=unit, n=amount: self._set_recent_range(u, n),
            ).pack(side="left", padx=2)
        ttk.Label(
            quick,
            text="（结束=当前时间，开始=往前推对应时长）",
            foreground="#666",
        ).grid(row=2, column=1, sticky="w", padx=2, pady=(3, 0))

        # --- Output and run actions ---
        of = ttk.LabelFrame(left, text="输出", padding=8)
        of.pack(fill="x", pady=(8, 0))
        output_entry = self._add_entry(
            of, "output_path", "CSV 文件", 0, 0, width=43
        )
        output_entry.grid_configure(sticky="ew")
        of.columnconfigure(1, weight=1)
        ttk.Button(of, text="浏览…", command=self.on_browse_output).grid(
            row=0, column=2, padx=4
        )

        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(8, 0))
        self.start_btn = ttk.Button(
            actions, text="开始拉取", command=self.on_start
        )
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            actions, text="取消", command=self.on_cancel, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=6)
        self.validation_label = ttk.Label(
            actions, text="", foreground="#b42318", wraplength=430
        )
        self.validation_label.pack(side="left", fill="x", expand=True, padx=(8, 0))

        # --- Points (right column) ---
        pf = ttk.LabelFrame(right, text="点位列表与筛选", padding=8)
        pf.pack(fill="both", expand=True)

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
        self.points_table.bind("<<PointsChanged>>", self._on_points_changed)

        # --- Run preview, completion notice and collapsible log ---
        run_panel = ttk.LabelFrame(right, text="运行状态与预览", padding=8)
        run_panel.pack(fill="x", pady=(8, 0))
        run_panel.columnconfigure(0, weight=1)

        self.notice_frame = tk.Frame(
            run_panel,
            background="#ecfdf3",
            highlightbackground="#86c89a",
            highlightthickness=1,
            padx=10,
            pady=8,
        )
        self.notice_title = tk.Label(
            self.notice_frame,
            text="",
            background="#ecfdf3",
            foreground="#146c2e",
            font=("Segoe UI", 10, "bold"),
        )
        self.notice_title.grid(row=0, column=0, sticky="w")
        self.notice_message = tk.Label(
            self.notice_frame,
            text="",
            background="#ecfdf3",
            foreground="#314437",
            anchor="w",
            justify="left",
            wraplength=620,
        )
        self.notice_message.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        notice_actions = tk.Frame(self.notice_frame, background="#ecfdf3")
        notice_actions.grid(row=2, column=0, sticky="w")
        self.notice_open_file_btn = ttk.Button(
            notice_actions, text="打开文件", command=self._open_notice_file
        )
        self.notice_open_file_btn.pack(side="left")
        self.notice_open_folder_btn = ttk.Button(
            notice_actions, text="打开目录", command=self._open_notice_folder
        )
        self.notice_open_folder_btn.pack(side="left", padx=4)
        self.repeat_btn = ttk.Button(
            notice_actions, text="再次拉取", command=self._repeat_last_pull
        )
        self.repeat_btn.pack(side="left")
        ttk.Button(
            notice_actions, text="关闭", command=self._hide_notice
        ).pack(side="left", padx=(4, 0))
        self.notice_frame.columnconfigure(0, weight=1)

        progress_row = ttk.Frame(run_panel)
        progress_row.grid(row=1, column=0, sticky="ew")
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(progress_row, text="等待开始", width=12)
        self.progress_label.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.progress_detail = ttk.Label(
            run_panel,
            text="当前分段 --  ·  速度 --  ·  已用 00:00  ·  剩余 --",
            foreground="#5f6b76",
        )
        self.progress_detail.grid(row=2, column=0, sticky="w", pady=(5, 0))

        log_header = ttk.Frame(run_panel)
        log_header.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        self.log_toggle_btn = ttk.Button(
            log_header, text="▶ 运行日志", command=self._toggle_log
        )
        self.log_toggle_btn.pack(side="left")
        ttk.Button(log_header, text="清空", command=self._clear_log).pack(
            side="right"
        )

        self.log_container = ttk.Frame(run_panel)
        self.log_container.grid(row=4, column=0, sticky="nsew", pady=(5, 0))
        self.log = tk.Text(
            self.log_container, height=8, wrap="word", state="disabled"
        )
        log_sb = ttk.Scrollbar(
            self.log_container, orient="vertical", command=self.log.yview
        )
        self.log.configure(yscrollcommand=log_sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_sb.pack(side="right", fill="y")
        self.log_container.grid_remove()

        for key, text in FIELD_TOOLTIPS.items():
            entry = self.entries.get(key)
            if entry is not None:
                self._tooltips.append(ToolTip(entry, text))
        self._tooltips.append(
            ToolTip(
                self.recent_hours_spinbox,
                "输入自定义小时数；支持键盘、上下箭头和鼠标滚轮。",
            )
        )
        for key, picker in self.time_pickers.items():
            description = (
                "设置拉取起始日期和时间；各部分支持直接输入、箭头和滚轮。"
                if key == "start_time"
                else "设置拉取结束日期和时间，必须晚于起始时间。"
            )
            for spinbox in picker._spinboxes.values():
                self._tooltips.append(ToolTip(spinbox, description))
        self._tooltips.append(
            ToolTip(
                self.points_table.search_entry,
                "按点位名、measurement 或备注实时筛选；不会删除隐藏点位。",
            )
        )
        self._progress_tooltip = ToolTip(
            self.progress_detail,
            "当前时间窗将在拉取开始后显示。",
        )
        self._tooltips.append(self._progress_tooltip)

        for key, var in self.vars.items():
            var.trace_add(
                "write",
                lambda *_args, field=key: self._on_field_edited(field),
            )
        self.recent_hours_var.trace_add("write", self._validate_recent_hours)

        def collect_run_lock_widgets(parent: tk.Widget) -> None:
            for child in parent.winfo_children():
                if isinstance(
                    child,
                    (ttk.Entry, ttk.Spinbox, ttk.Combobox, ttk.Button),
                ):
                    self._run_lock_widgets.append(
                        (child, str(child.cget("state")))
                    )
                collect_run_lock_widgets(child)

        for section in (conn, tf, of):
            collect_run_lock_widgets(section)

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
    # Inline validation and UI state
    # ------------------------------------------------------------------ #
    def _on_field_edited(self, field: str) -> None:
        if self._loading_widgets:
            return
        if field in CONNECTION_KEYS:
            if self._connection_signature() == self._verified_conn_signature:
                self._set_connection_status("success")
                self.connection_hint.configure(text="")
            else:
                self._set_connection_status("pending")
                self.connection_hint.configure(text="配置已更改，等待重新验证")
                self._clear_catalog()
        elif field == "measure_tag":
            self._clear_catalog()
        self._schedule_validation()

    def _on_points_changed(self, _event=None) -> None:
        if not self._loading_widgets:
            self._schedule_validation()

    def _validate_recent_hours(self, *_args) -> bool:
        valid = (
            self._read_int(
                self.recent_hours_var.get(),
                minimum=1,
                maximum=_CUSTOM_HOURS_MAX,
            )
            is not None
        )
        self.recent_hours_spinbox.configure(
            style=(
                _NUMBER_SPINBOX_STYLE
                if valid
                else "Invalid.Comfort.TSpinbox"
            )
        )
        return valid

    def _schedule_validation(self) -> None:
        if self._validation_after_id is not None:
            self.after_cancel(self._validation_after_id)
        self._validation_after_id = self.after(120, self._validate_now)

    @staticmethod
    def _read_int(
        raw: object,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int | None:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        if minimum is not None and value < minimum:
            return None
        if maximum is not None and value > maximum:
            return None
        return value

    def _validate_fields(self) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not self.vars["host"].get().strip():
            errors["host"] = "服务器地址不能为空"
        if self._read_int(self.vars["port"].get(), minimum=1, maximum=65535) is None:
            errors["port"] = "端口必须是 1–65535 之间的整数"
        if not self.vars["database"].get().strip():
            errors["database"] = "数据库不能为空"

        start = self.time_pickers["start_time"].parsed_value()
        end = self.time_pickers["end_time"].parsed_value()
        if start is None:
            errors["start_time"] = "起始日期或时间无效"
        if end is None:
            errors["end_time"] = "结束日期或时间无效"
        if start is not None and end is not None and end <= start:
            errors["end_time"] = "结束时间必须晚于起始时间"

        if self._read_int(self.vars["chunk_hours"].get(), minimum=1) is None:
            errors["chunk_hours"] = "分段小时数必须是大于 0 的整数"
        if (
            self._read_int(
                self.vars["utc_offset_hours"].get(), minimum=-14, maximum=14
            )
            is None
        ):
            errors["utc_offset_hours"] = "UTC 偏移必须是 -14 到 14 的整数"
        if not self.vars["value_field"].get().strip():
            errors["value_field"] = "取值字段不能为空"
        if not self.vars["measure_tag"].get().strip():
            errors["measure_tag"] = "点位标签不能为空"
        output_path = self.vars["output_path"].get().strip()
        if not output_path:
            errors["output_path"] = "CSV 输出路径不能为空"
        elif Path(output_path).suffix.lower() != ".csv":
            errors["output_path"] = "输出文件扩展名必须为 .csv"

        enabled_points = self.points_table.get_points()
        enabled_points = [point for point in enabled_points if point.enabled]
        if not any(point.name.strip() for point in enabled_points):
            errors["points"] = "至少需要一个启用且非空的点位"
        elif any(not point.name.strip() for point in enabled_points):
            errors["points"] = "启用的点位名称不能为空"
        elif any(not point.measurement.strip() for point in enabled_points):
            errors["points"] = "启用点位的 measurement 不能为空"
        else:
            names = [point.name.strip() for point in enabled_points]
            if len(names) != len(set(names)):
                errors["points"] = "启用的点位名称不能重复"
        return errors

    def _validate_now(self) -> dict[str, str]:
        self._validation_after_id = None
        errors = self._validate_fields()
        self._field_errors = errors

        for key, entry in self.entries.items():
            entry.configure(style="Invalid.TEntry" if key in errors else "TEntry")
        for key, picker in self.time_pickers.items():
            picker.set_invalid(key in errors)

        if errors:
            messages = list(errors.values())
            preview = "；".join(messages[:2])
            if len(messages) > 2:
                preview += f"；另有 {len(messages) - 2} 项"
            self.validation_label.configure(
                text=f"请修正 {len(messages)} 项：{preview}"
            )
        else:
            self.validation_label.configure(text="配置检查通过", foreground="#287a3e")
        if errors:
            self.validation_label.configure(foreground="#b42318")

        self.start_btn.configure(
            state="disabled" if errors or self._pull_active else "normal"
        )
        return errors

    def _validate_for_action(self) -> bool:
        self.points_table.commit_pending_edit()
        for picker in self.time_pickers.values():
            picker._sync_to_value()
        errors = self._validate_now()
        if not errors:
            return True

        first = next(iter(errors))
        if first in self.entries:
            self.entries[first].focus_set()
        elif first in self.time_pickers:
            next(iter(self.time_pickers[first]._spinboxes.values())).focus_set()
        elif first == "points":
            self.points_table.search_entry.focus_set()
        self.master.bell()
        return False

    def _connection_errors(self) -> dict[str, str]:
        all_errors = self._validate_fields()
        return {
            key: value
            for key, value in all_errors.items()
            if key in {"host", "port", "database"}
        }

    def _set_connection_status(self, state: str, detail: str = "") -> None:
        states = {
            "idle": ("●  未连接", "#eef2f6", "#59636e"),
            "pending": ("●  待验证", "#fff7db", "#8a6100"),
            "connecting": ("●  连接中", "#e8f2ff", "#1765a3"),
            "success": ("●  已连接", "#e8f7ec", "#18723a"),
            "error": ("●  失败", "#ffebeb", "#ad2424"),
        }
        text, background, foreground = states.get(state, states["idle"])
        self.conn_status.configure(
            text=text,
            background=background,
            foreground=foreground,
        )
        self.connection_hint.configure(text=detail)

    def _connection_config_from_widgets(self) -> AcquireConfig:
        cfg = AcquireConfig()
        cfg.host = self.vars["host"].get().strip()
        cfg.port = int(self.vars["port"].get().strip())
        cfg.username = self.vars["username"].get()
        cfg.password = self.vars["password"].get()
        cfg.database = self.vars["database"].get().strip()
        cfg.measure_tag = self.vars["measure_tag"].get().strip() or "measurePoint"
        return cfg

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
        if self._pull_active:
            if not messagebox.askyesno(
                "退出确认",
                "数据拉取仍在进行中，此时退出会中断拉取，"
                "输出文件可能不完整。\n\n确定要退出吗？",
            ):
                return
        try:
            self.points_table.commit_pending_edit()
            self._config_from_widgets().save(LAST_SESSION_PATH)
        except Exception:
            pass  # never block shutdown on a save failure
        self.master.destroy()

    def _load_into_widgets(self, cfg: AcquireConfig) -> None:
        previous_catalog_signature = self._catalog_signature()
        self._loading_widgets = True
        try:
            for key, var in self.vars.items():
                var.set(str(getattr(cfg, key)))
            self.points_table.set_points(cfg.points, notify=False)
        finally:
            self._loading_widgets = False
        if self._catalog_signature() != previous_catalog_signature:
            self._clear_catalog()
        self._validate_now()

    def _config_from_widgets(self) -> AcquireConfig:
        return AcquireConfig(
            host=self.vars["host"].get().strip(),
            port=int(self.vars["port"].get().strip()),
            username=self.vars["username"].get(),
            password=self.vars["password"].get(),
            database=self.vars["database"].get().strip(),
            start_time=self.vars["start_time"].get().strip(),
            end_time=self.vars["end_time"].get().strip(),
            chunk_hours=int(self.vars["chunk_hours"].get().strip()),
            utc_offset_hours=int(self.vars["utc_offset_hours"].get().strip()),
            value_field=self.vars["value_field"].get().strip(),
            measure_tag=self.vars["measure_tag"].get().strip(),
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

    def _set_log_expanded(self, expanded: bool) -> None:
        self._log_expanded = expanded
        if expanded:
            self.log_container.grid()
            self.log_toggle_btn.configure(text="▼ 运行日志")
        else:
            self.log_container.grid_remove()
            self.log_toggle_btn.configure(text="▶ 运行日志")

    def _toggle_log(self) -> None:
        self._set_log_expanded(not self._log_expanded)

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Progress and non-blocking notices
    # ------------------------------------------------------------------ #
    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        if seconds is None or seconds < 0:
            return "--"
        total = int(round(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _reset_progress(self) -> None:
        self._progress_done = 0
        self._progress_total = 0
        self._progress_phase = "preparing"
        self._current_segment = 0
        self._current_window = ""
        self._progress_tooltip.text = "当前时间窗将在拉取开始后显示。"
        self.progress.configure(value=0, maximum=100)
        self.progress_label.configure(text="准备中")
        self.progress_detail.configure(
            text="当前分段 --  ·  速度 --  ·  已用 00:00  ·  剩余 --"
        )

    def _update_progress_detail(self) -> None:
        elapsed = (
            time.monotonic() - self._pull_started_monotonic
            if self._pull_started_monotonic is not None
            else 0.0
        )
        segment = (
            f"{self._current_segment}/{self._progress_total}"
            if self._progress_total
            else "--"
        )
        if self._progress_phase == "exporting":
            self.progress_detail.configure(
                text=(
                    f"当前分段 {segment}  ·  正在整理并写入 CSV  ·  "
                    f"已用 {self._format_duration(elapsed)}  ·  剩余 --"
                )
            )
            return
        if self._progress_phase in {"cancelled", "failed"}:
            state = "已取消" if self._progress_phase == "cancelled" else "失败"
            self.progress_detail.configure(
                text=(
                    f"已完成 {self._progress_done}/{self._progress_total or '--'} 段"
                    f"  ·  已用 {self._format_duration(elapsed)}  ·  状态 {state}"
                )
            )
            return
        if self._progress_done > 0 and elapsed > 0:
            segments_per_second = self._progress_done / elapsed
            speed = f"{segments_per_second * 60:.1f} 段/分"
            remaining = max(0, self._progress_total - self._progress_done)
            eta = self._format_duration(remaining / segments_per_second)
        else:
            speed = "--"
            eta = "--"
        self.progress_detail.configure(
            text=(
                f"当前分段 {segment}  ·  速度 {speed}  ·  "
                f"已用 {self._format_duration(elapsed)}  ·  剩余 {eta}"
            )
        )

    def _schedule_progress_tick(self) -> None:
        if self._pull_progress_after_id is not None:
            self.after_cancel(self._pull_progress_after_id)
        self._pull_progress_after_id = self.after(500, self._progress_tick)

    def _progress_tick(self) -> None:
        self._pull_progress_after_id = None
        self._update_progress_detail()
        if self._pull_active:
            self._schedule_progress_tick()

    def _stop_progress_clock(self) -> None:
        if self._pull_progress_after_id is not None:
            self.after_cancel(self._pull_progress_after_id)
            self._pull_progress_after_id = None
        self._update_progress_detail()

    def _set_running_state(self, running: bool) -> None:
        self._pull_active = running
        self.cancel_btn.configure(state="normal" if running else "disabled")
        for button in (
            self.new_task_btn,
            self.load_preset_btn,
            self.history_btn,
            self.catalog_btn,
        ):
            button.configure(state="disabled" if running else "normal")
        for button in self._history_task_buttons:
            if button.winfo_exists():
                button.configure(state="disabled" if running else "normal")
        for widget, normal_state in self._run_lock_widgets:
            if widget.winfo_exists():
                widget.configure(
                    state="disabled" if running else normal_state
                )
        self.points_table.set_editable(not running)
        self.repeat_btn.configure(
            state=(
                "normal"
                if not running and self._last_completed_config is not None
                else "disabled"
            )
        )
        if running:
            self.start_btn.configure(state="disabled")
        else:
            self._validate_now()

    def _show_notice(
        self,
        title: str,
        message: str,
        *,
        kind: str = "success",
        output_path: str = "",
        allow_repeat: bool = False,
    ) -> None:
        palettes = {
            "success": ("#ecfdf3", "#86c89a", "#146c2e", "#314437"),
            "warning": ("#fff8e5", "#e4bd59", "#8a6100", "#594b25"),
            "error": ("#fff0f0", "#e39b9b", "#ad2424", "#573737"),
            "info": ("#edf6ff", "#8ab8df", "#1765a3", "#334b60"),
        }
        background, border, title_color, message_color = palettes.get(
            kind, palettes["info"]
        )
        self._notice_output_path = output_path
        self.notice_frame.configure(
            background=background,
            highlightbackground=border,
        )
        self.notice_title.configure(
            text=title, background=background, foreground=title_color
        )
        self.notice_message.configure(
            text=message, background=background, foreground=message_color
        )
        for child in self.notice_title.master.winfo_children():
            if isinstance(child, tk.Frame):
                child.configure(background=background)
        target = Path(output_path) if output_path else None
        file_available = bool(target and target.is_file())
        folder_available = bool(
            target
            and (
                target.is_dir()
                or target.parent.exists()
            )
        )
        self.notice_open_file_btn.configure(
            state="normal" if file_available else "disabled"
        )
        self.notice_open_folder_btn.configure(
            state="normal" if folder_available else "disabled"
        )
        self.repeat_btn.configure(
            state=(
                "normal"
                if (
                    allow_repeat
                    and self._last_completed_config is not None
                    and not self._pull_active
                )
                else "disabled"
            )
        )
        self.notice_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))

    def _hide_notice(self) -> None:
        self.notice_frame.grid_remove()

    def _open_file_path(self, output_path: str) -> None:
        path = Path(output_path)
        if not path.is_file():
            self._show_notice(
                "文件不存在",
                f"未找到输出文件：{path}",
                kind="error",
                output_path=str(path),
            )
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path.resolve()))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path.resolve())])
            else:
                subprocess.Popen(["xdg-open", str(path.resolve())])
        except Exception as exc:
            self._append_log(f"打开输出文件失败: {exc}")
            self._set_log_expanded(True)

    def _open_notice_file(self) -> None:
        self._open_file_path(self._notice_output_path)

    def _open_notice_folder(self) -> None:
        if self._notice_output_path:
            self._reveal_in_explorer(self._notice_output_path)

    def _repeat_last_pull(self) -> None:
        if self._pull_active or self._last_completed_config is None:
            return
        cfg = AcquireConfig.from_dict(self._last_completed_config.to_dict())
        self._load_into_widgets(cfg)
        self._hide_notice()
        if self._connection_signature() == self._verified_conn_signature:
            self._set_connection_status("success")
        else:
            self._set_connection_status("pending")
        self.after_idle(self.on_start)

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

    def on_new_task(self) -> None:
        if self._pull_active:
            return
        defaults = AcquireConfig()
        now = datetime.datetime.now().replace(microsecond=0)
        cfg = AcquireConfig(
            host=self.vars["host"].get().strip() or defaults.host,
            port=(
                self._read_int(
                    self.vars["port"].get(), minimum=1, maximum=65535
                )
                or defaults.port
            ),
            username=self.vars["username"].get(),
            password=self.vars["password"].get(),
            database=self.vars["database"].get().strip() or defaults.database,
            start_time=(now - datetime.timedelta(hours=24)).strftime(_TIME_FMT),
            end_time=now.strftime(_TIME_FMT),
            chunk_hours=24,
            utc_offset_hours=8,
            value_field="value",
            measure_tag="measurePoint",
            points=[],
            output_path="output/data.csv",
        )
        self._load_into_widgets(cfg)
        self.recent_hours_var.set(str(_CUSTOM_HOURS_DEFAULT))
        self._clear_log()
        self._hide_notice()
        self._reset_progress()
        if self._connection_signature() == self._verified_conn_signature:
            self._set_connection_status("success")
            if not self.points_table.catalog_size:
                self.on_load_catalog(silent=True)
        else:
            self._set_connection_status("pending")
        self._append_log("已新建任务；连接信息已保留")

    # ------------------------------------------------------------------ #
    # Run history
    # ------------------------------------------------------------------ #
    def _record_run(
        self,
        status: str,
        *,
        rows: int = 0,
        output_path: str = "",
        message: str = "",
    ) -> None:
        cfg = self._active_pull_config
        if cfg is None:
            return
        payload = cfg.to_dict()
        # History is intended for task discovery, not credential storage.
        # The current form or a matching bookmark supplies the password when
        # a historical task is loaded again.
        payload["password"] = ""
        elapsed = (
            time.monotonic() - self._pull_started_monotonic
            if self._pull_started_monotonic is not None
            else 0.0
        )
        record = RunRecord(
            started_at=self._pull_started_at,
            status=status,
            duration_seconds=max(0.0, elapsed),
            rows=rows,
            output_path=output_path or cfg.output_path,
            point_count=len(cfg.enabled_points()),
            config=payload,
            message=message,
        )
        try:
            self.run_history.append(record)
            self._refresh_history_view()
        except OSError as exc:
            self._append_log(f"保存运行历史失败: {exc}")

    def _config_for_history_record(self, record: RunRecord) -> AcquireConfig:
        cfg = AcquireConfig.from_dict(record.config)
        if cfg.password:
            return cfg

        current_connection = (
            self.vars["host"].get().strip(),
            self._read_int(self.vars["port"].get(), minimum=1, maximum=65535),
            self.vars["username"].get(),
            self.vars["database"].get().strip(),
        )
        record_connection = (cfg.host, cfg.port, cfg.username, cfg.database)
        if current_connection == record_connection:
            cfg.password = self.vars["password"].get()
            return cfg

        for name in self.bookmarks.names():
            bookmark = self.bookmarks.get(name)
            if bookmark is None:
                continue
            if (
                bookmark.host,
                bookmark.port,
                bookmark.username,
                bookmark.database,
            ) == record_connection:
                cfg.password = bookmark.password
                break
        return cfg

    def on_show_history(self) -> None:
        if self._history_window and self._history_window.winfo_exists():
            self._history_window.deiconify()
            self._history_window.lift()
            self._refresh_history_view()
            return

        window = tk.Toplevel(self)
        self._history_window = window
        window.title("拉取历史记录")
        window.geometry("960x430")
        window.minsize(760, 320)
        window.transient(self.master)

        body = ttk.Frame(window, padding=12)
        body.pack(fill="both", expand=True)
        columns = ("started", "status", "duration", "points", "rows", "output")
        tree = ttk.Treeview(body, columns=columns, show="headings")
        headings = {
            "started": "开始时间",
            "status": "状态",
            "duration": "耗时",
            "points": "点位",
            "rows": "行数",
            "output": "输出文件",
        }
        widths = {
            "started": 150,
            "status": 70,
            "duration": 75,
            "points": 60,
            "rows": 80,
            "output": 420,
        }
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(
                column,
                width=widths[column],
                anchor="w" if column in {"started", "output"} else "center",
            )
        tree.tag_configure("success", foreground="#18723a")
        tree.tag_configure("failed", foreground="#ad2424")
        tree.tag_configure("cancelled", foreground="#8a6100")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        tree.bind("<Double-1>", lambda _event: self._load_selected_history())
        self._history_tree = tree

        actions = ttk.Frame(window, padding=(12, 0, 12, 12))
        actions.pack(fill="x")
        load_task_button = ttk.Button(
            actions, text="加载到当前任务", command=self._load_selected_history
        )
        load_task_button.pack(side="left")
        run_task_button = ttk.Button(
            actions, text="再次拉取", command=self._run_selected_history
        )
        run_task_button.pack(side="left", padx=5)
        self._history_task_buttons = [load_task_button, run_task_button]
        if self._pull_active:
            for button in self._history_task_buttons:
                button.configure(state="disabled")
        ttk.Button(
            actions, text="打开文件", command=self._open_selected_history_file
        ).pack(side="left")
        ttk.Button(
            actions, text="打开目录", command=self._open_selected_history_folder
        ).pack(side="left", padx=5)
        ttk.Button(
            actions, text="清空历史", command=self._clear_history
        ).pack(side="right")
        ttk.Button(actions, text="关闭", command=window.destroy).pack(
            side="right", padx=5
        )

        def on_destroy(_event=None) -> None:
            self._history_window = None
            self._history_tree = None
            self._history_records_by_item = {}
            self._history_task_buttons = []

        window.bind("<Destroy>", lambda event: on_destroy() if event.widget is window else None)
        self._refresh_history_view()

    def _refresh_history_view(self) -> None:
        tree = self._history_tree
        if tree is None or not tree.winfo_exists():
            return
        tree.delete(*tree.get_children())
        self._history_records_by_item = {}
        status_labels = {
            "success": "成功",
            "failed": "失败",
            "cancelled": "已取消",
        }
        for record in self.run_history.records:
            item = tree.insert(
                "",
                "end",
                values=(
                    record.started_at.replace("T", " "),
                    status_labels.get(record.status, record.status),
                    self._format_duration(record.duration_seconds),
                    record.point_count,
                    record.rows,
                    record.output_path,
                ),
                tags=(record.status,),
            )
            self._history_records_by_item[item] = record

    def _selected_history_record(self) -> RunRecord | None:
        tree = self._history_tree
        if tree is None or not tree.winfo_exists():
            return None
        selection = tree.selection()
        if not selection:
            self.master.bell()
            return None
        return self._history_records_by_item.get(selection[0])

    def _load_selected_history(self) -> None:
        if self._pull_active:
            self.master.bell()
            return
        record = self._selected_history_record()
        if record is None:
            return
        try:
            cfg = self._config_for_history_record(record)
        except (TypeError, ValueError) as exc:
            self._append_log(f"历史配置无法载入: {exc}")
            self._set_log_expanded(True)
            return
        self._load_into_widgets(cfg)
        self._set_connection_status("pending")
        self._append_log(f"已载入历史任务: {record.started_at}")
        self._start_conn_test(silent=True, force=True)

    def _run_selected_history(self) -> None:
        if self._pull_active:
            self.master.bell()
            return
        record = self._selected_history_record()
        if record is None:
            return
        try:
            cfg = self._config_for_history_record(record)
        except (TypeError, ValueError) as exc:
            self._append_log(f"历史配置无法载入: {exc}")
            self._set_log_expanded(True)
            return
        self._load_into_widgets(cfg)
        if self._connection_signature() == self._verified_conn_signature:
            self._set_connection_status("success")
            self.connection_hint.configure(text="")
        else:
            self._set_connection_status("pending")
        if self._history_window and self._history_window.winfo_exists():
            self._history_window.destroy()
        self.after_idle(self.on_start)

    def _open_selected_history_file(self) -> None:
        record = self._selected_history_record()
        if record is None:
            return
        self._open_file_path(record.output_path)

    def _open_selected_history_folder(self) -> None:
        record = self._selected_history_record()
        if record and record.output_path:
            self._reveal_in_explorer(record.output_path)

    def _clear_history(self) -> None:
        if not self.run_history.records:
            return
        if messagebox.askyesno(
            "清空历史", "确定清空全部拉取历史吗？此操作不会删除 CSV 文件。"
        ):
            self.run_history.clear()
            self._refresh_history_view()

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
        self._loading_widgets = True
        try:
            self.vars["host"].set(bm.host)
            self.vars["port"].set(str(bm.port))
            self.vars["username"].set(bm.username)
            self.vars["password"].set(bm.password)
            self.vars["database"].set(bm.database)
        finally:
            self._loading_widgets = False
        self._validate_now()
        self._set_connection_status("pending")
        self._append_log(f"已切换到服务器书签: {bm.name}")
        self._start_conn_test(silent=True, force=True)

    def on_save_bookmark(self) -> None:
        if self._connection_errors():
            self._validate_now()
            self.connection_hint.configure(text="请先修正连接配置")
            self.master.bell()
            return
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

        self.bookmarks.upsert(
            ServerBookmark(
                name=name,
                host=self.vars["host"].get().strip(),
                port=int(self.vars["port"].get().strip()),
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
                    subprocess.Popen(["explorer", "/select,", str(target)])
                else:
                    os.startfile(str(folder))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:  # opening the folder is best-effort
            self._append_log(f"打开输出文件夹失败: {exc}")

    def _connection_signature(self) -> tuple:
        return tuple(str(self.vars[k].get()).strip() for k in CONNECTION_KEYS)

    def _catalog_signature(self) -> tuple:
        return (
            self._connection_signature(),
            self.vars["measure_tag"].get().strip(),
        )

    def _on_measure_tag_changed(self, _event=None) -> None:
        if (
            self._connection_signature() == self._verified_conn_signature
            and self.vars["measure_tag"].get().strip()
        ):
            self.on_load_catalog(silent=True)

    def on_test_connection(self) -> None:
        # Manual button: always test; the badge and log show the result.
        self._start_conn_test(silent=False, force=True)

    def _on_conn_field_changed(self, _event=None) -> None:
        # Debounce: a focus-out followed quickly by another shouldn't fire twice.
        if self._retest_after_id is not None:
            self.after_cancel(self._retest_after_id)
        self._retest_after_id = self.after(400, self._auto_retest)

    def _auto_retest(self) -> None:
        self._retest_after_id = None
        if self._connection_signature() == self._verified_conn_signature:
            self._set_connection_status("success")
            if not self.points_table.catalog_size:
                self.on_load_catalog(silent=True)
            return
        self._start_conn_test(silent=True, force=True)

    def _start_conn_test(self, *, silent: bool, force: bool = False) -> None:
        signature = self._connection_signature()
        connection_errors = self._connection_errors()
        if connection_errors:
            self._validate_now()
            self._set_connection_status(
                "pending", next(iter(connection_errors.values()))
            )
            return
        if signature != self._last_conn_signature:
            # Server changed: the old autocomplete catalog no longer applies.
            self._clear_catalog()
        if self._conn_worker and self._conn_worker.running:
            # Busy testing: remember the request and run it once finished.
            self._conn_pending = {"silent": silent, "force": force}
            return
        if not force and signature == self._verified_conn_signature:
            self._set_connection_status("success")
            self.connection_hint.configure(text="")
            return
        self._last_conn_signature = signature
        cfg = self._connection_config_from_widgets()
        self._set_connection_status("connecting", "正在验证服务器与数据库")
        self._append_log(f"正在测试连接 {cfg.host}:{cfg.port} / {cfg.database} …")
        self._conn_worker = ConnectionTestWorker(cfg)
        self._conn_worker.start()
        self.after(_POLL_MS, lambda worker=self._conn_worker: self._poll_conn(worker))

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
        connection_errors = self._connection_errors()
        if connection_errors or not self.vars["measure_tag"].get().strip():
            self._validate_now()
            self.catalog_label.configure(text="请先修正连接配置和点位标签")
            self.master.bell()
            return
        cfg = self._connection_config_from_widgets()
        cfg.measure_tag = self.vars["measure_tag"].get().strip()
        self._catalog_conn_sig = self._catalog_signature()
        self.catalog_btn.configure(state="disabled")
        self.catalog_label.configure(text="正在加载点位目录…")
        self._append_log(f"正在从 {cfg.host}:{cfg.port}/{cfg.database} 加载点位目录…")
        self._catalog_worker = CatalogWorker(cfg)
        self._catalog_worker.start()
        self.after(
            _POLL_MS,
            lambda worker=self._catalog_worker: self._poll_catalog(worker),
        )

    def on_start(self) -> None:
        if self._pull_active:
            return
        if not self._validate_for_action():
            return
        cfg = self._config_from_widgets()
        snapshot = AcquireConfig.from_dict(cfg.to_dict())
        self._active_pull_config = snapshot
        self._pull_started_monotonic = time.monotonic()
        self._pull_started_at = datetime.datetime.now().isoformat(timespec="seconds")
        self._hide_notice()
        self._reset_progress()
        self._set_log_expanded(True)
        self._set_running_state(True)
        if self._connection_signature() != self._verified_conn_signature:
            self._set_connection_status(
                "connecting", "任务正在使用该连接"
            )
        self._schedule_progress_tick()

        self._pull_worker = PullWorker(snapshot)
        try:
            self._pull_worker.start()
        except Exception as exc:
            self._progress_phase = "failed"
            self._append_log(f"✘ 无法启动后台拉取线程: {exc}")
            self._record_run("failed", message=str(exc))
            self._set_log_expanded(True)
            self._stop_progress_clock()
            self._set_running_state(False)
            self._show_notice(
                "拉取无法启动",
                str(exc),
                kind="error",
            )
            return
        self.after(
            _POLL_MS,
            lambda worker=self._pull_worker: self._poll_pull(worker),
        )

    def on_cancel(self) -> None:
        if self._pull_worker and self._pull_worker.running:
            self._pull_worker.cancel()
            self.cancel_btn.configure(state="disabled")
            self.progress_label.configure(text="取消中")
            self._append_log("已请求取消，正在停止…")

    def on_save_config(self) -> None:
        if not self._validate_for_action():
            return
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
            self._set_connection_status("pending")
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
        while True:
            try:
                msgs.append(worker.queue.get_nowait())
            except queue.Empty:
                break
        return msgs

    def _poll_conn(self, worker: ConnectionTestWorker | None = None) -> None:
        worker = worker or self._conn_worker
        if worker is None or worker is not self._conn_worker:
            return
        terminal = bool(getattr(worker, "_ui_terminal_seen", False))
        worker_signature = (
            str(worker.config.host).strip(),
            str(worker.config.port).strip(),
            str(worker.config.username).strip(),
            str(worker.config.password).strip(),
            str(worker.config.database).strip(),
        )
        stale = (
            self._conn_pending is not None
            or worker_signature != self._connection_signature()
        )
        for msg in self._drain(worker):
            if isinstance(msg, LogMsg):
                if not stale:
                    self._append_log(msg.text)
            elif isinstance(msg, DoneMsg):
                terminal = True
                worker._ui_terminal_seen = True
                if self._conn_pending is not None:
                    continue  # stale result: a newer test is queued
                if worker_signature != self._connection_signature():
                    continue
                # Connection OK -> auto-load the catalog to enable autocomplete.
                self._verified_conn_signature = worker_signature
                self._set_connection_status("success")
                self.connection_hint.configure(text="")
                self.on_load_catalog(silent=True)
            elif isinstance(msg, ErrorMsg):
                terminal = True
                worker._ui_terminal_seen = True
                if not stale:
                    self._append_log(msg.text)
                if self._conn_pending is not None:
                    continue  # stale result: a newer test is queued
                if worker_signature != self._connection_signature():
                    continue
                self._verified_conn_signature = None
                self._set_connection_status("error", "请检查地址、凭据或数据库")
                self._set_log_expanded(True)
        if worker.running or not terminal:
            self.after(_POLL_MS, lambda: self._poll_conn(worker))
        elif self._conn_pending is not None:
            pending, self._conn_pending = self._conn_pending, None
            self._start_conn_test(**pending)

    def _poll_catalog(self, worker: CatalogWorker | None = None) -> None:
        worker = worker or self._catalog_worker
        if worker is None or worker is not self._catalog_worker:
            return
        terminal = bool(getattr(worker, "_ui_terminal_seen", False))
        stale = (
            self._catalog_pending is not None
            or self._catalog_conn_sig != self._catalog_signature()
        )
        for msg in self._drain(worker):
            if isinstance(msg, LogMsg):
                if not stale:
                    self._append_log(msg.text)
            elif isinstance(msg, CatalogMsg):
                terminal = True
                worker._ui_terminal_seen = True
                if (
                    self._catalog_pending is not None
                    or self._catalog_conn_sig != self._catalog_signature()
                ):
                    self._append_log("点位目录结果已过期（连接已更改），忽略")
                    continue
                self.points_table.set_catalog(msg.catalog)
                self.catalog_label.configure(
                    text=f"已加载 {len(msg.catalog)} 个点位，输入时可自动联想匹配"
                )
            elif isinstance(msg, ErrorMsg):
                terminal = True
                worker._ui_terminal_seen = True
                if not stale:
                    self._append_log(msg.text)
                if self._catalog_pending is not None:
                    continue  # stale failure: a newer reload is queued
                self.catalog_label.configure(text="点位目录加载失败")
                self._set_log_expanded(True)
        if worker.running or not terminal:
            self.after(_POLL_MS, lambda: self._poll_catalog(worker))
        else:
            self.catalog_btn.configure(
                state="disabled" if self._pull_active else "normal"
            )
            if self._catalog_pending is not None:
                silent, self._catalog_pending = self._catalog_pending, None
                self.on_load_catalog(silent=silent)

    def _poll_pull(self, worker: PullWorker | None = None) -> None:
        worker = worker or self._pull_worker
        if worker is None or worker is not self._pull_worker:
            return
        terminal = False
        notice: tuple[str, str, str, str] | None = None
        for msg in self._drain(worker):
            if isinstance(msg, LogMsg):
                self._append_log(msg.text)
            elif isinstance(msg, SegmentMsg):
                self._progress_phase = "pulling"
                self._current_segment = msg.current
                self._progress_total = msg.total
                self._current_window = f"{msg.start} → {msg.end}"
                self._progress_tooltip.text = (
                    f"当前时间窗：{self._current_window}"
                )
                self.progress.configure(maximum=max(1, msg.total))
                self.progress_label.configure(text=f"{msg.current}/{msg.total}")
                self._update_progress_detail()
            elif isinstance(msg, ProgressMsg):
                self._progress_done = msg.done
                self._progress_total = msg.total
                self.progress.configure(maximum=msg.total, value=msg.done)
                if msg.total and msg.done >= msg.total:
                    self._progress_phase = "exporting"
                    self.progress_label.configure(text="正在写入")
                else:
                    self.progress_label.configure(text=f"{msg.done}/{msg.total}")
                self._update_progress_detail()
            elif isinstance(msg, DoneMsg):
                self._append_log(f"✔ 拉取完成: {msg.rows} 行 -> {msg.output_path}")
                self._progress_done = max(
                    self._progress_done, self._progress_total
                )
                self.progress.configure(
                    maximum=max(1, self._progress_total),
                    value=max(1, self._progress_total),
                )
                self.progress_label.configure(text="已完成")
                self._progress_phase = "complete"
                if self._active_pull_config is not None:
                    self._last_completed_config = AcquireConfig.from_dict(
                        self._active_pull_config.to_dict()
                    )
                    active_signature = (
                        str(self._active_pull_config.host).strip(),
                        str(self._active_pull_config.port).strip(),
                        str(self._active_pull_config.username).strip(),
                        str(self._active_pull_config.password).strip(),
                        str(self._active_pull_config.database).strip(),
                    )
                    if active_signature == self._connection_signature():
                        self._verified_conn_signature = active_signature
                        self._set_connection_status("success")
                self._record_run(
                    "success",
                    rows=msg.rows,
                    output_path=msg.output_path,
                )
                notice = (
                    "拉取完成",
                    f"已导出 {msg.rows} 行数据到：\n{msg.output_path}",
                    "success",
                    msg.output_path,
                )
                terminal = True
            elif isinstance(msg, CancelledMsg):
                self._append_log("已取消。")
                self.progress_label.configure(text="已取消")
                self._progress_phase = "cancelled"
                self._record_run("cancelled", message="用户取消")
                if self._connection_signature() != self._verified_conn_signature:
                    self._set_connection_status(
                        "pending", "任务已取消，连接状态未确认"
                    )
                notice = (
                    "任务已取消",
                    "拉取已停止；可以调整配置后重新开始。",
                    "warning",
                    "",
                )
                terminal = True
            elif isinstance(msg, ErrorMsg):
                self._append_log(f"✘ {msg.text}")
                self.progress_label.configure(text="失败")
                self._progress_phase = "failed"
                self._record_run("failed", message=msg.text)
                if self._connection_signature() != self._verified_conn_signature:
                    self._set_connection_status(
                        "pending", "任务失败，连接状态未确认"
                    )
                self._set_log_expanded(True)
                notice = ("拉取失败", msg.text, "error", "")
                terminal = True

        if terminal:
            self._stop_progress_clock()
            self._set_running_state(False)
            if notice is not None:
                title, message, kind, output_path = notice
                self._show_notice(
                    title,
                    message,
                    kind=kind,
                    output_path=output_path,
                    allow_repeat=(kind == "success"),
                )
        else:
            # A worker may have exited just before putting its terminal message;
            # keep polling until that message is observed.
            self.after(_POLL_MS, lambda: self._poll_pull(worker))


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


def _set_windows_app_id() -> None:
    """Give the taskbar a stable identity instead of grouping under Python."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        set_app_id(_WINDOWS_APP_ID)
    except Exception:
        pass  # cosmetic only — never block startup


def _set_window_icon(root: tk.Tk) -> None:
    """Apply the bundled icon to the window chrome and taskbar."""
    try:
        if sys.platform.startswith("win") and APP_ICON_ICO.exists():
            # Tk's default lets future Toplevels inherit the application icon.
            # The mapped root gets DPI-specific handles in the idle callback.
            root.iconbitmap(default=str(APP_ICON_ICO))
            root.iconbitmap(str(APP_ICON_ICO))
            root.after_idle(lambda: _set_windows_window_icons(root))
        elif APP_ICON_PNG.exists():
            icon_image = tk.PhotoImage(file=str(APP_ICON_PNG))
            root.iconphoto(True, icon_image)
            root.iconphoto(False, icon_image)
            # Tk must retain a reference for the lifetime of the window.
            root._data_acquirer_icon = icon_image
    except Exception:
        pass  # cosmetic only — never block startup


def _set_windows_window_icons(root: tk.Tk) -> None:
    """Bind separate DPI-sized ICO frames to the current Win32 window."""
    if not sys.platform.startswith("win") or not APP_ICON_ICO.exists():
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t

        hwnd = user32.GetAncestor(root.winfo_id(), 2) or root.winfo_id()
        image_icon = 1
        load_from_file = 0x0010
        small_width = user32.GetSystemMetrics(49)
        small_height = user32.GetSystemMetrics(50)
        large_width = user32.GetSystemMetrics(11)
        large_height = user32.GetSystemMetrics(12)
        small_icon = user32.LoadImageW(
            None,
            str(APP_ICON_ICO),
            image_icon,
            small_width,
            small_height,
            load_from_file,
        )
        large_icon = user32.LoadImageW(
            None,
            str(APP_ICON_ICO),
            image_icon,
            large_width,
            large_height,
            load_from_file,
        )
        if small_icon:
            user32.SendMessageW(hwnd, 0x0080, 0, small_icon)  # WM_SETICON / small
        if large_icon:
            user32.SendMessageW(hwnd, 0x0080, 1, large_icon)  # WM_SETICON / big
        root._windows_icon_handles = (small_icon, large_icon)
    except Exception:
        pass  # cosmetic only — never block startup


def main() -> None:
    _enable_windows_dpi_awareness()
    _set_windows_app_id()
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
    width = min(int(1220 * scale), root.winfo_screenwidth() - 40)
    height = min(int(760 * scale), root.winfo_screenheight() - 80)

    root.title("DataAcquirer — InfluxDB 数据拉取工具")
    _set_window_icon(root)
    root.geometry(f"{width}x{height}")
    root.minsize(
        min(int(1040 * scale), root.winfo_screenwidth() - 40),
        min(int(680 * scale), root.winfo_screenheight() - 80),
    )
    try:
        ttk.Style().theme_use("vista")  # nicer on Windows; falls back if absent
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
