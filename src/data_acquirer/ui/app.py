"""Main application window."""

from __future__ import annotations

import datetime
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..config import AcquireConfig
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

PRESETS_DIR = Path(__file__).resolve().parents[3] / "config" / "presets"
_POLL_MS = 100


class App(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=10)
        self.master = master
        self.pack(fill="both", expand=True)

        self._pull_worker: PullWorker | None = None
        self._conn_worker: ConnectionTestWorker | None = None
        self._catalog_worker: CatalogWorker | None = None

        self._build_widgets()
        self._load_into_widgets(self._default_config())

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _build_widgets(self) -> None:
        self.vars: dict[str, tk.Variable] = {}

        # --- Connection ---
        conn = ttk.LabelFrame(self, text="连接配置", padding=8)
        conn.pack(fill="x")
        self._add_entry(conn, "host", "地址 (host)", 0, 0, width=18)
        self._add_entry(conn, "port", "端口 (port)", 0, 2, width=8)
        self._add_entry(conn, "database", "数据库 (database)", 0, 4, width=24)
        self._add_entry(conn, "username", "用户名", 1, 0, width=18)
        self._add_entry(conn, "password", "密码", 1, 2, width=18, show="*")
        ttk.Button(conn, text="测试连接", command=self.on_test_connection).grid(
            row=1, column=4, columnspan=2, sticky="e", padx=4, pady=2
        )

        # --- Time window & behaviour ---
        tf = ttk.LabelFrame(self, text="时间范围与行为", padding=8)
        tf.pack(fill="x", pady=(8, 0))
        self._add_entry(tf, "start_time", "起始时间", 0, 0, width=22)
        self._add_entry(tf, "end_time", "终止时间", 0, 2, width=22)
        ttk.Label(tf, text="(YYYY-MM-DD HH:MM:SS)", foreground="#666").grid(
            row=0, column=4, sticky="w", padx=4
        )
        self._add_entry(tf, "chunk_hours", "分段(小时)", 1, 0, width=8)
        self._add_entry(tf, "utc_offset_hours", "UTC偏移(小时)", 1, 2, width=8)
        self._add_entry(tf, "value_field", "取值字段", 1, 4, width=12)
        self._add_entry(tf, "measure_tag", "点位标签", 1, 6, width=14)

        # --- Points ---
        pf = ttk.LabelFrame(self, text="点位列表", padding=8)
        pf.pack(fill="both", expand=True, pady=(8, 0))

        catbar = ttk.Frame(pf)
        catbar.pack(fill="x", pady=(0, 6))
        self.catalog_btn = ttk.Button(catbar, text="加载点位目录", command=self.on_load_catalog)
        self.catalog_btn.pack(side="left")
        self.catalog_label = ttk.Label(catbar, text="未加载点位目录（加载后输入点位可自动联想）", foreground="#666")
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
        self.cancel_btn = ttk.Button(actions, text="取消", command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=4)
        ttk.Button(actions, text="保存配置…", command=self.on_save_config).pack(side="left", padx=4)
        ttk.Button(actions, text="载入配置…", command=self.on_load_config).pack(side="left")

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

    def _add_entry(self, parent, key, label, row, col, width=20, show=None) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="e", padx=(4, 2), pady=2)
        var = tk.StringVar()
        self.vars[key] = var
        ttk.Entry(parent, textvariable=var, width=width, show=show).grid(
            row=row, column=col + 1, sticky="w", padx=(0, 8), pady=2
        )

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
    def on_browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="选择输出 CSV 文件",
            defaultextension=".csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            initialfile=Path(self.vars["output_path"].get()).name or "data.csv",
        )
        if path:
            self.vars["output_path"].set(path)

    def on_test_connection(self) -> None:
        if self._conn_worker and self._conn_worker.running:
            return
        cfg = self._config_from_widgets()
        self._append_log(f"正在测试连接 {cfg.host}:{cfg.port} / {cfg.database} …")
        self._conn_worker = ConnectionTestWorker(cfg)
        self._conn_worker.start()
        self.after(_POLL_MS, self._poll_conn)

    def on_load_catalog(self) -> None:
        if self._catalog_worker and self._catalog_worker.running:
            return
        cfg = self._config_from_widgets()
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
            elif isinstance(msg, ErrorMsg):
                self._append_log(msg.text)
                messagebox.showerror("连接失败", msg.text)
        if worker.running:
            self.after(_POLL_MS, self._poll_conn)

    def _poll_catalog(self) -> None:
        worker = self._catalog_worker
        if worker is None:
            return
        for msg in self._drain(worker):
            if isinstance(msg, LogMsg):
                self._append_log(msg.text)
            elif isinstance(msg, CatalogMsg):
                self.points_table.set_catalog(msg.catalog)
                self.catalog_label.configure(
                    text=f"已加载 {len(msg.catalog)} 个点位，输入时可自动联想匹配"
                )
            elif isinstance(msg, ErrorMsg):
                self._append_log(msg.text)
                self.catalog_label.configure(text="点位目录加载失败")
                messagebox.showerror("加载失败", msg.text)
        if worker.running:
            self.after(_POLL_MS, self._poll_catalog)
        else:
            self.catalog_btn.configure(state="normal")

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
                messagebox.showinfo("完成", f"已保存 {msg.rows} 行到\n{msg.output_path}")
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


def main() -> None:
    root = tk.Tk()
    root.title("DataAcquirer — InfluxDB 数据拉取工具")
    root.geometry("980x820")
    try:
        ttk.Style().theme_use("vista")  # nicer on Windows; falls back if absent
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
