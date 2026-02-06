# -*- coding: utf-8 -*-
"""
키움증권 Open API+ 연동 모듈
"""
import sys
import time
from collections import deque
from threading import Lock
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QObject, pyqtSignal, QTimer
from PyQt5.QtWidgets import QApplication


class AccountSignalEmitter(QObject):
    """계좌 데이터 변경 시그널 발신기"""
    # 예수금 변경 시그널: deposit (int)
    deposit_changed = pyqtSignal(int)
    # 잔고 변경 시그널: code, quantity, avg_price
    balance_changed = pyqtSignal(str, int, int)
    # 보유종목 전체 갱신 시그널: holdings_list (list)
    holdings_updated = pyqtSignal(list)
    # 전체 잔고 정보 갱신 시그널: balance_dict (dict)
    full_balance_updated = pyqtSignal(dict)


class RateLimiter:
    """TR 호출 제한 관리 클래스 (초당 5회 제한)"""

    def __init__(self, max_calls=5, period=1.0):
        """
        Args:
            max_calls: 기간 내 최대 호출 횟수
            period: 기간 (초)
        """
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()
        self.lock = Lock()

    def wait_if_needed(self):
        """필요시 대기하여 호출 제한 준수"""
        with self.lock:
            now = time.time()

            # 기간이 지난 호출 기록 제거
            while self.calls and self.calls[0] < now - self.period:
                self.calls.popleft()

            # 제한에 도달했으면 대기
            if len(self.calls) >= self.max_calls:
                sleep_time = self.calls[0] + self.period - now + 0.05  # 50ms 여유
                if sleep_time > 0:
                    time.sleep(sleep_time)

                # 다시 정리
                now = time.time()
                while self.calls and self.calls[0] < now - self.period:
                    self.calls.popleft()

            # 현재 호출 기록
            self.calls.append(time.time())


class OrderQueue:
    """주문 호출 큐 - 초당 주문 제한 준수 (에러코드 -308 방지)"""

    def __init__(self, kiwoom_api):
        self.kiwoom = kiwoom_api
        self._queue = deque()  # (order_func, args, kwargs, callback) 튜플
        self._is_processing = False
        self._process_timer = QTimer()
        self._process_timer.setSingleShot(True)
        self._process_timer.timeout.connect(self._process_next)
        # 주문 간 최소 간격 (키움 API 초당 주문 제한: 1초에 5건 미만 권장)
        self._min_interval_ms = 300  # 300ms 간격 = 초당 약 3건

    def enqueue(self, order_func, callback=None, *args, **kwargs):
        """
        주문 호출을 큐에 추가

        Args:
            order_func: 실행할 주문 함수 (예: kiwoom.send_order)
            callback: 결과를 받을 콜백 함수 (선택) - (result, args) 전달
            *args, **kwargs: 주문 함수에 전달할 인자
        """
        self._queue.append((order_func, args, kwargs, callback))
        self._debug(f"[주문큐] 추가됨: {order_func.__name__} (대기: {len(self._queue)}개)")

        # 처리 중이 아니면 시작
        if not self._is_processing:
            self._start_processing()

    def _start_processing(self):
        """큐 처리 시작"""
        if self._is_processing:
            return
        self._is_processing = True
        # 즉시 시작하지 않고 약간의 지연 후 시작 (이벤트 루프 안정화)
        self._process_timer.start(50)

    def _process_next(self):
        """큐에서 다음 주문 처리"""
        if not self._queue:
            self._is_processing = False
            self._debug("[주문큐] 큐 비어있음 - 처리 완료")
            return

        order_func, args, kwargs, callback = self._queue.popleft()
        self._debug(f"[주문큐] 처리 시작: {order_func.__name__} args={args[:3] if len(args) > 3 else args} (남은 대기: {len(self._queue)}개)")

        try:
            result = order_func(*args, **kwargs)
            self._debug(f"[주문큐] 결과: {result}")
            if callback:
                try:
                    callback(result, args)
                except Exception as e:
                    self._debug(f"[주문큐] 콜백 오류: {e}")
        except Exception as e:
            self._debug(f"[주문큐] 주문 실행 오류: {e}")
            if callback:
                try:
                    callback(-999, args)  # 오류 코드
                except Exception:
                    pass

        # 다음 주문 처리 (간격 유지)
        if self._queue:
            self._process_timer.start(self._min_interval_ms)
        else:
            self._is_processing = False
            self._debug("[주문큐] 모든 주문 처리 완료")

    def clear(self):
        """큐 비우기"""
        self._queue.clear()
        self._is_processing = False
        self._debug("[주문큐] 큐 초기화됨")

    def is_empty(self):
        """큐가 비어있는지 확인"""
        return len(self._queue) == 0

    def pending_count(self):
        """대기 중인 주문 개수"""
        return len(self._queue)

    def _debug(self, message):
        if self.kiwoom.debug:
            print(message)


class TRQueue:
    """TR 호출 큐 - 순차 처리로 중첩 호출 방지"""

    def __init__(self, kiwoom_api):
        self.kiwoom = kiwoom_api
        self._queue = deque()  # (tr_func, args, kwargs, callback) 튜플
        self._is_processing = False
        self._process_timer = QTimer()
        self._process_timer.setSingleShot(True)
        self._process_timer.timeout.connect(self._process_next)
        self._min_interval_ms = 250  # TR 호출 간 최소 간격

    def enqueue(self, tr_func, callback=None, *args, **kwargs):
        """
        TR 호출을 큐에 추가

        Args:
            tr_func: 실행할 TR 함수 (예: kiwoom.get_balance)
            callback: 결과를 받을 콜백 함수 (선택)
            *args, **kwargs: TR 함수에 전달할 인자
        """
        self._queue.append((tr_func, args, kwargs, callback))
        self._debug(f"[TR큐] 추가됨: {tr_func.__name__} (대기: {len(self._queue)}개)")

        # 처리 중이 아니면 시작
        if not self._is_processing:
            self._start_processing()

    def _start_processing(self):
        """큐 처리 시작"""
        if self._is_processing:
            return
        self._is_processing = True
        # 즉시 시작하지 않고 약간의 지연 후 시작 (이벤트 루프 안정화)
        self._process_timer.start(50)

    def _process_next(self):
        """큐에서 다음 TR 처리"""
        # TR이 이미 처리 중이면 대기
        if self.kiwoom._tr_busy:
            self._debug("[TR큐] TR 처리 중 - 대기")
            self._process_timer.start(self._min_interval_ms)
            return

        if not self._queue:
            self._is_processing = False
            self._debug("[TR큐] 큐 비어있음 - 처리 완료")
            return

        tr_func, args, kwargs, callback = self._queue.popleft()
        self._debug(f"[TR큐] 처리 시작: {tr_func.__name__} (남은 대기: {len(self._queue)}개)")

        try:
            result = tr_func(*args, **kwargs)
            if callback:
                try:
                    callback(result)
                except Exception as e:
                    self._debug(f"[TR큐] 콜백 오류: {e}")
        except Exception as e:
            self._debug(f"[TR큐] TR 실행 오류: {e}")
            if callback:
                try:
                    callback(None)
                except Exception:
                    pass

        # 다음 TR 처리 (간격 유지)
        if self._queue:
            self._process_timer.start(self._min_interval_ms)
        else:
            self._is_processing = False
            self._debug("[TR큐] 모든 TR 처리 완료")

    def clear(self):
        """큐 비우기"""
        self._queue.clear()
        self._is_processing = False
        self._debug("[TR큐] 큐 초기화됨")

    def is_empty(self):
        """큐가 비어있는지 확인"""
        return len(self._queue) == 0

    def pending_count(self):
        """대기 중인 TR 개수"""
        return len(self._queue)

    def _debug(self, message):
        if self.kiwoom.debug:
            print(message)


class KiwoomAPI:
    """키움증권 Open API+ 래퍼 클래스"""

    def __init__(self):
        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self.connected = False
        self.login_event_loop = None
        self.tr_event_loop = None
        self.order_event_loop = None

        self.account_list = []
        self.tr_data = {}
        self._expected_rqname = None
        self._expected_trcode = None
        self._pending_tr_data = {}
        self._tr_timed_out = False
        # ✅ TR 재진입 방지 플래그 (QEventLoop 중첩 호출 차단)
        self._tr_busy = False
        # 타임아웃 비활성화 (0 또는 None이면 무제한 대기)
        # opw00018 등 계좌 조회는 시간이 오래 걸릴 수 있음
        self._tr_timeout_ms = 0
        self._tr_record_overrides = {}

        # TR 호출 제한 (초당 5회)
        self.rate_limiter = RateLimiter(max_calls=5, period=1.0)

        # ✅ 계좌 데이터 시그널 발신기
        self.account_signals = AccountSignalEmitter()

        # ✅ 종목 코드/이름 캐시 (UI 프리징 방지)
        self._stock_cache = {}  # {code: name}
        self._stock_cache_loaded = False

        # ✅ TR 호출 큐 (중첩 호출 방지)
        self.tr_queue = TRQueue(self)

        # ✅ 주문 호출 큐 (에러코드 -308 방지 - 초당 주문 제한)
        self.order_queue = OrderQueue(self)

        # 이벤트 연결
        self.ocx.OnEventConnect.connect(self._on_event_connect)
        self.ocx.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.ocx.OnReceiveChejanData.connect(self._on_receive_chejan_data)
        self.ocx.OnReceiveRealData.connect(self._on_receive_real_data)
        self.ocx.OnReceiveMsg.connect(self._on_receive_msg)

        # 콜백 함수들
        self.on_chejan_callback = None
        self.on_real_data_callback = None
        self.on_message_callback = None

        # 이벤트 엔진 참조 (나중에 설정)
        self.event_engine = None
        # 디버그 로그
        self.debug = False

    def set_debug(self, enabled=True):
        """디버그 로그 토글"""
        self.debug = bool(enabled)

    def _debug(self, message):
        if self.debug:
            print(message)

    def is_tr_busy(self):
        """TR 요청 처리 중 여부 확인 (재진입 방지용)"""
        return self._tr_busy

    def is_tr_queue_busy(self):
        """TR 큐가 처리 중인지 확인"""
        return self.tr_queue._is_processing or self._tr_busy

    # ==================== 큐 기반 TR 조회 (비동기) ====================
    def get_balance_async(self, account, callback):
        """계좌 잔고 조회 (큐 기반 비동기)"""
        self.tr_queue.enqueue(self.get_balance, callback, account)

    def get_daily_candles_async(self, code, callback, count=60):
        """일봉 데이터 조회 (큐 기반 비동기)"""
        self.tr_queue.enqueue(self.get_daily_candles, callback, code, count)

    def get_stock_info_async(self, code, callback):
        """종목 기본 정보 조회 (큐 기반 비동기)"""
        self.tr_queue.enqueue(self.get_stock_info, callback, code)

    def get_deposit_async(self, account, callback):
        """예수금 상세 조회 (큐 기반 비동기)"""
        self.tr_queue.enqueue(self.get_deposit, callback, account)

    def set_event_engine(self, engine):
        """이벤트 엔진 설정"""
        self.event_engine = engine

    # ==================== 로그인 ====================
    def login(self):
        """로그인 요청"""
        server = self.ocx.dynamicCall("CommConnect()")
        self.login_event_loop = QEventLoop()
        self.login_event_loop.exec_()
        print("server:", server)

        # ✅ 로그인 성공 후 계좌 비밀번호 입력창 방지 설정
        if self.connected:
            self._setup_account_password()

        return self.connected

    def _setup_account_password(self):
        """
        계좌 비밀번호 입력창 자동 생성 방지
        KOA Studio에 저장된 비밀번호를 사용하도록 설정
        """
        try:
            # 계좌비밀번호 입력창 표시 안함 (저장된 비밀번호 사용)
            result = self.ocx.dynamicCall(
                "KOA_Functions(QString, QString)",
                "ShowAccountWindow", ""
            )
            print(f"[계좌설정] ShowAccountWindow: {result}")
        except Exception as e:
            print(f"[계좌설정] 오류: {e}")

    def _on_event_connect(self, err_code):
        """로그인 이벤트 핸들러"""
        if err_code == 0:
            self.connected = True
            self.account_list = self.get_account_list()

            # ✅ 서버 구분 확인 (실서버/모의투자)
            server_gubun = self.get_server_gubun()
            is_real = self.is_real_server()

            print(f"=" * 50)
            print(f"로그인 성공!")
            print(f"서버: {server_gubun}")
            print(f"계좌: {self.account_list}")
            print(f"=" * 50)

            # ✅ 모의투자 서버 연결 시 경고
            if not is_real:
                print("")
                print("[경고] 현재 모의투자 서버에 연결되어 있습니다!")
                print("[안내] 실계좌를 사용하려면:")
                print("  1. 영웅문HTS 자동로그인을 해제하세요.")
                print("  2. KOA Studio를 재실행하세요.")
                print("  3. 로그인 창에서 '모의투자' 체크를 해제하세요.")
                print("  4. 계좌 비밀번호를 다시 등록하세요.")
                print("")
        else:
            # ✅ 로그인 실패 (err_code != 0)
            self.connected = False
            print(f"로그인 실패: 에러코드 {err_code}")

        if self.login_event_loop:
            self.login_event_loop.exit()

    def is_connected(self):
        """연결 상태 확인"""
        state = self.ocx.dynamicCall("GetConnectState()")
        return state == 1

    # ==================== 계좌 정보 ====================
    def get_account_list(self):
        """계좌 목록 조회"""
        accounts = self.ocx.dynamicCall("GetLoginInfo(QString)", "ACCLIST")
        return accounts.strip(';').split(';') if accounts else []

    def get_login_info(self, tag):
        """로그인 정보 조회"""
        return self.ocx.dynamicCall("GetLoginInfo(QString)", tag)

    def get_server_gubun(self):
        """
        서버 구분 조회 (실서버/모의투자)

        Returns:
            str: "실서버" 또는 "모의투자"
        """
        gubun = self.ocx.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
        # 반환값: "1" = 모의투자, "" 또는 다른값 = 실서버
        if gubun == "1":
            return "모의투자"
        return "실서버"

    def is_real_server(self):
        """
        실서버 연결 여부 확인

        Returns:
            bool: 실서버면 True, 모의투자면 False
        """
        gubun = self.ocx.dynamicCall("GetLoginInfo(QString)", "GetServerGubun")
        # "1" = 모의투자, 그 외 = 실서버
        return gubun != "1"

    # ==================== TR 요청 ====================
    def set_input_value(self, id, value):
        """TR 입력값 설정"""
        self.ocx.dynamicCall("SetInputValue(QString, QString)", id, value)

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        """TR 요청 (Rate Limiting + 재진입 방지 적용)"""
        # ✅ 재진입 방지: 이미 TR 처리 중이면 경고 후 스킵
        if self._tr_busy:
            self._debug(f"[TR] BLOCKED (busy) rqname={rqname} trcode={trcode}")
            return

        # TR 호출 제한 대기
        self.rate_limiter.wait_if_needed()

        self._tr_busy = True
        self._debug(f"[TR] request rqname={rqname} trcode={trcode}")
        self.tr_data = {}

        try:
            self.ocx.dynamicCall(
                "CommRqData(QString, QString, int, QString)",
                rqname, trcode, next, screen_no
            )
            self.tr_event_loop = QEventLoop()
            self.tr_event_loop.exec_()
        finally:
            self._tr_busy = False
        self._debug(f"[TR] completed rqname={rqname} trcode={trcode}")

    def _on_receive_tr_data(self, screen_no, rqname, trcode, record_name, next,
                            unused1, unused2, unused3, unused4):
        """TR 데이터 수신 이벤트 핸들러"""
        self._debug(f"[TR] received rqname={rqname} trcode={trcode} record_name={record_name}")

        # TR 데이터 기본 정보 저장
        self.tr_data = {
            "screen_no": screen_no,
            "rqname": rqname,
            "trcode": trcode,
            "record_name": record_name,
            "next": next
        }

        # ✅ TR 응답 직후 데이터 즉시 추출 (버퍼 유실 방지)
        # opt10001: 주식기본정보
        if trcode == "opt10001":
            self.tr_data["opt10001_data"] = {
                "name": self.get_comm_data(trcode, rqname, 0, "종목명"),
                "price": self.get_comm_data(trcode, rqname, 0, "현재가"),
                "volume": self.get_comm_data(trcode, rqname, 0, "거래량"),
                "high": self.get_comm_data(trcode, rqname, 0, "고가"),
                "low": self.get_comm_data(trcode, rqname, 0, "저가"),
                "open": self.get_comm_data(trcode, rqname, 0, "시가"),
            }
            self._debug(f"[TR] opt10001 extracted: {self.tr_data['opt10001_data']}")

        # opt10081: 일봉데이터
        elif trcode == "opt10081":
            candles = []
            repeat_cnt = self.get_repeat_cnt(trcode, rqname)
            self._debug(f"[TR] opt10081 repeat_cnt={repeat_cnt} (rqname={rqname})")
            for i in range(min(repeat_cnt, 60)):
                candle = {
                    "date": self.get_comm_data(trcode, rqname, i, "일자"),
                    "open": self.get_comm_data(trcode, rqname, i, "시가"),
                    "high": self.get_comm_data(trcode, rqname, i, "고가"),
                    "low": self.get_comm_data(trcode, rqname, i, "저가"),
                    "close": self.get_comm_data(trcode, rqname, i, "현재가"),
                    "volume": self.get_comm_data(trcode, rqname, i, "거래량"),
                }
                candles.append(candle)
            self.tr_data["opt10081_candles"] = candles
            self._debug(f"[TR] opt10081 extracted {len(candles)} candles")

        # opw00018: 계좌잔고
        elif trcode == "opw00018":
            self._extract_opw00018_data(rqname)

        # opw00001: 예수금 상세
        elif trcode == "opw00001":
            self._extract_opw00001_data(rqname)

        self._debug(f"[TR] received rqname={rqname} trcode={trcode} record_name={record_name} next={next}")

        if self.tr_event_loop and self.tr_event_loop.isRunning():
            self.tr_event_loop.exit()

    def get_comm_data(self, trcode, record_name, index, item):
        """TR 데이터 가져오기"""
        data = self.ocx.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            trcode, record_name, index, item
        )
        return data.strip()

    def get_repeat_cnt(self, trcode, record_name):
        """반복 데이터 개수"""
        return self.ocx.dynamicCall(
            "GetRepeatCnt(QString, QString)",
            trcode, record_name
        )

    def _get_record_name(self, fallback=None, trcode=None, rqname=None):
        """마지막 TR 응답의 레코드명 가져오기 (없으면 rqname/fallback)"""
        if trcode:
            override = self._tr_record_overrides.get(trcode)
            if override:
                self._debug(f"[TR] record override used trcode={trcode} record_name={override}")
                return override
        if isinstance(self.tr_data, dict):
            name = self.tr_data.get("record_name")
            if name:
                return name
        if rqname:
            self._debug(f"[TR] record_name missing, rqname used trcode={trcode} record_name={rqname}")
            return rqname
        if fallback:
            self._debug(f"[TR] record_name missing, fallback used trcode={trcode} record_name={fallback}")
            return fallback
        return ""

    def set_tr_record_override(self, trcode, record_name):
        """TR 레코드명 수동 설정 (매뉴얼 기준 보정용)"""
        if trcode and record_name:
            self._tr_record_overrides[trcode] = record_name

    # ==================== 주식 기본 정보 ====================
    def get_master_code_name(self, code):
        """종목명 조회"""
        return self.ocx.dynamicCall("GetMasterCodeName(QString)", code)

    def get_code_list_by_market(self, market):
        """시장별 종목코드 목록 조회

        Args:
            market: 시장 구분 (0: 코스피, 10: 코스닥, 3: ELW, 8: ETF)

        Returns:
            종목코드 리스트
        """
        code_list = self.ocx.dynamicCall("GetCodeListByMarket(QString)", market)
        if code_list:
            return code_list.split(";")[:-1]  # 마지막 빈 문자열 제거
        return []

    def load_stock_cache(self):
        """
        전체 종목 코드/이름을 메모리 캐시에 로드 (로그인 직후 1회 호출)
        UI 프리징 방지를 위해 로그인 직후 메인 스레드에서 한 번만 호출
        """
        if self._stock_cache_loaded:
            return True

        print("[종목캐시] 전체 종목 로딩 시작...")
        self._stock_cache = {}

        try:
            # 코스피(0)와 코스닥(10) 종목 로드
            for market in ["0", "10"]:
                codes = self.get_code_list_by_market(market)
                for code in codes:
                    name = self.get_master_code_name(code)
                    if name:
                        self._stock_cache[code] = name

            self._stock_cache_loaded = True
            print(f"[종목캐시] 로딩 완료: {len(self._stock_cache)}개 종목")
            return True
        except Exception as e:
            print(f"[종목캐시] 로딩 오류: {e}")
            return False

    def is_stock_cache_loaded(self):
        """종목 캐시 로드 여부 확인"""
        return self._stock_cache_loaded

    def get_stock_name_from_cache(self, code):
        """캐시에서 종목명 조회 (캐시 미스 시 API 호출)"""
        if code in self._stock_cache:
            return self._stock_cache[code]
        # 캐시에 없으면 API 호출 후 캐시에 추가
        name = self.get_master_code_name(code)
        if name:
            self._stock_cache[code] = name
        return name

    def find_stocks_by_name(self, search_name):
        """종목명으로 종목코드 검색 (캐시 기반 - UI 프리징 없음)

        Args:
            search_name: 검색할 종목명 (부분 일치)

        Returns:
            [(종목코드, 종목명), ...] 리스트
        """
        results = []
        search_name = search_name.upper()  # 대소문자 무시

        # ✅ 캐시가 로드되어 있으면 캐시에서 검색 (빠름)
        if self._stock_cache_loaded:
            for code, name in self._stock_cache.items():
                if name and search_name in name.upper():
                    results.append((code, name))
            return results

        # 캐시가 없으면 기존 방식 (느림 - 호환성 유지)
        for market in ["0", "10"]:
            codes = self.get_code_list_by_market(market)
            for code in codes:
                name = self.get_master_code_name(code)
                if name and search_name in name.upper():
                    results.append((code, name))

        return results

    def get_stock_price(self, code):
        """현재가 조회 (opt10001)"""
        self.set_input_value("종목코드", code)
        self.comm_rq_data("주식기본정보", "opt10001", 0, "0101")

        # TR 핸들러에서 미리 추출된 데이터 사용
        data = self.tr_data.get("opt10001_data", {})
        price_str = data.get("price", "")
        self._debug(f"[opt10001] code={code} price='{price_str}'")
        return abs(int(price_str)) if price_str else 0

    def get_stock_info(self, code):
        """종목 기본 정보 조회"""
        self.set_input_value("종목코드", code)
        self.comm_rq_data("주식기본정보", "opt10001", 0, "0101")

        # TR 핸들러에서 미리 추출된 데이터 사용
        data = self.tr_data.get("opt10001_data", {})
        self._debug(f"[opt10001] code={code} extracted_data={data}")

        info = {
            "code": code,
            "name": data.get("name", ""),
            "price": abs(int(data.get("price") or 0)),
            "volume": int(data.get("volume") or 0),
            "high": abs(int(data.get("high") or 0)),
            "low": abs(int(data.get("low") or 0)),
            "open": abs(int(data.get("open") or 0)),
        }
        self._debug(f"[opt10001] result: price={info['price']} name='{info['name']}'")
        return info

    # ==================== TR 응답 내 데이터 추출 (버퍼 유실 방지) ====================
    def _extract_opw00018_data(self, rqname):
        """opw00018 데이터를 TR 응답 핸들러 내에서 즉시 추출"""
        def safe_int(val):
            if val is None or val.strip() == "":
                return 0
            try:
                return int(val.replace(",", "").replace("-", ""))
            except ValueError:
                return 0

        # 싱글 데이터 - 예수금 관련 필드들
        single_record_names = ["", "opw00018", rqname, "계좌평가결과"]
        deposit_fields = [
            "D+2추정예수금", "추정예탁자산", "예수금", "D+2예수금",
            "출금가능금액", "주문가능금액", "예탁자산평가액",
        ]

        deposit = 0
        single_record = ""

        for rec_name in single_record_names:
            for field in deposit_fields:
                val = self.get_comm_data("opw00018", rec_name, 0, field)
                deposit = safe_int(val)
                if deposit > 0:
                    single_record = rec_name
                    self._debug(f"[opw00018] found deposit: rec='{rec_name}' field='{field}' val={deposit}")
                    break
            if deposit > 0:
                break

        # 싱글 데이터 저장
        self.tr_data["opw00018_single"] = {
            "record_name": single_record,
            "deposit": deposit,
            "total_purchase": safe_int(self.get_comm_data("opw00018", single_record, 0, "총매입금액")),
            "total_eval": safe_int(self.get_comm_data("opw00018", single_record, 0, "총평가금액")),
            "total_profit": safe_int(self.get_comm_data("opw00018", single_record, 0, "총평가손익금액")),
            "profit_rate_str": self.get_comm_data("opw00018", single_record, 0, "총수익률(%)"),
        }

        # 멀티 데이터 - 보유종목
        record_names = ["", "opw00018", rqname, "계좌평가잔고개별합산"]
        repeat_cnt = 0
        used_record_name = ""

        for rec_name in record_names:
            repeat_cnt = self.get_repeat_cnt("opw00018", rec_name)
            if repeat_cnt > 0:
                used_record_name = rec_name
                self._debug(f"[opw00018] found holdings: rec='{rec_name}' count={repeat_cnt}")
                break

        holdings = []
        for i in range(repeat_cnt):
            code_raw = self.get_comm_data("opw00018", used_record_name, i, "종목번호").strip()
            if not code_raw:
                code_raw = self.get_comm_data("opw00018", used_record_name, i, "종목코드").strip()
            if not code_raw:
                continue

            holding = {
                "code_raw": code_raw,
                "name": self.get_comm_data("opw00018", used_record_name, i, "종목명").strip(),
                "quantity_str": self.get_comm_data("opw00018", used_record_name, i, "보유수량"),
                "quantity_str2": self.get_comm_data("opw00018", used_record_name, i, "현재보유량"),
                "avg_price_str": self.get_comm_data("opw00018", used_record_name, i, "매입가"),
                "avg_price_str2": self.get_comm_data("opw00018", used_record_name, i, "평균매입가"),
                "current_price_str": self.get_comm_data("opw00018", used_record_name, i, "현재가"),
                "eval_amount_str": self.get_comm_data("opw00018", used_record_name, i, "평가금액"),
                "profit_str": self.get_comm_data("opw00018", used_record_name, i, "평가손익"),
                "profit_str2": self.get_comm_data("opw00018", used_record_name, i, "손익금액"),
                "profit_rate_str": self.get_comm_data("opw00018", used_record_name, i, "수익률(%)"),
                "profit_rate_str2": self.get_comm_data("opw00018", used_record_name, i, "수익률"),
            }
            holdings.append(holding)

        self.tr_data["opw00018_holdings"] = holdings
        self._debug(f"[opw00018] extracted: deposit={deposit} holdings_count={len(holdings)}")

    def _extract_opw00001_data(self, rqname):
        """opw00001 데이터를 TR 응답 핸들러 내에서 즉시 추출"""
        record_names = ["", "opw00001", rqname, "예수금상세현황"]

        deposit_fields = {
            "예수금": "",
            "D+1예수금": "",
            "D+1추정예수금": "",
            "D+2예수금": "",
            "D+2추정예수금": "",
            "출금가능금액": "",
            "인출가능금액": "",
            "주문가능금액": "",
        }

        used_record = ""
        for rec_name in record_names:
            val = self.get_comm_data("opw00001", rec_name, 0, "예수금")
            if val and val.strip():
                used_record = rec_name
                self._debug(f"[opw00001] found record: '{rec_name}'")
                break

        for field in deposit_fields:
            deposit_fields[field] = self.get_comm_data("opw00001", used_record, 0, field)

        self.tr_data["opw00001_data"] = {
            "record_name": used_record,
            "fields": deposit_fields,
        }
        self._debug(f"[opw00001] extracted: {deposit_fields}")

    # ==================== 일봉 데이터 (이동평균 계산용) ====================
    def get_daily_candles(self, code, count=60):
        """일봉 데이터 조회 (opt10081)"""
        self.set_input_value("종목코드", code)
        self.set_input_value("기준일자", "")  # 오늘 기준
        self.set_input_value("수정주가구분", "1")
        self.comm_rq_data("일봉조회", "opt10081", 0, "0102")

        # TR 핸들러에서 미리 추출된 데이터 사용
        raw_candles = self.tr_data.get("opt10081_candles", [])
        self._debug(f"[opt10081] code={code} raw_candles_count={len(raw_candles)}")

        candles = []
        for raw in raw_candles[:count]:
            candle = {
                "date": raw.get("date", ""),
                "open": abs(int(raw.get("open") or 0)),
                "high": abs(int(raw.get("high") or 0)),
                "low": abs(int(raw.get("low") or 0)),
                "close": abs(int(raw.get("close") or 0)),
                "volume": int(raw.get("volume") or 0),
            }
            candles.append(candle)

        self._debug(f"[opt10081] code={code} candles_count={len(candles)}")
        return candles

    # ==================== 계좌 잔고 ====================
    def get_balance(self, account):
        """계좌 잔고 조회 (opw00018)"""
        self._debug(f"[opw00018] account={account} (len={len(account)})")

        self.set_input_value("계좌번호", account)
        self.set_input_value("비밀번호", "")  # 비밀번호 직접 전달
        self.set_input_value("비밀번호입력매체구분", "00")
        # 조회구분: 1=합산, 2=개별
        self.set_input_value("조회구분", "1")
        self.comm_rq_data("계좌잔고", "opw00018", 0, "0103")

        # ✅ TR 핸들러에서 미리 추출된 데이터 사용 (버퍼 유실 방지)
        def safe_int(val):
            if val is None or str(val).strip() == "":
                return 0
            try:
                return int(str(val).replace(",", "").replace("-", ""))
            except ValueError:
                return 0

        # 싱글 데이터 (예수금, 총평가 등)
        single_data = self.tr_data.get("opw00018_single", {})
        deposit = single_data.get("deposit", 0)
        total_purchase = single_data.get("total_purchase", 0)
        total_eval = single_data.get("total_eval", 0)
        total_profit = single_data.get("total_profit", 0)
        profit_rate_str = single_data.get("profit_rate_str", "")

        try:
            # 키움 API는 수익률을 100배한 값으로 반환 (예: -11.58% → -1158)
            profit_rate = float(profit_rate_str) / 100.0 if profit_rate_str else 0.0
        except ValueError:
            profit_rate = 0.0

        balance = {
            "total_purchase": total_purchase,
            "total_eval": total_eval,
            "total_profit": total_profit,
            "profit_rate": profit_rate,
            "deposit": deposit,
        }
        self._debug(f"[opw00018] single_data: deposit={deposit} total_eval={total_eval}")

        # 보유 종목 (멀티 데이터)
        holdings_raw = self.tr_data.get("opw00018_holdings", [])
        holdings = []

        for raw in holdings_raw:
            code_raw = raw.get("code_raw", "")
            if not code_raw:
                continue
            code = code_raw.replace("A", "").replace(" ", "")

            name = raw.get("name", "")

            # 보유수량
            quantity_str = raw.get("quantity_str", "")
            if not quantity_str or quantity_str.strip() == "":
                quantity_str = raw.get("quantity_str2", "")
            quantity = abs(safe_int(quantity_str))

            # 매입가
            avg_price_str = raw.get("avg_price_str", "")
            if not avg_price_str or avg_price_str.strip() == "":
                avg_price_str = raw.get("avg_price_str2", "")
            avg_price = abs(safe_int(avg_price_str))

            # 현재가
            current_price = abs(safe_int(raw.get("current_price_str", "")))

            # 평가금액
            eval_amount = abs(safe_int(raw.get("eval_amount_str", "")))
            if eval_amount == 0 and current_price > 0 and quantity > 0:
                eval_amount = current_price * quantity

            # 평가손익
            profit_str = raw.get("profit_str", "")
            if not profit_str or profit_str.strip() == "":
                profit_str = raw.get("profit_str2", "")
            profit = safe_int(profit_str)
            if profit == 0 and eval_amount > 0 and avg_price > 0:
                profit = eval_amount - (avg_price * quantity)

            # 수익률
            profit_rate_str = raw.get("profit_rate_str", "")
            if not profit_rate_str or profit_rate_str.strip() == "":
                profit_rate_str = raw.get("profit_rate_str2", "")
            try:
                # 키움 API는 수익률을 100배한 값으로 반환 (예: -11.58% → -1158)
                item_profit_rate = float(profit_rate_str) / 100.0 if profit_rate_str else 0.0
            except ValueError:
                item_profit_rate = 0.0
            if item_profit_rate == 0.0 and avg_price > 0:
                item_profit_rate = ((current_price - avg_price) / avg_price) * 100

            holding = {
                "code": code,
                "name": name,
                "quantity": quantity,
                "avg_price": avg_price,
                "current_price": current_price,
                "eval_amount": eval_amount,
                "profit": profit,
                "profit_rate": item_profit_rate,
            }

            if holding["quantity"] > 0:
                holdings.append(holding)
                self._debug(
                    "[opw00018] holding "
                    f"code={code} name={name} qty={quantity} avg={avg_price} "
                    f"cur={current_price} eval={eval_amount} profit={profit} rate={item_profit_rate:.2f}"
                )

        balance["holdings"] = holdings

        # ✅ 시그널 발신: 전체 잔고 정보 갱신
        try:
            self.account_signals.full_balance_updated.emit(balance)
            if deposit > 0:
                self.account_signals.deposit_changed.emit(deposit)
            if holdings:
                self.account_signals.holdings_updated.emit(holdings)
        except Exception:
            pass

        return balance

    def get_deposit(self, account):
        """
        ✅ 예수금 상세 조회 (opw00001)

        opw00018에서 예수금이 안 나올 경우 이 함수 사용
        """
        self.set_input_value("계좌번호", account)
        self.set_input_value("비밀번호", "")  # 비밀번호 직접 전달
        self.set_input_value("비밀번호입력매체구분", "00")
        self.set_input_value("조회구분", "2")  # 2: 일반조회
        self.comm_rq_data("예수금조회", "opw00001", 0, "0106")

        # ✅ TR 핸들러에서 미리 추출된 데이터 사용 (버퍼 유실 방지)
        def safe_int(val):
            if val is None or str(val).strip() == "":
                return 0
            try:
                val = str(val).strip().replace(",", "")
                if val.startswith("-"):
                    return -int(val[1:])
                return int(val.replace("-", ""))
            except ValueError:
                return 0

        # TR 핸들러에서 추출된 데이터
        extracted = self.tr_data.get("opw00001_data", {})
        fields = extracted.get("fields", {})

        def first_nonzero(field_names):
            for name in field_names:
                val = safe_int(fields.get(name, ""))
                if val != 0:
                    return val
            return 0

        deposit_info = {
            "deposit": first_nonzero(["예수금", "D+2추정예수금", "D+2예수금", "주문가능금액"]),
            "deposit_d1": first_nonzero(["D+1예수금", "D+1추정예수금"]),
            "deposit_d2": first_nonzero(["D+2예수금", "D+2추정예수금"]),
            "available": first_nonzero(["출금가능금액", "인출가능금액"]),
            "order_available": first_nonzero(["주문가능금액", "출금가능금액"]),
        }

        self._debug(f"[opw00001] extracted_fields={fields} parsed={deposit_info}")

        best_deposit = deposit_info.get("order_available", 0) or deposit_info.get("deposit_d2", 0) or deposit_info.get("deposit", 0)
        try:
            if best_deposit > 0:
                self.account_signals.deposit_changed.emit(best_deposit)
        except Exception:
            pass

        return deposit_info

    # ==================== 주문 ====================
    def send_order(self, rqname, screen_no, account, order_type, code, quantity, price, hoga, org_order_no=""):
        """
        주문 전송
        order_type: 1=신규매수, 2=신규매도, 3=매수취소, 4=매도취소, 5=매수정정, 6=매도정정
        hoga: 00=지정가, 03=시장가
        """
        result = self.ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            [rqname, screen_no, account, order_type, code, quantity, price, hoga, org_order_no]
        )
        return result

    def buy_stock(self, account, code, quantity, price=0):
        """
        매수 주문
        price=0이면 시장가
        """
        hoga = "03" if price == 0 else "00"
        return self.send_order("매수주문", "0201", account, 1, code, quantity, price, hoga)

    def sell_stock(self, account, code, quantity, price=0):
        """
        매도 주문
        price=0이면 시장가
        """
        hoga = "03" if price == 0 else "00"
        return self.send_order("매도주문", "0202", account, 2, code, quantity, price, hoga)

    # ==================== 큐 기반 주문 (에러코드 -308 방지) ====================
    def send_order_queued(self, rqname, screen_no, account, order_type, code, quantity, price, hoga, callback=None, org_order_no=""):
        """
        주문 전송 (큐 기반 - 초당 주문 제한 준수)

        Args:
            callback: 주문 결과 콜백 (result, args) 전달
        """
        self.order_queue.enqueue(
            self.send_order,
            callback,
            rqname, screen_no, account, order_type, code, quantity, price, hoga, org_order_no
        )

    def buy_stock_queued(self, account, code, quantity, price=0, callback=None):
        """
        매수 주문 (큐 기반)
        price=0이면 시장가
        """
        hoga = "03" if price == 0 else "00"
        self.order_queue.enqueue(
            self.send_order,
            callback,
            "매수주문", "0201", account, 1, code, quantity, price, hoga, ""
        )

    def sell_stock_queued(self, account, code, quantity, price=0, callback=None):
        """
        매도 주문 (큐 기반)
        price=0이면 시장가
        """
        hoga = "03" if price == 0 else "00"
        self.order_queue.enqueue(
            self.send_order,
            callback,
            "매도주문", "0202", account, 2, code, quantity, price, hoga, ""
        )

    def cancel_order(self, account, code, order_no, quantity):
        """
        주문 취소
        order_type: 3=매수취소, 4=매도취소
        """
        # 매도 주문 취소
        return self.send_order("주문취소", "0203", account, 4, code, quantity, 0, "00", order_no)

    def get_open_orders(self, account):
        """미체결 주문 조회 (opt10075)"""
        self.set_input_value("계좌번호", account)
        self.set_input_value("전체종목구분", "0")  # 0: 전체, 1: 종목
        self.set_input_value("매매구분", "0")  # 0: 전체, 1: 매도, 2: 매수
        self.set_input_value("종목코드", "")
        self.set_input_value("체결구분", "1")  # 1: 미체결
        self.comm_rq_data("미체결조회", "opt10075", 0, "0104")

        record_name = self._get_record_name("미체결조회", "opt10075", "미체결조회")
        orders = []
        repeat_cnt = self.get_repeat_cnt("opt10075", record_name)

        for i in range(repeat_cnt):
            order = {
                "order_no": self.get_comm_data("opt10075", record_name, i, "주문번호").strip(),
                "code": self.get_comm_data("opt10075", record_name, i, "종목코드").strip(),
                "name": self.get_comm_data("opt10075", record_name, i, "종목명").strip(),
                "order_type": self.get_comm_data("opt10075", record_name, i, "주문구분").strip(),  # +매수, -매도
                "order_quantity": int(self.get_comm_data("opt10075", record_name, i, "주문수량") or 0),
                "order_price": int(self.get_comm_data("opt10075", record_name, i, "주문가격") or 0),
                "not_executed": int(self.get_comm_data("opt10075", record_name, i, "미체결수량") or 0),
            }
            if order["not_executed"] > 0:
                orders.append(order)

        return orders

    def cancel_all_orders_for_stock(self, account, code):
        """
        특정 종목의 모든 미체결 주문 취소

        Args:
            account: 계좌번호
            code: 종목코드

        Returns:
            취소된 주문 수
        """
        orders = self.get_open_orders(account)
        cancelled_count = 0

        for order in orders:
            if order["code"] == code or order["code"] == code.replace("A", ""):
                # 매수 취소는 order_type=3, 매도 취소는 order_type=4
                cancel_type = 3 if "매수" in order["order_type"] or "+" in order["order_type"] else 4
                result = self.send_order(
                    "주문취소",
                    "0203",
                    account,
                    cancel_type,
                    order["code"],
                    order["not_executed"],
                    0,
                    "00",
                    order["order_no"]
                )
                if result == 0:
                    cancelled_count += 1

        return cancelled_count

    def cancel_buy_orders_for_stock(self, account, code):
        """
        특정 종목의 미체결 매수 주문만 취소 (조건 2-3)

        Args:
            account: 계좌번호
            code: 종목코드

        Returns:
            취소된 주문 수
        """
        orders = self.get_open_orders(account)
        cancelled_count = 0

        for order in orders:
            if order["code"] == code or order["code"] == code.replace("A", ""):
                # 매수 주문인 경우만 취소
                if "매수" in order["order_type"] or "+" in order["order_type"]:
                    result = self.send_order(
                        "주문취소",
                        "0203",
                        account,
                        3,  # 매수취소
                        order["code"],
                        order["not_executed"],
                        0,
                        "00",
                        order["order_no"]
                    )
                    if result == 0:
                        cancelled_count += 1

        return cancelled_count

    def cancel_sell_orders_for_stock(self, account, code):
        """
        특정 종목의 미체결 매도 주문만 취소

        Args:
            account: 계좌번호
            code: 종목코드

        Returns:
            취소된 주문 수
        """
        orders = self.get_open_orders(account)
        cancelled_count = 0

        for order in orders:
            if order["code"] == code or order["code"] == code.replace("A", ""):
                # 매도 주문인 경우만 취소
                if "매도" in order["order_type"] or "-" in order["order_type"]:
                    result = self.send_order(
                        "주문취소",
                        "0203",
                        account,
                        4,  # 매도취소
                        order["code"],
                        order["not_executed"],
                        0,
                        "00",
                        order["order_no"]
                    )
                    if result == 0:
                        cancelled_count += 1

        return cancelled_count

    def cancel_buy_orders_except_holdings(self, account, holding_codes):
        """
        보유종목을 제외한 모든 종목의 미체결 매수 주문 취소 (조건 8-2)

        Args:
            account: 계좌번호
            holding_codes: 보유 종목 코드 리스트

        Returns:
            취소된 주문 수
        """
        orders = self.get_open_orders(account)
        cancelled_count = 0

        # 보유 종목 코드를 정규화 (A 제거)
        normalized_holdings = set(c.replace("A", "") for c in holding_codes)

        for order in orders:
            order_code = order["code"].replace("A", "")

            # 매수 주문이고, 보유 종목이 아닌 경우만 취소
            if order_code not in normalized_holdings:
                if "매수" in order["order_type"] or "+" in order["order_type"]:
                    result = self.send_order(
                        "주문취소",
                        "0203",
                        account,
                        3,  # 매수취소
                        order["code"],
                        order["not_executed"],
                        0,
                        "00",
                        order["order_no"]
                    )
                    if result == 0:
                        cancelled_count += 1

        return cancelled_count

    # ==================== 당일 체결내역 조회 ====================
    def get_today_executions(self, account):
        """
        당일 체결내역 조회 (opt10085)

        Args:
            account: 계좌번호

        Returns:
            체결 내역 리스트: [{
                "code": 종목코드,
                "name": 종목명,
                "order_type": 주문구분 (매수/매도),
                "quantity": 체결수량,
                "price": 체결가격,
                "total_amount": 체결금액,
                "time": 체결시간,
                "order_no": 주문번호
            }]
        """
        self.set_input_value("계좌번호", account)
        self.set_input_value("비밀번호", "")
        self.set_input_value("비밀번호입력매체구분", "00")
        self.set_input_value("조회구분", "0")  # 0: 전체
        self.comm_rq_data("당일체결조회", "opt10085", 0, "0105")

        record_name = self._get_record_name("당일체결조회", "opt10085", "당일체결조회")
        executions = []
        repeat_cnt = self.get_repeat_cnt("opt10085", record_name)

        for i in range(repeat_cnt):
            execution = {
                "code": self.get_comm_data("opt10085", record_name, i, "종목코드").strip().replace("A", ""),
                "name": self.get_comm_data("opt10085", record_name, i, "종목명").strip(),
                "order_type": self.get_comm_data("opt10085", record_name, i, "매매구분").strip(),
                "quantity": abs(int(self.get_comm_data("opt10085", record_name, i, "체결수량") or 0)),
                "price": abs(int(self.get_comm_data("opt10085", record_name, i, "체결가") or 0)),
                "total_amount": abs(int(self.get_comm_data("opt10085", record_name, i, "체결금액") or 0)),
                "time": self.get_comm_data("opt10085", record_name, i, "체결시간").strip(),
                "order_no": self.get_comm_data("opt10085", record_name, i, "주문번호").strip(),
            }
            if execution["quantity"] > 0:
                executions.append(execution)

        return executions

    def get_today_executions_by_stock(self, account, code):
        """
        특정 종목의 당일 체결내역 조회

        Args:
            account: 계좌번호
            code: 종목코드

        Returns:
            해당 종목의 체결 내역 리스트
        """
        all_executions = self.get_today_executions(account)
        return [e for e in all_executions if e["code"] == code or e["code"] == code.replace("A", "")]

    # ==================== 실시간 데이터 ====================
    def set_real_reg(self, screen_no, code_list, fid_list, opt_type):
        """실시간 등록"""
        self.ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            screen_no, code_list, fid_list, opt_type
        )

    def set_real_remove(self, screen_no, code):
        """실시간 해제"""
        self.ocx.dynamicCall(
            "SetRealRemove(QString, QString)",
            screen_no, code
        )

    def _on_receive_real_data(self, code, real_type, real_data):
        """실시간 데이터 수신 이벤트"""
        if real_type == "주식체결":
            price = abs(int(self.get_comm_real_data(code, 10)))  # 현재가
            volume = int(self.get_comm_real_data(code, 15))  # 거래량

            # 이벤트 엔진으로 전달 (디바운스 적용됨)
            if self.event_engine:
                self.event_engine.push_event("price", code, {"price": price, "volume": volume})

            if self.on_real_data_callback:
                self.on_real_data_callback(code, price, volume)

    def get_comm_real_data(self, code, fid):
        """실시간 데이터 가져오기"""
        return self.ocx.dynamicCall(
            "GetCommRealData(QString, int)",
            code, fid
        )

    # ==================== 체결/잔고 ====================
    def _on_receive_chejan_data(self, gubun, item_cnt, fid_list):
        """체결/잔고 데이터 수신 이벤트"""
        if gubun == "0":  # 주문체결
            code = self.get_chejan_data(9001).strip().replace("A", "")
            order_status = self.get_chejan_data(913)  # 주문상태
            order_quantity = int(self.get_chejan_data(900) or 0)  # 주문수량
            executed_quantity = int(self.get_chejan_data(911) or 0)  # 체결수량
            executed_price = int(self.get_chejan_data(910) or 0)  # 체결가격
            order_type = self.get_chejan_data(905)  # 주문구분 (+매수, -매도)

            if self.on_chejan_callback:
                self.on_chejan_callback({
                    "type": "order",
                    "code": code,
                    "status": order_status,
                    "order_quantity": order_quantity,
                    "executed_quantity": executed_quantity,
                    "executed_price": executed_price,
                    "order_type": order_type
                })

        elif gubun == "1":  # 잔고변경
            code = self.get_chejan_data(9001).strip().replace("A", "")
            quantity = int(self.get_chejan_data(930) or 0)  # 보유수량
            avg_price = int(self.get_chejan_data(931) or 0)  # 평균단가

            # ✅ 시그널 발신: 잔고 변경
            try:
                self.account_signals.balance_changed.emit(code, quantity, avg_price)
            except Exception:
                pass

            if self.on_chejan_callback:
                self.on_chejan_callback({
                    "type": "balance",
                    "code": code,
                    "quantity": quantity,
                    "avg_price": avg_price
                })

    def get_chejan_data(self, fid):
        """체결 데이터 가져오기"""
        return self.ocx.dynamicCall("GetChejanData(int)", fid)

    # ==================== 메시지 ====================
    def _on_receive_msg(self, screen_no, rqname, trcode, msg):
        """메시지 수신 이벤트"""
        print(f"[메시지] {msg}")
        if self.on_message_callback:
            self.on_message_callback(screen_no, rqname, trcode, msg)

    # ==================== 콜백 설정 ====================
    def set_chejan_callback(self, callback):
        """체결 콜백 설정"""
        self.on_chejan_callback = callback

    def set_real_data_callback(self, callback):
        """실시간 데이터 콜백 설정"""
        self.on_real_data_callback = callback

    def set_message_callback(self, callback):
        """메시지 콜백 설정"""
        self.on_message_callback = callback


# 테스트 코드
if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom = KiwoomAPI()

    if kiwoom.login():
        print("로그인 성공!")
        accounts = kiwoom.get_account_list()
        print(f"계좌 목록: {accounts}")
    else:
        print("로그인 실패!")

    sys.exit(app.exec_())
