# -*- coding: utf-8 -*-
"""
키움증권 자동매매 시스템 설정 파일
"""
import json
import os

# 설정 파일 경로
CONFIG_FILE = "trading_config.json"

# 기본 설정값
DEFAULT_CONFIG = {
    # 키움 API 설정 (키움 Open API는 별도 API 키가 필요 없음, 로그인으로 인증)
    "kiwoom": {
        "account_number": "",  # 계좌번호 (로그인 후 자동으로 가져옴)
        "account_password": "",  # 계좌 비밀번호 (필요시)
        "use_mock_trading": False,  # 모의투자 사용 여부
    },

    # 매수 설정
    "buy": {
        "envelope_period": 20,  # 엔벨로프 이동평균 기간
        "envelope_percent": 19,  # 엔벨로프 하단 % - 매수 트리거 조건 (20일선 대비 -19%)
        "envelope_buy_percent": 20,  # 실제 지정가 매수 주문 가격 % (20일선 대비 -20% + 1호가)
        "max_buy_count": 3,  # 최대 매수 횟수 (3차 매수까지)
        "additional_buy_drop_percent": 10,  # 추가 매수 조건 (-10%)
        "buy_amount_per_stock": 1000000,  # 종목당 1회 매수 금액 (원)
        "max_holding_stocks": 3,  # 최대 동시 보유 종목 수
        "allow_reentry": True,  # 전량 매도 후 재진입 허용 여부
        "max_reentry_per_day": 1,  # 하루 최대 재진입 횟수
    },

    # 매도 설정
    "sell": {
        "profit_targets": [2.95, 4.95, 6.95],  # 목표 수익률 (%)
        "profit_sell_ratios": [30, 30, 30],  # 각 목표에서 매도 비중 (%)
        "ma20_sell_ratio": 10,  # 20일선 도달시 매도 비중 (%)
        "stoploss_use_market_order": True,  # 스탑로스 시 시장가 사용 여부
    },

    # 감시 종목 리스트 (최대 200개)
    "watchlist": [],
    "max_watchlist_count": 200,  # 최대 감시 종목 수

    # 포지션 정보 (종목별 매수 내역)
    "positions": {},

    # 미체결 주문 저장 (프로그램 재시작 시 복원용)
    "pending_orders": {
        # "종목코드": {
        #     "order_type": "buy" 또는 "sell",
        #     "quantity": 수량,
        #     "price": 가격,
        #     "buy_count": 몇차 매수인지 (매수일 경우),
        #     "target_name": 매도 목표명 (매도일 경우),
        #     "created_at": 생성 시간
        # }
    },

    # 세션 정보 (프로그램 재시작 시 상태 복원용)
    "session": {
        "last_trading_date": "",  # 마지막 거래일 (YYYYMMDD)
        "auto_trading_enabled": False,  # 자동매매 활성화 상태
        "orders_restored": False,  # 주문 복원 완료 여부
        "state_synced": False,  # 체결내역 기반 상태 동기화 완료 여부
    },

    # 에러 처리 설정
    "error_handling": {
        "order_retry_count": 3,  # 주문 실패 시 재시도 횟수
        "order_retry_interval_ms": 1000,  # 재시도 간격 (밀리초)
        "reconnect_interval_sec": 10,  # API 연결 끊김 시 재연결 간격 (초)
    },

    # 재진입 기록 (당일 재진입 횟수 추적)
    "reentry_history": {
        # "종목코드": {
        #     "date": "YYYYMMDD",
        #     "count": 재진입 횟수
        # }
    },

    # 당일 체결내역 저장 (프로그램 재시작 시 상태 확인용)
    "execution_history": {
        # "날짜(YYYYMMDD)": {
        #     "종목코드": [
        #         {
        #             "type": "buy" 또는 "sell",
        #             "quantity": 수량,
        #             "price": 가격,
        #             "time": 체결시간,
        #             "order_no": 주문번호
        #         }
        #     ]
        # }
    }
}


class Config:
    """설정 관리 클래스"""

    def __init__(self):
        self.config = self.load_config()

    def load_config(self):
        """설정 파일 로드"""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    # 기본 설정과 병합
                    merged = DEFAULT_CONFIG.copy()
                    self._deep_update(merged, saved_config)
                    return merged
            except Exception as e:
                print(f"설정 파일 로드 실패: {e}")
                return DEFAULT_CONFIG.copy()
        return DEFAULT_CONFIG.copy()

    def _deep_update(self, base_dict, update_dict):
        """딕셔너리 깊은 병합"""
        for key, value in update_dict.items():
            if key in base_dict and isinstance(base_dict[key], dict) and isinstance(value, dict):
                self._deep_update(base_dict[key], value)
            else:
                base_dict[key] = value

    def save_config(self):
        """설정 파일 저장"""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"설정 파일 저장 실패: {e}")
            return False

    def get(self, *keys):
        """설정값 가져오기 (점 표기법 지원)"""
        value = self.config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        return value

    def set(self, value, *keys):
        """설정값 설정하기"""
        if not keys:
            return False

        target = self.config
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]

        target[keys[-1]] = value
        return self.save_config()

    # 편의 메서드들
    def get_account_number(self):
        return self.get("kiwoom", "account_number")

    def set_account_number(self, account):
        return self.set(account, "kiwoom", "account_number")

    def get_watchlist(self):
        return self.get("watchlist") or []

    def add_to_watchlist(self, stock_code, stock_name=""):
        watchlist = self.get_watchlist()
        max_count = self.get("max_watchlist_count") or 200

        # 최대 개수 체크
        if len(watchlist) >= max_count:
            return False, f"최대 {max_count}개까지만 등록 가능합니다."

        # 중복 체크
        for item in watchlist:
            if item["code"] == stock_code:
                return False, "이미 등록된 종목입니다."

        watchlist.append({"code": stock_code, "name": stock_name})
        result = self.set(watchlist, "watchlist")
        return result, "등록 완료" if result else "저장 실패"

    def remove_from_watchlist(self, stock_code):
        watchlist = self.get_watchlist()
        watchlist = [item for item in watchlist if item["code"] != stock_code]
        return self.set(watchlist, "watchlist")

    def get_position(self, stock_code):
        """특정 종목의 포지션 정보 가져오기"""
        positions = self.get("positions") or {}
        return positions.get(stock_code, None)

    def update_position(self, stock_code, position_data):
        """포지션 정보 업데이트"""
        positions = self.get("positions") or {}
        positions[stock_code] = position_data
        return self.set(positions, "positions")

    def clear_position(self, stock_code):
        """포지션 정보 삭제"""
        positions = self.get("positions") or {}
        if stock_code in positions:
            del positions[stock_code]
            return self.set(positions, "positions")
        return True

    # ==================== 미체결 주문 관리 ====================
    def get_pending_orders(self):
        """저장된 미체결 주문 목록 가져오기"""
        return self.get("pending_orders") or {}

    def save_pending_order(self, code, order_info):
        """
        미체결 주문 저장

        Args:
            code: 종목 코드
            order_info: {
                "order_type": "buy" 또는 "sell",
                "quantity": 수량,
                "price": 가격,
                "buy_count": 몇차 매수 (매수일 경우),
                "target_name": 매도 목표명 (매도일 경우),
                "created_at": 생성 시간
            }
        """
        pending_orders = self.get_pending_orders()

        if code not in pending_orders:
            pending_orders[code] = []

        # 동일한 주문이 이미 있는지 확인 (중복 방지)
        for existing in pending_orders[code]:
            if (existing.get("order_type") == order_info.get("order_type") and
                existing.get("price") == order_info.get("price") and
                existing.get("buy_count") == order_info.get("buy_count")):
                return True  # 이미 존재함

        pending_orders[code].append(order_info)
        return self.set(pending_orders, "pending_orders")

    def remove_pending_order(self, code, order_type=None, price=None, buy_count=None):
        """
        미체결 주문 삭제

        Args:
            code: 종목 코드
            order_type: 주문 타입 (None이면 해당 종목 전체 삭제)
            price: 가격 (특정 가격의 주문만 삭제)
            buy_count: 매수 차수 (특정 차수 주문만 삭제)
        """
        pending_orders = self.get_pending_orders()

        if code not in pending_orders:
            return True

        if order_type is None:
            # 해당 종목의 모든 주문 삭제
            del pending_orders[code]
        else:
            # 조건에 맞는 주문만 삭제
            pending_orders[code] = [
                o for o in pending_orders[code]
                if not (o.get("order_type") == order_type and
                       (price is None or o.get("price") == price) and
                       (buy_count is None or o.get("buy_count") == buy_count))
            ]

            # 빈 리스트면 키 삭제
            if not pending_orders[code]:
                del pending_orders[code]

        return self.set(pending_orders, "pending_orders")

    def clear_pending_orders_for_stock(self, code, order_type=None):
        """특정 종목의 미체결 주문 전체 삭제"""
        pending_orders = self.get_pending_orders()

        if code not in pending_orders:
            return True

        if order_type is None:
            del pending_orders[code]
        else:
            pending_orders[code] = [
                o for o in pending_orders[code]
                if o.get("order_type") != order_type
            ]
            if not pending_orders[code]:
                del pending_orders[code]

        return self.set(pending_orders, "pending_orders")

    def clear_all_pending_orders(self):
        """모든 미체결 주문 삭제"""
        return self.set({}, "pending_orders")

    # ==================== 세션 관리 ====================
    def get_session_info(self):
        """세션 정보 가져오기"""
        return self.get("session") or {}

    def update_session_info(self, **kwargs):
        """세션 정보 업데이트"""
        session = self.get_session_info()
        session.update(kwargs)
        return self.set(session, "session")

    def get_last_trading_date(self):
        """마지막 거래일 가져오기"""
        session = self.get_session_info()
        return session.get("last_trading_date", "")

    def set_last_trading_date(self, date_str):
        """마지막 거래일 설정 (YYYYMMDD 형식)"""
        return self.update_session_info(last_trading_date=date_str)

    def is_orders_restored(self):
        """오늘 주문 복원이 완료되었는지 확인"""
        session = self.get_session_info()
        return session.get("orders_restored", False)

    def set_orders_restored(self, restored=True):
        """주문 복원 완료 상태 설정"""
        return self.update_session_info(orders_restored=restored)

    def reset_session_for_new_day(self, today_str):
        """새로운 거래일 시작 시 세션 리셋"""
        return self.update_session_info(
            last_trading_date=today_str,
            orders_restored=False,
            state_synced=False
        )

    def is_state_synced(self):
        """체결내역 기반 상태 동기화가 완료되었는지 확인"""
        session = self.get_session_info()
        return session.get("state_synced", False)

    def set_state_synced(self, synced=True):
        """상태 동기화 완료 설정"""
        return self.update_session_info(state_synced=synced)

    # ==================== 체결내역 관리 ====================
    def get_execution_history(self, date_str=None):
        """
        체결내역 가져오기

        Args:
            date_str: 날짜 (YYYYMMDD), None이면 전체

        Returns:
            체결내역 딕셔너리
        """
        history = self.get("execution_history") or {}
        if date_str:
            return history.get(date_str, {})
        return history

    def save_execution(self, code, execution_info, date_str=None):
        """
        체결내역 저장

        Args:
            code: 종목 코드
            execution_info: {
                "type": "buy" 또는 "sell",
                "quantity": 수량,
                "price": 가격,
                "time": 체결시간,
                "order_no": 주문번호
            }
            date_str: 날짜 (YYYYMMDD), None이면 오늘
        """
        from datetime import datetime
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        history = self.get("execution_history") or {}

        if date_str not in history:
            history[date_str] = {}

        if code not in history[date_str]:
            history[date_str][code] = []

        # 중복 체크 (주문번호 기준)
        order_no = execution_info.get("order_no", "")
        for existing in history[date_str][code]:
            if existing.get("order_no") == order_no and order_no:
                return True  # 이미 존재

        history[date_str][code].append(execution_info)
        return self.set(history, "execution_history")

    def get_stock_executions(self, code, date_str=None):
        """
        특정 종목의 체결내역 가져오기

        Args:
            code: 종목 코드
            date_str: 날짜 (YYYYMMDD), None이면 오늘
        """
        from datetime import datetime
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        history = self.get_execution_history(date_str)
        return history.get(code, [])

    def get_stock_sell_executions(self, code, date_str=None):
        """특정 종목의 매도 체결내역만 가져오기"""
        executions = self.get_stock_executions(code, date_str)
        return [e for e in executions if e.get("type") == "sell"]

    def get_stock_buy_executions(self, code, date_str=None):
        """특정 종목의 매수 체결내역만 가져오기"""
        executions = self.get_stock_executions(code, date_str)
        return [e for e in executions if e.get("type") == "buy"]

    def clear_old_execution_history(self, keep_days=7):
        """
        오래된 체결내역 정리 (기본 7일)

        Args:
            keep_days: 보관할 일수
        """
        from datetime import datetime, timedelta

        history = self.get("execution_history") or {}
        cutoff_date = (datetime.now() - timedelta(days=keep_days)).strftime("%Y%m%d")

        # 오래된 날짜 삭제
        dates_to_remove = [d for d in history.keys() if d < cutoff_date]
        for date_str in dates_to_remove:
            del history[date_str]

        if dates_to_remove:
            return self.set(history, "execution_history")
        return True

    # ==================== 재진입 관리 ====================
    def get_reentry_count(self, code, date_str=None):
        """
        특정 종목의 당일 재진입 횟수 조회

        Args:
            code: 종목 코드
            date_str: 날짜 (YYYYMMDD), None이면 오늘

        Returns:
            재진입 횟수
        """
        from datetime import datetime
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        reentry_history = self.get("reentry_history") or {}
        entry = reentry_history.get(code, {})

        # 날짜가 다르면 0
        if entry.get("date") != date_str:
            return 0

        return entry.get("count", 0)

    def increment_reentry_count(self, code, date_str=None):
        """
        특정 종목의 재진입 횟수 증가

        Args:
            code: 종목 코드
            date_str: 날짜 (YYYYMMDD), None이면 오늘

        Returns:
            증가 후 재진입 횟수
        """
        from datetime import datetime
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        reentry_history = self.get("reentry_history") or {}
        entry = reentry_history.get(code, {})

        # 날짜가 다르면 리셋
        if entry.get("date") != date_str:
            entry = {"date": date_str, "count": 0}

        entry["count"] = entry.get("count", 0) + 1
        reentry_history[code] = entry

        self.set(reentry_history, "reentry_history")
        return entry["count"]

    def can_reentry(self, code, date_str=None):
        """
        재진입 가능 여부 확인

        Args:
            code: 종목 코드
            date_str: 날짜 (YYYYMMDD), None이면 오늘

        Returns:
            재진입 가능 여부
        """
        allow_reentry = self.get("buy", "allow_reentry")
        if not allow_reentry:
            return False

        max_reentry = self.get("buy", "max_reentry_per_day") or 1
        current_count = self.get_reentry_count(code, date_str)

        return current_count < max_reentry

    def reset_reentry_for_new_day(self, date_str):
        """새로운 거래일 시작 시 재진입 기록 리셋"""
        # 오래된 재진입 기록 정리 (오늘 날짜와 다른 것들)
        reentry_history = self.get("reentry_history") or {}
        codes_to_remove = [
            code for code, entry in reentry_history.items()
            if entry.get("date") != date_str
        ]

        for code in codes_to_remove:
            del reentry_history[code]

        if codes_to_remove:
            return self.set(reentry_history, "reentry_history")
        return True


# 전역 설정 인스턴스
config = Config()
