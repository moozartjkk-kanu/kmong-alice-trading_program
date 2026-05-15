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
    # 기준봉 탐지
    # ------------------------------------------------------------------
    @staticmethod
    def find_reference_candle(candles: list, avg_vol_days: int = 20,
                              min_rise_pct: float = 3.0, vol_multiplier: float = 2.0,
                              search_days: int = 5):
        """
        최근 search_days일 이내에서 기준봉(큰 양봉) 탐지
          - 양봉 (종가 > 시가)
          - 상승률 >= min_rise_pct %
          - 거래량 >= avg_vol_days일 평균 × vol_multiplier
        candles: 최신순 (인덱스 0 = 당일)
        반환: {"index": i, "open": ..., "close": ..., "high": ...} or None
        """
        needed = search_days + avg_vol_days + 1
        if not candles or len(candles) < needed:
            return None

        for i in range(1, search_days + 1):
            c = candles[i]
            open_p  = float(c.get("open",   0) or 0)
            close_p = float(c.get("close",  0) or 0)
            high_p  = float(c.get("high",   0) or 0)
            vol     = float(c.get("volume", 0) or 0)

            if open_p <= 0 or close_p <= open_p:
                continue

            rise_pct = (close_p - open_p) / open_p * 100
            if rise_pct < min_rise_pct:
                continue

            # 기준봉 당일 기준 과거 avg_vol_days일 평균 거래량
            past_vols = [
                float(candles[j].get("volume", 0) or 0)
                for j in range(i + 1, i + avg_vol_days + 1)
                if j < len(candles)
            ]
            if len(past_vols) < avg_vol_days:
                continue

            avg_vol = sum(past_vols) / len(past_vols)
            if avg_vol <= 0 or vol < avg_vol * vol_multiplier:
                continue

            return {"index": i, "open": open_p, "close": close_p, "high": high_p}

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
            "trend_ok": False,
            "pullback_ok": False,
            "price_floor_ok": False,
            "strength_ok": False,
            "rebound_ok": False,
            "rebound_bullish": False,
            "rebound_volume": False,
            "rebound_prev_high": False,
            "ref_candle_ok": False,
            "ref_candle_found": False,
            "close_above_prev_ok": False,
            "near_high_support_ok": False,
            "match": False,
        }

        if not candles:
            return result

        close_prices = [c.get("close", 0) for c in candles]
        open_prices  = [c.get("open",  0) for c in candles]
        high_prices  = [c.get("high",  0) for c in candles]
        low_prices   = [c.get("low",   0) for c in candles]
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

        # ── 추세 조건 (MA 기반) ────────────────────────────────────────
        if conditions.get("trend_enabled"):
            trend_parts = []

            if conditions.get("trend_close_above_ma20", True):
                ma20 = self.calculate_sma(close_prices, 20)
                trend_parts.append(ma20 is not None and current_price > ma20)

            if conditions.get("trend_ma20_rising", True):
                ma20_today = self.calculate_sma(close_prices, 20)
                ma20_yesterday = self.calculate_sma(close_prices[1:], 20)
                trend_parts.append(
                    ma20_today is not None and ma20_yesterday is not None
                    and ma20_today > ma20_yesterday
                )

            if conditions.get("trend_ma20_above_ma60", True):
                ma20 = self.calculate_sma(close_prices, 20)
                ma60 = self.calculate_sma(close_prices, 60)
                trend_parts.append(ma20 is not None and ma60 is not None and ma20 > ma60)

            if conditions.get("trend_ma60_rising", False):
                ma60_today = self.calculate_sma(close_prices, 60)
                ma60_yesterday = self.calculate_sma(close_prices[1:], 60)
                trend_parts.append(
                    ma60_today is not None and ma60_yesterday is not None
                    and ma60_today > ma60_yesterday
                )

            # 저가 >= MA20 × ratio (추세 유지: MA 아래로 너무 빠지지 않음)
            if conditions.get("trend_low_above_ma20", False):
                ma20 = self.calculate_sma(close_prices, 20)
                low_ratio = float(conditions.get("trend_low_ratio", 0.97))
                today_low = float(low_prices[0]) if low_prices else None
                trend_parts.append(
                    ma20 is not None and today_low is not None
                    and today_low >= ma20 * low_ratio
                )

            trend_ok = all(trend_parts) if trend_parts else False
            result["trend_ok"] = trend_ok
            checks.append(trend_ok)

        # ── 눌림 조건 ──────────────────────────────────────────────────
        if conditions.get("pullback_enabled"):
            days = int(conditions.get("pullback_days", 5))
            ma_period = int(conditions.get("pullback_ma_period", 20))
            upper_ratio = float(conditions.get("pullback_ratio", 1.02))
            lower_ratio = float(conditions.get("pullback_lower_ratio", 0.97))
            ma = self.calculate_sma(close_prices, ma_period)
            pullback_ok = False
            if ma is not None and len(low_prices) >= days:
                upper = ma * upper_ratio
                lower = ma * lower_ratio
                # 최근 N일 내 하루라도 저가가 [lower, upper] 범위 안에 있으면 눌림 인정
                pullback_ok = any(
                    lower <= float(low_prices[i]) <= upper
                    for i in range(days)
                )
            # 눌림 구간(최근 N일) 평균 거래량 < 직전 N일 평균 거래량
            if pullback_ok and conditions.get("pullback_volume_decrease_enabled", False):
                pullback_vols = [float(volumes[i]) for i in range(1, days + 1) if i < len(volumes)]
                prev_vols = [float(volumes[i]) for i in range(days + 1, days * 2 + 1) if i < len(volumes)]
                if pullback_vols and prev_vols:
                    avg_pullback = sum(pullback_vols) / len(pullback_vols)
                    avg_prev = sum(prev_vols) / len(prev_vols)
                    if not (avg_prev > 0 and avg_pullback < avg_prev):
                        pullback_ok = False
            result["pullback_ok"] = pullback_ok
            checks.append(pullback_ok)

        # ── 과도한 하락 방지 ───────────────────────────────────────────
        if conditions.get("price_floor_enabled"):
            days = int(conditions.get("price_floor_days", 20))
            ratio = float(conditions.get("price_floor_ratio", 0.92))
            recent_highs = high_prices[:days]
            price_floor_ok = False
            if recent_highs:
                highest = max(float(h) for h in recent_highs)
                price_floor_ok = highest > 0 and current_price >= highest * ratio
            result["price_floor_ok"] = price_floor_ok
            checks.append(price_floor_ok)

        # ── 힘 유지 (최근 N일 내 종가가 이전 M일 최고가 돌파한 적 있음) ────
        if conditions.get("strength_enabled"):
            s_days = int(conditions.get("strength_days", 10))
            s_ref_days = int(conditions.get("strength_ref_days", 20))
            # close_prices[0] = 오늘, [1..s_days-1] = 최근 N일, [s_days..s_days+s_ref_days-1] = 기준 기간
            ref_closes = close_prices[s_days: s_days + s_ref_days]
            strength_ok = False
            if ref_closes and len(close_prices) >= s_days:
                ref_high = max(float(c) for c in ref_closes)
                strength_ok = any(float(close_prices[i]) > ref_high for i in range(s_days))
            result["strength_ok"] = strength_ok
            checks.append(strength_ok)

        # ── 반등 신호 (OR 결합) ────────────────────────────────────────
        if conditions.get("rebound_enabled"):
            rebound_parts = []

            if conditions.get("rebound_bullish_candle", True):
                bullish = (len(close_prices) > 0 and len(open_prices) > 0
                           and float(close_prices[0]) > float(open_prices[0]))
                result["rebound_bullish"] = bullish
                rebound_parts.append(bullish)

            if conditions.get("rebound_volume_increase", True):
                avg5 = self.calculate_avg_volume(volumes, 5)
                vol_ok = avg5 is not None and today_volume > avg5
                result["rebound_volume"] = vol_ok
                rebound_parts.append(vol_ok)

            if conditions.get("rebound_prev_high_breakout", True):
                prev_high = float(high_prices[1]) if len(high_prices) > 1 else None
                ph_ok = prev_high is not None and prev_high > 0 and current_price > prev_high
                result["rebound_prev_high"] = ph_ok
                rebound_parts.append(ph_ok)

            rebound_ok = any(rebound_parts) if rebound_parts else False
            result["rebound_ok"] = rebound_ok
            checks.append(rebound_ok)

        # ── 기준봉 눌림 조건 ──────────────────────────────────────────
        if conditions.get("ref_candle_pullback_enabled"):
            search_days  = int(conditions.get("ref_candle_search_days", 5))
            min_rise_pct = float(conditions.get("ref_candle_min_rise_pct", 3.0))
            vol_mult     = float(conditions.get("ref_candle_vol_multiplier", 2.0))
            vol_avg_days = int(conditions.get("ref_candle_vol_avg_days", 20))
            pb_max_ratio = float(conditions.get("ref_candle_pullback_max_ratio", 0.97))

            ref = self.find_reference_candle(
                candles, vol_avg_days, min_rise_pct, vol_mult, search_days
            )
            ref_candle_ok = False
            if ref:
                result["ref_candle_found"] = True
                # 현재가 <= 기준봉 고가 × 눌림 비율  AND  현재가 >= 기준봉 시가
                if (current_price <= ref["high"] * pb_max_ratio
                        and current_price >= ref["open"]):
                    ref_candle_ok = True
            result["ref_candle_ok"] = ref_candle_ok
            checks.append(ref_candle_ok)

        # ── 종가 > 전일 종가 ───────────────────────────────────────────
        if conditions.get("close_above_prev_enabled"):
            prev_close = float(close_prices[1]) if len(close_prices) > 1 else None
            ok = prev_close is not None and prev_close > 0 and current_price > prev_close
            result["close_above_prev_ok"] = ok
            checks.append(ok)

        # ── 최근 N일 고점 ±% 이내 지지 ────────────────────────────────
        if conditions.get("near_high_support_enabled"):
            nhs_days   = int(conditions.get("near_high_support_days", 10))
            nhs_pct    = float(conditions.get("near_high_support_pct", 2.0))
            past_highs = high_prices[1: nhs_days + 1]
            near_ok    = False
            if past_highs:
                recent_high = max(float(h) for h in past_highs)
                if recent_high > 0:
                    lower = recent_high * (1 - nhs_pct / 100)
                    upper = recent_high * (1 + nhs_pct / 100)
                    near_ok = lower <= current_price <= upper
            result["near_high_support_ok"] = near_ok
            checks.append(near_ok)

        # ── 조건 결합 ──────────────────────────────────────────────────
        if not checks:
            result["match"] = False
        elif mode == "OR":
            result["match"] = any(checks)
        else:  # AND
            result["match"] = all(checks)

        return result
