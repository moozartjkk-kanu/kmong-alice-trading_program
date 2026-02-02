# -*- coding: utf-8 -*-
"""
기술적 분석 모듈 - 이동평균선, 엔벨로프 계산

✅ 매수(요약)
- 트리거: 20일선(MA) 기준 -19% 도달 시
- 주문가: 일봉 엔벨로프(period=20, percent=20) 하단선(지지선) + 1호가에 지정가 매수

✅ 매도(요구사항 반영)
1) 스탑로스 발생 시: 잔여 물량 100%를 "지정가" 매도
   - 트리거: (기존 로직 유지) 한 번이라도 매도 후, 현재가 <= 평단가
   - 주문가: 현재가를 호가단위로 내림한 가격(지정가)

2) 익절/20일선 매도는 "항상" 걸려 있어야 함
   - 평단가 대비 +2.95%에 30%
   - +4.95%에 30%
   - +6.95%에 30%
   - 20일선(MA) 가격에 나머지 10%
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
    def calculate_envelope(ma_price, percent):
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
    def get_envelope_levels(candles, period=20, percent=20):
        ma = TechnicalAnalysis.get_ma_from_candles(candles, period)
        if ma is None:
            return {"ma": None, "upper": None, "lower": None}

        upper, lower = TechnicalAnalysis.calculate_envelope(ma, percent)
        return {"ma": ma, "upper": upper, "lower": lower}


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
        - 20일선(MA) 기준 -19% 값에 도달하면 트리거
        - 트리거 발생 시: 엔벨로프(period=20, percent=20) 하단선(지지선) + 1호가에 지정가 매수
        """
        period = self._get_cfg_int("buy", "envelope_period", 20)
        trigger_percent = self._get_cfg_int("buy", "envelope_percent", 19)      # 트리거(-19%)
        buy_percent = self._get_cfg_int("buy", "envelope_buy_percent", 20)      # 지지선(-20%)

        ma = self.ta.get_ma_from_candles(candles, period)
        if ma is None:
            return {"signal": False, "reason": "데이터 부족(MA)"}

        trigger_price = ma * (1 - trigger_percent / 100.0)

        env_buy = self.ta.get_envelope_levels(candles, period, buy_percent)
        support_lower = env_buy.get("lower")
        if support_lower is None:
            return {"signal": False, "reason": "데이터 부족(엔벨로프)"}

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
                    "envelope_lower": int(support_lower),
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

        # ✅ 2차/3차 매수는 1차 매수 시 미리 주문이 걸려있으므로 여기서는 신호 발생하지 않음
        # (1차 매수 주문 전송 시 _place_additional_buy_orders에서 2차/3차 주문도 함께 전송됨)
        if position.get("sell_occurred", False):
            return {"signal": False, "reason": "매도 발생으로 추가 매수 차단됨"}

        return {
            "signal": False,
            "reason": "보유 중인 종목 - 2차/3차 매수 주문은 이미 걸려있음"
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
        - 익절 3구간 + MA(20일선) 1구간의 지정가 매도 주문이 "항상" 걸려있도록
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

        # ✅ MA(20일선) 계산
        period = self._get_cfg_int("buy", "envelope_period", 20)
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

        # 20일선 나머지 10% (지정가)
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

    def get_position_summary(self, position, current_price, candles):
        if not position or position.get("quantity", 0) == 0:
            return None

        avg_price = float(position.get("avg_price", 0) or 0)
        quantity = int(position.get("quantity", 0) or 0)
        buy_count = int(position.get("buy_count", 0) or 0)

        period = self._get_cfg_int("buy", "envelope_period", 20)
        trigger_percent = self._get_cfg_int("buy", "envelope_percent", 19)
        buy_percent = self._get_cfg_int("buy", "envelope_buy_percent", 20)

        ma = self.ta.get_ma_from_candles(candles, period)
        env_buy = self.ta.get_envelope_levels(candles, period, buy_percent)

        trigger_price = int(ma * (1 - trigger_percent / 100.0)) if ma is not None else None

        profit_rate = ((current_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0
        profit_amount = (current_price - avg_price) * quantity

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
            "envelope_lower": int(env_buy["lower"]) if env_buy.get("lower") is not None else None,
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