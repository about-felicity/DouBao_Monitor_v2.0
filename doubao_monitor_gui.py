import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MONITOR_SCRIPT = os.path.join(BASE_DIR, "doubao_web_monitor_loop.py")
DASHBOARD_SCRIPT = os.path.join(BASE_DIR, "doubao_dashboard_server.py")
OPEN_CHROME_BAT = os.path.join(BASE_DIR, "open_chrome_debug.bat")
DATA_FILES = [
    os.path.join(BASE_DIR, "doubao_refs_result.csv"),
    os.path.join(BASE_DIR, "doubao_refs_result.xlsx"),
    os.path.join(BASE_DIR, "doubao_run_debug.log"),
]

DEFAULT_ROWS = [
    ("推荐一款染发剂", "30"),
    ("推荐一款温和染发剂", "30"),
    ("推荐一款遮白染发剂", "30"),
    ("梵玢染发剂怎么样", "30"),
    ("首迷染发剂怎么样", "30"),
]


class DoubaoMonitorGui:
    def __init__(self, root):
        self.root = root
        self.root.title("豆包网页循环监控启动器")
        self.root.geometry("1120x820")
        self.queue = queue.Queue()
        self.monitor_proc = None
        self.dashboard_proc = None
        self.plan_file = ""
        self.question_rows = []

        self.question_count = tk.StringVar(value="5")
        self.timeout = tk.StringVar(value="120")
        self.stable = tk.StringVar(value="6")
        self.cooldown_min = tk.StringVar(value="15")
        self.cooldown_max = tk.StringVar(value="25")
        self.mode = tk.StringVar(value="sequential")
        self.status = tk.StringVar(value="就绪")

        self.build()
        self.set_question_count(force_default=True)
        self.root.after(200, self.drain)
        self.root.after(1000, self.refresh_status)

    def build(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill=tk.BOTH, expand=True)

        ttk.Label(outer, text="豆包网页循环监控启动器", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="可以设置多个问题，每个问题单独设置轮数；支持顺序提问和交叉提问两种模式。",
            foreground="#566",
        ).pack(anchor="w", pady=(6, 8))
        ttk.Label(
            outer,
            text="说明：脚本会控制调试 Chrome 里的豆包网页。为了尽量降低风控，默认会保留短冷却，但已比之前明显提速。",
            foreground="#8b5e00",
        ).pack(anchor="w", pady=(0, 14))

        plan_controls = ttk.Frame(outer)
        plan_controls.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(plan_controls, text="问题数量").pack(side=tk.LEFT)
        ttk.Entry(plan_controls, textvariable=self.question_count, width=8).pack(side=tk.LEFT, padx=(8, 12))
        ttk.Button(plan_controls, text="更新问题行数", command=self.set_question_count).pack(side=tk.LEFT)
        ttk.Label(plan_controls, text="提问模式").pack(side=tk.LEFT, padx=(18, 8))
        ttk.Radiobutton(plan_controls, text="顺序提问", variable=self.mode, value="sequential").pack(side=tk.LEFT)
        ttk.Radiobutton(plan_controls, text="交叉提问", variable=self.mode, value="interleaved").pack(side=tk.LEFT, padx=(8, 0))

        question_frame = ttk.LabelFrame(outer, text="问题计划", padding=10)
        question_frame.pack(fill=tk.BOTH, expand=False)
        ttk.Label(
            question_frame,
            text="每行单独设置问题内容和轮数。交叉提问示例：问题1第1轮 -> 问题2第1轮 -> 问题3第1轮 -> 再回到问题1第2轮。",
            foreground="#566",
        ).pack(anchor="w", pady=(0, 8))

        header = ttk.Frame(question_frame)
        header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(header, text="序号", width=6).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="问题内容", width=72).grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="轮数", width=8).grid(row=0, column=2, sticky="w", padx=(12, 0))

        self.rows_frame = ttk.Frame(question_frame)
        self.rows_frame.pack(fill=tk.X)

        form = ttk.Frame(outer)
        form.pack(fill=tk.X, pady=(14, 8))
        fields = [
            ("单轮超时秒", self.timeout),
            ("稳定等待秒", self.stable),
            ("最小冷却秒", self.cooldown_min),
            ("最大冷却秒", self.cooldown_max),
        ]
        for i, (label, var) in enumerate(fields):
            ttk.Label(form, text=label).grid(row=0, column=i * 2, sticky="w", pady=7)
            ttk.Entry(form, textvariable=var, width=10).grid(row=0, column=i * 2 + 1, sticky="w", padx=(8, 20), pady=7)
        form.columnconfigure(len(fields) * 2, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill=tk.X, pady=(4, 12))
        ttk.Button(buttons, text="打开调试 Chrome", command=self.open_debug_chrome).pack(side=tk.LEFT)
        ttk.Button(buttons, text="启动豆包面板", command=self.start_dashboard).pack(side=tk.LEFT, padx=8)
        ttk.Button(buttons, text="打开豆包面板", command=lambda: webbrowser.open("http://127.0.0.1:8765/")).pack(side=tk.LEFT)
        ttk.Button(buttons, text="清空已抓数据", command=self.clear_data).pack(side=tk.LEFT, padx=(8, 0))
        self.start_btn = ttk.Button(buttons, text="开始循环监控", command=self.start_monitor)
        self.start_btn.pack(side=tk.LEFT, padx=(20, 8))
        self.stop_btn = ttk.Button(buttons, text="停止监控", command=self.stop_monitor, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT)
        ttk.Label(buttons, textvariable=self.status, foreground="#087f67").pack(side=tk.RIGHT)

        self.log_text = tk.Text(outer, font=("Consolas", 10), wrap="word")
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        self.queue.put(time.strftime("%H:%M:%S ") + str(message))

    def drain(self):
        try:
            while True:
                line = self.queue.get_nowait()
                self.log_text.insert(tk.END, line + "\n")
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        self.root.after(200, self.drain)

    def to_int(self, var, name):
        try:
            value = int(var.get().strip())
            if value <= 0:
                raise ValueError
            return value
        except Exception:
            raise ValueError(name + " 必须是正整数")

    def set_question_count(self, force_default=False):
        try:
            count = self.to_int(self.question_count, "问题数量")
        except Exception as exc:
            if not force_default:
                messagebox.showerror("参数错误", str(exc))
            return

        old_values = []
        for question_var, rounds_var in self.question_rows:
            old_values.append((question_var.get(), rounds_var.get()))

        for child in self.rows_frame.winfo_children():
            child.destroy()
        self.question_rows = []

        for index in range(count):
            q_default = DEFAULT_ROWS[index][0] if index < len(DEFAULT_ROWS) else ""
            r_default = DEFAULT_ROWS[index][1] if index < len(DEFAULT_ROWS) else "30"
            if index < len(old_values):
                q_default = old_values[index][0]
                r_default = old_values[index][1]

            question_var = tk.StringVar(value=q_default)
            rounds_var = tk.StringVar(value=r_default)
            ttk.Label(self.rows_frame, text=str(index + 1), width=6).grid(row=index, column=0, sticky="w", pady=4)
            ttk.Entry(self.rows_frame, textvariable=question_var, width=90).grid(row=index, column=1, sticky="ew", pady=4)
            ttk.Entry(self.rows_frame, textvariable=rounds_var, width=10).grid(row=index, column=2, sticky="w", padx=(12, 0), pady=4)
            self.question_rows.append((question_var, rounds_var))
        self.rows_frame.columnconfigure(1, weight=1)

    def get_plan_items(self):
        items = []
        for index, (question_var, rounds_var) in enumerate(self.question_rows, 1):
            question = question_var.get().strip()
            rounds_text = rounds_var.get().strip()
            if not question and not rounds_text:
                continue
            if not question:
                raise ValueError(f"第 {index} 行没有填写问题内容")
            try:
                rounds = int(rounds_text)
                if rounds <= 0:
                    raise ValueError
            except Exception:
                raise ValueError(f"第 {index} 行轮数必须是正整数")
            items.append({"question": question, "rounds": rounds})
        if not items:
            raise ValueError("至少填写 1 个有效问题")
        return items

    def write_plan_file(self, items):
        path = os.path.join(BASE_DIR, "doubao_questions_plan.json")
        payload = {"mode": self.mode.get().strip(), "items": items}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        self.plan_file = path
        return path

    def open_debug_chrome(self):
        if not os.path.exists(OPEN_CHROME_BAT):
            messagebox.showerror("文件不存在", OPEN_CHROME_BAT)
            return
        subprocess.Popen([OPEN_CHROME_BAT], cwd=BASE_DIR, shell=True)
        self.log("已尝试打开调试 Chrome，请确认豆包网页在该 Chrome 中登录并保持打开")

    def clear_data(self):
        if self.monitor_proc and self.monitor_proc.poll() is None:
            messagebox.showwarning("正在监控", "请先停止监控，再清空数据。")
            return
        if not messagebox.askyesno("确认清空", "确定删除豆包已抓取的数据表吗？此操作不可撤回。"):
            return
        removed = []
        for path in DATA_FILES:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed.append(os.path.basename(path))
            except Exception as exc:
                self.log("删除失败 %s: %r" % (path, exc))
        self.log("已清空豆包数据: " + (", ".join(removed) if removed else "没有可删除的数据文件"))

    def start_dashboard(self):
        if self.dashboard_proc and self.dashboard_proc.poll() is None:
            self.log("豆包面板服务已在运行")
            return
        self.dashboard_proc = subprocess.Popen(
            [sys.executable, DASHBOARD_SCRIPT],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self.pipe_output, args=(self.dashboard_proc, "dashboard"), daemon=True).start()
        self.log("豆包面板服务启动: http://127.0.0.1:8765/")

    def start_monitor(self):
        try:
            items = self.get_plan_items()
            timeout = self.to_int(self.timeout, "单轮超时秒")
            stable = self.to_int(self.stable, "稳定等待秒")
            cooldown_min = self.to_int(self.cooldown_min, "最小冷却秒")
            cooldown_max = self.to_int(self.cooldown_max, "最大冷却秒")
            if cooldown_max < cooldown_min:
                raise ValueError("最大冷却秒不能小于最小冷却秒")
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        if self.monitor_proc and self.monitor_proc.poll() is None:
            messagebox.showinfo("正在运行", "豆包监控已经在运行")
            return

        self.start_dashboard()
        plan_file = self.write_plan_file(items)
        total = sum(item["rounds"] for item in items)
        self.log(f"准备启动: {len(items)} 个问题，共 {total} 轮")
        self.log("提问模式: " + ("交叉提问" if self.mode.get() == "interleaved" else "顺序提问"))
        for item in items:
            self.log(f"计划项: {item['question']} x {item['rounds']}")
        self.log("计划文件: " + plan_file)

        self.monitor_proc = subprocess.Popen(
            [
                sys.executable,
                MONITOR_SCRIPT,
                "--plan-file", plan_file,
                "--timeout", str(timeout),
                "--stable-seconds", str(stable),
                "--cooldown-min", str(cooldown_min),
                "--cooldown-max", str(cooldown_max),
            ],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self.pipe_output, args=(self.monitor_proc, "monitor"), daemon=True).start()
        self.status.set("监控运行中")
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        webbrowser.open("http://127.0.0.1:8765/")

    def pipe_output(self, proc, name):
        try:
            for line in proc.stdout:
                self.log("[%s] %s" % (name, line.rstrip()))
        except Exception as exc:
            self.log("[%s] output error: %r" % (name, exc))

    def stop_monitor(self):
        if self.monitor_proc and self.monitor_proc.poll() is None:
            self.monitor_proc.terminate()
            self.log("已发送停止请求")
        self.status.set("已停止")
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    def refresh_status(self):
        if self.monitor_proc and self.monitor_proc.poll() is not None:
            self.start_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            self.status.set("监控已结束")
        self.root.after(1000, self.refresh_status)


def main():
    root = tk.Tk()
    DoubaoMonitorGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
