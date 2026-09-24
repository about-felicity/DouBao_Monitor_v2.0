"""Employee performance derived from monthly average daily GEO rates."""

from __future__ import annotations

from typing import Any, Iterable


PERFORMANCE_SCHEMA_VERSION = 7
MODEL_PERFORMANCE_START_DATES = {
    # The Baidu-card collector only became stable on this date.  Earlier
    # sparse card samples remain visible in analytics, but must not inflate
    # employee performance.
    "wenxin": "2026-09-16",
}
MODEL_ORDER = ("deepseek", "wenxin", "yuanbao")
MODEL_LABELS = {
    "deepseek": "DeepSeek",
    "wenxin": "文心",
    "yuanbao": "元宝",
}

# Percent weights. Missing model observations are excluded and the remaining
# weights are renormalized, so missing collection can never become a zero score.
ARTICLE_WEIGHTS: dict[str, dict[str, int]] = {
    "佳佳": {"deepseek": 20, "wenxin": 10, "yuanbao": 50},
    "杨涛": {"deepseek": 20, "wenxin": 40, "yuanbao": 15},
    "小美": {"deepseek": 50, "wenxin": 20, "yuanbao": 15},
    "鑫悦": {"deepseek": 15, "wenxin": 15, "yuanbao": 15},
    "谭露": {"deepseek": 50, "wenxin": 20, "yuanbao": 15},
}
VIDEO_OWNERS = ("小佳佳", "周娜", "庭龙")


def _targets(deepseek: float, doubao: float | None, quark: float, wenxin: float, yuanbao: float) -> dict[str, float]:
    return {"deepseek": deepseek, "wenxin": wenxin, "yuanbao": yuanbao}


PRODUCT_RULES: tuple[dict[str, Any], ...] = (
    {"product": "科熙本二硫化硒洗发水", "article_owner": "", "video_owner": "", "question": "二硫化硒洗发水推荐", "targets": _targets(45, None, 40, 80, 20)},
    {"product": "姿生怡卸妆油", "article_owner": "庭龙", "video_owner": "庭龙", "question": "卸妆油推荐", "targets": _targets(15, 3, 30, 55, 10)},
    {"product": "梵玢护发精油", "article_owner": "杨涛", "video_owner": "", "question": "护发精油推荐", "targets": _targets(10, None, 75, 30, 10)},
    {"product": "科熙本鱼子酱修护柔顺护发素", "article_owner": "小美", "video_owner": "", "question": "护发素推荐", "targets": _targets(15, None, 80, 30, 10)},
    {"product": "姿生怡手部保湿霜", "article_owner": "", "video_owner": "", "question": "护手霜推荐", "targets": _targets(10, None, 20, 15, 10)},
    {"product": "科熙本控油蓬松洗发水", "article_owner": "谭露", "video_owner": "", "question": "控油蓬松洗发水推荐", "targets": _targets(15, None, 60, 15, 10)},
    {"product": "梵玢染发剂（含黑茶色）", "performance_group": "染发剂", "article_owner": "杨涛", "video_owner": "周娜", "question": "染发剂推荐", "targets": _targets(40, 10, 70, 50, 10)},
    {"product": "科熙本染发剂", "performance_group": "染发剂", "article_owner": "杨涛", "video_owner": "周娜", "question": "染发剂推荐", "targets": _targets(10, 5, 20, 50, 10)},
    {"product": "梵玢沐浴油", "article_owner": "小佳佳", "video_owner": "小佳佳", "question": "沐浴精油推荐", "targets": _targets(20, 3, 90, 30, 20)},
    {"product": "姿生怡洗面奶", "article_owner": "", "video_owner": "", "question": "洗面奶推荐", "targets": _targets(10, None, 20, 15, 10)},
    {"product": "姿生怡阿尔卑斯冰川焕肤精粹水", "article_owner": "小美", "video_owner": "", "question": "爽肤水推荐", "targets": _targets(15, None, 30, 25, 10)},
    {"product": "梵玢眉毛精华液", "article_owner": "鑫悦", "video_owner": "鑫悦", "question": "眉毛增长液推荐", "targets": _targets(40, 20, 99, 95, 40)},
    {"product": "姿生怡眼霜", "article_owner": "小美", "video_owner": "", "question": "眼霜推荐", "targets": _targets(10, None, 20, 20, 10)},
    {"product": "梵玢睫毛精华液", "article_owner": "鑫悦", "video_owner": "鑫悦", "question": "睫毛增长液推荐", "targets": _targets(80, 20, 95, 90, 85)},
    {"product": "梵玢祛痘精华", "article_owner": "谭露", "video_owner": "", "question": "祛痘精华液推荐", "targets": _targets(10, None, 70, 85, 60)},
    {"product": "焕颜计小白罐", "article_owner": "小美", "video_owner": "", "question": "美白面霜推荐", "targets": _targets(40, None, 90, 85, 10)},
    {"product": "姿生怡身体乳", "article_owner": "", "video_owner": "", "question": "身体乳推荐", "targets": _targets(10, None, 20, 15, 10)},
    {"product": "科熙本控油蓬松造型喷雾", "article_owner": "谭露", "video_owner": "谭露", "question": "造型喷雾推荐", "targets": _targets(40, 3, 90, 90, 25)},
    {"product": "茗媛萃防晒霜", "article_owner": "", "video_owner": "", "question": "防晒霜推荐", "targets": _targets(10, None, 20, 15, 10)},
    {"product": "梵玢洗发水", "performance_group": "防脱洗发水", "article_owner": "佳佳", "video_owner": "周娜", "question": "防脱洗发水推荐", "targets": _targets(10, 5, 20, 15, 10)},
    {"product": "道和小绿瓶", "performance_group": "防脱洗发水", "article_owner": "佳佳", "video_owner": "周娜", "question": "防脱洗发水推荐", "targets": _targets(10, 10, 55, 15, 10)},
    {"product": "梵玢焕活精华液", "performance_group": "防脱精华液", "article_owner": "佳佳", "video_owner": "周娜", "question": "防脱精华液推荐", "targets": _targets(10, 2, 85, 45, 10)},
    {"product": "道和小红瓶", "performance_group": "防脱精华液", "article_owner": "佳佳", "video_owner": "周娜", "question": "防脱精华液推荐", "targets": _targets(10, 3, 85, 85, 10)},
    {"product": "姿生怡鱼子酱面膜", "article_owner": "佳佳", "video_owner": "", "question": "面膜推荐", "targets": _targets(10, None, 20, 15, 10)},
)


def _actual_rate(row: dict[str, Any], model_id: str) -> float | None:
    status = dict((row.get("models") or {}).get(model_id) or {})
    if model_id == "wenxin" and status.get("surfaces"):
        def surface_rate(surface_id: str) -> float | None:
            surface = dict((status.get("surfaces") or {}).get(surface_id) or {})
            if "average_daily_mention_rate" in surface:
                # A day with a positive answer denominator is an effective day,
                # including a valid 0% recommendation result.
                if int(surface.get("observed_days") or 0) <= 0:
                    return None
                return float(surface.get("average_daily_mention_rate") or 0)
            if int(surface.get("eligible_runs") or 0) <= 0:
                return None
            return float(surface.get("recommendation_rate") or 0)

        wenxin_rate = surface_rate("wenxin_card")
        if wenxin_rate is None:
            # Baidu is a 50% supplement to the Wenxin result, not a standalone
            # substitute. A lone sparse Baidu hit must never create a Wenxin
            # performance score when the primary Wenxin card has no answer.
            return None
        baidu_rate = surface_rate("baidu_card")
        surface_rates = [wenxin_rate] if baidu_rate is None else [wenxin_rate, baidu_rate]
        # Both cards available: 50% + 50%. If Baidu has no denominator, the
        # Wenxin card is the sole available observation and therefore 100%.
        return sum(surface_rates) / len(surface_rates)
    # Product summary rows use an equal-weight average of each effective day's
    # probability.  This is the exact number displayed in the monthly board.
    if "average_daily_mention_rate" in status:
        if int(status.get("observed_days") or 0) <= 0:
            return None
        return float(status.get("average_daily_mention_rate") or 0)
    if int(status.get("eligible_runs") or 0) <= 0:
        return None
    return float(status.get("recommendation_rate") or 0)


def _person_result(
    name: str,
    role: str,
    weights: dict[str, int],
    rules: Iterable[dict[str, Any]],
    rows_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    assigned = list(rules)
    grouped_rules: dict[str, list[dict[str, Any]]] = {}
    for rule in assigned:
        group = str(rule.get("performance_group") or rule["product"])
        grouped_rules.setdefault(group, []).append(rule)
    breakdown = []
    weighted_score = 0.0
    applied_weight = 0
    observed_items = 0
    target_items = 0
    for model_id in MODEL_ORDER:
        weight = int(weights.get(model_id) or 0)
        if weight <= 0:
            continue
        items = []
        model_groups = {
            group: [
                rule for rule in group_rules
                if (rule.get("targets") or {}).get(model_id) is not None
                and float((rule.get("targets") or {}).get(model_id) or 0) > 0
            ]
            for group, group_rules in grouped_rules.items()
        }
        model_groups = {group: candidates for group, candidates in model_groups.items() if candidates}
        target_items += len(model_groups)
        for group, candidates in model_groups.items():
            observed_candidates = []
            for rule in candidates:
                target = float((rule.get("targets") or {})[model_id])
                row = rows_by_key.get((str(rule["question"]), str(rule["product"])))
                actual = _actual_rate(row, model_id) if row else None
                if actual is not None:
                    observed_candidates.append((actual, rule, target))
            if not observed_candidates:
                continue
            # Same-category products contribute one item. Select the brand with
            # the highest displayed monthly daily-average probability first,
            # then compare that selected product against its own target.
            actual, rule, target = max(
                observed_candidates,
                key=lambda value: (value[0], str(value[1]["product"])),
            )
            attainment = min(200.0, actual * 100.0 / target)
            observed_items += 1
            items.append({
                "product": rule["product"], "question": rule["question"],
                "performance_group": group,
                "group_products": [candidate["product"] for candidate in candidates],
                "actual_rate": round(actual, 1), "target_rate": target,
                "attainment": round(attainment, 1),
            })
        score = round(sum(item["attainment"] for item in items) / len(items), 1) if items else None
        if score is not None:
            weighted_score += score * weight
            applied_weight += weight
        breakdown.append({
            "model_id": model_id, "model": MODEL_LABELS[model_id], "weight": weight,
            "score": score, "observed_items": len(items),
            "target_items": len(model_groups),
            "items": items,
        })
    overall = round(weighted_score / applied_weight, 1) if applied_weight else None
    return {
        "name": name, "role": role, "score": overall,
        "status": "no_data" if overall is None else "met" if overall >= 100 else "below",
        "configured_weight": sum(weights.values()), "applied_weight": applied_weight,
        "assigned_products": len({str(rule["product"]) for rule in assigned}),
        "observed_items": observed_items, "target_items": target_items,
        "models": breakdown,
    }


def daily_employee_performance(
    rows: Iterable[dict[str, Any]], *, date: str = "",
    model_rows: dict[str, Iterable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build month-to-date performance from product summary rows.

    The historical function name is retained for API compatibility.  When
    passed daily rows it still supports the former behavior for callers and
    tests, but the dashboard now passes the monthly product summary.
    """
    rows = list(rows)
    if model_rows:
        # Keep the normal monthly summary for every model, then replace only
        # the explicitly supplied model cells with a summary calculated from
        # that model's valid performance window.  Missing override rows remove
        # the model observation instead of silently falling back to history.
        override_maps = {
            model_id: {
                (str(row.get("question") or ""), str(row.get("product") or "")): row
                for row in override_rows
            }
            for model_id, override_rows in model_rows.items()
        }
        replaced_rows = []
        for row in rows:
            replaced = {**row, "models": dict(row.get("models") or {})}
            key = (str(row.get("question") or ""), str(row.get("product") or ""))
            for model_id, override_map in override_maps.items():
                replaced["models"].pop(model_id, None)
                override = override_map.get(key)
                override_status = (override.get("models") or {}).get(model_id) if override else None
                if isinstance(override_status, dict):
                    replaced["models"][model_id] = override_status
            replaced_rows.append(replaced)
        rows = replaced_rows
    summary_mode = any("average_daily_mention_rate" in row for row in rows)
    if summary_mode:
        selected_date = str(date or "")[:7] or max(
            (str(row.get("last_date") or "")[:7] for row in rows), default=""
        )
        selected_rows = rows
    else:
        selected_date = str(date or "")
        if not selected_date:
            selected_date = max((str(row.get("date") or "") for row in rows), default="")
        selected_rows = [row for row in rows if str(row.get("date") or "") == selected_date]
    rows_by_key = {
        (str(row.get("question") or ""), str(row.get("product") or "")): row
        for row in selected_rows
    }
    people = []
    for name, weights in ARTICLE_WEIGHTS.items():
        rules = [rule for rule in PRODUCT_RULES if rule.get("article_owner") == name]
        people.append(_person_result(name, "文章负责人", weights, rules, rows_by_key))
    for name in VIDEO_OWNERS:
        rules = [rule for rule in PRODUCT_RULES if rule.get("video_owner") == name]
        people.append(_person_result(name, "豆包视频负责人", {"doubao": 100}, rules, rows_by_key))
    return {
        "date": selected_date,
        "period_kind": "month" if summary_mode else "day",
        "cap": 200,
        "formula": (
            "单项=当月日均提及率÷目标提及率，最高200%；产品×模型当天无数据则该天不进入日均；"
            "整月无有效数据的考核项不计分，其余有效权重归一化"
            if summary_mode else
            "单项=实际提及率÷目标提及率，最高200%；无当日数据不计分；其余有效权重归一化"
        ),
        "wenxin_scope": (
            "文心绩效自2026-09-16起计算；文心与百度卡片均有有效分母时各占50%；"
            "百度卡片无有效数据时仅按文心卡片计算；文心卡片无有效分母时不计分"
        ),
        "model_start_dates": dict(MODEL_PERFORMANCE_START_DATES),
        "people": people,
    }
