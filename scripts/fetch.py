"""Collect competitor videos for タロットサムネ帳.

Reads data/channels.json (a list of @handles), and for each channel combines:
  - the channel's /videos page: channel id, name, subscribers, the latest ~30 long videos with durations
  - the channel's long-video RSS feed: exact view counts and publish times for the latest 15
Results are merged into data/videos.json, keeping history from earlier runs.
Standard library only, so it runs on a plain GitHub Actions runner.
"""
import json
import re
import statistics
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANNELS = ROOT / "data" / "channels.json"
OUT = ROOT / "data" / "videos.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
KEEP_DAYS = 400       # drop videos older than this
KEEP_PER_CHANNEL = 150
BASE_COUNT = 30       # "usual views" = median of this many latest videos
BASE_MIN_AGE = 3      # ...ignoring videos younger than this many days (they are still growing)

NOW = datetime.now(timezone.utc)


def get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ja-JP,ja;q=0.9",
        "Cookie": "PREF=hl=ja&gl=JP; CONSENT=YES+",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 - retry on any network error
            if attempt == 2:
                raise
            time.sleep(3 + attempt * 3)


def find(o, key, acc):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                acc.append(v)
            find(v, key, acc)
    elif isinstance(o, list):
        for v in o:
            find(v, key, acc)
    return acc


def parse_count(t):
    """'1.2万' / '12,345' / '1.5K' -> int"""
    t = t.replace(",", "")
    m = re.search(r"([\d.]+)\s*(万|億|K|M|B)?", t)
    if not m:
        return 0
    mult = {"万": 1e4, "億": 1e8, "K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2), 1)
    return int(float(m.group(1)) * mult)


def parse_ago(t):
    """'5 日前' / '2 weeks ago' -> days (float), or None"""
    m = re.search(r"(\d+)\s*(秒|分|時間|日|週間|か月|ヶ月|年|second|minute|hour|day|week|month|year)", t)
    if not m:
        return None
    unit = {"秒": 0, "分": 0, "時間": 1 / 24, "日": 1, "週間": 7, "か月": 30, "ヶ月": 30, "年": 365,
            "second": 0, "minute": 0, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
    return int(m.group(1)) * unit


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scrape_page(handle):
    html = get("https://www.youtube.com/" + urllib.parse.quote(handle) + "/videos")
    i = html.find("ytInitialData = ")
    if i < 0:
        raise ValueError("ytInitialData not found")
    data, _ = json.JSONDecoder().raw_decode(html[i + len("ytInitialData = "):])
    meta = find(data, "channelMetadataRenderer", [])
    cid = meta[0]["externalId"] if meta else None
    name = meta[0].get("title") if meta else None
    subs = 0
    for parts in find(data, "metadataParts", []):
        for p in parts:
            c = p.get("text", {}).get("content", "")
            if "登録者" in c or "subscriber" in c:
                subs = parse_count(c.replace("チャンネル登録者数", ""))
    videos = []
    for lv in find(data, "lockupViewModel", []):
        vid = lv.get("contentId")
        md = lv.get("metadata", {}).get("lockupMetadataViewModel")
        if not vid or not md:
            continue
        parts = [p["text"]["content"]
                 for row in md.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                 for p in row.get("metadataParts", []) if "text" in p]
        badges = find(lv.get("contentImage", {}), "thumbnailBadgeViewModel", [])
        videos.append({
            "id": vid,
            "title": md["title"]["content"],
            "views": parse_count(parts[0]) if parts else 0,
            "ago": parse_ago(parts[1]) if len(parts) > 1 else None,
            "dur": badges[0].get("text", "") if badges else "",
        })
    return {"id": cid, "name": name, "subs": subs, "videos": videos}


def read_feed(cid):
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "m": "http://search.yahoo.com/mrss/"}
    root = ET.fromstring(get("https://www.youtube.com/feeds/videos.xml?playlist_id=UULF" + cid[2:]))
    out = []
    for e in root.findall("a:entry", ns):
        stats = e.find("m:group/m:community/m:statistics", ns)
        out.append({
            "id": e.findtext("yt:videoId", namespaces=ns),
            "title": e.findtext("a:title", namespaces=ns),
            "published": e.findtext("a:published", namespaces=ns)[:19] + "Z",
            "views": int(stats.get("views")) if stats is not None else None,
        })
    return out, root.findtext("a:author/a:name", namespaces=ns)


def main():
    handles = json.loads(CHANNELS.read_text(encoding="utf-8"))
    prev = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"channels": [], "videos": []}
    prev_ch = {c["handle"]: c for c in prev["channels"]}
    store = {v["id"]: v for v in prev["videos"]}
    channels = []
    failures = []

    for handle in handles:
        ch = dict(prev_ch.get(handle, {"handle": handle, "name": handle.lstrip("@"), "subs": 0}))
        try:
            page = scrape_page(handle)
            ch.update({k: page[k] for k in ("id", "name", "subs") if page[k]})
        except Exception as e:  # noqa: BLE001 - a broken page should not stop other channels
            page = {"videos": []}
            print(f"[warn] page {handle}: {e}", file=sys.stderr)
        if not ch.get("id"):
            failures.append(handle)
            channels.append(ch)
            continue
        try:
            feed, author = read_feed(ch["id"])
            if author and ch["name"] == handle.lstrip("@"):
                ch["name"] = author
        except Exception as e:  # noqa: BLE001
            feed = []
            print(f"[warn] feed {handle}: {e}", file=sys.stderr)
        if not page["videos"] and not feed:
            failures.append(handle)

        for v in page["videos"]:
            rec = store.setdefault(v["id"], {"id": v["id"], "ch": handle})
            rec["title"] = v["title"]
            if v["dur"]:
                rec["dur"] = v["dur"]
            if not rec.get("exact"):
                rec["views"] = v["views"]
            if not rec.get("published") and v["ago"] is not None:
                rec["published"] = iso(NOW - timedelta(days=v["ago"]))
                rec["approx"] = True
        for v in feed:
            rec = store.setdefault(v["id"], {"id": v["id"], "ch": handle})
            rec.update(title=v["title"], published=v["published"], exact=True)
            rec.pop("approx", None)
            if v["views"] is not None:
                rec["views"] = v["views"]
        channels.append(ch)
        print(f"{handle}: page {len(page['videos'])}, feed {len(feed)}")
        time.sleep(1)

    # keep only listed channels, recent videos
    cutoff = iso(NOW - timedelta(days=KEEP_DAYS))
    videos = []
    for ch in channels:
        mine = sorted((v for v in store.values() if v["ch"] == ch["handle"] and v.get("published", "") >= cutoff),
                      key=lambda v: v["published"], reverse=True)[:KEEP_PER_CHANNEL]
        min_age = iso(NOW - timedelta(days=BASE_MIN_AGE))
        base = [v["views"] for v in mine if v["published"] <= min_age][:BASE_COUNT] or [v["views"] for v in mine]
        ch["median"] = int(statistics.median(base)) if base else 0
        for v in mine:
            v["score"] = round(v["views"] / ch["median"], 2) if ch["median"] else 0
        videos += mine

    OUT.write_text(json.dumps({"updated": iso(NOW), "channels": channels, "videos": videos},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {len(channels)} channels, {len(videos)} videos")
    if failures:
        print("[warn] no data for: " + ", ".join(failures), file=sys.stderr)
    if len(failures) == len(handles) and handles:
        sys.exit(1)


if __name__ == "__main__":
    main()
