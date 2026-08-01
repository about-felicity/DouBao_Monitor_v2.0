"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { MODEL_REGISTRY, RESERVED_MODEL_SLOTS, modelById, type ModelId } from "./modelRegistry";

type CountItem = { name: string; count: number };
type YuanbaoSource = { title: string; url: string; canonical_url: string; domain: string; media: string; type: string };
type YuanbaoProduct = { brand: string; product_name: string; category: string; rank: number; evidence: string };
type YuanbaoRun = {
  run_id: string; sequence: number; round: number; serial: string; question: string;
  reply: string; web_body: string; started_at: string; finished_at: string; day: string;
  status: string; sources: YuanbaoSource[]; brands: string[]; products: YuanbaoProduct[];
};
type YuanbaoDaily = {
  date: string; runs: number; successful_runs: number; sources: number; unique_sources: number;
  question_count: number; device_count: number; product_mentions: number;
  brands: CountItem[]; media: CountItem[]; types: CountItem[]; questions: CountItem[];
};
type YuanbaoData = {
  generated_at: string; total_runs: number; successful_runs: number; total_sources: number; unique_sources: number;
  question_count: number; device_count: number; date_range: string; questions: { question: string; runs: number }[];
  devices: { serial: string; runs: number; sources: number; latest: string }[]; runs: YuanbaoRun[];
  daily: YuanbaoDaily[]; top_media: CountItem[]; source_types: CountItem[]; brands: CountItem[];
  products: ProductRow[]; ai_analysis?: { model?: string; status?: string; error?: string };
};
type ProductRow = {
  brand?: string; product_name?: string; name?: string; category?: string; mention_runs?: number;
  eligible_runs?: number; mention_rate?: number; count?: number; run_count?: number; run_rate?: number;
  average_rank?: number; avg_rank?: number; best_rank?: number;
};
type DoubaoDailySeries = {
  question: string; dates: string[]; refs_by_date?: number[]; runs_by_date: number[];
  mentions_by_date?: number[]; media_rows?: { name: string; total: number; counts: number[] }[];
  type_rows?: { name: string; total: number; counts: number[] }[];
  brand_rows?: { name: string; total: number; counts: number[]; latest_rank?: number }[];
  product_rows?: { name: string; total: number; counts: number[]; latest_rank?: number }[];
};
type DoubaoStats = {
  ok?: boolean; generated_at: string; selected_question: string; total_runs: number; total_refs: number;
  unique_links: number; question_count: number; account_count: number; today_runs: number; today_run_date: string;
  latest_run_no: number; latest_question: string; latest_run_time: string; latest_complete: string;
  questions: { question: string; runs: number; refs: number; unique_links: number }[];
  device_options: { instance: string; nickname: string; run_count: number; reference_count: number; latest_at: string }[];
  by_media: CountItem[]; by_type: CountItem[]; by_domain: CountItem[];
  latest_items: { title: string; href: string; media: string; source_type: string; question: string; run_no: number }[];
  products: { total_mentions: number; total_product_runs: number; unique_products: number; unique_brands: number; by_brand: ProductRow[]; by_product: ProductRow[]; latest_products: ProductRow[] };
  product_coverage: Record<string, number>; capture_skips: { active_count: number; pending_save_count: number; resolved_count: number };
  daily_question_sources: DoubaoDailySeries[]; daily_question_products: DoubaoDailySeries[];
};
type ControlModel = { running: boolean; pid?: number | null; started_at?: string; last_exit_code?: number | null; ready: boolean; log?: string };
type ControlData = { ok: boolean; generated_at: string; models: Record<ModelId, ControlModel> };
type Platform = "all" | ModelId;
type View = "overview" | "daily" | "sources" | "products" | "runs" | "control";

const emptyYuanbao: YuanbaoData = {
  generated_at: "", total_runs: 0, successful_runs: 0, total_sources: 0, unique_sources: 0,
  question_count: 0, device_count: 0, date_range: "等待采集", questions: [], devices: [], runs: [], daily: [],
  top_media: [], source_types: [], brands: [], products: [],
};
const emptyDoubao: DoubaoStats = {
  generated_at: "", selected_question: "全部问题", total_runs: 0, total_refs: 0, unique_links: 0,
  question_count: 0, account_count: 0, today_runs: 0, today_run_date: "", latest_run_no: 0,
  latest_question: "", latest_run_time: "", latest_complete: "", questions: [], device_options: [],
  by_media: [], by_type: [], by_domain: [], latest_items: [], daily_question_sources: [], daily_question_products: [],
  products: { total_mentions: 0, total_product_runs: 0, unique_products: 0, unique_brands: 0, by_brand: [], by_product: [], latest_products: [] },
  product_coverage: {}, capture_skips: { active_count: 0, pending_save_count: 0, resolved_count: 0 },
};

const nav: { id: View; label: string; symbol: string }[] = [
  { id: "overview", label: "综合总览", symbol: "◈" }, { id: "daily", label: "每日分析", symbol: "▥" },
  { id: "sources", label: "信源策略", symbol: "◎" }, { id: "products", label: "产品与品牌", symbol: "◆" },
  { id: "runs", label: "运行明细", symbol: "≡" }, { id: "control", label: "采集控制", symbol: "▶" },
];

function apiBase() {
  if (typeof window === "undefined") return "http://127.0.0.1:8765";
  return window.location.port === "8765" ? "" : `${window.location.protocol}//${window.location.hostname}:8765`;
}
function fmt(value: number) { return new Intl.NumberFormat("zh-CN").format(value || 0); }
function pct(value: number, total: number) { return total ? `${(value * 100 / total).toFixed(1)}%` : "0%"; }
function shortTime(value: string) {
  if (!value) return "—";
  const date = new Date(value.replace(" ", "T"));
  return Number.isNaN(date.getTime()) ? value.slice(0, 16) : new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(date);
}
function count(items: string[]): CountItem[] {
  const map = new Map<string, number>();
  items.forEach((item) => map.set(item || "未知", (map.get(item || "未知") || 0) + 1));
  return [...map.entries()].map(([name, amount]) => ({ name, count: amount })).sort((a, b) => b.count - a.count);
}

function Kpi({ label, value, note, tone = "green" }: { label: string; value: string | number; note: string; tone?: string }) {
  return <article className={`kpi ${tone}`}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>;
}
function Bars({ items, total, limit = 8 }: { items: CountItem[]; total: number; limit?: number }) {
  const max = Math.max(1, ...items.map((item) => item.count));
  if (!items.length) return <Empty text="当前范围暂无数据" />;
  return <div className="bars">{items.slice(0, limit).map((item) => <div className="bar" key={item.name}>
    <div><b>{item.name || "未知"}</b><span>{fmt(item.count)} · {pct(item.count, total)}</span></div>
    <i><em style={{ width: `${item.count * 100 / max}%` }} /></i>
  </div>)}</div>;
}
function Empty({ text }: { text: string }) { return <div className="empty"><span>◇</span>{text}</div>; }
function PanelTitle({ eyebrow, title, meta }: { eyebrow?: string; title: string; meta?: string }) {
  return <div className="panel-title"><div>{eyebrow && <span>{eyebrow}</span>}<h2>{title}</h2></div>{meta && <small>{meta}</small>}</div>;
}

function combineDoubaoDaily(series: DoubaoDailySeries[], kind: "source" | "product") {
  const days = new Map<string, { date: string; runs: number; value: number }>();
  series.forEach((group) => group.dates.forEach((date, index) => {
    const row = days.get(date) || { date, runs: 0, value: 0 };
    row.runs += Number(group.runs_by_date[index] || 0);
    row.value += Number((kind === "source" ? group.refs_by_date : group.mentions_by_date)?.[index] || 0);
    days.set(date, row);
  }));
  return [...days.values()].sort((a, b) => b.date.localeCompare(a.date));
}

function doubaoDailyCounts(series: DoubaoDailySeries[], selectedDate: string, field: "media_rows" | "type_rows" | "brand_rows") {
  if (selectedDate === "all") return [];
  const totals = new Map<string, number>();
  series.forEach((group) => {
    const index = group.dates.indexOf(selectedDate);
    if (index < 0) return;
    (group[field] || []).forEach((row) => totals.set(row.name, (totals.get(row.name) || 0) + Number(row.counts[index] || 0)));
  });
  return [...totals.entries()].map(([name, amount]) => ({ name, count: amount })).sort((a, b) => b.count - a.count);
}

export function Dashboard() {
  const [platform, setPlatform] = useState<Platform>("all");
  const [view, setView] = useState<View>("overview");
  const [question, setQuestion] = useState("全部问题");
  const [device, setDevice] = useState("all");
  const [date, setDate] = useState("all");
  const [doubao, setDoubao] = useState<DoubaoStats>(emptyDoubao);
  const [yuanbao, setYuanbao] = useState<YuanbaoData>(emptyYuanbao);
  const [control, setControl] = useState<ControlData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [lastRefresh, setLastRefresh] = useState("");
  const [rounds, setRounds] = useState(10);
  const [actionMessage, setActionMessage] = useState("");

  const load = useCallback(async (selectedQuestion = question) => {
    try {
      const base = apiBase();
      const doubaoParams = new URLSearchParams({ question: selectedQuestion, device: "all", _: String(Date.now()) });
      const [d, y, c] = await Promise.all([
        fetch(`${base}${modelById("doubao").statsEndpoint}?${doubaoParams}`, { cache: "no-store" }),
        fetch(`${base}${modelById("yuanbao").statsEndpoint}?_=${Date.now()}`, { cache: "no-store" }),
        fetch(`${base}/api/control/status?_=${Date.now()}`, { cache: "no-store" }),
      ]);
      if (!d.ok || !y.ok || !c.ok) throw new Error("本地数据服务暂不可用");
      setDoubao(await d.json()); setYuanbao(await y.json()); setControl(await c.json());
      setError(""); setLastRefresh(new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "连接失败");
    } finally { setLoading(false); }
  }, [question]);

  useEffect(() => {
    const initial = window.setTimeout(load, 0);
    const timer = window.setInterval(load, 10000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [load]);

  const changeQuestion = (value: string) => { setQuestion(value); setDate("all"); };
  const yuanbaoRuns = useMemo(() => yuanbao.runs.filter((run) =>
    (question === "全部问题" || run.question === question) && (device === "all" || run.serial === device) &&
    (date === "all" || run.day === date)), [yuanbao.runs, question, device, date]);
  const yuanbaoSources = useMemo(() => yuanbaoRuns.flatMap((run) => run.sources), [yuanbaoRuns]);
  const yuanbaoMedia = useMemo(() => count(yuanbaoSources.map((source) => source.media || source.domain)), [yuanbaoSources]);
  const yuanbaoTypes = useMemo(() => count(yuanbaoSources.map((source) => source.type)), [yuanbaoSources]);
  const yuanbaoBrands = useMemo(() => count(yuanbaoRuns.flatMap((run) => [...new Set(run.brands)])), [yuanbaoRuns]);
  const yuanbaoProducts = useMemo(() => {
    const map = new Map<string, ProductRow>();
    yuanbaoRuns.forEach((run) => run.products.forEach((product) => {
      const key = `${product.brand}\u0000${product.product_name}`;
      const row = map.get(key) || { brand: product.brand, product_name: product.product_name, category: product.category, count: 0, run_count: 0, average_rank: 0 };
      row.count = (row.count || 0) + 1; row.run_count = (row.run_count || 0) + 1;
      row.average_rank = (row.average_rank || 0) + product.rank; map.set(key, row);
    }));
    return [...map.values()].map((row) => ({ ...row, average_rank: (row.average_rank || 0) / (row.run_count || 1), run_rate: (row.run_count || 0) * 100 / Math.max(1, yuanbaoRuns.length) }))
      .sort((a, b) => (b.run_count || 0) - (a.run_count || 0));
  }, [yuanbaoRuns]);
  const yuanbaoDays = useMemo(() => {
    const map = new Map<string, YuanbaoDaily>();
    yuanbaoRuns.forEach((run) => {
      if (!run.day) return;
      const row = map.get(run.day) || { date: run.day, runs: 0, successful_runs: 0, sources: 0, unique_sources: 0, question_count: 0, device_count: 0, product_mentions: 0, brands: [], media: [], types: [], questions: [] };
      row.runs += 1; row.successful_runs += run.status === "success" ? 1 : 0; row.sources += run.sources.length; row.product_mentions += run.products.length;
      map.set(run.day, row);
    });
    return [...map.values()].map((row) => {
      const runs = yuanbaoRuns.filter((run) => run.day === row.date);
      return { ...row,
        unique_sources: new Set(runs.flatMap((run) => run.sources.map((source) => source.canonical_url))).size,
        question_count: new Set(runs.map((run) => run.question)).size,
        device_count: new Set(runs.map((run) => run.serial)).size,
      };
    }).sort((a, b) => b.date.localeCompare(a.date));
  }, [yuanbaoRuns]);
  const doubaoSourceDays = useMemo(() => combineDoubaoDaily(doubao.daily_question_sources, "source").filter((row) => date === "all" || row.date === date), [doubao.daily_question_sources, date]);
  const doubaoProductDays = useMemo(() => combineDoubaoDaily(doubao.daily_question_products, "product").filter((row) => date === "all" || row.date === date), [doubao.daily_question_products, date]);
  const selectedDoubaoDay = date === "all" ? undefined : doubaoSourceDays.find((row) => row.date === date);
  const selectedDoubaoProductDay = date === "all" ? undefined : doubaoProductDays.find((row) => row.date === date);
  const doubaoRuns = selectedDoubaoDay?.runs ?? doubao.total_runs;
  const doubaoRefs = selectedDoubaoDay?.value ?? doubao.total_refs;
  const doubaoProductMentions = selectedDoubaoProductDay?.value ?? doubao.products.total_mentions;
  const doubaoMedia = date === "all" ? doubao.by_media : doubaoDailyCounts(doubao.daily_question_sources, date, "media_rows");
  const doubaoTypes = date === "all" ? doubao.by_type : doubaoDailyCounts(doubao.daily_question_sources, date, "type_rows");
  const doubaoBrands = date === "all" ? [] : doubaoDailyCounts(doubao.daily_question_products, date, "brand_rows");
  const dates = useMemo(() => [...new Set([
    ...yuanbao.daily.map((row) => row.date), ...combineDoubaoDaily(doubao.daily_question_sources, "source").map((row) => row.date),
  ])].filter(Boolean).sort().reverse(), [yuanbao.daily, doubao.daily_question_sources]);
  const questions = useMemo(() => [...new Set([...doubao.questions.map((item) => item.question), ...yuanbao.questions.map((item) => item.question)])], [doubao.questions, yuanbao.questions]);

  const controlAction = async (model: ModelId, action: "start" | "stop") => {
    setActionMessage("正在处理…");
    try {
      const response = await fetch(`${apiBase()}/api/control/${model}/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ rounds }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "操作失败");
      setActionMessage(`${model === "doubao" ? "豆包" : "元宝"}${action === "start" ? "采集已启动" : "采集已停止"}`);
      await load();
    } catch (reason) { setActionMessage(reason instanceof Error ? reason.message : "操作失败"); }
  };

  const visibleDoubao = platform !== "yuanbao";
  const visibleYuanbao = platform !== "doubao";
  const totalRuns = (visibleDoubao ? doubaoRuns : 0) + (visibleYuanbao ? yuanbaoRuns.length : 0);
  const totalSources = (visibleDoubao ? doubaoRefs : 0) + (visibleYuanbao ? yuanbaoSources.length : 0);

  return <main className="app-shell">
    <aside className="sidebar">
      <div className="identity"><div className="logo">AI</div><div><b>多模型监控台</b><span><i className={error ? "off" : ""} />{error || "本地数据在线"}</span></div></div>
      <div className="platform-switch" role="tablist" aria-label="模型范围">
        <button className={platform === "all" ? "active" : ""} onClick={() => setPlatform("all")}>综合</button>
        {MODEL_REGISTRY.map((model) => <button key={model.id} className={platform === model.id ? "active" : ""} onClick={() => setPlatform(model.id)}>{model.name}</button>)}
        <button className="reserved" disabled title="在模型注册表中增加适配器后自动启用">＋ 接入位</button>
      </div>
      <section className="filters">
        <label>问题范围<select value={question} onChange={(event) => changeQuestion(event.target.value)}><option>全部问题</option>{questions.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>自然日<select value={date} onChange={(event) => setDate(event.target.value)}><option value="all">全部日期</option>{dates.map((item) => <option key={item}>{item}</option>)}</select></label>
        {platform === "yuanbao" && <label>元宝设备<select value={device} onChange={(event) => setDevice(event.target.value)}><option value="all">全部设备</option>{yuanbao.devices.map((item) => <option value={item.serial} key={item.serial}>{item.serial}</option>)}</select></label>}
      </section>
      <nav>{nav.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} onClick={() => setView(item.id)}><span>{item.symbol}</span>{item.label}</button>)}</nav>
      <div className="side-status">{MODEL_REGISTRY.map((model) => <span key={model.id}>{model.name} <i className={control?.models[model.id]?.running ? "live" : ""} />{control?.models[model.id]?.running ? "运行中" : "已停止"}</span>)}<small>10 秒自动刷新 · {lastRefresh || "等待"}</small></div>
    </aside>

    <section className="workspace">
      <header className="topbar"><div><p>MULTI-MODEL INTELLIGENCE</p><h1>{nav.find((item) => item.id === view)?.label}</h1><span>{question} · {date === "all" ? "全部日期" : date}</span></div><button onClick={() => load()} disabled={loading}>{loading ? "读取中" : "刷新数据"}</button></header>
      <div className="content">
        {view !== "control" && <section className="kpi-grid">
          <Kpi label="有效回答轮次" value={fmt(totalRuns)} note={`${visibleDoubao ? `豆包 ${fmt(doubaoRuns)}` : ""}${platform === "all" ? " · " : ""}${visibleYuanbao ? `元宝 ${fmt(yuanbaoRuns.length)}` : ""}`} />
          <Kpi label="信源引用" value={fmt(totalSources)} note="当前模型、问题、设备与日期范围" tone="blue" />
          <Kpi label="监控问题" value={platform === "doubao" ? doubao.question_count : platform === "yuanbao" ? yuanbao.question_count : questions.length} note="跨模型合并同名问题" tone="amber" />
          <Kpi label="采集账号 / 设备" value={(visibleDoubao ? doubao.account_count : 0) + (visibleYuanbao ? yuanbao.device_count : 0)} note="豆包账号与元宝 MuMu 实例" tone="violet" />
        </section>}

        {view === "overview" && <>
          <section className="panel comparison"><PanelTitle eyebrow="MODEL COMPARISON" title="双模型数据对比" meta="口径按当前筛选范围" />
            <div className="model-cards">
              {visibleDoubao && <article className="model-card doubao"><div><span>豆包</span><em>{control?.models.doubao.running ? "采集中" : "已就绪"}</em></div><strong>{fmt(doubaoRuns)}<small>轮回答</small></strong><ul><li><b>{fmt(doubaoRefs)}</b> 信源引用</li><li><b>{date === "all" ? fmt(doubao.unique_links) : "按日"}</b> 唯一链接</li><li><b>{fmt(doubaoProductMentions)}</b> 产品提及</li></ul></article>}
              {visibleYuanbao && <article className="model-card yuanbao"><div><span>元宝</span><em>{control?.models.yuanbao.running ? "采集中" : "已就绪"}</em></div><strong>{fmt(yuanbaoRuns.length)}<small>轮回答</small></strong><ul><li><b>{fmt(yuanbaoSources.length)}</b> 信源引用</li><li><b>{fmt(new Set(yuanbaoSources.map((item) => item.canonical_url)).size)}</b> 唯一链接</li><li><b>{fmt(yuanbaoRuns.reduce((sum, run) => sum + run.products.length, 0))}</b> 产品提及</li></ul></article>}
              {platform === "all" && Array.from({ length: RESERVED_MODEL_SLOTS }, (_, index) => <article className="model-card reserved-card" key={`reserved-${index}`}><div><span>新模型接入位 {index + 1}</span><em>已预留</em></div><strong>＋<small>等待数据适配器</small></strong><p>注册模型后自动加入切换、对比、状态和采集控制。</p></article>)}
            </div>
          </section>
          <section className="two-col">
            <article className="panel"><PanelTitle title="最近自然日" meta="北京时区 UTC+8" /><div className="day-list">
              {visibleDoubao && doubaoSourceDays.slice(0, 4).map((row) => <div key={`d-${row.date}`}><i className="dot doubao" /><b>{row.date}</b><span>豆包 {fmt(row.runs)} 轮</span><strong>{fmt(row.value)} 信源</strong></div>)}
              {visibleYuanbao && yuanbaoDays.slice(0, 4).map((row) => <div key={`y-${row.date}`}><i className="dot yuanbao" /><b>{row.date}</b><span>元宝 {fmt(row.runs)} 轮</span><strong>{fmt(row.sources)} 信源</strong></div>)}
              {!doubaoSourceDays.length && !yuanbaoDays.length && <Empty text="暂无按日数据" />}
            </div></article>
            <article className="panel"><PanelTitle title="运行质量" meta="失败与待处理记录不隐藏" /><div className="quality-grid">
              <div><span>豆包最新轮次</span><b>#{doubao.latest_run_no || "—"}</b><small>{doubao.latest_complete === "True" ? "抓取完整" : "等待校验"}</small></div>
              <div><span>元宝成功率</span><b>{pct(yuanbao.successful_runs, yuanbao.total_runs)}</b><small>{yuanbao.successful_runs}/{yuanbao.total_runs} 轮</small></div>
              <div><span>豆包待保存</span><b>{doubao.capture_skips.pending_save_count || 0}</b><small>后台待恢复</small></div>
              <div><span>元宝 AI 分析</span><b>{yuanbao.ai_analysis?.status === "ready" ? "正常" : "待恢复"}</b><small>{yuanbao.ai_analysis?.model || "DeepSeek"}</small></div>
            </div></article>
          </section>
        </>}

        {view === "daily" && <section className="panel"><PanelTitle eyebrow="BEIJING DAILY ARCHIVE" title="一天一天的数据归档" meta={`${dates.length} 个自然日`} />
          <div className="daily-table table-wrap"><table><thead><tr><th>日期</th><th>模型</th><th>回答轮次</th><th>信源引用</th><th>唯一信源</th><th>产品提及</th><th>问题 / 设备</th></tr></thead><tbody>
            {visibleDoubao && doubaoSourceDays.map((row) => { const products = doubaoProductDays.find((item) => item.date === row.date); return <tr key={`db-${row.date}`}><td><b>{row.date}</b></td><td><span className="badge doubao">豆包</span></td><td>{fmt(row.runs)}</td><td>{fmt(row.value)}</td><td>按明细去重</td><td>{fmt(products?.value || 0)}</td><td>{question}</td></tr>; })}
            {visibleYuanbao && yuanbaoDays.map((row) => <tr key={`yb-${row.date}`}><td><b>{row.date}</b></td><td><span className="badge yuanbao">元宝</span></td><td>{fmt(row.runs)}</td><td>{fmt(row.sources)}</td><td>{fmt(row.unique_sources)}</td><td>{fmt(row.product_mentions)}</td><td>{row.question_count} 问题 · {row.device_count} 设备</td></tr>)}
          </tbody></table></div>
        </section>}

        {view === "sources" && <>
          <section className="two-col">
            {visibleDoubao && <article className="panel"><PanelTitle eyebrow="DOUBAO" title="豆包信源类型" meta={`${fmt(doubaoRefs)} 次引用`} /><Bars items={doubaoTypes} total={doubaoRefs} /></article>}
            {visibleYuanbao && <article className="panel"><PanelTitle eyebrow="YUANBAO" title="元宝信源类型" meta={`${fmt(yuanbaoSources.length)} 次引用`} /><Bars items={yuanbaoTypes} total={yuanbaoSources.length} /></article>}
          </section>
          <section className="two-col">
            {visibleDoubao && <article className="panel"><PanelTitle title="豆包头部媒体" meta={`${doubaoMedia.length} 家媒体`} /><Bars items={doubaoMedia} total={doubaoRefs} /></article>}
            {visibleYuanbao && <article className="panel"><PanelTitle title="元宝头部媒体" meta={`${yuanbaoMedia.length} 家媒体`} /><Bars items={yuanbaoMedia} total={yuanbaoSources.length} /></article>}
          </section>
          <section className="panel"><PanelTitle title="最新信源证据" meta="标题、媒体、类型和原始链接" /><div className="source-list">
            {visibleDoubao && doubao.latest_items.slice(0, 12).map((item, index) => <a href={item.href} target="_blank" rel="noreferrer" key={`db-${index}`}><i className="dot doubao" /><div><b>{item.title || item.href}</b><span>豆包 · {item.media} · {item.source_type}</span></div><em>↗</em></a>)}
            {visibleYuanbao && yuanbaoSources.slice(-12).reverse().map((item, index) => <a href={item.url} target="_blank" rel="noreferrer" key={`yb-${index}`}><i className="dot yuanbao" /><div><b>{item.title || item.url}</b><span>元宝 · {item.media} · {item.type}</span></div><em>↗</em></a>)}
          </div></section>
        </>}

        {view === "products" && <>
          <section className="two-col">
            {visibleDoubao && <article className="panel"><PanelTitle eyebrow="DOUBAO" title="豆包品牌提及率" meta={`${date === "all" ? fmt(doubao.products.unique_brands) : doubaoBrands.length} 个品牌`} />{date === "all" ? <ProductTable rows={doubao.products.by_brand.slice(0, 20)} totalRuns={doubao.products.total_product_runs} /> : <Bars items={doubaoBrands} total={doubaoRuns} limit={12} />}</article>}
            {visibleYuanbao && <article className="panel"><PanelTitle eyebrow="YUANBAO" title="元宝品牌提及率" meta={`${yuanbaoBrands.length} 个品牌`} /><Bars items={yuanbaoBrands} total={yuanbaoRuns.length} limit={12} /></article>}
          </section>
          <section className="panel"><PanelTitle title="产品排名与提及轮次" meta="同一轮同一产品只计一次" /><ProductTable rows={platform === "doubao" ? doubao.products.by_product : platform === "yuanbao" ? yuanbaoProducts : [...doubao.products.by_product.slice(0, 15), ...yuanbaoProducts.slice(0, 15)]} totalRuns={platform === "doubao" ? doubao.products.total_product_runs : yuanbaoRuns.length} /></section>
        </>}

        {view === "runs" && <section className="panel"><PanelTitle title="最近采集明细" meta="按完成时间倒序" />
          <div className="run-list">
            {visibleDoubao && <article><div><span className="badge doubao">豆包</span><b>第 {doubao.latest_run_no} 轮</b><time>{shortTime(doubao.latest_run_time)}</time></div><h3>{doubao.latest_question || "等待采集"}</h3><p>{doubao.latest_items.length} 条信源 · {doubao.products.latest_products.length} 个产品 · {doubao.latest_complete === "True" ? "本轮完整" : "本轮待核对"}</p></article>}
            {visibleYuanbao && [...yuanbaoRuns].reverse().slice(0, 30).map((run) => <article key={run.run_id}><div><span className="badge yuanbao">元宝</span><b>第 {run.sequence} 轮</b><time>{shortTime(run.finished_at)}</time></div><h3>{run.question}</h3><p>{run.sources.length} 条信源 · {run.products.length} 个产品 · {run.serial}</p><details><summary>查看回答正文</summary><div>{run.web_body || run.reply || "未保存正文"}</div></details></article>)}
          </div>
        </section>}

        {view === "control" && <section className="control-grid">
          <article className="panel launch-intro"><PanelTitle eyebrow="ONE CONTROL CENTER" title="一个面板启动两套采集" /><p>启动任务会沿用各自已有的问题计划、账号会话和断点。停止任务只结束本次进程，已经成功保存的数据不会丢失。</p><div className="round-control"><label>元宝本次轮数<input type="number" min="1" max="10000" value={rounds} onChange={(event) => setRounds(Math.max(1, Number(event.target.value) || 1))} /></label><span>豆包轮数读取现有豆包配置</span></div>{actionMessage && <div className="action-message">{actionMessage}</div>}</article>
          {MODEL_REGISTRY.filter((model) => model.supportsControl).map((model) => { const state = control?.models[model.id]; return <article className={`panel job-card ${state?.running ? "running" : ""}`} key={model.id}><div className="job-head"><div className={`model-icon ${model.tone}`}>{model.shortName}</div><div><h2>{model.name}采集</h2><span><i />{state?.running ? `运行中 · PID ${state.pid}` : state?.ready ? "配置就绪" : "缺少运行配置"}</span></div></div><dl><div><dt>启动时间</dt><dd>{state?.started_at ? shortTime(state.started_at) : "—"}</dd></div><div><dt>上次退出码</dt><dd>{state?.last_exit_code ?? "—"}</dd></div></dl><div className="job-actions"><button className="start" disabled={state?.running || !state?.ready} onClick={() => controlAction(model.id, "start")}>启动采集</button><button className="stop" disabled={!state?.running} onClick={() => controlAction(model.id, "stop")}>停止任务</button></div><small className="log-path">运行日志：{state?.log || "启动后生成"}</small></article>; })}
          {Array.from({ length: RESERVED_MODEL_SLOTS }, (_, index) => <article className="panel job-card reserved-job" key={`control-reserved-${index}`}><div className="job-head"><div className="model-icon reserved">＋</div><div><h2>新模型采集位 {index + 1}</h2><span><i />等待接入</span></div></div><p>实现数据适配器和启动器后，这里会自动出现状态与控制按钮。</p></article>)}
        </section>}
      </div>
    </section>
  </main>;
}

function ProductTable({ rows, totalRuns }: { rows: ProductRow[]; totalRuns: number }) {
  if (!rows.length) return <Empty text="当前范围暂无已审核产品" />;
  return <div className="table-wrap"><table><thead><tr><th>产品 / 品牌</th><th>提及轮次</th><th>提及率</th><th>平均排名</th><th>最佳排名</th></tr></thead><tbody>{rows.slice(0, 30).map((row, index) => {
    const runCount = row.run_count ?? row.mention_runs ?? row.count ?? 0;
    const rate = row.run_rate ?? row.mention_rate ?? (runCount * 100 / Math.max(1, totalRuns));
    return <tr key={`${row.brand || ""}-${row.product_name || row.name}-${index}`}><td><b>{row.name || [row.brand, row.product_name].filter(Boolean).join(" · ") || "未命名"}</b><small>{row.category || (row.product_name ? "产品" : "品牌")}</small></td><td>{fmt(runCount)}</td><td><strong className="rate">{Number(rate).toFixed(1)}%</strong></td><td>{row.avg_rank || row.average_rank ? `第 ${Number(row.avg_rank ?? row.average_rank).toFixed(2)} 位` : "—"}</td><td>{row.best_rank ? `第 ${row.best_rank} 位` : "—"}</td></tr>;
  })}</tbody></table></div>;
}
