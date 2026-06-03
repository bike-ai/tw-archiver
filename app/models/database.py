"""数据库初始化与核心模型"""
import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "tw-archiver.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS crawler_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crawler_user TEXT NOT NULL UNIQUE,
            prompt TEXT NOT NULL DEFAULT '',
            cookie_ct0 TEXT DEFAULT '',
            cookie_auth_token TEXT DEFAULT '',
            cookie_other TEXT DEFAULT '{}',
            is_scheduled INTEGER NOT NULL DEFAULT 0,
            schedule_expr TEXT DEFAULT '',
            is_enabled INTEGER NOT NULL DEFAULT 0,
            last_crawl_time TEXT DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );

        CREATE TABLE IF NOT EXISTS tweets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER NOT NULL,
            tweet_id TEXT NOT NULL,
            author_screen_name TEXT NOT NULL DEFAULT '',
            author_name TEXT DEFAULT '',
            author_avatar TEXT DEFAULT '',
            author_bio TEXT DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            url TEXT DEFAULT '',
            media_urls TEXT DEFAULT '[]',
            crawled_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (config_id) REFERENCES crawler_config(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS tweet_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER NOT NULL,
            tweet_id TEXT NOT NULL,
            original_url TEXT NOT NULL,
            resolved_url TEXT DEFAULT '',
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            saved_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (config_id) REFERENCES crawler_config(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tweets_config ON tweets(config_id);
        CREATE INDEX IF NOT EXISTS idx_tweets_tweet_id ON tweets(tweet_id);
        CREATE INDEX IF NOT EXISTS idx_links_config ON tweet_links(config_id);
    """)
    conn.commit()
    conn.close()


def config_to_dict(row):
    return {
        "id": row["id"],
        "crawler_user": row["crawler_user"],
        "prompt": row["prompt"],
        "cookie_ct0": row["cookie_ct0"] or "",
        "cookie_auth_token": row["cookie_auth_token"] or "",
        "cookie_other": json.loads(row["cookie_other"] or "{}"),
        "is_scheduled": bool(row["is_scheduled"]),
        "schedule_expr": row["schedule_expr"] or "",
        "is_enabled": bool(row["is_enabled"]),
        "last_crawl_time": row["last_crawl_time"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def tweet_to_dict(row):
    return {
        "id": row["id"],
        "config_id": row["config_id"],
        "tweet_id": row["tweet_id"],
        "author_screen_name": row["author_screen_name"],
        "author_name": row["author_name"],
        "author_avatar": row["author_avatar"],
        "author_bio": row["author_bio"],
        "content": row["content"],
        "created_at": row["created_at"],
        "url": row["url"],
        "media_urls": json.loads(row["media_urls"] or "[]"),
        "crawled_at": row["crawled_at"],
    }
