# GEO 多模型采集与分析平台

面向 DeepSeek、腾讯元宝和文心的采集、可靠回传、统一分析与实时数据面板。

## 项目结构

```text
monitor/
├─ collectors/                 # 三个模型的采集端，彼此隔离
│  ├─ deepseek/                # DeepSeek：Chrome 扩展与旧 App 采集器
│  ├─ yuanbao/                 # 腾讯元宝采集器
│  ├─ wenxin/                  # 文心/百度 AI 搜索采集器
│  └─ plugins/                 # 三模型统一注册入口
├─ dashboard/
│  ├─ backend/                 # 面板 API、统计接口和采集控制
│  ├─ frontend/                # React 实时面板
│  └─ static/                  # 面板静态工具页
├─ transport/                  # 数据回传、接收、离线队列和保留策略
├─ services/analysis/          # 产品、品牌和信源正文分析后台任务
├─ monitor_core/               # 三模型共享的数据库、质量规则和统计核心
├─ launchers/                  # 一键启动及运维入口
├─ deployment/                 # 部署、打包、依赖和防火墙配置
├─ tests/                      # Python 自动化测试
├─ tools/                      # 分析、修复、迁移和开发工具
├─ docs/                       # 部署与业务说明
├─ sample_data/                # 测试/演示数据，不参与实时统计
└─ legacy/                     # 已退出主链路的历史采集器
```

顶层只保留项目说明和 Git 配置。运行数据、密钥、浏览器登录目录、日志及缓存不会提交到 GitHub。

## 启动面板

在 Windows 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\launchers\start_unified_monitor.ps1
```

默认地址：

- 实时面板：<http://127.0.0.1:3000/>
- 数据 API：<http://127.0.0.1:8765/>
- 通用模型回传接收器：`8791`

常用双击入口都在 `launchers/` 中。

## 三模型边界

| 模型 | 采集实现 | 回传方式 |
| --- | --- | --- |
| DeepSeek | `collectors/deepseek/` | Chrome 扩展本地接收后通过通用回传 |
| 腾讯元宝 | `collectors/yuanbao/` | 通用离线队列与 `8791` 接收器 |
| 文心 | `collectors/wenxin/` | 通用离线队列与 `8791` 接收器 |

`collectors/plugins/<model>/plugin.py` 只负责注册模型元数据、问题文件、启动入口和统计数据位置，不混入其他模型的抓取逻辑。豆包、夸克和阿福的旧实现仅保留在 `legacy/`，不会出现在主面板的模型注册表中。

## 数据安全

- API Key、Cookie、配对令牌、Chrome 登录目录和真实采集结果均由 `.gitignore` 排除。
- 回传使用请求签名、请求 ID 去重和本地离线队列；网络中断不会删除未确认数据。
- 本地运行数据不会因 `git pull` 被覆盖。

## 测试

```powershell
python -m unittest discover -s tests -v
```

DeepSeek/夸克扩展的独立接收与同步测试分别位于对应采集目录的 `tests/` 中。

更多部署说明见 [`docs/`](docs/)。
