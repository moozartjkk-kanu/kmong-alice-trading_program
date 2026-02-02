# -*- coding: utf-8 -*-
"""
예수금 조회 테스트 스크립트
키움 OpenAPI TR 요청이 정상 동작하는지 확인
"""
import sys
from PyQt5.QtWidgets import QApplication
from kiwoom_api import KiwoomAPI


def on_message(screen_no, rqname, trcode, msg):
    """TR 메시지 수신 콜백"""
    print(f"[TR메시지] screen={screen_no} rqname={rqname} trcode={trcode}")
    print(f"           msg={msg}")


def main():
    app = QApplication(sys.argv)
    kiwoom = KiwoomAPI()

    # 디버그 모드 활성화
    kiwoom.set_debug(True)

    # 메시지 콜백 설정 (에러 메시지 확인용)
    kiwoom.set_message_callback(on_message)

    print("=" * 60)
    print("키움 OpenAPI 예수금 조회 테스트")
    print("=" * 60)

    # 로그인
    print("\n[1] 로그인 시도...")
    if not kiwoom.login():
        print("로그인 실패!")
        return

    print("로그인 성공!")

    # 서버 구분 확인
    server_gubun = kiwoom.get_server_gubun()
    is_real = kiwoom.is_real_server()
    print(f"\n[1-1] 서버 구분: {server_gubun} (실서버: {is_real})")

    # 계좌 목록 확인
    accounts = kiwoom.get_account_list()
    print(f"\n[2] 계좌 목록: {accounts}")

    if not accounts:
        print("계좌가 없습니다.")
        return

    account = accounts[0]
    print(f"테스트 계좌: {account}")

    # 예수금 조회 (opw00018)
    print("\n" + "=" * 60)
    print("[3] opw00018 (계좌잔고) 조회")
    print("=" * 60)

    balance = kiwoom.get_balance(account)

    print(f"\n결과:")
    print(f"  - 예수금: {balance.get('deposit', 0):,}원")
    print(f"  - 총매입금액: {balance.get('total_purchase', 0):,}원")
    print(f"  - 총평가금액: {balance.get('total_eval', 0):,}원")
    print(f"  - 총손익: {balance.get('total_profit', 0):,}원")
    print(f"  - 수익률: {balance.get('profit_rate', 0):.2f}%")
    print(f"  - 보유종목 수: {len(balance.get('holdings', []))}개")

    for h in balance.get('holdings', []):
        print(f"    [{h['code']}] {h['name']}: {h['quantity']}주 @ {h['avg_price']:,}원")

    # opw00001로 항상 조회 (비교용)
    print("\n" + "=" * 60)
    print("[4] opw00001 (예수금상세) 조회")
    print("=" * 60)

    deposit_info = kiwoom.get_deposit(account)

    print(f"\n결과:")
    print(f"  - 예수금: {deposit_info.get('deposit', 0):,}원")
    print(f"  - D+1예수금: {deposit_info.get('deposit_d1', 0):,}원")
    print(f"  - D+2예수금: {deposit_info.get('deposit_d2', 0):,}원")
    print(f"  - 출금가능금액: {deposit_info.get('available', 0):,}원")
    print(f"  - 주문가능금액: {deposit_info.get('order_available', 0):,}원")

    # 두 TR 결과 비교
    if balance.get('deposit', 0) == 0 and deposit_info.get('deposit', 0) == 0:
        print("\n" + "=" * 60)
        print("[주의] 두 TR 모두 예수금 0원")
        print("=" * 60)

    # ✅ opw00004 (계좌평가현황) 추가 테스트
    print("\n" + "=" * 60)
    print("[5] opw00004 (계좌평가현황) 조회")
    print("=" * 60)

    try:
        kiwoom.set_input_value("계좌번호", account)
        kiwoom.set_input_value("비밀번호", "")  # KOA Studio 저장된 비밀번호
        kiwoom.set_input_value("상장폐지조회구분", "0")
        kiwoom.set_input_value("비밀번호입력매체구분", "00")
        kiwoom.comm_rq_data("계좌평가현황", "opw00004", 0, "0107")

        # 싱글 데이터
        deposit = kiwoom.get_comm_data("opw00004", "", 0, "예수금")
        total_eval = kiwoom.get_comm_data("opw00004", "", 0, "유가잔고평가액")
        total_profit = kiwoom.get_comm_data("opw00004", "", 0, "총손익")

        print(f"  - 예수금: {deposit}")
        print(f"  - 유가잔고평가액: {total_eval}")
        print(f"  - 총손익: {total_profit}")

        # 멀티 데이터 (보유종목)
        cnt = kiwoom.get_repeat_cnt("opw00004", "")
        print(f"  - 보유종목 수: {cnt}개")

        for i in range(min(cnt, 5)):
            code = kiwoom.get_comm_data("opw00004", "", i, "종목코드")
            name = kiwoom.get_comm_data("opw00004", "", i, "종목명")
            qty = kiwoom.get_comm_data("opw00004", "", i, "보유수량")
            print(f"    [{code}] {name}: {qty}주")

    except Exception as e:
        print(f"  오류: {e}")

    print("\n" + "=" * 60)
    print("테스트 완료")
    print("=" * 60)

    # 이벤트 루프 종료
    app.quit()


if __name__ == "__main__":
    main()
