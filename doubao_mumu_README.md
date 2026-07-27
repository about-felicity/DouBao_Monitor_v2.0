# MuMu 豆包循环控制

主脚本：`doubao_mumu_loop.py`

双击启动：`open_doubao_mumu_loop.bat`

默认行为：

- 自动发现 MuMu ADB 端口；
- 自动连接或启动 Appium；
- 每轮返回对话列表并创建一个全新对话；
- 输入、校验并发送问题；
- 等待回答文字和聊天区域稳定；
- 断连、会话失效、控件暂时消失、页面跳转或超时后自动恢复；
- 默认每轮无限重试，不会因为单次异常退出；
- 将每次成功和失败写入 JSONL，并保存进度断点；
- 发生异常时保存页面 XML、截图和错误说明。

## 常用命令

同一个问题循环 10 次：

```powershell
python .\doubao_mumu_loop.py --question "推荐一款染发剂" --rounds 10
```

无限循环：

```powershell
python .\doubao_mumu_loop.py --question "推荐一款染发剂" --forever
```

按文件逐条循环问题：

```powershell
python .\doubao_mumu_loop.py --questions-file .\questions.txt --rounds 100
```

`questions.txt` 使用 UTF-8 编码，一行一个问题。空行和以 `#` 开头的行会被忽略；问题数少于轮数时会从头循环。

从上次成功进度继续：

```powershell
python .\doubao_mumu_loop.py --question "推荐一款染发剂" --rounds 100 --resume
```

## 输出文件

- `doubao_mumu_loop.log`：滚动运行日志；
- `doubao_mumu_results.jsonl`：每轮结果和错误记录；
- `doubao_mumu_state.json`：断点状态；
- `doubao_mumu_diagnostics\`：异常时的 XML、截图和错误说明。

默认 `--max-round-retries 0` 表示某一轮无限重试。只有人工按 `Ctrl+C` 才会安全停止；停止前会保存当前进度。
