"""爬虫引擎核心 - 使用 x.com GraphQL API"""
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
READ_SPEED_CHARS_PER_SEC = 6.5
READ_SPEED_WORDS_PER_SEC = 3.3

# 行为延迟范围（秒）
MIN_ACTION_DELAY = 2.0
MAX_ACTION_DELAY = 8.0

# 每次爬取的最大推文数
MAX_TWEETS_PER_CRAWL = 50

# x.com GraphQL 配置
BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
GRAPHQL_ENDPOINT = "https://x.com/i/api/graphql"
QUERY_IDS = {
    "UserByScreenName": "IGgvgiOx4QZndDHuD3x9TQ",
    "UserTweets": "PNd0vlufvrcIwrAnBYKE9g",
}

# 基础 feature switches（从x.com main.js提取）
BASE_FEATURES = [
    "rweb_video_screen_enabled", "rweb_cashtags_enabled",
    "profile_label_improvements_pcf_label_in_post_enabled",
    "responsive_web_profile_redirect_enabled", "rweb_tipjar_consumption_enabled",
    "verified_phone_label_enabled",
    "creator_subscriptions_tweet_preview_api_enabled",
    "responsive_web_graphql_timeline_navigation_enabled",
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled",
    "premium_content_api_read_enabled",
    "communities_web_enable_tweet_community_results_fetch",
    "c9s_tweet_anatomy_moderator_badge_enabled",
    "responsive_web_grok_analyze_button_fetch_trends_enabled",
    "responsive_web_grok_analyze_post_followups_enabled",
    "rweb_cashtags_composer_attachment_enabled",
    "responsive_web_jetfuel_frame",
    "responsive_web_grok_share_attachment_enabled",
    "responsive_web_grok_annotations_enabled",
    "articles_preview_enabled",
    "responsive_web_edit_tweet_api_enabled",
    "rweb_conversational_replies_downvote_enabled",
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled",
    "view_counts_everywhere_api_enabled",
    "longform_notetweets_consumption_enabled",
    "responsive_web_twitter_article_tweet_consumption_enabled",
    "content_disclosure_indicator_enabled",
    "content_disclosure_ai_generated_indicator_enabled",
    "responsive_web_grok_show_grok_translated_post",
    "responsive_web_grok_analysis_button_from_backend",
    "post_ctas_fetch_enabled",
    "freedom_of_speech_not_reach_fetch_enabled",
    "standardized_nudges_misinfo",
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled",
    "longform_notetweets_rich_text_read_enabled",
    "longform_notetweets_inline_media_enabled",
    "responsive_web_grok_image_annotation_enabled",
    "responsive_web_grok_imagine_annotation_enabled",
    "responsive_web_grok_community_note_auto_translation_is_enabled",
    "responsive_web_enhance_cards_enabled",
]

BASE_FIELD_TOGGLES = [
    "withPayments", "withAuxiliaryUserLabels",
    "withArticleRichContentState", "withArticlePlainText",
    "withArticleSummaryText", "withArticleVoiceOver",
    "withGrokAnalyze", "withDisallowedReplyControls",
]


def simulate_reading_time(text: str) -> float:
    """根据文本长度计算模拟阅读时间"""
    if not text:
        return random.uniform(1.0, 3.0)
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    read_time = (chinese_chars / READ_SPEED_CHARS_PER_SEC) + (english_words / READ_SPEED_WORDS_PER_SEC)
    jitter = random.uniform(0.7, 1.3)
    read_time *= jitter
    return max(1.0, min(read_time, 30.0))


def random_delay(min_s: float = MIN_ACTION_DELAY, max_s: float = MAX_ACTION_DELAY):
    """随机延迟模拟人操作间隔"""
    delay = random.uniform(min_s, max_s)
    time.sleep(delay)


def extract_tweet_links(text: str) -> list[str]:
    """从推文文本中提取所有链接"""
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


def _make_graphql_request(client: httpx.Client, operation: str, variables: dict) -> dict:
    """发送GraphQL请求到x.com"""
    qid = QUERY_IDS.get(operation)
    if not qid:
        raise ValueError(f"Unknown operation: {operation}")

    payload = {
        "variables": variables,
        "queryId": qid,
    }
    if operation == "UserTweets":
        payload["features"] = BASE_FEATURES
        payload["fieldToggles"] = BASE_FIELD_TOGGLES

    resp = client.post(
        f"{GRAPHQL_ENDPOINT}/{qid}/{operation}",
        json=payload,
    )
    if resp.status_code != 200:
        raise Exception(f"GraphQL {operation} failed: HTTP {resp.status_code} - {resp.text[:200]}")
    return resp.json()


def _extract_tweets_from_timeline(data: dict) -> list[dict]:
    """从GraphQL timeline中提取推文"""
    tweets = []
    try:
        timeline = data["data"]["user"]["result"]["timeline"]["timeline"]
        for instruction in timeline.get("instructions", []):
            if instruction.get("type") != "TimelineAddEntries":
                continue
            for entry in instruction.get("entries", []):
                item = entry.get("content", {}).get("itemContent", {})
                if item.get("itemType") != "TimelineTweet":
                    continue
                tweet_result = item.get("tweet_results", {}).get("result", {})
                if tweet_result.get("__typename") == "Tweet":
                    tweets.append(tweet_result)
    except (KeyError, TypeError, IndexError) as e:
        logger.warning(f"提取推文失败: {e}")
    return tweets


def _tweet_to_record(tweet: dict, screen_name: str) -> dict | None:
    """将GraphQL推文转为统一记录格式"""
    try:
        core = tweet.get("core", {})
        user_result = core.get("user_results", {}).get("result", {})
        user_core = user_result.get("core", {})
        legacy = tweet.get("legacy", {})
        if not legacy and "__typename" in tweet:
            # 新格式：legacy 可能在别的位置
            legacy = tweet.get("legacy", {})

        if not legacy or not legacy.get("full_text"):
            # 有些推文没有 legacy，跳过
            return None

        tweet_id = tweet.get("rest_id", "")
        full_text = legacy.get("full_text", "")

        # 提取媒体
        media_urls = []
        entities = legacy.get("entities", {})
        extended_entities = legacy.get("extended_entities", {})
        media_list = extended_entities.get("media", []) or entities.get("media", [])
        for m in media_list:
            if "media_url_https" in m:
                media_urls.append(m["media_url_https"])
            elif "media_url" in m:
                media_urls.append(m["media_url"])

        created_at = legacy.get("created_at", "")
        try:
            dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

        return {
            "tweet_id": tweet_id,
            "author_screen_name": screen_name,
            "author_name": user_core.get("name", screen_name),
            "author_avatar": user_result.get("avatar", {}).get("image_url", "").replace("_normal", "_400x400"),
            "author_bio": user_result.get("legacy", {}).get("description", user_result.get("profile_bio", {}).get("description", "")),
            "content": full_text,
            "created_at": created_at,
            "url": f"https://x.com/{screen_name}/status/{tweet_id}",
            "media_urls": media_urls,
        }
    except Exception as e:
        logger.warning(f"解析推文记录失败: {e}")
        return None


def crawl_user_tweets(config: dict, max_tweets: int = MAX_TWEETS_PER_CRAWL,
                     status_callback=None) -> tuple[int, list[str]]:
    """
    爬取指定用户的推文（使用 x.com GraphQL API）
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
        "Authorization": f"Bearer {BEARER_TOKEN}",
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Client-Language": "zh",
    }

    try:
        with httpx.Client(
            proxy="http://127.0.0.1:10809",
            headers=headers,
            timeout=30,
            follow_redirects=True,
        ) as client:
            random_delay()

            # 1. 获取用户信息
            if status_callback:
                status_callback(f"正在获取用户 @{screen_name} 信息...")

            user_data = _make_graphql_request(client, "UserByScreenName", {
                "screen_name": screen_name,
                "withGrokTranslatedBio": False,
            })

            user_result = user_data.get("data", {}).get("user", {}).get("result", {})
            rest_id = user_result.get("rest_id", "")
            if not rest_id:
                return 0, [f"无法获取用户 @{screen_name} 的ID"]

            author_name = user_result.get("core", {}).get("name", screen_name)
            author_avatar = user_result.get("avatar", {}).get("image_url", "").replace("_normal", "_400x400")
            author_bio = user_result.get("legacy", {}).get("description", "")

            # 模拟阅读用户资料
            simulate_reading_time(f"{author_name} {author_bio}")
            random_delay()

            # 2. 获取推文列表
            if status_callback:
                status_callback(f"正在获取 @{screen_name} 的推文...")

            has_more = True
            cursor = None
            all_tweets = []

            while has_more and len(all_tweets) < max_tweets:
                tweet_vars = {
                    "userId": rest_id,
                    "count": min(max_tweets - len(all_tweets), 20),
                    "includePromotedContent": False,
                    "withQuickPromoteEligibilityTweetReach": False,
                    "withVoice": False,
                    "withV2Timeline": True,
                }
                if cursor:
                    tweet_vars["cursor"] = cursor

                tweets_data = _make_graphql_request(client, "UserTweets", tweet_vars)
                tweets = _extract_tweets_from_timeline(tweets_data)
                all_tweets.extend(tweets)

                # 获取下一页cursor
                try:
                    timeline = tweets_data["data"]["user"]["result"]["timeline"]["timeline"]
                    for instruction in timeline.get("instructions", []):
                        if instruction.get("type") == "TimelineAddEntries":
                            for entry in reversed(instruction.get("entries", [])):
                                eid = entry.get("entryId", "")
                                if eid.startswith("cursor-bottom-"):
                                    cursor = entry.get("content", {}).get("value", "")
                                    has_more = bool(cursor)
                                    break
                            else:
                                has_more = False
                        elif instruction.get("type") == "TimelineClearCache":
                            pass
                except Exception:
                    has_more = False

                if cursor and len(all_tweets) < max_tweets:
                    random_delay(3, 8)

            if status_callback:
                status_callback(f"获取到 {len(all_tweets)} 条推文，开始处理...")

            # 3. 过滤和保存推文
            prompt_keywords = []
            if prompt:
                prompt_keywords = [k.strip().lower() for k in prompt.split() if k.strip()]

            for idx, tweet in enumerate(all_tweets):
                record = _tweet_to_record(tweet, screen_name)
                if not record:
                    continue

                # 关键词过滤
                if prompt_keywords:
                    full_text_lower = record["content"].lower()
                    if not any(kw in full_text_lower for kw in prompt_keywords):
                        continue

                save_tweet(config["id"], record)
                count += 1

                if status_callback:
                    status_callback(f"爬取进度: {count}/已处理{idx+1}条")

                # 提取链接
                links = extract_tweet_links(record["content"])
                for link in links:
                    resolved = resolve_url(link, client)
                    from app.api.tweets import save_link
                    save_link(config["id"], record["tweet_id"], link, resolved)
                    random_delay(0.5, 2.0)

                # 模拟阅读每条推文
                read_time = simulate_reading_time(record["content"])
                time.sleep(read_time)

            if status_callback and count == 0 and len(all_tweets) > 0:
                status_callback(f"共 {len(all_tweets)} 条推文均被关键词过滤，无新增")

    except Exception as e:
        errors.append(f"爬取异常: {str(e)}")
        logger.exception("爬取过程中出错")

    return count, errors
