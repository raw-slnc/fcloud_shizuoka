# -*- coding: utf-8 -*-
import sqlite3
import json
import os
from datetime import datetime


class CacheDB:
    """
    キー・JSON文字列・取得日時 を保存するシンプルな SQLite キャッシュ。
    スキーマは固定せず生 JSON を TEXT で保存するため、
    API レスポンスのフィールド変化に影響されない。
    """

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._path = db_path
        with sqlite3.connect(self._path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS cache (
                    key        TEXT PRIMARY KEY,
                    data       TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
            ''')

    def get(self, key: str):
        """キャッシュを返す。なければ (None, None)。"""
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                'SELECT data, fetched_at FROM cache WHERE key=?', (key,)
            ).fetchone()
        if row:
            return json.loads(row[0]), row[1]
        return None, None

    def put(self, key: str, data) -> str:
        """データを保存し、取得日時文字列を返す。"""
        ts = datetime.now().strftime('%Y-%m-%d %H:%M')
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO cache(key,data,fetched_at) VALUES(?,?,?)',
                (key, json.dumps(data, ensure_ascii=False), ts)
            )
        return ts

    def get_fetched_at(self, key: str):
        """取得日時だけを返す。なければ None。"""
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                'SELECT fetched_at FROM cache WHERE key=?', (key,)
            ).fetchone()
        return row[0] if row else None
