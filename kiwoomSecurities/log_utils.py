# -*- coding: utf-8 -*-
"""
날짜별 로그 파일 저장 유틸리티
날짜가 바뀌면 자동으로 새 파일에 저장
"""
import os
import threading
from datetime import datetime

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class DailyFileLogger:
    """날짜별 로그 파일 저장 클래스 (자정에 새 파일 자동 생성)"""

    def __init__(self, prefix, log_dir=None):
        self.prefix = prefix
        self.log_dir = log_dir or os.path.join(_BASE_DIR, "logs")
        self._lock = threading.Lock()
        self._current_date = None
        self._file = None
        os.makedirs(self.log_dir, exist_ok=True)

    def _get_file(self):
        today = datetime.now().strftime("%Y%m%d")
        if today != self._current_date:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
            self._current_date = today
            filepath = os.path.join(self.log_dir, f"{self.prefix}_{today}.log")
            self._file = open(filepath, "a", encoding="utf-8")
        return self._file

    def write(self, message):
        with self._lock:
            try:
                f = self._get_file()
                f.write(str(message) + "\n")
                f.flush()
            except Exception:
                pass

    def close(self):
        with self._lock:
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
