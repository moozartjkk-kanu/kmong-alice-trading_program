# -*- coding: utf-8 -*-
"""
자동매매 로직 모듈 (StopLoss 지정가 100% 매도 + 익일 재주문 유지)

✅ 요구사항 반영:
1. 20일선 기준 -19% 도달 시 엔벨로프(period20, percent20) 하단선+1호가에 지정가 매수
2. 스탑로스 시 지정가로 100% 전부 매도
3. 매도 주문은 매수 체결 직후 즉시 모두 걸어둠:
   - 평단가 +2.95%에 30%, +4.95%에 30%, +6.95%에 30%, 20일선에 10%
4. 스탑로스 발동 시: 미체결 매수 취소, 100% 매도까지 스탑로스 유지, 익일 재주문
5. 프로그램 재시작 시 상태 복원
6. 장 종료 후 미체결 주문 저장 및 다음 장 시작 시 복원
7. 매수/매도 상태 완전 추적 (1차/2차/3차, 익절1/2/3/20일선)
"""

from datetime import datetime, time as dt_time
import time
import queue
import threading

from technical_analysis import TechnicalAnalysis, TradingSignal
from event_engine import init_engine


class AutoTrader:
    """자동매매 실행 클래스"""

    # 장 시간 설정
    MARKET_OPEN_TIME = dt_time(9, 0, 0)       # 장 시작
    MARKET_CLOSE_TIME = dt_time(15, 30, 0)    # 장 마감
    PRE_MARKET_TIME = dt_time(8, 30, 0)       # 동시호가 시작

    # 매도 목표 설정 (고정)
    PROFIT_TARGETS = [2.95, 4.95, 6.95]  # 익절 목표 수익률 (%)
    PROFIT_RATIOS = [30, 30, 30]         # 익절 비중 (%)
    MA_SELL_RATIO = 10                    # 20일선 매도 비중 (%)

    def __init__(self, kiwoom_api, config):
        """
        Args:
            kiwoom_api: KiwoomAPI 인스턴스
            config: Config 인스턴스
        """
        self.kiwoom = kiwoom_api
        self.config = config
        self.signal = TradingSignal(config)
        self.ta = TechnicalAnalysis()

        # 미체결 주문 추적 (매수 주문)
        self.pending_buy_orders = {}  # {code: [order_info, ...]}

        # 이미 걸어둔 매도 주문 추적 (중복 주문 방지)
        self.placed_sell_orders = {}  # {code: {"익절1": True, "익절2": True, ...}}

        self.is_running = False
        self.account = None

        # 로그 콜백
        self.log_callback = None

        # 이벤트 엔진
        self.event_engine = None


        # ==================== 비동기 처리(실시간 콜백 경량화) ====================
        # 실시간 콜백에서는 큐 적재만 하고, 조건 계산은 워커 스레드에서 수행
        self.tick_queue = queue.Queue(maxsize=5000)   # (code, price, ts)
        self.order_queue = queue.Queue(maxsize=5000)  # 주문 의도(intent) 큐

        self._latest_price = {}  # {code: last_price}
        self._pending_order_codes = set()  # {(code, intent_type)} 중복 방지
        self._order_min_interval = 0.35    # 주문 전송 최소 간격(초)
        self._last_order_ts = 0.0

        self._worker_stop = threading.Event()
        self._signal_worker = None
        # 주문 복원 완료 여부
        self.orders_restored = False

        # ✅ 자동매도 체결 추적 (수동매도 구분용)
        self._auto_sell_executed = {}  # {code: True} - 자동매도 체결 시 설정, balance 처리 후 리셋

        # 체결 콜백 설정
        if self.kiwoom:
            self.kiwoom.set_chejan_callback(self._on_order_executed)

    # ==================== 로그 ====================
    def set_log_callback(self, callback):
        """로그 콜백 설정"""
        self.log_callback = callback

    def log(self, message, level="INFO"):
        """로그 출력"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {message}"
        print(log_msg)
        if self.log_callback:
            self.log_callback(log_msg)

    # ==================== 기본 ====================
    def set_account(self, account):
        """계좌 설정"""
        self.account = account
        self.config.set_account_number(account)

    def start(self):
        """자동매매 시작"""
        if not self.kiwoom or not self.kiwoom.is_connected():
            self.log("키움 API에 연결되어 있지 않습니다.", "ERROR")
            return False
        if not self.account:
            self.log("계좌가 설정되지 않았습니다.", "ERROR")
            return False

        self.is_running = True
        self.log("자동매매를 시작합니다.")

        # 프로그램 시작 시 전체 상태 동기화(권장)
        try:
            self.full_state_sync_on_startup()
        except Exception as e:
            self.log(f"시작 동기화 중 오류: {e}", "ERROR")

        # 감시 종목 및 보유 종목 가져오기
        watchlist = self.config.get_watchlist()
        watchlist_codes = [item["code"] for item in watchlist] if watchlist else []

        # 보유 종목 (우선순위)
        positions = self.config.get("positions") or {}
        priority_codes = [code for code, pos in positions.items() if pos.get("quantity", 0) > 0]

        # 이벤트 엔진 초기화 및 시작
        self.event_engine = init_engine(self.kiwoom, self.config, self.log_callback)
        self.event_engine.on_price_update = self._on_realtime_price_enqueue
        self.event_engine.on_trade_signal = self._on_trade_signal
        self.event_engine.start(watchlist_codes, priority_codes)


        # 신호 워커 스레드 시작 (조건 계산/신호 생성 전용)
        if not self._signal_worker or not self._signal_worker.is_alive():
            self._worker_stop.clear()
            self._signal_worker = threading.Thread(target=self._signal_worker_loop, daemon=True)
            self._signal_worker.start()
        self.log(f"감시 종목: {len(watchlist_codes)}개, 보유 종목(우선): {len(priority_codes)}개")
        return True

    def stop(self):
        """자동매매 중지"""
        self.is_running = False
        self.log("자동매매를 중지합니다.")


        # 워커 정지
        self._worker_stop.set()
        if self.event_engine:
            self.event_engine.stop()
            self.event_engine = None

    # ==================== 시간 체크 ====================
    def is_market_open(self):
        """장이 열려있는지 확인 (동시호가 포함)"""
        now = datetime.now().time()
        return self.PRE_MARKET_TIME <= now <= self.MARKET_CLOSE_TIME

    def is_trading_time(self):
        """거래 시간인지 확인 (동시호가 제외)"""
        now = datetime.now().time()
        return self.MARKET_OPEN_TIME <= now <= self.MARKET_CLOSE_TIME

    # ==================== 이벤트 콜백 ====================
    def _on_realtime_price_enqueue(self, code, price):
        """실시간 가격 업데이트 콜백(경량): 파싱 + 큐 적재만 수행"""
        if not self.is_running:
            return
        try:
            p = int(price) if price is not None else 0
        except Exception:
            return
        self._latest_price[code] = p
        try:
            self.tick_queue.put_nowait((code, p, time.time()))
        except queue.Full:
            # 큐가 꽉 차면 가장 오래된 이벤트를 일부 버리고 최신만 반영
            try:
                _ = self.tick_queue.get_nowait()
                self.tick_queue.put_nowait((code, p, time.time()))
            except Exception:
                pass

    def _on_trade_signal(self, code, signal_type, data):
        """매매 신호 콜백 (확장용)"""
        pass

    # ==================== 매매 조건 ====================

    # ==================== 워커(조건 계산) ====================
    def _signal_worker_loop(self):
        """tick_queue 소비 → (캐시된 candles로) 조건 계산 → 주문 의도(order_queue)에 적재"""
        while not self._worker_stop.is_set():
            try:
                code, price, ts = self.tick_queue.get(timeout=0.5)
            except Exception:
                continue

            if not self.is_running or not self.event_engine:
                continue

            # 봉데이터는 캐시만 사용 (TR 호출 금지)
            candles = None
            try:
                candles = self.event_engine.batch_scheduler.get_cached_candles(code)
            except Exception:
                candles = None

            if not candles:
                continue

            intents = self._evaluate_intents(code, price, candles)
            for intent in intents:
                try:
                    self.order_queue.put_nowait(intent)
                except queue.Full:
                    break

    def _evaluate_intents(self, code, current_price, candles):
        """주문을 직접 실행하지 않고, 실행해야 할 주문 의도(intent) 목록을 반환"""
        intents = []
        try:
            position = self.config.get_position(code)

            # 0) 스탑로스 주문 유지(필요 시)
            intents.append({"type": "ensure_stoploss", "code": code, "position": position})

            # 1) 스탑로스 발동 조건
            if position and position.get("quantity", 0) > 0:
                if self._should_trigger_stoploss(code, current_price, position):
                    intents.append({"type": "stoploss", "code": code, "price": current_price, "position": position})
                    return intents  # 스탑로스 최우선

            # 2) 보유 종목 매도 주문 보정
            if position and position.get("quantity", 0) > 0:
                intents.append({"type": "ensure_sell", "code": code, "position": position, "candles": candles})

            # 3) 매수 신호 (후순위)
            position = self.config.get_position(code)  # 갱신
            if position and position.get("stoploss_triggered", False):
                return intents
            if position and position.get("sell_occurred", False):
                return intents

            buy_signal = self.signal.check_buy_signal(code, current_price, candles, position)
            if buy_signal.get("signal"):
                if buy_signal.get("buy_count") == 1 and not self._can_buy_new_stock():
                    return intents
                intents.append({"type": "buy", "code": code, "price": current_price,
                                "buy_signal": buy_signal, "candles": candles})
        except Exception as e:
            self.log(f"[{code}] 워커 조건 계산 오류: {e}", "ERROR")
        return intents

    # ==================== 주문 큐 처리(메인 스레드) ====================
    def process_order_queue(self, max_per_tick=1):
        """메인(UI) 스레드에서 주기적으로 호출해 order_queue를 처리"""
        if not self.is_running:
            return
        processed = 0
        while processed < max_per_tick:
            try:
                intent = self.order_queue.get_nowait()
            except Exception:
                return

            intent_type = intent.get("type")
            code = intent.get("code")
            key = (code, intent_type)

            # 중복 방지(특히 buy/stoploss)
            if intent_type in ("buy", "stoploss"):
                if key in self._pending_order_codes:
                    continue
                self._pending_order_codes.add(key)

            # 주문 속도 제한
            now = time.time()
            if now - self._last_order_ts < self._order_min_interval:
                # 다시 큐잉(슬립 금지)
                try:
                    self.order_queue.put_nowait(intent)
                except Exception:
                    pass
                if key in self._pending_order_codes and intent_type in ("buy", "stoploss"):
                    self._pending_order_codes.discard(key)
                return

            try:
                if intent_type == "ensure_stoploss":
                    self._ensure_stoploss_order_if_needed(code, intent.get("position"))
                elif intent_type == "ensure_sell":
                    self._ensure_sell_orders_placed(code, intent.get("position"), intent.get("candles"))
                elif intent_type == "stoploss":
                    self._execute_stoploss(code, intent.get("price"), intent.get("position"))
                elif intent_type == "buy":
                    self._execute_buy(code, intent.get("price"), intent.get("buy_signal"), intent.get("candles"))

                self._last_order_ts = time.time()
            except Exception as e:
                self.log(f"[{code}] 주문 처리 오류({intent_type}): {e}", "ERROR")
                try:
                    self.order_queue.put_nowait(intent)
                except Exception:
                    pass
                if key in self._pending_order_codes and intent_type in ("buy", "stoploss"):
                    self._pending_order_codes.discard(key)

            processed += 1

    def _check_trading_conditions(self, code, current_price, candles):
        """
        매매 조건 확인 (이벤트 엔진에서 호출)

        ✅ 요구사항 반영:
        - 우선순위: 스탑로스 > 매도 주문 유지/보정 > 매수 신호
        - 스탑로스 발동 종목은 추가 매수 차단
        - 매도가 한번이라도 발생하면 추가 매수 차단
        """
        try:
            position = self.config.get_position(code)

            # 0) 스탑로스 유지 강제 (스탑로스가 한번이라도 발동된 보유종목은 항상 주문 유지)
            self._ensure_stoploss_order_if_needed(code, position)

            # 1) 스탑로스 조건 체크 (매도 이력 존재 + 현재가 <= 평단가)
            if position and position.get("quantity", 0) > 0:
                # 스탑로스 조건: 첫 매도 발생 후 현재가가 평단가 이하로 떨어지면 스탑로스 발동
                if self._should_trigger_stoploss(code, current_price, position):
                    self._execute_stoploss(code, current_price, position)
                    return

            # 2) 보유 종목에 대해 매도 주문이 모두 걸려있는지 확인/보정
            if position and position.get("quantity", 0) > 0:
                self._ensure_sell_orders_placed(code, position, candles)

            # 3) 매수 신호 확인 (매도보다 후순위)
            position = self.config.get_position(code)  # 갱신

            # ✅ 스탑로스 발동 이력이 있으면 추가 매수 차단
            if position and position.get("stoploss_triggered", False):
                return

            # ✅ 매도가 한번이라도 발생했으면 추가 매수 차단
            if position and position.get("sell_occurred", False):
                return

            buy_signal = self.signal.check_buy_signal(code, current_price, candles, position)

            if buy_signal.get("signal"):
                # 신규 매수인 경우 최대 보유 종목수 체크
                if buy_signal.get("buy_count") == 1:
                    if not self._can_buy_new_stock():
                        return

                self._execute_buy(code, current_price, buy_signal, candles)

        except Exception as e:
            self.log(f"[{code}] 매매 조건 확인 오류: {e}", "ERROR")

    def _should_trigger_stoploss(self, code, current_price, position):
        """
        스탑로스 발동 조건 확인

        조건: 첫 매도 발생(sold_targets가 비어있지 않음) 후 현재가 <= 평단가
        """
        if not position:
            return False

        # 이미 스탑로스 발동된 상태면 False (이미 주문 걸려있음)
        if position.get("stoploss_triggered", False):
            return False

        sold_targets = position.get("sold_targets", [])
        if not sold_targets:
            return False

        if "스탑로스" in sold_targets:
            return False

        avg_price = float(position.get("avg_price", 0) or 0)
        if avg_price <= 0:
            return False

        return current_price <= avg_price

    def _execute_stoploss(self, code, current_price, position):
        """
        ✅ 스탑로스 실행 (지정가 100% 매도)

        요구사항:
        - 스탑로스 발동 시 미체결 매수 주문 모두 취소
        - 100% 지정가 매도
        - 스탑로스 상태 영구 기록 (익일 재주문용)
        """
        try:
            total_quantity = position.get("quantity", 0)
            if total_quantity <= 0:
                return

            avg_price = float(position.get("avg_price", 0) or 0)

            # 스탑로스 가격: 현재가를 호가단위로 내림
            tick = self._get_tick_size(current_price)
            stoploss_price = (int(current_price) // tick) * tick
            if stoploss_price <= 0:
                stoploss_price = max(1, int(avg_price) - tick)

            stock_name = position.get("name", code)
            self.log(f"[{code} {stock_name}] 스탑로스 발동: 현재가({current_price:,}) <= 평단가({int(avg_price):,})", "WARNING")

            # 1) 해당 종목의 모든 미체결 주문 취소 (매수 + 기존 매도)
            try:
                self.kiwoom.cancel_all_orders_for_stock(self.account, code)
            except Exception as ce:
                self.log(f"[{code}] 스탑로스 전 주문취소 중 오류: {ce}", "ERROR")

            # 2) 매수 미체결(저장된 데이터 포함) 정리
            self._cancel_pending_buy_orders(code)

            # 3) 기존 매도 주문 정리 (스탑로스 제외 모두 취소/삭제)
            self.config.clear_pending_orders_for_stock(code, order_type="sell")
            if code in self.placed_sell_orders:
                del self.placed_sell_orders[code]

            # 4) 포지션에 스탑로스 상태 영구 기록
            position["sell_occurred"] = True
            position["stoploss_triggered"] = True
            position["stoploss_price"] = stoploss_price
            sold_targets = position.get("sold_targets", [])
            if "스탑로스" not in sold_targets:
                sold_targets.append("스탑로스")
            position["sold_targets"] = sold_targets
            position["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.config.update_position(code, position)

            # 5) 스탑로스 주문 저장 (익일 재주문용, persist=True)
            self.config.save_pending_order(code, {
                "order_type": "sell",
                "quantity": total_quantity,
                "price": stoploss_price,
                "target_name": "스탑로스",
                "sell_ratio": 100,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "persist": True
            })

            # 6) 주문 전송 (지정가)
            result = self._send_sell_with_retry(code, total_quantity, stoploss_price)

            if result == 0:
                self.log(f"[{code}] 스탑로스 지정가 매도 주문 전송 성공: {total_quantity}주 @ {stoploss_price:,}원", "SUCCESS")
            else:
                self.log(f"[{code}] 스탑로스 지정가 매도 주문 실패: 에러코드 {result}", "ERROR")

        except Exception as e:
            self.log(f"[{code}] 스탑로스 실행 중 오류: {e}", "ERROR")

    def _can_buy_new_stock(self):
        """신규 종목 매수 가능 여부 확인 (최대 보유 종목수 체크)"""
        max_holding = self.config.get("buy", "max_holding_stocks") or 3
        positions = self.config.get("positions") or {}
        current_holding_count = sum(1 for pos in positions.values() if pos.get("quantity", 0) > 0)
        return current_holding_count < max_holding

    # ==================== 매도 주문 선제적 걸기 ====================
    def _ensure_sell_orders_placed(self, code, position, candles):
        """
        ✅ 보유 종목에 대해 모든 매도 주문이 걸려있는지 확인하고 없으면 걸기

        요구사항: 매수 체결 시점에 바로 모든 매도 주문(익절1~3 + 20일선)이 걸려있어야 함
        - 평단가 +2.95%에 30%
        - 평단가 +4.95%에 30%
        - 평단가 +6.95%에 30%
        - 20일선 가격에 나머지 10%
        """
        if not position or position.get("quantity", 0) <= 0:
            return

        # 스탑로스 발동 종목은 스탑로스 주문만 유지
        if position.get("stoploss_triggered", False):
            return

        avg_price = float(position.get("avg_price", 0) or 0)
        if avg_price <= 0:
            return

        # 이미 체결된 타겟은 주문 걸지 않음
        sold_targets = position.get("sold_targets", [])

        # 이미 걸어둔 주문 확인
        if code not in self.placed_sell_orders:
            self.placed_sell_orders[code] = {}

        # 초기 수량 (전체물량 기준 비중 계산용)
        initial_qty = int(position.get("initial_quantity", 0) or 0)
        current_qty = int(position.get("quantity", 0) or 0)
        if initial_qty <= 0:
            initial_qty = current_qty

        # 20일선 가격 계산
        period = 20
        ma20 = self.ta.get_ma_from_candles(candles, period)

        # 매도 주문 계획 생성
        sell_orders_to_place = self._calculate_sell_orders(
            avg_price, initial_qty, current_qty, ma20, sold_targets
        )

        # 주문 걸기
        for order in sell_orders_to_place:
            target_name = order["target_name"]

            # 이미 걸어둔 주문인지 확인
            if self.placed_sell_orders[code].get(target_name, False):
                continue

            # 이미 체결된 타겟인지 확인
            if target_name in sold_targets:
                continue

            # 장 시간 외면 주문 걸지 않음 (저장만)
            if not self.is_market_open():
                self.config.save_pending_order(code, {
                    "order_type": "sell",
                    "quantity": order["quantity"],
                    "price": order["price"],
                    "target_name": target_name,
                    "sell_ratio": order.get("sell_ratio", 0),
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                continue

            # 주문 전송
            result = self.kiwoom.sell_stock(self.account, code, order["quantity"], order["price"])

            if result == 0:
                self.log(f"[{code}] {target_name} 매도 주문 설정: {order['quantity']}주 @ {order['price']:,}원", "SUCCESS")
                self.placed_sell_orders[code][target_name] = True

                # 미체결 주문 저장
                self.config.save_pending_order(code, {
                    "order_type": "sell",
                    "quantity": order["quantity"],
                    "price": order["price"],
                    "target_name": target_name,
                    "sell_ratio": order.get("sell_ratio", 0),
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
            else:
                self.log(f"[{code}] {target_name} 매도 주문 실패: 에러코드 {result}", "ERROR")

    def _calculate_sell_orders(self, avg_price, initial_qty, current_qty, ma20, sold_targets):
        """
        ✅ 매도 주문 계획 계산

        전체물량 기준:
        - 익절1 (+2.95%): 30%
        - 익절2 (+4.95%): 30%
        - 익절3 (+6.95%): 30%
        - 20일선: 나머지 10%
        """
        orders = []

        # 비중 계산 (초기 수량 기준)
        q1 = int(initial_qty * 0.30)  # 30%
        q2 = int(initial_qty * 0.30)  # 30%
        q3 = int(initial_qty * 0.30)  # 30%
        q_ma = initial_qty - q1 - q2 - q3  # 나머지 (약 10%)

        # 최소 1주 보장
        if q1 <= 0:
            q1 = 1
        if q2 <= 0:
            q2 = 1
        if q3 <= 0:
            q3 = 1
        if q_ma <= 0:
            q_ma = 1

        # 이미 체결된 타겟들의 수량을 현재 수량에서 제외하여 계산
        used_qty = 0

        # 익절1: +2.95%
        if "익절1" not in sold_targets and used_qty + q1 <= current_qty:
            price1 = self._ceil_to_tick(avg_price * 1.0295)
            if price1:
                orders.append({
                    "target_name": "익절1",
                    "quantity": min(q1, current_qty - used_qty),
                    "price": price1,
                    "sell_ratio": 30
                })
                used_qty += min(q1, current_qty - used_qty)

        # 익절2: +4.95%
        if "익절2" not in sold_targets and used_qty + q2 <= current_qty:
            price2 = self._ceil_to_tick(avg_price * 1.0495)
            if price2:
                orders.append({
                    "target_name": "익절2",
                    "quantity": min(q2, current_qty - used_qty),
                    "price": price2,
                    "sell_ratio": 30
                })
                used_qty += min(q2, current_qty - used_qty)

        # 익절3: +6.95%
        if "익절3" not in sold_targets and used_qty + q3 <= current_qty:
            price3 = self._ceil_to_tick(avg_price * 1.0695)
            if price3:
                orders.append({
                    "target_name": "익절3",
                    "quantity": min(q3, current_qty - used_qty),
                    "price": price3,
                    "sell_ratio": 30
                })
                used_qty += min(q3, current_qty - used_qty)

        # 20일선: 나머지
        if ma20 and "20일선" not in sold_targets:
            remaining = current_qty - used_qty
            if remaining > 0:
                ma_price = self._ceil_to_tick(ma20)
                if ma_price:
                    orders.append({
                        "target_name": "20일선",
                        "quantity": remaining,
                        "price": ma_price,
                        "sell_ratio": 10
                    })

        return orders

    def _ceil_to_tick(self, price):
        """호가 단위로 올림"""
        if price is None:
            return None
        try:
            p = float(price)
            tick = self._get_tick_size(int(p))
            return ((int(p) + tick - 1) // tick) * tick
        except Exception:
            return None

    def place_all_sell_orders_for_position(self, code, position, candles):
        """
        ✅ 매수 체결 직후 모든 매도 주문을 즉시 걸기

        이 메서드는 매수 체결 콜백에서 호출됨
        """
        if not self.is_market_open():
            self.log(f"[{code}] 장 시간 외 - 매도 주문은 장 시작 시 복원됩니다.", "INFO")
            return

        self._ensure_sell_orders_placed(code, position, candles)

    # ==================== 주문 단위 ====================
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

    # ==================== 매수 ====================
    def _execute_buy(self, code, current_price, signal, candles):
        """
        ✅ 매수 실행 (지정가 매수: 엔벨로프 하한선 + 1호가)

        요구사항 반영:
        - 스탑로스 발동 이력 종목은 재매수 차단
        - 매도가 한번이라도 발생하면 당일 재매수 완전 차단 (재진입 불가)
        - 1차 매수 시 2차/3차 매수 주문도 미리 걸기
        - 매수 체결 시 즉시 모든 매도 주문 설정
        """
        try:
            buy_count = signal.get("buy_count", 1)
            reason = signal.get("reason", "")
            ma20 = signal.get("ma20", 0)

            buy_price = signal.get("target_price", 0)
            if buy_price <= 0:
                buy_price = current_price

            position = self.config.get_position(code)

            # ✅ 스탑로스 발동 이력 종목은 재매수 차단
            if position and position.get("stoploss_triggered", False):
                self.log(f"[{code}] 스탑로스 발동 이력 종목 - 재매수 차단", "WARNING")
                return

            # ✅ 매도가 한번이라도 발생하면 당일 재매수 완전 차단
            if position and position.get("sell_occurred", False):
                self.log(f"[{code}] 매도 발생 종목 - 당일 재매수 차단", "WARNING")
                return

            # ✅ 이전에 포지션이 있었던 종목은 재매수 차단 (재진입 불가)
            if buy_count == 1 and position:
                if position.get("quantity", 0) == 0 and position.get("avg_price", 0) > 0:
                    self.log(f"[{code}] 이전 매도 이력 존재 - 당일 재매수 차단", "WARNING")
                    return

            buy_amount = self.config.get("buy", "buy_amount_per_stock")
            quantity = buy_amount // buy_price
            if quantity <= 0:
                self.log(f"[{code}] 매수 수량이 0입니다. (매수금액: {buy_amount:,}, 매수가: {buy_price:,})", "WARNING")
                return

            stock_name = self.kiwoom.get_master_code_name(code)
            self.log(f"[{code} {stock_name}] 매수 신호 발생: {reason}")

            # 주문 실패 시 재시도 로직
            retry_count = self.config.get("error_handling", "order_retry_count") or 3
            retry_interval = (self.config.get("error_handling", "order_retry_interval_ms") or 1000) / 1000.0

            result = -1
            for attempt in range(retry_count):
                result = self.kiwoom.buy_stock(self.account, code, quantity, buy_price)
                if result == 0:
                    break
                if attempt < retry_count - 1:
                    self.log(f"[{code}] 매수 주문 재시도 {attempt + 2}/{retry_count}", "WARNING")
                    time.sleep(retry_interval)

            if result == 0:
                self.log(f"[{code}] {buy_count}차 매수 주문 전송 성공: {quantity}주 @ {buy_price:,}원 (지정가)", "SUCCESS")

                # 미체결 매수 주문 추적 (메모리)
                if code not in self.pending_buy_orders:
                    self.pending_buy_orders[code] = []
                self.pending_buy_orders[code].append({
                    "buy_count": buy_count,
                    "quantity": quantity,
                    "price": buy_price,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })

                # 미체결 주문 저장(파일)
                self.config.save_pending_order(code, {
                    "order_type": "buy",
                    "quantity": quantity,
                    "price": buy_price,
                    "buy_count": buy_count,
                    "ma20": ma20,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })

                # 포지션 정보 기록 (체결 전 임시 기록)
                position = self.config.get_position(code) or {}
                old_quantity = position.get("quantity", 0)

                if buy_count == 1 and old_quantity == 0:
                    new_position = {
                        "code": code,
                        "name": stock_name,
                        "quantity": 0,
                        "avg_price": 0,
                        "buy_count": buy_count,
                        "last_buy_price": buy_price,
                        "target_buy_price": buy_price,
                        "first_buy_price": buy_price,  # ✅ 1차 매수가 저장 (2차/3차 계산용)
                        "ma20": ma20,
                        "sold_targets": [],
                        "sell_occurred": False,
                        # ✅ 초기 수량 (전체물량 기준 비중 계산용) - 체결 시 업데이트됨
                        "initial_quantity": 0,
                        # 스탑로스 유지용 상태
                        "stoploss_triggered": False,
                        "stoploss_price": 0,
                        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    self.config.update_position(code, new_position)

                    # ✅ 1차 매수 시 2차/3차 매수 주문도 미리 걸기
                    self._place_additional_buy_orders(code, buy_price, ma20)

                else:
                    position["buy_count"] = buy_count
                    position["last_buy_price"] = buy_price
                    position["target_buy_price"] = buy_price
                    position["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.config.update_position(code, position)

            else:
                self.log(f"[{code}] 매수 주문 실패 (재시도 {retry_count}회 후): 에러코드 {result}", "ERROR")

        except Exception as e:
            self.log(f"[{code}] 매수 실행 중 오류: {e}", "ERROR")

    def _place_additional_buy_orders(self, code, first_buy_price, ma20):
        """
        ✅ 1차 매수 주문 전송 후 2차/3차 매수 주문도 미리 걸기

        요구사항:
        - 2차 주문: 1차 주문 가격의 -10%
        - 3차 주문: 2차 주문 가격의 -10% (= 1차 × 0.81)
        """
        try:
            max_buy_count = self.config.get("buy", "max_buy_count") or 3
            drop_percent = self.config.get("buy", "additional_buy_drop_percent") or 10
            buy_amount = self.config.get("buy", "buy_amount_per_stock")

            # 2차 매수가: 1차 × (1 - drop_percent/100)
            second_buy_price_raw = first_buy_price * (1 - drop_percent / 100.0)
            second_buy_price = self._floor_to_tick(second_buy_price_raw)
            if second_buy_price is None or second_buy_price <= 0:
                self.log(f"[{code}] 2차 매수가 계산 실패", "WARNING")
                return

            tick = self._get_tick_size(int(second_buy_price))
            second_buy_price = second_buy_price + tick  # 호가단위 +1호가

            # 3차 매수가: 2차 × (1 - drop_percent/100) = 1차 × 0.81
            third_buy_price_raw = second_buy_price * (1 - drop_percent / 100.0)
            third_buy_price = self._floor_to_tick(third_buy_price_raw)
            if third_buy_price is None or third_buy_price <= 0:
                third_buy_price = 0

            if third_buy_price > 0:
                tick3 = self._get_tick_size(int(third_buy_price))
                third_buy_price = third_buy_price + tick3

            # 2차 매수 주문 걸기
            if max_buy_count >= 2 and second_buy_price > 0:
                quantity_2 = buy_amount // second_buy_price
                if quantity_2 > 0:
                    result_2 = self.kiwoom.buy_stock(self.account, code, quantity_2, second_buy_price)
                    if result_2 == 0:
                        self.log(f"[{code}] 2차 매수 예약 주문 전송: {quantity_2}주 @ {second_buy_price:,}원", "SUCCESS")

                        # 미체결 주문 저장
                        self.config.save_pending_order(code, {
                            "order_type": "buy",
                            "quantity": quantity_2,
                            "price": second_buy_price,
                            "buy_count": 2,
                            "ma20": ma20,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })

                        if code not in self.pending_buy_orders:
                            self.pending_buy_orders[code] = []
                        self.pending_buy_orders[code].append({
                            "buy_count": 2,
                            "quantity": quantity_2,
                            "price": second_buy_price,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                    else:
                        self.log(f"[{code}] 2차 매수 예약 주문 실패: 에러코드 {result_2}", "ERROR")

            # 3차 매수 주문 걸기
            if max_buy_count >= 3 and third_buy_price > 0:
                quantity_3 = buy_amount // third_buy_price
                if quantity_3 > 0:
                    result_3 = self.kiwoom.buy_stock(self.account, code, quantity_3, third_buy_price)
                    if result_3 == 0:
                        self.log(f"[{code}] 3차 매수 예약 주문 전송: {quantity_3}주 @ {third_buy_price:,}원", "SUCCESS")

                        # 미체결 주문 저장
                        self.config.save_pending_order(code, {
                            "order_type": "buy",
                            "quantity": quantity_3,
                            "price": third_buy_price,
                            "buy_count": 3,
                            "ma20": ma20,
                            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })

                        if code not in self.pending_buy_orders:
                            self.pending_buy_orders[code] = []
                        self.pending_buy_orders[code].append({
                            "buy_count": 3,
                            "quantity": quantity_3,
                            "price": third_buy_price,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                    else:
                        self.log(f"[{code}] 3차 매수 예약 주문 실패: 에러코드 {result_3}", "ERROR")

        except Exception as e:
            self.log(f"[{code}] 추가 매수 주문 설정 중 오류: {e}", "ERROR")

    def _floor_to_tick(self, price):
        """호가 단위로 내림"""
        if price is None:
            return None
        try:
            p = float(price)
            tick = self._get_tick_size(int(p))
            return (int(p) // tick) * tick
        except Exception:
            return None

    # ==================== 매도 ====================
    def _execute_sell(self, code, current_price, signal, position):
        """
        ✅ 일반 매도 실행 (스탑로스는 _execute_stoploss에서 처리)

        이 메서드는 수동 매도 또는 기타 매도 신호에 사용됨
        주요 매도 주문(익절1~3, 20일선)은 _ensure_sell_orders_placed에서 선제적으로 처리
        """
        try:
            target_name = signal.get("target_name", "")
            sell_ratio = signal.get("sell_ratio", 0)
            reason = signal.get("reason", "")
            target_price = signal.get("target_price", 0)
            explicit_quantity = signal.get("sell_quantity")

            total_quantity = position.get("quantity", 0)
            if total_quantity <= 0:
                return

            # 스탑로스는 _execute_stoploss에서 처리
            if target_name == "스탑로스":
                self._execute_stoploss(code, current_price, position)
                return

            # 일반 매도 로직
            sell_quantity = self.signal.calculate_sell_quantity(total_quantity, sell_ratio, explicit_quantity)
            if sell_quantity <= 0:
                return

            stock_name = position.get("name", code)
            self.log(f"[{code} {stock_name}] 매도 신호 발생: {reason}")

            # 주문 전송
            result = self._send_sell_with_retry(code, sell_quantity, target_price)

            if result == 0:
                price_str = f"{target_price:,}원 (지정가)" if target_price > 0 else "시장가"
                self.log(f"[{code}] {target_name} 매도 주문 전송 성공: {sell_quantity}주 @ {price_str}", "SUCCESS")

                # 미체결 매도 주문 저장
                self.config.save_pending_order(code, {
                    "order_type": "sell",
                    "quantity": sell_quantity,
                    "price": target_price,
                    "target_name": target_name,
                    "sell_ratio": sell_ratio,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })

                # 상태 업데이트
                sold_targets = position.get("sold_targets", [])
                if target_name and target_name not in sold_targets:
                    sold_targets.append(target_name)

                position["sell_occurred"] = True
                position["sold_targets"] = sold_targets
                position["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # ✅ 매도 발생 시 미체결 매수주문 취소
                self._cancel_pending_buy_orders(code)

                self.config.update_position(code, position)
            else:
                self.log(f"[{code}] 매도 주문 실패: 에러코드 {result}", "ERROR")

        except Exception as e:
            self.log(f"[{code}] 매도 실행 중 오류: {e}", "ERROR")

    def _send_sell_with_retry(self, code, quantity, price):
        """매도 주문 재시도 공통"""
        retry_count = self.config.get("error_handling", "order_retry_count") or 3
        retry_interval = (self.config.get("error_handling", "order_retry_interval_ms") or 1000) / 1000.0

        result = -1
        for attempt in range(retry_count):
            result = self.kiwoom.sell_stock(self.account, code, quantity, price)
            if result == 0:
                return 0
            if attempt < retry_count - 1:
                self.log(f"[{code}] 매도 주문 재시도 {attempt + 2}/{retry_count}", "WARNING")
                time.sleep(retry_interval)

        return result

    # ==================== 미체결 매수 취소 ====================
    def _cancel_pending_buy_orders(self, code):
        """
        조건 2-3: 특정 종목의 미체결 매수 주문 취소
        매도가 발생하면 해당 종목의 미체결 매수 주문을 모두 취소
        """
        if not self.kiwoom or not self.account:
            return
        try:
            cancelled = self.kiwoom.cancel_buy_orders_for_stock(self.account, code)
            if cancelled > 0:
                self.log(f"[{code}] 매도 발생으로 미체결 매수주문 {cancelled}건 취소", "INFO")

            # 내부 추적에서도 제거 (메모리)
            if code in self.pending_buy_orders:
                del self.pending_buy_orders[code]

            # 저장된 미체결 주문에서도 제거 (파일)
            self.config.clear_pending_orders_for_stock(code, order_type="buy")

        except Exception as e:
            self.log(f"[{code}] 미체결 매수주문 취소 중 오류: {e}", "ERROR")

    # ==================== 체결 콜백 ====================
    def _on_order_executed(self, data):
        # 주문/체결 이벤트가 오면 pending 중복 방지 키를 해제
        """
        ✅ 주문 체결 콜백

        요구사항 반영:
        - 매수 체결 시 initial_quantity 설정 (전체물량 기준 비중 계산용)
        - 매수 체결 시 즉시 모든 매도 주문 설정
        - 매도 체결 시 sold_targets 업데이트
        """
        try:
            if data["type"] == "order":
                code = data["code"]
                executed_qty = data["executed_quantity"]
                executed_price = data["executed_price"]
                order_type = data["order_type"]

                if executed_qty > 0:
                    is_buy = ("+" in order_type) or ("매수" in order_type)
                    type_str = "매수" if is_buy else "매도"
                    self.log(f"[체결] {code} {type_str} 체결: {executed_qty}주 @ {executed_price:,}원")

                    exec_type = "buy" if is_buy else "sell"
                    self.config.save_execution(code, {
                        "type": exec_type,
                        "quantity": executed_qty,
                        "price": executed_price,
                        "time": datetime.now().strftime("%H%M%S"),
                        "order_no": str(data.get("order_no", ""))
                    })

                    if is_buy:
                        position = self.config.get_position(code)
                        if position:
                            old_executed_price = position.get("last_executed_price", 0)
                            old_executed_qty = position.get("last_executed_qty", 0)

                            if old_executed_qty > 0 and old_executed_price > 0:
                                total_qty = old_executed_qty + executed_qty
                                new_executed_price = ((old_executed_price * old_executed_qty) + (executed_price * executed_qty)) / total_qty
                                position["last_executed_price"] = int(new_executed_price)
                                position["last_executed_qty"] = total_qty
                            else:
                                position["last_executed_price"] = executed_price
                                position["last_executed_qty"] = executed_qty

                            # ✅ 체결된 매수 주문의 buy_count 찾아서 포지션 업데이트
                            pending_orders = self.config.get_pending_orders().get(code, [])
                            for order in pending_orders:
                                if order.get("order_type") == "buy" and int(order.get("price", 0)) == int(executed_price):
                                    order_buy_count = order.get("buy_count", 1)
                                    current_buy_count = position.get("buy_count", 0)
                                    if order_buy_count > current_buy_count:
                                        position["buy_count"] = order_buy_count
                                        self.log(f"[{code}] {order_buy_count}차 매수 체결 확인", "INFO")
                                    break

                            self.config.update_position(code, position)

                        self.config.remove_pending_order(code, order_type="buy", price=executed_price)
                        self._check_and_cancel_excess_orders()

                    else:
                        # ✅ 매도 체결 시 sold_targets 업데이트
                        position = self.config.get_position(code)
                        target_name = ""
                        is_auto_sell = False

                        if position:
                            # 체결된 매도 주문의 target_name 찾기 (자동매도 여부 판단)
                            pending_orders = self.config.get_pending_orders().get(code, [])
                            for order in pending_orders:
                                if order.get("order_type") == "sell" and int(order.get("price", 0)) == int(executed_price):
                                    target_name = order.get("target_name", "")
                                    if target_name:
                                        is_auto_sell = True  # ✅ 자동매도
                                        sold_targets = position.get("sold_targets", [])
                                        if target_name not in sold_targets:
                                            sold_targets.append(target_name)
                                            position["sold_targets"] = sold_targets
                                            position["sell_occurred"] = True
                                            self.config.update_position(code, position)
                                            self.log(f"[{code}] {target_name} 매도 체결 완료 (자동)", "SUCCESS")

                                            # ✅ 매도 발생 시 미체결 매수 주문 취소
                                            self._cancel_pending_buy_orders(code)
                                    break

                            # placed_sell_orders에서 해당 타겟 제거
                            if code in self.placed_sell_orders and target_name in self.placed_sell_orders[code]:
                                del self.placed_sell_orders[code][target_name]

                        # ✅ 자동매도 플래그 설정 (balance에서 재계산 여부 판단용)
                        if is_auto_sell:
                            self._auto_sell_executed[code] = True
                        else:
                            self.log(f"[{code}] 수동 매도 체결 감지 @ {executed_price:,}원", "INFO")

                        self.config.remove_pending_order(code, order_type="sell", price=executed_price)

            elif data["type"] == "balance":
                code = data["code"]
                quantity = data["quantity"]
                avg_price = data["avg_price"]

                position = self.config.get_position(code)
                if position:
                    old_quantity = position.get("quantity", 0)
                    position["quantity"] = quantity
                    position["avg_price"] = avg_price
                    position["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                    # ✅ 매수로 인한 수량 증가 시
                    if quantity > old_quantity:
                        is_additional_buy = old_quantity > 0  # 추가 매수 여부

                        if is_additional_buy:
                            # ✅ 추가 매수: 기존 매도 주문 취소 후 새 수량 기준으로 재발주
                            self.log(f"[{code}] 추가 매수 체결 - 매도 주문 재계산 ({old_quantity}주 → {quantity}주)", "INFO")

                            # 기존 매도 주문 취소
                            try:
                                cancelled = self.kiwoom.cancel_sell_orders_for_stock(self.account, code)
                                if cancelled > 0:
                                    self.log(f"[{code}] 기존 매도 주문 {cancelled}건 취소", "INFO")
                            except Exception as e:
                                self.log(f"[{code}] 기존 매도 주문 취소 오류: {e}", "ERROR")

                            # placed_sell_orders 초기화 (체결 완료된 것만 유지)
                            sold_targets = position.get("sold_targets", [])
                            if code in self.placed_sell_orders:
                                self.placed_sell_orders[code] = {k: v for k, v in self.placed_sell_orders[code].items() if k in sold_targets}

                            # pending_orders에서 매도 주문 정리
                            self.config.clear_pending_orders_for_stock(code, order_type="sell")

                        # initial_quantity 설정/갱신 (새로운 총 수량으로)
                        position["initial_quantity"] = quantity
                        log_type = "초기" if not is_additional_buy else "갱신"
                        self.log(f"[{code}] {log_type} 수량 설정: {quantity}주", "INFO")

                        # ✅ 매수 체결 후 매도 주문 설정 (새 수량 기준)
                        self._schedule_sell_orders_after_buy(code, position)

                    # ✅ 매도로 인한 수량 감소 시
                    elif quantity < old_quantity:
                        position["sell_occurred"] = True
                        self.config.update_position(code, position)

                        # ✅ 자동매도인지 수동매도인지 확인
                        is_auto_sell = self._auto_sell_executed.pop(code, False)

                        if is_auto_sell:
                            # 자동매도: 초기 비중대로 유지, 재계산 안 함
                            self.log(f"[{code}] 자동매도 체결 - 기존 매도 주문 유지 (남은 {quantity}주)", "INFO")
                        else:
                            # 수동매도: 남은 수량 기준으로 재계산/재발주
                            if quantity > 0:
                                self.log(f"[{code}] 수동매도 체결 - 자동매도 주문 재계산 필요 (남은 {quantity}주)", "INFO")
                                self._recalculate_sell_orders_on_quantity_decrease(code, position)
                            # 남은 수량 0이면 아래 if quantity == 0에서 정리

                    self.config.update_position(code, position)

                    # 전량 매도 완료 시 정리
                    if quantity == 0:
                        position = self.config.get_position(code) or {}
                        # pending 주문 삭제
                        self.config.clear_pending_orders_for_stock(code, order_type="sell")
                        # placed_sell_orders 정리
                        if code in self.placed_sell_orders:
                            del self.placed_sell_orders[code]
                        # stoploss_price는 0으로 (매도 완료)
                        position["stoploss_price"] = 0
                        self.config.update_position(code, position)
                        self.log(f"[{code}] 전량 매도 완료 - 자동매도 재계산 스킵", "SUCCESS")

                self._check_and_cancel_excess_orders()

        except Exception as e:
            self.log(f"체결 처리 중 오류: {e}", "ERROR")

    def _schedule_sell_orders_after_buy(self, code, position):
        """
        ✅ 매수 체결 후 모든 매도 주문 설정

        요구사항: 매수 체결 시점에 바로 모든 매도 주문이 걸려있어야 함
        """
        try:
            if not self.is_market_open():
                self.log(f"[{code}] 장 시간 외 - 매도 주문은 장 시작 시 복원됩니다.", "INFO")
                return

            # 스탑로스 발동 종목은 제외
            if position.get("stoploss_triggered", False):
                return

            # 일봉 데이터 가져오기
            candles = None
            if self.event_engine:
                candles = self.event_engine.get_candles(code)
            if not candles:
                candles = self.kiwoom.get_daily_candles(code, 30) if self.kiwoom else None

            if candles:
                self._ensure_sell_orders_placed(code, position, candles)
            else:
                self.log(f"[{code}] 일봉 데이터 없음 - 매도 주문 설정 대기", "WARNING")

        except Exception as e:
            self.log(f"[{code}] 매도 주문 설정 중 오류: {e}", "ERROR")

    def _recalculate_sell_orders_on_quantity_decrease(self, code, position):
        """
        ✅ 수동 매도 등으로 보유 수량이 감소했을 때 자동매도 주문 재계산/재발주

        요구사항:
        - 남은 수량이 0이면 재계산하지 않음 (호출 전 체크됨)
        - 남은 수량이 있으면 기존 매도 주문 취소 후 남은 수량으로 재발주
        """
        try:
            remaining_qty = position.get("quantity", 0)

            # 남은 수량이 없으면 재계산하지 않음
            if remaining_qty <= 0:
                self.log(f"[{code}] 남은 수량 없음 - 자동매도 재계산 스킵", "INFO")
                return

            # 스탑로스 발동 종목은 스탑로스 주문만 유지 (별도 처리)
            if position.get("stoploss_triggered", False):
                self.log(f"[{code}] 스탑로스 발동 종목 - 스탑로스 주문 유지", "INFO")
                return

            # 장 시간 외면 재계산 대기
            if not self.is_market_open():
                self.log(f"[{code}] 장 시간 외 - 자동매도 재계산 대기", "INFO")
                return

            stock_name = position.get("name", code)
            self.log(f"[{code} {stock_name}] 수량 감소 감지 - 자동매도 주문 재계산 시작 (남은 수량: {remaining_qty}주)", "INFO")

            # 1) 해당 종목의 모든 미체결 매도 주문 취소
            try:
                cancelled = self.kiwoom.cancel_sell_orders_for_stock(self.account, code)
                if cancelled > 0:
                    self.log(f"[{code}] 기존 매도 주문 {cancelled}건 취소", "INFO")
            except Exception as e:
                self.log(f"[{code}] 기존 매도 주문 취소 오류: {e}", "ERROR")
                # 취소 실패해도 계속 진행 (새 주문이 중복될 수 있지만 안전)

            # 2) placed_sell_orders 초기화 (이미 체결된 타겟은 sold_targets에 있음)
            sold_targets = position.get("sold_targets", [])
            if code in self.placed_sell_orders:
                # 체결 완료된 타겟만 유지
                self.placed_sell_orders[code] = {k: v for k, v in self.placed_sell_orders[code].items() if k in sold_targets}

            # 3) pending_orders에서 매도 주문 정리
            self.config.clear_pending_orders_for_stock(code, order_type="sell")

            # 4) 일봉 데이터 가져오기
            candles = None
            if self.event_engine:
                candles = self.event_engine.get_candles(code)
            if not candles:
                candles = self.kiwoom.get_daily_candles(code, 30) if self.kiwoom else None

            if not candles:
                self.log(f"[{code}] 일봉 데이터 없음 - 자동매도 재계산 대기", "WARNING")
                return

            # 5) 남은 수량을 initial_quantity로 사용하여 새 매도 주문 계산 및 발주
            # 기존 initial_quantity 백업
            original_initial = position.get("initial_quantity", 0)

            # 남은 수량으로 initial_quantity 임시 설정 (비중 계산용)
            position["initial_quantity"] = remaining_qty

            # 매도 주문 재발주
            self._ensure_sell_orders_placed(code, position, candles)

            # initial_quantity 복원 (전체 물량 기록 유지)
            position["initial_quantity"] = original_initial
            self.config.update_position(code, position)

            self.log(f"[{code}] 자동매도 주문 재계산 완료 (남은 {remaining_qty}주 기준)", "SUCCESS")

        except Exception as e:
            self.log(f"[{code}] 자동매도 재계산 오류: {e}", "ERROR")

    def _check_and_cancel_excess_orders(self):
        """조건 8-2: 최대 보유 종목수 도달시 보유종목 외의 미체결 매수주문 취소"""
        if not self.kiwoom or not self.account:
            return
        try:
            max_holding = self.config.get("buy", "max_holding_stocks") or 3
            positions = self.config.get("positions") or {}

            holding_codes = [code for code, pos in positions.items() if pos.get("quantity", 0) > 0]
            current_holding_count = len(holding_codes)

            if current_holding_count >= max_holding:
                cancelled = self.kiwoom.cancel_buy_orders_except_holdings(self.account, holding_codes)
                if cancelled > 0:
                    self.log(
                        f"[조건 8-2] 최대 보유종목({max_holding}개) 도달 - "
                        f"보유종목 외 미체결 매수주문 {cancelled}건 취소",
                        "WARNING"
                    )

                codes_to_remove = [code for code in self.pending_buy_orders.keys() if code not in holding_codes]
                for code in codes_to_remove:
                    del self.pending_buy_orders[code]

        except Exception as e:
            self.log(f"미체결 주문 정리 중 오류: {e}", "ERROR")

    # ==================== 수동 주문 ====================
    def manual_sell(self, code, quantity, price=0):
        """수동 매도"""
        if not self.kiwoom or not self.kiwoom.is_connected():
            self.log("키움 API에 연결되어 있지 않습니다.", "ERROR")
            return False
        if not self.account:
            self.log("계좌가 설정되지 않았습니다.", "ERROR")
            return False
        # ✅ 장 시간 체크
        if not self.is_market_open():
            self.log("장 시간이 아닙니다. 수동 매도 불가", "ERROR")
            return False
        try:
            stock_name = self.kiwoom.get_master_code_name(code)
            price_str = f"{price:,}원" if price > 0 else "시장가"
            self.log(f"[{code} {stock_name}] 수동 매도 주문: {quantity}주 @ {price_str}")
            result = self.kiwoom.sell_stock(self.account, code, quantity, price)
            if result == 0:
                self.log(f"[{code}] 수동 매도 주문 전송 성공", "SUCCESS")
                return True
            self.log(f"[{code}] 수동 매도 주문 실패: 에러코드 {result}", "ERROR")
            return False
        except Exception as e:
            self.log(f"수동 매도 중 오류: {e}", "ERROR")
            return False

    def manual_buy(self, code, quantity, price=0):
        """수동 매수"""
        if not self.kiwoom or not self.kiwoom.is_connected():
            self.log("키움 API에 연결되어 있지 않습니다.", "ERROR")
            return False
        if not self.account:
            self.log("계좌가 설정되지 않았습니다.", "ERROR")
            return False
        # ✅ 장 시간 체크
        if not self.is_market_open():
            self.log("장 시간이 아닙니다. 수동 매수 불가", "ERROR")
            return False
        try:
            stock_name = self.kiwoom.get_master_code_name(code)
            price_str = f"{price:,}원" if price > 0 else "시장가"
            self.log(f"[{code} {stock_name}] 수동 매수 주문: {quantity}주 @ {price_str}")
            result = self.kiwoom.buy_stock(self.account, code, quantity, price)
            if result == 0:
                self.log(f"[{code}] 수동 매수 주문 전송 성공", "SUCCESS")
                return True
            self.log(f"[{code}] 수동 매수 주문 실패: 에러코드 {result}", "ERROR")
            return False
        except Exception as e:
            self.log(f"수동 매수 중 오류: {e}", "ERROR")
            return False

    # ==================== 분석 ====================
    def get_stock_analysis(self, code):
        """종목 분석 정보 조회"""
        try:
            candles = self.kiwoom.get_daily_candles(code, 30)
            if not candles:
                return None

            stock_info = self.kiwoom.get_stock_info(code)
            current_price = stock_info.get("price", 0)

            envelope = self.ta.get_envelope_levels(candles, 20, 20)
            position = self.config.get_position(code)

            buy_signal = self.signal.check_buy_signal(code, current_price, candles, position)
            sell_signals = []
            position_summary = None

            if position and position.get("quantity", 0) > 0:
                sell_signals = self.signal.check_sell_signals(code, current_price, candles, position)
                position_summary = self.signal.get_position_summary(position, current_price, candles)

            return {
                "stock_info": stock_info,
                "envelope": envelope,
                "buy_signal": buy_signal,
                "sell_signals": sell_signals,
                "position": position,
                "position_summary": position_summary
            }

        except Exception as e:
            self.log(f"[{code}] 분석 중 오류: {e}", "ERROR")
            return None

    # ==================== 상태 동기화 ====================
    def sync_positions_from_account(self, balance=None):
        """
        ✅ 계좌 잔고에서 포지션 동기화

        요구사항 반영:
        - initial_quantity 설정 (전체물량 기준 비중 계산용)
        - 기존 상태(stoploss_triggered, sold_targets 등) 유지

        Args:
            balance: 이미 조회된 잔고 데이터 (None이면 직접 조회)
        """
        if not self.kiwoom or not self.account:
            return
        try:
            if balance is None:
                balance = self.kiwoom.get_balance(self.account)
            holdings = balance.get("holdings", [])

            for holding in holdings:
                code = holding["code"]
                existing = self.config.get_position(code)

                if existing:
                    existing["quantity"] = holding["quantity"]
                    existing["avg_price"] = holding["avg_price"]

                    # ✅ initial_quantity가 없거나 0이면 현재 수량으로 설정
                    if existing.get("initial_quantity", 0) == 0:
                        existing["initial_quantity"] = holding["quantity"]

                    self.config.update_position(code, existing)
                else:
                    new_position = {
                        "code": code,
                        "name": holding["name"],
                        "quantity": holding["quantity"],
                        "avg_price": holding["avg_price"],
                        "buy_count": 1,
                        "last_buy_price": holding["avg_price"],
                        "sold_targets": [],
                        "sell_occurred": False,
                        # ✅ 초기 수량 설정
                        "initial_quantity": holding["quantity"],
                        # 스탑로스 유지 상태 기본값
                        "stoploss_triggered": False,
                        "stoploss_price": 0,
                        "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    self.config.update_position(code, new_position)

            self.log(f"포지션 동기화 완료: {len(holdings)}개 종목")

        except Exception as e:
            self.log(f"포지션 동기화 오류: {e}", "ERROR")

    def sync_state_from_executions(self):
        """프로그램 재시작 시 당일 체결내역 기반 상태 동기화"""
        if not self.kiwoom or not self.account:
            self.log("상태 동기화 불가: API 또는 계좌 미설정", "WARNING")
            return

        if self.config.is_state_synced():
            self.log("상태 동기화 이미 완료됨", "INFO")
            return

        self.log("체결내역 기반 상태 동기화 시작...")
        try:
            self.sync_positions_from_account()

            today_executions = self.kiwoom.get_today_executions(self.account)
            self.log(f"당일 체결내역: {len(today_executions)}건")

            today_str = datetime.now().strftime("%Y%m%d")
            executions_by_code = {}

            for execution in today_executions:
                code = execution["code"]
                exec_type = "buy" if "매수" in execution["order_type"] else "sell"
                quantity = execution["quantity"]
                price = execution["price"]
                order_no = execution["order_no"]

                self.config.save_execution(code, {
                    "type": exec_type,
                    "quantity": quantity,
                    "price": price,
                    "time": execution["time"],
                    "order_no": order_no
                }, today_str)

                if code not in executions_by_code:
                    executions_by_code[code] = {"buys": [], "sells": []}

                executions_by_code[code]["buys" if exec_type == "buy" else "sells"].append({
                    "quantity": quantity,
                    "price": price,
                    "time": execution["time"]
                })

            for code, exec_data in executions_by_code.items():
                position = self.config.get_position(code)
                if not position:
                    continue

                avg_price = position.get("avg_price", 0)
                buys = exec_data["buys"]
                sells = exec_data["sells"]

                if buys:
                    old_buy_count = position.get("buy_count", 0)
                    new_buy_count = max(old_buy_count, len(buys))
                    if new_buy_count > old_buy_count:
                        position["buy_count"] = min(new_buy_count, 3)
                        self.log(f"[{code}] 매수 체결 확인 - buy_count: {position['buy_count']}", "INFO")

                if sells and avg_price > 0:
                    position["sell_occurred"] = True

                    sold_targets = position.get("sold_targets", [])
                    profit_targets = self.config.get("sell", "profit_targets") or [2.95, 4.95, 6.95]

                    for sell in sells:
                        sell_price = sell["price"]
                        profit_rate = ((sell_price - avg_price) / avg_price) * 100 if avg_price > 0 else 0

                        for i, target in enumerate(profit_targets):
                            target_name = f"목표{i+1}({target}%)"
                            if target_name not in sold_targets and abs(profit_rate - target) <= 0.5:
                                sold_targets.append(target_name)
                                self.log(f"[{code}] 매도 목표 확인: {target_name} (수익률: {profit_rate:.2f}%)", "INFO")
                                break

                        if "20일선" not in sold_targets and 0 < profit_rate < profit_targets[0]:
                            sold_targets.append("20일선")
                            self.log(f"[{code}] 20일선 매도 확인 (수익률: {profit_rate:.2f}%)", "INFO")

                        if "손절" not in sold_targets and profit_rate < 0:
                            sold_targets.append("손절")
                            self.log(f"[{code}] 손절 매도 확인 (수익률: {profit_rate:.2f}%)", "INFO")

                    position["sold_targets"] = sold_targets
                    self.config.clear_pending_orders_for_stock(code, order_type="buy")
                    self.config.update_position(code, position)

            self._cleanup_stale_positions()
            self.config.clear_old_execution_history(keep_days=7)
            self.config.set_state_synced(True)
            self.log("체결내역 기반 상태 동기화 완료")

        except Exception as e:
            self.log(f"상태 동기화 오류: {e}", "ERROR")

    def _cleanup_stale_positions(self):
        """실제 잔고에 없는 오래된 포지션 정리"""
        if not self.kiwoom or not self.account:
            return
        try:
            balance = self.kiwoom.get_balance(self.account)
            actual_holdings = {h["code"] for h in balance.get("holdings", [])}

            positions = self.config.get("positions") or {}
            for code in list(positions.keys()):
                position = positions[code]
                saved_quantity = position.get("quantity", 0)

                if code not in actual_holdings:
                    if saved_quantity > 0:
                        position["quantity"] = 0
                        position["sell_occurred"] = True
                        self.config.update_position(code, position)
                        self.log(f"[{code}] 포지션 정리: 실제 보유 없음 (전량 매도 추정)", "INFO")

        except Exception as e:
            self.log(f"포지션 정리 오류: {e}", "ERROR")

    def full_state_sync_on_startup(self):
        """프로그램 시작 시 전체 상태 동기화 + 스탑로스 유지 주문 보정"""
        self.log("프로그램 시작 - 전체 상태 동기화 시작...")

        today_str = datetime.now().strftime("%Y%m%d")
        last_date = self.config.get_last_trading_date()

        if last_date != today_str:
            self.config.reset_session_for_new_day(today_str)
            self.log(f"새로운 거래일: {today_str}")

        self.sync_state_from_executions()
        self.clear_stale_pending_orders()

        # 장이 열려 있다면(또는 장 시작 시점) 스탑로스 유지 주문을 보정
        if self.is_market_open():
            self.ensure_all_stoploss_orders()

        self.log("전체 상태 동기화 완료")

    # ==================== 스탑로스 유지 로직 ====================
    def ensure_all_stoploss_orders(self):
        """
        스탑로스가 한번이라도 발동된 종목 중,
        보유수량이 남아있으면 스탑로스 지정가 매도 주문이 반드시 존재하도록 보정.
        (장종료로 주문이 사라지는 케이스 대비: 장 시작/프로그램 실행 중 재주문)
        """
        if not self.kiwoom or not self.account:
            return

        positions = self.config.get("positions") or {}
        for code, pos in positions.items():
            self._ensure_stoploss_order_if_needed(code, pos)

    def _ensure_stoploss_order_if_needed(self, code, position):
        """
        개별 종목 스탑로스 유지 보정:
        - stoploss_triggered=True
        - quantity>0
        - stoploss_price>0
        - 현재 API 미체결 주문에 "동일 가격의 스탑로스(전량) 매도 주문"이 없으면 재주문
        """
        try:
            if not position:
                return
            if not position.get("stoploss_triggered", False):
                return

            qty = position.get("quantity", 0)
            price = position.get("stoploss_price", 0)
            if qty <= 0 or price <= 0:
                return

            # 장 시간 외면 재주문하지 않음 (장 시작 시 ensure_all_stoploss_orders에서 걸림)
            if not self.is_market_open():
                return

            # API 미체결에 이미 스탑로스 주문이 있는지 확인
            api_pending_orders = self.kiwoom.get_open_orders(self.account)
            for o in api_pending_orders:
                o_code = o.get("code")
                o_type_raw = o.get("order_type", "")
                o_price = o.get("order_price", 0)
                o_qty = o.get("quantity", 0)

                is_sell = ("매도" in o_type_raw) or ("-" in o_type_raw)
                if o_code == code and is_sell and int(o_price) == int(price):
                    # 수량이 일부만 남아서 주문 수량이 다를 수도 있으니,
                    # 같은 가격의 매도 미체결이 있으면 "존재"로 판단
                    return

            # 없으면 재주문: 다른 주문이 남아있다면 혼선 방지 위해 매도/매수 모두 취소 후 스탑로스만 재설정
            self.log(f"[{code}] 스탑로스 유지 주문 없음 -> 재주문 (잔여 {qty}주 @ {price:,}원)", "WARNING")
            try:
                self.kiwoom.cancel_all_orders_for_stock(self.account, code)
            except Exception:
                pass

            # 매수 미체결은 항상 취소(요구사항)
            self._cancel_pending_buy_orders(code)

            # 저장된 pending sell도 최신 잔여수량/가격으로 갱신
            self.config.save_pending_order(code, {
                "order_type": "sell",
                "quantity": qty,
                "price": price,
                "target_name": "스탑로스",
                "sell_ratio": 100,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "persist": True
            })

            result = self._send_sell_with_retry(code, qty, price)
            if result == 0:
                self.log(f"[{code}] 스탑로스 유지 재주문 성공: {qty}주 @ {price:,}원", "SUCCESS")
            else:
                self.log(f"[{code}] 스탑로스 유지 재주문 실패: 에러코드 {result}", "ERROR")

        except Exception as e:
            self.log(f"[{code}] 스탑로스 유지 보정 오류: {e}", "ERROR")

    # ==================== 주문 복원 ====================
    def check_and_restore_orders(self):
        """
        프로그램 시작/장 시작 시 저장된 미체결 주문 복원

        복원 조건:
        1. 장이 열려있을 때
        2. 아직 오늘 복원하지 않았을 때
        3. 저장된 미체결 주문이 있을 때
        4. (매수 주문) 매도 발생 종목은 복원하지 않음
        5. API 미체결 주문과 중복되지 않을 때

        + 추가(요구사항):
        - 스탑로스 발동 이력이 있고 보유수량이 남아있으면, 스탑로스 주문은 반드시 복원/유지
        """
        if not self.kiwoom or not self.account:
            self.log("주문 복원 불가: API 또는 계좌 미설정", "WARNING")
            return

        if not self.is_market_open():
            self.log("주문 복원 대기: 장 시간 외", "INFO")
            return

        today_str = datetime.now().strftime("%Y%m%d")
        last_date = self.config.get_last_trading_date()

        if last_date != today_str:
            self.config.reset_session_for_new_day(today_str)
            self.orders_restored = False
            self.log(f"새로운 거래일 시작: {today_str}")

        if self.config.is_orders_restored():
            # 그래도 스탑로스 유지 주문은 다시 점검
            self.ensure_all_stoploss_orders()
            self.log("주문 복원 이미 완료됨", "INFO")
            return

        pending_orders = self.config.get_pending_orders()
        if not pending_orders:
            self.log("복원할 미체결 주문 없음", "INFO")
            # 스탑로스 유지 주문 점검
            self.ensure_all_stoploss_orders()
            self.config.set_orders_restored(True)
            return

        self.log(f"미체결 주문 복원 시작: {len(pending_orders)}개 종목")

        api_pending_orders = self.kiwoom.get_open_orders(self.account)
        api_pending_set = set()
        for order in api_pending_orders:
            api_order_type = order.get("order_type", "")
            order_type = "buy" if ("매수" in api_order_type or "+" in api_order_type) else "sell"
            order_price = order.get("order_price", 0)
            api_pending_set.add((order["code"], order_type, int(order_price)))

        restored_count = 0
        skipped_count = 0
        duplicate_count = 0

        for code, orders in pending_orders.items():
            position = self.config.get_position(code)

            for order in orders:
                order_type = order.get("order_type")
                quantity = int(order.get("quantity", 0))
                price = int(order.get("price", 0))
                buy_count = order.get("buy_count")
                target_name = order.get("target_name")

                if (code, order_type, price) in api_pending_set:
                    self.log(f"[{code}] 주문 복원 스킵 (이미 미체결 주문 존재)", "INFO")
                    duplicate_count += 1
                    self.config.remove_pending_order(code, order_type=order_type, price=price)
                    continue

                if order_type == "buy":
                    # 매도 발생 종목은 매수 복원하지 않음 (기존)
                    if position and position.get("sell_occurred", False):
                        self.log(f"[{code}] 매수 주문 복원 스킵 (매도 발생)", "INFO")
                        skipped_count += 1
                        self.config.remove_pending_order(code, order_type="buy", price=price)
                        continue

                    if not self._can_buy_new_stock() and (not position or position.get("quantity", 0) == 0):
                        self.log(f"[{code}] 매수 주문 복원 스킵 (최대 보유 종목 도달)", "INFO")
                        skipped_count += 1
                        self.config.remove_pending_order(code, order_type="buy", price=price)
                        continue

                    result = self.kiwoom.buy_stock(self.account, code, quantity, price)
                    if result == 0:
                        self.log(f"[{code}] {buy_count}차 매수 주문 복원: {quantity}주 @ {price:,}원", "SUCCESS")
                        restored_count += 1
                    else:
                        self.log(f"[{code}] 매수 주문 복원 실패: 에러코드 {result}", "ERROR")

                elif order_type == "sell":
                    # 보유 수량 없으면 매도 복원 X
                    if not position or position.get("quantity", 0) <= 0:
                        self.log(f"[{code}] 매도 주문 복원 스킵 (보유 수량 없음)", "INFO")
                        skipped_count += 1
                        self.config.remove_pending_order(code, order_type="sell", price=price)
                        continue

                    holding_qty = int(position.get("quantity", 0))
                    if quantity > holding_qty:
                        self.log(f"[{code}] 매도 수량 조정: {quantity} -> {holding_qty}", "INFO")
                        quantity = holding_qty

                    result = self.kiwoom.sell_stock(self.account, code, quantity, price)
                    if result == 0:
                        self.log(f"[{code}] {target_name} 매도 주문 복원: {quantity}주 @ {price:,}원", "SUCCESS")
                        restored_count += 1
                    else:
                        self.log(f"[{code}] 매도 주문 복원 실패: 에러코드 {result}", "ERROR")

        # 복원 후 스탑로스 유지 주문 강제 점검
        self.ensure_all_stoploss_orders()

        # ✅ 보유 종목에 대해 모든 매도 주문이 걸려있는지 확인/보정
        self._restore_all_sell_orders_for_holdings()

        self.log(f"주문 복원 완료: 성공 {restored_count}건, 스킵 {skipped_count}건, 중복 {duplicate_count}건")
        self.config.set_orders_restored(True)
        self.orders_restored = True

    def _restore_all_sell_orders_for_holdings(self):
        """
        ✅ 장 시작 시 모든 보유 종목에 대해 매도 주문이 걸려있는지 확인/보정

        요구사항: 매도 주문은 항상 걸려있어야 함 (익절1~3 + 20일선)
        """
        if not self.kiwoom or not self.account:
            return

        if not self.is_market_open():
            return

        positions = self.config.get("positions") or {}

        for code, position in positions.items():
            if position.get("quantity", 0) <= 0:
                continue

            # 스탑로스 발동 종목은 스탑로스 주문만 유지
            if position.get("stoploss_triggered", False):
                continue

            # 일봉 데이터 가져오기
            candles = None
            if self.event_engine:
                candles = self.event_engine.get_candles(code)
            if not candles:
                candles = self.kiwoom.get_daily_candles(code, 30) if self.kiwoom else None

            if candles:
                self._ensure_sell_orders_placed(code, position, candles)
            else:
                self.log(f"[{code}] 일봉 데이터 없음 - 매도 주문 복원 대기", "WARNING")

    # ==================== 저장/정리 ====================
    def save_current_state(self):
        """현재 상태 저장 (프로그램 종료 시 호출)"""
        try:
            self.config.save_config()
            today_str = datetime.now().strftime("%Y%m%d")
            self.config.update_session_info(
                last_trading_date=today_str,
                auto_trading_enabled=self.is_running
            )
            self.log("현재 상태 저장 완료")
        except Exception as e:
            self.log(f"상태 저장 오류: {e}", "ERROR")

    def clear_stale_pending_orders(self):
        """
        오래된 미체결 주문 정리
        - 이미 체결된 주문
        - 매도 발생한 종목의 매수 주문
        - 전량 매도 완료된 종목의 주문

        + 주의(요구사항):
        - 스탑로스 유지(persist) 목적의 매도 주문은 보유수량이 남아있으면 삭제하지 않음
        """
        pending_orders = self.config.get_pending_orders()
        cleaned_count = 0

        for code in list(pending_orders.keys()):
            position = self.config.get_position(code)
            orders = pending_orders[code]

            qty = position.get("quantity", 0) if position else 0
            stoploss_triggered = position.get("stoploss_triggered", False) if position else False

            # 전량 매도 완료면 모두 삭제
            if not position or qty == 0:
                self.config.clear_pending_orders_for_stock(code)
                cleaned_count += 1
                continue

            # 매도 발생한 종목의 매수 주문 삭제 (스탑로스든 일반이든 매도 발생이면 매수 미체결은 정리)
            if position and position.get("sell_occurred", False):
                buy_orders = [o for o in orders if o.get("order_type") == "buy"]
                if buy_orders:
                    self.config.clear_pending_orders_for_stock(code, order_type="buy")
                    cleaned_count += len(buy_orders)

            # 스탑로스 유지 종목이면, 매도 미체결은 남겨둬야 함(익일 재주문용)
            # (다른 일반 매도 주문은 정책에 따라 정리해도 되지만, 여기서는 보수적으로 유지)
            if stoploss_triggered:
                pass

        if cleaned_count > 0:
            self.log(f"오래된 미체결 주문 {cleaned_count}건 정리 완료")

    # ==================== 기타 ====================
    def check_and_trade(self, code):
        """타이머 기반 매매 호출용"""
        if not self.is_running:
            return
        try:
            if self.event_engine:
                candles = self.event_engine.get_candles(code)
            else:
                candles = self.kiwoom.get_daily_candles(code, 30)

            if not candles:
                self.log(f"[{code}] 일봉 데이터를 가져올 수 없습니다.", "WARNING")
                return

            current_price = candles[0]["close"] if candles else 0
            if current_price == 0:
                current_price = self.kiwoom.get_stock_price(code)

            self._check_trading_conditions(code, current_price, candles)

        except Exception as e:
            self.log(f"[{code}] 매매 확인 중 오류: {e}", "ERROR")

    def get_pending_orders_summary(self):
        """저장된 미체결 주문 요약 조회"""
        pending_orders = self.config.get_pending_orders()
        summary = {
            "total_stocks": len(pending_orders),
            "buy_orders": 0,
            "sell_orders": 0,
            "details": []
        }

        for code, orders in pending_orders.items():
            stock_name = ""
            if self.kiwoom:
                stock_name = self.kiwoom.get_master_code_name(code)

            for order in orders:
                order_type = order.get("order_type")
                if order_type == "buy":
                    summary["buy_orders"] += 1
                else:
                    summary["sell_orders"] += 1

                summary["details"].append({
                    "code": code,
                    "name": stock_name,
                    "type": order_type,
                    "quantity": order.get("quantity"),
                    "price": order.get("price"),
                    "target_name": order.get("target_name"),
                    "created_at": order.get("created_at")
                })

        return summary