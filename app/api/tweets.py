"""推文数据操作"""
import json
from app.models.database import get_db, tweet_to_dict


def save_tweet(config_id, tweet_data):
    """保存单条推文，已存在则跳过"""
    db = get_db()
    existing = db.execute(
        "SELECT id FROM tweets WHERE tweet_id=?", (tweet_data["tweet_id"],)
    ).fetchone()
    if existing:
        db.close()
        return existing["id"]

    cursor = db.execute(
        """INSERT INTO tweets 
        (config_id, tweet_id, author_screen_name, author_name, author_avatar, author_bio, 
         content, created_at, url, media_urls)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            config_id,
            tweet_data["tweet_id"],
            tweet_data.get("author_screen_name", ""),
            tweet_data.get("author_name", ""),
            tweet_data.get("author_avatar", ""),
            tweet_data.get("author_bio", ""),
            tweet_data.get("content", ""),
            tweet_data.get("created_at", ""),
            tweet_data.get("url", ""),
            json.dumps(tweet_data.get("media_urls", [])),
        )
    )
    db.commit()
    tweet_id = cursor.lastrowid
    db.close()
    return tweet_id


def get_tweets_by_config(config_id, limit=50, offset=0):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM tweets WHERE config_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (config_id, limit, offset)
    ).fetchall()
    total = db.execute("SELECT COUNT(*) FROM tweets WHERE config_id=?", (config_id,)).fetchone()[0]
    db.close()
    return [tweet_to_dict(r) for r in rows], total


def get_all_tweets_for_user(config_id):
    """获取某个用户的所有推文（用于生成H5页面）"""
    db = get_db()
    rows = db.execute(
        "SELECT * FROM tweets WHERE config_id=? ORDER BY created_at DESC",
        (config_id,)
    ).fetchall()
    db.close()
    return [tweet_to_dict(r) for r in rows]


def get_tweet_author_info(config_id):
    """获取某个用户的作者信息（头像、名称、简介）"""
    db = get_db()
    row = db.execute(
        "SELECT author_screen_name, author_name, author_avatar, author_bio FROM tweets WHERE config_id=? AND author_avatar != '' LIMIT 1",
        (config_id,)
    ).fetchone()
    db.close()
    if row:
        return {
            "screen_name": row["author_screen_name"],
            "name": row["author_name"],
            "avatar": row["author_avatar"],
            "bio": row["author_bio"],
        }
    return None


def get_all_crawled_configs():
    """获取所有有推文数据的配置"""
    db = get_db()
    rows = db.execute("""
        SELECT DISTINCT c.id, c.crawler_user, c.is_enabled, c.last_crawl_time,
               (SELECT COUNT(*) FROM tweets WHERE config_id=c.id) as tweet_count
        FROM crawler_config c
        WHERE c.id IN (SELECT DISTINCT config_id FROM tweets)
        ORDER BY c.crawler_user
    """).fetchall()
    db.close()
    return [dict(r) for r in rows]


def save_link(config_id, tweet_id, original_url, resolved_url="", title="", content=""):
    db = get_db()
    existing = db.execute(
        "SELECT id FROM tweet_links WHERE config_id=? AND original_url=?",
        (config_id, original_url)
    ).fetchone()
    if existing:
        db.close()
        return
    db.execute(
        "INSERT INTO tweet_links (config_id, tweet_id, original_url, resolved_url, title, content) VALUES (?,?,?,?,?,?)",
        (config_id, tweet_id, original_url, resolved_url, title, content)
    )
    db.commit()
    db.close()


def get_links_for_tweet(config_id, tweet_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM tweet_links WHERE config_id=? AND tweet_id=?", (config_id, tweet_id)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]
