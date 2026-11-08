"""気象庁の防災情報（無料の公式データ）を5分ごとに見て、新しい大きな出来事があれば、
非公開の投稿リポジトリ（x-poster）の「ニュース速報」をすぐに動かす、見張り専用のプログラムです。

・このリポジトリには、Xの鍵もAIの鍵も、投稿の中身もありません（公開して大丈夫なものだけ）
・見るもの: 最大震度が「震度の基準」以上の地震／大津波警報・津波警報・津波注意報／噴火速報／特別警報
・新しい出来事を見つけたら、GitHub の API で x-poster の news-watch.yml を動かす（鍵は Secrets の X_POSTER_TOKEN）
・速報を書く・確かめる・投稿する・同じ出来事を2回投稿しないのは、x-poster の news_watch.py の役目
・見た出来事は seen.json に残す（同じ出来事で何度も x-poster を動かさないため）

使うのは Python の標準の道具だけです。
  試しに確認する（x-poster を動かさない）: python3 watch.py --dry-run
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen.json"
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))

FEED = "https://www.data.jma.go.jp/developer/xml/feed/{}.xml"
USER_AGENT = "x-poster-watch/1.0"
INTENSITY_ORDER = ["1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"]
INTENSITY_NAMES = {"5弱": "5-", "5強": "5+", "6弱": "6-", "6強": "6+"}
TSUNAMI_LEVELS = ("大津波警報", "津波警報", "津波注意報")
JST = timezone(timedelta(hours=9))


def local(tag):
    return tag.rsplit("}", 1)[-1]


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return ET.fromstring(response.read())


def find_all(element, name):
    return [el for el in element.iter() if local(el.tag) == name]


def text_of(element, name):
    found = find_all(element, name)
    return (found[0].text or "").strip() if found else ""


def entries(feed):
    root = fetch(FEED.format(feed))
    result = []
    for entry in [el for el in root if local(el.tag) == "entry"]:
        link = [el for el in entry if local(el.tag) == "link"]
        result.append({
            "title": text_of(entry, "title"),
            "updated": datetime.fromisoformat(text_of(entry, "updated").replace("Z", "+00:00")),
            "url": link[0].get("href") if link else "",
            "content": text_of(entry, "content"),
        })
    return result


def is_real(root):
    control = [el for el in root if local(el.tag) == "Control"]
    head = [el for el in root if local(el.tag) == "Head"]
    status = text_of(control[0], "Status") if control else ""
    info_type = text_of(head[0], "InfoType") if head else ""
    return status == "通常" and info_type != "取消"


def event_id(root, fallback):
    head = [el for el in root if local(el.tag) == "Head"]
    return (text_of(head[0], "EventID") if head else "") or fallback


def new_events(since):
    """基準を満たす出来事の一覧（キー, 説明）。"""
    threshold = INTENSITY_NAMES.get(CONFIG["震度の基準"], CONFIG["震度の基準"])
    found = []
    for entry in entries("eqvol_l"):  # 地震・津波・火山（過去数日分。実行が遅れても取りこぼさない）
        if entry["updated"] < since:
            continue
        title = entry["title"]
        # 地震は「震源・震度に関する情報」を待つ（「震度速報」は数分早いが震源がなく、速報の文章が書けないため）
        if title not in ("震源・震度に関する情報", "津波警報・注意報・予報a", "噴火速報"):
            continue
        root = fetch(entry["url"])
        if not is_real(root):
            continue
        eid = event_id(root, entry["url"])
        if title == "震源・震度に関する情報":
            values = [el.text.strip() for el in find_all(root, "MaxInt") if el.text and el.text.strip() in INTENSITY_ORDER]
            top = max(values, key=INTENSITY_ORDER.index) if values else None
            if top and INTENSITY_ORDER.index(top) >= INTENSITY_ORDER.index(threshold):
                found.append((f"地震:{eid}", f"最大震度{top}の地震（{title}）"))
        elif title == "噴火速報":
            found.append((f"噴火:{eid}", "噴火速報"))
        else:
            names = {text_of(kind, "Name") for kind in find_all(root, "Kind")}
            levels = [name for name in TSUNAMI_LEVELS if name in names]
            if levels:
                found.append((f"津波:{eid}:{levels[0]}", levels[0]))
    if CONFIG.get("特別警報", True):
        for entry in entries("extra_l"):
            if entry["updated"] < since or "特別警報" not in entry["content"]:
                continue
            office = re.search(r"_(\d{6})\.xml$", entry["url"])
            day = entry["updated"].astimezone(JST).strftime("%Y%m%d")
            found.append((f"特別警報:{office.group(1) if office else entry['url']}:{day}", "特別警報"))
    return list(dict.fromkeys(found))  # 同じ出来事の電文が何本あっても1件にする


def trigger(dry_run=False):
    """x-poster の「ニュース速報」を動かす。dry_run=True なら、お試しモード（投稿しない）で動かす（鍵が使えるかの確認）。"""
    token = os.environ.get("X_POSTER_TOKEN", "")
    if not token:
        raise SystemExit("X_POSTER_TOKEN（Secrets）がありません")
    url = (f"https://api.github.com/repos/{CONFIG['投稿のリポジトリ']}/actions/workflows/"
           f"{CONFIG['動かすワークフロー']}/dispatches")
    body = json.dumps({"ref": "main", "inputs": {"dry_run": "true" if dry_run else "false"}}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.status


def ping():
    """毎日1回の確認: x-poster を、お試しモード（投稿しない）で動かせるか試す。
    動かせなければ、この実行が「失敗」になり、GitHub からメールが届く（鍵の間違い・期限切れに早く気づくため）。"""
    try:
        status = trigger(dry_run=True)
    except Exception as error:
        print(f"x-poster を動かせませんでした（{error}）。X_POSTER_TOKEN の鍵が間違っているか、期限が切れている可能性があります")
        return 1
    print(f"x-poster をお試しモードで動かせました（返事の番号 {status}）。鍵は使えます")
    return 0


def main():
    if "--ping" in sys.argv:
        return ping()
    dry_run = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc)
    seen = json.loads(SEEN_FILE.read_text(encoding="utf-8")) if SEEN_FILE.exists() else {}
    first_time = not seen
    events = new_events(now - timedelta(minutes=float(CONFIG["何分前の電文まで見るか"])))
    fresh = [(key, about) for key, about in events if key not in seen]
    print(f"基準を満たす出来事: {len(events)}件 / まだ扱っていない出来事: {len(fresh)}件")
    for key, about in fresh:
        print(f"  ・{about}（{key}）")

    if fresh and not first_time and not dry_run:
        status = trigger()
        print(f"x-poster の「ニュース速報」を動かしました（返事の番号 {status}）")
    elif fresh and first_time:
        print("はじめての実行なので、記録だけします（x-poster は動かしません）")

    for key, about in fresh:
        seen[key] = {"見た時刻": now.astimezone(JST).isoformat(timespec="seconds"), "内容": about}
    # 古い記録は30日で消す。「最終確認」は、毎日1回この見張りが動いていることを残す（公開リポジトリが60日動きがないと止まるのを防ぐ）
    limit = (now - timedelta(days=30)).astimezone(JST).isoformat()
    seen = {k: v for k, v in seen.items() if k == "最終確認" or v.get("見た時刻", "") >= limit}
    seen["最終確認"] = {"見た時刻": now.astimezone(JST).strftime("%Y-%m-%d"), "内容": "見張りが動いた日"}
    if not dry_run:
        SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
