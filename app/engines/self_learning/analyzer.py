"""
Self-Learning AI Module — Stage 12: analyzer
=============================================
Чистые функции анализа журнала AI-решений (таблица ai_decisions).

На этом этапе у бота ещё нет закрытых сделок (trades = 0), поэтому
Self-Learning анализирует то, что реально есть — журнал РЕШЕНИЙ AI
Decision Engine. Из него извлекается:

  * статистика решений (по символам, направлениям, confidence);
  * паттерны scoring — какие источники-компоненты дают баллы,
    а какие "молчат" / не подключены;
  * распределение final_score по порогам (no-trade / small / normal);
  * распределение решений по часам суток;
  * разнонаправленные решения (directional conflicts).

ВАЖНО (правило спеки):
  Модуль НИЧЕГО не меняет в боевых правилах. Он только наблюдает и
  ФОРМУЛИРУЕТ предложения текстом — для человека. Любые изменения
  весов/фильтров одобряет человек. Серьёзные предложения по весам
  имеют смысл только после накопления реальных закрытых сделок
  (>= 100 closed trades + 30 дней форвард-теста).

Модуль не зависит от БД и не импортирует SQLAlchemy: работает на
нормализованных словарях, что делает его легко тестируемым и
безопасным для импорта. Адаптер row_to_decision() превращает ORM
объект AiDecision (или словарь) в нормализованный словарь.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean, median
from typing import Any, Iterable, Optional

__all__ = [
    "KNOWN_SOURCES",
    "SERIOUS_MIN_CLOSED_TRADES",
    "row_to_decision",
    "normalize",
    "decision_counts",
    "component_stats",
    "final_score_stats",
    "time_of_day",
    "directional_conflicts",
    "generate_insights",
    "generate_suggestions",
    "build_report",
    "format_report_text",
]


# Полный «универсум» источников-компонентов scoring (Stage 10/13).
# Используется, чтобы видеть не только то, что срабатывает, но и то,
# что вообще не подключено / не присылает данных.
KNOWN_SOURCES = [
    "tradingview",
    "liquidity",
    "delta",
    "imbalance",
    "volume",
    "trend",
    "oi_funding",
    "news",
    "social",
]

# По спеке: серьёзные предложения по изменению весов допустимы только
# после достаточного количества РЕАЛЬНЫХ закрытых сделок.
SERIOUS_MIN_CLOSED_TRADES = 100

# Пороги score (из спеки scoring system).
SCORE_NO_TRADE = 70   # < 70  -> NO TRADE
SCORE_NORMAL = 85     # >= 85 -> normal position; 70..84 -> small


# --------------------------------------------------------------------------
# Нормализация: ORM AiDecision / dict -> единый словарь решения
# --------------------------------------------------------------------------

def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _get(row: Any, key: str, default: Any = None) -> Any:
    """Достать поле из ORM-объекта или dict (утиная типизация)."""
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def row_to_decision(row: Any) -> dict:
    """ORM AiDecision (или dict) -> нормализованный словарь решения."""
    full = _get(row, "full_response", None)
    if not isinstance(full, dict):
        full = {}

    final_score = _get(row, "final_score", None)
    if final_score is None:
        final_score = full.get("final_score")

    components: list[dict] = []
    for c in (full.get("components") or []):
        if not isinstance(c, dict):
            continue
        components.append(
            {
                "name": c.get("name"),
                "available": bool(c.get("available")),
                "points": _to_float(c.get("points", 0)) or 0.0,
            }
        )

    decision = _get(row, "decision", None)
    return {
        "created_at": _as_datetime(_get(row, "created_at", None)),
        "decision": (decision.upper() if isinstance(decision, str) else None),
        "direction": _get(row, "direction", None) or None,
        "confidence": _get(row, "confidence", None) or None,
        "final_score": _to_float(final_score),
        "symbol": full.get("symbol"),
        "components": components,
        "reason": list(full.get("reason") or []),
        "warnings": list(full.get("warnings") or []),
    }


def normalize(rows: Iterable[Any]) -> list[dict]:
    return [row_to_decision(r) for r in rows]


# --------------------------------------------------------------------------
# Аналитические агрегаты (чистые функции над нормализованными решениями)
# --------------------------------------------------------------------------

def decision_counts(decisions: list[dict]) -> dict:
    by_decision = Counter(d["decision"] or "UNKNOWN" for d in decisions)
    by_symbol = Counter(d["symbol"] or "unknown" for d in decisions)
    by_direction = Counter(d["direction"] or "n/a" for d in decisions)
    by_confidence = Counter(d["confidence"] or "n/a" for d in decisions)
    return {
        "total": len(decisions),
        "by_decision": dict(by_decision),
        "by_symbol": dict(by_symbol),
        "by_direction": dict(by_direction),
        "by_confidence": dict(by_confidence),
    }


def component_stats(decisions: list[dict]) -> dict:
    total = len(decisions)
    names = list(KNOWN_SOURCES)
    for d in decisions:
        for c in d["components"]:
            if c["name"] and c["name"] not in names:
                names.append(c["name"])

    stats: dict[str, dict] = {}
    for name in names:
        present = 0
        fired = 0
        fired_points: list[float] = []
        for d in decisions:
            comp = next((c for c in d["components"] if c["name"] == name), None)
            if comp and comp["available"]:
                present += 1
                if comp["points"] > 0:
                    fired += 1
                    fired_points.append(comp["points"])
        stats[name] = {
            "present": present,
            "fired": fired,
            "avail_rate": round(present / total, 3) if total else 0.0,
            "fire_rate": round(fired / total, 3) if total else 0.0,
            "avg_points_when_fired": round(mean(fired_points), 2) if fired_points else 0.0,
        }
    return stats


def final_score_stats(decisions: list[dict]) -> dict:
    vals = [d["final_score"] for d in decisions if d["final_score"] is not None]
    buckets = {"no_trade_lt70": 0, "small_70_84": 0, "normal_85plus": 0}
    for v in vals:
        if v < SCORE_NO_TRADE:
            buckets["no_trade_lt70"] += 1
        elif v < SCORE_NORMAL:
            buckets["small_70_84"] += 1
        else:
            buckets["normal_85plus"] += 1
    return {
        "count": len(vals),
        "min": round(min(vals), 2) if vals else None,
        "max": round(max(vals), 2) if vals else None,
        "mean": round(mean(vals), 2) if vals else None,
        "median": round(median(vals), 2) if vals else None,
        "buckets": buckets,
    }


def time_of_day(decisions: list[dict]) -> dict:
    hours = Counter(
        d["created_at"].hour for d in decisions if d["created_at"] is not None
    )
    return {h: n for h, n in sorted(hours.items())}


def directional_conflicts(decisions: list[dict], window_minutes: int = 30) -> dict:
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        if d["direction"] in ("LONG", "SHORT") and d["created_at"] is not None:
            by_symbol[d["symbol"] or "unknown"].append(d)

    conflicts_by_symbol: dict[str, int] = {}
    total = 0
    for symbol, items in by_symbol.items():
        items.sort(key=lambda x: x["created_at"])
        cnt = 0
        for prev, cur in zip(items, items[1:]):
            if prev["direction"] != cur["direction"]:
                delta_min = (cur["created_at"] - prev["created_at"]).total_seconds() / 60.0
                if delta_min <= window_minutes:
                    cnt += 1
        if cnt:
            conflicts_by_symbol[symbol] = cnt
            total += cnt
    return {
        "total": total,
        "by_symbol": conflicts_by_symbol,
        "window_minutes": window_minutes,
    }


# --------------------------------------------------------------------------
# Наблюдения и предложения (текст для человека — НЕ автоизменения)
# --------------------------------------------------------------------------

def generate_insights(counts, comps, scores, conflicts, hours) -> list[str]:
    insights: list[str] = []
    if counts["total"] == 0:
        return insights

    # Направленность
    dir_counts = {k: v for k, v in counts["by_direction"].items() if k in ("LONG", "SHORT")}
    dir_total = sum(dir_counts.values())
    if dir_total:
        top_dir, top_n = max(dir_counts.items(), key=lambda x: x[1])
        pct = round(100 * top_n / dir_total)
        if pct >= 80:
            insights.append(f"Сильный перекос в {top_dir}: {pct}% решений.")

    # Компоненты — доминирующие и молчащие
    for name, s in comps.items():
        if s["present"] == 0:
            insights.append(f"Источник «{name}» не подключён или не присылает данных (0 решений).")
        elif s["fire_rate"] >= 0.9:
            insights.append(
                f"«{name}» срабатывает почти всегда ({round(100 * s['fire_rate'])}%) — доминирующий фактор."
            )
        elif s["fire_rate"] <= 0.1:
            insights.append(
                f"«{name}» срабатывает редко ({round(100 * s['fire_rate'])}%) при наличии данных."
            )

    # Score
    if scores["max"] is not None and scores["max"] < SCORE_NORMAL:
        insights.append(
            f"Максимальный final_score = {scores['max']} (<{SCORE_NORMAL}): все TRADE остаются small."
        )
    if scores["buckets"]["no_trade_lt70"]:
        insights.append(
            f"{scores['buckets']['no_trade_lt70']} записей с score<{SCORE_NO_TRADE} — "
            "такие в норме не должны попадать в журнал TRADE, проверить фильтр/дедуп."
        )

    # Конфликты
    if conflicts["total"]:
        syms = ", ".join(conflicts["by_symbol"].keys())
        insights.append(
            f"Разнонаправленные решения в окне {conflicts['window_minutes']} мин: "
            f"{conflicts['total']} (символы: {syms})."
        )

    # Время
    if hours:
        peak_hour, peak_n = max(hours.items(), key=lambda x: x[1])
        insights.append(f"Пик активности решений — около {peak_hour}:00 UTC ({peak_n} шт).")

    return insights


def generate_suggestions(counts, comps, scores, conflicts, closed_trades: int = 0) -> list[str]:
    suggestions: list[str] = []
    if counts["total"] == 0:
        return suggestions

    not_connected = [n for n, s in comps.items() if s["present"] == 0]
    if not_connected:
        suggestions.append("Подключить недостающие источники: " + ", ".join(not_connected) + ".")

    active = [n for n, s in comps.items() if s["present"]]
    dominant = [n for n, s in comps.items() if s["present"] and s["fire_rate"] >= 0.9]
    if dominant and len(active) <= 2:
        suggestions.append(
            "Решения опираются почти только на " + ", ".join(dominant)
            + " — добавить подтверждающие фильтры, чтобы снизить зависимость от одного фактора."
        )

    if scores["max"] is not None and scores["max"] < SCORE_NORMAL:
        suggestions.append(
            f"Ни одно решение не дотягивает до normal ({SCORE_NORMAL}+). Вероятная причина — "
            "неполный набор источников; пересматривать веса имеет смысл только после их подключения."
        )

    if conflicts["total"]:
        suggestions.append(
            "Рассмотреть cooldown по символу после смены направления, чтобы убрать "
            "разнонаправленные решения подряд."
        )

    # Дисклеймер про серьёзные изменения (правило спеки)
    if closed_trades < SERIOUS_MIN_CLOSED_TRADES:
        suggestions.append(
            f"ВНИМАНИЕ: закрытых сделок {closed_trades} (<{SERIOUS_MIN_CLOSED_TRADES}). "
            "Это наблюдения по журналу НАМЕРЕНИЙ, а не по результатам. Серьёзные изменения "
            "весов/правил откладываются до набора реальной статистики сделок. Все пункты "
            "выше — только предложения для человека, не автоизменения."
        )
    return suggestions


# --------------------------------------------------------------------------
# Оркестратор + текстовый рендер
# --------------------------------------------------------------------------

def build_report(rows: Iterable[Any], *, closed_trades: int = 0) -> dict:
    decisions = normalize(rows)
    total = len(decisions)
    if total == 0:
        return {
            "status": "no_data",
            "message": "Журнал решений пуст — анализировать пока нечего.",
            "total_decisions": 0,
            "closed_trades": closed_trades,
            "insights": [],
            "suggestions": [],
        }

    counts = decision_counts(decisions)
    comps = component_stats(decisions)
    scores = final_score_stats(decisions)
    hours = time_of_day(decisions)
    conflicts = directional_conflicts(decisions)

    insights = generate_insights(counts, comps, scores, conflicts, hours)
    suggestions = generate_suggestions(counts, comps, scores, conflicts, closed_trades)

    return {
        "status": "ok",
        "total_decisions": total,
        "closed_trades": closed_trades,
        "serious_changes_unlocked": closed_trades >= SERIOUS_MIN_CLOSED_TRADES,
        "counts": counts,
        "components": comps,
        "final_score": scores,
        "time_of_day": hours,
        "conflicts": conflicts,
        "insights": insights,
        "suggestions": suggestions,
    }


def format_report_text(report: dict) -> str:
    if report.get("status") == "no_data":
        return "Self-Learning report (Stage 12)\n" + report["message"]

    lines: list[str] = []
    lines.append("Self-Learning report (Stage 12)")
    lines.append(
        f"Решений в анализе: {report['total_decisions']} | "
        f"закрытых сделок: {report['closed_trades']}"
    )

    c = report["counts"]
    lines.append("")
    lines.append("Символы: " + ", ".join(f"{k}={v}" for k, v in c["by_symbol"].items()))
    lines.append("Направления: " + ", ".join(f"{k}={v}" for k, v in c["by_direction"].items()))
    lines.append("Confidence: " + ", ".join(f"{k}={v}" for k, v in c["by_confidence"].items()))

    s = report["final_score"]
    if s["count"]:
        lines.append(
            f"final_score: min={s['min']} max={s['max']} avg={s['mean']} | "
            f"<70={s['buckets']['no_trade_lt70']} "
            f"70-84={s['buckets']['small_70_84']} "
            f"85+={s['buckets']['normal_85plus']}"
        )

    if report["insights"]:
        lines.append("")
        lines.append("Наблюдения:")
        lines.extend(f"  - {x}" for x in report["insights"])

    if report["suggestions"]:
        lines.append("")
        lines.append("Предложения (одобряет человек):")
        lines.extend(f"  - {x}" for x in report["suggestions"])

    return "\n".join(lines)
