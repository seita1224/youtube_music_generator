"""月次 LLM 予算の監視 / アラート (US5, T119, FR-026)。

当月の ``usage_log.cost_usd`` 合計が ``Settings.monthly_budget_usd`` の
50% / 80% / 100% 閾値を跨いだときに Slack 通知を発行する責務を持つ
(``alert.py`` を後段で追加)。
"""
