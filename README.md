# tw-archiver 🐦

Twitter爬虫管理平台 —— 模拟人行为爬取推文，H5阅读，定时调度。

## 功能

- **配置管理** — 新增/编辑/删除爬取配置，支持搜索
- **智能爬虫** — 模拟人阅读速度+随机延迟，解析t.co短链接，防封
- **实时状态** — 点"爬取"实时看到进度
- **H5阅读** — 每用户独立阅读页，头像/内容/媒体/链接
- **汇总索引** — 左侧用户列表，点进子页面
- **定时调度** — 支持"每天9点"或标准cron表达式

## 技术栈

Python FastAPI + SQLite + 原生前端（无框架）

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 5010
# 或
./start.sh start

# 停止
./start.sh stop
```

## 使用

1. 打开管理页 → http://localhost:5010/
2. 点「新增」，填 Twitter用户名 和 Cookie（ct0 + auth_token）
3. 点「保存并启用」→ 自动开始爬取
4. 点「阅读」看爬取结果

### Cookie 获取

浏览器打开 x.com 登录后，按 F12 → Application → Cookies → x.com，复制：
- `ct0` 的值
- `auth_token` 的值

## 目录结构

```
tw-archiver/
├── app/
│   ├── main.py           # FastAPI 应用 + 页面渲染
│   ├── api/
│   │   ├── config.py     # 配置 CRUD
│   │   └── tweets.py     # 推文存储/查询
│   ├── crawler/
│   │   └── engine.py     # 爬虫引擎（模拟人行为）
│   └── models/
│       └── database.py   # SQLite 表结构
├── data/                 # SQLite 数据库
├── start.sh              # 启动/停止脚本
└── requirements.txt
```
