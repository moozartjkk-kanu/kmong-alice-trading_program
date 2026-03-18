# -*- coding: utf-8 -*-
"""
키움증권 종목 탐색 시스템 - 설정 파일
"""
import json
import os

CONFIG_FILE = "scan_config.json"

DEFAULT_CONFIG = {
    # 키움 API 설정
    "kiwoom": {
        "account_number": "",
        "use_mock_trading": False,
    },

    # 스캔 조건 설정
    "scan": {
        # 조건 결합 방식: "AND" (모두 만족) | "OR" (하나라도 만족)
        "condition_mode": "AND",

        # 탐색 시장: "kospi" | "kosdaq" | "both"
        "market": "both",

        # 탐색 대상 종목 수 (거래량 상위)
        "top_n": 150,

        # OPT10030(거래량 상위) 입력값
        # 키움 매뉴얼 기준 입력 파라미터를 그대로 사용 (환경에 맞게 수정 필요)
        # 예: {"시장구분": "000", "정렬구분": "1", "관리종목포함": "1", ...}
        "opt10030_params": {},

        # RSI 조건
        "rsi_enabled": True,
        "rsi_period": 14,
        "rsi_min": 0,       # RSI >= rsi_min
        "rsi_max": 30,      # RSI <= rsi_max

        # 이동평균 조건
        "ma_enabled": False,
        "ma_period": 20,                # 5 | 20 | 60
        "ma_condition": "above",        # "above"=현재가>MA, "below"=현재가<MA, "golden"=골든크로스(5>20)
        "ma_short_period": 5,           # 골든크로스용 단기
        "ma_long_period": 20,           # 골든크로스용 장기

        # 거래량 조건
        "volume_enabled": True,
        "volume_avg_days": 20,          # N일 평균 거래량 기준
        "volume_ratio": 2.0,            # 당일 누적 >= 평균 × N배

        # 가격 돌파 조건
        "breakout_enabled": False,
        "breakout_days": 20,            # N일 고가 돌파 여부

        # 수급 조건 (외국인/기관 매매 동향, opt10059)
        # ※ 수급 조건 활성화 시 종목당 TR 1회 추가 → 탐색 시간 증가
        "supply_enabled": False,
        "foreign_consec_days": 3,               # 외국인 순매수 N일 이상 연속 (0=미사용)
        "institution_turnover_enabled": True,   # 기관 순매수 전환 (오늘 > 0)

        # 거래대금 조건 (opt10081 거래대금 필드 활용, 단위: 원(KRW))
        "trading_value_enabled": False,
        "trading_value_min_billion": 100.0,     # 거래대금 최소 (억원), 0=미사용
        "trading_value_increase_enabled": False,
        "trading_value_increase_pct": 200.0,    # 거래대금 증가율 (%, N일 평균 대비)
        "trading_value_avg_days": 20,           # 거래대금 평균 기준 일수
    }
}


class Config:
    """설정 관리 클래스"""

    def __init__(self):
        self.config = self._load()

    def _load(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                merged = self._deep_copy(DEFAULT_CONFIG)
                self._deep_update(merged, saved)
                return merged
            except Exception as e:
                print(f"[Config] 설정 파일 로드 실패: {e}")
        return self._deep_copy(DEFAULT_CONFIG)

    def _deep_copy(self, d):
        return json.loads(json.dumps(d))

    def _deep_update(self, base, update):
        for k, v in update.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._deep_update(base[k], v)
            else:
                base[k] = v

    def save(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"[Config] 설정 저장 실패: {e}")
            return False

    def get(self, *keys):
        val = self.config
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return None
        return val

    def set(self, value, *keys):
        if not keys:
            return False
        target = self.config
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]
        target[keys[-1]] = value
        return self.save()

    # ── 편의 메서드 ──────────────────────────────────────────
    def get_account_number(self):
        return self.get("kiwoom", "account_number") or ""

    def set_account_number(self, v):
        return self.set(v, "kiwoom", "account_number")

    def get_scan(self):
        return self.get("scan") or self._deep_copy(DEFAULT_CONFIG["scan"])

    def save_scan(self, scan_dict):
        return self.set(scan_dict, "scan")


# 전역 인스턴스
config = Config()
