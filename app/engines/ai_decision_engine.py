"""
AI Decision Engine — Stage 10

Объединяет все scores и возвращает JSON:
{
  "decision": "TRADE" | "NO_TRADE",
  "direction": "LONG" | "SHORT",
  "probability": 0-100,
  "confidence": "LOW/MEDIUM/HIGH",
  "risk_level": "LOW/MEDIUM/HIGH",
  "market_score": 0-100,
  "liquidity_score": 0-100,
  "orderflow_score": 0-100,
  "news_score": 0-100,
  "social_score": 0-100,
  "trend_score": 0-100,
  "final_score": 0-100,
  "reason": [...],
  "warnings": [...]
}

Сейчас: заглушка.
"""
