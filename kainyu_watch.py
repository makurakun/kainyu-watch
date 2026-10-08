#!/usr/bin/env python3
"""
介入ウォッチ: 財務相・財務官などの「円安けん制発言」をニュースから拾ってスマホに通知する。

データ元 : Google ニュース RSS（無料・キー不要）
レート   : Yahoo Finance チャートAPI（無料・キー不要）
通知     : ntfy（無料）

環境変数
  NTFY_TOPIC     必須。ntfy のトピック名（推測されにくい文字列に）
  MIN_LEVEL      通知する最低レベル 1〜3（既定 2）
  MAX_AGE_MIN    何分前までの記事を対象にするか（既定 180）
  STATE_FILE     既読記録ファイル（既定 kainyu_seen.json）
"""
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MIN_LEVEL = int(os.environ.get("MIN_LEVEL", "2"))
MAX_AGE_MIN = int(os.environ.get("MAX_AGE_MIN", "180"))
STATE_FILE = os.environ.get("STATE_FILE", "kainyu_seen.json")

# ニュース検索クエリ（Google ニュースの検索語）
QUERIES = [
    "片山財務相 為替",
    "片山財務相 円安",
    "三村財務官",
    "財務官 円安",
    "官房長官 為替 円安",
    "高市首相 円安",
    "為替介入",
    "レートチェック 円",
    "ベセント 円",
]

# 発言者・当局（タイトルにどれか必要）
SPEAKERS = ["片山", "財務相", "三村", "財務官", "財務省", "官房長官", "高市", "首相",
            "ベセント", "米財務長官", "政府・日銀", "政府、日銀", "当局"]

# 為替の話であること（タイトルにどれか必要）
FX_WORDS = ["為替", "円安", "円相場", "ドル円", "円買い", "介入", "円高", "レートチェック"]

# 強さレベル（数字が大きいほど介入に近い）
LEVEL3 = ["タイミングが近づ", "退避勧告", "避難勧告", "介入を実施", "介入実施", "介入した",
          "円買い介入", "協調介入", "レートチェック", "スマホを離さ", "断固たる措置を取るタイミング"]
LEVEL2 = ["断固たる", "果断", "あらゆる手段", "あらゆる措置", "フリーハンド", "排除しない",
          "投機的", "行き過ぎ", "憂慮", "一方的", "一方向", "急激", "しっかり対応", "適切な対応",
          "適切に対応", "満足も安心も"]
LEVEL1 = ["緊張感", "注視", "けん制", "牽制", "警戒"]

LEVEL_LABEL = {3: "🚨 介入直前クラス", 2: "⚠️ 強いけん制", 1: "ℹ️ けん制"}
LEVEL_PRIORITY = {3: 5, 2: 4, 1: 3}  # ntfy priority（5=最大）

UA = {"User-Agent": "Mozilla/5.0 (kainyu-watch)"}


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": []}


def save_state(state):
    state["seen"] = state["seen"][-800:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def fetch_news(query):
    q = urllib.parse.quote(f"{query} when:1d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        src = (it.findtext("source") or "").strip()
        try:
            ts = parsedate_to_datetime(pub).timestamp() if pub else 0
        except (TypeError, ValueError):
            ts = 0
        items.append({"title": title, "link": link, "ts": ts, "source": src})
    return items


def clean_title(title, source):
    # 末尾の " - 媒体名" を落とす
    if source and title.endswith(" - " + source):
        return title[: -len(" - " + source)].strip()
    return re.sub(r"\s+-\s+[^-]{1,30}$", "", title).strip()


def dedupe_key(title):
    t = re.sub(r"[\s「」『』（）()【】・、。,.!！?？:：=＝\-－—…]", "", title)
    return t[:28]


# 為替担当の当局者（見出しに「為替」がなくても強い言葉なら拾う）
FX_OFFICIALS = ["片山", "三村", "財務相", "財務官", "ベセント", "米財務長官"]


# 過去の介入を振り返る記事（議事要旨・実施額の公表など）は「けん制」扱いに下げる
RETRO_WORDS = ["議事要旨", "判明", "実施額", "介入額", "公表", "振り返", "明らかに", "効果は", "検証"]
RETRO_RE = re.compile(r"\d+月(\d+日)?の.{0,12}介入")


def is_retro(title):
    return any(w in title for w in RETRO_WORDS) or bool(RETRO_RE.search(title))


def classify(title):
    level, hits = _classify(title)
    if level >= 2 and is_retro(title):
        return 1, hits
    return level, hits


def _classify(title):
    if not any(w in title for w in SPEAKERS):
        return 0, []
    has_fx = any(w in title for w in FX_WORDS)
    is_official = any(w in title for w in FX_OFFICIALS)
    for level, words in ((3, LEVEL3), (2, LEVEL2), (1, LEVEL1)):
        hits = [w for w in words if w in title]
        if hits and (has_fx or (level >= 2 and is_official)):
            return level, hits
    if has_fx:
        return 1, []  # 当局×為替の話題だが特定ワードなし
    return 0, []


def fetch_usdjpy():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/JPY=X?interval=5m&range=1d"
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        return None


def notify(level, title, source, link, hits, rate):
    lines = [title]
    if source:
        lines.append(f"（{source}）")
    if hits:
        lines.append("キーワード: " + "、".join(hits))
    if rate:
        lines.append(f"ドル円: {rate:.2f}円")
    payload = {
        "topic": NTFY_TOPIC,
        "title": f"{LEVEL_LABEL[level]}｜円安けん制",
        "message": "\n".join(lines),
        "priority": LEVEL_PRIORITY[level],
        "tags": ["yen"],
    }
    if link:
        payload["click"] = link
    r = requests.post("https://ntfy.sh", json=payload, timeout=15)
    r.raise_for_status()


def main():
    if not NTFY_TOPIC:
        print("NTFY_TOPIC が未設定です", file=sys.stderr)
        sys.exit(1)

    state = load_state()
    seen = set(state["seen"])
    now = time.time()
    found = {}

    for q in QUERIES:
        try:
            items = fetch_news(q)
        except Exception as e:
            print(f"[warn] 取得失敗 {q}: {e}", file=sys.stderr)
            continue
        for it in items:
            if it["ts"] and now - it["ts"] > MAX_AGE_MIN * 60:
                continue
            title = clean_title(it["title"], it["source"])
            key = dedupe_key(title)
            if key in seen or key in found:
                continue
            level, hits = classify(title)
            if level == 0:
                continue
            found[key] = (level, title, it, hits)
        time.sleep(1)

    targets = sorted(found.items(), key=lambda kv: (-kv[1][0], kv[1][2]["ts"]))
    rate = fetch_usdjpy() if any(v[0] >= MIN_LEVEL for _, v in targets) else None

    sent = 0
    for key, (level, title, it, hits) in targets:
        if level >= MIN_LEVEL and sent < 5:  # 1回あたり最大5件
            try:
                notify(level, title, it["source"], it["link"], hits, rate)
                sent += 1
                print(f"[通知 L{level}] {title}")
            except Exception as e:
                print(f"[warn] 通知失敗: {e}", file=sys.stderr)
                continue
        state["seen"].append(key)  # レベル未満も既読扱い（後で重複しないように）

    save_state(state)
    print(f"候補 {len(targets)} 件 / 通知 {sent} 件")


if __name__ == "__main__":
    main()
