from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from monitor_core.plugins import ROOT, discover_plugins
from monitor_core.collector_guard import collector_guard_port


MODELS = {
    "deepseek": "DeepSeek",
    "yuanbao": "腾讯元宝",
    "wenxin": "文心",
}
LOGIN_REQUIRED_MODELS = frozenset({"yuanbao"})


def focus_existing_panel(model: str) -> bool:
    """Restore an existing model panel when its launcher is clicked again."""
    if os.name != "nt":
        return False
    expected_title = f"{MODELS[model]} 远端采集控制面板"
    found: list[int] = []
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value == expected_title:
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(callback_type(visit), 0)
    if not found:
        return False
    hwnd = found[0]
    user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    return True


def account_gate_open(model: str, verified: bool) -> bool:
    return model not in LOGIN_REQUIRED_MODELS or verified


def remote_collector_running(model: str) -> bool:
    """Return whether this model's collector guard is already owned."""
    guard = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        guard.bind(("127.0.0.1", collector_guard_port(model)))
        return False
    except OSError:
        return True
    finally:
        guard.close()


def console_python_executable() -> str:
    """Use console Python for workers so tracebacks reach the panel log."""
    executable = Path(sys.executable)
    if executable.name.casefold() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(executable)


def build_worker_command(
    model: str,
    rounds: int,
    question_mode: str,
    pairing: str = "",
    tasks: int = 1,
) -> list[str]:
    command = [
        console_python_executable(),
        str(ROOT / "remote_model_worker.py"),
        "--model",
        model,
        "--rounds",
        str(max(1, rounds)),
        "--tasks",
        str(max(1, min(int(tasks), 4))),
        "--question-mode",
        question_mode,
        "--restart-completed",
    ]
    if pairing:
        command.extend(["--pairing", pairing])
    return command


class RemoteModelPanel:
    def __init__(self, model: str, auto_start: bool = False) -> None:
        self.model = model
        self.model_name = MODELS[model]
        self.process: subprocess.Popen[str] | None = None
        self.account_verified = False
        self.account_check_running = False
        self.auto_start = auto_start
        self.external_worker_running = False
        self.messages: queue.Queue[str] = queue.Queue()
        self.config_path = ROOT / "runtime" / "remote_workers" / f"{model}_panel.json"
        self.settings = self.load_settings()
        self.root = tk.Tk()
        self.root.title(f"{self.model_name} 远端采集控制面板")
        self.root.geometry("900x650")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.status_var = tk.StringVar(value="未启动")
        account_hint = (
            "无需模拟器或登录；使用专用 Chrome 抓取百度搜索 AI 回答"
            if model == "wenxin"
            else "首次启动将自动打开模拟器和专用 Chrome 检测登录"
        )
        self.account_var = tk.StringVar(value=account_hint)
        embedded_sync = ROOT / "runtime" / "remote_workers" / f"{model}_sync.json"
        local_pairing = ROOT / "runtime" / "lan_result_pairing.json"
        default_pairing = str(self.settings.get("pairing") or "")
        if not default_pairing and not embedded_sync.exists() and local_pairing.exists():
            default_pairing = str(local_pairing)
        self.pairing_var = tk.StringVar(value=default_pairing)
        self.pairing_hint_var = tk.StringVar(
            value=(
                "已内置主机回传配置，无需选择"
                if embedded_sync.exists()
                else "已自动识别本机主机配对文件"
                if default_pairing
                else "首次启动请选择主机配对文件"
            )
        )
        self.rounds_var = tk.StringVar(value=str(self.settings.get("rounds") or 10))
        self.mode_var = tk.StringVar(value=str(self.settings.get("question_mode") or "interleaved"))
        self.tasks_var = tk.StringVar(value=str(self.settings.get("tasks") or 1))
        self.build_ui()
        self.root.after(200, self.refresh)
        if remote_collector_running(self.model):
            self.external_worker_running = True
            self.status_var.set("运行中（已有采集任务）")
            self.account_var.set(
                "本机已有其他采集器运行；仍可检测百度搜索，但暂不能启动文心采集"
                if self.model == "wenxin"
                else "已检测到本机采集器正在运行；无需重复点击启动"
            )
            self.start_button.configure(state="disabled")
            self.append_log("已识别正在运行的远端采集器；本窗口不会重复启动第二个任务。")
        elif self.model in LOGIN_REQUIRED_MODELS:
            self.root.after(800, self.check_account)
        elif self.auto_start:
            # Web-only collectors still need their browser check before an
            # automatic launch.  The successful check is what calls start().
            self.root.after(800, self.check_account)

    def load_settings(self) -> dict[str, Any]:
        try:
            value = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def save_settings(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "model": self.model,
                    "pairing": self.pairing_var.get().strip(),
                    "rounds": self.rounds(),
                    "tasks": self.tasks(),
                    "question_mode": self.mode_var.get(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.config_path)

    def rounds(self) -> int:
        try:
            return max(1, int(self.rounds_var.get().strip()))
        except ValueError:
            raise ValueError("轮数必须是大于 0 的整数")

    def tasks(self) -> int:
        if self.model != "wenxin":
            return 1
        try:
            value = int(self.tasks_var.get().strip())
        except ValueError as exc:
            raise ValueError("并发任务数必须是 1 到 4") from exc
        if value not in (1, 2, 3, 4):
            raise ValueError("并发任务数必须是 1 到 4")
        return value

    def build_ui(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        container = ttk.Frame(self.root, padding=18)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text=f"{self.model_name} 远端采集", style="Title.TLabel").pack(anchor="w")
        interval = "随机等待 120–300 秒，降低风控风险" if self.model == "deepseek" else "按模型安全节奏运行"
        ttk.Label(container, text=f"本电脑只运行 {self.model_name}；{interval}").pack(anchor="w", pady=(4, 14))

        settings = ttk.LabelFrame(container, text="运行设置", padding=12)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="主机配对文件").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(settings, textvariable=self.pairing_var).grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(settings, text="选择", command=self.choose_pairing).grid(row=0, column=2, padx=(8, 0), pady=5)
        ttk.Label(settings, textvariable=self.pairing_hint_var).grid(row=0, column=3, sticky="w", padx=(10, 0), pady=5)
        ttk.Label(settings, text="每个问题轮数").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Entry(settings, textvariable=self.rounds_var, width=18).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(settings, text="问题顺序").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
        ttk.Combobox(
            settings,
            textvariable=self.mode_var,
            values=("interleaved", "sequential"),
            state="readonly",
            width=18,
        ).grid(row=2, column=1, sticky="w", pady=5)
        if self.model == "wenxin":
            ttk.Label(settings, text="并发任务数（最多4个）").grid(
                row=3, column=0, sticky="w", padx=(0, 10), pady=5
            )
            ttk.Combobox(
                settings,
                textvariable=self.tasks_var,
                values=("1", "2", "3", "4"),
                state="readonly",
                width=18,
            ).grid(row=3, column=1, sticky="w", pady=5)

        identity_title = "百度搜索环境检测" if self.model == "wenxin" else "登录与账号检测"
        identity = ttk.LabelFrame(container, text=identity_title, padding=12)
        identity.pack(fill="x", pady=(12, 0))
        ttk.Label(identity, textvariable=self.account_var).pack(side="left", fill="x", expand=True)
        account_button_text = "打开并检测百度搜索" if self.model == "wenxin" else "打开并重新检测"
        self.account_button = ttk.Button(identity, text=account_button_text, command=self.check_account)
        self.account_button.pack(side="right")

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=12)
        ttk.Button(actions, text="打开问题文件", command=self.open_questions).pack(side="left", padx=8)
        self.start_button = ttk.Button(
            actions,
            text="启动采集",
            command=self.start,
            state="disabled" if self.model in LOGIN_REQUIRED_MODELS else "normal",
        )
        self.start_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="停止采集", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Label(actions, textvariable=self.status_var, style="Status.TLabel").pack(side="right")

        log_frame = ttk.LabelFrame(container, text="实时日志", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, wrap="word", font=("Consolas", 10), state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        self.log.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def choose_pairing(self) -> None:
        path = filedialog.askopenfilename(title="选择主机生成的 lan_result_pairing.json", filetypes=(("JSON", "*.json"),))
        if path:
            self.pairing_var.set(path)
            self.pairing_hint_var.set("已选择新的主机配对文件")

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def check_account(self) -> None:
        if self.account_check_running or (self.process and self.process.poll() is None):
            return
        self.account_check_running = True
        self.account_verified = False
        self.status_var.set("正在检查百度搜索" if self.model == "wenxin" else "正在检查账号")
        self.account_var.set(
            "正在启动专用 Chrome 并检测百度搜索页面……"
            if self.model == "wenxin"
            else "正在启动模拟器 App 和专用 Chrome，请在打开的窗口中完成登录……"
        )
        self.account_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.append_log(
            "开始检测百度搜索 AI 采集环境；无需模拟器或账号登录。"
            if self.model == "wenxin"
            else "开始登录检测：请分别登录模拟器 App 与专用 Chrome；登录后点击“打开并重新检测”。"
        )
        threading.Thread(target=self.account_worker, daemon=True).start()

    def account_worker(self) -> None:
        try:
            plugin = discover_plugins()[self.model]
            result = plugin.account_check()
            self.messages.put(json.dumps(result, ensure_ascii=False, indent=2))
            self.messages.put("__ACCOUNT_OK__" if result.get("ok") else "__ACCOUNT_FAIL__")
        except Exception as exc:
            self.messages.put(f"账号检查失败：{type(exc).__name__}: {exc}")
            self.messages.put("__ACCOUNT_FAIL__")

    def open_questions(self) -> None:
        plugin = discover_plugins()[self.model]
        path = getattr(plugin, "questions", None)
        if not path or not Path(path).exists():
            messagebox.showerror("错误", "找不到该模型的问题文件")
            return
        os.startfile(Path(path))

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if remote_collector_running(self.model):
            self.external_worker_running = True
            self.status_var.set("运行中（已有采集任务）")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="disabled")
            self.append_log("启动请求未重复执行：本机已有远端采集器正在运行。")
            return
        if not account_gate_open(self.model, self.account_verified):
            messagebox.showwarning(
                "请先完成登录检测",
                f"请先登录{self.model_name}模拟器 App 和专用 Chrome，然后点击“打开并重新检测”。",
            )
            self.check_account()
            return
        try:
            pairing = self.pairing_var.get().strip()
            sync_config = ROOT / "runtime" / "remote_workers" / f"{self.model}_sync.json"
            if not pairing and not sync_config.exists():
                raise ValueError("首次启动必须选择主机的 lan_result_pairing.json")
            command = build_worker_command(
                self.model,
                self.rounds(),
                self.mode_var.get(),
                pairing,
                tasks=self.tasks(),
            )
            self.save_settings()
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONWARNINGS"] = "ignore"
            self.process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            threading.Thread(target=self.read_process, daemon=True).start()
            self.status_var.set(f"运行中（{self.tasks()}个任务）" if self.model == "wenxin" else "运行中")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.append_log(f"启动命令：{' '.join(command)}")
        except Exception as exc:
            messagebox.showerror("无法启动", str(exc))

    def read_process(self) -> None:
        if not self.process or not self.process.stdout:
            return
        for line in self.process.stdout:
            self.messages.put(line)

    def stop(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        process_id = self.process.pid
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process_id), "/T", "/F"], capture_output=True, check=False)
        else:
            self.process.send_signal(signal.SIGTERM)
        self.append_log("已发送停止命令，正在保存当前进度。")

    def refresh(self) -> None:
        while True:
            try:
                message = self.messages.get_nowait()
            except queue.Empty:
                break
            if message == "__ACCOUNT_OK__":
                self.account_check_running = False
                self.account_verified = True
                self.status_var.set("百度搜索检测通过" if self.model == "wenxin" else "登录检测通过")
                self.account_var.set(
                    "百度搜索页面可用；正文和信源将直接从 AI 回答板块抓取"
                    if self.model == "wenxin"
                    else "登录检测通过；启动采集时还会再次校验，防止中途退出账号"
                )
                self.account_button.configure(state="normal")
                self.start_button.configure(state="disabled" if self.external_worker_running else "normal")
                if self.external_worker_running:
                    self.append_log("百度搜索检测已通过；本机另一个采集任务仍在运行，文心启动按钮保持禁用。")
                if self.auto_start and (not self.process or self.process.poll() is not None):
                    self.auto_start = False
                    self.root.after(100, self.start)
            elif message == "__ACCOUNT_FAIL__":
                self.account_check_running = False
                self.account_verified = False
                self.status_var.set("百度搜索检测失败" if self.model == "wenxin" else "账号检查失败")
                self.account_var.set(
                    "百度搜索页面不可用；请检查网络或页面风控后重试"
                    if self.model == "wenxin"
                    else "未登录或账号不一致；请完成两端登录后重新检测"
                )
                self.account_button.configure(state="normal")
                self.start_button.configure(state="disabled")
                if self.model == "wenxin":
                    messagebox.showwarning("百度搜索不可用", "请检查网络、百度搜索页面或风控提示后重新检测。")
                else:
                    messagebox.showwarning(
                        "请完成登录",
                        "模拟器 App 或专用 Chrome 尚未登录/账号不一致。\n\n"
                        "请逐个实例完成登录，然后点击“打开并重新检测”。",
                    )
            else:
                self.append_log(message)
        if self.process and self.process.poll() is not None:
            return_code = self.process.returncode
            self.status_var.set("已停止" if return_code == 0 else f"异常退出 {return_code}")
            self.start_button.configure(
                state="normal" if account_gate_open(self.model, self.account_verified) else "disabled"
            )
            self.stop_button.configure(state="disabled")
            self.process = None
        if self.external_worker_running and not remote_collector_running(self.model):
            self.external_worker_running = False
            self.status_var.set("已有采集任务已结束")
            if self.model in LOGIN_REQUIRED_MODELS:
                self.account_verified = False
                self.start_button.configure(state="disabled")
                self.root.after(100, self.check_account)
            else:
                self.start_button.configure(state="normal")
        self.root.after(200, self.refresh)

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("确认退出", "采集仍在运行，是否停止采集并关闭面板？"):
                return
            self.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=tuple(MODELS), required=True)
    parser.add_argument("--auto-start", action="store_true")
    args = parser.parse_args()
    if focus_existing_panel(args.model):
        return 0
    RemoteModelPanel(args.model, auto_start=args.auto_start).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
