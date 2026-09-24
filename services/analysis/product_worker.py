"""Continuously repair pending product analysis directly in PostgreSQL."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from legacy.doubao.core import doubao_env_loader  # noqa: F401
from legacy.doubao.core import save_doubao_refs as saver
from monitor_core.database import (
    complete_product_provider_reviews,
    defer_product_analysis,
    mark_product_provider_unavailable,
    pending_product_runs,
    recover_product_provider,
    update_product_analysis,
    verified_product_runs,
)
from monitor_core.product_analysis import (
    batch_model_review,
    build_knowledge,
    deterministic_review,
)

ROOT = PROJECT_ROOT
LOCK = ROOT / "runtime" / "remote_product_ai_worker.lock"
LOG = ROOT / "runtime" / "remote_product_ai_worker.log"
RATE_STATE = ROOT / "runtime" / "remote_product_ai_rate_state.json"


def process_lock_token(pid: int | None = None) -> str:
    """Identify a process instance, not only its reusable PID."""
    process_id = int(pid or os.getpid())
    if os.name != "nt":
        try:
            start_ticks = Path(f"/proc/{process_id}/stat").read_text(
                encoding="ascii"
            ).split()[21]
            return f"{process_id}:{start_ticks}"
        except (OSError, IndexError):
            pass
    return str(process_id)


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + message + "\n")


def dashboard_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=2):
            return True
    except OSError:
        return False


def provider_unavailable_error(exc: Exception) -> bool:
    """Return whether retrying now would only waste paid API calls."""
    message = str(exc or "").casefold()
    return any(token in message for token in (
        "http error 401",
        "http error 402",
        "http error 403",
        "http error 429",
        "insufficient",
        "balance",
        "quota",
        "credit",
        "payment required",
        "invalid api key",
        "authentication",
        "余额",
        "欠费",
        "额度",
    ))


def acquire_lock() -> bool:
    try:
        if LOCK.exists():
            owner = LOCK.read_text(encoding="utf-8").strip()
            pid = int(owner.split(":", 1)[0])
            if pid > 0:
                if ":" in owner:
                    if process_lock_token(pid) == owner:
                        return False
                elif not (Path("/.dockerenv").exists() and pid == 1):
                    os.kill(pid, 0)
                    return False
    except (OSError, ValueError):
        pass
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(process_lock_token(), encoding="utf-8")
    return True


def reserve_hourly_slots(requested: int, hourly_limit: int) -> int:
    """Persistently reserve bounded analysis attempts in a rolling hour."""
    now = time.time()
    calls: list[float] = []
    try:
        value = json.loads(RATE_STATE.read_text(encoding="utf-8-sig"))
        calls = [float(item) for item in value.get("calls", []) if now - float(item) < 3600]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    allowed = max(0, min(max(0, hourly_limit - len(calls)), requested))
    if allowed:
        calls.extend([now] * allowed)
    RATE_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = RATE_STATE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"calls": calls, "hourly_limit": hourly_limit}, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(RATE_STATE)
    return allowed


def main() -> int:
    if not acquire_lock():
        return 0
    repair_days = [
        value.strip() for value in os.environ.get("REMOTE_PRODUCT_AI_DAYS", "").split(",")
        if value.strip()
    ]
    repair_models = {
        value.strip().casefold()
        for value in os.environ.get("REMOTE_PRODUCT_AI_MODELS", "").split(",")
        if value.strip()
    }
    scan_batch = max(10, min(100, int(os.environ.get("REMOTE_PRODUCT_AI_SCAN_BATCH", "100"))))
    model_batch = max(1, min(8, int(os.environ.get("REMOTE_PRODUCT_AI_MODEL_BATCH", "8"))))
    interval = max(10 if repair_days else 30, int(os.environ.get("REMOTE_PRODUCT_AI_INTERVAL", "60")))
    # Three attempts left hundreds of difficult historical rows permanently
    # invisible to the worker. Keep the paid-call rate cap unchanged, but give
    # grounded re-review enough chances to recover transient/model omissions.
    max_retries = max(1, min(10, int(os.environ.get("REMOTE_PRODUCT_AI_MAX_RETRIES", "10"))))
    retry_delay = max(300, int(os.environ.get("REMOTE_PRODUCT_AI_RETRY_DELAY", "900")))
    hourly_limit = max(1, min(60, int(os.environ.get("REMOTE_PRODUCT_AI_CALLS_PER_HOUR", "10"))))
    provider_backoff = max(
        300, int(os.environ.get("REMOTE_PRODUCT_AI_PROVIDER_BACKOFF", "1800"))
    )
    paid_backoff_until = 0.0
    dashboard_was_down = False
    knowledge = build_knowledge(verified_product_runs())
    knowledge_updated_at = time.monotonic()
    log(f"start: storage=postgresql mode=algorithm_then_paid_llm local_model=disabled "
        f"scan_batch={scan_batch} model_batch={model_batch} "
        f"max_retries={max_retries} retry_delay={retry_delay} "
        f"hourly_limit={hourly_limit} provider_backoff={provider_backoff} "
        f"days={repair_days or 'all'} models={sorted(repair_models) or 'all'}")
    try:
        while True:
            if not dashboard_running():
                if not dashboard_was_down:
                    log("pause: dashboard unavailable; worker remains alive")
                dashboard_was_down = True
                time.sleep(10)
                continue
            if dashboard_was_down:
                log("resume: dashboard available")
                dashboard_was_down = False
            if time.monotonic() - knowledge_updated_at >= 300:
                knowledge = build_knowledge(verified_product_runs())
                knowledge_updated_at = time.monotonic()
            pending = pending_product_runs(scan_batch, max_retries=max_retries,
                                           days=repair_days or None)
            if not pending and repair_days:
                # During a target-date repair, keep current-day ingestion
                # moving whenever every target row is either cooling down or
                # already complete.
                pending = pending_product_runs(scan_batch, max_retries=max_retries)
            if repair_models:
                pending = [
                    run for run in pending
                    if str(run.get("model_id") or "").casefold() in repair_models
                ]

            ambiguous = []
            algorithm_count = 0
            for run in pending:
                try:
                    answer = str(run.get("answer") or "")
                    question = str(run.get("question") or "")
                    if not answer.strip():
                        update_product_analysis(str(run["model_id"]), str(run["run_id"]), [],
                                                "no_answer", "", "none")
                        continue
                    rule_products = saver.extract_products(answer)
                    products, method = deterministic_review(
                        answer, question, knowledge, rule_products,
                        saver.numbered_product_block_count(answer),
                    )
                    # Cross-day back-testing showed that dual-parser consensus is
                    # not accurate enough to certify a new answer. Only an exact
                    # question+answer match previously verified by the paid model
                    # can pass with zero tokens; everything else waits for the
                    # paid, grounded batch reviewer. There is no local LLM layer.
                    if products is None or method != "verified_answer_reuse":
                        ambiguous.append(run)
                        continue
                    saver.validate_grounded_ai_products(answer, products)
                    update_product_analysis(str(run["model_id"]), str(run["run_id"]), products,
                                            "ai_verified", "deterministic-v1", method)
                    algorithm_count += 1
                    log(json.dumps({"model": run["model_id"], "run_id": run["run_id"],
                                    "status": "ai_verified", "method": method,
                                    "products": len(products), "llm_calls": 0}, ensure_ascii=False))
                except (KeyError, OSError, ValueError) as exc:
                    # One stale or no-longer-grounded reuse candidate must not
                    # terminate the global analysis worker. Send it through the
                    # grounded reviewer while the remaining backlog continues.
                    ambiguous.append(run)
                    log(json.dumps({
                        "model": run.get("model_id"), "run_id": run.get("run_id"),
                        "status": "deterministic_reuse_rejected",
                        "error": str(exc),
                    }, ensure_ascii=False))

            paid_items = [
                {
                    **run,
                    "id": str(index),
                    "full_context": int(run.get("_product_ai_retry_count") or 0) > 0,
                }
                for index, run in enumerate(ambiguous[:model_batch])
            ]
            paid_available = time.monotonic() >= paid_backoff_until
            if paid_items and paid_available and reserve_hourly_slots(1, hourly_limit):
                try:
                    paid_results, usage = batch_model_review(paid_items, knowledge)
                    rejected_rows = 0
                    for item in paid_items:
                        item_id = str(item["id"])
                        if item_id not in paid_results:
                            rejected_rows += 1
                            defer_product_analysis(
                                str(item["model_id"]), str(item["run_id"]), retry_delay
                            )
                            continue
                        products = paid_results[item_id]
                        update_product_analysis(
                            str(item["model_id"]), str(item["run_id"]), products,
                            "ai_verified", str(usage.get("model") or ""),
                            "llm_batch_grounded",
                        )
                    status = "llm_batch_partial" if rejected_rows else "llm_batch_complete"
                    log(json.dumps({"status": status, **usage}, ensure_ascii=False))
                    try:
                        recovered = recover_product_provider("deepseek")
                        if recovered:
                            log(json.dumps({
                                "status": "provider_recovered_review_started",
                                "provider": "deepseek",
                                "incident_id": recovered.get("id"),
                                "affected_runs": recovered.get("affected_runs", 0),
                            }, ensure_ascii=False))
                    except Exception as incident_exc:
                        log(json.dumps({
                            "status": "provider_incident_state_error",
                            "action": "recover",
                            "error": str(incident_exc),
                        }, ensure_ascii=False))
                except Exception as exc:
                    unavailable = provider_unavailable_error(exc)
                    if unavailable:
                        # Billing/auth/quota failures are provider-wide. Do not
                        # poison every row's retry counter or repeatedly spend
                        # requests; leave the rows pending and cool down once.
                        paid_backoff_until = time.monotonic() + provider_backoff
                        try:
                            incident = mark_product_provider_unavailable("deepseek", str(exc))
                            log(json.dumps({
                                "status": "provider_outage_marked",
                                "provider": "deepseek",
                                "incident_id": incident.get("id"),
                                "started_at": str(incident.get("started_at") or ""),
                                "affected_runs": incident.get("affected_runs", 0),
                            }, ensure_ascii=False))
                        except Exception as incident_exc:
                            log(json.dumps({
                                "status": "provider_incident_state_error",
                                "action": "mark",
                                "error": str(incident_exc),
                            }, ensure_ascii=False))
                    else:
                        for run in paid_items:
                            defer_product_analysis(
                                str(run["model_id"]), str(run["run_id"]), retry_delay
                            )
                    log(json.dumps({
                        "status": "llm_batch_error",
                        "items": len(paid_items),
                        "provider_backoff": provider_backoff if unavailable else 0,
                        "rows_deferred": 0 if unavailable else len(paid_items),
                        "error": str(exc),
                    }, ensure_ascii=False))
            elif paid_items and not paid_available:
                # Silent by design: one log entry at the original failure is
                # enough, and repeated messages would hide useful audit data.
                pass
            elif paid_items:
                log(json.dumps({"status": "rate_limited", "paid_items": len(paid_items),
                                "hourly_limit": hourly_limit}, ensure_ascii=False))
            if algorithm_count or paid_items:
                # Refresh after a large deterministic pass so exact-answer reuse
                # is available without waiting five minutes.
                knowledge = build_knowledge(verified_product_runs())
                knowledge_updated_at = time.monotonic()
            try:
                completed = complete_product_provider_reviews("deepseek")
                if completed:
                    log(json.dumps({
                        "status": "provider_recheck_complete",
                        "provider": "deepseek",
                        "incident_id": completed.get("id"),
                        "affected_runs": completed.get("affected_runs", 0),
                    }, ensure_ascii=False))
            except Exception as incident_exc:
                log(json.dumps({
                    "status": "provider_incident_state_error",
                    "action": "complete",
                    "error": str(incident_exc),
                }, ensure_ascii=False))
            time.sleep(interval if pending else max(interval, 60))
    except Exception as exc:
        log(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            if LOCK.read_text(encoding="utf-8").strip() == process_lock_token():
                LOCK.unlink(missing_ok=True)
        except OSError:
            pass
        log("stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
