"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getIcon } from "./ModelIcon";

type Source = {
  title: string;
  url: string;
  canonical_url: string;
  media: string;
  type: string;
  count?: number;
  brand_mentions?: string[];
  owned_brands?: string[];
  own_products?: string[];
  own_brand?: boolean;
  brand_match_scope?: string;
};
type CountItem = { name: string; count: number };
type Keyword = { term: string; count: number };
type Run = {
  run_id: string;
  sequence: number;
  question: string;
  finished_at: string;
  serial: string;
  answer: string;
  status: string;
  sources: Source[];
};
type MentionItem = { name: string; mentions: number; mention_rate: number; rank: number; rank_change?: number | null; average_position?: number | null };
type MentionDay = { date: string; runs: number; items: MentionItem[] };
type SourceBrandDay = { date: string; runs: number; sources: number; branded_sources: number; branded_source_rate: number; title_branded_sources: number; title_branded_source_rate: number; owned_sources: number; owned_source_rate: number; article_keywords: Keyword[]; video_keywords: Keyword[] };
type Model = {
  id: string;
  name: string;
  short_name: string;
  tone: string;
  runs: number;
  sources: number;
  unique_sources: number;
  question_count: number;
  device_count: number;
  source_types: CountItem[];
  media: CountItem[];
  daily: {
    date: string;
    runs: number;
    sources: number;
    unique_sources: number;
  }[];
  questions: {
    question: string;
    runs: number;
    sources: number;
    unique_sources: number;
    avg_sources: number;
  }[];
  top_articles: Source[];
  top_videos: Source[];
  article_keywords: Keyword[];
  video_keywords: Keyword[];
  brand_daily: MentionDay[];
  product_daily: MentionDay[];
  source_brand_daily: SourceBrandDay[];
  daily_source_top: { date: string; top_articles: Source[]; top_videos: Source[] }[];
  owned_source_count: number;
  branded_source_count: number;
  recent_runs: Run[];
};
type CatalogModel = {
  id: string;
  name: string;
  short_name: string;
  tone: string;
  supports_control: boolean;
  execution: "local" | "remote";
};
type Analytics = {
  generated_at: string;
  models: Model[];
  model_catalog: CatalogModel[];
  questions: string[];
  dates: string[];
  analysis_method?: Record<string, string | number>;
};
type ControlState = {
  running: boolean;
  starting?: boolean;
  phase?: string;
  startup_error?: string;
  ready: boolean;
  pid?: number;
  started_at?: string;
  last_exit_code?: number;
  log?: string;
};
type AccountState = {
  ok: boolean;
  status: string;
  message: string;
  mobile?: { masked?: string };
  web?: { masked?: string };
  location?: string;
};
type RemoteEvent = {
  request_id: string;
  status: "queued" | "processed" | "error";
  source_device: string;
  question: string;
  received_at: string;
  processed_at: string;
  account_uid_masked?: string;
  rows_written: number;
  message?: string;
};
type RemoteActivity = {
  queue: { queued: number; processed: number; errors: number };
  events: RemoteEvent[];
};
type View = "overview" | "compare" | "sources" | "brands" | "runs" | "control";
type QuestionMode = "interleaved" | "sequential";

const emptyAnalytics: Analytics = {
  generated_at: "",
  models: [],
  model_catalog: [],
  questions: [],
  dates: [],
};
const views: { id: View; label: string; hint: string; symbol: string }[] = [
  { id: "overview", label: "模型总览", hint: "先看规模与质量", symbol: "◈" },
  { id: "compare", label: "问题对比", hint: "同一问题横向比较", symbol: "⇄" },
  { id: "sources", label: "信源洞察", hint: "每日 Top 10 与关键词", symbol: "◎" },
  { id: "brands", label: "品牌与产品", hint: "提及率、排名与自有信源", symbol: "◇" },
  { id: "runs", label: "回答审计", hint: "逐轮查看原始证据", symbol: "≡" },
  { id: "control", label: "采集控制", hint: "问题、账号与任务", symbol: "▶" },
];

function apiBase() {
  if (typeof window === "undefined") return "http://127.0.0.1:8765";
  return window.location.port === "8765"
    ? ""
    : `${window.location.protocol}//${window.location.hostname}:8765`;
}
function fmt(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}
function pct(value: number, total: number) {
  return total ? `${((value * 100) / total).toFixed(1)}%` : "0%";
}
function timeText(value?: string) {
  if (!value) return "—";
  const date = new Date(value.replace(" ", "T"));
  return Number.isNaN(date.getTime())
    ? value.slice(0, 16)
    : date.toLocaleString("zh-CN", {
        hour12: false,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}
function beijingTime(value?: string) {
  return value ? value.slice(5, 16).replace("T", " ") : "等待接收";
}

function Empty({ children }: { children: string }) {
  return (
    <div className="empty">
      <b>◇</b>
      <span>{children}</span>
    </div>
  );
}
function SectionTitle({
  eyebrow,
  title,
  note,
}: {
  eyebrow?: string;
  title: string;
  note?: string;
}) {
  return (
    <div className="section-title">
      <div>
        {eyebrow && <span>{eyebrow}</span>}
        <h2>{title}</h2>
      </div>
      {note && <small>{note}</small>}
    </div>
  );
}
function Bars({ items, total }: { items: CountItem[]; total: number }) {
  const max = Math.max(1, ...items.map((item) => item.count));
  if (!items.length) return <Empty>当前范围暂无数据</Empty>;
  return (
    <div className="bars">
      {items.slice(0, 9).map((item) => (
        <div className="bar" key={item.name}>
          <div>
            <b>{item.name}</b>
            <span>
              {fmt(item.count)} · {pct(item.count, total)}
            </span>
          </div>
          <i>
            <em style={{ width: `${(item.count * 100) / max}%` }} />
          </i>
        </div>
      ))}
    </div>
  );
}
function SourceTop({
  title,
  items,
  tone,
}: {
  title: string;
  items: Source[];
  tone: string;
}) {
  return (
    <article className="source-block">
      <div className="source-block-title">
        <span className={`source-kind ${tone}`}>
          {tone === "video" ? "视频" : "文章"}
        </span>
        <h3>{title}</h3>
        <small>每轮同链接只计一次</small>
      </div>
      {items.length ? (
        <ol>
          {items.map((item, index) => (
            <li key={item.canonical_url}>
              <span>{index + 1}</span>
              <a href={item.url} target="_blank" rel="noreferrer">
                <b>{item.title || item.url}</b>
                <small>
                  {item.media} · 出现 {item.count || 1} 轮
                </small>
                {item.own_brand && (
                  <em className="owned-source">自有产品 · {item.own_products?.join("、") || item.owned_brands?.join("、")} · {item.brand_match_scope}</em>
                )}
              </a>
            </li>
          ))}
        </ol>
      ) : (
        <Empty>暂无对应信源</Empty>
      )}
    </article>
  );
}
function Keywords({
  title,
  items,
  tone,
}: {
  title: string;
  items: Keyword[];
  tone: string;
}) {
  return (
    <div className="keyword-box">
      <div>
        <b>{title}</b>
        <small>按信源标题文案的跨链接出现次数</small>
      </div>
      {items.length ? (
        <div className="keyword-cloud">
          {items.map((item, index) => (
            <span
              className={`${tone} size-${Math.min(3, Math.floor(index / 5))}`}
              key={item.term}
            >
              {item.term}
              <i>{item.count}</i>
            </span>
          ))}
        </div>
      ) : (
        <Empty>样本不足，至少需两个标题共同出现</Empty>
      )}
    </div>
  );
}

function MentionTrend({ title, days }: { title: string; days: MentionDay[] }) {
  const rows = days.slice(-7).reverse().flatMap((day) => day.items.slice(0, 10).map((item) => ({ ...item, date: day.date, runs: day.runs })));
  return (
    <article className="trend-block">
      <h3>{title}</h3>
      <small>提及率 = 当日提及轮次 ÷ 当日有效运行轮次；排名按提及轮次降序</small>
      {rows.length ? <div className="trend-table"><table><thead><tr><th>日期</th><th>名称</th><th>提及</th><th>提及率</th><th>名次</th><th>变化</th><th>正文位次</th></tr></thead><tbody>
        {rows.map((row) => <tr key={`${row.date}-${row.name}`}><td>{row.date}</td><td><b>{row.name}</b></td><td>{row.mentions}/{row.runs}</td><td>{row.mention_rate.toFixed(1)}%</td><td>#{row.rank}</td><td className={(row.rank_change || 0) > 0 ? "rise" : (row.rank_change || 0) < 0 ? "fall" : ""}>{row.rank_change == null ? "新" : row.rank_change > 0 ? `↑${row.rank_change}` : row.rank_change < 0 ? `↓${Math.abs(row.rank_change)}` : "—"}</td><td>{row.average_position ? row.average_position.toFixed(1) : "—"}</td></tr>)}
      </tbody></table></div> : <Empty>当前范围暂无正文品牌或产品结果</Empty>}
    </article>
  );
}

function SourceBrandTrend({ days }: { days: SourceBrandDay[] }) {
  return <article className="trend-block"><h3>品牌信源与自有品牌信源变化</h3><small>视频只核验标题；文章核验标题及已归档正文</small>
    {days.length ? <div className="trend-table"><table><thead><tr><th>日期</th><th>信源</th><th>标题含品牌</th><th>标题品牌率</th><th>标题/正文含品牌</th><th>自有品牌</th><th>自有率</th></tr></thead><tbody>
      {days.slice(-14).reverse().map((row) => <tr key={row.date}><td>{row.date}</td><td>{row.sources}</td><td>{row.title_branded_sources}</td><td>{row.title_branded_source_rate.toFixed(1)}%</td><td>{row.branded_sources} · {row.branded_source_rate.toFixed(1)}%</td><td>{row.owned_sources}</td><td>{row.owned_source_rate.toFixed(1)}%</td></tr>)}
    </tbody></table></div> : <Empty>当前范围暂无信源品牌数据</Empty>}
  </article>;
}

function KeywordDailyTrend({ title, days, field }: { title: string; days: SourceBrandDay[]; field: "article_keywords" | "video_keywords" }) {
  const rows = days.slice(-7).reverse().flatMap((day) => day[field].slice(0, 10).map((item, index) => ({ ...item, date: day.date, rank: index + 1 })));
  return <article className="trend-block"><h3>{title}</h3><small>标题本地分词；显示每日 Top 10，便于观察主题升降</small>
    {rows.length ? <div className="trend-table keyword-trend"><table><thead><tr><th>日期</th><th>关键词</th><th>标题数</th><th>当日名次</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.date}-${row.term}`}><td>{row.date}</td><td><b>{row.term}</b></td><td>{row.count}</td><td>#{row.rank}</td></tr>)}</tbody></table></div> : <Empty>当前范围标题样本不足</Empty>}
  </article>;
}

function RemoteTransferLog({ activity }: { activity?: RemoteActivity }) {
  const queue = activity?.queue || { queued: 0, processed: 0, errors: 0 };
  const events = activity?.events || [];
  return (
    <section className="remote-transfer">
      <div className="remote-transfer-head">
        <div>
          <span className="live-dot" />
          <b>实时回传日志</b>
          <small>每 3 秒刷新 · 北京时间</small>
        </div>
        <div className="remote-counters">
          <span>处理中<b>{queue.queued}</b></span>
          <span>已入库<b>{queue.processed}</b></span>
          <span className={queue.errors ? "danger" : ""}>异常<b>{queue.errors}</b></span>
        </div>
      </div>
      <div className="remote-event-list">
        {events.length ? events.map((event) => (
          <div className={`remote-event ${event.status}`} key={`${event.status}-${event.request_id}`}>
            <time>{beijingTime(event.processed_at || event.received_at)}</time>
            <i>{event.status === "processed" ? "已入库" : event.status === "queued" ? "处理中" : "异常"}</i>
            <div>
              <b>{event.question || "未记录问题"}</b>
              <small>{event.source_device}{event.account_uid_masked ? ` · UID ${event.account_uid_masked}` : ""}</small>
            </div>
            <strong>{event.status === "processed" ? `${event.rows_written} 条信源` : event.message || "等待写入正式数据"}</strong>
          </div>
        )) : <Empty>等待远端豆包数据回传</Empty>}
      </div>
    </section>
  );
}

export function Dashboard() {
  const [view, setView] = useState<View>("overview");
  const [model, setModel] = useState("");
  const [question, setQuestion] = useState("");
  const [date, setDate] = useState("");
  const [analytics, setAnalytics] = useState<Analytics>(emptyAnalytics);
  const [control, setControl] = useState<Record<string, ControlState>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [accounts, setAccounts] = useState<Record<string, AccountState>>({});
  const [startingModels, setStartingModels] = useState<Record<string, boolean>>({});
  const [remoteActivity, setRemoteActivity] = useState<Record<string, RemoteActivity>>({});
  const [rounds, setRounds] = useState<Record<string, number>>({});
  const [questionModes, setQuestionModes] = useState<
    Record<string, QuestionMode>
  >({});
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [lastRefresh, setLastRefresh] = useState("");
  const draftsInitialized = useRef(false);

  const load = useCallback(async () => {
    try {
      const base = apiBase();
      const params = new URLSearchParams({ _: String(Date.now()) });
      if (model) params.set("model", model);
      if (question) params.set("question", question);
      if (date) params.set("date", date);
      const [a, c] = await Promise.all([
        fetch(`${base}/api/analytics?${params}`, { cache: "no-store" }),
        fetch(`${base}/api/control/status?_=${Date.now()}`, {
          cache: "no-store",
        }),
      ]);
      if (!a.ok || !c.ok) throw new Error("本地数据服务暂不可用");
      const analyticsPayload = await a.json();
      const controlPayload = await c.json();
      setAnalytics(analyticsPayload);
      setControl(controlPayload.models || {});
      setError("");
      setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
      if (!draftsInitialized.current && analyticsPayload.model_catalog?.length) {
        draftsInitialized.current = true;
        const questionRows = await Promise.all(
          analyticsPayload.model_catalog.map(async (item: CatalogModel) => {
            const response = await fetch(
              `${base}/api/models/${item.id}/questions`,
              { cache: "no-store" },
            );
            const payload = response.ok
              ? await response.json()
              : { questions: [], question_mode: "interleaved" };
            return {
              id: item.id,
              questions: (payload.questions || []).join("\n"),
              questionMode:
                payload.question_mode === "sequential"
                  ? "sequential"
                  : "interleaved",
            };
          }),
        );
        setDrafts(
          Object.fromEntries(
            questionRows.map((row) => [row.id, row.questions]),
          ),
        );
        setQuestionModes(
          Object.fromEntries(
            questionRows.map((row) => [row.id, row.questionMode]),
          ),
        );
        setRounds(
          Object.fromEntries(
            analyticsPayload.model_catalog.map((item: CatalogModel) => [
              item.id,
              10,
            ]),
          ),
        );
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "连接失败");
    } finally {
      setLoading(false);
    }
  }, [model, question, date]);

  useEffect(() => {
    const initial = window.setTimeout(load, 0);
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 5000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    if (question && !analytics.questions.includes(question)) setQuestion("");
    if (date && !analytics.dates.includes(date)) setDate("");
  }, [analytics.questions, analytics.dates, question, date]);

  const remoteModelIds = analytics.model_catalog
    .filter((item) => item.execution === "remote")
    .map((item) => item.id)
    .join(",");

  useEffect(() => {
    if (!remoteModelIds) return;
    let active = true;
    const ids = remoteModelIds.split(",");
    const refresh = async () => {
      const rows = await Promise.all(
        ids.map(async (id) => {
          try {
            const response = await fetch(
              `${apiBase()}/api/models/${id}/activity?limit=40&_=${Date.now()}`,
              { cache: "no-store" },
            );
            return [id, response.ok ? await response.json() : undefined] as const;
          } catch {
            return [id, undefined] as const;
          }
        }),
      );
      if (active) {
        setRemoteActivity((old) => ({
          ...old,
          ...Object.fromEntries(rows.filter((row) => row[1])),
        }));
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [remoteModelIds]);

  const selectedModels = analytics.models;
  const totals = useMemo(
    () =>
      selectedModels.reduce(
        (sum, item) => ({
          runs: sum.runs + item.runs,
          sources: sum.sources + item.sources,
          unique: sum.unique + item.unique_sources,
          devices: sum.devices + item.device_count,
        }),
        { runs: 0, sources: 0, unique: 0, devices: 0 },
      ),
    [selectedModels],
  );

  async function saveQuestions(modelId: string) {
    const questions = (drafts[modelId] || "")
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
    const response = await fetch(
      `${apiBase()}/api/models/${modelId}/questions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          questions,
          question_mode: questionModes[modelId] || "interleaved",
        }),
      },
    );
    const payload = await response.json();
    if (!response.ok || !payload.ok)
      throw new Error(payload.error || "保存失败");
  }
  async function accountCheck(modelId: string) {
    setMessage("正在校验模拟器与网页账号…");
    try {
      const response = await fetch(
        `${apiBase()}/api/models/${modelId}/account-check`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        },
      );
      const payload = await response.json();
      if (!response.ok || !payload.ok)
        throw new Error(payload.error || "校验失败");
      setAccounts((old) => ({ ...old, [modelId]: payload.account }));
      setMessage(payload.account.message);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "校验失败");
    }
  }
  async function controlAction(modelId: string, action: "start" | "stop") {
    if (action === "start") {
      setStartingModels((old) => ({ ...old, [modelId]: true }));
      setControl((old) => ({ ...old, [modelId]: { running: false, ready: true, ...old[modelId], starting: true, phase: "正在保存问题并提交启动请求" } }));
    }
    setMessage(action === "start" ? "启动请求已接收，正在准备浏览器与账号校验…" : "正在停止任务…");
    try {
      if (action === "start") await saveQuestions(modelId);
      const response = await fetch(
        `${apiBase()}/api/control/${modelId}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            rounds: rounds[modelId] || 10,
            question_mode: questionModes[modelId] || "interleaved",
          }),
        },
      );
      const payload = await response.json();
      if (!response.ok || !payload.ok)
        throw new Error(payload.error || "操作失败");
      setMessage(
        `${analytics.model_catalog.find((item) => item.id === modelId)?.name || modelId}${action === "start" ? "已启动" : "已停止"}`,
      );
      await load();
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "操作失败");
    } finally {
      if (action === "start") setStartingModels((old) => ({ ...old, [modelId]: false }));
    }
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div>MI</div>
          <span>
            <b>GEO大模型监测</b>
            <small>
              <i className={error ? "off" : ""} />
              {error || "数据服务在线"}
            </small>
          </span>
        </div>
        <div className="scope">
          <span>模型范围</span>
          <button
            className={!model ? "active" : ""}
            onClick={() => setModel("")}
          >
            <svg className="model-icon all-models" width={29} height={29} viewBox="0 0 29 29" fill="none">
              <rect width="29" height="29" rx="8" fill="url(#all-grad)" />
              <circle cx="10" cy="10" r="3" fill="white" opacity="0.9" />
              <circle cx="19" cy="10" r="3" fill="white" opacity="0.9" />
              <circle cx="10" cy="19" r="3" fill="white" opacity="0.9" />
              <circle cx="19" cy="19" r="3" fill="white" opacity="0.9" />
              <defs>
                <linearGradient id="all-grad" x1="0" y1="0" x2="29" y2="29" gradientUnits="userSpaceOnUse">
                  <stop stopColor="#34C298" />
                  <stop offset="1" stopColor="#128062" />
                </linearGradient>
              </defs>
            </svg>
            <b>全部模型</b>
            <small>综合比较</small>
          </button>
          {analytics.model_catalog.map((item) => {
            const Icon = getIcon(item.tone);
            return (
              <button
                className={model === item.id ? "active" : ""}
                key={item.id}
                onClick={() => setModel(item.id)}
              >
                {Icon ? <Icon className={`model-icon ${item.tone}`} size={29} /> : <i className={item.tone}>{item.short_name}</i>}
                <b>{item.name}</b>
                <small>{control[item.id]?.running ? "采集中" : "已停止"}</small>
              </button>
            );
          })}
        </div>
        <nav>
          {views.map((item) => (
            <button
              key={item.id}
              className={view === item.id ? "active" : ""}
              onClick={() => setView(item.id)}
            >
              <i>{item.symbol}</i>
              <span>
                <b>{item.label}</b>
                <small>{item.hint}</small>
              </span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span>5 秒自动刷新</span>
          <b>{lastRefresh || "等待首次读取"}</b>
        </div>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <span>
              MODEL INTELLIGENCE /{" "}
              {views.find((item) => item.id === view)?.label}
            </span>
            <h1>
              {view === "compare" && question
                ? `“${question}”跨模型表现`
                : views.find((item) => item.id === view)?.label}
            </h1>
            <p>所有统计按北京时间自然日归档；同一轮同一链接仅计一次。</p>
          </div>
          <button onClick={() => load()} disabled={loading}>
            {loading ? "读取中" : "刷新数据"}
          </button>
        </header>
        {view !== "control" && (
          <div className="filterbar">
            <label>
              问题
              <select
                data-testid="question-filter"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
              >
                <option value="">全部问题</option>
                {analytics.questions.map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </label>
            <label>
              自然日
              <select
                data-testid="date-filter"
                value={date}
                onChange={(event) => setDate(event.target.value)}
              >
                <option value="">全部日期</option>
                {analytics.dates.map((item) => (
                  <option key={item}>{item}</option>
                ))}
              </select>
            </label>
            <div>
              <span>当前口径</span>
              <b>
                {model
                  ? analytics.model_catalog.find((item) => item.id === model)
                      ?.name
                  : "全部模型"}{" "}
                · {question || "全部问题"} · {date || "全部日期"}
              </b>
            </div>
          </div>
        )}

        <div className="content">
          {view !== "control" && (
            <section className="kpis">
              <article>
                <span>有效回答</span>
                <b>{fmt(totals.runs)}</b>
                <small>当前筛选范围内完成轮次</small>
              </article>
              <article>
                <span>信源引用</span>
                <b>{fmt(totals.sources)}</b>
                <small>去重后 {fmt(totals.unique)} 条链接</small>
              </article>
              <article>
                <span>平均信源 / 回答</span>
                <b>
                  {totals.runs
                    ? (totals.sources / totals.runs).toFixed(1)
                    : "0"}
                </b>
                <small>衡量回答证据密度</small>
              </article>
              <article>
                <span>模型 / 设备</span>
                <b>
                  {selectedModels.length} / {totals.devices}
                </b>
                <small>当前参与统计的采集端</small>
              </article>
            </section>
          )}

          {view === "overview" && (
            <>
              <section className="panel">
                <SectionTitle
                  eyebrow="MODEL HEALTH"
                  title="模型表现一眼看清"
                  note="点击左侧模型可进入单模型视角"
                />
                <div className="model-grid">
                  {selectedModels.map((item) => {
                    const Icon = getIcon(item.tone);
                    return (
                    <article
                      className={`model-card ${item.tone}`}
                      key={item.id}
                    >
                      <header>
                        {Icon ? <Icon className={`model-icon ${item.tone}`} size={38} /> : <i>{item.short_name}</i>}
                        <div>
                          <b>{item.name}</b>
                          <small>
                            {control[item.id]?.running
                              ? "● 正在采集"
                              : "配置就绪"}
                          </small>
                        </div>
                      </header>
                      <strong>
                        {fmt(item.runs)}
                        <small>轮有效回答</small>
                      </strong>
                      <dl>
                        <div>
                          <dt>信源引用</dt>
                          <dd>{fmt(item.sources)}</dd>
                        </div>
                        <div>
                          <dt>唯一链接</dt>
                          <dd>{fmt(item.unique_sources)}</dd>
                        </div>
                        <div>
                          <dt>平均信源</dt>
                          <dd>
                            {item.runs
                              ? (item.sources / item.runs).toFixed(1)
                              : "0"}
                          </dd>
                        </div>
                      </dl>
                    </article>
                  );
                  })}
                </div>
              </section>
              <section className="two-col">
                <article className="panel">
                  <SectionTitle title="按日采集脉搏" note="最近 7 个自然日" />
                  <div className="daily-list">
                    {selectedModels
                      .flatMap((item) =>
                        item.daily
                          .slice(0, 7)
                          .map((row) => ({
                            ...row,
                            model: item.name,
                            tone: item.tone,
                          })),
                      )
                      .sort((a, b) => b.date.localeCompare(a.date))
                      .slice(0, 14)
                      .map((row, index) => (
                        <div key={`${row.model}-${row.date}-${index}`}>
                          <i className={row.tone} />
                          <b>{row.date}</b>
                          <span>{row.model}</span>
                          <strong>
                            {row.runs} 轮 · {row.sources} 信源
                          </strong>
                        </div>
                      ))}
                  </div>
                </article>
                <article className="panel">
                  <SectionTitle
                    title="证据结构"
                    note="文章、视频、社交与电商"
                  />
                  {selectedModels.map((item) => (
                    <div className="model-bars" key={item.id}>
                      <b>{item.name}</b>
                      <Bars items={item.source_types} total={item.sources} />
                    </div>
                  ))}
                </article>
              </section>
            </>
          )}

          {view === "compare" && (
            <>
              {!question ? (
                <section className="panel compare-prompt">
                  <b>先选择一个问题</b>
                  <p>
                    选择问题后，这里会用完全相同的口径横向比较每个模型的回答轮次、信源数量、唯一链接和证据密度。
                  </p>
                  <div>
                    {analytics.questions.map((item) => (
                      <button key={item} onClick={() => setQuestion(item)}>
                        {item}
                      </button>
                    ))}
                  </div>
                </section>
              ) : (
                <>
                  <section className="panel">
                    <SectionTitle
                      eyebrow="QUESTION BENCHMARK"
                      title="同一问题，各模型证据表现"
                      note={date || "全部日期"}
                    />
                    <div className="compare-table">
                      <table>
                        <thead>
                          <tr>
                            <th>模型</th>
                            <th>回答轮次</th>
                            <th>信源引用</th>
                            <th>唯一链接</th>
                            <th>平均信源</th>
                            <th>相对证据量</th>
                          </tr>
                        </thead>
                        <tbody>
                          {selectedModels.map((item) => {
                            const Icon = getIcon(item.tone);
                            const max = Math.max(
                              1,
                              ...selectedModels.map((row) => row.sources),
                            );
                            return (
                              <tr key={item.id}>
                                <td>
                                  <span className={`model-pill ${item.tone}`}>
                                    {Icon ? <Icon className="model-icon" size={28} /> : item.short_name}
                                  </span>
                                  <b>{item.name}</b>
                                </td>
                                <td>{item.runs}</td>
                                <td>
                                  <strong>{item.sources}</strong>
                                </td>
                                <td>{item.unique_sources}</td>
                                <td>
                                  {item.runs
                                    ? (item.sources / item.runs).toFixed(2)
                                    : "0"}
                                </td>
                                <td>
                                  <i className="evidence">
                                    <em
                                      style={{
                                        width: `${(item.sources * 100) / max}%`,
                                      }}
                                    />
                                  </i>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </section>
                  <section className="model-source-grid">
                    {selectedModels.map((item) => (
                      <article className="panel" key={item.id}>
                        <SectionTitle
                          eyebrow={item.name.toUpperCase()}
                          title="信源构成"
                          note={`${item.sources} 次引用`}
                        />
                        <Bars items={item.source_types} total={item.sources} />
                        <div className="media-chips">
                          {item.media.slice(0, 8).map((row) => (
                            <span key={row.name}>
                              {row.name}
                              <b>{row.count}</b>
                            </span>
                          ))}
                        </div>
                        <div className="compare-source-tops">
                          <SourceTop title="文章链接 Top 10" items={item.top_articles} tone="article" />
                          <SourceTop title="视频链接 Top 10" items={item.top_videos} tone="video" />
                        </div>
                      </article>
                    ))}
                  </section>
                </>
              )}
            </>
          )}

          {view === "sources" && (
            <>
              {selectedModels.map((item) => (
                <section className="panel source-section" key={item.id}>
                  <SectionTitle
                    eyebrow={item.name.toUpperCase()}
                    title={`${question || "全部问题"} · 信源策略`}
                    note={date || "逐日展开最近 7 天"}
                  />
                  {(date ? [{ date, top_articles: item.top_articles, top_videos: item.top_videos }] : item.daily_source_top.slice(0, 7)).map((day) => (
                    <div className="daily-source-group" key={`${item.id}-${day.date}`}>
                      <h3>{day.date} · {item.name}</h3>
                      <div className="source-pair">
                        <SourceTop title="高频文章 Top 10" items={day.top_articles} tone="article" />
                        <SourceTop title="高频视频 Top 10" items={day.top_videos} tone="video" />
                      </div>
                    </div>
                  ))}
                  <div className="keyword-pair">
                    <Keywords
                      title="文章文案关键词"
                      items={item.article_keywords}
                      tone="article"
                    />
                    <Keywords
                      title="视频文案关键词"
                      items={item.video_keywords}
                      tone="video"
                    />
                  </div>
                </section>
              ))}
            </>
          )}

          {view === "brands" && (
            <>
              <section className="analysis-note panel">
                <b>准确性与成本口径</b>
                <span>正文优先使用采集端结构化品牌/产品结果；未覆盖项只做跨模型词表精确命中。视频仅检查标题，文章检查标题与已归档正文。本页统计不调用大模型，日常 Token 消耗为 0。</span>
              </section>
              {selectedModels.map((item) => (
                <section className="panel brand-section" key={item.id}>
                  <SectionTitle eyebrow={item.name.toUpperCase()} title={`${question || "全部问题"} · 品牌与产品每日表现`} note={date || "最近 7–14 个自然日"} />
                  <div className="brand-kpis">
                    <span><b>{item.branded_source_count}</b> 条含品牌信源</span>
                    <span><b>{item.owned_source_count}</b> 条自有品牌信源</span>
                    <span><b>{item.sources ? ((item.owned_source_count * 100) / item.sources).toFixed(1) : "0"}%</b> 自有信源率</span>
                  </div>
                  <div className="trend-grid">
                    <MentionTrend title="正文品牌提及率与每日名次" days={item.brand_daily} />
                    <MentionTrend title="正文产品提及率与每日名次" days={item.product_daily} />
                  </div>
                  <SourceBrandTrend days={item.source_brand_daily} />
                  <div className="trend-grid">
                    <KeywordDailyTrend title="文章标题关键词每日变化" days={item.source_brand_daily} field="article_keywords" />
                    <KeywordDailyTrend title="视频标题关键词每日变化" days={item.source_brand_daily} field="video_keywords" />
                  </div>
                  <div className="keyword-pair">
                    <Keywords title="文章标题关键词（当前范围）" items={item.article_keywords} tone="article" />
                    <Keywords title="视频标题关键词（当前范围）" items={item.video_keywords} tone="video" />
                  </div>
                </section>
              ))}
            </>
          )}

          {view === "runs" && (
            <section className="panel">
              <SectionTitle
                eyebrow="ANSWER AUDIT"
                title="逐轮回答与信源审计"
                note="按完成时间倒序"
              />
              <div className="run-list">
                {selectedModels
                  .flatMap((item) =>
                    item.recent_runs.map((run) => ({ ...run, model: item })),
                  )
                  .sort((a, b) => b.finished_at.localeCompare(a.finished_at))
                  .map((run) => (
                    <article key={`${run.model.id}-${run.run_id}`}>
                      <header>
                        <span className={`model-pill ${run.model.tone}`}>
                          {(() => { const Icon = getIcon(run.model.tone); return Icon ? <Icon className="model-icon" size={28} /> : run.model.short_name; })()}
                        </span>
                        <b>
                          {run.model.name} · 第 {run.sequence} 轮
                        </b>
                        <time>{timeText(run.finished_at)}</time>
                      </header>
                      <h3>{run.question}</h3>
                      <p>
                        {run.sources.length} 条信源 · {run.serial}
                      </p>
                      <details>
                        <summary>查看回答正文与信源</summary>
                        <div className="answer-text">
                          {run.answer || "未保存回答正文"}
                        </div>
                        <ul>
                          {run.sources.map((source) => (
                            <li key={source.canonical_url}>
                              <a
                                href={source.url}
                                target="_blank"
                                rel="noreferrer"
                              >
                                {source.title || source.url}
                              </a>
                              <small>
                                {source.media} · {source.type}
                              </small>
                              {source.own_brand && <em className="owned-source">自有产品 · {source.own_products?.join("、") || source.owned_brands?.join("、")} · {source.brand_match_scope}</em>}
                            </li>
                          ))}
                        </ul>
                      </details>
                    </article>
                  ))}
              </div>
            </section>
          )}

          {view === "control" && (
            <>
              <section className="control-hero">
                <div>
                  <span>COLLECTION WORKSPACE</span>
                  <h2>每个模型独立配置，互不影响</h2>
                  <p>
                    问题清单、账号校验、采集进程、分析与数据文件都归属于各自模型插件。新增模型只增加插件目录，不修改现有模型。
                  </p>
                </div>
                {message && <b>{message}</b>}
              </section>
              <section className="control-grid">
                {analytics.model_catalog.map((item) => {
                  const state = control[item.id];
                  const account = accounts[item.id];
                  const remote = item.execution === "remote";
                  const starting = Boolean(state?.starting || startingModels[item.id]);
                  return (
                    <article
                      className={`panel control-card ${state?.running ? "running" : ""} ${starting ? "starting" : ""}`}
                      data-model-id={item.id}
                      key={item.id}
                    >
                      <header>
                        {(() => { const Icon = getIcon(item.tone); return Icon ? <Icon className={`model-icon ${item.tone}`} size={42} /> : <i className={item.tone}>{item.short_name}</i>; })()}
                        <div>
                          <h2>{item.name}</h2>
                          <span>
                            {remote
                              ? "● 远端采集 · 本机接收"
                              : starting
                                ? `● ${state?.phase || "正在启动"}`
                                : state?.running
                                ? `● 运行中 · PID ${state.pid}`
                                : state?.ready
                                  ? "● 配置就绪"
                                  : "● 缺少配置"}
                          </span>
                        </div>
                      </header>
                      {!remote && (starting || state?.startup_error) && (
                        <div className={`startup-feedback ${state?.startup_error ? "failed" : ""}`}>
                          <span className={starting ? "startup-spinner" : ""} />
                          <div>
                            <b>{state?.startup_error || state?.phase || "正在启动"}</b>
                            <small>{state?.startup_error ? "请修正后重新点击启动" : "Chrome 启动、账号校验完成后会自动运行采集脚本"}</small>
                          </div>
                        </div>
                      )}
                      {!remote && state?.running && state?.phase && (
                        <div className="runtime-feedback">
                          <span className="runtime-dot" />
                          <div>
                            <b>{state.phase}</b>
                            <small>状态每 2 秒从采集日志同步</small>
                          </div>
                        </div>
                      )}
                      <label>
                        提问问题{" "}
                        <small>
                          {remote
                            ? "保存为远端下次同步的问题计划"
                            : "每行一个，启动前自动保存"}
                        </small>
                        <textarea
                          value={drafts[item.id] || ""}
                          onChange={(event) =>
                            setDrafts((old) => ({
                              ...old,
                              [item.id]: event.target.value,
                            }))
                          }
                        />
                      </label>
                      <div className="control-row">
                        <label>
                          每题轮数
                          <input
                            type="number"
                            min="1"
                            max="10000"
                            value={rounds[item.id] || 10}
                            disabled={remote}
                            onChange={(event) =>
                              setRounds((old) => ({
                                ...old,
                                [item.id]: Math.max(
                                  1,
                                  Number(event.target.value) || 1,
                                ),
                              }))
                            }
                          />
                        </label>
                        <label>
                          执行顺序
                          <select
                            value={questionModes[item.id] || "interleaved"}
                            onChange={(event) =>
                              setQuestionModes((old) => ({
                                ...old,
                                [item.id]: event.target.value as QuestionMode,
                              }))
                            }
                          >
                            <option value="interleaved">
                              交叉提问（每题一轮）
                            </option>
                            <option value="sequential">
                              顺序提问（单题全部轮次）
                            </option>
                          </select>
                        </label>
                        <button
                          className="account-button"
                          onClick={() => accountCheck(item.id)}
                        >
                          校验模拟器 / 网页账号
                        </button>
                      </div>
                      <div
                        className={`account-result ${account?.status || "idle"}`}
                      >
                        <b>
                          {account
                            ? account.message
                            : remote
                              ? "账号校验在远端豆包控制端执行，本机显示校验状态"
                              : "启动前建议执行账号一致性校验"}
                        </b>
                        {account?.mobile && (
                          <small>
                            模拟器 {account.mobile.masked} · 网页{" "}
                            {account.web?.masked} ·{" "}
                            {account.location === "remote" ? "远端" : "本机"}
                          </small>
                        )}
                      </div>
                      <div className="job-meta">
                        <span>
                          {remote ? "运行位置" : "启动时间"}
                          <b>
                            {remote
                              ? "另一台电脑"
                              : timeText(state?.started_at)}
                          </b>
                        </span>
                        <span>
                          {remote ? "传输方式" : "上次退出码"}
                          <b>
                            {remote
                              ? "局域网加密回传"
                              : (state?.last_exit_code ?? "—")}
                          </b>
                        </span>
                      </div>
                      <div className="job-actions">
                        <button
                          onClick={() =>
                            saveQuestions(item.id)
                              .then(() => setMessage(`${item.name}问题已保存`))
                              .catch((reason) => setMessage(reason.message))
                          }
                        >
                          仅保存问题
                        </button>
                        <button
                          className="start"
                          disabled={remote || starting || state?.running || !state?.ready}
                          onClick={() => controlAction(item.id, "start")}
                        >
                          {remote ? "请在远端启动" : starting ? "正在启动…" : "启动采集"}
                        </button>
                        <button
                          className="stop"
                          disabled={remote || (!starting && !state?.running)}
                          onClick={() => controlAction(item.id, "stop")}
                        >
                          停止
                        </button>
                      </div>
                      <small className="log">
                        {remote
                          ? "远端离线时自动排队，联网后续传"
                          : `日志：${state?.log || "启动后生成"}`}
                      </small>
                      {remote && (
                        <RemoteTransferLog activity={remoteActivity[item.id]} />
                      )}
                    </article>
                  );
                })}
              </section>
            </>
          )}
        </div>
      </section>
    </main>
  );
}
