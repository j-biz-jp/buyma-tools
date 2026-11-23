# -*- coding: utf-8 -*-
"""
BUYMA トレンドレーダーのデータ(trend/data.json)を更新するプログラム。

BUYMA公式の「人気アイテムランキング」と「人気ブランドランキング」のページを
1回ずつ読み込み、ランキング情報を取り出して data.json に保存します。
AIは使いません。GitHub Actions から毎朝自動で実行されます。

手元のパソコンで試す場合:
    pip install requests beautifulsoup4
    python scripts/update_trend.py
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "trend" / "data.json"

ITEM_URL = "https://www.buyma.com/rank/"
BRAND_URL = "https://www.buyma.com/rank/fashion.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.8",
}
TOP_N = 100          # 取り込むアイテムの件数
MIN_ITEMS = 80       # これより少なければ失敗とみなして保存しない
KEEP_DAYS = 30       # 何日分の履歴を残すか

# 「ハイブランドの人気商品」に使うブランド(ブランドランキングの上位から最大5つ)
LUXURY = [
    "MAX MARA", "PRADA", "CHANEL", "HERMES", "MONCLER", "CELINE", "LOEWE",
    "BOTTEGA VENETA", "MIUMIU", "MAISON MARGIELA", "BALENCIAGA", "GUCCI",
    "SAINT LAURENT", "LOUIS VUITTON", "CHRISTIAN DIOR", "FENDI", "BURBERRY",
    "VALENTINO", "GIVENCHY", "TOD'S",
]
USED = re.compile(r"中古|ランク[:：]")
IMG = re.compile(r"/imgdata/item/([0-9]+/[0-9]+/[0-9]+)/")


def img_key(node):
    """商品写真のURLから、写真を表す短い文字列(例 260921/0138130766/838142298)を取り出す"""
    img = node.select_one("img") if node else None
    m = IMG.search((img.get("src") or img.get("data-src") or "") if img else "")
    return m.group(1) if m else ""


def jst_today():
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")


def clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("　", " ")).strip()


def fetch(url):
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    res.encoding = res.apparent_encoding or "utf-8"
    return res.text


def updated_date(soup):
    m = re.search(r"(\d{4}\.\d{2}\.\d{2})\s*更新", soup.get_text(" "))
    return m.group(1) if m else ""


def parse_items(html):
    soup = BeautifulSoup(html, "html.parser")
    rows, seen = [], set()
    for li in soup.select("li.bc-ranking__item"):
        a = li.select_one("a[data-ga-item-id]")
        if not a:
            continue
        try:
            rank = int(a.get("data-ga-index", "0"))
            price = int(a.get("data-ga-price", "0"))
        except ValueError:
            continue
        item_id = a.get("data-ga-item-id", "")
        if not item_id or rank < 1 or rank > TOP_N or item_id in seen:
            continue
        seen.add(item_id)
        brand = clean(a.get("data-ga-item-brand", ""))
        if brand.lower() == "null":
            brand = ""
        rows.append([rank, item_id, brand, clean(a.get("data-ga-item-name", "")), price,
                     clean(a.get("data-ga-item-category", "")), img_key(li)])
    rows.sort(key=lambda r: r[0])
    return rows, updated_date(soup)


def parse_brands(html):
    soup = BeautifulSoup(html, "html.parser")
    blocks = []
    for box in soup.select("div.ranking-contents__container"):
        en = box.select_one(".ranking__name--en")
        if not en:
            continue
        jp = box.select_one(".ranking__name--jp")
        items = []
        for pos, li in enumerate(box.select("li.ranking-contents__list-item"), start=1):
            a = li.select_one("a[data-ga-item-id]")
            if not a:
                continue
            name = clean(a.get("data-ga-item-name", ""))
            try:
                price = int(a.get("data-ga-price", "0"))
            except ValueError:
                continue
            items.append([pos, a.get("data-ga-item-id", ""), name, price, img_key(li)])
        blocks.append({"brand": clean(en.get_text()), "kana": clean(jp.get_text() if jp else ""), "items": items})
    brands = [[b["brand"], b["kana"]] for b in blocks[:20]]
    luxury = []
    for b in blocks:
        if b["brand"].upper() in LUXURY:
            items = [i for i in b["items"] if not USED.search(i[2])][:5]
            if items:
                luxury.append({"brand": b["brand"], "kana": b["kana"], "items": items})
        if len(luxury) >= 5:
            break
    return brands, luxury, updated_date(soup)


def main():
    old = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}

    items, items_upd = parse_items(fetch(ITEM_URL))
    time.sleep(3)
    brands, luxury, brands_upd = parse_brands(fetch(BRAND_URL))

    if len(items) < MIN_ITEMS:
        print(f"[エラー] アイテムが {len(items)} 件しか取れませんでした。保存せずに終了します。"
              "BUYMAのページの作りが変わった可能性があります。")
        return 1
    if len(brands) < 10:
        print(f"[注意] ブランドが {len(brands)} 件しか取れませんでした。ブランドは前回の内容を使います。")
        brands, luxury, brands_upd = old.get("brands", []), old.get("luxury", []), old.get("brandsUpdated", "")

    today = jst_today()
    # 履歴の日付は「BUYMAがランキングを更新した日」にする。
    # BUYMAがまだ更新していない朝は、前日と同じ日付として上書きされるので、同じデータが2日分並ばない。
    day = items_upd.replace(".", "-") if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", items_upd or "") else today
    history = [h for h in old.get("history", []) if h.get("date") != day]
    history.append({"date": day, "items": items})
    history = sorted(history, key=lambda h: h["date"])[-KEEP_DAYS:]

    data = {
        "fetchedAt": today,
        "itemsUpdated": items_upd or old.get("itemsUpdated", ""),
        "brandsUpdated": brands_upd or old.get("brandsUpdated", ""),
        "brands": brands,
        "history": history,
        "luxury": luxury or old.get("luxury", []),
    }
    DATA.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    top = items[0]
    print(f"[完了] {today} 更新 / アイテム {len(items)}件 / ブランド {len(brands)}件 / "
          f"ハイブランド {len(luxury)}件 / 1位: {top[2]} {top[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
