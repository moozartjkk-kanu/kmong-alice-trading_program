# -*- coding: utf-8 -*-
"""
기술적 분석 모듈 - 이동평균선, main condition 계산

✅ 매수(요약)
- 트리거: 메인 기준(MA) 기준 -19% 도달 시
- 주문가: 일봉 main condition(period=20, percent=20) 하단선(지지선) + 1호가에 지정가 매수

✅ 매도(요구사항 반영)
1) 스탑로스 발생 시: 잔여 물량 100%를 "지정가" 매도
   - 트리거: (기존 로직 유지) 한 번이라도 매도 후, 현재가 <= 평단가
   - 주문가: 현재가를 호가단위로 내림한 가격(지정가)

2) 익절/메인 기준 매도는 "항상" 걸려 있어야 함
   - 평단가 대비 +2.95%에 30%
   - +4.95%에 30%
   - +6.95%에 30%
   - 메인 기준(MA) 가격에 나머지 10%
   => check_sell_signals()는 "현재가 돌파 여부"가 아니라
      "지금 포지션 기준으로 걸어둬야 하는 지정가 매도 주문들"을 반환하도록 구성.
"""

class TechnicalAnalysis:
    """기술적 분석 클래스"""

    @staticmethod
    def calculate_sma(prices, period):
        if not prices or len(prices) < period:
            return None
        try:
            return float(sum(prices[:period]) / period)
        except Exception:
            return None

    @staticmethod
    def calculate_main_condition(ma_price, percent):
        if ma_price is None:
            return None, None
        try:
            upper = float(ma_price) * (1 + float(percent) / 100.0)
            lower = float(ma_price) * (1 - float(percent) / 100.0)
            return upper, lower
        except Exception:
            return None, None

    @staticmethod
    def get_ma_from_candles(candles, period=20):
        if not candles or len(candles) < period:
            return None

        close_prices = []
        for candle in candles[:period]:
            v = candle.get("close")
            try:
                if v is None:
                    return None
                close_prices.append(float(v))
            except Exception:
                return None

        if len(close_prices) < period:
            return None

        return float(sum(close_prices) / period)

    @staticmethod
    def get_main_condition_levels(candles, period=20, percent=20):
        ma = TechnicalAnalysis.get_ma_from_candles(candles, period)
        if ma is None:
            return {"ma": None, "upper": None, "lower": None}

        upper, lower = TechnicalAnalysis.calculate_main_condition(ma, percent)
        return {"ma": ma, "upper": upper, "lower": lower}

    @staticmethod
    def calculate_rsi(candles, period=14):
        """RSI(period) 계산 (candles 최신순). 값 없으면 None 반환."""
        if not candles or len(candles) < period + 1:
            return None
        try:
            closes = [float(c.get("close") or 0) for c in candles[:period + 1]]
            closes.reverse()
            gains, losses = [], []
            for i in range(1, len(closes)):
                d = closes[i] - closes[i - 1]
                gains.append(max(d, 0.0))
                losses.append(max(-d, 0.0))
            avg_gain = sum(gains) / period
            avg_loss = sum(losses) / period
            if avg_loss == 0:
                return 100.0
            rs = avg_gain / avg_loss
            return round(100 - (100 / (1 + rs)), 2)
        except Exception:
            return None

    @staticmethod
    def find_reference_candle(candles, lookback=5, min_rise_pct=3.0, vol_ratio=2.0):
        """
        기준봉 탐색 (candles 최신순, candles[0]=가장 최근).
        조건: 양봉, 상승률 >= min_rise_pct%, 거래량 >= 20일 평균의 vol_ratio배.
        Returns: (index, candle_dict) or None
        """
        if not candles or len(candles) < 21:
            return None
        try:
            avg_vol = sum(float(c.get("volume") or 0) for c in candles[:20]) / 20
        except Exception:
            return None
        for i in range(min(lookback, len(candles) - 1)):
            c = candles[i]
            try:
                o = float(c.get("open") or 0)
                cl = float(c.get("close") or 0)
                vol = float(c.get("volume") or 0)
            except Exception:
                continue
            if o <= 0 or cl <= o:
                continue
            if (cl - o) / o * 100 < min_rise_pct:
                continue
            if avg_vol > 0 and vol < avg_vol * vol_ratio:
                continue
            return (i, c)
        return None


class TradingSignal:
    """매매 신호 분석 클래스"""

    ORDER_TYPE_LIMIT = "limit"      # 지정가
    ORDER_TYPE_MARKET = "market"    # 시장가 (현재 본 코드에서는 스탑로스도 지정가로 변경)

    def __init__(self, config):
        self.config = config
        self.ta = TechnicalAnalysis()

    # ---------------- Config Helpers ----------------
    def _get_cfg_int(self, section, key, default):
        try:
            v = self.config.get(section, key)
            return int(v) if v is not None else default
        except Exception:
            return default

    def _get_cfg_float_list(self, section, key, default):
        try:
            v = self.config.get(section, key)
            if isinstance(v, (list, tuple)) and v:
                return [float(x) for x in v]
        except Exception:
            pass
        return default

    def _get_cfg_int_list(self, section, key, default):
        try:
            v = self.config.get(section, key)
            if isinstance(v, (list, tuple)) and v:
                return [int(x) for x in v]
        except Exception:
            pass
        return default

    # ---------------- Price/Tick Helpers ----------------
    def _get_tick_size(self, price):
        """주가에 따른 호가 단위 반환 (한국거래소 규정)"""
        if price < 1000:
            return 1
        elif price < 5000:
            return 5
        elif price < 10000:
            return 10
        elif price < 50000:
            return 50
        elif price < 100000:
            return 100
        elif price < 500000:
            return 500
        else:
            return 1000

    def _floor_to_tick(self, price):
        """호가 단위로 내림 정렬"""
        if price is None:
            return None
        try:
            p = float(price)
            tick = self._get_tick_size(int(p))
            return (int(p) // tick) * tick
        except Exception:
            return None

    def _ceil_to_tick(self, price):
        """호가 단위로 올림 정렬"""
        if price is None:
            return None
        try:
            p = float(price)
            tick = self._get_tick_size(int(p))
            return ((int(p) + tick - 1) // tick) * tick
        except Exception:
            return None

    # ---------------- Buy Signal ----------------
    def check_buy_signal(self, code, current_price, candles, position=None):
        """
        ✅ 요구사항 반영(이전 요청):
        - 메인 기준(MA) 기준 -19% 값에 도달하면 트리거
        - 트리거 발생 시: main condition(period=20, percent=20) 하단선(지지선) + 1호가에 지정가 매수
        """
        period = self._get_cfg_int("buy", "main_condition_period", 20)
        trigger_percent = self._get_cfg_int("buy", "main_condition_percent", 19)      # 트리거(-19%)
        buy_percent = self._get_cfg_int("buy", "main_condition_buy_percent", 20)      # 지지선(-20%)

        ma = self.ta.get_ma_from_candles(candles, period)
        if ma is None:
            return {"signal": False, "reason": "데이터 부족(MA)"}

        trigger_price = ma * (1 - trigger_percent / 100.0)

        env_buy = self.ta.get_main_condition_levels(candles, period, buy_percent)
        support_lower = env_buy.get("lower")
        if support_lower is None:
            return {"signal": False, "reason": "데이터 부족(main condition)"}

        max_buy_count = self._get_cfg_int("buy", "max_buy_count", 3)
        drop_percent = self._get_cfg_int("buy", "additional_buy_drop_percent", 10)

        # 1차 매수
        if position is None or position.get("quantity", 0) == 0:
            if current_price <= trigger_price:
                support_floor = self._floor_to_tick(support_lower)
                if support_floor is None:
                    return {"signal": False, "reason": "호가 계산 실패"}

                tick = self._get_tick_size(int(support_floor))
                limit_buy_price = support_floor + tick

                return {
                    "signal": True,
                    "buy_count": 1,
                    "reason": (
                        f"1차 매수: {period}일선(MA) 기준 -{trigger_percent}% 도달 "
                        f"(현재가: {current_price:,}, 트리거: {int(trigger_price):,}) / "
                        f"지지선(-{buy_percent}%): {int(support_lower):,} → 지정가: {limit_buy_price:,}"
                    ),
                    "target_price": limit_buy_price,
                    "main_condition_lower": int(support_lower),
                    "ma20": int(ma),  # 기존 키 호환 (의미: period선)
                    "order_type": self.ORDER_TYPE_LIMIT
                }

            return {
                "signal": False,
                "reason": (
                    f"매수 조건 미충족 (현재가: {current_price:,}, "
                    f"트리거(MA-{trigger_percent}%): {int(trigger_price):,})"
                )
            }

        # ✅ 2차/3차 매수는 실시간 가격이 트리거(대상가 + 5호가)에 도달하면 자동 주문됨
        # (trading_logic._check_additional_buy_trigger에서 처리)
        if position.get("sell_occurred", False):
            return {"signal": False, "reason": "매도 발생으로 추가 매수 차단됨"}

        return {
            "signal": False,
            "reason": "보유 중인 종목 - 추가 매수는 트리거 도달 시 자동 주문"
        }

    # ---------------- Sell Planning / Signals ----------------
    def _compute_sell_plan_quantities(self, total_quantity, ratios):
        """
        ratios: [30,30,30,10] 같은 비율 리스트
        - 앞의 항목들은 내림(floor) 처리
        - 마지막 항목은 잔여 전량을 가져가도록 remainder 처리
        """
        if total_quantity <= 0:
            return [0] * len(ratios)

        qs = []
        used = 0
        for i, r in enumerate(ratios):
            if i == len(ratios) - 1:
                q = max(total_quantity - used, 0)
            else:
                q = int(total_quantity * (r / 100.0))
                # 최소 1주 강제는 "주문을 꼭 걸어야 한다"는 요구가 있을 때만 의미가 있는데,
                # 수량이 부족하면 역전이 생길 수 있으므로 여기서는 0 허용(상위에서 필터)
            used += q
            qs.append(q)

        # 혹시 내림 누적으로 used > total_quantity인 경우 방어
        if sum(qs) > total_quantity:
            diff = sum(qs) - total_quantity
            qs[-1] = max(qs[-1] - diff, 0)

        return qs

    def check_sell_signals(self, code, current_price, candles, position):
        """
        ✅ 요구사항 반영:
        - 익절 3구간 + MA(메인 기준) 1구간의 지정가 매도 주문이 "항상" 걸려있도록
          '현재가 돌파 시'가 아니라 '걸어둘 주문 리스트'를 반환.

        - 스탑로스는 (한 번이라도 매도 후) 현재가 <= 평단가면
          잔여물량 100%를 "지정가"로 매도 주문 반환 (최우선)
        """
        if position is None or position.get("quantity", 0) == 0:
            return []

        avg_price = float(position.get("avg_price", 0) or 0)
        current_qty = int(position.get("quantity", 0) or 0)
        sold_targets = position.get("sold_targets", []) or []

        if avg_price <= 0 or current_qty <= 0:
            return []

        # ✅ MA(메인 기준) 계산
        period = self._get_cfg_int("buy", "main_condition_period", 20)
        ma = self.ta.get_ma_from_candles(candles, period)
        ma_target_name = f"{period}일선"

        # ============ 스탑로스 (최고 우선순위) ============
        # 기존 조건 유지: "한 번이라도 매도 후"에만 스탑로스 활성화
        if len(sold_targets) > 0 and "스탑로스" not in sold_targets:
            if current_price <= avg_price:
                # ✅ 지정가로 잔여 전량 매도
                stop_price = self._floor_to_tick(current_price)
                if stop_price is None:
                    stop_price = self._floor_to_tick(avg_price) or int(avg_price)

                return [{
                    "signal": True,
                    "target_name": "스탑로스",
                    "sell_ratio": 100,
                    "sell_quantity": current_qty,
                    "target_price": int(stop_price),          # ✅ 지정가
                    "order_type": self.ORDER_TYPE_LIMIT,      # ✅ 시장가 -> 지정가로 변경
                    "reason": (
                        f"스탑로스: (매도 이력 존재) 현재가({current_price:,}) <= 평단가({int(avg_price):,}) "
                        f"→ 잔여 {current_qty}주 전량 지정가({int(stop_price):,}) 매도"
                    ),
                    "priority": 1
                }]

        # ============ 익절/MA 지정가 매도 주문 '계획' 생성 ============
        profit_targets = self._get_cfg_float_list("sell", "profit_targets", [2.95, 4.95, 6.95])
        profit_ratios = self._get_cfg_int_list("sell", "profit_sell_ratios", [30, 30, 30])
        ma_ratio = self._get_cfg_int("sell", "ma20_sell_ratio", 10)

        # 요구사항대로 강제: 30/30/30/10 형태로 유지되도록(설정이 다르면 기본값 사용)
        # - 사용자가 설정으로 바꿔도 되지만, "반드시"라 하셔서 안전하게 고정 로직으로 맞춥니다.
        target_rates = [2.95, 4.95, 6.95]
        target_ratios = [30, 30, 30, 10]

        # 설정값을 쓰고 싶으면 아래 2줄을 주석 해제하고 위 고정값을 제거하세요.
        # target_rates = profit_targets[:3] if len(profit_targets) >= 3 else [2.95, 4.95, 6.95]
        # target_ratios = (profit_ratios[:3] + [ma_ratio]) if len(profit_ratios) >= 3 else [30, 30, 30, 10]

        # ⚠️ "전체물량 중" 기준 비중이 이상적이지만,
        # 현재 position에 원본 총수량(초기 보유수량)이 없으면 계산이 불가합니다.
        # - 가능하면 position에 initial_quantity를 저장해두세요.
        base_qty = int(position.get("initial_quantity", 0) or 0)
        if base_qty <= 0:
            base_qty = current_qty  # fallback: 현재 잔량 기준으로 비중 나눔

        planned_qs = self._compute_sell_plan_quantities(base_qty, target_ratios)
        q1, q2, q3, q_ma = planned_qs

        # "이미 체결된 타겟"이 있으면 그 주문은 더 이상 걸 필요 없음.
        # 다만 base_qty 기준으로 분할했을 때 이미 일부 체결로 잔량이 줄었을 수 있으니
        # 최종적으로 current_qty를 넘지 않도록 clamp 합니다.
        desired = []
        used_qty = 0

        def _append_order_if_needed(name, qty, price, reason, priority=3):
            nonlocal used_qty
            if qty <= 0:
                return
            if name in sold_targets:
                return
            remain = current_qty - used_qty
            if remain <= 0:
                return
            qty2 = min(qty, remain)
            if qty2 <= 0:
                return
            desired.append({
                "signal": True,
                "target_name": name,
                "sell_ratio": None,             # 계획 주문이므로 ratio 대신 수량 우선
                "sell_quantity": qty2,
                "target_price": int(price),
                "order_type": self.ORDER_TYPE_LIMIT,
                "reason": reason,
                "priority": priority
            })
            used_qty += qty2

        # 익절 1~3 목표가 계산(호가 단위 올림)
        # 목표가 = 평단가*(1+rate)
        raw1 = avg_price * (1 + target_rates[0] / 100.0)
        raw2 = avg_price * (1 + target_rates[1] / 100.0)
        raw3 = avg_price * (1 + target_rates[2] / 100.0)
        p1 = self._ceil_to_tick(raw1) or int(raw1)
        p2 = self._ceil_to_tick(raw2) or int(raw2)
        p3 = self._ceil_to_tick(raw3) or int(raw3)

        _append_order_if_needed(
            "익절1", q1, p1,
            f"익절1 지정가 매도: 평단가({int(avg_price):,}) 대비 +{target_rates[0]}% → {int(p1):,}원, 비중 30%"
        )
        _append_order_if_needed(
            "익절2", q2, p2,
            f"익절2 지정가 매도: 평단가({int(avg_price):,}) 대비 +{target_rates[1]}% → {int(p2):,}원, 비중 30%"
        )
        _append_order_if_needed(
            "익절3", q3, p3,
            f"익절3 지정가 매도: 평단가({int(avg_price):,}) 대비 +{target_rates[2]}% → {int(p3):,}원, 비중 30%"
        )

        # 메인 기준 나머지 10% (지정가)
        if ma is not None:
            ma_price = self._ceil_to_tick(ma) or int(ma)
            # 마지막은 "나머지"가 이상적이므로, q_ma 대신 남은 잔량을 전부 걸어버리는 방식이 안정적
            # (요구: "나머지 10%"지만, rounding/부분체결/기체결 때문에 딱 10%가 불가능할 수 있어 잔량 기준으로 마무리)
            remaining_for_ma = current_qty - used_qty
            if remaining_for_ma > 0 and ma_target_name not in sold_targets:
                desired.append({
                    "signal": True,
                    "target_name": ma_target_name,
                    "sell_ratio": None,
                    "sell_quantity": remaining_for_ma,
                    "target_price": int(ma_price),
                    "order_type": self.ORDER_TYPE_LIMIT,
                    "reason": f"{period}일선 지정가 매도: {int(ma_price):,}원 (잔여 물량 정리)",
                    "priority": 3
                })

        # 우선순위 정렬
        desired.sort(key=lambda x: x.get("priority", 99))
        return desired

    # ---------------- Quantity Helper (기존 호환) ----------------
    def calculate_sell_quantity(self, total_quantity, sell_ratio, explicit_quantity=None):
        if total_quantity <= 0:
            return 0
        if explicit_quantity is not None:
            return min(explicit_quantity, total_quantity)
        if sell_ratio >= 100:
            return total_quantity
        try:
            quantity = int(total_quantity * sell_ratio / 100)  # 내림
        except Exception:
            quantity = 0
        return max(quantity, 1)

    # ==================== 눌림목 전략 ====================
    def check_pullback_buy_signal(self, code, current_price, candles, position=None):
        """
        단기 추세 눌림목 매수 신호 확인.
        조건:
          [추세] MA20 > MA60, 둘 다 전일 대비 상승, 현재가 > MA20
          [기준봉] 최근 5일 이내 양봉+상승률≥3%+거래량≥20일평균2배
          [눌림] 기준봉 이후 1~5봉, 현재가 ≤ 기준봉고가×0.97, 현재가 ≥ 기준봉시가
          [지지] MA20±2% 또는 최근 10일 고점±2% 이내
          [진입] 현재가 > 전일 종가 (양봉 흐름)
          [RSI14] > 40
        """
        if position and position.get("quantity", 0) > 0:
            return {"signal": False, "reason": "이미 보유 중"}

        if not candles or len(candles) < 61:
            return {"signal": False, "reason": "데이터 부족 (최소 61봉 필요)"}

        # ---- 1. 추세 조건 ----
        ma20 = self.ta.get_ma_from_candles(candles, 20)
        ma60 = self.ta.get_ma_from_candles(candles, 60)
        if ma20 is None or ma60 is None:
            return {"signal": False, "reason": "MA 계산 불가"}

        if ma20 <= ma60:
            return {"signal": False, "reason": f"추세 미충족: MA20({ma20:.0f}) <= MA60({ma60:.0f})"}

        if current_price <= ma20:
            return {"signal": False, "reason": f"추세 미충족: 현재가({current_price:,}) <= MA20({ma20:.0f})"}

        # MA20·MA60 전일 대비 상승
        ma20_prev = self.ta.get_ma_from_candles(candles[1:], 20)
        ma60_prev = self.ta.get_ma_from_candles(candles[1:], 60)
        if ma20_prev is None or ma60_prev is None:
            return {"signal": False, "reason": "이전 MA 계산 불가"}
        if ma20 <= ma20_prev:
            return {"signal": False, "reason": f"MA20 하락 중 ({ma20:.0f} <= {ma20_prev:.0f})"}
        if ma60 <= ma60_prev:
            return {"signal": False, "reason": f"MA60 하락 중 ({ma60:.0f} <= {ma60_prev:.0f})"}

        # ---- 2. 기준봉 탐색 (최근 5일 이내) ----
        ref = self.ta.find_reference_candle(candles, lookback=5)
        if ref is None:
            return {"signal": False, "reason": "기준봉 없음 (최근 5일 이내 조건 미충족)"}

        ref_idx, ref_candle = ref
        ref_high = float(ref_candle.get("high") or 0)
        ref_open = float(ref_candle.get("open") or 0)

        # ---- 3. 눌림 조건 (기준봉 이후 1~5봉) ----
        bars_since_ref = ref_idx + 1  # candles[0]이 가장 최근이므로
        if bars_since_ref < 1 or bars_since_ref > 5:
            return {"signal": False, "reason": f"눌림 타이밍 벗어남 (기준봉 이후 {bars_since_ref}봉)"}

        pullback_upper = ref_high * 0.97
        if current_price > pullback_upper:
            return {
                "signal": False,
                "reason": f"눌림 미진행: 현재가({current_price:,}) > 기준봉고가×0.97({pullback_upper:.0f})"
            }
        if ref_open > 0 and current_price < ref_open:
            return {
                "signal": False,
                "reason": f"과도한 눌림: 현재가({current_price:,}) < 기준봉시가({ref_open:.0f})"
            }

        # ---- 4. 지지 조건 ----
        near_ma20 = (ma20 * 0.98) <= current_price <= (ma20 * 1.02)
        try:
            highs_10 = [float(c.get("high") or 0) for c in candles[1:11]]
            recent_10d_high = max(highs_10) if highs_10 else 0
        except Exception:
            recent_10d_high = 0
        near_10d_high = (recent_10d_high > 0 and
                         abs(current_price - recent_10d_high) / recent_10d_high <= 0.02)

        if not near_ma20 and not near_10d_high:
            return {
                "signal": False,
                "reason": (
                    f"지지 미확인: MA20±2%({ma20*0.98:.0f}~{ma20*1.02:.0f})"
                    f" 또는 10일고점±2%({recent_10d_high:.0f}) 미해당"
                )
            }

        # ---- 5. 진입 조건 ----
        # 5-a. 현재가 > 전일 종가 (양봉 흐름)
        try:
            prev_close = float(candles[1].get("close") or 0) if len(candles) > 1 else 0
        except Exception:
            prev_close = 0
        if prev_close > 0 and current_price <= prev_close:
            return {
                "signal": False,
                "reason": f"진입 조건 미충족: 현재가({current_price:,}) <= 전일종가({prev_close:.0f})"
            }

        # 5-b. 거래량 증가: 오늘(candles[0]) > 전일(candles[1])
        # candles[0]은 장 중 TR 조회 시 당일 누적 거래량, [1]은 전일 완성 거래량
        try:
            today_vol = float(candles[0].get("volume") or 0)
            prev_vol = float(candles[1].get("volume") or 0) if len(candles) > 1 else 0
        except Exception:
            today_vol, prev_vol = 0.0, 0.0

        if prev_vol > 0 and today_vol <= prev_vol:
            return {
                "signal": False,
                "reason": (
                    f"거래량 감소: 오늘({int(today_vol):,}) <= 전일({int(prev_vol):,})"
                    f" (오늘/전일 비율: {today_vol/prev_vol*100:.0f}%)"
                )
            }

        # ---- 6. RSI(14) > 40 ----
        rsi = self.ta.calculate_rsi(candles, period=14)
        if rsi is not None and rsi <= 40:
            return {"signal": False, "reason": f"RSI({rsi:.1f}) <= 40"}

        # ---- 기준봉 거래량 정보 (로그용) ----
        try:
            ref_vol = float(ref_candle.get("volume") or 0)
            avg_vol_20 = sum(float(c.get("volume") or 0) for c in candles[:20]) / 20
            ref_vol_ratio = ref_vol / avg_vol_20 if avg_vol_20 > 0 else 0
        except Exception:
            ref_vol, avg_vol_20, ref_vol_ratio = 0.0, 0.0, 0.0

        # ---- 매수 신호 ----
        limit_price = self._ceil_to_tick(current_price) or current_price
        rsi_str = f"{rsi:.1f}" if rsi is not None else "-"
        vol_ratio_str = f"{today_vol/prev_vol*100:.0f}%" if prev_vol > 0 else "-"
        return {
            "signal": True,
            "strategy": "pullback",
            "buy_count": 1,
            "reason": (
                f"[{code}] 눌림목 매수: MA20({ma20:.0f})/MA60({ma60:.0f}) 상승추세, "
                f"기준봉 {bars_since_ref}봉전(거래량 {ref_vol_ratio:.1f}배) 이후 눌림, "
                f"{'MA20' if near_ma20 else '10일고점'} 지지, "
                f"오늘거래량 전일대비 {vol_ratio_str}, RSI({rsi_str})"
            ),
            "target_price": limit_price,
            "ma20": int(ma20),
            "order_type": self.ORDER_TYPE_LIMIT,
        }

    def get_position_summary(self, position, current_price, candles):
        if not position or position.get("quantity", 0) == 0:
            return None

        avg_price = float(position.get("avg_price", 0) or 0)
        quantity = int(position.get("quantity", 0) or 0)
        buy_count = int(position.get("buy_count", 0) or 0)

        period = self._get_cfg_int("buy", "main_condition_period", 20)
        trigger_percent = self._get_cfg_int("buy", "main_condition_percent", 19)
        buy_percent = self._get_cfg_int("buy", "main_condition_buy_percent", 20)

        ma = self.ta.get_ma_from_candles(candles, period)
        env_buy = self.ta.get_main_condition_levels(candles, period, buy_percent)

        trigger_price = int(ma * (1 - trigger_percent / 100.0)) if ma is not None else None

        profit_rate = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
        profit_amount = (current_price - avg_price) * quantity

        # 매도 목표가 리스트 계산 (UI 표시용)
        sell_targets = []
        if avg_price > 0:
            # 실제 매도 로직과 동일한 기준(고정 익절 1~3 + MA)
            target_rates = [2.95, 4.95, 6.95]
            raw1 = avg_price * (1 + target_rates[0] / 100.0)
            raw2 = avg_price * (1 + target_rates[1] / 100.0)
            raw3 = avg_price * (1 + target_rates[2] / 100.0)
            p1 = self._ceil_to_tick(raw1) or int(raw1)
            p2 = self._ceil_to_tick(raw2) or int(raw2)
            p3 = self._ceil_to_tick(raw3) or int(raw3)

            sell_targets.append({"name": "익절1", "price": int(p1)})
            sell_targets.append({"name": "익절2", "price": int(p2)})
            sell_targets.append({"name": "익절3", "price": int(p3)})

            if ma is not None:
                ma_price = self._ceil_to_tick(ma) or int(ma)
                sell_targets.append({"name": f"추가 매도 목표가", "price": int(ma_price)})

        return {
            "avg_price": int(avg_price) if avg_price else 0,
            "quantity": quantity,
            "buy_count": buy_count,
            "current_price": current_price,
            "profit_rate": round(profit_rate, 2),
            "profit_amount": profit_amount,
            "eval_amount": current_price * quantity,
            "ma20": int(ma) if ma is not None else None,
            "trigger_price_ma_minus_percent": trigger_price,
            "main_condition_lower": int(env_buy["lower"]) if env_buy.get("lower") is not None else None,
            "sell_targets": sell_targets,
            "sold_targets": position.get("sold_targets", []) or []
        }


# ---------------- 테스트 ----------------
if __name__ == "__main__":
    test_candles = [{"close": 10000 - i * 100} for i in range(30)]  # 최신순 가정

    class DummyConfig:
        def get(self, section, key):
            return None

    ts = TradingSignal(DummyConfig())

    # 포지션 예시: 초기수량(initial_quantity)을 넣어두면 "전체물량 중 30/30/30/10"에 더 근접합니다.
    position = {
        "avg_price": 9000,
        "quantity": 10,
        "initial_quantity": 10,
        "buy_count": 1,
        "sold_targets": [],         # 체결된 타겟 이름들 기록(있으면 해당 주문은 더 이상 안 걸음)
        "sell_occurred": False
    }

    current_price = 9200
    sell_orders = ts.check_sell_signals("000000", current_price, test_candles, position)
    print("걸어둘 매도 주문들:")
    for o in sell_orders:
        print(o)

    # 스탑로스 조건 테스트: 매도 이력 존재 + 현재가 <= 평단
    position2 = dict(position)
    position2["sold_targets"] = ["익절1"]  # 매도 이력 존재로 간주
    current_price2 = 8900
    stop = ts.check_sell_signals("000000", current_price2, test_candles, position2)
    print("\n스탑로스 주문:")
    for o in stop:
        print(o)
