from __future__ import annotations

from datetime import datetime
import getpass
import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any

import doubao_mumu_web_pipeline as pipeline

if str(pipeline.MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(pipeline.MONITOR_DIR))
import doubao_brand_settings as brand_settings


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "doubao_mumu_panel_config.json"
JOB_RUNNER = BASE_DIR / "doubao_mumu_scheduled_job.py"
SCHEDULE_BAT = BASE_DIR / "run_scheduled_doubao_job.bat"
JOB_LOG = BASE_DIR / "doubao_mumu_scheduled_job.log"
PIPELINE_LOG = BASE_DIR / "doubao_mumu_web_pipeline.log"
READINESS_PATH = BASE_DIR / "doubao_panel_readiness.json"
TASK_NAME = "DoubaoMuMuMonitorPipeline"
CREATE_NO_WINDOW = (
    subprocess.CREATE_NO_WINDOW
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
    else 0
)

SCHEDULE_LABELS = {
    "不启用定时": "none",
    "每天固定时间": "daily",
    "每隔若干分钟": "interval",
    "仅运行一次": "once",
}
SCHEDULE_LABELS_REVERSE = {value: key for key, value in SCHEDULE_LABELS.items()}


class QueueLogHandler(logging.Handler):
    def __init__(self, target: queue.Queue[Any]) -> None:
        super().__init__()
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.target.put(("log", self.format(record)))
        except Exception:
            pass


class QuestionDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        question: str = "",
        repeat: int = 10,
    ) -> None:
        super().__init__(parent)
        self.title(title)
        self.geometry("620x250")
        self.resizable(True, False)
        self.transient(parent)
        self.grab_set()
        self.result: tuple[str, int] | None = None

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)
        ttk.Label(outer, text="问题内容").pack(anchor="w")
        self.text = tk.Text(outer, height=5, wrap="word", font=("Microsoft YaHei UI", 10))
        self.text.pack(fill=tk.X, pady=(6, 12))
        self.text.insert("1.0", question)

        count_row = ttk.Frame(outer)
        count_row.pack(fill=tk.X)
        ttk.Label(count_row, text="重复次数").pack(side=tk.LEFT)
        self.repeat_var = tk.StringVar(value=str(repeat))
        ttk.Spinbox(
            count_row,
            from_=1,
            to=100000,
            textvariable=self.repeat_var,
            width=12,
        ).pack(side=tk.LEFT, padx=(10, 0))

        buttons = ttk.Frame(outer)
        buttons.pack(fill=tk.X, pady=(18, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="确定", command=self.confirm).pack(
            side=tk.RIGHT,
            padx=(0, 8),
        )
        self.bind("<Control-Return>", lambda _event: self.confirm())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.text.focus_set()

    def confirm(self) -> None:
        question = self.text.get("1.0", tk.END).strip()
        try:
            repeat = int(self.repeat_var.get().strip())
        except ValueError:
            messagebox.showerror("参数错误", "重复次数必须是整数。", parent=self)
            return
        if not question:
            messagebox.showerror("参数错误", "问题内容不能为空。", parent=self)
            return
        if repeat <= 0:
            messagebox.showerror("参数错误", "重复次数必须大于 0。", parent=self)
            return
        self.result = (question, repeat)
        self.destroy()


class DoubaoMuMuControlPanel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("豆包 MuMu 批量提问与定时控制台")
        self.root.geometry("1180x760")
        self.root.minsize(980, 700)
        self.events: queue.Queue[Any] = queue.Queue()
        self.job_process: subprocess.Popen[Any] | None = None
        self.probe_running = False
        self.job_log_offset = 0
        self.readiness_ready = False
        self.ready_instance_count = 0
        self.last_readiness_state = ""
        self.prepared_browser_process: subprocess.Popen[Any] | None = None
        self.prepared_cdp_port: int | None = None
        self.cached_mobile_accounts: dict[
            str, tuple[dict[str, str], float]
        ] = {}
        self.prepared_browser_processes: dict[
            str, subprocess.Popen[Any]
        ] = {}
        self.prepared_cdp_ports: dict[str, int] = {}
        self.autosave_job: str | None = None
        self.schedule_probe_running = False
        self.auto_start_on_ready = (
            os.environ.pop("DOUBAO_PANEL_AUTOSTART_ON_READY", "") == "1"
        )
        self.resume_completed_rounds = max(
            0,
            int(os.environ.pop("DOUBAO_PANEL_RESUME_COMPLETED_ROUNDS", "0") or 0),
        )

        self.device_var = tk.StringVar(value="未检测")
        self.mobile_account_var = tk.StringVar(value="未检测")
        self.web_account_var = tk.StringVar(value="未检测")
        self.status_var = tk.StringVar(value="就绪")
        self.readiness_var = tk.StringVar(
            value="正在检查 MuMu、网页登录和账号一致性……"
        )
        self.question_mode_var = tk.StringVar(value="interleaved")
        self.device_index_var = tk.StringVar(value="")

        self.min_wait_var = tk.StringVar(value="8")
        self.stable_var = tk.StringVar(value="5")
        self.answer_timeout_var = tk.StringVar(value="240")
        self.sync_timeout_var = tk.StringVar(value="300")
        self.retry_delay_var = tk.StringVar(value="5")
        self.cooldown_min_var = tk.StringVar(value="10")
        self.cooldown_max_var = tk.StringVar(value="20")
        self.max_retries_var = tk.StringVar(value="0")

        self.schedule_mode_var = tk.StringVar(value="不启用定时")
        self.daily_time_var = tk.StringVar(value="09:00")
        self.interval_minutes_var = tk.StringVar(value="60")
        self.once_datetime_var = tk.StringVar(
            value=(datetime.now(pipeline.BEIJING_TZ).replace(
                tzinfo=None, second=0, microsecond=0
            )).strftime(
                "%Y-%m-%d %H:%M"
            )
        )
        self.schedule_status_var = tk.StringVar(
            value="正在检查 Windows 定时任务……"
        )

        self.build_ui_v2()
        self.load_config()
        self.install_autosave()
        self.root.after(200, self.drain_events)
        self.root.after(1000, self.refresh_process_status)
        self.root.after(1000, self.poll_job_log)
        # Open one isolated web session for every running MuMu first.  Account
        # validation is intentionally manual so the user has time to log in.
        self.root.after(700, self.prepare_browser_sessions)
        self.root.after(1200, self.refresh_schedule_status)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def build_ui(self) -> None:
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Status.TLabel", foreground="#087f5b")
        style.configure("Warn.TLabel", foreground="#9a6700")

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            outer,
            text="豆包 MuMu 批量提问与定时控制台",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "每个问题可单独设置重复次数；手机端负责发送，"
                "同账号网页端负责定位新会话、抓取和保存。"
            ),
            foreground="#566",
        ).pack(anchor="w", pady=(5, 10))

        identity = ttk.LabelFrame(outer, text="设备与账号", padding=10)
        identity.pack(fill=tk.X)
        identity.columnconfigure(1, weight=1)
        identity.columnconfigure(3, weight=1)
        ttk.Label(identity, text="MuMu 设备").grid(row=0, column=0, sticky="w")
        ttk.Label(identity, textvariable=self.device_var).grid(
            row=0,
            column=1,
            sticky="w",
            padx=(8, 20),
        )
        ttk.Label(identity, text="运行状态").grid(row=0, column=2, sticky="w")
        ttk.Label(
            identity,
            textvariable=self.status_var,
            style="Status.TLabel",
        ).grid(row=0, column=3, sticky="w", padx=(8, 20))
        ttk.Label(identity, text="MuMu 账号").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(identity, textvariable=self.mobile_account_var).grid(
            row=1,
            column=1,
            sticky="w",
            padx=(8, 20),
            pady=(8, 0),
        )
        ttk.Label(identity, text="网页账号").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Label(identity, textvariable=self.web_account_var).grid(
            row=1,
            column=3,
            sticky="w",
            padx=(8, 20),
            pady=(8, 0),
        )
        ttk.Label(identity, text="MuMu 实例编号").grid(
            row=0,
            column=4,
            sticky="e",
            padx=(20, 6),
        )
        ttk.Entry(identity, textvariable=self.device_index_var, width=8).grid(
            row=0,
            column=5,
            sticky="e",
        )
        self.probe_button = ttk.Button(
            identity,
            text="识别设备与账号",
            command=self.probe_account,
        )
        self.probe_button.grid(row=1, column=4, columnspan=2, sticky="e", pady=(8, 0))
        self.readiness_label = tk.Label(
            identity,
            textvariable=self.readiness_var,
            anchor="w",
            justify=tk.LEFT,
            font=("Microsoft YaHei UI", 10, "bold"),
            fg="#9a6700",
            bg="#f8fafc",
            padx=8,
            pady=7,
        )
        self.readiness_label.grid(
            row=2,
            column=0,
            columnspan=6,
            sticky="ew",
            pady=(10, 0),
        )

        question_frame = ttk.LabelFrame(outer, text="问题计划", padding=10)
        question_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        mode_row = ttk.Frame(question_frame)
        mode_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(mode_row, text="执行顺序").pack(side=tk.LEFT)
        ttk.Radiobutton(
            mode_row,
            text="交叉提问（问题1第1次 → 问题2第1次）",
            variable=self.question_mode_var,
            value="interleaved",
        ).pack(side=tk.LEFT, padx=(12, 8))
        ttk.Radiobutton(
            mode_row,
            text="顺序提问（先完成问题1全部次数）",
            variable=self.question_mode_var,
            value="sequential",
        ).pack(side=tk.LEFT)
        self.total_label = ttk.Label(mode_row, text="0 个问题 / 0 轮")
        self.total_label.pack(side=tk.RIGHT)

        tree_wrap = ttk.Frame(question_frame)
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("number", "question", "repeat"),
            show="headings",
            selectmode="extended",
            height=7,
        )
        self.tree.heading("number", text="序号")
        self.tree.heading("question", text="问题内容")
        self.tree.heading("repeat", text="重复次数")
        self.tree.column("number", width=70, anchor="center", stretch=False)
        self.tree.column("question", width=850, anchor="w")
        self.tree.column("repeat", width=100, anchor="center", stretch=False)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(tree_wrap, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda _event: self.edit_question())

        question_buttons = ttk.Frame(question_frame)
        question_buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(question_buttons, text="添加问题", command=self.add_question).pack(side=tk.LEFT)
        ttk.Button(question_buttons, text="编辑", command=self.edit_question).pack(side=tk.LEFT, padx=6)
        ttk.Button(question_buttons, text="删除", command=self.delete_questions).pack(side=tk.LEFT)
        ttk.Button(question_buttons, text="统一设置次数", command=self.set_selected_repeat).pack(
            side=tk.LEFT,
            padx=(12, 6),
        )
        ttk.Button(question_buttons, text="上移", command=lambda: self.move_selected(-1)).pack(side=tk.LEFT)
        ttk.Button(question_buttons, text="下移", command=lambda: self.move_selected(1)).pack(
            side=tk.LEFT,
            padx=6,
        )
        ttk.Button(question_buttons, text="从 TXT 导入", command=self.import_questions).pack(
            side=tk.LEFT,
            padx=(12, 0),
        )

        runtime = ttk.LabelFrame(outer, text="运行参数", padding=10)
        runtime.pack(fill=tk.X, pady=(10, 0))
        runtime_fields = [
            ("最少等待秒", self.min_wait_var),
            ("稳定秒数", self.stable_var),
            ("回答超时秒", self.answer_timeout_var),
            ("网页同步超时秒", self.sync_timeout_var),
            ("异常重试秒", self.retry_delay_var),
            ("轮间最小冷却秒", self.cooldown_min_var),
            ("轮间最大冷却秒", self.cooldown_max_var),
            ("最大重试次数（0=持续）", self.max_retries_var),
        ]
        for index, (label, variable) in enumerate(runtime_fields):
            row = index // 4
            column = (index % 4) * 2
            ttk.Label(runtime, text=label).grid(
                row=row,
                column=column,
                sticky="w",
                pady=5,
            )
            ttk.Entry(runtime, textvariable=variable, width=10).grid(
                row=row,
                column=column + 1,
                sticky="w",
                padx=(7, 18),
                pady=5,
            )

        schedule = ttk.LabelFrame(outer, text="整个任务定时", padding=10)
        schedule.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(schedule, text="模式").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            schedule,
            textvariable=self.schedule_mode_var,
            values=list(SCHEDULE_LABELS),
            state="readonly",
            width=16,
        ).grid(row=0, column=1, sticky="w", padx=(7, 20))
        ttk.Label(schedule, text="每天时间 HH:MM").grid(row=0, column=2, sticky="w")
        ttk.Entry(schedule, textvariable=self.daily_time_var, width=9).grid(
            row=0,
            column=3,
            sticky="w",
            padx=(7, 20),
        )
        ttk.Label(schedule, text="间隔分钟").grid(row=0, column=4, sticky="w")
        ttk.Entry(schedule, textvariable=self.interval_minutes_var, width=9).grid(
            row=0,
            column=5,
            sticky="w",
            padx=(7, 20),
        )
        ttk.Label(schedule, text="单次时间 YYYY-MM-DD HH:MM").grid(
            row=0,
            column=6,
            sticky="w",
        )
        ttk.Entry(schedule, textvariable=self.once_datetime_var, width=18).grid(
            row=0,
            column=7,
            sticky="w",
            padx=(7, 0),
        )
        ttk.Label(
            schedule,
            text="定时由 Windows 任务计划执行；若上一次整批任务尚未结束，本次触发会跳过，不会堆积或抢控设备。",
            style="Warn.TLabel",
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(8, 0))

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=(10, 8))
        ttk.Button(actions, text="保存配置", command=self.save_config_clicked).pack(side=tk.LEFT)
        self.start_button = tk.Button(
            actions,
            text="开始提问循环（等待准备完成）",
            command=self.start_now,
            state=tk.DISABLED,
            bg="#d1d5db",
            fg="#6b7280",
            disabledforeground="#6b7280",
            activebackground="#d1d5db",
            relief=tk.FLAT,
            padx=18,
            pady=6,
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        self.start_button.pack(side=tk.LEFT, padx=(8, 0))
        self.stop_button = ttk.Button(
            actions,
            text="停止当前任务",
            command=self.stop_current,
            state=tk.DISABLED,
        )
        self.stop_button.pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="安装/更新定时", command=self.install_schedule).pack(
            side=tk.LEFT,
            padx=(12, 0),
        )
        ttk.Button(actions, text="删除定时", command=self.delete_schedule).pack(side=tk.LEFT, padx=8)
        ttk.Button(actions, text="查看定时状态", command=self.query_schedule).pack(side=tk.LEFT)
        ttk.Button(
            actions,
            text="打开实时数据面板",
            command=lambda: os.startfile("http://127.0.0.1:8765/"),
        ).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(actions, text="打开程序文件夹", command=self.open_folder).pack(side=tk.RIGHT)

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, height=7, wrap="word", font=("Consolas", 9))
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def build_ui_v2(self) -> None:
        """Compact dashboard-style layout with an always-visible action bar."""
        palette = {
            "page": "#F3F6FA",
            "card": "#FFFFFF",
            "line": "#DCE3EC",
            "text": "#172033",
            "muted": "#667085",
            "blue": "#2563EB",
            "blue_dark": "#1D4ED8",
            "green": "#059669",
            "green_dark": "#047857",
            "soft_blue": "#EFF6FF",
        }
        self.root.configure(bg=palette["page"])
        self.root.geometry("1180x760")
        self.root.minsize(980, 680)

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            ".",
            font=("Microsoft YaHei UI", 9),
            background=palette["page"],
            foreground=palette["text"],
        )
        style.configure(
            "Panel.TFrame",
            background=palette["card"],
        )
        style.configure(
            "Title.TLabel",
            background=palette["page"],
            foreground=palette["text"],
            font=("Microsoft YaHei UI", 19, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=palette["page"],
            foreground=palette["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background=palette["card"],
            foreground=palette["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "Meta.TLabel",
            background=palette["card"],
            foreground=palette["muted"],
        )
        style.configure(
            "Value.TLabel",
            background=palette["card"],
            foreground=palette["text"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "Status.TLabel",
            background=palette["card"],
            foreground=palette["green_dark"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "Card.TLabelframe",
            background=palette["card"],
            bordercolor=palette["line"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=palette["card"],
            foreground=palette["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "TButton",
            padding=(11, 6),
            borderwidth=1,
        )
        style.map(
            "TButton",
            background=[("active", "#E9EEF5")],
        )
        style.configure(
            "Accent.TButton",
            background=palette["blue"],
            foreground="#FFFFFF",
            bordercolor=palette["blue"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Accent.TButton",
            background=[("active", palette["blue_dark"])],
            foreground=[("active", "#FFFFFF")],
        )
        style.configure(
            "Danger.TButton",
            background="#FFF1F2",
            foreground="#BE123C",
            bordercolor="#FECDD3",
        )
        style.configure(
            "Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground=palette["text"],
            rowheight=29,
            bordercolor=palette["line"],
        )
        style.configure(
            "Treeview.Heading",
            background="#F8FAFC",
            foreground="#475467",
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(6, 7),
        )
        style.map(
            "Treeview",
            background=[("selected", "#DBEAFE")],
            foreground=[("selected", "#1E3A8A")],
        )
        style.configure(
            "TNotebook",
            background=palette["page"],
            borderwidth=0,
        )
        style.configure(
            "TNotebook.Tab",
            padding=(18, 8),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#FFFFFF"), ("!selected", "#E9EEF5")],
            foreground=[("selected", palette["blue"]), ("!selected", "#667085")],
        )

        outer = tk.Frame(self.root, bg=palette["page"])
        outer.pack(fill=tk.BOTH, expand=True, padx=16, pady=(12, 10))

        header = tk.Frame(outer, bg=palette["page"])
        header.pack(fill=tk.X, pady=(0, 9))
        ttk.Label(
            header,
            text="豆包 MuMu 批量提问控制台",
            style="Title.TLabel",
        ).pack(side=tk.LEFT, anchor="w")
        tk.Label(
            header,
            text="手机端发送 · 网页端抓取 · 北京时间记录",
            bg=palette["soft_blue"],
            fg=palette["blue_dark"],
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=12,
            pady=6,
        ).pack(side=tk.RIGHT)

        identity = ttk.LabelFrame(
            outer,
            text=" 设备与账号 ",
            padding=(12, 8),
            style="Card.TLabelframe",
        )
        identity.pack(fill=tk.X, pady=(0, 9))
        identity.columnconfigure(1, weight=1)

        ttk.Label(identity, text="运行状态", style="Meta.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(identity, textvariable=self.status_var, style="Status.TLabel").grid(
            row=0, column=1, sticky="w", padx=(8, 20)
        )
        ttk.Label(identity, text="实例（空=全部）", style="Meta.TLabel").grid(
            row=0, column=2, sticky="e", padx=(12, 7)
        )
        ttk.Entry(identity, textvariable=self.device_index_var, width=12).grid(
            row=0, column=3, sticky="e"
        )
        self.probe_button = ttk.Button(
            identity,
            text="重新检测账号",
            command=self.probe_account,
        )
        self.probe_button.grid(
            row=0, column=4, sticky="e", padx=(10, 0)
        )

        self.account_mapping_tree = ttk.Treeview(
            identity,
            columns=("instance", "device", "mobile", "web", "state"),
            show="headings",
            height=3,
            selectmode="none",
        )
        for column, title, width, anchor in (
            ("instance", "MuMu 实例", 90, tk.CENTER),
            ("device", "设备地址", 190, tk.W),
            ("mobile", "MuMu 手机账号", 275, tk.W),
            ("web", "对应网页账号 / Chrome", 310, tk.W),
            ("state", "匹配状态", 145, tk.CENTER),
        ):
            self.account_mapping_tree.heading(column, text=title)
            self.account_mapping_tree.column(
                column,
                width=width,
                minwidth=70,
                anchor=anchor,
                stretch=column in {"device", "mobile", "web"},
            )
        self.account_mapping_tree.tag_configure("ready", foreground="#047857")
        self.account_mapping_tree.tag_configure("waiting", foreground="#9A6700")
        self.account_mapping_tree.tag_configure("error", foreground="#B42318")
        self.account_mapping_tree.grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="ew",
            pady=(8, 0),
        )
        self.account_mapping_scroll = ttk.Scrollbar(
            identity,
            orient=tk.VERTICAL,
            command=self.account_mapping_tree.yview,
        )
        self.account_mapping_tree.configure(
            yscrollcommand=self.account_mapping_scroll.set
        )
        self.account_mapping_scroll.grid(
            row=1,
            column=4,
            sticky="ns",
            pady=(8, 0),
        )

        self.readiness_label = tk.Label(
            identity,
            textvariable=self.readiness_var,
            anchor="w",
            justify=tk.LEFT,
            font=("Microsoft YaHei UI", 9, "bold"),
            fg="#9A6700",
            bg="#FFF8E6",
            padx=10,
            pady=7,
        )
        self.readiness_label.grid(
            row=2, column=0, columnspan=5, sticky="ew", pady=(8, 0)
        )

        footer = tk.Frame(
            outer,
            bg=palette["card"],
            highlightbackground=palette["line"],
            highlightthickness=1,
            padx=10,
            pady=9,
        )
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(9, 0))

        self.start_button = tk.Button(
            footer,
            text="▶  开始提问循环（等待准备完成）",
            command=self.start_now,
            state=tk.DISABLED,
            bg="#D1D5DB",
            fg="#6B7280",
            disabledforeground="#6B7280",
            activebackground="#D1D5DB",
            relief=tk.FLAT,
            cursor="hand2",
            padx=22,
            pady=9,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            footer,
            text="■ 停止任务",
            command=self.stop_current,
            state=tk.DISABLED,
            style="Danger.TButton",
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 16))

        ttk.Button(
            footer,
            text="保存配置",
            command=self.save_config_clicked,
        ).pack(side=tk.LEFT)
        ttk.Button(
            footer,
            text="定时任务",
            command=self.install_schedule,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            footer,
            text="定时状态",
            command=self.query_schedule,
        ).pack(side=tk.LEFT)
        ttk.Button(
            footer,
            text="删除定时",
            command=self.delete_schedule,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Button(
            footer,
            text="打开实时面板",
            command=lambda: os.startfile("http://127.0.0.1:8765/"),
            style="Accent.TButton",
        ).pack(side=tk.RIGHT)
        ttk.Button(
            footer,
            text="程序文件夹",
            command=self.open_folder,
        ).pack(side=tk.RIGHT, padx=(0, 6))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)
        question_tab = ttk.Frame(notebook, padding=10, style="Panel.TFrame")
        schedule_tab = ttk.Frame(notebook, padding=10, style="Panel.TFrame")
        brand_tab = ttk.Frame(notebook, padding=10, style="Panel.TFrame")
        notebook.add(question_tab, text="  问题与运行参数  ")
        notebook.add(brand_tab, text="  自有品牌与竞品  ")
        notebook.add(schedule_tab, text="  定时与运行日志  ")

        mode_row = ttk.Frame(question_tab, style="Panel.TFrame")
        mode_row.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(mode_row, text="执行顺序", style="Section.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Radiobutton(
            mode_row,
            text="交叉提问",
            variable=self.question_mode_var,
            value="interleaved",
        ).pack(side=tk.LEFT, padx=(14, 8))
        ttk.Radiobutton(
            mode_row,
            text="顺序提问",
            variable=self.question_mode_var,
            value="sequential",
        ).pack(side=tk.LEFT)
        self.total_label = ttk.Label(
            mode_row,
            text="0 个问题 / 0 轮",
            style="Status.TLabel",
        )
        self.total_label.pack(side=tk.RIGHT)

        tree_wrap = ttk.Frame(question_tab, style="Panel.TFrame")
        tree_wrap.pack(fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("number", "question", "repeat"),
            show="headings",
            selectmode="extended",
            height=6,
        )
        self.tree.heading("number", text="序号")
        self.tree.heading("question", text="问题内容")
        self.tree.heading("repeat", text="重复次数")
        self.tree.column("number", width=66, anchor="center", stretch=False)
        self.tree.column("question", width=820, anchor="w")
        self.tree.column("repeat", width=100, anchor="center", stretch=False)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        question_scroll = ttk.Scrollbar(
            tree_wrap, orient=tk.VERTICAL, command=self.tree.yview
        )
        question_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=question_scroll.set)
        self.tree.bind("<Double-1>", lambda _event: self.edit_question())

        question_buttons = ttk.Frame(question_tab, style="Panel.TFrame")
        question_buttons.pack(fill=tk.X, pady=(8, 8))
        for text, command in (
            ("＋ 添加问题", self.add_question),
            ("编辑", self.edit_question),
            ("删除", self.delete_questions),
            ("统一次数", self.set_selected_repeat),
            ("↑ 上移", lambda: self.move_selected(-1)),
            ("↓ 下移", lambda: self.move_selected(1)),
            ("从 TXT 导入", self.import_questions),
        ):
            ttk.Button(question_buttons, text=text, command=command).pack(
                side=tk.LEFT, padx=(0, 6)
            )

        runtime = ttk.LabelFrame(
            question_tab,
            text=" 运行参数 ",
            padding=(10, 7),
            style="Card.TLabelframe",
        )
        runtime.pack(fill=tk.X)
        runtime_fields = [
            ("最少等待秒", self.min_wait_var),
            ("稳定秒数", self.stable_var),
            ("回答超时秒", self.answer_timeout_var),
            ("网页同步超时秒", self.sync_timeout_var),
            ("异常重试秒", self.retry_delay_var),
            ("最小冷却秒", self.cooldown_min_var),
            ("最大冷却秒", self.cooldown_max_var),
            ("最大重试次数", self.max_retries_var),
        ]
        for index, (label, variable) in enumerate(runtime_fields):
            row = index // 4
            column = (index % 4) * 2
            ttk.Label(runtime, text=label, style="Meta.TLabel").grid(
                row=row, column=column, sticky="w", pady=4
            )
            ttk.Entry(runtime, textvariable=variable, width=10).grid(
                row=row,
                column=column + 1,
                sticky="w",
                padx=(7, 20),
                pady=4,
            )

        brand_intro = tk.Label(
            brand_tab,
            text=(
                "每行填写一个品牌；需要别名时使用“品牌名|别名1|别名2”。"
                "保存后，正文抓取器会重新识别品牌，实时面板会标注自有品牌、竞品及其命中信源。"
            ),
            bg="#EFF6FF",
            fg="#1D4ED8",
            anchor="w",
            justify=tk.LEFT,
            padx=10,
            pady=8,
        )
        brand_intro.pack(fill=tk.X, pady=(0, 9))
        brand_columns = ttk.Frame(brand_tab, style="Panel.TFrame")
        brand_columns.pack(fill=tk.BOTH, expand=True)
        brand_columns.columnconfigure(0, weight=1)
        brand_columns.columnconfigure(1, weight=1)
        owned_frame = ttk.LabelFrame(
            brand_columns,
            text=" 自有品牌 ",
            padding=8,
            style="Card.TLabelframe",
        )
        competitor_frame = ttk.LabelFrame(
            brand_columns,
            text=" 竞品品牌 ",
            padding=8,
            style="Card.TLabelframe",
        )
        owned_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        competitor_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self.owned_brand_text = tk.Text(
            owned_frame,
            height=13,
            wrap="none",
            font=("Microsoft YaHei UI", 10),
            relief=tk.FLAT,
            padx=8,
            pady=8,
        )
        self.competitor_brand_text = tk.Text(
            competitor_frame,
            height=13,
            wrap="none",
            font=("Microsoft YaHei UI", 10),
            relief=tk.FLAT,
            padx=8,
            pady=8,
        )
        self.owned_brand_text.pack(fill=tk.BOTH, expand=True)
        self.competitor_brand_text.pack(fill=tk.BOTH, expand=True)
        self.owned_brand_text.bind(
            "<KeyRelease>", lambda _event: self.schedule_autosave()
        )
        self.competitor_brand_text.bind(
            "<KeyRelease>", lambda _event: self.schedule_autosave()
        )
        brand_actions = ttk.Frame(brand_tab, style="Panel.TFrame")
        brand_actions.pack(fill=tk.X, pady=(9, 0))
        ttk.Button(
            brand_actions,
            text="保存品牌设置并刷新分析",
            command=self.save_brand_settings_clicked,
            style="Accent.TButton",
        ).pack(side=tk.LEFT)
        ttk.Label(
            brand_actions,
            text="品牌词库命中时不调用大模型；只有完全未命中时才调用模型兜底。",
            style="Meta.TLabel",
        ).pack(side=tk.LEFT, padx=(12, 0))

        schedule = ttk.LabelFrame(
            schedule_tab,
            text=" 整个任务定时（北京时间） ",
            padding=12,
            style="Card.TLabelframe",
        )
        schedule.pack(fill=tk.X)
        ttk.Label(schedule, text="模式", style="Meta.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Combobox(
            schedule,
            textvariable=self.schedule_mode_var,
            values=list(SCHEDULE_LABELS),
            state="readonly",
            width=16,
        ).grid(row=0, column=1, sticky="w", padx=(7, 22))
        ttk.Label(schedule, text="每天时间 HH:MM", style="Meta.TLabel").grid(
            row=0, column=2, sticky="w"
        )
        ttk.Entry(schedule, textvariable=self.daily_time_var, width=10).grid(
            row=0, column=3, sticky="w", padx=(7, 22)
        )
        ttk.Label(schedule, text="间隔分钟", style="Meta.TLabel").grid(
            row=0, column=4, sticky="w"
        )
        ttk.Entry(
            schedule, textvariable=self.interval_minutes_var, width=10
        ).grid(row=0, column=5, sticky="w", padx=(7, 22))
        ttk.Label(
            schedule,
            text="单次时间 YYYY-MM-DD HH:MM",
            style="Meta.TLabel",
        ).grid(row=0, column=6, sticky="w")
        ttk.Entry(
            schedule, textvariable=self.once_datetime_var, width=18
        ).grid(row=0, column=7, sticky="w", padx=(7, 0))
        tk.Label(
            schedule,
            text=(
                "定时由 Windows 任务计划执行。若上一批任务尚未结束，"
                "本次会自动跳过，不会抢占模拟器。"
            ),
            bg="#FFF8E6",
            fg="#9A6700",
            anchor="w",
            padx=9,
            pady=6,
        ).grid(row=1, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        tk.Label(
            schedule,
            textvariable=self.schedule_status_var,
            bg="#F8FAFC",
            fg="#334155",
            anchor="w",
            padx=9,
            pady=6,
        ).grid(row=2, column=0, columnspan=8, sticky="ew", pady=(8, 0))

        log_frame = ttk.LabelFrame(
            schedule_tab,
            text=" 运行日志 ",
            padding=7,
            style="Card.TLabelframe",
        )
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.log_text = tk.Text(
            log_frame,
            height=12,
            wrap="word",
            font=("Cascadia Mono", 9),
            bg="#0F172A",
            fg="#DCE7F7",
            insertbackground="#FFFFFF",
            selectbackground="#334155",
            relief=tk.FLAT,
            padx=10,
            pady=8,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll = ttk.Scrollbar(
            log_frame, orient=tk.VERTICAL, command=self.log_text.yview
        )
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def log(self, message: str) -> None:
        stamp = datetime.now(pipeline.BEIJING_TZ).strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"{stamp} {message}\n")
        self.log_text.see(tk.END)

    def set_readiness(
        self,
        ready: bool,
        message: str,
        *,
        state_key: str,
    ) -> None:
        self.readiness_ready = ready
        self.readiness_var.set(message)
        if ready:
            self.readiness_label.configure(fg="#047857", bg="#ecfdf5")
            if not (self.job_process and self.job_process.poll() is None):
                self.start_button.configure(
                    state=tk.NORMAL,
                    text="开始提问循环",
                    bg="#16a34a",
                    fg="white",
                    activebackground="#15803d",
                    activeforeground="white",
                )
        else:
            self.readiness_label.configure(fg="#9a3412", bg="#fff7ed")
            self.start_button.configure(
                state=tk.DISABLED,
                text="开始提问循环（等待准备完成）",
                bg="#d1d5db",
                fg="#6b7280",
                activebackground="#d1d5db",
            )
        if state_key != self.last_readiness_state:
            self.log(message)
            self.last_readiness_state = state_key

    def show_account_mappings(self, rows: list[dict[str, Any]]) -> None:
        tree = self.account_mapping_tree
        tree.delete(*tree.get_children())
        tree.configure(height=min(6, max(2, len(rows))))
        state_labels = {
            "ready": "账号一致",
            "waiting_manual_check": "等待手动检测",
            "mobile_not_logged_in": "手机端未登录",
            "web_not_logged_in": "网页未登录",
            "capture_not_ready": "抓取器未就绪",
            "account_mismatch": "账号不一致",
            "web_check_failed": "网页检测失败",
        }
        for item in rows:
            device = item.get("device") or {}
            state = str(item.get("state") or "waiting_manual_check")
            tag = (
                "ready"
                if state == "ready"
                else "error"
                if state in {
                    "mobile_not_logged_in",
                    "account_mismatch",
                    "web_check_failed",
                }
                else "waiting"
            )
            tree.insert(
                "",
                tk.END,
                values=(
                    f"实例 {item.get('index', '-')}",
                    str(device.get("serial") or device.get("name") or "未识别"),
                    str(item.get("mobile") or "等待手动检测"),
                    str(item.get("web") or "等待网页登录"),
                    state_labels.get(state, state or "等待检测"),
                ),
                tags=(tag,),
            )

    def prepare_browser_sessions(self) -> None:
        if self.probe_running:
            return
        if self.job_process and self.job_process.poll() is None:
            return
        self.probe_running = True
        self.probe_button.configure(state=tk.DISABLED)
        self.status_var.set("正在按 MuMu 数量打开网页")
        self.set_readiness(
            False,
            "正在识别已启动的 MuMu，并为每个实例打开独立 Chrome……",
            state_key="opening_browsers",
        )
        requested = self.device_index_var.get().strip() or None
        threading.Thread(
            target=self.prepare_browser_worker,
            args=(requested,),
            daemon=True,
        ).start()

    def prepare_browser_worker(self, requested: str | None) -> None:
        logger = logging.getLogger("doubao_mumu_panel_browser_prepare")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.NullHandler())
        try:
            devices = pipeline.discover_mumu_instances(logger, None)
            if requested:
                requested_indices = {
                    value.strip()
                    for value in re.split(r"[,，、\s]+", requested)
                    if value.strip()
                }
                devices = [
                    item for item in devices
                    if str(item["index"]) in requested_indices
                ]
                missing = requested_indices - {
                    str(item["index"]) for item in devices
                }
                if missing:
                    raise pipeline.PipelineError(
                        "未发现实例：" + "、".join(sorted(missing))
                    )

            browser_processes: dict[str, subprocess.Popen[Any]] = {}
            cdp_ports: dict[str, int] = {}
            opened: list[str] = []
            for device in devices:
                index = str(device["index"])
                preferred_port = pipeline.browser_port_for_slot(index)
                if (
                    preferred_port is not None
                    and pipeline.port_is_listening(preferred_port)
                    and pipeline.doubao_debug_port_ready(preferred_port)
                ):
                    port = preferred_port
                else:
                    launch_port = (
                        preferred_port
                        if preferred_port is not None
                        and not pipeline.port_is_listening(preferred_port)
                        else None
                    )
                    port, process = pipeline.launch_account_browser(
                        logger,
                        f"instance_{index}",
                        browser_slot=index,
                        preferred_port=launch_port,
                    )
                    browser_processes[index] = process
                cdp_ports[index] = int(port)
                opened.append(f"实例 {index} → Chrome CDP {port}")

            self.events.put(
                (
                    "browsers_prepared",
                    {
                        "ok": True,
                        "devices": devices,
                        "browser_processes": browser_processes,
                        "cdp_ports": cdp_ports,
                        "opened": opened,
                    },
                )
            )
        except Exception as exc:
            self.events.put(
                (
                    "browsers_prepared",
                    {
                        "ok": False,
                        "error": str(exc),
                        "devices": [],
                        "browser_processes": {},
                        "cdp_ports": {},
                    },
                )
            )

    def finish_browser_prepare(self, result: dict[str, Any]) -> None:
        self.probe_running = False
        running = self.job_process and self.job_process.poll() is None
        self.probe_button.configure(
            state=tk.DISABLED if running else tk.NORMAL
        )
        for index, process in (result.get("browser_processes") or {}).items():
            self.prepared_browser_processes[str(index)] = process
        for index, port in (result.get("cdp_ports") or {}).items():
            self.prepared_cdp_ports[str(index)] = int(port)

        if not result.get("ok"):
            self.device_var.set("未检测到已启动的 MuMu")
            self.mobile_account_var.set("等待手动检测")
            self.web_account_var.set("尚未打开")
            self.show_account_mappings([])
            self.status_var.set("浏览器准备失败")
            self.set_readiness(
                False,
                "无法按设备打开 Chrome：" + str(result.get("error") or "未知错误"),
                state_key="browser_prepare_failed",
            )
            return

        devices = result.get("devices") or []
        opened = result.get("opened") or []
        self.ready_instance_count = len(devices)
        self.device_var.set(
            f"{len(devices)} 台：" + "；".join(
                f"实例 {item['index']} {item['serial']}" for item in devices
            )
        )
        self.mobile_account_var.set("等待点击“重新检测账号”")
        self.web_account_var.set("；".join(opened))
        cdp_ports = result.get("cdp_ports") or {}
        self.show_account_mappings(
            [
                {
                    "index": str(item["index"]),
                    "device": item,
                    "mobile": "等待点击“重新检测账号”",
                    "web": (
                        f"Chrome 已打开 / CDP "
                        f"{cdp_ports.get(str(item['index']), '未就绪')}"
                    ),
                    "state": "waiting_manual_check",
                }
                for item in devices
            ]
        )
        self.status_var.set("等待网页登录和手动检测")
        self.set_readiness(
            False,
            (
                f"已为 {len(devices)} 个 MuMu 实例打开 {len(devices)} 个独立 Chrome。"
                "请分别登录对应账号，完成后点击“重新检测账号”。"
            ),
            state_key="waiting_manual_account_check",
        )

    def drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "probe_done":
                    self.finish_probe(payload)
                elif kind == "browsers_prepared":
                    self.finish_browser_prepare(payload)
                elif kind == "task_result":
                    title, code, output = payload
                    self.log(f"{title}：退出码={code}\n{output}".strip())
                    if code == 0:
                        messagebox.showinfo(title, "操作成功。")
                    else:
                        messagebox.showerror(title, output or f"退出码 {code}")
                    self.refresh_schedule_status(force=True)
                elif kind == "schedule_status":
                    self.schedule_probe_running = False
                    self.schedule_status_var.set(str(payload))
        except queue.Empty:
            pass
        self.root.after(200, self.drain_events)

    def tree_items(self) -> list[dict[str, Any]]:
        result = []
        for item_id in self.tree.get_children():
            values = self.tree.item(item_id, "values")
            result.append({"text": str(values[1]).strip(), "repeat": int(values[2])})
        return result

    def refill_tree(self, items: list[dict[str, Any]]) -> None:
        self.tree.delete(*self.tree.get_children())
        for item in items:
            self.tree.insert(
                "",
                tk.END,
                values=("", str(item["text"]), int(item["repeat"])),
            )
        self.renumber()

    def renumber(self) -> None:
        total = 0
        children = self.tree.get_children()
        for index, item_id in enumerate(children, 1):
            values = list(self.tree.item(item_id, "values"))
            values[0] = index
            total += int(values[2])
            self.tree.item(item_id, values=values)
        self.total_label.configure(
            text=f"{len(children)} 个问题 / 每实例 {total} 轮"
        )

    def add_question(self) -> None:
        dialog = QuestionDialog(self.root, title="添加问题")
        self.root.wait_window(dialog)
        if dialog.result:
            question, repeat = dialog.result
            self.tree.insert("", tk.END, values=("", question, repeat))
            self.renumber()
            self.schedule_autosave()

    def edit_question(self) -> None:
        selected = self.tree.selection()
        if len(selected) != 1:
            messagebox.showinfo("选择问题", "请选择一行进行编辑。")
            return
        item_id = selected[0]
        values = self.tree.item(item_id, "values")
        dialog = QuestionDialog(
            self.root,
            title="编辑问题",
            question=str(values[1]),
            repeat=int(values[2]),
        )
        self.root.wait_window(dialog)
        if dialog.result:
            self.tree.item(
                item_id,
                values=(values[0], dialog.result[0], dialog.result[1]),
            )
            self.renumber()
            self.schedule_autosave()

    def delete_questions(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        if not messagebox.askyesno("删除问题", f"确定删除选中的 {len(selected)} 行吗？"):
            return
        for item_id in selected:
            self.tree.delete(item_id)
        self.renumber()
        self.schedule_autosave()

    def set_selected_repeat(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("选择问题", "请先选择一行或多行。")
            return
        repeat = simpledialog.askinteger(
            "统一设置次数",
            "这些问题各重复多少次？",
            parent=self.root,
            minvalue=1,
            maxvalue=100000,
        )
        if repeat is None:
            return
        for item_id in selected:
            values = list(self.tree.item(item_id, "values"))
            values[2] = repeat
            self.tree.item(item_id, values=values)
        self.renumber()
        self.schedule_autosave()

    def move_selected(self, direction: int) -> None:
        selected = self.tree.selection()
        if len(selected) != 1:
            messagebox.showinfo("选择问题", "请选择一行移动。")
            return
        item_id = selected[0]
        children = list(self.tree.get_children())
        index = children.index(item_id)
        target = index + direction
        if target < 0 or target >= len(children):
            return
        self.tree.move(item_id, "", target)
        self.tree.selection_set(item_id)
        self.renumber()
        self.schedule_autosave()

    def import_questions(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self.root,
            title="导入问题列表",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not filename:
            return
        try:
            lines = [
                line.strip()
                for line in Path(filename).read_text(encoding="utf-8-sig").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        repeat = simpledialog.askinteger(
            "默认次数",
            "导入的每个问题默认重复多少次？",
            parent=self.root,
            initialvalue=10,
            minvalue=1,
            maxvalue=100000,
        )
        if repeat is None:
            return
        for line in lines:
            self.tree.insert("", tk.END, values=("", line, repeat))
        self.renumber()
        self.schedule_autosave()
        self.log(f"从 {filename} 导入 {len(lines)} 个问题。")

    def number_value(
        self,
        variable: tk.StringVar,
        name: str,
        *,
        integer: bool = False,
        minimum: float = 0,
    ) -> int | float:
        text = variable.get().strip()
        try:
            value: int | float = int(text) if integer else float(text)
        except ValueError as exc:
            raise ValueError(f"{name} 必须是数字。") from exc
        if value < minimum:
            raise ValueError(f"{name} 不能小于 {minimum:g}。")
        return value

    def collect_config(
        self,
        *,
        validate_schedule: bool = True,
    ) -> dict[str, Any]:
        questions = self.tree_items()
        if not questions:
            raise ValueError("至少添加一个问题。")
        cooldown_min = self.number_value(
            self.cooldown_min_var,
            "轮间最小冷却秒",
        )
        cooldown_max = self.number_value(
            self.cooldown_max_var,
            "轮间最大冷却秒",
        )
        if cooldown_max < cooldown_min:
            raise ValueError("轮间最大冷却秒不能小于最小冷却秒。")
        schedule_mode = SCHEDULE_LABELS.get(self.schedule_mode_var.get(), "none")
        config = {
            "version": 2,
            "device_index": self.device_index_var.get().strip(),
            "question_mode": self.question_mode_var.get().strip(),
            "questions": questions,
            "runtime": {
                "min_wait": self.number_value(self.min_wait_var, "最少等待秒"),
                "stable_seconds": self.number_value(
                    self.stable_var,
                    "稳定秒数",
                    minimum=1,
                ),
                "answer_timeout": self.number_value(
                    self.answer_timeout_var,
                    "回答超时秒",
                    minimum=1,
                ),
                "sync_timeout": self.number_value(
                    self.sync_timeout_var,
                    "网页同步超时秒",
                    minimum=1,
                ),
                "retry_delay": self.number_value(self.retry_delay_var, "异常重试秒"),
                "cooldown_min": cooldown_min,
                "cooldown_max": cooldown_max,
                "max_round_retries": self.number_value(
                    self.max_retries_var,
                    "最大重试次数",
                    integer=True,
                ),
                "verbose": False,
            },
            "schedule": {
                "mode": schedule_mode,
                "daily_time": self.daily_time_var.get().strip(),
                "interval_minutes": self.number_value(
                    self.interval_minutes_var,
                    "间隔分钟",
                    integer=True,
                    minimum=1,
                ),
                "once_datetime": self.once_datetime_var.get().strip(),
            },
        }
        if validate_schedule:
            self.validate_schedule(config["schedule"])
        return config

    def validate_schedule(self, schedule: dict[str, Any]) -> None:
        mode = schedule["mode"]
        if mode == "daily":
            try:
                datetime.strptime(str(schedule["daily_time"]), "%H:%M")
            except ValueError as exc:
                raise ValueError("每天时间必须使用 HH:MM，例如 09:30。") from exc
        elif mode == "interval":
            minutes = int(schedule["interval_minutes"])
            if minutes < 1 or minutes > 1439:
                raise ValueError("间隔分钟必须在 1 到 1439 之间。")
        elif mode == "once":
            try:
                value = datetime.strptime(
                    str(schedule["once_datetime"]),
                    "%Y-%m-%d %H:%M",
                )
            except ValueError as exc:
                raise ValueError(
                    "单次时间必须使用 YYYY-MM-DD HH:MM。"
                ) from exc
            if value <= datetime.now(pipeline.BEIJING_TZ).replace(tzinfo=None):
                raise ValueError("单次运行时间必须晚于现在。")

    def write_config(self, config: dict[str, Any]) -> None:
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(CONFIG_PATH)

    def current_brand_settings(self) -> dict[str, Any]:
        return {
            "version": 1,
            "owned_brands": brand_settings.parse_editor_text(
                self.owned_brand_text.get("1.0", tk.END)
            ),
            "competitor_brands": brand_settings.parse_editor_text(
                self.competitor_brand_text.get("1.0", tk.END)
            ),
        }

    def load_brand_settings(self) -> None:
        settings = brand_settings.load_settings()
        self.owned_brand_text.delete("1.0", tk.END)
        self.owned_brand_text.insert(
            "1.0",
            brand_settings.editor_text(settings.get("owned_brands")),
        )
        self.competitor_brand_text.delete("1.0", tk.END)
        self.competitor_brand_text.insert(
            "1.0",
            brand_settings.editor_text(settings.get("competitor_brands")),
        )

    def save_brand_settings(self) -> dict[str, Any]:
        return brand_settings.save_settings(self.current_brand_settings())

    def save_brand_settings_clicked(self) -> None:
        try:
            saved = self.save_brand_settings()
            self.write_config(self.collect_config(validate_schedule=False))
        except Exception as exc:
            messagebox.showerror("品牌设置错误", str(exc))
            return
        self.log(
            "品牌设置已保存："
            f"自有品牌 {len(saved['owned_brands'])} 个，"
            f"竞品 {len(saved['competitor_brands'])} 个；"
            "后台正文分析会自动刷新。"
        )
        messagebox.showinfo("品牌设置", "品牌设置已保存并进入后台刷新队列。")

    def install_autosave(self) -> None:
        variables = (
            self.device_index_var,
            self.question_mode_var,
            self.min_wait_var,
            self.stable_var,
            self.answer_timeout_var,
            self.sync_timeout_var,
            self.retry_delay_var,
            self.cooldown_min_var,
            self.cooldown_max_var,
            self.max_retries_var,
            self.schedule_mode_var,
            self.daily_time_var,
            self.interval_minutes_var,
            self.once_datetime_var,
        )
        for variable in variables:
            variable.trace_add(
                "write",
                lambda *_args: self.schedule_autosave(),
            )
        self.owned_brand_text.bind(
            "<KeyRelease>",
            lambda _event: self.schedule_autosave(),
            add="+",
        )
        self.competitor_brand_text.bind(
            "<KeyRelease>",
            lambda _event: self.schedule_autosave(),
            add="+",
        )

    def schedule_autosave(self) -> None:
        if self.autosave_job is not None:
            try:
                self.root.after_cancel(self.autosave_job)
            except Exception:
                pass
        self.autosave_job = self.root.after(700, self.autosave_now)

    def autosave_now(self) -> None:
        self.autosave_job = None
        try:
            self.write_config(self.collect_config(validate_schedule=False))
            self.save_brand_settings()
        except Exception:
            # Keep the last valid saved configuration while a numeric field is
            # temporarily empty during editing.  The next valid keystroke saves.
            return

    def save_config_clicked(self) -> None:
        try:
            config = self.collect_config(validate_schedule=False)
            self.write_config(config)
            self.save_brand_settings()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.log(f"配置已保存：{CONFIG_PATH.name}")
        messagebox.showinfo("保存配置", "配置已保存。")

    def load_config(self) -> None:
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            self.log(f"配置读取失败，将使用界面默认值：{exc}")
            return
        self.device_index_var.set(str(config.get("device_index") or ""))
        self.question_mode_var.set(
            str(config.get("question_mode") or "interleaved")
        )
        self.refill_tree(config.get("questions") or [])
        runtime = config.get("runtime") or {}
        field_map = [
            (self.min_wait_var, "min_wait", 8),
            (self.stable_var, "stable_seconds", 5),
            (self.answer_timeout_var, "answer_timeout", 240),
            (self.sync_timeout_var, "sync_timeout", 300),
            (self.retry_delay_var, "retry_delay", 5),
            (self.cooldown_min_var, "cooldown_min", 10),
            (self.cooldown_max_var, "cooldown_max", 20),
            (self.max_retries_var, "max_round_retries", 0),
        ]
        for variable, key, default in field_map:
            variable.set(str(runtime.get(key, default)))
        schedule = config.get("schedule") or {}
        mode = str(schedule.get("mode") or "none")
        self.schedule_mode_var.set(
            SCHEDULE_LABELS_REVERSE.get(mode, "不启用定时")
        )
        self.daily_time_var.set(str(schedule.get("daily_time") or "09:00"))
        self.interval_minutes_var.set(
            str(schedule.get("interval_minutes") or 60)
        )
        self.once_datetime_var.set(
            str(
                schedule.get("once_datetime")
                or datetime.now(pipeline.BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
            )
        )
        self.load_brand_settings()
        self.log(f"已加载配置：{CONFIG_PATH.name}")

    def probe_account(self) -> None:
        self.begin_probe(manual=True)

    def begin_probe(self, *, manual: bool) -> None:
        if self.probe_running:
            return
        if self.job_process and self.job_process.poll() is None:
            if manual:
                messagebox.showwarning(
                    "任务运行中",
                    "请先停止当前任务，再检测账号。",
                )
            return
        self.probe_running = True
        self.probe_button.configure(state=tk.DISABLED)
        if manual:
            self.status_var.set("正在识别设备与账号")
        requested = self.device_index_var.get().strip() or None
        threading.Thread(
            target=self.probe_worker,
            args=(requested, manual),
            daemon=True,
        ).start()

    def probe_worker(self, requested: str | None, manual: bool) -> None:
        logger = logging.getLogger("doubao_mumu_panel_probe")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        if manual:
            handler = QueueLogHandler(self.events)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        else:
            logger.addHandler(logging.NullHandler())
        try:
            devices = pipeline.discover_mumu_instances(logger, None)
            if requested:
                requested_indices = {
                    value.strip()
                    for value in re.split(r"[,，;；\s]+", requested)
                    if value.strip()
                }
                devices = [
                    item for item in devices
                    if str(item["index"]) in requested_indices
                ]
                missing = requested_indices - {
                    str(item["index"]) for item in devices
                }
                if missing:
                    raise pipeline.PipelineError(
                        "未发现实例：" + "、".join(sorted(missing))
                    )
            adb = pipeline.resolve_adb()
        except Exception as exc:
            result = {
                "ok": False,
                "manual": manual,
                "state": "mumu_not_ready",
                "device": "未检测到已启动的 MuMu",
                "mobile": "MuMu 端未就绪",
                "web": "等待 MuMu 端",
                "message": (
                    "MuMu 端未就绪：请启动 MuMu 模拟器，打开豆包 App。"
                ),
                "error": str(exc),
            }
            self.events.put(("probe_done", result))
            return

        try:
            grabber = pipeline.import_grabber()
        except Exception as exc:
            self.events.put(
                (
                    "probe_done",
                    {
                        "ok": False,
                        "manual": manual,
                        "state": "grabber_not_ready",
                        "device": f"已发现 {len(devices)} 台 MuMu",
                        "mobile": "等待网页抓取模块",
                        "web": "抓取模块加载失败",
                        "message": "网页抓取模块加载失败，程序会自动重试。",
                        "error": str(exc),
                    },
                )
            )
            return
        instance_results: list[dict[str, Any]] = []
        browser_processes: dict[str, subprocess.Popen[Any]] = {}
        cdp_ports: dict[str, int] = {}
        for device in devices:
            index = str(device["index"])
            serial = str(device["serial"])
            cached = self.cached_mobile_accounts.get(serial)
            cache_fresh = bool(
                not manual
                and cached
                and time.monotonic() - cached[1] < 60
            )
            if cache_fresh:
                account = dict(cached[0])
            else:
                probe_device_lock = pipeline.DeviceLock(logger, serial)
                try:
                    probe_device_lock.acquire()
                    account = pipeline.read_mobile_account(logger, adb, serial)
                    self.cached_mobile_accounts[serial] = (
                        dict(account),
                        time.monotonic(),
                    )
                except Exception as exc:
                    self.cached_mobile_accounts.pop(serial, None)
                    instance_results.append(
                        {
                            "index": index,
                            "device": device,
                            "ready": False,
                            "state": "mobile_not_logged_in",
                            "mobile": "未登录",
                            "web": "等待 MuMu 端登录",
                            "message": (
                                f"实例 {index} 的 MuMu 端未登录："
                                "请在该模拟器的豆包 App 登录账号。"
                            ),
                            "error": str(exc),
                        }
                    )
                    continue
                finally:
                    probe_device_lock.release()
            try:
                preferred_port = pipeline.browser_port_for_slot(index)
                browser = pipeline.find_matching_browser(
                    grabber,
                    account["uid"],
                    preferred_port=preferred_port,
                    require_capture_ready=False,
                )
                browser_process = None
                identity: dict[str, Any]
                if browser:
                    identity = browser
                    matched_port = int(identity.get("port") or 0)
                    if matched_port:
                        pipeline.remember_browser_port(index, matched_port)
                        preferred_port = matched_port
                elif (
                    preferred_port is not None
                    and pipeline.port_is_listening(preferred_port)
                ):
                    identity = pipeline.web_identity(
                        grabber,
                        preferred_port,
                    )
                else:
                    port, browser_process = pipeline.launch_account_browser(
                        logger,
                        account["uid"],
                        browser_slot=index,
                        preferred_port=preferred_port,
                    )
                    identity = {
                        "uid": "",
                        "loggedIn": False,
                        "captureReady": False,
                        "port": port,
                    }
                if browser_process is not None:
                    browser_processes[index] = browser_process
                port = int(identity.get("port") or preferred_port or 0)
                if port:
                    cdp_ports[index] = port
                if (
                    identity.get("loggedIn")
                    and str(identity.get("uid") or "") == account["uid"]
                    and identity.get("captureReady")
                ):
                    state = "ready"
                    ready = True
                    web_text = (
                        f"同 UID {pipeline.mask_uid(account['uid'])} / CDP {port}"
                    )
                    message = f"实例 {index} 的手机端与网页端账号一致。"
                elif identity.get("detectionError"):
                    state = "web_check_failed"
                    ready = False
                    web_text = f"网页暂不可读 / CDP {port}"
                    message = f"实例 {index} 的网页正在刷新，程序会自动重试。"
                elif not identity.get("loggedIn") or not identity.get("uid"):
                    state = "web_not_logged_in"
                    ready = False
                    web_text = f"未登录 / CDP {port}"
                    message = (
                        f"实例 {index} 的网页端未登录：请在对应调试 Chrome "
                        f"登录 MuMu UID {pipeline.mask_uid(account['uid'])}。"
                    )
                elif (
                    str(identity.get("uid") or "") == account["uid"]
                    and not identity.get("captureReady")
                ):
                    state = "capture_not_ready"
                    ready = False
                    web_text = f"账号匹配，抓取器未就绪 / CDP {port}"
                    message = f"实例 {index} 的网页抓取器正在自动注入。"
                else:
                    state = "account_mismatch"
                    ready = False
                    web_text = (
                        f"错号 {pipeline.mask_uid(str(identity.get('uid') or ''))}"
                        f" / CDP {port}"
                    )
                    message = (
                        f"实例 {index} 网页账号不一致：请切换为 MuMu UID "
                        f"{pipeline.mask_uid(account['uid'])}。"
                    )
                instance_results.append(
                    {
                        "index": index,
                        "device": device,
                        "account": account,
                        "ready": ready,
                        "state": state,
                        "mobile": (
                            f"UID {pipeline.mask_uid(account['uid'])} / "
                            f"{account['screen_name'] or '未设置昵称'}"
                        ),
                        "web": web_text,
                        "message": message,
                        "cdp_port": port,
                    }
                )
            except Exception as exc:
                logger.exception("实例 %s 网页端检测异常", index)
                instance_results.append(
                    {
                        "index": index,
                        "device": device,
                        "account": account,
                        "ready": False,
                        "state": "web_check_failed",
                        "mobile": (
                            f"UID {pipeline.mask_uid(account['uid'])} / "
                            f"{account['screen_name'] or '未设置昵称'}"
                        ),
                        "web": "网页检测失败",
                        "message": f"实例 {index} 网页检测失败，程序将自动重试。",
                        "error": str(exc),
                    }
                )

        ready = bool(instance_results) and all(
            item.get("ready") for item in instance_results
        )
        not_ready_messages = [
            str(item["message"])
            for item in instance_results
            if not item.get("ready")
        ]
        result = {
            "ok": True,
            "manual": manual,
            "state": (
                "ready"
                if ready
                else "multi_waiting_"
                + "_".join(
                    str(item["index"])
                    for item in instance_results
                    if not item.get("ready")
                )
            ),
            "ready": ready,
            "device": (
                f"{len(instance_results)} 台："
                + "；".join(
                    f"实例 {item['index']} {item['device']['serial']}"
                    for item in instance_results
                )
            ),
            "mobile": "；".join(
                f"实例 {item['index']} {item['mobile']}"
                for item in instance_results
            ),
            "web": "；".join(
                f"实例 {item['index']} {item['web']}"
                for item in instance_results
            ),
            "message": (
                f"全部 {len(instance_results)} 个实例和独立网页账号准备完成，"
                "可以并行开始提问。"
                if ready
                else "尚未全部准备完成：" + " ".join(not_ready_messages)
            ),
            "instances": instance_results,
            "browser_processes": browser_processes,
            "cdp_ports": cdp_ports,
        }
        self.events.put(("probe_done", result))

    def finish_probe(self, result: dict[str, Any]) -> None:
        self.probe_running = False
        self.ready_instance_count = len(result.get("instances") or [])
        running = self.job_process and self.job_process.poll() is None
        self.probe_button.configure(
            state=tk.DISABLED if running else tk.NORMAL
        )
        self.device_var.set(str(result.get("device") or "未检测"))
        self.mobile_account_var.set(
            str(result.get("mobile") or "未检测")
        )
        self.web_account_var.set(str(result.get("web") or "未检测"))
        self.show_account_mappings(list(result.get("instances") or []))
        for index, process in (result.get("browser_processes") or {}).items():
            self.prepared_browser_processes[str(index)] = process
        for index, port in (result.get("cdp_ports") or {}).items():
            self.prepared_cdp_ports[str(index)] = int(port)
        self.set_readiness(
            bool(result.get("ready")),
            str(result.get("message") or "准备状态未知。"),
            state_key=str(result.get("state") or "unknown"),
        )
        try:
            snapshot = {
                "checked_at": pipeline.beijing_now(),
                "ready": bool(result.get("ready")),
                "state": str(result.get("state") or "unknown"),
                "message": str(result.get("message") or ""),
                "device": str(result.get("device") or ""),
                "mobile": str(result.get("mobile") or ""),
                "web": str(result.get("web") or ""),
                "error": str(result.get("error") or ""),
                "instances": result.get("instances") or [],
            }
            temporary = READINESS_PATH.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(READINESS_PATH)
        except Exception:
            pass
        if result.get("ok"):
            self.status_var.set(
                "全部准备完成"
                if result.get("ready")
                else "等待登录或账号匹配"
            )
        else:
            self.status_var.set("准备条件未完成")
            if result.get("manual"):
                self.log(
                    "检测详情："
                    + str(result.get("error") or "未知错误")
                )
        if (
            bool(result.get("ready"))
            and self.auto_start_on_ready
            and not (self.job_process and self.job_process.poll() is None)
        ):
            self.auto_start_on_ready = False
            self.log("修复完成，自动恢复刚才未发送的提问任务。")
            self.root.after(300, self.start_now)

    def start_now(self) -> None:
        if self.job_process and self.job_process.poll() is None:
            messagebox.showinfo("正在运行", "当前整批任务已经在运行。")
            return
        if not self.readiness_ready:
            messagebox.showwarning(
                "尚未准备完成",
                self.readiness_var.get()
                + "\n\nMuMu 端和网页端都登录且数字 UID 完全一致后，"
                "按钮才会变为绿色。",
            )
            return
        try:
            from doubao_remote_startup import environment_check

            environment_check()
        except Exception as exc:
            self.status_var.set("运行环境未就绪")
            messagebox.showerror(
                "运行环境未就绪",
                f"{exc}\n\n请重新运行根目录的“一键部署并启动.bat”。",
            )
            self.log(f"任务未启动，运行环境自检失败：{exc}")
            return
        try:
            config = self.collect_config()
            self.write_config(config)
            self.save_brand_settings()
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.job_log_offset = JOB_LOG.stat().st_size if JOB_LOG.exists() else 0
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        if self.resume_completed_rounds:
            child_env["DOUBAO_SKIP_INITIAL_ROUNDS"] = str(
                self.resume_completed_rounds
            )
            self.log(
                f"恢复模式：前 {self.resume_completed_rounds} 轮已经保存，"
                "本次从下一轮继续。"
            )
            self.resume_completed_rounds = 0
        self.job_process = subprocess.Popen(
            [
                sys.executable,
                str(JOB_RUNNER),
                "--config",
                str(CONFIG_PATH),
            ],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=child_env,
            creationflags=CREATE_NO_WINDOW,
        )
        self.status_var.set("整批任务运行中")
        self.readiness_ready = False
        self.start_button.configure(
            state=tk.DISABLED,
            text="提问循环运行中",
            bg="#2563eb",
            fg="white",
        )
        self.stop_button.configure(state=tk.NORMAL)
        self.probe_button.configure(state=tk.DISABLED)
        self.log(
            f"已启动多实例整批任务：{self.ready_instance_count or '自动发现'} 台，"
            f"{len(config['questions'])} 个问题，"
            f"每台共 {sum(int(item['repeat']) for item in config['questions'])} 轮。"
        )

    def stop_current(self) -> None:
        process = self.job_process
        if process is None or process.poll() is not None:
            return
        if not messagebox.askyesno(
            "停止任务",
            "确定停止当前整批任务及其子进程吗？已保存成功的轮次不会丢失。",
        ):
            return
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
        )
        self.status_var.set("已人工停止")
        self.log("已停止当前任务进程树。")

    def refresh_process_status(self) -> None:
        if self.job_process is not None and self.job_process.poll() is not None:
            code = self.job_process.returncode
            self.status_var.set("任务完成" if code == 0 else f"任务结束：{code}")
            self.set_readiness(
                False,
                "任务已结束。如需再次运行，请点击“重新检测账号”核对两端登录状态。",
                state_key="waiting_manual_recheck_after_job",
            )
            self.stop_button.configure(state=tk.DISABLED)
            self.probe_button.configure(state=tk.NORMAL)
            self.job_process = None
        self.root.after(1000, self.refresh_process_status)

    def poll_job_log(self) -> None:
        try:
            if JOB_LOG.exists():
                size = JOB_LOG.stat().st_size
                if size < self.job_log_offset:
                    self.job_log_offset = 0
                if size > self.job_log_offset:
                    with JOB_LOG.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(self.job_log_offset)
                        chunk = handle.read()
                        self.job_log_offset = handle.tell()
                    for line in chunk.splitlines():
                        self.log_text.insert(tk.END, line + "\n")
                    self.log_text.see(tk.END)
        except Exception:
            pass
        self.root.after(1000, self.poll_job_log)

    def task_command(self, config: dict[str, Any]) -> list[str]:
        schedule = config["schedule"]
        mode = schedule["mode"]
        command = [
            "schtasks",
            "/Create",
            "/TN",
            TASK_NAME,
            "/TR",
            f'"{SCHEDULE_BAT}"',
            "/F",
            "/RL",
            "LIMITED",
            "/RU",
            getpass.getuser(),
            "/IT",
        ]
        if mode == "daily":
            command += ["/SC", "DAILY", "/ST", str(schedule["daily_time"])]
        elif mode == "interval":
            command += [
                "/SC",
                "MINUTE",
                "/MO",
                str(schedule["interval_minutes"]),
            ]
        elif mode == "once":
            value = datetime.strptime(
                str(schedule["once_datetime"]),
                "%Y-%m-%d %H:%M",
            )
            command += [
                "/SC",
                "ONCE",
                "/SD",
                value.strftime("%m/%d/%Y"),
                "/ST",
                value.strftime("%H:%M"),
            ]
        else:
            raise ValueError("当前选择了“不启用定时”，无需安装。")
        return command

    def run_task_command(self, title: str, command: list[str]) -> None:
        def worker() -> None:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            output = (result.stdout or result.stderr).strip()
            self.events.put(
                ("task_result", (title, result.returncode, output))
            )

        threading.Thread(target=worker, daemon=True).start()

    def refresh_schedule_status(self, *, force: bool = False) -> None:
        if not self.schedule_probe_running:
            self.schedule_probe_running = True

            def worker() -> None:
                script = (
                    "$ErrorActionPreference='Stop';"
                    f"$t=Get-ScheduledTask -TaskName '{TASK_NAME}';"
                    "$i=$t|Get-ScheduledTaskInfo;"
                    "$next=if($i.NextRunTime -and $i.NextRunTime.Year -gt 2000)"
                    "{$i.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss')}else{'无'};"
                    "$last=if($i.LastRunTime -and $i.LastRunTime.Year -gt 2000)"
                    "{$i.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss')}else{'尚未运行'};"
                    "[pscustomobject]@{State=[string]$t.State;Next=$next;"
                    "Last=$last;Result=$i.LastTaskResult}|ConvertTo-Json -Compress"
                )
                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        script,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=CREATE_NO_WINDOW,
                )
                if result.returncode != 0:
                    text = "未安装 Windows 定时任务，因此没有下次运行；请先选择定时模式并点击“定时任务”。"
                else:
                    try:
                        data = json.loads(result.stdout.strip())
                        next_run = str(data.get("Next") or "无")
                        text = (
                            f"任务状态：{data.get('State') or '未知'}　"
                            f"下次运行：{next_run}　"
                            f"上次运行：{data.get('Last') or '尚未运行'}　"
                            f"结果码：{data.get('Result')}"
                        )
                        if next_run == "无":
                            text += "（单次任务已过期或任务已禁用，请重新安装定时）"
                    except Exception:
                        text = "定时任务状态读取失败，将稍后自动重试。"
                self.events.put(("schedule_status", text))

            threading.Thread(target=worker, daemon=True).start()
        if not force:
            self.root.after(30000, self.refresh_schedule_status)

    def install_schedule(self) -> None:
        try:
            config = self.collect_config()
            if config["schedule"]["mode"] == "none":
                raise ValueError("请先选择每天、间隔或单次定时模式。")
            self.write_config(config)
            self.save_brand_settings()
            command = self.task_command(config)
        except Exception as exc:
            messagebox.showerror("定时配置错误", str(exc))
            return
        self.log("正在安装/更新 Windows 定时任务。")
        self.run_task_command("安装/更新定时", command)

    def delete_schedule(self) -> None:
        if not messagebox.askyesno("删除定时", "确定删除这个 Windows 定时任务吗？"):
            return
        self.run_task_command(
            "删除定时",
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        )

    def query_schedule(self) -> None:
        self.run_task_command(
            "定时状态",
            ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
        )

    def open_folder(self) -> None:
        os.startfile(str(BASE_DIR))

    def on_close(self) -> None:
        if self.job_process and self.job_process.poll() is None:
            if not messagebox.askyesno(
                "任务仍在运行",
                "关闭面板不会停止后台任务。确定关闭面板吗？",
            ):
                return
        self.autosave_now()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    DoubaoMuMuControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
