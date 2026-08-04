import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = new URL("../app/", import.meta.url);

test("统一面板由后端模型目录驱动并提供完整分析工作视图", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /\/api\/models/);
  assert.match(source, /\/api\/analytics/);
  assert.match(source, /模型总览/);
  assert.match(source, /问题对比/);
  assert.match(source, /信源洞察/);
  assert.match(source, /品牌与产品/);
  assert.match(source, /回答审计/);
  assert.match(source, /采集控制/);
  assert.doesNotMatch(source, /modelRegistry/);
});

test("采集控制包含问题计划、账号校验及启停能力", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /每行一个，启动前自动保存/);
  assert.match(source, /仅保存问题/);
  assert.match(source, /校验模拟器 \/ 网页账号/);
  assert.match(source, /account-check/);
  assert.match(source, /api\/control\/\$\{modelId\}/);
  assert.match(source, /实时回传日志/);
  assert.match(source, /api\/models\/\$\{id\}\/activity/);
  assert.match(source, /每 3 秒刷新/);
  assert.match(source, /启动请求已接收/);
  assert.match(source, /正在启动…/);
  assert.match(source, /Chrome 启动、账号校验完成后会自动运行采集脚本/);
  assert.match(source, /状态每 2 秒从采集日志同步/);
});

test("信源分析区区分文章视频并显示每日 Top 10 与关键词", async () => {
  const source = await readFile(new URL("Dashboard.tsx", app), "utf8");
  assert.match(source, /高频文章 Top 10/);
  assert.match(source, /高频视频 Top 10/);
  assert.match(source, /文章文案关键词/);
  assert.match(source, /视频文案关键词/);
  assert.match(source, /自有品牌/);
  assert.match(source, /文章标题关键词每日变化/);
  assert.match(source, /正文产品提及率与每日名次/);
});

test("页面元数据使用通用多模型产品名称", async () => {
  const layout = await readFile(new URL("layout.tsx", app), "utf8");
  assert.match(layout, /模型情报台/);
  assert.doesNotMatch(layout, /Starter Project|codex-preview|豆包 × 元宝/);
});
