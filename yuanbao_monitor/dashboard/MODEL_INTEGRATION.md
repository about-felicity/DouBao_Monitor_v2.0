# 新大模型接入契约

统一面板已经为后续模型预留模型切换、对比卡片、运行状态和采集控制位置。

## 接入步骤

1. 在根目录 `doubao_dashboard_server.py` 的 `MODEL_REGISTRY` 注册模型元数据。
2. 为模型实现 `/api/models/<model-id>/stats` 数据适配器。
3. 如果支持采集控制，在 `_start_controlled_job` 中增加启动命令，并让状态返回 `running`、`ready`、`pid`、`started_at` 和 `log`。
4. 在 `app/modelRegistry.ts` 增加相同 ID 的前端定义，并为模型原始数据增加类型和适配逻辑。
5. 将 `RESERVED_MODEL_SLOTS` 调整为仍需保留的空位数量。

## 推荐的统一统计口径

新适配器至少应提供以下字段，保证能够进入综合总览和每日分析：

```json
{
  "generated_at": "2026-08-01T10:00:00+08:00",
  "total_runs": 0,
  "successful_runs": 0,
  "total_sources": 0,
  "unique_sources": 0,
  "question_count": 0,
  "device_count": 0,
  "daily": [
    {
      "date": "2026-08-01",
      "runs": 0,
      "sources": 0,
      "unique_sources": 0,
      "product_mentions": 0
    }
  ]
}
```

模型特有的回答、信源证据、产品排名和质量指标可以保留额外字段，由对应适配器展示。
