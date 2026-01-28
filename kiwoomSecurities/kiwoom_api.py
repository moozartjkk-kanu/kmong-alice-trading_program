# -*- coding: utf-8 -*-
"""
키움증권 Open API+ 연동 모듈
"""
import sys
import time
from collections import deque
from threading import Lock
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer, QObject, pyqtSignal
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

        # TR 호출 제한 (초당 5회)
        self.rate_limiter = RateLimiter(max_calls=5, period=1.0)

        # ✅ 계좌 데이터 시그널 발신기
        self.account_signals = AccountSignalEmitter()

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

    def set_event_engine(self, engine):
        """이벤트 엔진 설정"""
        self.event_engine = engine

    # ==================== 로그인 ====================
    def login(self):
        """로그인 요청"""
        self.ocx.dynamicCall("CommConnect()")
        self.login_event_loop = QEventLoop()
        self.login_event_loop.exec_()
        return self.connected

    def _on_event_connect(self, err_code):
        """로그인 이벤트 핸들러"""
        if err_code == 0:
            self.connected = True
            self.account_list = self.get_account_list()
            print(f"로그인 성공! 계좌: {self.account_list}")
        else:
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

    # ==================== TR 요청 ====================
    def set_input_value(self, id, value):
        """TR 입력값 설정"""
        self.ocx.dynamicCall("SetInputValue(QString, QString)", id, value)

    def comm_rq_data(self, rqname, trcode, next, screen_no):
        """TR 요청 (Rate Limiting 적용)"""
        # TR 호출 제한 대기
        self.rate_limiter.wait_if_needed()

        self.ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname, trcode, next, screen_no
        )
        self.tr_event_loop = QEventLoop()
        self.tr_event_loop.exec_()

    def _on_receive_tr_data(self, screen_no, rqname, trcode, record_name, next,
                            unused1, unused2, unused3, unused4):
        """TR 데이터 수신 이벤트 핸들러"""
        self.tr_data = {
            "screen_no": screen_no,
            "rqname": rqname,
            "trcode": trcode,
            "record_name": record_name,
            "next": next
        }

        if self.tr_event_loop:
            self.tr_event_loop.exit()

    def get_comm_data(self, trcode, rqname, index, item):
        """TR 데이터 가져오기"""
        data = self.ocx.dynamicCall(
            "GetCommData(QString, QString, int, QString)",
            trcode, rqname, index, item
        )
        return data.strip()

    def get_repeat_cnt(self, trcode, rqname):
        """반복 데이터 개수"""
        return self.ocx.dynamicCall(
            "GetRepeatCnt(QString, QString)",
            trcode, rqname
        )

    # ==================== 주식 기본 정보 ====================
    def get_master_code_name(self, code):
        """종목명 조회"""
        return self.ocx.dynamicCall("GetMasterCodeName(QString)", code)

    def get_stock_price(self, code):
        """현재가 조회 (opt10001)"""
        self.set_input_value("종목코드", code)
        self.comm_rq_data("주식기본정보", "opt10001", 0, "0101")

        price = self.get_comm_data("opt10001", "주식기본정보", 0, "현재가")
        return abs(int(price)) if price else 0

    def get_stock_info(self, code):
        """종목 기본 정보 조회"""
        self.set_input_value("종목코드", code)
        self.comm_rq_data("주식기본정보", "opt10001", 0, "0101")

        info = {
            "code": code,
            "name": self.get_comm_data("opt10001", "주식기본정보", 0, "종목명"),
            "price": abs(int(self.get_comm_data("opt10001", "주식기본정보", 0, "현재가") or 0)),
            "volume": int(self.get_comm_data("opt10001", "주식기본정보", 0, "거래량") or 0),
            "high": abs(int(self.get_comm_data("opt10001", "주식기본정보", 0, "고가") or 0)),
            "low": abs(int(self.get_comm_data("opt10001", "주식기본정보", 0, "저가") or 0)),
            "open": abs(int(self.get_comm_data("opt10001", "주식기본정보", 0, "시가") or 0)),
        }
        return info

    # ==================== 일봉 데이터 (이동평균 계산용) ====================
    def get_daily_candles(self, code, count=60):
        """일봉 데이터 조회 (opt10081)"""
        self.set_input_value("종목코드", code)
        self.set_input_value("기준일자", "")  # 오늘 기준
        self.set_input_value("수정주가구분", "1")
        self.comm_rq_data("일봉조회", "opt10081", 0, "0102")

        candles = []
        repeat_cnt = min(self.get_repeat_cnt("opt10081", "일봉조회"), count)

        for i in range(repeat_cnt):
            candle = {
                "date": self.get_comm_data("opt10081", "일봉조회", i, "일자"),
                "open": abs(int(self.get_comm_data("opt10081", "일봉조회", i, "시가") or 0)),
                "high": abs(int(self.get_comm_data("opt10081", "일봉조회", i, "고가") or 0)),
                "low": abs(int(self.get_comm_data("opt10081", "일봉조회", i, "저가") or 0)),
                "close": abs(int(self.get_comm_data("opt10081", "일봉조회", i, "현재가") or 0)),
                "volume": int(self.get_comm_data("opt10081", "일봉조회", i, "거래량") or 0),
            }
            candles.append(candle)

        return candles

    # ==================== 계좌 잔고 ====================
    def get_balance(self, account):
        """계좌 잔고 조회 (opw00018)"""
        self.set_input_value("계좌번호", account)
        self.set_input_value("비밀번호", "")
        self.set_input_value("비밀번호입력매체구분", "00")
        self.set_input_value("조회구분", "1")
        self.comm_rq_data("계좌잔고", "opw00018", 0, "0103")

        # 예수금 값 파싱 (빈 문자열이나 None 처리)
        def safe_int(val):
            if val is None or val.strip() == "":
                return 0
            try:
                return int(val.replace(",", "").replace("-", ""))
            except ValueError:
                return 0

        # ✅ 예수금 관련 필드들 조회
        # opw00018 싱글 데이터: rqname은 comm_rq_data에서 사용한 "계좌잔고"를 사용
        deposit = 0

        # 1차: D+2추정예수금 시도
        deposit_d2 = self.get_comm_data("opw00018", "계좌잔고", 0, "D+2추정예수금")
        deposit = safe_int(deposit_d2)

        # 2차: 예수금 시도
        if deposit == 0:
            deposit_raw = self.get_comm_data("opw00018", "계좌잔고", 0, "예수금")
            deposit = safe_int(deposit_raw)

        # 3차: 출금가능금액 시도
        if deposit == 0:
            deposit_available = self.get_comm_data("opw00018", "계좌잔고", 0, "출금가능금액")
            deposit = safe_int(deposit_available)

        # 4차: 추정예탁자산 시도
        if deposit == 0:
            deposit_estimate = self.get_comm_data("opw00018", "계좌잔고", 0, "추정예탁자산")
            deposit = safe_int(deposit_estimate)

        # 5차: 주문가능금액 시도
        if deposit == 0:
            deposit_order = self.get_comm_data("opw00018", "계좌잔고", 0, "주문가능금액")
            deposit = safe_int(deposit_order)

        # 싱글 데이터 필드 조회 (rqname = "계좌잔고")
        total_purchase = safe_int(self.get_comm_data("opw00018", "계좌잔고", 0, "총매입금액"))
        total_eval = safe_int(self.get_comm_data("opw00018", "계좌잔고", 0, "총평가금액"))
        total_profit = safe_int(self.get_comm_data("opw00018", "계좌잔고", 0, "총평가손익금액"))
        profit_rate_str = self.get_comm_data("opw00018", "계좌잔고", 0, "총수익률(%)")

        try:
            profit_rate = float(profit_rate_str) if profit_rate_str else 0.0
        except ValueError:
            profit_rate = 0.0

        balance = {
            "total_purchase": total_purchase,
            "total_eval": total_eval,
            "total_profit": total_profit,
            "profit_rate": profit_rate,
            "deposit": deposit,
        }

        # 보유 종목
        holdings = []
        repeat_cnt = self.get_repeat_cnt("opw00018", "계좌잔고")
        for i in range(repeat_cnt):
            holding = {
                "name": self.get_comm_data("opw00018", "계좌잔고", i, "종목명").strip(),
                "code": self.get_comm_data("opw00018", "계좌잔고", i, "종목번호").strip().replace("A", ""),
                "quantity": int(self.get_comm_data("opw00018", "계좌잔고", i, "보유수량") or 0),
                "avg_price": int(self.get_comm_data("opw00018", "계좌잔고", i, "매입가") or 0),
                "current_price": int(self.get_comm_data("opw00018", "계좌잔고", i, "현재가") or 0),
                "eval_amount": int(self.get_comm_data("opw00018", "계좌잔고", i, "평가금액") or 0),
                "profit": int(self.get_comm_data("opw00018", "계좌잔고", i, "평가손익") or 0),
                "profit_rate": float(self.get_comm_data("opw00018", "계좌잔고", i, "수익률(%)") or 0),
            }
            if holding["quantity"] > 0:
                holdings.append(holding)

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
        self.set_input_value("비밀번호", "")
        self.set_input_value("비밀번호입력매체구분", "00")
        self.set_input_value("조회구분", "2")  # 2: 일반조회
        self.comm_rq_data("예수금조회", "opw00001", 0, "0106")

        def safe_int(val):
            if val is None or val.strip() == "":
                return 0
            try:
                # 음수 값 처리 (마이너스 기호가 앞에 있을 수 있음)
                val = val.strip().replace(",", "")
                if val.startswith("-"):
                    return -int(val[1:])
                return int(val.replace("-", ""))
            except ValueError:
                return 0

        # rqname = "예수금조회" (comm_rq_data에서 사용한 이름)
        deposit_info = {
            "deposit": safe_int(self.get_comm_data("opw00001", "예수금조회", 0, "예수금")),
            "deposit_d1": safe_int(self.get_comm_data("opw00001", "예수금조회", 0, "D+1추정예수금")),
            "deposit_d2": safe_int(self.get_comm_data("opw00001", "예수금조회", 0, "D+2추정예수금")),
            "available": safe_int(self.get_comm_data("opw00001", "예수금조회", 0, "출금가능금액")),
            "order_available": safe_int(self.get_comm_data("opw00001", "예수금조회", 0, "주문가능금액")),
        }

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
            rqname, screen_no, account, order_type, code, quantity, price, hoga, org_order_no
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

        orders = []
        repeat_cnt = self.get_repeat_cnt("opt10075", "미체결조회")

        for i in range(repeat_cnt):
            order = {
                "order_no": self.get_comm_data("opt10075", "미체결조회", i, "주문번호").strip(),
                "code": self.get_comm_data("opt10075", "미체결조회", i, "종목코드").strip(),
                "name": self.get_comm_data("opt10075", "미체결조회", i, "종목명").strip(),
                "order_type": self.get_comm_data("opt10075", "미체결조회", i, "주문구분").strip(),  # +매수, -매도
                "order_quantity": int(self.get_comm_data("opt10075", "미체결조회", i, "주문수량") or 0),
                "order_price": int(self.get_comm_data("opt10075", "미체결조회", i, "주문가격") or 0),
                "not_executed": int(self.get_comm_data("opt10075", "미체결조회", i, "미체결수량") or 0),
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

        executions = []
        repeat_cnt = self.get_repeat_cnt("opt10085", "당일체결조회")

        for i in range(repeat_cnt):
            execution = {
                "code": self.get_comm_data("opt10085", "당일체결조회", i, "종목코드").strip().replace("A", ""),
                "name": self.get_comm_data("opt10085", "당일체결조회", i, "종목명").strip(),
                "order_type": self.get_comm_data("opt10085", "당일체결조회", i, "매매구분").strip(),
                "quantity": abs(int(self.get_comm_data("opt10085", "당일체결조회", i, "체결수량") or 0)),
                "price": abs(int(self.get_comm_data("opt10085", "당일체결조회", i, "체결가") or 0)),
                "total_amount": abs(int(self.get_comm_data("opt10085", "당일체결조회", i, "체결금액") or 0)),
                "time": self.get_comm_data("opt10085", "당일체결조회", i, "체결시간").strip(),
                "order_no": self.get_comm_data("opt10085", "당일체결조회", i, "주문번호").strip(),
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
