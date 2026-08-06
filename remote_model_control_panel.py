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
        self.messages: queue.Queue[str] = queue.Queue()
        self.config_path = ROOT / "runtime" / "remote_workers" / f"{model}_panel.json"
        self.settings = self.load_settings()
        self.root = tk.Tk()
        self.root.title(f"{self.model_name} 远端采集控制面板")
        self.root.geometry("900x650")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.status_var = tk.StringVar(value="未启动")
        self.pairing_var = tk.StringVar(value=str(self.settings.get("pairing") or ""))
        self.rounds_var = tk.StringVar(value=str(self.settings.get("rounds") or 1000000))
        self.mode_var = tk.StringVar(value=str(self.settings.get("question_mode") or "interleaved"))
        self.build_ui()
        self.root.after(200, self.refresh)

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

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=12)
        ttk.Button(actions, text="账号检查", command=self.check_account).pack(side="left")
        ttk.Button(actions, text="打开问题文件", command=self.open_questions).pack(side="left", padx=8)
        self.start_button = ttk.Button(actions, text="启动采集", command=self.start)
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

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def check_account(self) -> None:
        self.status_var.set("正在检查账号")
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
                self.status_var.set("账号一致")
            elif message == "__ACCOUNT_FAIL__":
                self.status_var.set("账号检查失败")
            else:
                self.append_log(message)
        if self.process and self.process.poll() is not None:
            return_code = self.process.returncode
            self.status_var.set("已停止" if return_code == 0 else f"异常退出 {return_code}")
            self.start_button.configure(state="normal")
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
