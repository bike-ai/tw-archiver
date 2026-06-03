"""tw-archiver - Twitter爬虫管理平台"""
import json
import logging
import threading
import time
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.models.database import init_db
from app.api.config import (
    list_configs, get_config, create_config, update_config, delete_config,
    update_last_crawl_time
)
from app.api.tweets import get_tweets_by_config, get_all_tweets_for_user, get_tweet_author_info, get_all_crawled_configs
from app.crawler.engine import crawl_user_tweets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# 爬取任务锁，防止同一用户同时被爬
_crawl_locks = {}
_scheduler_running = False

# 爬取状态追踪
_crawl_status: dict[int, dict] = {}  # config_id -> {running, progress, count, errors, started_at}

def _set_crawl_status(config_id: int, **kwargs):
    if config_id not in _crawl_status:
        _crawl_status[config_id] = {"running": False, "progress": "", "count": 0, "errors": [], "started_at": None}
    _crawl_status[config_id].update(**kwargs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("数据库初始化完成")
    # 启动定时调度器
    _start_scheduler()
    yield
    global _scheduler_running
    _scheduler_running = False


app = FastAPI(title="tw-archiver", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# ====== 请求模型 ======

class ConfigCreate(BaseModel):
    crawler_user: str
    prompt: str = ""
    cookie_ct0: str = ""
    cookie_auth_token: str = ""
    cookie_other: str = "{}"
    is_scheduled: bool = False
    schedule_expr: str = ""
    is_enabled: bool = False
    save_and_enable: bool = False  # 保存并启用


class ConfigUpdate(BaseModel):
    crawler_user: str | None = None
    prompt: str | None = None
    cookie_ct0: str | None = None
    cookie_auth_token: str | None = None
    cookie_other: str | None = None
    is_scheduled: bool | None = None
    schedule_expr: str | None = None
    is_enabled: bool | None = None


# ====== 调度器 ======

def _check_scheduled_tasks():
    """每分钟检查一次定时任务"""
    while _scheduler_running:
        try:
            configs = list_configs()
            now = datetime.now()
            for cfg in configs:
                if not cfg["is_enabled"] or not cfg["is_scheduled"] or not cfg["schedule_expr"]:
                    continue
                # 简单cron解析：支持 "每天9点" 格式和标准cron
                expr = cfg["schedule_expr"].strip()
                if _match_schedule(expr, now, cfg["last_crawl_time"]):
                    logger.info(f"定时任务触发: {cfg['crawler_user']}")
                    thread = threading.Thread(target=_run_crawl, args=(cfg["id"],), daemon=True)
                    thread.start()
        except Exception as e:
            logger.error(f"调度器错误: {e}")
        time.sleep(60)


def _match_schedule(expr: str, now: datetime, last_crawl: str | None) -> bool:
    """简单匹配调度表达式"""
    import re
    expr = expr.strip().lower()

    # "每天9点" / "每天 9:00" / "每天 09:00"
    m = re.match(r'每天\s*(\d{1,2})(?::(\d{2}))?', expr)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        if now.hour == hour and now.minute == minute:
            # 如果上次爬取时间不是今天，才触发
            if last_crawl:
                lc = datetime.strptime(last_crawl[:10], "%Y-%m-%d")
                if lc.date() == now.date():
                    return False
            return True

    # 标准cron: "0 9 * * *"
    try:
        parts = expr.split()
        if len(parts) == 5:
            cron_min, cron_hour = parts[0], parts[1]
            min_match = cron_min == "*" or int(cron_min) == now.minute
            hour_match = cron_hour == "*" or int(cron_hour) == now.hour
            if min_match and hour_match:
                if last_crawl:
                    lc = datetime.strptime(last_crawl[:10], "%Y-%m-%d")
                    if lc.date() == now.date():
                        return False
                return True
    except (ValueError, IndexError):
        pass

    return False


def _run_crawl(config_id: int):
    lock = _crawl_locks.get(config_id)
    if lock and lock.locked():
        logger.warning(f"配置 {config_id} 正在爬取中，跳过")
        return
    if lock is None:
        _crawl_locks[config_id] = threading.Lock()

    with _crawl_locks[config_id]:
        cfg = get_config(config_id)
        if not cfg:
            logger.warning(f"配置 {config_id} 不存在")
            _set_crawl_status(config_id, running=False, progress="配置不存在", errors=["配置不存在"])
            return
        logger.info(f"开始爬取: {cfg['crawler_user']}")
        _set_crawl_status(config_id, running=True, progress=f"正在爬取 @{cfg['crawler_user']}...", count=0, errors=[], started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        count, errors = crawl_user_tweets(cfg, status_callback=lambda msg: _set_crawl_status(config_id, progress=msg))
        if count > 0:
            update_last_crawl_time(config_id)
        _set_crawl_status(config_id, running=False, progress="完成" if not errors else f"完成（{len(errors)}个错误）", count=count, errors=errors[:5])
        msg = f"爬取 {cfg['crawler_user']}: 新增 {count} 条推文"
        if errors:
            msg += f"，错误: {'; '.join(errors[:3])}"
        logger.info(msg)
        return msg


def _start_scheduler():
    global _scheduler_running
    _scheduler_running = True
    t = threading.Thread(target=_check_scheduled_tasks, daemon=True)
    t.start()
    logger.info("定时调度器已启动")


# ====== API 路由 ======

@app.get("/api/configs")
def api_list_configs(search: str = ""):
    return JSONResponse(list_configs(search))


@app.get("/api/configs/{config_id}")
def api_get_config(config_id: int):
    cfg = get_config(config_id)
    if not cfg:
        raise HTTPException(404, "配置不存在")
    return JSONResponse(cfg)


@app.post("/api/configs")
def api_create_config(data: ConfigCreate):
    data_dict = data.model_dump()
    save_and_enable = data_dict.pop("save_and_enable", False)
    try:
        cfg = create_config(data_dict)
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(400, "该爬取用户已存在")
        raise HTTPException(500, str(e))

    if save_and_enable:
        # 启用并立即爬取
        update_config(cfg["id"], {"is_enabled": True})
        _run_crawl(cfg["id"])
        cfg = get_config(cfg["id"])

    return JSONResponse(cfg)


@app.put("/api/configs/{config_id}")
def api_update_config(config_id: int, data: ConfigUpdate):
    data_dict = {k: v for k, v in data.model_dump().items() if v is not None}
    if not data_dict:
        raise HTTPException(400, "没有要更新的字段")
    if "is_enabled" in data_dict:
        data_dict["is_enabled"] = int(data_dict["is_enabled"])
    if "is_scheduled" in data_dict:
        data_dict["is_scheduled"] = int(data_dict["is_scheduled"])
    cfg = update_config(config_id, data_dict)
    if not cfg:
        raise HTTPException(404, "配置不存在")
    return JSONResponse(cfg)


@app.delete("/api/configs/{config_id}")
def api_delete_config(config_id: int):
    delete_config(config_id)
    return JSONResponse({"ok": True})


@app.post("/api/configs/{config_id}/crawl")
def api_crawl_now(config_id: int):
    """立即爬取"""
    cfg = get_config(config_id)
    if not cfg:
        raise HTTPException(404, "配置不存在")
    msg = _run_crawl(config_id)
    return JSONResponse({"ok": True, "message": msg})


@app.get("/api/configs/{config_id}/crawl-status")
def api_crawl_status(config_id: int):
    """获取爬取状态"""
    status = _crawl_status.get(config_id, {"running": False, "progress": "", "count": 0, "errors": [], "started_at": None})
    return JSONResponse(status)


@app.get("/api/configs/{config_id}/tweets")
def api_get_tweets(config_id: int, limit: int = 50, offset: int = 0):
    cfg = get_config(config_id)
    if not cfg:
        raise HTTPException(404, "配置不存在")
    tweets, total = get_tweets_by_config(config_id, limit, offset)
    return JSONResponse({"tweets": tweets, "total": total})


# ====== 页面路由 ======

@app.get("/", response_class=HTMLResponse)
def index():
    """管理主页"""
    return HTMLResponse(_render_index())


@app.get("/reader/{config_id}", response_class=HTMLResponse)
def reader_page(config_id: int):
    """H5阅读页面"""
    cfg = get_config(config_id)
    if not cfg:
        return HTMLResponse("配置不存在", status_code=404)
    tweets = get_all_tweets_for_user(config_id)
    author = get_tweet_author_info(config_id)
    return HTMLResponse(_render_reader(cfg, tweets, author))


@app.get("/h5", response_class=HTMLResponse)
def h5_index():
    """汇总H5页面"""
    configs = get_all_crawled_configs()
    return HTMLResponse(_render_h5_index(configs))


def _render_index():
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>tw-archiver 管理</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#f0f2f5; color:#333; padding:20px; }
.container { max-width:1400px; margin:0 auto; }
.header { display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; }
.header h1 { font-size:24px; color:#1a1a2e; }
.header h1 span { color:#1da1f2; }
.btn { padding:8px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; font-weight:500; transition:all .2s; }
.btn-primary { background:#1da1f2; color:#fff; }
.btn-primary:hover { background:#1a91da; }
.btn-success { background:#17bf63; color:#fff; }
.btn-success:hover { background:#14a855; }
.btn-warning { background:#ffad1f; color:#fff; }
.btn-warning:hover { background:#e89c1c; }
.btn-danger { background:#e0245e; color:#fff; }
.btn-danger:hover { background:#c91e52; }
.btn-sm { padding:4px 12px; font-size:12px; }
.search-bar { margin-bottom:20px; display:flex; gap:10px; }
.search-bar input { flex:1; max-width:400px; padding:10px 16px; border:1px solid #ddd; border-radius:8px; font-size:14px; outline:none; }
.search-bar input:focus { border-color:#1da1f2; box-shadow:0 0 0 3px rgba(29,161,242,.15); }
.table-wrap { background:#fff; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,.08); overflow:auto; }
table { width:100%; border-collapse:collapse; }
th { background:#f7f8fa; padding:12px 16px; text-align:left; font-size:13px; font-weight:600; color:#5b7083; border-bottom:1px solid #e1e8ed; white-space:nowrap; }
td { padding:12px 16px; font-size:14px; border-bottom:1px solid #f0f2f5; vertical-align:middle; }
tr:hover td { background:#f8f9fe; }
.badge { display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600; }
.badge-on { background:#e8f5e9; color:#17bf63; }
.badge-off { background:#fce4ec; color:#e0245e; }
.badge-sch { background:#e3f2fd; color:#1da1f2; }
.actions { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
.crawl-status { font-size:11px; color:#666; white-space:nowrap; }
.tag { display:inline-block; padding:2px 8px; background:#f0f2f5; border-radius:4px; font-size:11px; color:#666; margin-right:4px; margin-bottom:2px; }

/* Modal */
.modal-overlay { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,.5); z-index:1000; justify-content:center; align-items:center; }
.modal-overlay.show { display:flex; }
.modal { background:#fff; border-radius:16px; width:90%; max-width:640px; max-height:90vh; overflow-y:auto; padding:28px; box-shadow:0 20px 60px rgba(0,0,0,.3); }
.modal h2 { margin-bottom:20px; font-size:20px; color:#1a1a2e; }
.form-group { margin-bottom:16px; }
.form-group label { display:block; font-size:13px; font-weight:600; color:#5b7083; margin-bottom:6px; }
.form-group input,.form-group textarea,.form-group select { width:100%; padding:10px 14px; border:1px solid #ddd; border-radius:8px; font-size:14px; outline:none; transition:border-color .2s; }
.form-group input:focus,.form-group textarea:focus { border-color:#1da1f2; box-shadow:0 0 0 3px rgba(29,161,242,.15); }
.form-group textarea { min-height:80px; resize:vertical; font-family:inherit; }
.form-row { display:flex; gap:16px; }
.form-row .form-group { flex:1; }
.form-actions { display:flex; gap:10px; justify-content:flex-end; margin-top:24px; }
.empty-state { text-align:center; padding:60px 20px; color:#999; }
.empty-state p { font-size:16px; }
.toast { position:fixed; bottom:24px; right:24px; padding:12px 24px; border-radius:8px; color:#fff; font-size:14px; z-index:2000; animation:slideIn .3s ease; }
.toast-success { background:#17bf63; }
.toast-error { background:#e0245e; }
@keyframes slideIn { from { transform:translateY(20px); opacity:0; } to { transform:translateY(0); opacity:1; } }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🐦 <span>tw-archiver</span> 管理</h1>
    <button class="btn btn-primary" onclick="openAddModal()">+ 新增</button>
  </div>
  <div class="search-bar">
    <input type="text" id="searchInput" placeholder="搜索爬取用户..." onkeyup="searchConfigs()">
  </div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>爬取用户</th>
          <th>爬取提示词</th>
          <th>定时</th>
          <th>状态</th>
          <th>定时表达式</th>
          <th>上次爬取</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="configTable"></tbody>
    </table>
  </div>
</div>

<div class="modal-overlay" id="modalOverlay">
  <div class="modal">
    <h2 id="modalTitle">新增配置</h2>
    <input type="hidden" id="editId">
    <div class="form-group">
      <label>爬取用户 (Twitter用户名)</label>
      <input type="text" id="fCrawlerUser" placeholder="例如: elonmusk">
    </div>
    <div class="form-group">
      <label>爬取提示词 (可选，空格分隔多个关键词)</label>
      <input type="text" id="fPrompt" placeholder="例如: AI bitcoin tesla">
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Cookie ct0</label>
        <input type="text" id="fCt0" placeholder="从浏览器获取">
      </div>
      <div class="form-group">
        <label>Cookie auth_token</label>
        <input type="text" id="fAuthToken" placeholder="从浏览器获取">
      </div>
    </div>
    <div class="form-group">
      <label>其他Cookie (JSON)</label>
      <input type="text" id="fCookieOther" placeholder='{"twid":"..."}'>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>是否定时</label>
        <select id="fScheduled"><option value="0">否</option><option value="1">是</option></select>
      </div>
      <div class="form-group">
        <label>定时表达式 / Cron</label>
        <input type="text" id="fScheduleExpr" placeholder="每天9点 / 0 9 * * *">
      </div>
      <div class="form-group">
        <label>启用</label>
        <select id="fEnabled"><option value="0">否</option><option value="1">是</option></select>
      </div>
    </div>
    <div class="form-actions">
      <button class="btn btn-warning" onclick="closeModal()">取消</button>
      <button class="btn btn-success" onclick="saveConfig()">保存</button>
      <button class="btn btn-primary" onclick="saveAndEnableConfig()">保存并启用</button>
    </div>
  </div>
</div>

<script>
let configs = [];

async function loadConfigs() {
  const res = await fetch('/api/configs');
  configs = await res.json();
  renderTable(configs);
}

function renderTable(data) {
  const tbody = document.getElementById('configTable');
  if (data.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-state"><p>暂无配置，点击右上角新增</p></td></tr>';
    return;
  }
  tbody.innerHTML = data.map(c => {
    const keywords = c.prompt ? c.prompt.split(' ').filter(k => k).map(k => `<span class="tag">${k}</span>`).join('') : '-';
    return `<tr>
      <td><strong>@${c.crawler_user}</strong></td>
      <td>${keywords}</td>
      <td><span class="badge ${c.is_scheduled ? 'badge-sch' : 'badge-off'}">${c.is_scheduled ? '✅' : '❌'}</span></td>
      <td><span class="badge ${c.is_enabled ? 'badge-on' : 'badge-off'}">${c.is_enabled ? '已启用' : '已停用'}</span></td>
      <td>${c.schedule_expr || '-'}</td>
      <td>${c.last_crawl_time ? new Date(c.last_crawl_time).toLocaleString() : '-'}</td>
      <td class="actions">
        <span id="crawlStatus${c.id}" class="crawl-status"></span>
        <button class="btn btn-primary btn-sm" onclick="crawlNow(${c.id})">爬取</button>
        <button class="btn btn-warning btn-sm" onclick="editConfig(${c.id})">编辑</button>
        <button class="btn btn-success btn-sm" onclick="window.open('/reader/${c.id}','_blank')">阅读</button>
        <button class="btn btn-danger btn-sm" onclick="deleteConfig(${c.id})">删除</button>
      </td>
    </tr>`;
  }).join('');
}

function searchConfigs() {
  const q = document.getElementById('searchInput').value;
  if (!q) { renderTable(configs); return; }
  const filtered = configs.filter(c => c.crawler_user.toLowerCase().includes(q.toLowerCase()));
  renderTable(filtered);
}

function openAddModal() {
  document.getElementById('modalTitle').textContent = '新增配置';
  document.getElementById('editId').value = '';
  ['fCrawlerUser','fPrompt','fCt0','fAuthToken','fCookieOther','fScheduleExpr'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('fScheduled').value = '0';
  document.getElementById('fEnabled').value = '0';
  document.getElementById('modalOverlay').classList.add('show');
}

function closeModal() {
  document.getElementById('modalOverlay').classList.remove('show');
}

function getFormData() {
  return {
    crawler_user: document.getElementById('fCrawlerUser').value.trim(),
    prompt: document.getElementById('fPrompt').value.trim(),
    cookie_ct0: document.getElementById('fCt0').value.trim(),
    cookie_auth_token: document.getElementById('fAuthToken').value.trim(),
    cookie_other: document.getElementById('fCookieOther').value.trim() || '{}',
    is_scheduled: document.getElementById('fScheduled').value === '1',
    schedule_expr: document.getElementById('fScheduleExpr').value.trim(),
    is_enabled: document.getElementById('fEnabled').value === '1',
  };
}

async function saveConfig() {
  const data = getFormData();
  if (!data.crawler_user) { alert('请输入爬取用户'); return; }
  const editId = document.getElementById('editId').value;
  const url = editId ? '/api/configs/' + editId : '/api/configs';
  const method = editId ? 'PUT' : 'POST';
  const res = await fetch(url, { method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) });
  if (!res.ok) { const e = await res.json(); showToast(e.detail || '保存失败', 'error'); return; }
  closeModal();
  await loadConfigs();
  showToast('保存成功', 'success');
}

async function saveAndEnableConfig() {
  const data = getFormData();
  if (!data.crawler_user) { alert('请输入爬取用户'); return; }
  data.save_and_enable = true;
  data.is_enabled = true;
  const res = await fetch('/api/configs', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) });
  if (!res.ok) { const e = await res.json(); showToast(e.detail || '保存失败', 'error'); return; }
  closeModal();
  await loadConfigs();
  showToast('已保存并开始爬取', 'success');
}

async function editConfig(id) {
  const cfg = configs.find(c => c.id === id);
  if (!cfg) return;
  document.getElementById('modalTitle').textContent = '编辑配置';
  document.getElementById('editId').value = id;
  document.getElementById('fCrawlerUser').value = cfg.crawler_user;
  document.getElementById('fPrompt').value = cfg.prompt;
  document.getElementById('fCt0').value = cfg.cookie_ct0 || '';
  document.getElementById('fAuthToken').value = cfg.cookie_auth_token || '';
  document.getElementById('fCookieOther').value = JSON.stringify(cfg.cookie_other || {});
  document.getElementById('fScheduled').value = cfg.is_scheduled ? '1' : '0';
  document.getElementById('fScheduleExpr').value = cfg.schedule_expr || '';
  document.getElementById('fEnabled').value = cfg.is_enabled ? '1' : '0';
  document.getElementById('modalOverlay').classList.add('show');
}

async function crawlNow(id) {
  const res = await fetch('/api/configs/' + id + '/crawl', { method:'POST' });
  if (!res.ok) { showToast('爬取失败', 'error'); return; }
  showToast('开始爬取...', 'success');
  // 轮询爬取状态
  const statusEl = document.getElementById('crawlStatus' + id);
  const poll = setInterval(async () => {
    const sr = await fetch('/api/configs/' + id + '/crawl-status');
    const st = await sr.json();
    if (statusEl) {
      if (st.running) {
        statusEl.innerHTML = '<span style="color:#1da1f2;animation:pulse 1s infinite">● ' + (st.progress || '爬取中') + '</span>';
      } else {
        statusEl.innerHTML = '<span style="color:#17bf63">✓ ' + (st.progress || '完成') + '</span>';
        clearInterval(poll);
        await loadConfigs();
      }
    }
  }, 1500);
  // 超时停止轮询
  setTimeout(() => clearInterval(poll), 120000);
}

async function deleteConfig(id) {
  if (!confirm('确定删除此配置？所有推文数据将被清除！')) return;
  await fetch('/api/configs/' + id, { method:'DELETE' });
  await loadConfigs();
  showToast('已删除', 'success');
}

function showToast(msg, type='success') {
  const t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

loadConfigs();
</script>
</body>
</html>"""


def _render_reader(cfg, tweets, author):
    author_name = author["name"] if author else cfg["crawler_user"]
    author_avatar = author["avatar"] if author else ""
    author_bio = author["bio"] if author else ""
    screen_name = author["screen_name"] if author else cfg["crawler_user"]

    tweets_html = ""
    for t in tweets:
        # 处理内容中的链接
        import re
        content = t["content"]
        content = re.sub(r'(https?://\S+)', r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', content)
        # 处理换行
        content = content.replace("\n", "<br>")
        media_html = ""
        for mu in t.get("media_urls", []):
            if mu:
                media_html += f'<img src="{mu}" alt="" loading="lazy">'

        tweets_html += f"""
        <div class="tweet">
            <div class="tweet-header">
                <img class="tweet-avatar" src="{author_avatar}" alt="">
                <div class="tweet-author">
                    <div class="tweet-name">{author_name}</div>
                    <div class="tweet-screen_name">@{screen_name}</div>
                </div>
                <div class="tweet-time">{t['created_at']}</div>
            </div>
            <div class="tweet-content">{content}</div>
            {f'<div class="tweet-media">{media_html}</div>' if media_html else ''}
            <div class="tweet-footer">
                <a href="{t['url']}" target="_blank" rel="noopener noreferrer">查看原文</a>
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>@{screen_name} - tw-archiver</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#f7f9fa; color:#14171a; }}
.profile {{ background:#fff; padding:24px 16px; border-bottom:1px solid #e1e8ed; text-align:center; }}
.profile img {{ width:72px; height:72px; border-radius:50%; margin-bottom:10px; }}
.profile h1 {{ font-size:20px; font-weight:700; }}
.profile .screen_name {{ color:#657786; font-size:14px; margin-bottom:6px; }}
.profile .bio {{ color:#14171a; font-size:14px; max-width:500px; margin:0 auto; }}
.tweets {{ max-width:600px; margin:0 auto; padding:8px; }}
.tweet {{ background:#fff; border-radius:12px; padding:16px; margin-bottom:8px; box-shadow:0 1px 2px rgba(0,0,0,.05); }}
.tweet-header {{ display:flex; align-items:center; gap:10px; margin-bottom:10px; }}
.tweet-avatar {{ width:40px; height:40px; border-radius:50%; }}
.tweet-author {{ flex:1; }}
.tweet-name {{ font-weight:700; font-size:15px; }}
.tweet-screen_name {{ color:#657786; font-size:13px; }}
.tweet-time {{ color:#657786; font-size:12px; white-space:nowrap; }}
.tweet-content {{ font-size:15px; line-height:1.6; word-wrap:break-word; }}
.tweet-content a {{ color:#1da1f2; word-break:break-all; }}
.tweet-media {{ margin-top:10px; }}
.tweet-media img {{ width:100%; border-radius:12px; margin-bottom:4px; }}
.tweet-footer {{ margin-top:10px; padding-top:10px; border-top:1px solid #f0f2f5; }}
.tweet-footer a {{ color:#657786; font-size:13px; text-decoration:none; }}
.back {{ display:block; text-align:center; padding:16px; }}
.back a {{ color:#1da1f2; text-decoration:none; font-size:14px; }}
</style>
</head>
<body>
<div class="profile">
    {f'<img src="{author_avatar}" alt="">' if author_avatar else ''}
    <h1>{author_name}</h1>
    <div class="screen_name">@{screen_name}</div>
    {f'<div class="bio">{author_bio}</div>' if author_bio else ''}
</div>
<div class="tweets">
    {tweets_html if tweets_html else '<div style="text-align:center;padding:40px;color:#999;">暂无推文</div>'}
</div>
<div class="back"><a href="/h5">← 返回汇总</a></div>
</body>
</html>"""


def _render_h5_index(configs):
    cards = ""
    for c in configs:
        cards += f"""
        <a class="user-card" href="/reader/{c['id']}">
            <div class="user-name">@{c['crawler_user']}</div>
            <div class="user-meta">{c['tweet_count']} 条推文 · {'已启用' if c['is_enabled'] else '已停用'}</div>
            <div class="user-time">{'上次: ' + c['last_crawl_time'][:16] if c['last_crawl_time'] else '未爬取'}</div>
        </a>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>tw-archiver 汇总</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; background:#f0f2f5; color:#14171a; }}
.header {{ background:#1da1f2; color:#fff; padding:20px 16px; }}
.header h1 {{ font-size:24px; font-weight:700; }}
.header p {{ font-size:14px; opacity:.85; margin-top:4px; }}
.user-grid {{ max-width:600px; margin:0 auto; padding:12px; }}
.user-card {{ display:block; background:#fff; border-radius:12px; padding:16px; margin-bottom:8px; text-decoration:none; color:#14171a; box-shadow:0 1px 2px rgba(0,0,0,.05); transition:transform .15s; }}
.user-card:active {{ transform:scale(.98); }}
.user-name {{ font-weight:700; font-size:16px; margin-bottom:4px; }}
.user-meta {{ color:#657786; font-size:13px; margin-bottom:2px; }}
.user-time {{ color:#999; font-size:12px; }}
.empty {{ text-align:center; padding:60px 20px; color:#999; font-size:16px; }}
</style>
</head>
<body>
<div class="header">
    <h1>🐦 tw-archiver</h1>
    <p>已爬取用户汇总</p>
</div>
<div class="user-grid">
    {cards if cards else '<div class="empty">还没有爬取数据，先去管理页添加配置吧</div>'}
</div>
</body>
</html>"""
