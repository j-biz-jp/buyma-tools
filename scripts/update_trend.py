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


# ジャンル別ランキング(服以外もカバーするため)。BUYMAのカテゴリ別ランキングを上位20件ずつ取り込む
CATS = [("bags", "バッグ"), ("wallet-accessories", "財布・小物"), ("accessories", "アクセサリー"),
        ("shoes", "靴"), ("watches", "腕時計"), ("eyewear", "アイウェア")]
CAT_TOP = 20


def fetch_cats(old_cats):
    cats = {}
    for slug, label in CATS:
        entry = {"label": label}
        for sex in ("women", "men"):
            try:
                time.sleep(3)
                rows, _ = parse_items(fetch(f"https://www.buyma.com/rank/{sex}/{slug}/"))
                entry[sex] = rows[:CAT_TOP]
            except Exception as e:
                print(f"[注意] {sex}/{slug} のランキングを読めませんでした: {e}")
                entry[sex] = (old_cats.get(slug) or {}).get(sex, [])
        cats[slug] = entry
    return cats


PULSE_WAIT = 1.5     # 1商品ごとに空ける秒数(BUYMAに負担をかけないため)
RE_ACCESS = re.compile(r"アクセス[:：]\s*([\d,]+)")
RE_FAV = re.compile(r"お気に入りアイテム登録[:：]\s*([\d,]+)\s*人")


def fetch_pulse(ids):
    """商品ページから、公開されているアクセス数とお気に入り登録数を読む(1日1回)"""
    out = {}
    for item_id in ids:
        try:
            time.sleep(PULSE_WAIT)
            text = BeautifulSoup(fetch(f"https://www.buyma.com/item/{item_id}/"), "html.parser").get_text(" ")
            a, f = RE_ACCESS.search(text), RE_FAV.search(text)
            if a or f:
                out[item_id] = [int(a.group(1).replace(",", "")) if a else None,
                                int(f.group(1).replace(",", "")) if f else None]
        except Exception as e:
            print(f"[注意] 商品 {item_id} のアクセス数を読めませんでした: {e}")
    return out


FX_URL = "https://api.frankfurter.dev/v1/latest?base=JPY&symbols=USD,EUR,GBP,KRW,HKD,CNY,AUD,CAD,CHF"


def fetch_fx():
    """利益チェッカー用の為替レート(欧州中央銀行の参考レート)。1外貨=何円 の形にする"""
    try:
        res = requests.get(FX_URL, timeout=20)
        res.raise_for_status()
        j = res.json()
        rates = {k: round(1 / v, 4 if k == "KRW" else 2) for k, v in j.get("rates", {}).items() if v}
        if len(rates) >= 4:
            return {"date": j.get("date", ""), "jpy": rates}
    except Exception as e:  # 為替が取れなくてもランキングの更新は続ける
        print(f"[注意] 為替レートを取得できませんでした: {e}")
    return None


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

    cats = fetch_cats(old.get("cats", {}))
    pulse = list(old.get("pulse", []))
    got = {}
    if not any(p.get("date") == today for p in pulse):  # 同じ日の2回目(お昼の回)は読み直さない
        ids = []
        for row in items + [r for c in cats.values() for sx in ("women", "men") for r in c.get(sx, [])]:
            if row[1] not in ids:
                ids.append(row[1])
        got = fetch_pulse(ids)
        if got:
            pulse.append({"date": today, "s": got})
    pulse = sorted(pulse, key=lambda p: p["date"])[-KEEP_DAYS:]

    data = {
        "fetchedAt": today,
        "itemsUpdated": items_upd or old.get("itemsUpdated", ""),
        "brandsUpdated": brands_upd or old.get("brandsUpdated", ""),
        "brands": brands,
        "history": history,
        "luxury": luxury or old.get("luxury", []),
        "fx": fetch_fx() or old.get("fx"),
        "pulse": pulse,
        "cats": cats,
    }
    DATA.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    top = items[0]
    print(f"[完了] {today} 更新 / アイテム {len(items)}件 / ブランド {len(brands)}件 / "
          f"ハイブランド {len(luxury)}件 / 注目度 {len(got)}件 / 1位: {top[2]} {top[3]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
