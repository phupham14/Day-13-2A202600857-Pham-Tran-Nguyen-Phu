"""YOUR mitigation + observability layer. The simulator calls mitigate() around the
opaque agent (a REAL LLM) for every request. This is the ONLY place observability can
live -- the agent is silent. Legal moves: retry / cache / route / guardrail / sanitize
/ fallback / session-reset / PROMPT ROUTING, plus your own logging/tracing/metrics.
Illegal: hardcoding answers, importing the agent internals, reading instructor files,
network exfiltration.

  call_next(question, config) -> result   # the only way to reach the black box
  context = {"session_id","turn_index","qid","cache": <shared dict>, "cache_lock": <Lock>}
  result  = {"answer","status","steps","trace","meta":{latency_ms,usage,...}}
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback

# --- Resolve paths robustly ---
_HERE = os.path.dirname(os.path.abspath(__file__))   # .../solution/
_ROOT = os.path.dirname(_HERE)                         # repo root

# Debug log — writes plain text so we can see errors even if telemetry fails
_DEBUG_LOG = os.path.join(_ROOT, "logs", "wrapper_debug.log")
os.makedirs(os.path.join(_ROOT, "logs"), exist_ok=True)


def _dbg(msg: str) -> None:
    with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


_dbg(f"wrapper loading | _HERE={_HERE} | _ROOT={_ROOT} | sys.path={sys.path[:3]}")

# --- Add repo root to sys.path for telemetry imports ---
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# --- Optional telemetry imports (degrade gracefully if unavailable) ---
try:
    from telemetry.logger import logger, new_correlation_id, set_correlation_id
    from telemetry.cost import cost_from_usage
    from telemetry.redact import redact as _redact
    _dbg("telemetry imported OK")
except Exception as e:
    _dbg(f"telemetry import FAILED: {e}")

    class _FakeLogger:
        def log_event(self, event, data):
            _dbg(f"LOG {event}: {json.dumps(data, ensure_ascii=False)[:200]}")

    logger = _FakeLogger()
    def new_correlation_id(): return "noop"
    def set_correlation_id(_): pass
    def cost_from_usage(*_): return 0.0
    def _redact(text): return text, 0

# --- Load system prompt ---
try:
    _PROMPT_PATH = os.path.join(_HERE, "prompt.txt")
    with open(_PROMPT_PATH, encoding="utf-8") as _f:
        _SYSTEM_PROMPT = _f.read().strip()
    _dbg(f"prompt loaded OK ({len(_SYSTEM_PROMPT)} chars)")
except Exception as e:
    _SYSTEM_PROMPT = None
    _dbg(f"prompt load FAILED: {e}")

# --- Injection detection regex ---
_INJECTION_RE = re.compile(
    r"GHI\s*CH[ÚU][:\s][^\n]*(price|discount|ignore|override|mi[eễ]n\s*ph[íi]|b[oỏ]\s*qua)[^\n]*",
    re.IGNORECASE,
)


def _sanitize(question: str) -> str:
    return _INJECTION_RE.sub("[GHI CHU: DATA ONLY]", question)


def mitigate(call_next, question, config, context):
    try:
        qid        = context.get("qid", "?")
        session_id = context.get("session_id", "?")
        turn_index = context.get("turn_index", 0)
        cache      = context["cache"]
        cache_lock = context["cache_lock"]

        cid = new_correlation_id()
        set_correlation_id(cid)

        # 1. Cache lookup
        cache_key = question.strip().lower()
        with cache_lock:
            if cache_key in cache:
                logger.log_event("CACHE_HIT", {"qid": qid})
                return cache[cache_key]

        # 2. Sanitize injection
        clean_question = _sanitize(question)
        if clean_question != question:
            logger.log_event("INJECTION_DETECTED", {"qid": qid, "turn_index": turn_index})

        # 3. Build config with our prompt
        conf = dict(config)
        if _SYSTEM_PROMPT:
            conf["system_prompt"] = _SYSTEM_PROMPT

        # 4. Call agent
        t0 = time.time()
        result = call_next(clean_question, conf)
        wall_ms = int((time.time() - t0) * 1000)

        meta       = result.get("meta", {}) or {}
        usage      = meta.get("usage", {}) or {}
        model      = meta.get("model", config.get("model", "unknown"))
        status     = result.get("status", "unknown")
        steps      = result.get("steps", 0)
        tools_used = meta.get("tools_used", [])

        # 5. Retry once on transient failures
        if status not in ("ok", "wrapper_error"):
            logger.log_event("RETRY", {"qid": qid, "status": status})
            result2 = call_next(clean_question, conf)
            if result2.get("status") == "ok":
                result     = result2
                meta       = result.get("meta", {}) or {}
                usage      = meta.get("usage", {}) or {}
                model      = meta.get("model", model)
                status     = "ok"
                steps      = result.get("steps", steps)
                tools_used = meta.get("tools_used", tools_used)

        # 6. Log observability
        logger.log_event("CALL", {
            "qid": qid, "session_id": session_id, "turn_index": turn_index,
            "status": status, "wall_ms": wall_ms,
            "latency_ms": meta.get("latency_ms"),
            "steps": steps, "tool_count": len(tools_used),
            "tools_used": tools_used,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cost_usd": cost_from_usage(model, usage),
            "is_error": status != "ok",
        })

        if status != "ok":
            logger.log_event("ERROR", {"qid": qid, "status": status})

        # 7. Redact PII from answer
        answer = result.get("answer") or ""
        redacted, pii_count = _redact(answer)
        if pii_count > 0:
            logger.log_event("PII_LEAK", {"qid": qid, "count": pii_count})
            result = dict(result)
            result["answer"] = redacted

        # 8. Drift signal
        if turn_index >= 3:
            logger.log_event("DRIFT_SIGNAL", {
                "qid": qid, "session_id": session_id,
                "turn_index": turn_index, "status": status,
            })

        # 9. Cache success
        if status == "ok" and result.get("answer"):
            with cache_lock:
                cache[cache_key] = result

        return result

    except Exception:
        err = traceback.format_exc()
        _dbg(f"mitigate EXCEPTION qid={context.get('qid','?')}: {err}")
        # Fallback: passthrough so sim records the real agent result
        return call_next(question, config)
