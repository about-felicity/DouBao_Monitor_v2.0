from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Any

from monitor_core.plugins import ROOT, discover_plugins


MODELS = {
    "deepseek": "DeepSeek",
    "yuanbao": "腾讯元宝",
    "wenxin": "文心",
}
LOGIN_REQUIRED_MODELS = frozenset({"yuanbao", "wenxin"})


def account_gate_open(model: str, verified: bool) -> bool:
    return model not in LOGIN_REQUIRED_MODELS or verified


def build_worker_command(model: str, rounds: int, question_mode: str, pairing: str = "") -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "remote_model_worker.py"),
        "--model",
        model,
        "--rounds",
        str(max(1, rounds)),
        "--question-mode",
        question_mode,
    ]
    if pairing:
        command.extend(["--pairing", pairing])
    return command


class RemoteModelPanel:
    def __init__(self, model: str) -> None:
        self.model = model
        self.model_name = MODELS[model]
        self.process: subprocess.Popen[str] | None = None
        self.account_verified = False
        self.account_check_running = False
        self.messages: queue.Queue[str] = queue.Queue()
        self.config_path = ROOT / "runtime" / "remote_workers" / f"{model}_panel.json"
        self.settings = self.load_settings()
        self.root = tk.Tk()
        self.root.title(f"{self.model_name} 远端采集控制面板")
        self.root.geometry("900x650")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.status_var = tk.StringVar(value="未启动")
        self.account_var = tk.StringVar(value="首次启动将自动打开模拟器和专用 Chrome 检测登录")
        self.pairing_var = tk.StringVar(value=str(self.settings.get("pairing") or ""))
        embedded_sync = ROOT / "runtime" / "remote_workers" / f"{model}_sync.json"
        self.pairing_hint_var = tk.StringVar(
            value="已内置主机回传配置，无需选择" if embedded_sync.exists() else "首次启动请选择主机配对文件"
        )
        self.rounds_var = tk.StringVar(value=str(self.settings.get("rounds") or 10))
        self.mode_var = tk.StringVar(value=str(self.settings.get("question_mode") or "interleaved"))
        self.build_ui()
        self.root.after(200, self.refresh)
        if self.model in LOGIN_REQUIRED_MODELS:
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

        identity = ttk.LabelFrame(container, text="登录与账号检测", padding=12)
        identity.pack(fill="x", pady=(12, 0))
        ttk.Label(identity, textvariable=self.account_var).pack(side="left", fill="x", expand=True)
        self.account_button = ttk.Button(identity, text="打开并重新检测", command=self.check_account)
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
        self.status_var.set("正在检查账号")
        self.account_var.set("正在启动模拟器 App 和专用 Chrome，请在打开的窗口中完成登录……")
        self.account_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self.append_log("开始登录检测：请分别登录模拟器 App 与专用 Chrome；登录后点击“打开并重新检测”。")
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
            command = build_worker_command(self.model, self.rounds(), self.mode_var.get(), pairing)
            self.save_settings()
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
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
            self.status_var.set("运行中")
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
                self.status_var.set("登录检测通过")
                self.account_var.set("登录检测通过；启动采集时还会再次校验，防止中途退出账号")
                self.account_button.configure(state="normal")
                self.start_button.configure(state="normal")
            elif message == "__ACCOUNT_FAIL__":
                self.account_check_running = False
                self.account_verified = False
                self.status_var.set("账号检查失败")
                self.account_var.set("未登录或账号不一致；请完成两端登录后重新检测")
                self.account_button.configure(state="normal")
                self.start_button.configure(state="disabled")
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
    args = parser.parse_args()
    RemoteModelPanel(args.model).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
