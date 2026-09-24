type PerformanceModel = {
  model_id: string;
  model: string;
  weight: number;
  score: number | null;
  observed_items: number;
  target_items: number;
  items: Array<{
    product: string;
    question: string;
    actual_rate: number;
    target_rate: number;
    attainment: number;
    performance_group?: string;
    group_products?: string[];
  }>;
};

type PerformancePerson = {
  name: string;
  role: string;
  score: number | null;
  status: "met" | "below" | "no_data";
  configured_weight: number;
  applied_weight: number;
  assigned_products: number;
  observed_items: number;
  target_items: number;
  models: PerformanceModel[];
};

export type EmployeePerformance = {
  date: string;
  cap: number;
  period_kind?: "month" | "day";
  formula?: string;
  wenxin_scope?: string;
  people: PerformancePerson[];
};

function scoreTone(score: number | null) {
  if (score === null) return "empty";
  if (score >= 100) return "met";
  if (score >= 80) return "near";
  return "below";
}

export function PerformanceBoard({ payload }: { payload?: EmployeePerformance }) {
  const people = payload?.people || [];
  if (!people.length) return null;
  return (
    <section className="panel performance-board">
      <details className="performance-collapse" open>
        <summary><span className="collapse-open">收起绩效面板</span><span className="collapse-closed">展开绩效面板</span></summary>
        <div className="performance-content">
        <header className="performance-heading">
        <div>
          <span>DAILY PERFORMANCE</span>
          <h2>{payload?.period_kind === "month" ? "当月人员绩效" : "每日人员绩效"}</h2>
          <p>{payload?.formula}</p>
        </div>
        <div className="performance-date"><small>{payload?.period_kind === "month" ? "考核月份" : "考核日期"}</small><b>{payload?.date || "暂无"}</b><em>文心双卡各50%</em></div>
      </header>
      <div className="performance-grid">
        {people.map((person) => {
          const tone = scoreTone(person.score);
          const effectiveModels = person.models.filter((model) => model.score !== null);
          const weightedTerms = effectiveModels.map((model) =>
            `${model.score!.toFixed(1)}×${model.weight}%`
          );
          return (
            <article className={`performance-card ${tone}`} key={person.name}>
              <header><div><b>{person.name}</b><small>{person.role} · {person.assigned_products}个产品</small></div><span>{person.score === null ? "暂无数据" : person.score >= 100 ? "已达标" : "未达标"}</span></header>
              <div className="performance-score"><strong>{person.score === null ? "—" : person.score.toFixed(1)}</strong>{person.score !== null && <i>%</i>}<small>综合绩效</small></div>
              <div className="performance-progress"><i style={{ width: `${Math.min(100, (person.score || 0) / 2)}%` }} /><span /></div>
              <div className="performance-models">
                {person.models.map((item) => (
                  <div className={item.score === null ? "empty" : ""} key={item.model_id}>
                    <span>{item.model}<small>权重 {item.weight}%</small></span>
                    <b>{item.score === null ? "—" : `${item.score.toFixed(1)}%`}</b>
                    <em>{item.observed_items}/{item.target_items}项</em>
                  </div>
                ))}
              </div>
              <footer>
                <span>有效考核 {person.observed_items}/{person.target_items} 项</span>
                <span>有效权重 {person.applied_weight}%{person.applied_weight > 0 && person.applied_weight < person.configured_weight ? " · 已归一化" : ""}</span>
              </footer>
              <div className="performance-final-formula">
                <b>最终得分怎么算</b>
                {person.score === null || !weightedTerms.length ? (
                  <span>当前没有有效考核数据，暂不计算绩效。</span>
                ) : (
                  <>
                    <span>（{weightedTerms.join(" + ")}）÷ {person.applied_weight}%</span>
                    <strong>= {person.score.toFixed(1)}%</strong>
                  </>
                )}
              </div>
              <details className="performance-details">
                <summary>展开完整计算明细</summary>
                <div className="performance-calculation-list">
                  {person.models.map((model) => (
                    <section className={`performance-model-detail ${model.score === null ? "empty" : ""}`} key={model.model_id}>
                      <header>
                        <span><b>{model.model}</b><small>人员权重 {model.weight}% · 有效 {model.observed_items}/{model.target_items} 项</small></span>
                        <strong>{model.score === null ? "不计分" : `模型得分 ${model.score.toFixed(1)}%`}</strong>
                      </header>
                      {model.score === null ? (
                        <p className="performance-no-data">没有有效分母，本模型权重从最终分母中排除。</p>
                      ) : (
                        <>
                          <p className="performance-model-formula">
                            模型得分 = 各有效产品达成率平均 = {model.score.toFixed(1)}%；
                            加权项 = {model.score.toFixed(1)} × {model.weight}%
                          </p>
                          <div className="performance-item-list">
                            {(model.items || []).map((item) => {
                              const met = item.actual_rate >= item.target_rate;
                              const grouped = item.performance_group && item.group_products && item.group_products.length > 1;
                              return <p className={met ? "met" : "below"} key={`${model.model_id}-${item.product}-${item.question}`}>
                                <span>
                                  <b>{grouped ? `${item.performance_group}（同类取最高：${item.product}）` : item.product}</b>
                                  <small>{item.question}{grouped ? ` · 同类：${item.group_products!.join("、")}` : ""}</small>
                                </span>
                                <em>实际概率 {item.actual_rate.toFixed(1)}% ÷ 目标概率 {item.target_rate.toFixed(1)}%</em>
                                <strong>= {item.attainment.toFixed(1)}%</strong>
                              </p>;
                            })}
                          </div>
                        </>
                      )}
                    </section>
                  ))}
                </div>
              </details>
            </article>
          );
        })}
      </div>
        <p className="performance-note">按上方同月份汇总中的日均提及率考核；当天该产品×模型无数据时，该天不纳入平均。每个有效项对照目标，单项最高计200%。{payload?.wenxin_scope}</p>
        </div>
      </details>
    </section>
  );
}
