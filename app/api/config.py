"""配置CRUD操作"""
from app.models.database import get_db, config_to_dict


def list_configs(search_user=None):
    db = get_db()
    if search_user:
        rows = db.execute(
            "SELECT * FROM crawler_config WHERE crawler_user LIKE ? ORDER BY updated_at DESC",
            (f"%{search_user}%",)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM crawler_config ORDER BY updated_at DESC"
        ).fetchall()
    db.close()
    return [config_to_dict(r) for r in rows]


def get_config(config_id):
    db = get_db()
    row = db.execute("SELECT * FROM crawler_config WHERE id=?", (config_id,)).fetchone()
    db.close()
    return config_to_dict(row) if row else None


def get_config_by_user(crawler_user):
    db = get_db()
    row = db.execute("SELECT * FROM crawler_config WHERE crawler_user=?", (crawler_user,)).fetchone()
    db.close()
    return config_to_dict(row) if row else None


def create_config(data):
    db = get_db()
    cursor = db.execute(
        """INSERT INTO crawler_config 
        (crawler_user, prompt, cookie_ct0, cookie_auth_token, cookie_other, is_scheduled, schedule_expr, is_enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["crawler_user"],
            data.get("prompt", ""),
            data.get("cookie_ct0", ""),
            data.get("cookie_auth_token", ""),
            data.get("cookie_other", "{}"),
            int(data.get("is_scheduled", False)),
            data.get("schedule_expr", ""),
            int(data.get("is_enabled", False)),
        )
    )
    db.commit()
    config_id = cursor.lastrowid
    row = db.execute("SELECT * FROM crawler_config WHERE id=?", (config_id,)).fetchone()
    db.close()
    return config_to_dict(row)


def update_config(config_id, data):
    db = get_db()
    fields = []
    values = []
    for key in ["crawler_user", "prompt", "cookie_ct0", "cookie_auth_token", "cookie_other",
                 "is_scheduled", "schedule_expr", "is_enabled"]:
        if key in data:
            fields.append(f"{key}=?")
            values.append(data[key])
    if not fields:
        db.close()
        return None
    fields.append("updated_at=datetime('now','localtime')")
    values.append(config_id)
    db.execute(f"UPDATE crawler_config SET {', '.join(fields)} WHERE id=?", values)
    db.commit()
    row = db.execute("SELECT * FROM crawler_config WHERE id=?", (config_id,)).fetchone()
    db.close()
    return config_to_dict(row)


def delete_config(config_id):
    db = get_db()
    db.execute("DELETE FROM crawler_config WHERE id=?", (config_id,))
    db.commit()
    db.close()


def update_last_crawl_time(config_id):
    db = get_db()
    db.execute(
        "UPDATE crawler_config SET last_crawl_time=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE id=?",
        (config_id,)
    )
    db.commit()
    db.close()
