# -*- coding: utf-8 -*-
"""
이벤트 처리 엔진 - 큐 기반 처리, 디바운스, 배치 갱신 관리
"""
import time
import threading
from queue import Queue, Empty
from collections import defaultdict
from datetime import datetime
from PyQt5.QtCore import QTimer, QObject, pyqtSignal


class Debouncer:
    """디바운스 처리 클래스 - 동일 종목의 연속 이벤트 병합"""

    def __init__(self, delay_ms=200):
        """
        Args:
            delay_ms: 디바운스 지연 시간 (밀리초)
        """
        self.delay = delay_ms / 1000.0  # 초 단위로 변환
        self.last_events = {}  # {code: (timestamp, data)}
        self.lock = threading.Lock()

    def should_process(self, code, data):
        """
        이벤트 처리 여부 결정

        Args:
            code: 종목 코드
            data: 이벤트 데이터

        Returns:
            bool: 처리해야 하면 True
        """
        with self.lock:
            now = time.time()
            last = self.last_events.get(code)

            if last is None or (now - last[0]) >= self.delay:
                self.last_events[code] = (now, data)
                return True

            # 마지막 이벤트 데이터 업데이트 (나중에 처리될 때 최신 데이터 사용)
            self.last_events[code] = (last[0], data)
            return False

    def get_pending(self, code):
        """대기 중인 이벤트 데이터 가져오기"""
        with self.lock:
            if code in self.last_events:
                return self.last_events[code][1]
            return None


class BatchScheduler:
    """배치 스케줄러 - 봉데이터/MA20 갱신용"""

    def __init__(self, batch_size=10, interval_seconds=60):
        """
        Args:
            batch_size: 한 번에 처리할 종목 수
            interval_seconds: 전체 종목 갱신 주기 (초)
        """
        self.batch_size = batch_size
        self.interval = interval_seconds
        self.stocks = []  # 관리할 종목 리스트
        self.current_index = 0
        self.last_full_cycle = 0
        self.lock = threading.Lock()

        # 캐시된 봉데이터 {code: {"candles": [...], "updated_at": timestamp}}
        self.candle_cache = {}
        self.cache_ttl = 60  # 캐시 유효 시간 (초)

    def set_stocks(self, stock_codes):
        """관리할 종목 설정"""
        with self.lock:
            self.stocks = list(stock_codes)
            self.current_index = 0

    def get_next_batch(self):
        """다음 배치 종목 가져오기"""
        with self.lock:
            if not self.stocks:
                return []

            start = self.current_index
            end = min(start + self.batch_size, len(self.stocks))
            batch = self.stocks[start:end]

            self.current_index = end
            if self.current_index >= len(self.stocks):
                self.current_index = 0
                self.last_full_cycle = time.time()

            return batch

    def get_cached_candles(self, code):
        """캐시된 봉데이터 가져오기 (유효하면)"""
        with self.lock:
            cached = self.candle_cache.get(code)
            if cached:
                if time.time() - cached["updated_at"] < self.cache_ttl:
                    return cached["candles"]
            return None

    def update_cache(self, code, candles):
        """봉데이터 캐시 업데이트"""
        with self.lock:
            self.candle_cache[code] = {
                "candles": candles,
                "updated_at": time.time()
            }

    def is_cache_valid(self, code):
        """캐시가 유효한지 확인"""
        with self.lock:
            cached = self.candle_cache.get(code)
            if cached:
                return time.time() - cached["updated_at"] < self.cache_ttl
            return False


class RealTimeManager:
    """
    실시간 데이터 관리 - 등록 제한 및 우선순위 관리

    키움 OpenAPI+ 실시간 등록 규칙:
    - 한 화면번호당 최대 100종목
    - 200종목 등록하려면 화면번호 2개 사용 (예: "1000", "1001")
    """

    MAX_STOCKS_PER_SCREEN = 100  # 화면당 최대 종목 수
    MAX_SCREENS = 2  # 사용할 화면 수
    MAX_REALTIME_STOCKS = MAX_STOCKS_PER_SCREEN * MAX_SCREENS  # 총 200종목

    # 화면번호 (실시간용)
    SCREEN_NUMBERS = ["1000", "1001"]

    def __init__(self):
        self.registered_stocks = set()  # 현재 실시간 등록된 종목
        self.priority_stocks = set()  # 우선순위 종목 (보유 종목)
        self.lock = threading.Lock()

        # 화면별 등록 종목 관리
        self.screen_stocks = {screen: set() for screen in self.SCREEN_NUMBERS}

    def set_priority_stocks(self, codes):
        """우선순위 종목 설정 (보유 종목은 항상 실시간)"""
        with self.lock:
            self.priority_stocks = set(codes)

    def calculate_registrations(self, watchlist_codes):
        """
        실시간 등록할 종목 계산 (화면번호별로 분배)

        Args:
            watchlist_codes: 감시 종목 리스트

        Returns:
            dict: {
                "screen_registrations": {화면번호: [등록할 종목들]},
                "to_unregister": {화면번호: [해제할 종목들]}
            }
        """
        with self.lock:
            all_candidates = list(watchlist_codes)

            # 우선순위 종목 (보유 종목)은 반드시 포함, 앞쪽에 배치
            priority_list = [c for c in all_candidates if c in self.priority_stocks]
            other_list = [c for c in all_candidates if c not in self.priority_stocks]

            # 우선순위 종목 + 일반 종목 순서로 정렬
            sorted_candidates = priority_list + other_list

            # 최대 등록 가능 수만큼 자르기
            to_register_all = sorted_candidates[:self.MAX_REALTIME_STOCKS]

            # 화면번호별로 분배 (각 화면에 100개씩)
            screen_registrations = {}
            for i, screen_no in enumerate(self.SCREEN_NUMBERS):
                start_idx = i * self.MAX_STOCKS_PER_SCREEN
                end_idx = start_idx + self.MAX_STOCKS_PER_SCREEN
                screen_stocks = to_register_all[start_idx:end_idx]
                screen_registrations[screen_no] = screen_stocks

            # 해제할 종목 계산 (화면별)
            new_registered = set(to_register_all)
            to_unregister = {}

            for screen_no in self.SCREEN_NUMBERS:
                old_stocks = self.screen_stocks.get(screen_no, set())
                new_stocks = set(screen_registrations.get(screen_no, []))

                # 기존에 있었지만 새로 등록 안 하는 종목
                unregister_list = list(old_stocks - new_stocks)
                to_unregister[screen_no] = unregister_list

                # 화면별 종목 업데이트
                self.screen_stocks[screen_no] = new_stocks

            # 전체 등록 종목 업데이트
            self.registered_stocks = new_registered

            return {
                "screen_registrations": screen_registrations,
                "to_unregister": to_unregister
            }

    def get_unregistered_stocks(self, watchlist_codes):
        """실시간 미등록 종목 (순환 조회 대상)"""
        with self.lock:
            return [c for c in watchlist_codes if c not in self.registered_stocks]

    def get_screen_numbers(self):
        """사용 중인 화면번호 리스트 반환"""
        return self.SCREEN_NUMBERS.copy()

    def get_registration_summary(self):
        """등록 현황 요약"""
        with self.lock:
            summary = {
                "total_registered": len(self.registered_stocks),
                "max_capacity": self.MAX_REALTIME_STOCKS,
                "screens": {}
            }
            for screen_no in self.SCREEN_NUMBERS:
                summary["screens"][screen_no] = len(self.screen_stocks.get(screen_no, set()))
            return summary


class EventEngine(QObject):
    """
    메인 이벤트 처리 엔진

    - 큐 기반 이벤트 처리
    - 디바운스로 과도한 이벤트 병합
    - 배치 스케줄러로 TR 분산 (QTimer 기반 - 메인 스레드에서 실행)
    - 실시간 등록 관리
    """

    # 배치 처리 타이밍 상수
    BATCH_INTERVAL_MS = 3000  # 배치 간격 (3초)
    STOCK_INTERVAL_MS = 350   # 종목 간 간격 (350ms)

    def __init__(self, kiwoom_api, config, log_callback=None):
        """
        Args:
            kiwoom_api: KiwoomAPI 인스턴스
            config: Config 인스턴스
            log_callback: 로그 콜백 함수
        """
        super().__init__()
        self.kiwoom = kiwoom_api
        self.config = config
        self.log_callback = log_callback

        # 이벤트 큐
        self.event_queue = Queue()

        # 디바운서 (200ms)
        self.debouncer = Debouncer(delay_ms=200)

        # 배치 스케줄러 (10종목씩, 60초 주기로 전체 갱신)
        # 200종목 기준: 10종목/3초 = 200종목/60초
        self.batch_scheduler = BatchScheduler(batch_size=10, interval_seconds=60)

        # 실시간 관리자
        self.realtime_manager = RealTimeManager()

        # 워커 스레드 (이벤트 처리용)
        self.is_running = False
        self.worker_thread = None

        # 배치 타이머 (QTimer - 메인 스레드에서 실행되어 TR 호출 안전)
        # 배치 간격 타이머: 새로운 배치를 가져오는 주기
        self.batch_timer = QTimer()
        self.batch_timer.timeout.connect(self._on_batch_timer)

        # 종목 처리 타이머: 배치 내 개별 종목 처리용
        self.stock_timer = QTimer()
        self.stock_timer.timeout.connect(self._on_stock_timer)

        self.batch_index = 0  # 현재 배치 내 처리 중인 인덱스
        self.current_batch = []  # 현재 배치 종목 리스트

        # 콜백
        self.on_price_update = None  # (code, price) 콜백
        self.on_trade_signal = None  # (code, signal_type, data) 콜백

    def log(self, message, level="INFO"):
        """로그 출력"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {message}"
        print(log_msg)
        if self.log_callback:
            self.log_callback(log_msg)

    def start(self, watchlist_codes, priority_codes=None):
        """
        이벤트 엔진 시작

        Args:
            watchlist_codes: 감시 종목 리스트
            priority_codes: 우선순위 종목 (보유 종목)
        """
        if self.is_running:
            return

        self.is_running = True

        # 키움 API에 이벤트 엔진 등록
        if self.kiwoom:
            self.kiwoom.set_event_engine(self)

        # 우선순위 종목 설정
        if priority_codes:
            self.realtime_manager.set_priority_stocks(priority_codes)

        # 배치 스케줄러에 종목 설정
        self.batch_scheduler.set_stocks(watchlist_codes)

        # 실시간 등록
        self._setup_realtime(watchlist_codes)

        # 워커 스레드 시작
        self.worker_thread = threading.Thread(target=self._event_worker, daemon=True)
        self.worker_thread.start()

        # 배치 갱신 타이머 시작 (QTimer - 메인 스레드에서 실행)
        self.batch_index = 0
        self.current_batch = []
        self.batch_timer.start(self.BATCH_INTERVAL_MS)

        self.log("이벤트 엔진 시작")

    def stop(self):
        """이벤트 엔진 중지"""
        self.is_running = False

        # 배치 타이머 중지
        self.batch_timer.stop()
        self.stock_timer.stop()

        # 모든 화면번호의 실시간 해제
        if self.kiwoom:
            for screen_no in self.realtime_manager.get_screen_numbers():
                self.kiwoom.set_real_remove(screen_no, "ALL")
            self.kiwoom.set_event_engine(None)

        self.log("이벤트 엔진 중지")

    def _setup_realtime(self, watchlist_codes):
        """
        실시간 등록 설정 (화면번호별 분배)

        키움 OpenAPI+ 규칙:
        - 한 화면번호당 최대 100종목
        - 200종목은 화면번호 2개로 분배 (1000, 1001)
        """
        result = self.realtime_manager.calculate_registrations(watchlist_codes)
        screen_registrations = result["screen_registrations"]
        to_unregister = result["to_unregister"]

        total_registered = 0

        for screen_no in self.realtime_manager.get_screen_numbers():
            # 해당 화면의 해제할 종목
            unregister_codes = to_unregister.get(screen_no, [])
            for code in unregister_codes:
                self.kiwoom.set_real_remove(screen_no, code)

            # 해당 화면에 등록할 종목
            register_codes = screen_registrations.get(screen_no, [])
            if register_codes:
                codes_str = ";".join(register_codes)
                # FID: 10(현재가), 15(거래량), 20(체결시간)
                # opt_type: "0"=최초등록, "1"=추가등록
                opt_type = "0"  # 최초 등록 (기존 해제 후 새로 등록)
                self.kiwoom.set_real_reg(screen_no, codes_str, "10;15;20", opt_type)
                total_registered += len(register_codes)

        # 등록 현황 로그
        summary = self.realtime_manager.get_registration_summary()
        screen_info = ", ".join([f"{k}:{v}개" for k, v in summary["screens"].items()])
        self.log(f"실시간 등록: 총 {total_registered}종목 ({screen_info})")

        # 미등록 종목은 순환 조회 대상
        unregistered = self.realtime_manager.get_unregistered_stocks(watchlist_codes)
        if unregistered:
            self.log(f"순환 조회 대상: {len(unregistered)}종목 (200종목 초과분)")

    def push_event(self, event_type, code, data):
        """
        이벤트 큐에 추가 (디바운스 적용)

        Args:
            event_type: 이벤트 타입 ('price', 'trade', 'batch')
            code: 종목 코드
            data: 이벤트 데이터
        """
        # 가격 이벤트는 디바운스 적용
        if event_type == "price":
            if not self.debouncer.should_process(code, data):
                return  # 디바운스로 스킵

        self.event_queue.put({
            "type": event_type,
            "code": code,
            "data": data,
            "timestamp": time.time()
        })

    def _event_worker(self):
        """이벤트 처리 워커 스레드"""
        while self.is_running:
            try:
                event = self.event_queue.get(timeout=0.1)
                self._process_event(event)
            except Empty:
                continue
            except Exception as e:
                self.log(f"이벤트 처리 오류: {e}", "ERROR")

    def _process_event(self, event):
        """개별 이벤트 처리"""
        event_type = event["type"]
        code = event["code"]
        data = event["data"]

        if event_type == "price":
            # 가격 업데이트 콜백
            if self.on_price_update:
                self.on_price_update(code, data.get("price", 0))

        elif event_type == "trade":
            # 매매 신호 콜백
            if self.on_trade_signal:
                self.on_trade_signal(code, data.get("signal_type"), data)

        elif event_type == "batch":
            # 배치 처리 결과 (봉데이터 캐시 업데이트됨)
            pass

    def _on_batch_timer(self):
        """
        배치 타이머 콜백 (QTimer - 메인 스레드에서 실행)
        새로운 배치를 가져와서 종목 처리 시작
        """
        if not self.is_running:
            return

        # 이전 배치 처리 중이면 스킵
        if self.stock_timer.isActive():
            return

        # 다음 배치 종목 가져오기
        self.current_batch = self.batch_scheduler.get_next_batch()
        if not self.current_batch:
            return

        # 첫 번째 종목부터 처리 시작
        self.batch_index = 0
        self._process_current_stock()

    def _on_stock_timer(self):
        """
        종목 처리 타이머 콜백 (QTimer - 메인 스레드에서 실행)
        배치 내 다음 종목 처리
        """
        if not self.is_running:
            self.stock_timer.stop()
            return

        self._process_current_stock()

    def _process_current_stock(self):
        """현재 배치의 다음 종목 처리"""
        if not self.is_running:
            return

        # 배치 내 모든 종목 처리 완료
        if self.batch_index >= len(self.current_batch):
            self.stock_timer.stop()
            return

        code = self.current_batch[self.batch_index]
        self.batch_index += 1

        try:
            candles = self.kiwoom.get_daily_candles(code, 25)
            if candles:
                self.batch_scheduler.update_cache(code, candles)

                # 배치 완료 이벤트
                self.push_event("batch", code, {"candles": candles})

        except Exception as e:
            self.log(f"[{code}] 봉데이터 조회 오류: {e}", "ERROR")

        # 다음 종목 처리를 위한 타이머 시작 (아직 처리할 종목이 남았으면)
        if self.batch_index < len(self.current_batch):
            self.stock_timer.start(self.STOCK_INTERVAL_MS)

    def get_candles(self, code, force_refresh=False):
        """
        봉데이터 가져오기 (캐시 우선)

        Args:
            code: 종목 코드
            force_refresh: 강제 새로고침 여부

        Returns:
            봉데이터 리스트
        """
        if not force_refresh:
            cached = self.batch_scheduler.get_cached_candles(code)
            if cached:
                return cached

        # 캐시 없으면 직접 조회
        candles = self.kiwoom.get_daily_candles(code, 25)
        if candles:
            self.batch_scheduler.update_cache(code, candles)
        return candles


# 싱글톤 인스턴스
_engine_instance = None


def get_engine():
    """이벤트 엔진 싱글톤 인스턴스"""
    global _engine_instance
    return _engine_instance


def init_engine(kiwoom_api, config, log_callback=None):
    """이벤트 엔진 초기화"""
    global _engine_instance
    _engine_instance = EventEngine(kiwoom_api, config, log_callback)
    return _engine_instance
