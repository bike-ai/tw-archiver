"""爬虫引擎核心 - 模拟人行为"""
import time
import random
import re
import json
import logging
from datetime import datetime
from typing import Optional

import httpx

from app.api.tweets import save_tweet, get_tweet_author_info

logger = logging.getLogger(__name__)

# 阅读速度：中文约400字/分钟，英文约200词/分钟
READ_SPEED_CHARS_PER_SEC = 6.5  # 中文字符
READ_SPEED_WORDS_PER_SEC = 3.3   # 英文单词

# 行为延迟范围（秒）
MIN_ACTION_DELAY = 2.0
MAX_ACTION_DELAY = 8.0

# 页面滚动间隔
SCROLL_INTERVAL_MIN = 4.0
SCROLL_INTERVAL_MAX = 12.0

# 每次爬取的最大推文数
MAX_TWEETS_PER_CRAWL = 50


def simulate_reading_time(text: str) -> float:
    """根据文本长度计算模拟阅读时间"""
    if not text:
        return random.uniform(1.0, 3.0)
    # 粗略统计中英文混合
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    read_time = (chinese_chars / READ_SPEED_CHARS_PER_SEC) + (english_words / READ_SPEED_WORDS_PER_SEC)
    # 加随机抖动 ±30%
    jitter = random.uniform(0.7, 1.3)
    read_time *= jitter
    # 至少1秒，最长30秒
    return max(1.0, min(read_time, 30.0))


def random_delay(min_s: float = MIN_ACTION_DELAY, max_s: float = MAX_ACTION_DELAY):
    """随机延迟模拟人操作间隔"""
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


def extract_tweet_links(text: str) -> list[str]:
    """从推文文本中提取所有链接，包括 t.co 短链接"""
    urls = re.findall(r'https?://t\.co/\w+', text)
    urls += re.findall(r'https?://(?!t\.co)[^\s]+', text)
    return list(set(urls))


def resolve_url(url: str, client: httpx.Client) -> str:
    """解析 t.co 短链接为真实URL"""
    try:
        resp = client.head(url, follow_redirects=True, timeout=10)
        return str(resp.url)
    except Exception:
        return url


def parse_twitter_api_tweet(tweet: dict) -> dict:
    """将 Twitter API v2 返回的推文数据解析为统一格式"""
    # 支持 v2 api 和 graphql 两种格式
    entities = tweet.get("entities", {})

    # v2 api 格式
    if "text" in tweet:
        # 提取媒体
        media_urls = []
        attachments = tweet.get("attachments", {})
        if "media_keys" in attachments:
            media_urls = attachments.get("media_keys", [])

        return {
            "tweet_id": tweet.get("id", ""),
            "author_screen_name": "",
            "author_name": tweet.get("author_name", ""),
            "author_avatar": tweet.get("author_profile_image_url", ""),
            "author_bio": tweet.get("author_description", ""),
            "content": tweet.get("text", ""),
            "created_at": tweet.get("created_at", ""),
            "url": f"https://x.com/i/web/status/{tweet.get('id', '')}",
            "media_urls": media_urls,
        }
    return None


def crawl_user_tweets(config: dict, max_tweets: int = MAX_TWEETS_PER_CRAWL) -> tuple[int, list[str]]:
    """
    爬取指定用户的推文
    返回 (爬取成功数, [错误信息])
    """
    errors = []
    count = 0

    cookie_ct0 = config.get("cookie_ct0", "")
    cookie_auth_token = config.get("cookie_auth_token", "")
    if not cookie_ct0 or not cookie_auth_token:
        return 0, ["缺少必要cookie: ct0 或 auth_token"]

    screen_name = config.get("crawler_user", "").lstrip("@")
    prompt = config.get("prompt", "")

    # 准备Cookie
    cookie_str = f"ct0={cookie_ct0}; auth_token={cookie_auth_token}"
    other_cookies = config.get("cookie_other", {})
    if isinstance(other_cookies, dict):
        for k, v in other_cookies.items():
            cookie_str += f"; {k}={v}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cookie": cookie_str,
        "Referer": f"https://x.com/{screen_name}",
        "Origin": "https://x.com",
        "X-Csrf-Token": cookie_ct0,
        "Authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
                          "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
    }

    try:
        with httpx.Client(
            proxy="http://127.0.0.1:10809",
            headers=headers,
            timeout=30,
            follow_redirects=True,
        ) as client:
            # 先访问用户页面获取用户信息
            random_delay()

            # 使用 X API 的 UserTweets 端点
            # 首先获取用户ID
            user_by_screen_name_url = f"https://api.x.com/1.1/users/show.json?screen_name={screen_name}"
            resp = client.get(user_by_screen_name_url)
            simulate_reading_time(resp.text[:200])
            random_delay()

            if resp.status_code != 200:
                return 0, [f"获取用户信息失败: HTTP {resp.status_code}"]

            user_data = resp.json()
            user_id = user_data.get("id_str", "")
            author_name = user_data.get("name", screen_name)
            author_avatar = user_data.get("profile_image_url_https", "").replace("_normal", "_400x400")
            author_bio = user_data.get("description", "")

            # 获取用户时间线
            timeline_url = f"https://api.x.com/1.1/statuses/user_timeline.json"
            params = {
                "user_id": user_id,
                "count": min(max_tweets, 200),
                "include_rts": 1,
                "exclude_replies": 0,
                "tweet_mode": "extended",
                "include_ext_alt_text": "true",
                "include_entities": "true",
            }

            resp = client.get(timeline_url, params=params)
            simulate_reading_time(resp.text[:500])
            random_delay()

            if resp.status_code != 200:
                return 0, [f"获取推文失败: HTTP {resp.status_code}"]

            tweets = resp.json()
            if isinstance(tweets, dict) and "errors" in tweets:
                return 0, [f"API错误: {json.dumps(tweets['errors'][:3])}"]

            if not isinstance(tweets, list):
                return 0, [f"意外的响应格式"]

            # 过滤已爬取过的
            new_tweets = tweets[:max_tweets]

            for tweet_data in new_tweets:
                tweet = tweet_data.get("retweeted_status", tweet_data)
                full_text = tweet.get("full_text", tweet.get("text", ""))

                # 如果有prompt，检查是否匹配
                if prompt:
                    prompt_keywords = [k.strip().lower() for k in prompt.split() if k.strip()]
                    if prompt_keywords and not any(kw in full_text.lower() for kw in prompt_keywords):
                        continue

                # 提取媒体
                media_urls = []
                entities = tweet.get("entities", {})
                if "media" in entities:
                    for m in entities["media"]:
                        if "media_url_https" in m:
                            media_urls.append(m["media_url_https"])
                        elif "media_url" in m:
                            media_urls.append(m["media_url"])

                tweet_id = tweet.get("id_str", "")
                created_at = tweet.get("created_at", "")

                # 转换Twitter日期格式
                try:
                    dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
                    created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass

                tweet_record = {
                    "tweet_id": tweet_id,
                    "author_screen_name": screen_name,
                    "author_name": author_name,
                    "author_avatar": author_avatar,
                    "author_bio": author_bio,
                    "content": full_text,
                    "created_at": created_at,
                    "url": f"https://x.com/{screen_name}/status/{tweet_id}",
                    "media_urls": media_urls,
                }

                save_tweet(config["id"], tweet_record)
                count += 1

                # 提取链接并保存
                links = extract_tweet_links(full_text)
                for link in links:
                    resolved = resolve_url(link, client)
                    from app.api.tweets import save_link
                    save_link(config["id"], tweet_id, link, resolved)
                    random_delay(0.5, 2.0)

                # 模拟阅读每条推文
                read_time = simulate_reading_time(full_text)
                time.sleep(read_time)

    except Exception as e:
        errors.append(f"爬取异常: {str(e)}")
        logger.exception("爬取过程中出错")

    return count, errors
