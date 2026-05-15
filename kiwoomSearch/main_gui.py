# -*- coding: utf-8 -*-
"""
키움증권 종목 탐색 시스템 - 메인 GUI
"""
import sys
from collections import deque
from datetime import datetime

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QTextEdit, QComboBox, QTabWidget, QMessageBox, QHeaderView,
    QFrame, QGridLayout, QProgressBar, QDoubleSpinBox, QSpinBox,
    QCheckBox, QDialog, QRadioButton, QButtonGroup, QScrollArea
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QColor

from config import Config
from kiwoom_api import KiwoomAPI
from scanner import Scanner


# ──────────────────────────────────────────────────────────────────────────────
# 이용 약관 다이얼로그
# ──────────────────────────────────────────────────────────────────────────────
class DisclaimerDialog(QDialog):
    """이용 약관 동의 다이얼로그 (스크롤 끝까지 내려야 체크박스 활성화)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("중요 안내 사항")
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.CustomizeWindowHint)
        self.setModal(True)
        self.setMinimumWidth(540)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(
            "*프로그램 이용 시 필독사항*\n\n"
            "본 프로그램은 투자 도구로 제공되는 소프트웨어이며\n"
            "특정 종목에 대한 매수 또는 매도를 권유하거나\n"
            "투자 자문을 제공하지 않습니다.\n\n"
            "본 프로그램은 사용자가 설정한 기술적 조건(RSI, 이동평균, 거래량, 가격 돌파)에\n"
            "부합하는 종목을 탐색하여 리스트업 하는 기능만을 수행합니다.\n\n"
            "탐색된 종목에 대한 매매 판단과 최종 책임은 전적으로 사용자 본인에게 있습니다.\n\n"
            "본 프로그램은 수익을 보장하지 않으며\n"
            "투자 결과에 따라 손실이 발생할 수 있습니다.\n\n"
            "프로그램 사용으로 발생하는 모든 투자 결과 및 손익에 대한 책임은\n"
            "사용자 본인에게 있으며 프로그램 제공자는 이에 대해 어떠한 책임도 부담하지 않습니다.\n\n"
            "탐색 조건은 일봉 기준으로 계산되며, 당일 장중 미완성 봉을 포함합니다.\n\n"
            "***프로그램 이용은 위 내용을 충분히 이해하고 동의한 것으로 간주됩니다.***"
        )
        self.text.setMinimumHeight(260)
        self.text.verticalScrollBar().valueChanged.connect(self._on_scroll)
        layout.addWidget(self.text)

        self.hint = QLabel("↓ 내용을 끝까지 스크롤해야 동의할 수 있습니다.")
        self.hint.setStyleSheet("color: #e65100; font-size: 9pt;")
        layout.addWidget(self.hint)

        self.check = QCheckBox("위 내용을 모두 읽었으며 동의합니다.")
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self.check.setFont(f)
        self.check.setEnabled(False)
        self.check.stateChanged.connect(lambda s: self.ok_btn.setEnabled(s == Qt.Checked))
        layout.addWidget(self.check)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("취소 (종료)"); cancel.setMinimumWidth(100)
        cancel.clicked.connect(self.reject)
        self.ok_btn = QPushButton("확인"); self.ok_btn.setMinimumWidth(100)
        self.ok_btn.setEnabled(False)
        self.ok_btn.setStyleSheet(
            "QPushButton:enabled{background:#4CAF50;color:white;font-weight:bold;}"
            "QPushButton:disabled{background:#ccc;color:#888;}"
        )
        self.ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel); btn_row.addWidget(self.ok_btn)
        layout.addLayout(btn_row)

    def _on_scroll(self, v):
        if v >= self.text.verticalScrollBar().maximum():
            self.check.setEnabled(True)
            self.hint.setText("✔ 내용을 모두 확인했습니다. 아래에 동의해 주세요.")
            self.hint.setStyleSheet("color: #2e7d32; font-size: 9pt;")

    def closeEvent(self, e):
        self.reject(); e.accept()


# ──────────────────────────────────────────────────────────────────────────────
# 진행 표시 다이얼로그
# ──────────────────────────────────────────────────────────────────────────────
class ScanProgressDialog(QDialog):
    """
    스캔 진행 상황 팝업 (비모달, 항상 최상위)
    - 단계 표시 (전체 스캔 / 갱신 스캔)
    - 진행 바
    - 현재 처리 중인 종목명
    - 취소 버튼
    """

    def __init__(self, cancel_callback, parent=None):
        super().__init__(parent)
        self._cancel_cb = cancel_callback
        self.setWindowTitle("종목 탐색 중...")
        self.setWindowFlags(
            Qt.Window | Qt.WindowTitleHint | Qt.CustomizeWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setFixedWidth(440)
        self.setModal(False)

        v = QVBoxLayout(self)
        v.setSpacing(10)
        v.setContentsMargins(16, 14, 16, 12)

        # 단계 레이블
        self._phase_lbl = QLabel("준비 중...")
        f = QFont(); f.setBold(True); f.setPointSize(11)
        self._phase_lbl.setFont(f)
        v.addWidget(self._phase_lbl)

        # 진행 바
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFormat("0 / 0")
        self._bar.setFixedHeight(22)
        v.addWidget(self._bar)

        # 현재 처리 종목
        self._name_lbl = QLabel("")
        self._name_lbl.setStyleSheet("color:#555; font-size:9pt;")
        self._name_lbl.setAlignment(Qt.AlignCenter)
        v.addWidget(self._name_lbl)

        # 캐시 안내
        self._cache_lbl = QLabel("")
        self._cache_lbl.setStyleSheet("color:#1565C0; font-size:9pt;")
        self._cache_lbl.setAlignment(Qt.AlignCenter)
        v.addWidget(self._cache_lbl)

        # 취소 버튼
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton("취소")
        self._cancel_btn.setMinimumWidth(90)
        self._cancel_btn.setStyleSheet(
            "background:#f44336; color:white; font-weight:bold;"
        )
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self._cancel_btn)
        v.addLayout(btn_row)

    def update(self, phase: str, done: int, total: int, name: str):
        """진행 상황 갱신 (Scanner.progress_cb 에서 호출)."""
        self._phase_lbl.setText(phase)
        if total > 0:
            self._bar.setMaximum(total)
            self._bar.setValue(done)
            pct = int(done / total * 100)
            self._bar.setFormat(f"{done} / {total}  ({pct}%)")
        self._name_lbl.setText(name)

    def set_cache_info(self, cached: int, total: int):
        """캐시 재사용 종목 수 표시."""
        if cached > 0:
            self._cache_lbl.setText(f"캐시 재사용 {cached}개 / 신규 TR 조회 {total}개")
        else:
            self._cache_lbl.setText("")

    def mark_done(self):
        """완료 상태로 변경."""
        self._phase_lbl.setText("탐색 완료")
        self._bar.setFormat("완료")
        self._name_lbl.setText("")
        self._cancel_btn.setText("닫기")
        self._cancel_btn.setStyleSheet(
            "background:#4CAF50; color:white; font-weight:bold;"
        )
        # 취소 콜백을 닫기 동작으로 교체
        try:
            self._cancel_btn.clicked.disconnect()
        except Exception:
            pass
        self._cancel_btn.clicked.connect(self.hide)

    def _on_cancel(self):
        if self._cancel_cb:
            self._cancel_cb()
        self.hide()

    def closeEvent(self, e):
        # X 버튼 클릭 시도 → 취소 처리
        self._on_cancel()
        e.ignore()  # 실제 닫기는 hide로 대체


# ──────────────────────────────────────────────────────────────────────────────
# 메인 윈도우
# ──────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):

    # 로그 시그널 (스레드 안전)
    _log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.config  = Config()
        self.kiwoom  = None
        self.scanner: Scanner | None = None
        self._progress_dlg: ScanProgressDialog | None = None

        # 탐색 결과 캐시 {code: row_dict}
        self._result_cache: dict = {}

        # 로그 버퍼
        self._log_buf = deque(maxlen=3000)
        self._log_signal.connect(self._append_log)

        self._init_ui()

        # 로그 플러시 타이머
        self._flush_timer = QTimer()
        self._flush_timer.timeout.connect(self._flush_log)
        self._flush_timer.start(200)

        # 다음 갱신 카운트다운 타이머 (1초마다)
        self._countdown = 0
        self._countdown_timer = QTimer()
        self._countdown_timer.timeout.connect(self._tick_countdown)

    # ──────────────────────────────────────────────────────────────────────
    # UI 구성
    # ──────────────────────────────────────────────────────────────────────
    def _init_ui(self):
        self.setWindowTitle("종목 탐색 시스템")
        self.setGeometry(100, 100, 1300, 850)

        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(8, 8, 8, 4)
        vbox.setSpacing(6)

        vbox.addWidget(self._build_top_bar())

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_scan_tab(),     "탐색종목")
        self._tabs.addTab(self._build_settings_tab(), "설정")
        vbox.addWidget(self._tabs, 1)

        vbox.addWidget(self._build_log_frame())

    # ── 상단 바 ────────────────────────────────────────────────────────────
    def _build_top_bar(self):
        frame = QFrame()
        frame.setFrameStyle(QFrame.StyledPanel)
        h = QHBoxLayout(frame)
        h.setContentsMargins(6, 4, 6, 4)

        self.status_lbl = QLabel("연결 상태: 미연결")
        self.status_lbl.setStyleSheet("color:red; font-weight:bold;")
        h.addWidget(self.status_lbl)

        self.login_btn = QPushButton("로그인")
        self.login_btn.setMinimumWidth(80)
        self.login_btn.clicked.connect(self._do_login)
        h.addWidget(self.login_btn)

        h.addWidget(QLabel("  |  계좌:"))
        self.account_combo = QComboBox()
        self.account_combo.setMinimumWidth(140)
        h.addWidget(self.account_combo)

        h.addStretch()

        # 다음 갱신 카운트다운
        self.countdown_lbl = QLabel("")
        self.countdown_lbl.setStyleSheet("color:#555; font-size:9pt;")
        h.addWidget(self.countdown_lbl)

        self.scan_btn = QPushButton("자동탐색 시작")
        self.scan_btn.setMinimumWidth(130)
        self.scan_btn.setMinimumHeight(36)
        self.scan_btn.setStyleSheet(
            "background:#4CAF50; color:white; font-weight:bold; font-size:11pt;"
        )
        self.scan_btn.setEnabled(False)
        self.scan_btn.clicked.connect(self._toggle_scan)
        h.addWidget(self.scan_btn)

        return frame

    # ── 탐색종목 탭 ────────────────────────────────────────────────────────
    def _build_scan_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 4)

        # 진행 바
        prog_row = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("대기 중")
        self.progress_bar.setFixedHeight(20)
        prog_row.addWidget(self.progress_bar, 1)

        self.progress_lbl = QLabel("")
        self.progress_lbl.setStyleSheet("font-size:9pt; color:#333;")
        prog_row.addWidget(self.progress_lbl)
        v.addLayout(prog_row)

        # 결과 요약
        summary_row = QHBoxLayout()
        self.result_count_lbl = QLabel("탐색 결과: -")
        self.result_count_lbl.setStyleSheet("font-weight:bold;")
        summary_row.addWidget(self.result_count_lbl)
        summary_row.addStretch()

        self.last_scan_lbl = QLabel("마지막 갱신: -")
        self.last_scan_lbl.setStyleSheet("font-size:9pt; color:#555;")
        summary_row.addWidget(self.last_scan_lbl)
        v.addLayout(summary_row)

        # 결과 테이블
        self.result_table = QTableWidget()
        self.result_table.setColumnCount(9)
        self.result_table.setHorizontalHeaderLabels([
            "종목명 (코드)", "현재가", "RSI", "이평(MA)", "거래량비", "돌파",
            "수급", "거래대금", "조건"
        ])
        hdr = self.result_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 9):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.result_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setSortingEnabled(True)
        v.addWidget(self.result_table, 1)

        return w

    # ── 설정 탭 ────────────────────────────────────────────────────────────
    def _build_settings_tab(self):
        # 스크롤 영역으로 감싸서 창 크기에 무관하게 설정 내용을 스크롤로 볼 수 있게 함
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        w = QWidget()
        scroll.setWidget(w)

        v = QVBoxLayout(w)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(10)

        scan = self.config.get_scan()

        # ── 탐색 대상 ──────────────────────────────────────────────────────
        target_grp = QGroupBox("탐색 대상")
        tg = QGridLayout(target_grp)

        tg.addWidget(QLabel("탐색 시장:"), 0, 0)
        self.market_combo = QComboBox()
        self.market_combo.addItem("코스피 + 코스닥", "both")
        self.market_combo.addItem("코스피만",        "kospi")
        self.market_combo.addItem("코스닥만",        "kosdaq")
        saved_market = scan.get("market", "both")
        idx_map = {"both": 0, "kospi": 1, "kosdaq": 2}
        self.market_combo.setCurrentIndex(idx_map.get(saved_market, 0))
        tg.addWidget(self.market_combo, 0, 1)

        tg.addWidget(QLabel("거래량 상위"), 1, 0)
        self.top_n_spin = QSpinBox()
        self.top_n_spin.setRange(50, 2000)
        self.top_n_spin.setValue(int(scan.get("top_n", 300)))
        self.top_n_spin.setSuffix(" 종목")
        tg.addWidget(self.top_n_spin, 1, 1)
        tg.addWidget(QLabel("전일 평균 거래량 기준 상위 N종목 탐색"),
                     1, 2, alignment=Qt.AlignLeft)

        v.addWidget(target_grp)

        # ── 조건 결합 ──────────────────────────────────────────────────────
        mode_grp = QGroupBox("조건 결합 방식")
        mg = QHBoxLayout(mode_grp)
        self.mode_and = QRadioButton("AND (모든 조건 만족)")
        self.mode_or  = QRadioButton("OR  (하나라도 만족)")
        bg = QButtonGroup(self)
        bg.addButton(self.mode_and); bg.addButton(self.mode_or)
        if scan.get("condition_mode", "AND") == "AND":
            self.mode_and.setChecked(True)
        else:
            self.mode_or.setChecked(True)
        mg.addWidget(self.mode_and); mg.addWidget(self.mode_or); mg.addStretch()
        v.addWidget(mode_grp)

        # ── RSI 조건 ───────────────────────────────────────────────────────
        rsi_grp = QGroupBox("RSI 조건")
        rsi_grp.setCheckable(True)
        rsi_grp.setChecked(bool(scan.get("rsi_enabled", True)))
        rg = QGridLayout(rsi_grp)

        rg.addWidget(QLabel("RSI 기간:"), 0, 0)
        self.rsi_period = QSpinBox(); self.rsi_period.setRange(2, 100)
        self.rsi_period.setValue(int(scan.get("rsi_period", 14)))
        rg.addWidget(self.rsi_period, 0, 1)

        rg.addWidget(QLabel("RSI 최솟값 (이상):"), 1, 0)
        self.rsi_min = QDoubleSpinBox(); self.rsi_min.setRange(0, 100); self.rsi_min.setDecimals(1)
        self.rsi_min.setValue(float(scan.get("rsi_min", 0)))
        rg.addWidget(self.rsi_min, 1, 1)

        rg.addWidget(QLabel("RSI 최댓값 (이하):"), 2, 0)
        self.rsi_max = QDoubleSpinBox(); self.rsi_max.setRange(0, 100); self.rsi_max.setDecimals(1)
        self.rsi_max.setValue(float(scan.get("rsi_max", 30)))
        rg.addWidget(self.rsi_max, 2, 1)

        rg.addWidget(QLabel("예) RSI 0~30: 과매도 구간 탐색"), 0, 2, 3, 1, Qt.AlignTop | Qt.AlignLeft)
        self.rsi_grp = rsi_grp
        v.addWidget(rsi_grp)

        # ── 이동평균 조건 ──────────────────────────────────────────────────
        ma_grp = QGroupBox("이동평균 조건")
        ma_grp.setCheckable(True)
        ma_grp.setChecked(bool(scan.get("ma_enabled", False)))
        mag = QGridLayout(ma_grp)

        mag.addWidget(QLabel("조건 유형:"), 0, 0)
        self.ma_cond_combo = QComboBox()
        self.ma_cond_combo.addItems(["현재가 > MA (MA 위)", "현재가 < MA (MA 아래)", "골든크로스 (단기 > 장기)"])
        cond_map = {"above": 0, "below": 1, "golden": 2}
        self.ma_cond_combo.setCurrentIndex(cond_map.get(scan.get("ma_condition", "above"), 0))
        self.ma_cond_combo.currentIndexChanged.connect(self._on_ma_cond_changed)
        mag.addWidget(self.ma_cond_combo, 0, 1, 1, 2)

        self.ma_period_lbl = QLabel("이평 기간:")
        mag.addWidget(self.ma_period_lbl, 1, 0)
        self.ma_period = QSpinBox(); self.ma_period.setRange(1, 200)
        self.ma_period.setValue(int(scan.get("ma_period", 20)))
        mag.addWidget(self.ma_period, 1, 1)

        self.ma_short_lbl = QLabel("단기 기간:")
        self.ma_short_spin = QSpinBox(); self.ma_short_spin.setRange(1, 100)
        self.ma_short_spin.setValue(int(scan.get("ma_short_period", 5)))
        self.ma_long_lbl = QLabel("장기 기간:")
        self.ma_long_spin = QSpinBox(); self.ma_long_spin.setRange(1, 200)
        self.ma_long_spin.setValue(int(scan.get("ma_long_period", 20)))

        mag.addWidget(self.ma_short_lbl, 2, 0)
        mag.addWidget(self.ma_short_spin, 2, 1)
        mag.addWidget(self.ma_long_lbl, 3, 0)
        mag.addWidget(self.ma_long_spin, 3, 1)

        self.ma_grp = ma_grp
        v.addWidget(ma_grp)
        self._on_ma_cond_changed(self.ma_cond_combo.currentIndex())

        # ── 거래량 조건 ────────────────────────────────────────────────────
        vol_grp = QGroupBox("거래량 조건")
        vol_grp.setCheckable(True)
        vol_grp.setChecked(bool(scan.get("volume_enabled", True)))
        vg = QGridLayout(vol_grp)

        vg.addWidget(QLabel("평균 기간 (일):"), 0, 0)
        self.vol_days = QSpinBox(); self.vol_days.setRange(1, 120)
        self.vol_days.setValue(int(scan.get("volume_avg_days", 20)))
        vg.addWidget(self.vol_days, 0, 1)

        vg.addWidget(QLabel("배수 (이상):"), 1, 0)
        self.vol_ratio = QDoubleSpinBox()
        self.vol_ratio.setRange(0.1, 100); self.vol_ratio.setDecimals(1)
        self.vol_ratio.setValue(float(scan.get("volume_ratio", 2.0)))
        self.vol_ratio.setSuffix(" 배")
        vg.addWidget(self.vol_ratio, 1, 1)

        vg.addWidget(QLabel("예) 20일 평균 거래량의 2배 이상이면 조건 만족"), 0, 2, 2, 1, Qt.AlignTop | Qt.AlignLeft)
        self.vol_grp = vol_grp
        v.addWidget(vol_grp)

        # ── 가격 돌파 조건 ─────────────────────────────────────────────────
        bo_grp = QGroupBox("가격 돌파 조건")
        bo_grp.setCheckable(True)
        bo_grp.setChecked(bool(scan.get("breakout_enabled", False)))
        bg2 = QGridLayout(bo_grp)

        bg2.addWidget(QLabel("기간 (일):"), 0, 0)
        self.bo_days = QSpinBox(); self.bo_days.setRange(1, 250)
        self.bo_days.setValue(int(scan.get("breakout_days", 20)))
        bg2.addWidget(self.bo_days, 0, 1)
        bg2.addWidget(QLabel("예) 20일 고가 돌파 시 조건 만족"), 0, 2, Qt.AlignLeft)
        self.bo_grp = bo_grp
        v.addWidget(bo_grp)

        # ── 수급 조건 ──────────────────────────────────────────────────────
        supply_grp = QGroupBox("수급 조건  ※ 활성화 시 종목당 TR 1회 추가 (탐색 시간 증가)")
        supply_grp.setCheckable(True)
        supply_grp.setChecked(bool(scan.get("supply_enabled", False)))
        sg = QGridLayout(supply_grp)

        sg.addWidget(QLabel("외국인 순매수 연속 (일):"), 0, 0)
        self.foreign_consec_spin = QSpinBox()
        self.foreign_consec_spin.setRange(0, 20)
        self.foreign_consec_spin.setValue(int(scan.get("foreign_consec_days", 3)))
        self.foreign_consec_spin.setSpecialValueText("사용 안 함")
        sg.addWidget(self.foreign_consec_spin, 0, 1)

        self.institution_turnover_chk = QCheckBox("기관 순매수 전환 (오늘 > 0)")
        self.institution_turnover_chk.setChecked(
            bool(scan.get("institution_turnover_enabled", True)))
        sg.addWidget(self.institution_turnover_chk, 1, 0, 1, 2)

        sg.addWidget(QLabel("예) 외국인 3일 연속 순매수\n+ 기관 순매수 전환"),
                     0, 2, 2, 1, Qt.AlignTop | Qt.AlignLeft)
        self.supply_grp = supply_grp
        v.addWidget(supply_grp)

        # ── 거래대금 조건 ──────────────────────────────────────────────────
        tv_grp = QGroupBox("거래대금 조건")
        tv_grp.setCheckable(True)
        tv_grp.setChecked(bool(scan.get("trading_value_enabled", False)))
        tvg = QGridLayout(tv_grp)

        tvg.addWidget(QLabel("거래대금 최소 (억원):"), 0, 0)
        self.tv_min_spin = QDoubleSpinBox()
        self.tv_min_spin.setRange(0, 100000)
        self.tv_min_spin.setDecimals(0)
        self.tv_min_spin.setValue(float(scan.get("trading_value_min_billion", 100)))
        self.tv_min_spin.setSuffix(" 억원")
        tvg.addWidget(self.tv_min_spin, 0, 1)

        self.tv_increase_chk = QCheckBox("거래대금 증가율 조건 사용")
        self.tv_increase_chk.setChecked(
            bool(scan.get("trading_value_increase_enabled", False)))
        tvg.addWidget(self.tv_increase_chk, 1, 0, 1, 2)
        self.tv_increase_chk.toggled.connect(self._on_tv_increase_toggled)

        tvg.addWidget(QLabel("증가율 기준 (이상):"), 2, 0)
        self.tv_ratio_spin = QDoubleSpinBox()
        self.tv_ratio_spin.setRange(100, 10000)
        self.tv_ratio_spin.setDecimals(0)
        self.tv_ratio_spin.setValue(float(scan.get("trading_value_increase_pct", 200)))
        self.tv_ratio_spin.setSuffix(" %")
        tvg.addWidget(self.tv_ratio_spin, 2, 1)

        tvg.addWidget(QLabel("평균 기간 (일):"), 3, 0)
        self.tv_avg_days_spin = QSpinBox()
        self.tv_avg_days_spin.setRange(1, 120)
        self.tv_avg_days_spin.setValue(int(scan.get("trading_value_avg_days", 20)))
        tvg.addWidget(self.tv_avg_days_spin, 3, 1)

        tvg.addWidget(QLabel("예) 거래대금 > 100억\n20일 평균 대비 200% 이상"),
                      0, 2, 4, 1, Qt.AlignTop | Qt.AlignLeft)
        self.tv_grp = tv_grp
        v.addWidget(tv_grp)
        self._on_tv_increase_toggled(self.tv_increase_chk.isChecked())

        # ── 추세 조건 (MA 기반) ────────────────────────────────────────────
        trend_grp = QGroupBox("추세 조건 (MA 기반)")
        trend_grp.setCheckable(True)
        trend_grp.setChecked(bool(scan.get("trend_enabled", False)))
        trendg = QVBoxLayout(trend_grp)

        self.trend_close_above_ma20_chk = QCheckBox("현재가 > MA20 (MA 위에 위치)")
        self.trend_close_above_ma20_chk.setChecked(bool(scan.get("trend_close_above_ma20", True)))
        trendg.addWidget(self.trend_close_above_ma20_chk)

        self.trend_ma20_rising_chk = QCheckBox("MA20 상승 중 (오늘 MA20 > 어제 MA20)")
        self.trend_ma20_rising_chk.setChecked(bool(scan.get("trend_ma20_rising", True)))
        trendg.addWidget(self.trend_ma20_rising_chk)

        self.trend_ma60_rising_chk = QCheckBox("MA60 상승 중 (오늘 MA60 > 어제 MA60)")
        self.trend_ma60_rising_chk.setChecked(bool(scan.get("trend_ma60_rising", False)))
        trendg.addWidget(self.trend_ma60_rising_chk)

        self.trend_ma20_above_ma60_chk = QCheckBox("MA20 > MA60 (정배열)")
        self.trend_ma20_above_ma60_chk.setChecked(bool(scan.get("trend_ma20_above_ma60", True)))
        trendg.addWidget(self.trend_ma20_above_ma60_chk)

        trend_low_row = QHBoxLayout()
        self.trend_low_above_ma20_chk = QCheckBox("저가 ≥ MA20 ×")
        self.trend_low_above_ma20_chk.setChecked(bool(scan.get("trend_low_above_ma20", False)))
        trend_low_row.addWidget(self.trend_low_above_ma20_chk)
        self.trend_low_ratio_spin = QDoubleSpinBox()
        self.trend_low_ratio_spin.setRange(0.7, 1.0)
        self.trend_low_ratio_spin.setDecimals(2)
        self.trend_low_ratio_spin.setSingleStep(0.01)
        self.trend_low_ratio_spin.setValue(float(scan.get("trend_low_ratio", 0.97)))
        trend_low_row.addWidget(self.trend_low_ratio_spin)
        trend_low_row.addStretch()
        trendg.addLayout(trend_low_row)

        self.trend_grp = trend_grp
        v.addWidget(trend_grp)

        # ── 눌림 조건 ──────────────────────────────────────────────────────
        pullback_grp = QGroupBox("눌림 조건")
        pullback_grp.setCheckable(True)
        pullback_grp.setChecked(bool(scan.get("pullback_enabled", False)))
        pbg = QGridLayout(pullback_grp)

        pbg.addWidget(QLabel("최근 기간 (일):"), 0, 0)
        self.pullback_days_spin = QSpinBox()
        self.pullback_days_spin.setRange(1, 30)
        self.pullback_days_spin.setValue(int(scan.get("pullback_days", 5)))
        pbg.addWidget(self.pullback_days_spin, 0, 1)

        pbg.addWidget(QLabel("MA 기간:"), 1, 0)
        self.pullback_ma_period_spin = QSpinBox()
        self.pullback_ma_period_spin.setRange(1, 120)
        self.pullback_ma_period_spin.setValue(int(scan.get("pullback_ma_period", 20)))
        pbg.addWidget(self.pullback_ma_period_spin, 1, 1)

        pbg.addWidget(QLabel("상한 배율 (≤ MA ×):"), 2, 0)
        self.pullback_ratio_spin = QDoubleSpinBox()
        self.pullback_ratio_spin.setRange(1.0, 1.2)
        self.pullback_ratio_spin.setDecimals(2)
        self.pullback_ratio_spin.setSingleStep(0.01)
        self.pullback_ratio_spin.setValue(float(scan.get("pullback_ratio", 1.02)))
        pbg.addWidget(self.pullback_ratio_spin, 2, 1)

        pbg.addWidget(QLabel("하한 배율 (≥ MA ×):"), 3, 0)
        self.pullback_lower_ratio_spin = QDoubleSpinBox()
        self.pullback_lower_ratio_spin.setRange(0.7, 1.0)
        self.pullback_lower_ratio_spin.setDecimals(2)
        self.pullback_lower_ratio_spin.setSingleStep(0.01)
        self.pullback_lower_ratio_spin.setValue(float(scan.get("pullback_lower_ratio", 0.97)))
        pbg.addWidget(self.pullback_lower_ratio_spin, 3, 1)

        self.pullback_volume_decrease_chk = QCheckBox("눌림 구간 거래량 감소 (최근 N일 평균 < 직전 N일 평균)")
        self.pullback_volume_decrease_chk.setChecked(bool(scan.get("pullback_volume_decrease_enabled", False)))
        pbg.addWidget(self.pullback_volume_decrease_chk, 4, 0, 1, 2)

        pbg.addWidget(QLabel("예) 최근 5일 내 저가가\nMA20×0.97 ~ MA20×1.02 범위"),
                      0, 2, 4, 1, Qt.AlignTop | Qt.AlignLeft)
        self.pullback_grp = pullback_grp
        v.addWidget(pullback_grp)

        # ── 기준봉 눌림 조건 (신규) ────────────────────────────────────────
        ref_grp = QGroupBox("기준봉 눌림 조건 (최근 N일 이내 큰 양봉 후 눌림)")
        ref_grp.setCheckable(True)
        ref_grp.setChecked(bool(scan.get("ref_candle_pullback_enabled", False)))
        rfg = QGridLayout(ref_grp)

        rfg.addWidget(QLabel("기준봉 탐색 기간 (일):"), 0, 0)
        self.ref_search_days_spin = QSpinBox()
        self.ref_search_days_spin.setRange(1, 20)
        self.ref_search_days_spin.setValue(int(scan.get("ref_candle_search_days", 5)))
        rfg.addWidget(self.ref_search_days_spin, 0, 1)

        rfg.addWidget(QLabel("최소 상승률 (%이상):"), 1, 0)
        self.ref_min_rise_spin = QDoubleSpinBox()
        self.ref_min_rise_spin.setRange(0.5, 30.0)
        self.ref_min_rise_spin.setDecimals(1)
        self.ref_min_rise_spin.setValue(float(scan.get("ref_candle_min_rise_pct", 3.0)))
        self.ref_min_rise_spin.setSuffix(" %")
        rfg.addWidget(self.ref_min_rise_spin, 1, 1)

        rfg.addWidget(QLabel("거래량 배수 (N일 평균 ×):"), 2, 0)
        self.ref_vol_mult_spin = QDoubleSpinBox()
        self.ref_vol_mult_spin.setRange(1.0, 20.0)
        self.ref_vol_mult_spin.setDecimals(1)
        self.ref_vol_mult_spin.setValue(float(scan.get("ref_candle_vol_multiplier", 2.0)))
        self.ref_vol_mult_spin.setSuffix(" 배")
        rfg.addWidget(self.ref_vol_mult_spin, 2, 1)

        rfg.addWidget(QLabel("거래량 평균 기간 (일):"), 3, 0)
        self.ref_vol_avg_days_spin = QSpinBox()
        self.ref_vol_avg_days_spin.setRange(5, 60)
        self.ref_vol_avg_days_spin.setValue(int(scan.get("ref_candle_vol_avg_days", 20)))
        rfg.addWidget(self.ref_vol_avg_days_spin, 3, 1)

        rfg.addWidget(QLabel("눌림 최대 비율 (기준봉 고가 ×):"), 4, 0)
        self.ref_pullback_ratio_spin = QDoubleSpinBox()
        self.ref_pullback_ratio_spin.setRange(0.7, 1.0)
        self.ref_pullback_ratio_spin.setDecimals(2)
        self.ref_pullback_ratio_spin.setSingleStep(0.01)
        self.ref_pullback_ratio_spin.setValue(float(scan.get("ref_candle_pullback_max_ratio", 0.97)))
        rfg.addWidget(self.ref_pullback_ratio_spin, 4, 1)

        rfg.addWidget(QLabel(
            "기준봉: 양봉 + 상승률≥N% + 거래량≥N배\n\n"
            "눌림: 현재가 ≤ 기준봉 고가 × 비율\n       현재가 ≥ 기준봉 시가"),
            0, 2, 5, 1, Qt.AlignTop | Qt.AlignLeft)
        self.ref_grp = ref_grp
        v.addWidget(ref_grp)

        # ── 종가 > 전일 종가 조건 (신규) ───────────────────────────────────
        close_prev_grp = QGroupBox("종가 > 전일 종가 조건")
        close_prev_grp.setCheckable(True)
        close_prev_grp.setChecked(bool(scan.get("close_above_prev_enabled", False)))
        cpg = QHBoxLayout(close_prev_grp)
        cpg.addWidget(QLabel("현재가(종가)가 전일 종가보다 높을 때 조건 만족"))
        self.close_prev_grp = close_prev_grp
        v.addWidget(close_prev_grp)

        # ── 최근 N일 고점 ±% 이내 지지 조건 (신규) ────────────────────────
        nhs_grp = QGroupBox("최근 N일 고점 ±% 이내 지지 조건")
        nhs_grp.setCheckable(True)
        nhs_grp.setChecked(bool(scan.get("near_high_support_enabled", False)))
        nhsg = QGridLayout(nhs_grp)

        nhsg.addWidget(QLabel("기간 (일):"), 0, 0)
        self.nhs_days_spin = QSpinBox()
        self.nhs_days_spin.setRange(1, 60)
        self.nhs_days_spin.setValue(int(scan.get("near_high_support_days", 10)))
        nhsg.addWidget(self.nhs_days_spin, 0, 1)

        nhsg.addWidget(QLabel("허용 범위 (%):"), 1, 0)
        self.nhs_pct_spin = QDoubleSpinBox()
        self.nhs_pct_spin.setRange(0.5, 20.0)
        self.nhs_pct_spin.setDecimals(1)
        self.nhs_pct_spin.setValue(float(scan.get("near_high_support_pct", 2.0)))
        self.nhs_pct_spin.setSuffix(" %")
        nhsg.addWidget(self.nhs_pct_spin, 1, 1)

        nhsg.addWidget(QLabel("예) 최근 10일 고점 ±2% 범위 내에\n현재가가 위치하면 조건 만족"),
                       0, 2, 2, 1, Qt.AlignTop | Qt.AlignLeft)
        self.nhs_grp = nhs_grp
        v.addWidget(nhs_grp)

        # ── 과도한 하락 방지 ───────────────────────────────────────────────
        floor_grp = QGroupBox("과도한 하락 방지")
        floor_grp.setCheckable(True)
        floor_grp.setChecked(bool(scan.get("price_floor_enabled", False)))
        flg = QGridLayout(floor_grp)

        flg.addWidget(QLabel("기간 (일):"), 0, 0)
        self.floor_days_spin = QSpinBox()
        self.floor_days_spin.setRange(1, 250)
        self.floor_days_spin.setValue(int(scan.get("price_floor_days", 20)))
        flg.addWidget(self.floor_days_spin, 0, 1)

        flg.addWidget(QLabel("비율 (이상):"), 1, 0)
        self.floor_ratio_spin = QDoubleSpinBox()
        self.floor_ratio_spin.setRange(0.5, 1.0)
        self.floor_ratio_spin.setDecimals(2)
        self.floor_ratio_spin.setSingleStep(0.01)
        self.floor_ratio_spin.setValue(float(scan.get("price_floor_ratio", 0.92)))
        flg.addWidget(self.floor_ratio_spin, 1, 1)

        flg.addWidget(QLabel("예) 종가 ≥ 20일 최고가 × 0.92"),
                      0, 2, 2, 1, Qt.AlignTop | Qt.AlignLeft)
        self.floor_grp = floor_grp
        v.addWidget(floor_grp)

        # ── 힘 유지 조건 ───────────────────────────────────────────────────
        strength_grp = QGroupBox("힘 유지 (최근 N일 내 종가가 이전 M일 최고가 돌파)")
        strength_grp.setCheckable(True)
        strength_grp.setChecked(bool(scan.get("strength_enabled", False)))
        stg = QGridLayout(strength_grp)

        stg.addWidget(QLabel("확인 기간 (일):"), 0, 0)
        self.strength_days_spin = QSpinBox()
        self.strength_days_spin.setRange(1, 60)
        self.strength_days_spin.setValue(int(scan.get("strength_days", 10)))
        stg.addWidget(self.strength_days_spin, 0, 1)

        stg.addWidget(QLabel("기준 기간 (일):"), 1, 0)
        self.strength_ref_days_spin = QSpinBox()
        self.strength_ref_days_spin.setRange(1, 120)
        self.strength_ref_days_spin.setValue(int(scan.get("strength_ref_days", 20)))
        stg.addWidget(self.strength_ref_days_spin, 1, 1)

        stg.addWidget(QLabel("예) 최근 10일 내 종가 >\n이전 20일 종가 최고가"),
                      0, 2, 2, 1, Qt.AlignTop | Qt.AlignLeft)
        self.strength_grp = strength_grp
        v.addWidget(strength_grp)

        # ── 반등 신호 조건 (OR 결합) ───────────────────────────────────────
        rebound_grp = QGroupBox("반등 신호 (아래 중 하나 이상 만족)")
        rebound_grp.setCheckable(True)
        rebound_grp.setChecked(bool(scan.get("rebound_enabled", False)))
        rbg = QVBoxLayout(rebound_grp)

        self.rebound_bullish_chk = QCheckBox("오늘 양봉 (종가 > 시가)")
        self.rebound_bullish_chk.setChecked(bool(scan.get("rebound_bullish_candle", True)))
        rbg.addWidget(self.rebound_bullish_chk)

        self.rebound_volume_chk = QCheckBox("거래량 증가 (당일 거래량 > 5일 평균)")
        self.rebound_volume_chk.setChecked(bool(scan.get("rebound_volume_increase", True)))
        rbg.addWidget(self.rebound_volume_chk)

        self.rebound_prev_high_chk = QCheckBox("전일 고가 돌파 (현재가 > 전일 고가)")
        self.rebound_prev_high_chk.setChecked(bool(scan.get("rebound_prev_high_breakout", True)))
        rbg.addWidget(self.rebound_prev_high_chk)

        self.rebound_grp = rebound_grp
        v.addWidget(rebound_grp)

        # ── 저장 버튼 ──────────────────────────────────────────────────────
        save_row = QHBoxLayout()
        save_row.addStretch()
        save_btn = QPushButton("설정 저장")
        save_btn.setMinimumWidth(120)
        save_btn.setStyleSheet("background:#2196F3; color:white; font-weight:bold;")
        save_btn.clicked.connect(self._save_settings)
        save_row.addWidget(save_btn)
        v.addLayout(save_row)

        v.addStretch()
        return scroll

    # ── 로그 프레임 ────────────────────────────────────────────────────────
    def _build_log_frame(self):
        frame = QGroupBox("로그")
        v = QVBoxLayout(frame)
        v.setContentsMargins(4, 4, 4, 4)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFixedHeight(110)
        f = QFont("Consolas", 9)
        self.log_text.setFont(f)
        v.addWidget(self.log_text)
        return frame

    # ──────────────────────────────────────────────────────────────────────
    # UI 이벤트 핸들러
    # ──────────────────────────────────────────────────────────────────────
    def _on_ma_cond_changed(self, idx: int):
        """골든크로스 선택 시 단기/장기 기간 입력 표시"""
        is_golden = (idx == 2)
        self.ma_period_lbl.setVisible(not is_golden)
        self.ma_period.setVisible(not is_golden)
        self.ma_short_lbl.setVisible(is_golden)
        self.ma_short_spin.setVisible(is_golden)
        self.ma_long_lbl.setVisible(is_golden)
        self.ma_long_spin.setVisible(is_golden)

    def _on_tv_increase_toggled(self, checked: bool):
        """거래대금 증가율 조건 사용 토글 시 관련 입력 표시/숨김"""
        self.tv_ratio_spin.setEnabled(checked)
        self.tv_avg_days_spin.setEnabled(checked)

    def _save_settings(self):
        """설정 저장"""
        cond_map = {0: "above", 1: "below", 2: "golden"}
        scan = {
            "market":            self.market_combo.currentData(),
            "condition_mode":    "AND" if self.mode_and.isChecked() else "OR",
            "top_n":             self.top_n_spin.value(),
            "rsi_enabled":       self.rsi_grp.isChecked(),
            "rsi_period":        self.rsi_period.value(),
            "rsi_min":           self.rsi_min.value(),
            "rsi_max":           self.rsi_max.value(),
            "ma_enabled":        self.ma_grp.isChecked(),
            "ma_period":         self.ma_period.value(),
            "ma_condition":      cond_map[self.ma_cond_combo.currentIndex()],
            "ma_short_period":   self.ma_short_spin.value(),
            "ma_long_period":    self.ma_long_spin.value(),
            "volume_enabled":    self.vol_grp.isChecked(),
            "volume_avg_days":   self.vol_days.value(),
            "volume_ratio":      self.vol_ratio.value(),
            "breakout_enabled":  self.bo_grp.isChecked(),
            "breakout_days":     self.bo_days.value(),
            # 수급 조건
            "supply_enabled":                self.supply_grp.isChecked(),
            "foreign_consec_days":           self.foreign_consec_spin.value(),
            "institution_turnover_enabled":  self.institution_turnover_chk.isChecked(),
            # 거래대금 조건
            "trading_value_enabled":         self.tv_grp.isChecked(),
            "trading_value_min_billion":     self.tv_min_spin.value(),
            "trading_value_increase_enabled": self.tv_increase_chk.isChecked(),
            "trading_value_increase_pct":    self.tv_ratio_spin.value(),
            "trading_value_avg_days":        self.tv_avg_days_spin.value(),
            # 추세 조건
            "trend_enabled":                 self.trend_grp.isChecked(),
            "trend_close_above_ma20":        self.trend_close_above_ma20_chk.isChecked(),
            "trend_ma20_rising":             self.trend_ma20_rising_chk.isChecked(),
            "trend_ma60_rising":             self.trend_ma60_rising_chk.isChecked(),
            "trend_ma20_above_ma60":         self.trend_ma20_above_ma60_chk.isChecked(),
            "trend_low_above_ma20":          self.trend_low_above_ma20_chk.isChecked(),
            "trend_low_ratio":               self.trend_low_ratio_spin.value(),
            # 눌림 조건
            "pullback_enabled":              self.pullback_grp.isChecked(),
            "pullback_days":                 self.pullback_days_spin.value(),
            "pullback_ma_period":            self.pullback_ma_period_spin.value(),
            "pullback_ratio":                self.pullback_ratio_spin.value(),
            "pullback_lower_ratio":          self.pullback_lower_ratio_spin.value(),
            "pullback_volume_decrease_enabled": self.pullback_volume_decrease_chk.isChecked(),
            # 과도한 하락 방지
            "price_floor_enabled":           self.floor_grp.isChecked(),
            "price_floor_days":              self.floor_days_spin.value(),
            "price_floor_ratio":             self.floor_ratio_spin.value(),
            # 힘 유지
            "strength_enabled":              self.strength_grp.isChecked(),
            "strength_days":                 self.strength_days_spin.value(),
            "strength_ref_days":             self.strength_ref_days_spin.value(),
            # 반등 신호
            "rebound_enabled":               self.rebound_grp.isChecked(),
            "rebound_bullish_candle":        self.rebound_bullish_chk.isChecked(),
            "rebound_volume_increase":       self.rebound_volume_chk.isChecked(),
            "rebound_prev_high_breakout":    self.rebound_prev_high_chk.isChecked(),
            # 기준봉 눌림 조건 (신규)
            "ref_candle_pullback_enabled":   self.ref_grp.isChecked(),
            "ref_candle_search_days":        self.ref_search_days_spin.value(),
            "ref_candle_min_rise_pct":       self.ref_min_rise_spin.value(),
            "ref_candle_vol_multiplier":     self.ref_vol_mult_spin.value(),
            "ref_candle_vol_avg_days":       self.ref_vol_avg_days_spin.value(),
            "ref_candle_pullback_max_ratio": self.ref_pullback_ratio_spin.value(),
            # 종가 > 전일 종가 (신규)
            "close_above_prev_enabled":      self.close_prev_grp.isChecked(),
            # 최근 N일 고점 지지 (신규)
            "near_high_support_enabled":     self.nhs_grp.isChecked(),
            "near_high_support_days":        self.nhs_days_spin.value(),
            "near_high_support_pct":         self.nhs_pct_spin.value(),
        }
        if self.config.save_scan(scan):
            self.log("[설정] 저장 완료")
            if self.scanner and self.scanner.is_running():
                self.scanner.apply_new_conditions()
            QMessageBox.information(self, "설정 저장", "설정이 저장되었습니다.")
        else:
            QMessageBox.warning(self, "저장 실패", "설정 저장에 실패했습니다.")

    # ──────────────────────────────────────────────────────────────────────
    # 로그인
    # ──────────────────────────────────────────────────────────────────────
    def _do_login(self):
        try:
            self.kiwoom = KiwoomAPI()
        except Exception as e:
            QMessageBox.critical(self, "오류", f"KiwoomAPI 초기화 실패:\n{e}")
            return

        self.log("[로그인] 키움 로그인 시도 중...")
        ok = self.kiwoom.login()
        if not ok:
            self.status_lbl.setText("연결 상태: 로그인 실패")
            self.status_lbl.setStyleSheet("color:red; font-weight:bold;")
            self.log("[로그인] 실패")
            return

        self.status_lbl.setText("연결 상태: 연결됨")
        self.status_lbl.setStyleSheet("color:green; font-weight:bold;")
        self.login_btn.setEnabled(False)

        accounts = self.kiwoom.get_login_info("ACCLIST").split(";")
        accounts = [a for a in accounts if a]
        self.account_combo.addItems(accounts)
        if accounts:
            self.config.set_account_number(accounts[0])

        self.log(f"[로그인] 성공 — 계좌 {len(accounts)}개")

        # 종목 캐시 로딩
        self.log("[로그인] 종목 캐시 로딩 중...")
        self.kiwoom.load_stock_cache()
        self.log("[로그인] 종목 캐시 완료")

        self.scan_btn.setEnabled(True)

    # ──────────────────────────────────────────────────────────────────────
    # 자동탐색 토글
    # ──────────────────────────────────────────────────────────────────────
    def _toggle_scan(self):
        if self.scanner and self.scanner.is_running():
            self._stop_scan()
        else:
            self._start_scan()

    def _start_scan(self):
        # ── 진행 다이얼로그 생성 ───────────────────────────────────────────
        self._progress_dlg = ScanProgressDialog(
            cancel_callback=self._stop_scan,
            parent=self,
        )
        self._progress_dlg.show()

        # ── Scanner 생성 (새 API) ─────────────────────────────────────────
        self.scanner = Scanner(
            kiwoom      = self.kiwoom,
            config      = self.config,
            log_cb      = self.log,
            progress_cb = self._on_scan_progress,
            result_cb   = self._on_scan_result,
            done_cb     = self._on_scan_done,
        )
        self.scanner.start()

        self.scan_btn.setText("자동탐색 중지")
        self.scan_btn.setStyleSheet(
            "background:#f44336; color:white; font-weight:bold; font-size:11pt;"
        )
        self.progress_bar.setFormat("스캔 중...")
        self.log("[탐색] 자동탐색 시작")

        self._countdown_timer.start(1000)
        self._tick_countdown()

    def _stop_scan(self):
        if self.scanner:
            self.scanner.stop()
        self._countdown_timer.stop()
        self.countdown_lbl.setText("")
        if self._progress_dlg:
            self._progress_dlg.hide()
            self._progress_dlg = None
        self.scan_btn.setText("자동탐색 시작")
        self.scan_btn.setStyleSheet(
            "background:#4CAF50; color:white; font-weight:bold; font-size:11pt;"
        )
        self.progress_bar.setFormat("중지됨")
        self.progress_bar.setValue(0)
        self.log("[탐색] 자동탐색 중지")

    # ──────────────────────────────────────────────────────────────────────
    # 스캔 진행 / 결과 처리
    # ──────────────────────────────────────────────────────────────────────
    def _on_scan_progress(self, phase: str, done: int, total: int, name: str):
        """Scanner.progress_cb — 진행 다이얼로그 & 진행 바 동시 갱신."""
        # 진행 다이얼로그
        if self._progress_dlg:
            self._progress_dlg.update(phase, done, total, name)

        # 탐색종목 탭 진행 바
        if total > 0:
            pct = int(done / total * 100)
            self.progress_bar.setValue(pct)
            self.progress_bar.setFormat(f"{phase}  {done}/{total}")
            self.progress_lbl.setText(name)

    def _on_scan_done(self):
        """Scanner.done_cb — 스캔 1회 완료."""
        if self._progress_dlg:
            self._progress_dlg.mark_done()
        self.progress_bar.setFormat("완료")
        self.progress_lbl.setText("")
        self._tick_countdown()

    def _on_scan_result(self, results: list):
        """Scanner.result_cb — 조건 만족 종목 목록 수신 → 테이블 갱신."""
        self._result_cache = {r["code"]: r for r in results}
        self._refresh_table()

        now = datetime.now().strftime("%H:%M:%S")
        self.last_scan_lbl.setText(f"마지막 갱신: {now}")
        self.result_count_lbl.setText(f"탐색 결과: {len(results)}종목")
        self.log(f"[결과] {len(results)}종목 탐색됨 ({now})")

    def _refresh_table(self):
        """_result_cache → result_table 전체 재렌더"""
        rows = list(self._result_cache.values())
        self.result_table.setSortingEnabled(False)
        self.result_table.setRowCount(len(rows))

        for r_idx, row in enumerate(rows):
            code   = row.get("code", "")
            name   = row.get("name", "")
            price  = row.get("price", 0)
            rsi    = row.get("rsi")
            ma     = row.get("ma")
            ma_s   = row.get("ma_short")
            vr     = row.get("volume_ratio")
            bo     = row.get("breakout", False)
            supply_ok = row.get("supply_ok", False)
            supply_data_available = row.get("supply_data_available", True)
            tv     = row.get("trading_value")
            tv_ratio = row.get("trading_value_ratio")

            # 종목명 (코드)
            self._set(r_idx, 0, f"{name} ({code})")

            # 현재가
            self._set(r_idx, 1, f"{int(price):,}" if price else "-", align=Qt.AlignRight | Qt.AlignVCenter)

            # RSI
            rsi_txt = f"{rsi:.1f}" if rsi is not None else "-"
            item_rsi = self._make_item(rsi_txt, align=Qt.AlignCenter)
            if rsi is not None:
                if rsi <= 30:
                    item_rsi.setForeground(QColor("#1565C0"))   # 파랑 (과매도)
                elif rsi >= 70:
                    item_rsi.setForeground(QColor("#c62828"))   # 빨강 (과매수)
            self.result_table.setItem(r_idx, 2, item_rsi)

            # 이평(MA)
            if ma_s is not None and ma is not None:
                ma_txt = f"단{int(ma_s):,} / 장{int(ma):,}"
            elif ma is not None:
                dir_txt = "↑" if price > ma else "↓"
                ma_txt = f"{int(ma):,} {dir_txt}"
            else:
                ma_txt = "-"
            self._set(r_idx, 3, ma_txt, align=Qt.AlignCenter)

            # 거래량비
            vr_txt = f"{vr:.1f}x" if vr is not None else "-"
            item_vr = self._make_item(vr_txt, align=Qt.AlignCenter)
            if vr is not None and vr >= float(self.config.get("scan", "volume_ratio") or 2):
                item_vr.setForeground(QColor("#e65100"))   # 주황
            self.result_table.setItem(r_idx, 4, item_vr)

            # 돌파
            bo_txt = "●" if bo else "-"
            item_bo = self._make_item(bo_txt, align=Qt.AlignCenter)
            if bo:
                item_bo.setForeground(QColor("#2e7d32"))
            self.result_table.setItem(r_idx, 5, item_bo)

            # 수급
            supply_enabled = bool(self.config.get("scan", "supply_enabled"))
            if not supply_enabled:
                supply_txt = "-"
                supply_color = None
            elif not supply_data_available:
                supply_txt = "?"
                supply_color = QColor("#ef6c00")
            elif supply_ok:
                supply_txt = "●"
                supply_color = QColor("#6a1b9a")   # 보라
            else:
                supply_txt = "✕"
                supply_color = QColor("#9e9e9e")
            item_sup = self._make_item(supply_txt, align=Qt.AlignCenter)
            if supply_color:
                item_sup.setForeground(supply_color)
            self.result_table.setItem(r_idx, 6, item_sup)

            # 거래대금
            tv_enabled = bool(self.config.get("scan", "trading_value_enabled"))
            if not tv_enabled or tv is None:
                tv_txt = "-"
            else:
                tv_bil = tv / 100
                if tv_ratio is not None:
                    tv_txt = f"{tv_bil:,.0f}억 ({tv_ratio:.0f}%)"
                else:
                    tv_txt = f"{tv_bil:,.0f}억"
            item_tv = self._make_item(tv_txt, align=Qt.AlignRight | Qt.AlignVCenter)
            if tv_enabled and row.get("trading_value_ok"):
                item_tv.setForeground(QColor("#1565C0"))
            self.result_table.setItem(r_idx, 7, item_tv)

            # 조건 요약
            conds = []
            if row.get("rsi_ok"):           conds.append("RSI")
            if row.get("ma_ok"):            conds.append("MA")
            if row.get("volume_ok"):        conds.append("거래량")
            if row.get("breakout"):         conds.append("돌파")
            if row.get("supply_ok"):        conds.append("수급")
            if row.get("trading_value_ok"): conds.append("거래대금")
            if row.get("trend_ok"):         conds.append("추세")
            if row.get("pullback_ok"):      conds.append("눌림")
            if row.get("price_floor_ok"):   conds.append("하락방지")
            if row.get("strength_ok"):      conds.append("힘유지")
            if row.get("rebound_ok"):           conds.append("반등")
            if row.get("ref_candle_ok"):        conds.append("기준봉눌림")
            if row.get("close_above_prev_ok"):  conds.append("전일종가↑")
            if row.get("near_high_support_ok"): conds.append("고점지지")
            self._set(r_idx, 8, " / ".join(conds) if conds else "-")

        self.result_table.setSortingEnabled(True)

    def _set(self, r, c, text, align=Qt.AlignLeft | Qt.AlignVCenter):
        self.result_table.setItem(r, c, self._make_item(text, align))

    @staticmethod
    def _make_item(text, align=Qt.AlignLeft | Qt.AlignVCenter):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(align)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        return item

    # ──────────────────────────────────────────────────────────────────────
    # 카운트다운
    # ──────────────────────────────────────────────────────────────────────
    def _tick_countdown(self):
        if not self.scanner or not self.scanner.is_running():
            self.countdown_lbl.setText("")
            return

        if self.scanner.is_scanning():
            elapsed = self.scanner.get_scan_elapsed_seconds()
            m, s = divmod(elapsed, 60)
            self.countdown_lbl.setText(f"탐색 경과: {m:02d}:{s:02d}  |  완료 후 02:00:00 뒤 갱신")
            return

        remaining = self.scanner.get_next_refresh_remaining_seconds()
        if remaining is None:
            self.countdown_lbl.setText("다음 갱신 예약 중...")
            return

        m, s = divmod(remaining, 60)
        self.countdown_lbl.setText(f"다음 갱신: {m:02d}:{s:02d}")

    # ──────────────────────────────────────────────────────────────────────
    # 로그
    # ──────────────────────────────────────────────────────────────────────
    def log(self, msg: str):
        """스레드 안전 로그 (시그널 경유)"""
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_signal.emit(f"[{ts}] {msg}")

    def _append_log(self, msg: str):
        self._log_buf.append(msg)

    def _flush_log(self):
        if not self._log_buf:
            return
        lines = []
        while self._log_buf:
            lines.append(self._log_buf.popleft())
        self.log_text.append("\n".join(lines))
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ──────────────────────────────────────────────────────────────────────
    # 종료
    # ──────────────────────────────────────────────────────────────────────
    def closeEvent(self, e):
        if self.scanner and self.scanner.is_running():
            self.scanner.stop()
        if self._progress_dlg:
            self._progress_dlg.hide()
        e.accept()


# ──────────────────────────────────────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 이용 약관 동의
    dlg = DisclaimerDialog()
    if dlg.exec_() != QDialog.Accepted:
        sys.exit(0)

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
