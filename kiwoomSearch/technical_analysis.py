# -*- coding: utf-8 -*-
"""
기술적 분석 모듈
- RSI (Wilder's Smoothing)
- 단순 이동평균 (SMA)
- 거래량 비율 (당일 누적 / N일 평균)
- N일 고가 돌파
- 조건 통합 평가
"""


class TechnicalAnalysis:
    """지표 계산 클래스 (일봉 기준, 미완성 당일봉 포함)"""

    _TRADING_VALUE_UNIT_KRW = 1_000_000

    # ------------------------------------------------------------------
    # RSI
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_rsi(close_prices: list, period: int = 14):
        """
        RSI 계산 (Wilder's Smoothing Method)

        Args:
            close_prices: 종가 리스트 (최신순 - 인덱스 0 = 가장 최근)
            period: RSI 기간 (기본 14)

        Returns:
            float | None: RSI 값 (0~100), 데이터 부족 시 None
        """
        if not close_prices or len(close_prices) < period + 1:
            return None

        # 최신순 → 오래된순으로 뒤집기
        prices = list(reversed(close_prices[:period + 2]))

        # 첫 번째 평균 계산 (단순평균)
        gains = []
        losses = []
        for i in range(1, period + 1):
            diff = prices[i] - prices[i - 1]
            if diff > 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        # 나머지 데이터로 Wilder 평활
        for i in range(period + 1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gain = diff if diff > 0 else 0.0
            loss = abs(diff) if diff < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1 + rs)), 2)

    # ------------------------------------------------------------------
    # 이동평균
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_sma(close_prices: list, period: int):
        """
        단순 이동평균 (SMA)

        Args:
            close_prices: 종가 리스트 (최신순)
            period: 이평 기간

        Returns:
            float | None
        """
        if not close_prices or len(close_prices) < period:
            return None
        try:
            return round(sum(float(p) for p in close_prices[:period]) / period, 2)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 거래량
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_avg_volume(volumes: list, days: int):
        """
        N일 평균 거래량 (당일 봉 제외, 과거 봉 기준)

        Args:
            volumes: 거래량 리스트 (최신순, 인덱스 0 = 당일 또는 최신)
            days: 평균 산출 기간

        Returns:
            float | None
        """
        # 인덱스 1부터 days개 (당일 제외)
        past = volumes[1: days + 1]
        if not past or len(past) < days:
            return None
        try:
            return sum(float(v) for v in past) / len(past)
        except Exception:
            return None

    @staticmethod
    def calculate_volume_ratio(today_volume: int, avg_volume: float):
        """
        당일 거래량 / N일 평균 거래량

        Returns:
            float | None
        """
        if not avg_volume or avg_volume <= 0:
            return None
        try:
            return round(today_volume / avg_volume, 2)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 가격 돌파
    # ------------------------------------------------------------------
    @staticmethod
    def calculate_highest_high(highs: list, days: int):
        """
        N일 최고가 (당일 제외, 과거 봉 기준)

        Args:
            highs: 고가 리스트 (최신순, 인덱스 0 = 당일)
            days: 기간

        Returns:
            float | None
        """
        past = highs[1: days + 1]
        if not past or len(past) < days:
            return None
        try:
            return max(float(h) for h in past)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 통합 조건 평가
    # ------------------------------------------------------------------
    def evaluate(
        self,
        candles: list,
        current_price: int,
        today_volume: int,
        conditions: dict,
        investor_data: list = None,
    ) -> dict:
        """
        조건 평가

        Args:
            candles: 일봉 리스트 (최신순)
                     각 봉: {"open", "high", "low", "close", "volume", "trading_value", "date"}
                     인덱스 0 = 당일 미완성봉 (장중)
            current_price: 실시간 현재가
            today_volume: 당일 누적 거래량 (실시간)
            conditions: config["scan"] 딕셔너리
            investor_data: opt10059 결과 (최신순)
                           [{"date", "foreign", "institution"}, ...]
                           양수=순매수, 음수=순매도

        Returns:
            {
                "rsi": float | None,
                "rsi_ok": bool,
                "ma": float | None,
                "ma_short": float | None,
                "ma_ok": bool,
                "volume_ratio": float | None,
                "volume_ok": bool,
                "highest": float | None,
                "breakout_ok": bool,
                "supply_ok": bool,
                "trading_value": int | None,
                "trading_value_ratio": float | None,
                "trading_value_ok": bool,
                "match": bool,
            }
        """
        result = {
            "rsi": None,
            "rsi_ok": False,
            "ma": None,
            "ma_short": None,
            "ma_ok": False,
            "volume_ratio": None,
            "volume_ok": False,
            "highest": None,
            "breakout_ok": False,
            "supply_ok": False,
            "supply_data_available": False,
            "supply_foreign_ok": False,
            "supply_institution_ok": False,
            "trading_value": None,
            "trading_value_ratio": None,
            "trading_value_ok": False,
            "match": False,
        }

        if not candles:
            return result

        close_prices = [c.get("close", 0) for c in candles]
        high_prices  = [c.get("high",  0) for c in candles]
        volumes      = [c.get("volume", 0) for c in candles]

        mode = conditions.get("condition_mode", "AND")
        checks = []  # (enabled, ok) 튜플 목록

        # ── RSI ────────────────────────────────────────────────────────
        if conditions.get("rsi_enabled"):
            period  = int(conditions.get("rsi_period", 14))
            rsi_min = float(conditions.get("rsi_min", 0))
            rsi_max = float(conditions.get("rsi_max", 30))
            rsi = self.calculate_rsi(close_prices, period)
            result["rsi"] = rsi
            ok = rsi is not None and rsi_min <= rsi <= rsi_max
            result["rsi_ok"] = ok
            checks.append(ok)

        # ── 이동평균 ───────────────────────────────────────────────────
        if conditions.get("ma_enabled"):
            cond      = conditions.get("ma_condition", "above")
            ma_period = int(conditions.get("ma_period", 20))

            if cond == "golden":
                short_p = int(conditions.get("ma_short_period", 5))
                long_p  = int(conditions.get("ma_long_period", 20))
                ma_s = self.calculate_sma(close_prices, short_p)
                ma_l = self.calculate_sma(close_prices, long_p)
                result["ma"]       = ma_l
                result["ma_short"] = ma_s
                ok = ma_s is not None and ma_l is not None and ma_s > ma_l
            else:
                ma = self.calculate_sma(close_prices, ma_period)
                result["ma"] = ma
                if cond == "above":
                    ok = ma is not None and current_price > ma
                else:  # "below"
                    ok = ma is not None and current_price < ma

            result["ma_ok"] = ok
            checks.append(ok)

        # ── 거래량 ─────────────────────────────────────────────────────
        if conditions.get("volume_enabled"):
            avg_days = int(conditions.get("volume_avg_days", 20))
            ratio_th = float(conditions.get("volume_ratio", 2.0))
            avg_vol  = self.calculate_avg_volume(volumes, avg_days)
            vol_ratio = self.calculate_volume_ratio(today_volume, avg_vol)
            result["volume_ratio"] = vol_ratio
            ok = vol_ratio is not None and vol_ratio >= ratio_th
            result["volume_ok"] = ok
            checks.append(ok)

        # ── 가격 돌파 ──────────────────────────────────────────────────
        if conditions.get("breakout_enabled"):
            days = int(conditions.get("breakout_days", 20))
            highest = self.calculate_highest_high(high_prices, days)
            result["highest"] = highest
            ok = highest is not None and current_price > highest
            result["breakout_ok"] = ok
            checks.append(ok)

        # ── 수급 조건 (외국인/기관) ─────────────────────────────────
        if conditions.get("supply_enabled"):
            inv = investor_data or []
            supply_data_available = any(
                (row.get("foreign", 0) or row.get("institution", 0))
                for row in inv
            )
            result["supply_data_available"] = supply_data_available
            supply_parts = []

            if supply_data_available:
                # 외국인 순매수 N일 연속
                foreign_days = int(conditions.get("foreign_consec_days", 3))
                if foreign_days > 0:
                    if len(inv) >= foreign_days:
                        consecutive = all(inv[i]["foreign"] > 0 for i in range(foreign_days))
                    else:
                        consecutive = False
                    result["supply_foreign_ok"] = consecutive
                    supply_parts.append(consecutive)

                # 기관 순매수 전환 (오늘 > 0)
                if conditions.get("institution_turnover_enabled", True):
                    inst_today = inv[0]["institution"] if inv else 0
                    inst_ok = inst_today > 0
                    result["supply_institution_ok"] = inst_ok
                    supply_parts.append(inst_ok)

            supply_ok = all(supply_parts) if supply_parts else False
            result["supply_ok"] = supply_ok
            checks.append(supply_ok)

        # ── 거래대금 조건 ───────────────────────────────────────────────
        if conditions.get("trading_value_enabled"):
            tv_values = [abs(int(c.get("trading_value") or 0)) for c in candles]
            today_tv = tv_values[0] if tv_values else 0
            result["trading_value"] = today_tv
            tv_parts = []

            # 거래대금 최소 (억원) — API 단위가 원(KRW)이라고 가정
            tv_min_billion = float(conditions.get("trading_value_min_billion", 100))
            if tv_min_billion > 0:
                tv_parts.append(today_tv >= tv_min_billion * 100)

            # 거래대금 증가율 (%)
            if conditions.get("trading_value_increase_enabled", False):
                avg_days = int(conditions.get("trading_value_avg_days", 20))
                past_tvs = [v for v in tv_values[1: avg_days + 1] if v > 0]
                if past_tvs:
                    avg_tv = sum(past_tvs) / len(past_tvs)
                    if avg_tv > 0:
                        ratio_pct = round(today_tv / avg_tv * 100, 1)
                        result["trading_value_ratio"] = ratio_pct
                        threshold = float(conditions.get("trading_value_increase_pct", 200))
                        tv_parts.append(ratio_pct >= threshold)
                    else:
                        tv_parts.append(False)
                else:
                    tv_parts.append(False)

            tv_ok = all(tv_parts) if tv_parts else False
            result["trading_value_ok"] = tv_ok
            checks.append(tv_ok)

        # ── 조건 결합 ──────────────────────────────────────────────────
        if not checks:
            result["match"] = False
        elif mode == "OR":
            result["match"] = any(checks)
        else:  # AND
            result["match"] = all(checks)

        return result
