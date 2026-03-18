# -*- coding: utf-8 -*-
"""
종목 스캔 엔진 (메인 스레드 + TRQueue 기반)

동작 흐름
─────────────────────────────────────────────────────────────────
Phase 1 (최초 or 탐색 조건 변경 후)
  ① opt10030 TR로 거래량 상위 top_n 종목 코드 수집
  ② 일봉 배치 조회 (TRQueue, 250ms 간격)
     - 캐시(15분 TTL) 있는 종목은 즉시 처리 (TR 호출 없음)
  ③ 상위 N종목 목록을 top_codes_cache.json 에 저장

Phase 2 (15분 자동 갱신)
  ① 저장된 상위 N종목만 일봉 갱신
  ② 조건 평가 → 결과 발행
─────────────────────────────────────────────────────────────────
TR 호출은 kiwoom.tr_queue(TRQueue) 를 통해 메인 스레드에서 순차 실행.
QThread 사용 없음.
"""

import json
import os
import time
from datetime import datetime

from PyQt5.QtCore import QTimer

from technical_analysis import TechnicalAnalysis

# ── 상수 ─────────────────────────────────────────────────────────────────────
_SCREEN_BASE      = 3000              # 실시간 등록 화면번호 시작
_MAX_PER_SCREEN   = 100               # 화면당 최대 등록 종목
_CANDLE_COUNT     = 70                # 일봉 요청 개수 (RSI14 + MA60 여유)
_CANDLE_TTL       = 900               # 메모리 캐시 유효 시간 (초, 15분)
_CANDLE_FILE_TTL  = 86400             # 파일 캐시 유효 시간 (초, 24시간)
_RT_FIDS          = "10;13"           # 현재가(10); 누적거래량(13)
_TOP_CODES_FILE   = "top_codes_cache.json"
_CANDLE_CACHE_FILE = "candle_cache.json"

# ── 모듈 레벨 공유 캐시 ───────────────────────────────────────────────────────
CANDLE_CACHE: dict = {}   # code → {"data": list, "ts": float}
RT_CACHE:     dict = {}   # code → (price: int, cum_vol: int)
_candle_file_loaded = False   # 파일 캐시 1회 로드 여부


# ── 캔들 캐시 파일 I/O ─────────────────────────────────────────────────────────
def _load_candle_cache_from_file():
    """프로그램 시작 시 파일에서 캔들 캐시를 메모리로 로드."""
    global CANDLE_CACHE
    try:
        if not os.path.exists(_CANDLE_CACHE_FILE):
            return
        with open(_CANDLE_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        now = time.time()
        loaded = 0
        for code, entry in raw.items():
            # 24시간 이내 데이터만 로드
            if now - entry.get("ts", 0) < _CANDLE_FILE_TTL:
                CANDLE_CACHE[code] = entry
                loaded += 1
        print(f"[캔들캐시] 파일에서 {loaded}개 로드 ({len(raw) - loaded}개 만료 제거)")
    except Exception as e:
        print(f"[캔들캐시] 파일 로드 실패: {e}")


def _save_candle_cache_to_file(top_codes: list):
    """상위 N 종목의 캔들만 파일로 저장 (재시작 후 재사용)."""
    try:
        to_save = {
            code: CANDLE_CACHE[code]
            for code in top_codes
            if code in CANDLE_CACHE
        }
        with open(_CANDLE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False)
        print(f"[캔들캐시] {len(to_save)}개 파일 저장 완료")
    except Exception as e:
        print(f"[캔들캐시] 파일 저장 실패: {e}")


# ── 조건 지문 (fingerprint) ────────────────────────────────────────────────────
def _get_fingerprint(conditions: dict) -> str:
    """랭킹에 영향을 주는 조건(시장·종목수)으로 지문 생성."""
    return f"{conditions.get('market', 'both')}_{conditions.get('top_n', 300)}"


# ── 상위 종목 캐시 I/O ─────────────────────────────────────────────────────────
def _load_top_codes_cache() -> tuple:
    """(codes: list, fingerprint: str) 반환."""
    try:
        if os.path.exists(_TOP_CODES_FILE):
            with open(_TOP_CODES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("codes", []), data.get("fingerprint", "")
    except Exception:
        pass
    return [], ""


def _save_top_codes_cache(codes: list, fingerprint: str):
    try:
        with open(_TOP_CODES_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {"codes": codes, "ts": time.time(), "fingerprint": fingerprint},
                f, ensure_ascii=False,
            )
    except Exception:
        pass


def _make_row(code: str, name: str, price: int, ev: dict) -> dict:
    return {
        "code":         code,
        "name":         name,
        "price":        price,
        "rsi":          ev.get("rsi"),
        "ma":           ev.get("ma"),
        "ma_short":     ev.get("ma_short"),
        "volume_ratio": ev.get("volume_ratio"),
        "breakout":     ev.get("breakout_ok", False),
        "rsi_ok":       ev.get("rsi_ok", False),
        "ma_ok":        ev.get("ma_ok", False),
        "volume_ok":    ev.get("volume_ok", False),
    }


# ─────────────────────────────────────────────────────────────────────────────
class Scanner:
    """
    메인 스레드 기반 종목 탐색 컨트롤러

    Callbacks (모두 선택)
    ─────────────────────
    log_cb(msg: str)
    progress_cb(phase: str, done: int, total: int, name: str)
    result_cb(results: list[dict])
    done_cb()          스캔 완료(평가 직후)
    """

    AUTO_REFRESH_MS  = 15 * 60 * 1000  # 15분
    _file_cache_loaded = False            # 클래스 레벨 플래그 (프로세스 내 1회)

    def __init__(self, kiwoom, config,
                 log_cb=None, progress_cb=None, result_cb=None, done_cb=None):
        self.kiwoom       = kiwoom
        self.config       = config
        self._log_cb      = log_cb
        self._progress_cb = progress_cb
        self._result_cb   = result_cb
        self._done_cb     = done_cb

        self._is_running   = False
        self._is_scanning  = False
        self._cancelled    = False
        self._phase        = "idle"   # "phase1" | "phase2" | "idle"

        self._top_codes: list  = []
        self._avg_volumes: dict = {}
        self._tr_total = 0
        self._tr_done  = 0
        self._registered_screens: list = []
        self._watch_codes: list = []

        # opt10030 수집 상태
        self._opt10030_target = 0
        self._opt10030_params: dict = {}
        self._opt10030_codes: list = []
        self._opt10030_pages = 0

        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._on_auto_refresh)

        self._rt_eval_timer = QTimer()
        self._rt_eval_timer.setSingleShot(True)
        self._rt_eval_timer.timeout.connect(self._reevaluate_realtime)

        self._ta = TechnicalAnalysis()
        self.kiwoom.on_real_data_callback = self._on_realtime

        # 프로세스 내 최초 1회: 파일 캐시 → 메모리 로드
        if not Scanner._file_cache_loaded:
            _load_candle_cache_from_file()
            Scanner._file_cache_loaded = True

    # ── 외부 인터페이스 ────────────────────────────────────────────────────────
    def start(self):
        """자동탐색 시작.

        조건 지문(시장+종목수) 비교 결과:
          - 지문 일치 + 저장된 top_codes 존재 → Phase2 (상위 N만 갱신)
          - 지문 불일치 or 캐시 없음           → Phase1 (거래량 상위 수집 + 재스캔)
        """
        if self._is_running:
            return
        self._is_running = True
        self._cancelled  = False

        conditions   = self.config.get_scan()
        fp_current   = _get_fingerprint(conditions)
        saved_codes, fp_saved = _load_top_codes_cache()

        if saved_codes and fp_current == fp_saved:
            self._top_codes = saved_codes
            self._log(
                f"[캐시] 저장된 상위 {len(saved_codes)}종목 로드"
                f" (조건 동일: {fp_current}) → Phase2 갱신"
            )
            self._run_phase2()
        else:
            if saved_codes and fp_current != fp_saved:
                self._log(
                    f"[Phase1] 탐색 조건 변경 감지"
                    f" ({fp_saved} → {fp_current}) → 전체 재스캔"
                )
            else:
                self._log("[Phase1] 저장된 캐시 없음 → 전체 스캔 시작")
            self._run_phase1()

        self._refresh_timer.start(self.AUTO_REFRESH_MS)

    def stop(self):
        """자동탐색 중지."""
        self._is_running  = False
        self._is_scanning = False
        self._cancelled   = True
        self._phase       = "idle"
        self._refresh_timer.stop()
        self._rt_eval_timer.stop()
        self.kiwoom.tr_queue.clear()    # 대기 중인 TR 모두 제거
        self._unregister_realtime()
        self._log("자동탐색 중지")

    def is_running(self) -> bool:
        return self._is_running

    def is_scanning(self) -> bool:
        return self._is_scanning

    # ── Phase 1: 거래량 상위 수집 (opt10030) ─────────────────────────────────
    def _run_phase1(self):
        self._phase        = "phase1"
        self._is_scanning  = True
        self._avg_volumes  = {}
        self._start_opt10030_fetch()

    @staticmethod
    def _build_opt10030_params(market: str) -> dict:
        """market 설정 → opt10030 기본 파라미터 자동 생성."""
        market_code = {"kospi": "001", "kosdaq": "101"}.get(market, "000")
        return {
            "시장구분":     market_code,  # 000=전체, 001=코스피, 101=코스닥
            "정렬구분":     "1",          # 1=거래량순
            "관리종목포함": "1",          # 1=미포함
            "신용구분":     "0",          # 0=전체
            "거래량구분":   "5",          # 5만주 이상
            "종목조건":     "1",          # 1=관리종목 제외
            "대상":         "0",          # 0=전체
            "가격조건":     "0",          # 0=전체
            "거래대금조건": "0",          # 0=전체
        }

    def _start_opt10030_fetch(self):
        """opt10030로 거래량 상위 top_n 종목 코드 수집."""
        params = self.config.get("scan", "opt10030_params") or {}
        if not params:
            market = self.config.get("scan", "market") or "both"
            params = self._build_opt10030_params(market)
            self._log(f"[Phase1] opt10030_params 미설정 → market='{market}' 기본값 사용")

        self._opt10030_target = int(self.config.get("scan", "top_n") or 300)
        self._opt10030_params = params   # 결정된 params 저장 (다음 페이지에서 재사용)
        self._opt10030_codes = []
        self._opt10030_pages = 0
        self._request_opt10030_page(prev_next=0)

    def _request_opt10030_page(self, prev_next=0):
        if self._cancelled:
            return
        self._opt10030_pages += 1
        params = self._opt10030_params
        self.kiwoom.tr_queue.enqueue(
            self.kiwoom.get_volume_top_opt10030,
            lambda result, pn=prev_next: self._on_opt10030_page(result),
            params, prev_next,
        )

    def _on_opt10030_page(self, result: dict):
        if self._cancelled:
            return

        rows = (result or {}).get("rows", []) or []
        next_flag = str((result or {}).get("next", "")).strip().upper()

        for row in rows:
            code = (row.get("code") or "").strip()
            if code:
                self._opt10030_codes.append(code)

        done = min(len(self._opt10030_codes), self._opt10030_target)
        if self._progress_cb:
            name = rows[-1].get("name", "") if rows else ""
            self._progress_cb("거래량 상위 수집", done, self._opt10030_target, name)

        has_next = next_flag in ("Y", "2", "TRUE", "T", "1")
        if done < self._opt10030_target and has_next:
            self._request_opt10030_page(prev_next="Y")
            return

        # Phase1 완료 → 상위 N 확정 후 Phase2 진행
        self._top_codes = self._opt10030_codes[: self._opt10030_target]
        fp = _get_fingerprint(self.config.get_scan())
        _save_top_codes_cache(self._top_codes, fp)
        self._log(f"[Phase1] opt10030 완료 → 상위 {len(self._top_codes)}종목 저장 (지문 {fp})")

        self._run_phase2()

    # ── Phase 2: 상위 N종목 일봉 갱신 ────────────────────────────────────────
    def _run_phase2(self):
        self._log(f"[Phase2] 상위 {len(self._top_codes)}종목 일봉 갱신")
        self._phase        = "phase2"
        self._is_scanning  = True
        self._avg_volumes  = {}
        self._start_fetch(self._top_codes)

    # ── 공통 fetch 로직 (TRQueue 기반) ───────────────────────────────────────
    def _start_fetch(self, codes: list):
        """
        일봉을 TRQueue 로 순차 조회.
        캐시(15분) 유효한 종목은 즉시 처리 (TR 호출 없음).
        """
        self._tr_done  = 0
        self._avg_volumes = {}

        now      = time.time()
        uncached = []
        for code in codes:
            entry = CANDLE_CACHE.get(code)
            if entry and (now - entry["ts"]) < _CANDLE_TTL:
                # 캐시 hit → 즉시 처리 (TR 없음)
                self._process_candle(code, entry["data"])
            else:
                uncached.append(code)

        cached_cnt = len(codes) - len(uncached)
        self._tr_total = len(uncached)
        self._log(
            f"[TR] 일봉 {len(uncached)}개 조회 예정"
            f" (캐시 재사용 {cached_cnt}개)"
        )

        if not uncached:
            self._on_all_fetched()
            return

        # TRQueue 에 일괄 enqueue (내부에서 250ms 간격 순차 실행)
        for code in uncached:
            self.kiwoom.tr_queue.enqueue(
                self.kiwoom.get_daily_candles,
                # lambda default-arg 캡처로 클로저 버그 방지
                lambda candles, c=code: self._on_tr_callback(c, candles),
                code, _CANDLE_COUNT,
            )

    def _on_tr_callback(self, code: str, candles):
        """TRQueue 콜백 — 일봉 TR 수신 후 호출됨 (메인 스레드)."""
        if self._cancelled:
            return

        if candles:
            CANDLE_CACHE[code] = {"data": candles, "ts": time.time()}
        self._process_candle(code, candles or [])

        self._tr_done += 1
        name  = self.kiwoom.get_stock_name_from_cache(code) or code
        phase = "일봉 조회" if self._phase == "phase2" else "전체 스캔"
        if self._progress_cb:
            self._progress_cb(phase, self._tr_done, self._tr_total, name)

        if self._tr_done >= self._tr_total:
            self._on_all_fetched()

    def _process_candle(self, code: str, candles: list):
        """일봉에서 평균 거래량 추출 (랭킹용)."""
        if candles and len(candles) > 1:
            vols = [c.get("volume", 0) for c in candles[1:21]]
            self._avg_volumes[code] = sum(vols) / len(vols) if vols else 0
        else:
            self._avg_volumes[code] = 0

    def _on_all_fetched(self):
        """일봉 fetch 완료 후 조건 평가 및 결과 발행."""
        if self._cancelled:
            return

        self._is_scanning = False
        self._phase       = "idle"

        # 캔들 캐시 파일 저장 (상위 N만)
        _save_candle_cache_to_file(self._top_codes)

        # 조건 평가 → 결과 발행
        self._evaluate_and_emit()

        if self._done_cb:
            self._done_cb()

    def _evaluate_and_emit(self):
        conditions = self.config.get_scan()
        results    = []

        for code in self._top_codes:
            name    = self.kiwoom.get_stock_name_from_cache(code) or code
            entry   = CANDLE_CACHE.get(code)
            candles = entry["data"] if entry else []
            rt      = RT_CACHE.get(code)
            price     = rt[0] if rt else (candles[0].get("close",  0) if candles else 0)
            today_vol = rt[1] if rt else (candles[0].get("volume", 0) if candles else 0)

            ev = self._ta.evaluate(candles, price, today_vol, conditions)
            if ev["match"]:
                results.append(_make_row(code, name, price, ev))

        # 조건 만족 종목만 실시간 감시
        self._watch_codes = [r["code"] for r in results]
        self._register_realtime(self._watch_codes)

        self._log(f"[평가] 조건 만족 {len(results)}개 / {len(self._top_codes)}개")
        if self._result_cb:
            self._result_cb(results)

    # ── 15분 자동 갱신 ─────────────────────────────────────────────────────────
    def _on_auto_refresh(self):
        if not self._is_running or self._is_scanning:
            return
        self._log("[자동갱신] 15분 경과 → Phase2 갱신 시작")
        self._cancelled = False
        self._run_phase2()

    # ── 실시간 등록/해제 ───────────────────────────────────────────────────────
    def _register_realtime(self, codes=None):
        self._unregister_realtime()
        codes = codes or []
        screens = []
        for i, start in enumerate(range(0, len(codes), _MAX_PER_SCREEN)):
            chunk = codes[start: start + _MAX_PER_SCREEN]
            sn    = str(_SCREEN_BASE + i)
            self.kiwoom.set_real_reg(sn, ";".join(chunk), _RT_FIDS, "0")
            screens.append(sn)
        self._registered_screens = screens
        self._log(f"[실시간] {len(codes)}종목 등록 ({len(screens)}개 화면)")

    def _unregister_realtime(self):
        for sn in self._registered_screens:
            self.kiwoom.set_real_remove(sn, "ALL")
        self._registered_screens = []

    # ── 실시간 가격·거래량 수신 ──────────────────────────────────────────────
    def _on_realtime(self, code: str, price: int, cum_vol: int):
        RT_CACHE[code] = (price, cum_vol)
        if not self._rt_eval_timer.isActive():
            self._rt_eval_timer.start(500)

    def _reevaluate_realtime(self):
        """실시간 값으로 조건 재평가 (TR 없음, 캐시 일봉 사용)."""
        if not self._is_running or self._is_scanning:
            return
        self._evaluate_and_emit()

    # ── 유틸 ───────────────────────────────────────────────────────────────────
    def _log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}] {msg}"
        print(full)
        if self._log_cb:
            self._log_cb(full)
