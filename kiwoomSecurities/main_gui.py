# -*- coding: utf-8 -*-
"""
키움증권 자동매매 시스템 - 메인 GUI
"""
import sys
from collections import deque
import time  # ✅ 추가: 로그 스팸/쓰로틀용

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTabWidget,
    QMessageBox, QHeaderView, QFrame, QGridLayout, QInputDialog
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QColor


class StockSearchWorker(QThread):
    """백그라운드 종목 검색 워커 (UI 프리징 방지)"""
    search_finished = pyqtSignal(list)  # [(code, name), ...]
    search_error = pyqtSignal(str)

    def __init__(self, kiwoom, search_text):
        super().__init__()
        self.kiwoom = kiwoom
        self.search_text = search_text

    def run(self):
        try:
            results = self.kiwoom.find_stocks_by_name(self.search_text)
            self.search_finished.emit(results)
        except Exception as e:
            self.search_error.emit(str(e))


class StockCacheLoaderWorker(QThread):
    """백그라운드 종목 캐시 로더 (로그인 직후 사용)"""
    load_finished = pyqtSignal(bool, int)  # (success, count)

    def __init__(self, kiwoom):
        super().__init__()
        self.kiwoom = kiwoom

    def run(self):
        try:
            success = self.kiwoom.load_stock_cache()
            count = len(self.kiwoom._stock_cache) if success else 0
            self.load_finished.emit(success, count)
        except Exception as e:
            print(f"[종목캐시] 로딩 오류: {e}")
            self.load_finished.emit(False, 0)

from config import Config
from kiwoom_api import KiwoomAPI
from trading_logic import AutoTrader
from technical_analysis import TechnicalAnalysis


class MainWindow(QMainWindow):
    """메인 윈도우"""

    def __init__(self):
        super().__init__()
        self.config = Config()
        self.kiwoom = None
        self.trader = None
        self.ta = TechnicalAnalysis()

        self.init_ui()

        # ✅ 로그 버퍼링 (UI 프리징 방지)
        self._log_buffer = deque(maxlen=5000)
        self._log_flush_timer = QTimer()
        self._log_flush_timer.timeout.connect(self._flush_log_buffer)
        self._log_flush_timer.start(200)

        # ✅ 주문 큐 처리 타이머 (주문은 메인(UI) 스레드에서만)
        self.order_timer = QTimer()
        self.order_timer.timeout.connect(self._drain_order_queue)
        # 자동매매 시작 시에만 start()


        # ✅ 타이머 겹침(중복 실행) 방지 플래그 (필수)
        self._is_refreshing_watchlist = False
        self._is_checking_signals = False

        # ✅ 정지 처리중 플래그 (필수: stop 중 교착/응답없음 방지)
        self._is_stopping = False

        # ✅ 로그 스팸 방지용 (권장)
        self._last_watchlist_log_ts = 0.0

        # ✅ 비동기 워치리스트 갱신용 큐
        self._watchlist_refresh_queue = []
        self._watchlist_refresh_period = 20
        self._watchlist_refresh_percent = 19

        # ✅ 워치리스트 헤더를 설정값으로 반영 (권장)
        self._update_watchlist_header()

        # 자동 갱신 타이머
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self.refresh_data)

        # 자동매매 체크 타이머
        self.trading_timer = QTimer()
        self.trading_timer.timeout.connect(self.check_trading_signals)

        # ✅ 장 열림 감지 타이머 (주문 복원 자동 호출용)
        self._orders_restored_today = False
        self._last_market_open_check_date = None
        self.market_open_timer = QTimer()
        self.market_open_timer.timeout.connect(self._check_market_open_and_restore)
        self.market_open_timer.start(60000)  # 1분마다 체크

        # ✅ 실시간 UI 업데이트용 행 매핑 (code → row)
        self._holdings_code_to_row = {}
        self._watchlist_code_to_row = {}

        # ✅ 잔고 변경 디바운스 타이머 (과도한 TR 호출 방지)
        self._balance_changed_timer = QTimer()
        self._balance_changed_timer.setSingleShot(True)
        self._balance_changed_timer.timeout.connect(self.refresh_holdings)

        # ✅ 백그라운드 검색 워커 (UI 프리징 방지)
        self._search_worker = None
        self._cache_loader_worker = None
        self._pending_search_callback = None

        # 초기 감시 종목 로드 (로그인 전에도 목록 표시)
        QTimer.singleShot(100, self._load_initial_watchlist)

    # =========================
    # 공용 유틸
    # =========================
    def _update_watchlist_header(self):
        """워치리스트 테이블 헤더에 현재 엔벨로프 설정값 반영 (권장)"""
        try:
            percent = self.config.get("buy", "envelope_percent") or 19
        except Exception:
            percent = 19

        headers = ["종목코드", "종목명", "현재가", "20일선", f"엔벨로프하단(-{percent}%)"]
        if hasattr(self, "watchlist_table") and self.watchlist_table is not None:
            self.watchlist_table.setHorizontalHeaderLabels(headers)

    def _fmt_int_or_dash(self, v):
        """None/비정상 값 포맷팅 안전 처리 (필수: 크래시 방지)"""
        try:
            if v is None:
                return "-"
            return f"{int(v):,}"
        except Exception:
            return "-"

    def _load_initial_watchlist(self):
        """프로그램 시작 시 저장된 감시 종목 목록 로드 (가격 정보 제외)"""
        watchlist = self.config.get_watchlist()
        self.watchlist_table.setRowCount(len(watchlist))

        self._watchlist_code_to_row = {}
        for row, stock in enumerate(watchlist):
            code = stock["code"]
            name = stock.get("name", "")
            self._watchlist_code_to_row[code] = row
            self.watchlist_table.setItem(row, 0, QTableWidgetItem(code))
            self.watchlist_table.setItem(row, 1, QTableWidgetItem(name))
            self.watchlist_table.setItem(row, 2, QTableWidgetItem("-"))
            self.watchlist_table.setItem(row, 3, QTableWidgetItem("-"))
            self.watchlist_table.setItem(row, 4, QTableWidgetItem("-"))

        if watchlist:
            self.log(f"[시스템] 저장된 감시 종목 {len(watchlist)}개 로드 완료")

    def init_ui(self):
        """UI 초기화"""
        self.setWindowTitle("키움증권 자동매매 시스템 - 엔벨로프 전략")
        self.setGeometry(100, 100, 1400, 900)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        top_frame = self.create_top_frame()
        main_layout.addWidget(top_frame)

        tab_widget = QTabWidget()
        main_layout.addWidget(tab_widget)

        trading_tab = self.create_trading_tab()
        tab_widget.addTab(trading_tab, "매매 관리")

        watchlist_tab = self.create_watchlist_tab()
        tab_widget.addTab(watchlist_tab, "종목 관리")

        settings_tab = self.create_settings_tab()
        tab_widget.addTab(settings_tab, "설정")

        log_frame = self.create_log_frame()
        main_layout.addWidget(log_frame)

    def create_top_frame(self):
        """상단 프레임 (연결 상태, 계좌 정보)"""
        frame = QFrame()
        frame.setFrameStyle(QFrame.StyledPanel)
        layout = QHBoxLayout(frame)

        self.status_label = QLabel("연결 상태: 미연결")
        self.status_label.setStyleSheet("color: red; font-weight: bold;")
        layout.addWidget(self.status_label)

        self.login_btn = QPushButton("로그인")
        self.login_btn.clicked.connect(self.do_login)
        layout.addWidget(self.login_btn)

        layout.addWidget(QLabel("  |  "))

        layout.addWidget(QLabel("계좌:"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(150)
        self.account_combo.currentTextChanged.connect(self.on_account_changed)
        layout.addWidget(self.account_combo)

        layout.addWidget(QLabel("  |  "))

        self.balance_label = QLabel("예수금: -")
        layout.addWidget(self.balance_label)

        layout.addStretch()

        self.auto_trade_btn = QPushButton("자동매매 시작")
        self.auto_trade_btn.setStyleSheet(
            "background-color: #4CAF50; color: white; font-weight: bold; padding: 10px 20px;"
        )
        self.auto_trade_btn.clicked.connect(self.toggle_auto_trade)
        self.auto_trade_btn.setEnabled(False)
        layout.addWidget(self.auto_trade_btn)

        return frame

    def create_trading_tab(self):
        """매매 관리 탭"""
        widget = QWidget()
        layout = QHBoxLayout(widget)

        left_group = QGroupBox("보유 종목 현황")
        left_layout = QVBoxLayout(left_group)

        self.holdings_table = QTableWidget()
        self.holdings_table.setColumnCount(8)
        self.holdings_table.setHorizontalHeaderLabels([
            "종목코드", "종목명", "보유수량", "평균단가", "현재가", "평가금액", "손익", "수익률"
        ])
        self.holdings_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.holdings_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.holdings_table.itemSelectionChanged.connect(self.on_holding_selected)
        left_layout.addWidget(self.holdings_table)

        refresh_btn = QPushButton("잔고 새로고침")
        refresh_btn.clicked.connect(self.refresh_holdings)
        left_layout.addWidget(refresh_btn)

        layout.addWidget(left_group, 2)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)

        analysis_group = QGroupBox("종목 분석")
        analysis_layout = QGridLayout(analysis_group)

        self.analysis_code_label = QLabel("종목: -")
        self.analysis_ma20_label = QLabel("20일선: -")
        self.analysis_envelope_label = QLabel("엔벨로프 하한: -")
        self.analysis_buy_signal_label = QLabel("매수 신호: -")
        self.analysis_position_label = QLabel("포지션: -")

        analysis_layout.addWidget(self.analysis_code_label, 0, 0, 1, 2)
        analysis_layout.addWidget(self.analysis_ma20_label, 1, 0)
        analysis_layout.addWidget(self.analysis_envelope_label, 1, 1)
        analysis_layout.addWidget(self.analysis_buy_signal_label, 2, 0, 1, 2)
        analysis_layout.addWidget(self.analysis_position_label, 3, 0, 1, 2)

        right_layout.addWidget(analysis_group)

        # 수동 매도
        sell_group = QGroupBox("수동 매도")
        sell_layout = QGridLayout(sell_group)

        sell_layout.addWidget(QLabel("종목코드/종목명:"), 0, 0)
        self.manual_sell_code = QLineEdit()
        self.manual_sell_code.setPlaceholderText("예: 005930, 삼성전자")
        sell_layout.addWidget(self.manual_sell_code, 0, 1)

        sell_layout.addWidget(QLabel("수량:"), 1, 0)
        self.manual_sell_qty = QSpinBox()
        self.manual_sell_qty.setRange(1, 999999)
        self.manual_sell_qty.setValue(1)
        sell_layout.addWidget(self.manual_sell_qty, 1, 1)

        sell_layout.addWidget(QLabel("비중(%):"), 2, 0)
        self.manual_sell_ratio = QSpinBox()
        self.manual_sell_ratio.setRange(1, 100)
        self.manual_sell_ratio.setValue(100)
        self.manual_sell_ratio.setSuffix(" %")
        self.manual_sell_ratio.valueChanged.connect(self.on_sell_ratio_changed)
        sell_layout.addWidget(self.manual_sell_ratio, 2, 1)

        sell_layout.addWidget(QLabel("가격:"), 3, 0)
        self.manual_sell_price = QSpinBox()
        self.manual_sell_price.setRange(0, 99999999)
        self.manual_sell_price.setValue(0)
        self.manual_sell_price.setSpecialValueText("시장가")
        sell_layout.addWidget(self.manual_sell_price, 3, 1)

        self.manual_sell_btn = QPushButton("매도 주문 (수량)")
        self.manual_sell_btn.setStyleSheet("background-color: #2196F3; color: white; font-weight: bold;")
        self.manual_sell_btn.clicked.connect(self.do_manual_sell)
        self.manual_sell_btn.setEnabled(False)
        sell_layout.addWidget(self.manual_sell_btn, 4, 0)

        self.manual_sell_ratio_btn = QPushButton("매도 주문 (비중)")
        self.manual_sell_ratio_btn.setStyleSheet("background-color: #9C27B0; color: white; font-weight: bold;")
        self.manual_sell_ratio_btn.clicked.connect(self.do_manual_sell_by_ratio)
        self.manual_sell_ratio_btn.setEnabled(False)
        sell_layout.addWidget(self.manual_sell_ratio_btn, 4, 1)

        self.cancel_orders_btn = QPushButton("전량주문 취소")
        self.cancel_orders_btn.setStyleSheet("background-color: #FF9800; color: white; font-weight: bold;")
        self.cancel_orders_btn.clicked.connect(self.do_cancel_all_orders)
        self.cancel_orders_btn.setEnabled(False)
        sell_layout.addWidget(self.cancel_orders_btn, 5, 0, 1, 2)

        right_layout.addWidget(sell_group)

        # 수동 매수
        buy_group = QGroupBox("수동 매수")
        buy_layout = QGridLayout(buy_group)

        buy_layout.addWidget(QLabel("종목코드/종목명:"), 0, 0)
        self.manual_buy_code = QLineEdit()
        self.manual_buy_code.setPlaceholderText("예: 005930, 삼성전자")
        buy_layout.addWidget(self.manual_buy_code, 0, 1)

        buy_layout.addWidget(QLabel("수량:"), 1, 0)
        self.manual_buy_qty = QSpinBox()
        self.manual_buy_qty.setRange(1, 999999)
        self.manual_buy_qty.setValue(1)
        buy_layout.addWidget(self.manual_buy_qty, 1, 1)

        buy_layout.addWidget(QLabel("가격:"), 2, 0)
        self.manual_buy_price = QSpinBox()
        self.manual_buy_price.setRange(0, 99999999)
        self.manual_buy_price.setValue(0)
        self.manual_buy_price.setSpecialValueText("시장가")
        buy_layout.addWidget(self.manual_buy_price, 2, 1)

        self.manual_buy_btn = QPushButton("매수 주문")
        self.manual_buy_btn.setStyleSheet("background-color: #f44336; color: white; font-weight: bold;")
        self.manual_buy_btn.clicked.connect(self.do_manual_buy)
        self.manual_buy_btn.setEnabled(False)
        buy_layout.addWidget(self.manual_buy_btn, 3, 0, 1, 2)

        right_layout.addWidget(buy_group)

        # 매도 목표가
        target_group = QGroupBox("매도 목표가 (선택 종목)")
        target_layout = QVBoxLayout(target_group)

        self.sell_targets_table = QTableWidget()
        self.sell_targets_table.setColumnCount(3)
        self.sell_targets_table.setHorizontalHeaderLabels(["구분", "목표가", "상태"])
        self.sell_targets_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        target_layout.addWidget(self.sell_targets_table)

        right_layout.addWidget(target_group)

        right_layout.addStretch()
        layout.addWidget(right_widget, 1)

        return widget

    def create_watchlist_tab(self):
        """종목 관리 탭"""
        widget = QWidget()
        layout = QHBoxLayout(widget)

        left_group = QGroupBox("감시 종목 리스트 (자동매매 대상)")
        left_layout = QVBoxLayout(left_group)

        self.watchlist_table = QTableWidget()
        self.watchlist_table.setColumnCount(5)
        self.watchlist_table.setHorizontalHeaderLabels([
            "종목코드", "종목명", "현재가", "20일선", "엔벨로프하단(-19%)"
        ])
        self.watchlist_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.watchlist_table.setSelectionBehavior(QTableWidget.SelectRows)
        left_layout.addWidget(self.watchlist_table)

        add_layout = QHBoxLayout()
        self.add_code_input = QLineEdit()
        self.add_code_input.setPlaceholderText("종목코드 또는 종목명 입력 (예: 005930, 삼성전자)")
        add_layout.addWidget(self.add_code_input)

        add_btn = QPushButton("종목 추가")
        add_btn.clicked.connect(self.add_to_watchlist)
        add_layout.addWidget(add_btn)

        remove_btn = QPushButton("선택 삭제")
        remove_btn.clicked.connect(self.remove_from_watchlist)
        add_layout.addWidget(remove_btn)

        left_layout.addLayout(add_layout)

        refresh_watchlist_btn = QPushButton("종목 정보 새로고침")
        refresh_watchlist_btn.clicked.connect(self.refresh_watchlist)
        left_layout.addWidget(refresh_watchlist_btn)

        layout.addWidget(left_group)
        return widget

    def create_settings_tab(self):
        """설정 탭"""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        buy_group = QGroupBox("매수 설정")
        buy_layout = QGridLayout(buy_group)

        buy_layout.addWidget(QLabel("엔벨로프 기간:"), 0, 0)
        self.setting_envelope_period = QSpinBox()
        self.setting_envelope_period.setRange(5, 60)
        self.setting_envelope_period.setValue(self.config.get("buy", "envelope_period"))
        buy_layout.addWidget(self.setting_envelope_period, 0, 1)

        buy_layout.addWidget(QLabel("트리거 % (하단):"), 0, 2)
        self.setting_envelope_percent = QSpinBox()
        self.setting_envelope_percent.setRange(5, 50)
        self.setting_envelope_percent.setValue(self.config.get("buy", "envelope_percent") or 19)
        self.setting_envelope_percent.setToolTip("매수 신호 트리거 조건 (예: 19% = MA 대비 -19%에서 트리거)")
        buy_layout.addWidget(self.setting_envelope_percent, 0, 3)

        buy_layout.addWidget(QLabel("매수가 % (하단):"), 1, 0)
        self.setting_envelope_buy_percent = QSpinBox()
        self.setting_envelope_buy_percent.setRange(5, 50)
        self.setting_envelope_buy_percent.setValue(self.config.get("buy", "envelope_buy_percent") or 20)
        self.setting_envelope_buy_percent.setToolTip("실제 지정가 매수 주문 가격 (예: 20% = MA × 0.80 + 1호가)")
        buy_layout.addWidget(self.setting_envelope_buy_percent, 1, 1)

        buy_layout.addWidget(QLabel("추가매수 하락률 %:"), 1, 2)
        self.setting_add_drop = QSpinBox()
        self.setting_add_drop.setRange(5, 30)
        self.setting_add_drop.setValue(self.config.get("buy", "additional_buy_drop_percent"))
        buy_layout.addWidget(self.setting_add_drop, 1, 3)

        buy_layout.addWidget(QLabel("1회 매수 금액:"), 2, 0)
        self.setting_buy_amount = QSpinBox()
        self.setting_buy_amount.setRange(100000, 100000000)
        self.setting_buy_amount.setSingleStep(100000)
        self.setting_buy_amount.setValue(self.config.get("buy", "buy_amount_per_stock"))
        self.setting_buy_amount.setSuffix(" 원")
        buy_layout.addWidget(self.setting_buy_amount, 2, 1)

        buy_layout.addWidget(QLabel("최대 동시 보유 종목수:"), 2, 2)
        self.setting_max_holding = QSpinBox()
        self.setting_max_holding.setRange(1, 50)
        self.setting_max_holding.setValue(self.config.get("buy", "max_holding_stocks") or 3)
        self.setting_max_holding.setSuffix(" 종목")
        buy_layout.addWidget(self.setting_max_holding, 2, 3)

        # 재진입 허용 체크박스 삭제됨 - 매도 발생 시 당일 재매수 금지

        layout.addWidget(buy_group)

        sell_group = QGroupBox("매도 설정")
        sell_layout = QGridLayout(sell_group)

        sell_layout.addWidget(QLabel("익절 1 (수익률 %):"), 0, 0)
        self.setting_profit1 = QDoubleSpinBox()
        self.setting_profit1.setRange(0.1, 50)
        self.setting_profit1.setDecimals(2)
        self.setting_profit1.setValue(self.config.get("sell", "profit_targets")[0])
        sell_layout.addWidget(self.setting_profit1, 0, 1)

        sell_layout.addWidget(QLabel("매도 비중 %:"), 0, 2)
        self.setting_ratio1 = QSpinBox()
        self.setting_ratio1.setRange(1, 100)
        self.setting_ratio1.setValue(self.config.get("sell", "profit_sell_ratios")[0])
        sell_layout.addWidget(self.setting_ratio1, 0, 3)

        sell_layout.addWidget(QLabel("익절 2 (수익률 %):"), 1, 0)
        self.setting_profit2 = QDoubleSpinBox()
        self.setting_profit2.setRange(0.1, 50)
        self.setting_profit2.setDecimals(2)
        self.setting_profit2.setValue(self.config.get("sell", "profit_targets")[1])
        sell_layout.addWidget(self.setting_profit2, 1, 1)

        sell_layout.addWidget(QLabel("매도 비중 %:"), 1, 2)
        self.setting_ratio2 = QSpinBox()
        self.setting_ratio2.setRange(1, 100)
        self.setting_ratio2.setValue(self.config.get("sell", "profit_sell_ratios")[1])
        sell_layout.addWidget(self.setting_ratio2, 1, 3)

        sell_layout.addWidget(QLabel("익절 3 (수익률 %):"), 2, 0)
        self.setting_profit3 = QDoubleSpinBox()
        self.setting_profit3.setRange(0.1, 50)
        self.setting_profit3.setDecimals(2)
        self.setting_profit3.setValue(self.config.get("sell", "profit_targets")[2])
        sell_layout.addWidget(self.setting_profit3, 2, 1)

        sell_layout.addWidget(QLabel("매도 비중 %:"), 2, 2)
        self.setting_ratio3 = QSpinBox()
        self.setting_ratio3.setRange(1, 100)
        self.setting_ratio3.setValue(self.config.get("sell", "profit_sell_ratios")[2])
        sell_layout.addWidget(self.setting_ratio3, 2, 3)

        sell_layout.addWidget(QLabel("20일선 도달시 매도 비중 %:"), 3, 0, 1, 2)
        self.setting_ma20_ratio = QSpinBox()
        self.setting_ma20_ratio.setRange(1, 100)
        self.setting_ma20_ratio.setValue(self.config.get("sell", "ma20_sell_ratio"))
        sell_layout.addWidget(self.setting_ma20_ratio, 3, 2, 1, 2)

        layout.addWidget(sell_group)

        # 모의투자 체크박스 삭제됨 - 로그인 시 서버 자동 감지

        save_btn = QPushButton("설정 저장")
        save_btn.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 10px;")
        save_btn.clicked.connect(self.save_settings)
        layout.addWidget(save_btn)

        layout.addStretch()
        return widget

    def create_log_frame(self):
        """로그 프레임"""
        group = QGroupBox("로그")
        layout = QVBoxLayout(group)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        self.log_text.setFont(QFont("Consolas", 9))
        layout.addWidget(self.log_text)

        clear_btn = QPushButton("로그 지우기")
        clear_btn.clicked.connect(lambda: self.log_text.clear())
        layout.addWidget(clear_btn)

        return group

    def log(self, message):
        """로그 출력(버퍼링)"""
        try:
            self._log_buffer.append(str(message))
        except Exception:
            if hasattr(self, 'log_text') and self.log_text is not None:
                self.log_text.append(str(message))
                self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    
    def _flush_log_buffer(self):
        """로그 버퍼를 주기적으로 UI에 반영"""
        if not hasattr(self, "log_text") or self.log_text is None:
            return
        if not hasattr(self, "_log_buffer") or not self._log_buffer:
            return

        batch = 0
        while self._log_buffer and batch < 300:
            self.log_text.append(self._log_buffer.popleft())
            batch += 1

        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def _drain_order_queue(self):
        """주문 큐를 메인(UI) 스레드에서 처리"""
        if self._is_stopping:
            return
        if not self.trader or not getattr(self.trader, "is_running", False):
            return
        # ✅ TR 재진입 방지: TR 또는 TR 큐 처리 중이면 스킵
        if self.kiwoom and self.kiwoom.is_tr_queue_busy():
            return
        try:
            self.trader.process_order_queue()
        except Exception as e:
            self.log(f"[시스템] 주문 큐 처리 오류: {e}")

    # =========================
    # 종목 캐시 로딩 (UI 프리징 방지)
    # =========================
    def _start_stock_cache_loading(self):
        """백그라운드에서 종목 캐시 로딩 시작"""
        if not self.kiwoom:
            return

        self.log("[시스템] 종목 캐시 로딩 시작...")
        self._cache_loader_worker = StockCacheLoaderWorker(self.kiwoom)
        self._cache_loader_worker.load_finished.connect(self._on_cache_load_finished)
        self._cache_loader_worker.start()

    def _on_cache_load_finished(self, success, count):
        """종목 캐시 로딩 완료 콜백"""
        if success:
            self.log(f"[시스템] 종목 캐시 로딩 완료: {count}개 종목")
        else:
            self.log("[시스템] 종목 캐시 로딩 실패 - 검색 시 API 호출 사용")

    # =========================
    # 로그인 / 계좌
    # =========================
    def do_login(self):
        """로그인"""
        try:
            self.log("[시스템] 키움증권 로그인 시도 중...")
            self.kiwoom = KiwoomAPI()

            if self.kiwoom.login():
                # ✅ 서버 구분 확인 (실서버/모의투자)
                server_gubun = self.kiwoom.get_server_gubun()
                is_real = self.kiwoom.is_real_server()

                self.log(f"[시스템] 로그인 성공! (서버: {server_gubun})")

                # ✅ 서버 구분에 따라 UI 색상 및 텍스트 변경
                if is_real:
                    self.status_label.setText(f"연결 상태: 연결됨 (실서버)")
                    self.status_label.setStyleSheet("color: green; font-weight: bold;")
                else:
                    self.status_label.setText(f"연결 상태: 연결됨 (모의투자)")
                    self.status_label.setStyleSheet("color: orange; font-weight: bold;")
                    # 모의투자 연결 시 경고 메시지
                    self.log("[경고] 모의투자 서버에 연결되었습니다!")
                    self.log("[안내] 실계좌 연결 방법:")
                    self.log("  1. 영웅문HTS 자동로그인을 해제하세요.")
                    self.log("  2. KOA Studio를 재실행하고 로그인 창에서 '모의투자' 체크 해제")
                    self.log("  3. 계좌 비밀번호를 KOA Studio에서 다시 등록하세요.")
                    QMessageBox.warning(
                        self,
                        "모의투자 서버 연결",
                        "현재 모의투자 서버에 연결되었습니다.\n\n"
                        "실계좌를 사용하려면:\n"
                        "1. 영웅문HTS 자동로그인을 해제\n"
                        "2. KOA Studio 재실행 후 로그인 시 '모의투자' 체크 해제\n"
                        "3. 계좌 비밀번호 다시 등록"
                    )

                self.login_btn.setEnabled(False)

                # ✅ 디버그 모드 활성화 (문제 해결 후 False로 변경)
                self.kiwoom.set_debug(True)

                # ✅ 계좌 데이터 시그널 연결
                self.kiwoom.account_signals.deposit_changed.connect(self._on_deposit_changed)
                self.kiwoom.account_signals.balance_changed.connect(self._on_balance_changed)
                self.kiwoom.account_signals.full_balance_updated.connect(self._on_full_balance_updated)
                self.kiwoom.account_signals.holdings_updated.connect(self._on_holdings_updated)

                # ✅ 실시간 시세 콜백 등록 (보유종목/감시종목 UI 실시간 갱신)
                self.kiwoom.set_real_data_callback(self._on_realtime_price)

                # ✅ 종목 캐시 로드 (UI 프리징 방지 - 백그라운드 스레드에서 실행)
                self._start_stock_cache_loading()

                self.trader = AutoTrader(self.kiwoom, self.config)
                self.trader.set_log_callback(self.log)

                accounts = self.kiwoom.get_account_list() or []
                accounts = [a.strip() for a in accounts if a and a.strip()]

                self.account_combo.blockSignals(True)
                self.account_combo.clear()
                self.account_combo.addItems(accounts)
                self.account_combo.blockSignals(False)

                if not accounts:
                    self.log("[시스템] 계좌 목록이 비어있습니다. 자동매매를 사용할 수 없습니다.")
                    QMessageBox.warning(self, "계좌 없음", "계좌를 불러오지 못했습니다. (실/모의 설정 확인)")
                    self.auto_trade_btn.setEnabled(False)
                    return

                self.account_combo.setCurrentIndex(0)
                self.trader.set_account(accounts[0])
                self.log(f"[시스템] 계좌 설정: {accounts[0]}")

                self.auto_trade_btn.setEnabled(True)
                self.manual_sell_btn.setEnabled(True)
                self.manual_sell_ratio_btn.setEnabled(True)
                self.manual_buy_btn.setEnabled(True)
                self.cancel_orders_btn.setEnabled(True)

                self.refresh_timer.start(60000)
                self.refresh_data()

                self.trader.full_state_sync_on_startup()
                self._check_pending_orders_on_startup()

            else:
                self.log("[시스템] 로그인 실패!")
                QMessageBox.warning(self, "로그인 실패", "키움증권 로그인에 실패했습니다.")

        except Exception as e:
            self.log(f"[시스템] 로그인 오류: {e}")
            QMessageBox.critical(self, "오류", f"로그인 중 오류 발생: {e}")

    def on_account_changed(self, account):
        """계좌 변경"""
        if not account or not account.strip():
            self.auto_trade_btn.setEnabled(False)
            return

        if self.trader:
            self.trader.set_account(account.strip())
            self.auto_trade_btn.setEnabled(True)
            self.refresh_holdings()

    # =========================
    # 자동매매
    # =========================
    def toggle_auto_trade(self):
        """자동매매 시작/중지"""
        if not self.trader:
            return

        if not self.account_combo.currentText().strip():
            QMessageBox.warning(self, "오류", "유효한 계좌를 선택해주세요.")
            return

        # ✅ 정지 처리중이면 연타 방지
        if self._is_stopping:
            return

        if self.trader.is_running:
            # ✅ 1) 먼저 타이머를 멈춰 재진입/교착 가능성 최소화
            self._is_stopping = True
            self.trading_timer.stop()
            self.refresh_timer.stop()  # (권장) stop 중 갱신도 멈춰서 UI 프리징/경합 줄임
            # ✅ 주문 큐 타이머도 정지
            try:
                self.order_timer.stop()
            except Exception:
                pass

            # ✅ 2) 버튼을 즉시 바꿔서 UI 반응 확보
            self.auto_trade_btn.setEnabled(False)
            self.auto_trade_btn.setText("정지 처리중...")
            self.auto_trade_btn.setStyleSheet(
                "background-color: #9E9E9E; color: white; font-weight: bold; padding: 10px 20px;"
            )

            # ✅ 3) 실제 stop/save는 다음 이벤트 루프에서 실행 (UI 프리징 완화)
            QTimer.singleShot(0, self._stop_autotrade_async)
            return

        # 시작
        ok = False
        try:
            ok = self.trader.start()
        except Exception as e:
            self.log(f"[시스템] 자동매매 시작 오류: {e}")
            QMessageBox.critical(self, "오류", f"자동매매 시작 중 오류: {e}")
            return

        if ok:
            self.auto_trade_btn.setText("자동매매 중지")
            self.auto_trade_btn.setStyleSheet(
                "background-color: #f44336; color: white; font-weight: bold; padding: 10px 20px;"
            )

            # (권장) 시작 직후에도 stop을 눌렀을 때 대비해 플래그 리셋
            self._is_stopping = False

            # ✅ 블로킹 작업들을 비동기로 처리하여 UI 프리징 방지
            QTimer.singleShot(100, self._start_autotrade_async)

            # ✅ 주문 큐 타이머 시작 (주문은 메인 스레드에서만)
            try:
                self.order_timer.start(100)
            except Exception:
                pass

            self.trading_timer.start(30000)
            self.refresh_timer.start(60000)  # 혹시 꺼져있었으면 다시 켬
        else:
            self.log("[시스템] 자동매매 시작 실패 (AutoTrader.start()가 False 반환)")
            QMessageBox.warning(self, "시작 실패", "자동매매 시작에 실패했습니다. 로그를 확인해주세요.")

    def _start_autotrade_async(self):
        """자동매매 시작 후 블로킹 작업을 비동기로 처리"""
        if self._is_stopping or not self.trader or not self.trader.is_running:
            return

        try:
            self.trader.clear_stale_pending_orders()
        except Exception as e:
            self.log(f"[시스템] 미체결 주문 정리 오류: {e}")

        # 주문 복원은 별도 타이머로 처리 (UI 프리징 방지)
        QTimer.singleShot(500, self._restore_orders_async)

    def _restore_orders_async(self):
        """주문 복원을 비동기로 처리"""
        if self._is_stopping or not self.trader or not self.trader.is_running:
            return

        # ✅ 장이 열렸을 때만 복원 실행 (장 전이면 장 열림 타이머가 처리)
        if not self.trader.is_market_open():
            self.log("[시스템] 장 시간이 아니어서 주문 복원 대기 - 장 열림 시 자동 복원됨")
            return

        try:
            self.trader.check_and_restore_orders()
            self._orders_restored_today = True  # ✅ 복원 완료 표시
        except Exception as e:
            self.log(f"[시스템] 주문 복원 오류: {e}")

    def _check_market_open_and_restore(self):
        """
        장 열림 감지 시 check_and_restore_orders() 자동 호출
        - 매 1분마다 체크
        - 날짜 변경 시 플래그 초기화
        - 장이 열렸고 아직 복원하지 않았으면 주문 복원 실행
        """
        from datetime import datetime, time as dt_time

        now = datetime.now()
        today = now.date()

        # 날짜가 변경되면 플래그 초기화
        if self._last_market_open_check_date != today:
            self._last_market_open_check_date = today
            self._orders_restored_today = False

        # trader가 없거나 실행 중이 아니면 스킵
        if not self.trader or not self.trader.is_running:
            return

        # 정규장 시간 체크 (09:00 ~ 15:30)
        current_time = now.time()
        market_open = dt_time(9, 0)
        market_close = dt_time(15, 30)

        if not (market_open <= current_time <= market_close):
            return

        # ✅ TR 재진입 방지: TR 또는 TR 큐 처리 중이면 스킵
        if self.kiwoom and self.kiwoom.is_tr_queue_busy():
            return

        # ✅ 이미 복원했어도 미체결 주문이 없으면 다시 복원 시도
        if self._orders_restored_today:
            try:
                # 실제 미체결 주문 확인
                api_pending = self.trader.kiwoom.get_open_orders(self.trader.account)
                pending_orders = self.trader.config.get_pending_orders()

                if api_pending or not pending_orders:
                    # 미체결 주문이 있거나 복원할 주문이 없으면 스킵
                    return
                else:
                    # 미체결 주문이 없고 복원할 주문이 있으면 다시 복원
                    self.log("[시스템] 기존 주문 취소 감지 - 주문 재복원 시작")
                    self._orders_restored_today = False
            except Exception as e:
                self.log(f"[시스템] 미체결 주문 확인 오류: {e}")
                return

        # 주문 복원 실행
        self.log("[시스템] 정규장 시간 - 주문 복원 자동 실행")
        try:
            self.trader.check_and_restore_orders()
            self._orders_restored_today = True
        except Exception as e:
            self.log(f"[시스템] 장 열림 주문 복원 오류: {e}")

    def _stop_autotrade_async(self):
        """UI 프리징을 줄이기 위해 stop/save를 이벤트루프 다음 tick에서 수행"""
        t0 = time.time()
        try:
            self.log("[시스템] 자동매매 정지 시작...")

            # ✅ stop이 오래 걸릴 수 있으니, 먼저 running 플래그를 내려서 루프가 빨리 멈추게 유도
            # (가능하면 AutoTrader.stop() 내부에서 처리하는 게 더 좋음)
            try:
                self.trader.is_running = False
            except Exception:
                pass

            self.trader.stop()
            self.log(f"[DBG] trader.stop() 완료 ({time.time() - t0:.2f}s)")

            t1 = time.time()
            self.trader.save_current_state()
            self.log(f"[DBG] save_current_state() 완료 ({time.time() - t1:.2f}s)")

            self.log("[시스템] 자동매매 중지 완료")
        except Exception as e:
            self.log(f"[시스템] 자동매매 정지 오류: {e}")
        finally:
            self._is_stopping = False

            # UI 복구
            self.auto_trade_btn.setEnabled(True)
            self.auto_trade_btn.setText("자동매매 시작")
            self.auto_trade_btn.setStyleSheet(
                "background-color: #4CAF50; color: white; font-weight: bold; padding: 10px 20px;"
            )

            # (선택) 정지 후에는 잔고/워치리스트는 계속 갱신되게
            if self.kiwoom and self.kiwoom.is_connected():
                self.refresh_timer.start(60000)

    def check_trading_signals(self):
        """
        매매 신호 확인 (순환 조회용)
        - 실시간 등록 종목: 이벤트 엔진에서 자동 처리
        - 실시간 미등록 종목: 이 타이머로 순환 조회
        """
        if self._is_stopping:
            return

        if not self.trader or not self.trader.is_running:
            return

        if self._is_checking_signals:
            return

        # ✅ TR 재진입 방지: TR 또는 TR 큐 처리 중이면 스킵 (다음 타이머 주기에 재시도)
        if self.kiwoom and self.kiwoom.is_tr_queue_busy():
            return

        self._is_checking_signals = True

        try:
            if self.trader.event_engine:
                watchlist = self.config.get_watchlist()
                watchlist_codes = [item["code"] for item in watchlist]
                unregistered = self.trader.event_engine.realtime_manager.get_unregistered_stocks(watchlist_codes)

                for code in unregistered[:5]:
                    if self._is_stopping or not self.trader.is_running:
                        break
                    self.trader.check_and_trade(code)
            else:
                watchlist = self.config.get_watchlist()
                for stock in watchlist:
                    if self._is_stopping or not self.trader.is_running:
                        break
                    self.trader.check_and_trade(stock["code"])
        finally:
            self._is_checking_signals = False

    # =========================
    # 계좌 데이터 시그널 슬롯
    # =========================
    def _on_deposit_changed(self, deposit):
        """예수금 변경 시그널 처리"""
        try:
            self.balance_label.setText(f"예수금: {deposit:,}원")
        except Exception:
            pass

    def _on_balance_changed(self, _code, _quantity, _avg_price):
        """잔고 변경 시그널 처리 (개별 종목) - 디바운스 적용으로 과도한 TR 호출 방지"""
        try:
            # ✅ 1초 디바운스: 연속 체결 시 마지막 이벤트 후 1초 뒤에 한 번만 TR 조회
            self._balance_changed_timer.start(1000)
        except Exception:
            pass

    def _on_full_balance_updated(self, balance):
        """전체 잔고 정보 갱신 시그널 처리"""
        try:
            deposit = balance.get("deposit", 0) or 0
            if deposit > 0:
                self.balance_label.setText(f"예수금: {deposit:,}원")
        except Exception:
            pass

    def _on_holdings_updated(self, holdings):
        """보유종목 전체 갱신 시그널 처리"""
        try:
            self.holdings_table.setRowCount(len(holdings))
            self._holdings_code_to_row = {}

            for row, holding in enumerate(holdings):
                code = holding["code"]
                self._holdings_code_to_row[code] = row
                self.holdings_table.setItem(row, 0, QTableWidgetItem(code))
                self.holdings_table.setItem(row, 1, QTableWidgetItem(holding["name"]))
                self.holdings_table.setItem(row, 2, QTableWidgetItem(f"{holding['quantity']:,}"))
                self.holdings_table.setItem(row, 3, QTableWidgetItem(f"{holding['avg_price']:,}"))
                self.holdings_table.setItem(row, 4, QTableWidgetItem(f"{holding['current_price']:,}"))
                self.holdings_table.setItem(row, 5, QTableWidgetItem(f"{holding['eval_amount']:,}"))
                self.holdings_table.setItem(row, 6, QTableWidgetItem(f"{holding['profit']:,}"))

                profit_rate = holding.get("profit_rate", 0.0) or 0.0
                rate_item = QTableWidgetItem(f"{profit_rate:.2f}%")
                if profit_rate > 0:
                    rate_item.setForeground(QColor("red"))
                elif profit_rate < 0:
                    rate_item.setForeground(QColor("blue"))
                self.holdings_table.setItem(row, 7, rate_item)
        except Exception:
            pass

    # =========================
    # 실시간 시세 UI 반영
    # =========================
    def _on_realtime_price(self, code, price, volume):
        """실시간 시세 콜백 → 보유종목/감시종목 테이블 즉시 갱신"""
        try:
            self._update_holdings_realtime(code, price)
        except Exception:
            pass
        try:
            self._update_watchlist_realtime(code, price)
        except Exception:
            pass

    def _update_holdings_realtime(self, code, price):
        """보유종목 테이블에서 해당 종목의 현재가·평가금액·손익·수익률 실시간 갱신"""
        row = self._holdings_code_to_row.get(code)
        if row is None or row >= self.holdings_table.rowCount():
            return

        # 보유수량·평균단가는 테이블에서 읽기
        qty_item = self.holdings_table.item(row, 2)
        avg_item = self.holdings_table.item(row, 3)
        if not qty_item or not avg_item:
            return

        try:
            quantity = int(qty_item.text().replace(",", ""))
            avg_price = int(avg_item.text().replace(",", ""))
        except (ValueError, AttributeError):
            return

        if quantity <= 0 or avg_price <= 0:
            return

        eval_amount = price * quantity
        profit = eval_amount - (avg_price * quantity)
        profit_rate = ((price - avg_price) / avg_price) * 100

        self.holdings_table.setItem(row, 4, QTableWidgetItem(f"{price:,}"))
        self.holdings_table.setItem(row, 5, QTableWidgetItem(f"{eval_amount:,}"))
        self.holdings_table.setItem(row, 6, QTableWidgetItem(f"{profit:,}"))

        rate_item = QTableWidgetItem(f"{profit_rate:.2f}%")
        if profit_rate > 0:
            rate_item.setForeground(QColor("red"))
        elif profit_rate < 0:
            rate_item.setForeground(QColor("blue"))
        self.holdings_table.setItem(row, 7, rate_item)

    def _update_watchlist_realtime(self, code, price):
        """감시종목 테이블에서 해당 종목의 현재가 실시간 갱신"""
        row = self._watchlist_code_to_row.get(code)
        if row is None or row >= self.watchlist_table.rowCount():
            return
        self.watchlist_table.setItem(row, 2, QTableWidgetItem(f"{price:,}"))

    # =========================
    # 데이터 갱신
    # =========================
    def refresh_data(self):
        """데이터 갱신 (TR 큐 기반으로 순차 처리됨)"""
        if self._is_stopping:
            return
        # ✅ TR 큐가 순차 처리하므로 별도 지연 없이 요청 추가
        self.refresh_holdings()
        self.refresh_watchlist()

    def refresh_holdings(self):
        """보유 종목 갱신 (큐 기반 비동기)"""
        if not self.kiwoom or not self.kiwoom.is_connected():
            return

        account = self.account_combo.currentText().strip()
        if not account:
            return

        # ✅ TR 큐에 잔고 조회 요청 추가 (중첩 호출 방지)
        self.log(f"[잔고조회] 계좌번호: {account} (큐 기반 요청)")
        self.kiwoom.get_balance_async(account, self._on_balance_received)

    def _on_balance_received(self, balance):
        """잔고 조회 결과 콜백 (큐에서 호출)"""
        if balance is None:
            self.log("[잔고조회] 조회 실패")
            return

        try:
            account = self.account_combo.currentText().strip()
            deposit = balance.get("deposit", 0) or 0
            holdings = balance.get("holdings", [])
            total_eval = balance.get("total_eval", 0) or 0
            self.log(f"[잔고조회] 예수금={deposit:,}원, 총평가={total_eval:,}원, 보유종목={len(holdings)}개")

            # ✅ 예수금이 0이면 opw00001 TR로 재조회 시도 (큐 기반)
            if deposit == 0 and account:
                self.log(f"[잔고조회] opw00018 예수금=0, opw00001로 재조회 요청...")
                self.kiwoom.get_deposit_async(account, self._on_deposit_received)
            else:
                self._update_holdings_ui(balance, deposit)

        except Exception as e:
            self.log(f"[시스템] 잔고 조회 결과 처리 오류: {e}")

    def _on_deposit_received(self, deposit_info):
        """예수금 재조회 결과 콜백"""
        if deposit_info is None:
            self.log("[잔고조회] 예수금 재조회 실패")
            return

        try:
            self.log(f"[잔고조회] opw00001 결과: {deposit_info}")
            # 주문가능금액 > D+2예수금 > 예수금 순으로 사용
            deposit = deposit_info.get("order_available", 0) or 0
            if deposit == 0:
                deposit = deposit_info.get("deposit_d2", 0) or 0
            if deposit == 0:
                deposit = deposit_info.get("deposit", 0) or 0

            # ✅ 예수금이 여전히 0이면 원인 안내
            if deposit == 0 and self.kiwoom.is_real_server():
                self.log("[경고] 실계좌 예수금 조회 실패!")
                self.log("[안내] 해결 방법:")
                self.log("  1. KOA Studio에서 [도구 > 계좌비밀번호 저장] 확인")
                self.log("  2. 계좌번호 선택 후 비밀번호 입력 및 '등록' 클릭")
                self.log("  3. 'AUTO' 체크박스가 선택되어 있는지 확인")
                self.log("  4. 프로그램 재시작 후 다시 시도")

            self.balance_label.setText(f"예수금: {deposit:,}원")

        except Exception as e:
            self.log(f"[잔고조회] 예수금 처리 오류: {e}")

    def _update_holdings_ui(self, balance, deposit):
        """보유종목 UI 업데이트"""
        try:
            self.balance_label.setText(f"예수금: {deposit:,}원")

            holdings = balance.get("holdings", [])
            self.holdings_table.setRowCount(len(holdings))
            self._holdings_code_to_row = {}

            for row, holding in enumerate(holdings):
                code = holding["code"]
                self._holdings_code_to_row[code] = row
                self.holdings_table.setItem(row, 0, QTableWidgetItem(code))
                self.holdings_table.setItem(row, 1, QTableWidgetItem(holding["name"]))
                self.holdings_table.setItem(row, 2, QTableWidgetItem(f"{holding['quantity']:,}"))
                self.holdings_table.setItem(row, 3, QTableWidgetItem(f"{holding['avg_price']:,}"))
                self.holdings_table.setItem(row, 4, QTableWidgetItem(f"{holding['current_price']:,}"))
                self.holdings_table.setItem(row, 5, QTableWidgetItem(f"{holding['eval_amount']:,}"))
                self.holdings_table.setItem(row, 6, QTableWidgetItem(f"{holding['profit']:,}"))

                profit_rate = holding.get("profit_rate", 0.0) or 0.0
                rate_item = QTableWidgetItem(f"{profit_rate:.2f}%")
                if profit_rate > 0:
                    rate_item.setForeground(QColor("red"))
                elif profit_rate < 0:
                    rate_item.setForeground(QColor("blue"))
                self.holdings_table.setItem(row, 7, rate_item)

            if self.trader:
                self.trader.sync_positions_from_account(balance)

        except Exception as e:
            self.log(f"[시스템] 보유종목 UI 업데이트 오류: {e}")

    def refresh_watchlist(self):
        """감시 종목 갱신 (비동기 방식으로 UI 프리징 방지)"""
        if self._is_stopping:
            return

        if self._is_refreshing_watchlist:
            return
        self._is_refreshing_watchlist = True

        try:
            watchlist = self.config.get_watchlist()

            now = time.time()
            if now - self._last_watchlist_log_ts > 10:
                self.log(f"[시스템] 감시 종목 로드: {len(watchlist)}개")
                self._last_watchlist_log_ts = now

            self._update_watchlist_header()

            # ✅ 행 수가 다를 때만 setRowCount 호출 (불필요한 리셋 방지)
            current_row_count = self.watchlist_table.rowCount()
            if current_row_count != len(watchlist):
                self.watchlist_table.setRowCount(len(watchlist))

            period = self.config.get("buy", "envelope_period") or 20
            percent = self.config.get("buy", "envelope_percent") or 19

            # ✅ 코드/이름 매핑 갱신 + 기본 정보 설정 (기존 값 유지)
            self._watchlist_code_to_row = {}
            for row, stock in enumerate(watchlist):
                code = stock["code"]
                name = stock.get("name", "")
                self._watchlist_code_to_row[code] = row

                # 코드/이름은 항상 설정 (변경될 수 있음)
                self.watchlist_table.setItem(row, 0, QTableWidgetItem(code))
                self.watchlist_table.setItem(row, 1, QTableWidgetItem(name))

                # ✅ 기존 값이 없을 때만 "-"로 초기화 (기존 값 유지)
                if not self.watchlist_table.item(row, 2):
                    self.watchlist_table.setItem(row, 2, QTableWidgetItem("-"))
                if not self.watchlist_table.item(row, 3):
                    self.watchlist_table.setItem(row, 3, QTableWidgetItem("-"))
                if not self.watchlist_table.item(row, 4):
                    self.watchlist_table.setItem(row, 4, QTableWidgetItem("-"))

            # 캐시된 데이터로 먼저 표시 (이벤트 엔진의 배치 스케줄러 캐시 사용)
            if self.trader and self.trader.event_engine:
                batch_scheduler = self.trader.event_engine.batch_scheduler
                for row, stock in enumerate(watchlist):
                    code = stock["code"]
                    cached_candles = batch_scheduler.get_cached_candles(code)
                    if cached_candles:
                        try:
                            current_price = cached_candles[0].get("close")
                            envelope = self.ta.get_envelope_levels(cached_candles, period, percent)
                            self.watchlist_table.setItem(row, 2, QTableWidgetItem(self._fmt_int_or_dash(current_price)))
                            self.watchlist_table.setItem(row, 3, QTableWidgetItem(self._fmt_int_or_dash(envelope.get("ma"))))
                            self.watchlist_table.setItem(row, 4, QTableWidgetItem(self._fmt_int_or_dash(envelope.get("lower"))))
                        except Exception:
                            pass

            self.watchlist_table.viewport().update()

            # 캐시가 없는 종목들만 비동기로 조회 시작 (UI 프리징 방지)
            self._watchlist_refresh_queue = []
            for row, stock in enumerate(watchlist):
                code = stock["code"]
                has_cache = False
                if self.trader and self.trader.event_engine:
                    has_cache = self.trader.event_engine.batch_scheduler.is_cache_valid(code)
                if not has_cache:
                    self._watchlist_refresh_queue.append((row, stock))

            self._watchlist_refresh_period = period
            self._watchlist_refresh_percent = percent

            # 큐가 있으면 비동기 갱신 시작
            if self._watchlist_refresh_queue and self.kiwoom and self.kiwoom.is_connected():
                self.log(f"[시스템] 종목 정보 조회 시작: {len(self._watchlist_refresh_queue)}개 종목")
                QTimer.singleShot(100, self._refresh_watchlist_next)
            else:
                self._is_refreshing_watchlist = False
                if not self.kiwoom or not self.kiwoom.is_connected():
                    self.log("[시스템] 로그인 후 종목 정보를 새로고침해주세요.")

        except Exception as e:
            self.log(f"[시스템] 감시 종목 갱신 오류: {e}")
            self._is_refreshing_watchlist = False

    def _refresh_watchlist_next(self):
        """비동기로 감시 종목 정보를 하나씩 갱신 (TR 큐 기반)"""
        if self._is_stopping:
            self._is_refreshing_watchlist = False
            return

        if not self._watchlist_refresh_queue:
            self._is_refreshing_watchlist = False
            self.watchlist_table.viewport().update()
            return

        if not self.kiwoom or not self.kiwoom.is_connected():
            self._is_refreshing_watchlist = False
            return

        row, stock = self._watchlist_refresh_queue.pop(0)
        code = stock["code"]

        # 테이블 행 유효성 확인
        if row >= self.watchlist_table.rowCount():
            # 테이블이 리셋되었을 수 있음, 다음 종목으로 진행
            if self._watchlist_refresh_queue:
                QTimer.singleShot(100, self._refresh_watchlist_next)
            else:
                self._is_refreshing_watchlist = False
            return

        # ✅ TR 큐에 일봉 조회 요청 추가 (row, code를 클로저로 캡처)
        def on_candles_received(candles, r=row, c=code):
            self._on_watchlist_candles_received(r, c, candles)

        count = max(self._watchlist_refresh_period + 5, 25)
        self.kiwoom.get_daily_candles_async(code, on_candles_received, count)

    def _on_watchlist_candles_received(self, row, code, candles):
        """감시종목 일봉 조회 결과 콜백"""
        try:
            # 테이블 행 유효성 재확인
            if row >= self.watchlist_table.rowCount():
                self._continue_watchlist_refresh()
                return

            if candles and len(candles) > 0:
                current_price = candles[0].get("close", 0)
                envelope = self.ta.get_envelope_levels(candles, self._watchlist_refresh_period, self._watchlist_refresh_percent)

                # UI 업데이트
                self.watchlist_table.setItem(row, 2, QTableWidgetItem(self._fmt_int_or_dash(current_price)))
                self.watchlist_table.setItem(row, 3, QTableWidgetItem(self._fmt_int_or_dash(envelope.get("ma"))))
                self.watchlist_table.setItem(row, 4, QTableWidgetItem(self._fmt_int_or_dash(envelope.get("lower"))))

                # 캐시 업데이트 (이벤트 엔진이 있으면)
                if self.trader and self.trader.event_engine:
                    self.trader.event_engine.batch_scheduler.update_cache(code, candles)

                self._continue_watchlist_refresh()
            else:
                # 일봉 데이터 실패 시 현재가만이라도 조회 (opt10001 fallback - 큐 기반)
                self.log(f"[시스템] [{code}] 일봉 데이터 없음, 현재가 조회 시도...")

                def on_stock_info_received(stock_info, r=row, c=code):
                    self._on_watchlist_stock_info_received(r, c, stock_info)

                self.kiwoom.get_stock_info_async(code, on_stock_info_received)

        except Exception as e:
            self.log(f"[시스템] [{code}] 일봉 처리 오류: {e}")
            self._continue_watchlist_refresh()

    def _on_watchlist_stock_info_received(self, row, code, stock_info):
        """감시종목 현재가 조회 결과 콜백 (fallback)"""
        try:
            if row >= self.watchlist_table.rowCount():
                self._continue_watchlist_refresh()
                return

            if stock_info and stock_info.get("price", 0) > 0:
                current_price = stock_info.get("price", 0)
                self.watchlist_table.setItem(row, 2, QTableWidgetItem(self._fmt_int_or_dash(current_price)))
                self.watchlist_table.setItem(row, 3, QTableWidgetItem("-"))
                self.watchlist_table.setItem(row, 4, QTableWidgetItem("-"))
                self.log(f"[시스템] [{code}] 현재가 조회 성공: {current_price:,}원")
            else:
                self.log(f"[시스템] [{code}] 현재가 조회 실패")

        except Exception as e:
            self.log(f"[시스템] [{code}] 현재가 처리 오류: {e}")

        self._continue_watchlist_refresh()

    def _continue_watchlist_refresh(self):
        """감시종목 갱신 계속 진행"""
        if self._watchlist_refresh_queue:
            # 다음 종목은 TR 큐가 알아서 순차 처리하므로 바로 호출
            QTimer.singleShot(50, self._refresh_watchlist_next)
        else:
            self._is_refreshing_watchlist = False
            self.watchlist_table.viewport().update()
            self.watchlist_table.viewport().update()

    # =========================
    # 워치리스트 관리
    # =========================
    def add_to_watchlist(self):
        """감시 종목 추가 (종목코드 또는 종목명으로 검색)"""
        input_text = self.add_code_input.text().strip()
        if not input_text:
            QMessageBox.warning(self, "입력 오류", "종목코드 또는 종목명을 입력해주세요.")
            return

        code = ""
        name = ""

        # 6자리 숫자인 경우 종목코드로 처리
        if input_text.isdigit() and len(input_text) == 6:
            code = input_text
            if self.kiwoom and self.kiwoom.is_connected():
                # ✅ 캐시에서 먼저 조회 (빠름)
                name = self.kiwoom.get_stock_name_from_cache(code)
                if not name:
                    QMessageBox.warning(self, "오류", f"종목코드 '{code}'를 찾을 수 없습니다.")
                    return
            else:
                self.log("[시스템] 로그인 전 종목 추가: 종목명은 로그인 후 자동 표시될 수 있습니다.")
            # 종목코드 입력은 바로 추가
            self._add_stock_to_watchlist(code, name)
        else:
            # 종목명으로 검색 - 백그라운드 처리
            if not self.kiwoom or not self.kiwoom.is_connected():
                QMessageBox.warning(self, "오류", "종목명 검색은 로그인 후 가능합니다.")
                return

            # ✅ 캐시가 로드되어 있으면 동기적으로 빠르게 검색
            if self.kiwoom.is_stock_cache_loaded():
                results = self.kiwoom.find_stocks_by_name(input_text)
                self._handle_watchlist_search_results(results, input_text)
            else:
                # 캐시 미로드 시 백그라운드 검색
                self.log(f"[시스템] '{input_text}' 검색 중...")
                self._search_worker = StockSearchWorker(self.kiwoom, input_text)
                self._search_worker.search_finished.connect(
                    lambda results: self._handle_watchlist_search_results(results, input_text)
                )
                self._search_worker.search_error.connect(
                    lambda err: QMessageBox.warning(self, "검색 오류", f"검색 중 오류 발생: {err}")
                )
                self._search_worker.start()

    def _handle_watchlist_search_results(self, results, search_text):
        """종목 검색 결과 처리 (감시종목 추가용)"""
        if not results:
            QMessageBox.warning(self, "검색 결과 없음", f"'{search_text}'에 해당하는 종목을 찾을 수 없습니다.")
            return
        elif len(results) == 1:
            code, name = results[0]
            self._add_stock_to_watchlist(code, name)
        else:
            # 여러 결과가 있으면 사용자에게 선택하도록 함
            items = [f"{r[0]} - {r[1]}" for r in results]
            selected, ok = QInputDialog.getItem(
                self, "종목 선택",
                f"'{search_text}' 검색 결과 ({len(results)}건):",
                items, 0, False
            )
            if ok and selected:
                idx = items.index(selected)
                code, name = results[idx]
                self._add_stock_to_watchlist(code, name)

    def _add_stock_to_watchlist(self, code, name):
        """감시종목에 종목 추가"""
        success, message = self.config.add_to_watchlist(code, name)
        if success:
            self.log(f"[시스템] 감시 종목 추가: {code} {name}")
            self.add_code_input.clear()
            self.refresh_watchlist()
        else:
            QMessageBox.warning(self, "오류", message)

    def remove_from_watchlist(self):
        """감시 종목 삭제"""
        selected = self.watchlist_table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "선택 오류", "삭제할 종목을 선택해주세요.")
            return

        row = selected[0].row()
        code = self.watchlist_table.item(row, 0).text()

        reply = QMessageBox.question(
            self, "삭제 확인",
            f"종목 '{code}'를 감시 목록에서 삭제하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.config.remove_from_watchlist(code)
            self.log(f"[시스템] 감시 종목 삭제: {code}")
            self.refresh_watchlist()

    def resolve_stock_code(self, input_text):
        """종목코드 또는 종목명을 입력받아 종목코드와 종목명을 반환 (캐시 기반 - UI 프리징 없음)

        Args:
            input_text: 종목코드(6자리 숫자) 또는 종목명

        Returns:
            (code, name) 튜플. 실패 시 (None, None) 반환
        """
        if not input_text:
            QMessageBox.warning(self, "입력 오류", "종목코드 또는 종목명을 입력해주세요.")
            return None, None

        # 6자리 숫자인 경우 종목코드로 처리
        if input_text.isdigit() and len(input_text) == 6:
            code = input_text
            if self.kiwoom and self.kiwoom.is_connected():
                # ✅ 캐시에서 먼저 조회 (빠름)
                name = self.kiwoom.get_stock_name_from_cache(code)
                if not name:
                    QMessageBox.warning(self, "오류", f"종목코드 '{code}'를 찾을 수 없습니다.")
                    return None, None
                return code, name
            else:
                return code, ""
        else:
            # 종목명으로 검색 (캐시 기반이면 빠름)
            if not self.kiwoom or not self.kiwoom.is_connected():
                QMessageBox.warning(self, "오류", "종목명 검색은 로그인 후 가능합니다.")
                return None, None

            # ✅ 캐시 기반 검색 (캐시 로드 시 빠름, 미로드 시 기존 방식)
            results = self.kiwoom.find_stocks_by_name(input_text)
            if not results:
                QMessageBox.warning(self, "검색 결과 없음", f"'{input_text}'에 해당하는 종목을 찾을 수 없습니다.")
                return None, None
            elif len(results) == 1:
                return results[0]
            else:
                # 여러 결과가 있으면 사용자에게 선택하도록 함
                items = [f"{r[0]} - {r[1]}" for r in results]
                selected, ok = QInputDialog.getItem(
                    self, "종목 선택",
                    f"'{input_text}' 검색 결과 ({len(results)}건):",
                    items, 0, False
                )
                if ok and selected:
                    idx = items.index(selected)
                    return results[idx]
                else:
                    return None, None

    # =========================
    # 보유종목 선택 / 분석
    # =========================
    def on_holding_selected(self):
        """보유 종목 선택시"""
        selected = self.holdings_table.selectedItems()
        if not selected:
            return

        row = selected[0].row()
        code = self.holdings_table.item(row, 0).text()
        name = self.holdings_table.item(row, 1).text()
        quantity = self.holdings_table.item(row, 2).text().replace(",", "")

        self.manual_sell_code.setText(code)
        self.manual_sell_qty.setValue(int(quantity))

        if self.trader:
            analysis = self.trader.get_stock_analysis(code)
            if analysis:
                self.update_analysis_display(code, name, analysis)

    def update_analysis_display(self, code, name, analysis):
        """종목 분석 정보 표시"""
        stock_info = analysis.get("stock_info", {}) or {}
        envelope = analysis.get("envelope", {}) or {}
        buy_signal = analysis.get("buy_signal", {}) or {}
        position_summary = analysis.get("position_summary")

        price_txt = self._fmt_int_or_dash(stock_info.get("price"))
        ma_txt = self._fmt_int_or_dash(envelope.get("ma"))
        lower_txt = self._fmt_int_or_dash(envelope.get("lower"))

        self.analysis_code_label.setText(f"종목: {code} {name} (현재가: {price_txt}원)")
        self.analysis_ma20_label.setText(f"20일선: {ma_txt}원")
        self.analysis_envelope_label.setText(f"엔벨로프 하한: {lower_txt}원")

        signal_text = buy_signal.get("reason", "-")
        if buy_signal.get("signal"):
            self.analysis_buy_signal_label.setStyleSheet("color: red; font-weight: bold;")
        else:
            self.analysis_buy_signal_label.setStyleSheet("")
        self.analysis_buy_signal_label.setText(f"매수 신호: {signal_text}")

        if position_summary:
            self.analysis_position_label.setText(
                f"포지션: {position_summary['buy_count']}차 매수 / "
                f"평단 {position_summary['avg_price']:,}원 / "
                f"수익률 {position_summary['profit_rate']:.2f}%"
            )

            targets = position_summary.get("sell_targets", [])
            sold = position_summary.get("sold_targets", [])

            self.sell_targets_table.setRowCount(len(targets))
            for i, target in enumerate(targets):
                self.sell_targets_table.setItem(i, 0, QTableWidgetItem(target["name"]))
                self.sell_targets_table.setItem(i, 1, QTableWidgetItem(f"{target['price']:,}원"))

                if target["name"] in sold:
                    status_item = QTableWidgetItem("매도완료")
                    status_item.setForeground(QColor("gray"))
                else:
                    status_item = QTableWidgetItem("대기")
                self.sell_targets_table.setItem(i, 2, status_item)
        else:
            self.analysis_position_label.setText("포지션: 없음")
            self.sell_targets_table.setRowCount(0)

    # =========================
    # 수동매매 / 주문취소
    # =========================
    def on_sell_ratio_changed(self, ratio):
        """비중 변경시 수량 자동 계산"""
        code = self.manual_sell_code.text().strip()
        if not code:
            return

        for row in range(self.holdings_table.rowCount()):
            if self.holdings_table.item(row, 0).text() == code:
                total_qty_str = self.holdings_table.item(row, 2).text().replace(",", "")
                try:
                    total_qty = int(total_qty_str)
                    sell_qty = max(1, int(total_qty * ratio / 100))
                    self.manual_sell_qty.setValue(sell_qty)
                except ValueError:
                    pass
                break

    def do_manual_sell(self):
        """수동 매도 (수량 기준)"""
        if not self.trader:
            return

        input_text = self.manual_sell_code.text().strip()
        quantity = self.manual_sell_qty.value()
        price = self.manual_sell_price.value()

        code, name = self.resolve_stock_code(input_text)
        if not code:
            return

        display_name = f"{code} {name}" if name else code
        reply = QMessageBox.question(
            self, "매도 확인",
            f"종목 {display_name}를 {quantity}주 {'시장가' if price == 0 else f'{price:,}원'}에 매도하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.trader.manual_sell(code, quantity, price)

    def do_manual_sell_by_ratio(self):
        """수동 매도 (비중 기준)"""
        if not self.trader:
            return

        input_text = self.manual_sell_code.text().strip()
        ratio = self.manual_sell_ratio.value()
        price = self.manual_sell_price.value()

        code, name = self.resolve_stock_code(input_text)
        if not code:
            return

        total_qty = 0
        for row in range(self.holdings_table.rowCount()):
            if self.holdings_table.item(row, 0).text() == code:
                total_qty_str = self.holdings_table.item(row, 2).text().replace(",", "")
                try:
                    total_qty = int(total_qty_str)
                except ValueError:
                    pass
                break

        display_name = f"{code} {name}" if name else code
        if total_qty <= 0:
            QMessageBox.warning(self, "오류", f"종목 {display_name}의 보유 수량을 확인할 수 없습니다.")
            return

        sell_qty = max(1, int(total_qty * ratio / 100))

        reply = QMessageBox.question(
            self, "매도 확인",
            f"종목 {display_name}를 {ratio}% ({sell_qty}주/{total_qty}주) "
            f"{'시장가' if price == 0 else f'{price:,}원'}에 매도하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.trader.manual_sell(code, sell_qty, price)

    def do_manual_buy(self):
        """수동 매수"""
        if not self.trader:
            return

        input_text = self.manual_buy_code.text().strip()
        quantity = self.manual_buy_qty.value()
        price = self.manual_buy_price.value()

        code, name = self.resolve_stock_code(input_text)
        if not code:
            return

        display_name = f"{code} {name}" if name else code
        reply = QMessageBox.question(
            self, "매수 확인",
            f"종목 {display_name}를 {quantity}주 {'시장가' if price == 0 else f'{price:,}원'}에 매수하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            self.trader.manual_buy(code, quantity, price)

    def do_cancel_all_orders(self):
        """전량주문 취소"""
        if not self.kiwoom or not self.kiwoom.is_connected():
            QMessageBox.warning(self, "오류", "키움 API에 연결되어 있지 않습니다.")
            return

        code = self.manual_sell_code.text().strip()
        if not code:
            QMessageBox.warning(self, "입력 오류", "종목코드를 입력해주세요.")
            return

        account = self.account_combo.currentText().strip()
        if not account:
            QMessageBox.warning(self, "오류", "계좌를 선택해주세요.")
            return

        reply = QMessageBox.question(
            self, "전량주문 취소 확인",
            f"종목 {code}의 모든 미체결 주문을 취소하시겠습니까?",
            QMessageBox.Yes | QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            cancelled = self.kiwoom.cancel_all_orders_for_stock(account, code)
            if cancelled > 0:
                self.log(f"[{code}] {cancelled}건의 주문이 취소되었습니다.")
                QMessageBox.information(self, "취소 완료", f"{cancelled}건의 주문이 취소되었습니다.")
            else:
                self.log(f"[{code}] 취소할 미체결 주문이 없습니다.")
                QMessageBox.information(self, "알림", "취소할 미체결 주문이 없습니다.")

    # =========================
    # 설정 저장
    # =========================
    def save_settings(self):
        """설정 저장"""
        self.config.set(self.setting_envelope_period.value(), "buy", "envelope_period")
        self.config.set(self.setting_envelope_percent.value(), "buy", "envelope_percent")
        self.config.set(self.setting_envelope_buy_percent.value(), "buy", "envelope_buy_percent")
        self.config.set(self.setting_add_drop.value(), "buy", "additional_buy_drop_percent")
        self.config.set(self.setting_buy_amount.value(), "buy", "buy_amount_per_stock")
        self.config.set(self.setting_max_holding.value(), "buy", "max_holding_stocks")
        # 재진입 허용 설정 삭제됨 - 매도 발생 시 당일 재매수 금지

        self.config.set(
            [self.setting_profit1.value(), self.setting_profit2.value(), self.setting_profit3.value()],
            "sell", "profit_targets"
        )
        self.config.set(
            [self.setting_ratio1.value(), self.setting_ratio2.value(), self.setting_ratio3.value()],
            "sell", "profit_sell_ratios"
        )
        self.config.set(self.setting_ma20_ratio.value(), "sell", "ma20_sell_ratio")

        # 모의투자 설정 삭제됨 - 로그인 시 서버 자동 감지

        self.config.save_config()

        self._update_watchlist_header()

        self.log("[시스템] 설정이 저장되었습니다.")
        QMessageBox.information(self, "저장 완료", "설정이 저장되었습니다.")

    # =========================
    # 미체결 주문 알림 / 종료
    # =========================
    def _check_pending_orders_on_startup(self):
        """프로그램 시작 시 저장된 미체결 주문 확인 및 알림"""
        if not self.trader:
            return

        summary = self.trader.get_pending_orders_summary()
        total = summary["buy_orders"] + summary["sell_orders"]

        if total > 0:
            msg = (
                f"저장된 미체결 주문이 있습니다.\n\n"
                f"- 매수 주문: {summary['buy_orders']}건\n"
                f"- 매도 주문: {summary['sell_orders']}건\n\n"
                f"자동매매를 시작하면 장 시간에 자동으로 복원됩니다.\n"
                f"복원을 원하지 않으면 설정에서 삭제할 수 있습니다."
            )
            self.log(f"[시스템] 저장된 미체결 주문: 매수 {summary['buy_orders']}건, 매도 {summary['sell_orders']}건")
            QMessageBox.information(self, "미체결 주문 복원 알림", msg)
        else:
            self.log("[시스템] 복원할 미체결 주문 없음")

    def closeEvent(self, event):
        """종료시 상태 저장"""
        if self.trader:
            if self.trader.is_running and not self._is_stopping:
                # 종료 중에도 stop이 오래 걸릴 수 있어, UI 종료 시점엔 안전하게 처리
                self._is_stopping = True
                try:
                    self.trader.stop()
                except Exception as e:
                    self.log(f"[시스템] 종료 중 stop 오류: {e}")

            try:
                self.trader.save_current_state()
            except Exception as e:
                self.log(f"[시스템] 종료 중 상태 저장 오류: {e}")

            self.log("[시스템] 상태 저장 완료")

        self.refresh_timer.stop()
        self.trading_timer.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()