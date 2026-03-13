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
    QCheckBox, QDialog, QRadioButton, QButtonGroup
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
        self.result_table.setColumnCount(7)
        self.result_table.setHorizontalHeaderLabels([
            "종목명 (코드)", "현재가", "RSI", "이평(MA)", "거래량비", "돌파", "조건"
        ])
        hdr = self.result_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 7):
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
        w = QWidget()
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
        return w

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

        self._countdown = 15 * 60
        self._countdown_timer.start(1000)

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

    def _on_scan_result(self, results: list):
        """Scanner.result_cb — 조건 만족 종목 목록 수신 → 테이블 갱신."""
        self._result_cache = {r["code"]: r for r in results}
        self._refresh_table()

        now = datetime.now().strftime("%H:%M:%S")
        self.last_scan_lbl.setText(f"마지막 갱신: {now}")
        self.result_count_lbl.setText(f"탐색 결과: {len(results)}종목")
        self.log(f"[결과] {len(results)}종목 탐색됨 ({now})")

        self._countdown = 15 * 60

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

            # 조건 요약
            conds = []
            if row.get("rsi_ok"):    conds.append("RSI")
            if row.get("ma_ok"):     conds.append("MA")
            if row.get("volume_ok"): conds.append("거래량")
            if row.get("breakout"):  conds.append("돌파")
            self._set(r_idx, 6, " / ".join(conds) if conds else "-")

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
        if self._countdown > 0:
            self._countdown -= 1
        m, s = divmod(self._countdown, 60)
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
