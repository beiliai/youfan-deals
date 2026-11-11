"""Youfan's public product catalogue and operator promotion client."""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ORIGIN = "https://youfan.beiliai.com/api"
PLATFORMS = {"taobao": "tao", "pdd": "pdd"}
CHINA = timezone(timedelta(hours=8))
TAOBAO_ITEM_HOSTS = {"item.taobao.com", "h5.m.taobao.com", "detail.tmall.com", "detail.tmall.hk"}
TAOBAO_SHARE_HOSTS = {"m.tb.cn", "e.tb.cn", "s.click.taobao.com", "uland.taobao.com"}
PDD_ITEM_HOSTS = {"mobile.yangkeduo.com", "mobile.pinduoduo.com", "yangkeduo.com", "pinduoduo.com"}
PDD_SHARE_HOSTS = {"p.pinduoduo.com", "p.pdd.cn"}
ITEM_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")


class DealError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(path, **params):
    request = Request(f"{ORIGIN}{path}?{urlencode(params)}",
                      headers={"Accept": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            body = json.loads(response.read(4_000_000))
    except HTTPError as exc:
        raise DealError("这件商品暂时查不到，请稍后再试。") from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise DealError("暂时连不上购物平台，请稍后再试。") from exc
    if not isinstance(body, dict) or not (
        body.get("code") == 0 or body.get("success") is True
    ):
        raise DealError("这次没查到商品，请检查链接或换个关键词。")
    data = body.get("data")
    if not isinstance(data, dict):
        raise DealError("这次没查到可用的商品信息。")
    return data


def money(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def amount_text(value):
    amount = money(value)
    return format(amount, "f") if amount is not None else None


def coupon_status(amount, start, end, now):
    if not amount:
        return "not_reported"
    try:
        start_at = datetime.fromisoformat(str(start))
        end_at = datetime.fromisoformat(str(end))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=CHINA)
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=CHINA)
    except ValueError:
        return "unverified"
    if end_at < now:
        return "expired"
    if start_at > now:
        return "not_started"
    return "reported_active"


def product(platform, data, now):
    if platform == "taobao":
        original, price = data.get("originalPrice"), data.get("actualPrice")
        amount = data.get("couponPrice")
        threshold = data.get("couponConditions")
        start, end = data.get("couponStartTime"), data.get("couponEndTime")
    else:
        original, price = data.get("startPrice"), data.get("endPrice")
        amount, threshold = data.get("coupon"), data.get("couponLimit")
        start, end = data.get("couponStart"), data.get("couponEnd")
    return {
        "platform": platform,
        "item_id": str(data.get("goodsId") or data.get("goodsSign") or ""),
        "title": data.get("title") or data.get("dtitle"),
        "image_url": https_link(data.get("mainPic") or data.get("img") or data.get("imgs")),
        "shop_name": data.get("shopName") or "",
        "original_price_cny": amount_text(original),
        "platform_price_cny": amount_text(price),
        "coupon": {
            "status": coupon_status(money(amount), start, end, now),
            "amount_cny": amount_text(amount) if money(amount) else None,
            "minimum_order_cny": amount_text(threshold) if money(amount) else None,
            "starts_at": start,
            "ends_at": end,
        },
        "checked_at": now.isoformat(timespec="seconds"),
    }


def item_path(platform, item_id, action):
    if platform not in PLATFORMS:
        raise DealError("请选择淘宝或拼多多。")
    if not ITEM_ID.fullmatch(item_id):
        raise DealError("没找到这件商品，请重新发商品链接或商品名。")
    return f"/{PLATFORMS[platform]}/goods/{action}"


def customer_price_summary(item):
    price = item["platform_price_cny"]
    price_text = f"现在显示 {Decimal(price).normalize():f} 元。" if price is not None else "这次没查到价格。"
    coupon = item["coupon"]
    if coupon["status"] != "reported_active":
        return price_text + "这次没查到可确认的优惠券。"
    amount = Decimal(coupon["amount_cny"]).normalize()
    threshold = coupon["minimum_order_cny"]
    if threshold is None:
        coupon_text = f"查到一张 {amount:f} 元券。"
    elif Decimal(threshold) == 0:
        coupon_text = f"查到一张 {amount:f} 元券，无门槛。"
    else:
        coupon_text = f"查到一张满 {Decimal(threshold).normalize():f} 元可用的 {amount:f} 元券。"
    ends = datetime.fromisoformat(str(coupon["ends_at"]))
    if ends.tzinfo is not None:
        ends = ends.astimezone(CHINA)
    expires = f"有效期到 {ends.year} 年 {ends.month} 月 {ends.day} 日 {ends:%H:%M}。"
    return price_text + coupon_text + expires + "能不能领、最后付多少，打开商品页就能看。"


def get_deal(platform: str, item_id: str):
    """Recheck current provider price and coupon metadata."""
    path = item_path(platform, item_id, "detail")
    result = product(platform, fetch(path, goodsId=item_id), datetime.now(CHINA))
    if not result["title"]:
        raise DealError("这次没查到这件商品的详情。")
    result["item_id"] = item_id
    result["price_summary_zh"] = customer_price_summary(result)
    return result


def search_deals(keyword: str, platform: str = "all", max_price: str | None = None,
                 only_coupon: bool = False, limit: int = 5, pages: int = 2):
    """Search up to three pages per platform and verify the selected products."""
    keyword = keyword.strip()
    budget = money(max_price)
    if not keyword or len(keyword) > 80 or not 1 <= limit <= 10 or not 1 <= pages <= 3:
        raise DealError("请用简短的商品名搜索，每次最多返回 10 件商品。")
    if max_price is not None and budget is None:
        raise DealError("请提供有效的预算，例如 30 元以内。")
    if platform != "all" and platform not in PLATFORMS:
        raise DealError("请选择淘宝、拼多多，或一起查。")
    items, errors, scanned = [], {}, {}
    for name in PLATFORMS if platform == "all" else (platform,):
        seen, candidates = set(), []
        list_id = None
        scanned[name] = 0
        for page in range(1, pages + 1):
            try:
                if name == "taobao":
                    params = {"keyWords": keyword, "pageId": page, "pageSize": 20}
                    if only_coupon:
                        params["hasCoupon"] = 1
                    data = fetch("/tao/goods/search", **params)
                else:
                    params = {"keyword": keyword, "page": page, "pageSize": 20}
                    if only_coupon:
                        params["withCoupon"] = 1
                    if list_id:
                        params["listId"] = list_id
                    data = fetch("/pdd/goods/list", **params)
                    list_id = data.get("listId") or list_id
                rows = data.get("list")
                if not isinstance(rows, list):
                    raise DealError("这次没查到可用的商品信息。")
            except DealError as exc:
                errors[name] = str(exc)
                break
            scanned[name] += 1
            if not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item = product(name, row, datetime.now(CHINA))
                item_id = item["item_id"]
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    candidates.append(item)
        candidates.sort(key=lambda item: (
            only_coupon and item["coupon"]["status"] != "reported_active",
            money(item["platform_price_cny"]) if item["platform_price_cny"] is not None else Decimal("Infinity"),
        ))
        def verify(candidate):
            try:
                return get_deal(name, candidate["item_id"]), None
            except DealError as exc:
                return None, str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            checked = list(pool.map(verify, candidates[:max(8, limit * 2)]))
        verified_items = []
        for item, error in checked:
            if error:
                errors[name] = error
                continue
            if only_coupon and item["coupon"]["status"] != "reported_active":
                continue
            price = money(item["platform_price_cny"])
            if budget is not None and (price is None or price > budget):
                continue
            verified_items.append(item)
        verified_items.sort(key=lambda item: money(item["platform_price_cny"])
                            if money(item["platform_price_cny"]) is not None else Decimal("Infinity"))
        items.extend(verified_items[:limit])
    items.sort(key=lambda item: money(item["platform_price_cny"])
               if item["platform_price_cny"] is not None else Decimal("Infinity"))
    return {"keyword": keyword, "items": items[:limit], "errors": errors, "pages_checked": scanned,
            "scope_note": "仅比较本次查到并核对过的商品，不代表全网最低价。"}


def share_link(text):
    match = re.search(r"https?://[^\s<>“”‘’【】（）。，；！]+", text, re.I)
    if not match:
        raise DealError("请发一条淘宝或拼多多的商品链接。")
    url = match.group().rstrip("),.;]}")
    try:
        parts = urlsplit(url)
        if parts.username or parts.password or parts.port not in (None, 443, 80) or len(url) > 2000:
            raise ValueError
    except ValueError as exc:
        raise DealError("这条链接看起来不完整，请从商品页重新分享。") from exc
    return url, parts


def follow_store_link(url, hosts):
    """Follow only redirects within the supported storefront hosts."""
    opener = build_opener(NoRedirect())
    for _ in range(4):
        _, parts = share_link(url)
        if parts.hostname not in hosts or parts.scheme != "https" or parts.port not in (None, 443):
            break
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(request, timeout=5):
                return url
        except HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code not in (301, 302, 303, 307, 308) or not location:
                break
            url = urljoin(url, location)
        except (OSError, ValueError):
            break
    return None


def link_item_id(parts, keys, pattern=ITEM_ID):
    query = parse_qs(parts.query)
    for key in keys:
        values = query.get(key, [])
        if len(values) == 1 and pattern.fullmatch(values[0]):
            return values[0]
    return None


def lookup_link(text: str):
    """Check the exact product and coupon from a Taobao or Pinduoduo share link."""
    url, parts = share_link(text)
    host = parts.hostname
    item_id = None
    if host in TAOBAO_ITEM_HOSTS | TAOBAO_SHARE_HOSTS:
        platform = "taobao"
        if host in TAOBAO_SHARE_HOSTS and host != "uland.taobao.com":
            expanded = follow_store_link(url, TAOBAO_ITEM_HOSTS | TAOBAO_SHARE_HOSTS)
            if expanded:
                parts = urlsplit(expanded)
        if parts.hostname in TAOBAO_ITEM_HOSTS and parts.path.lower().endswith(("/item.htm", "/detail.htm", "/item.html")):
            item_id = link_item_id(parts, ("id", "itemId", "item_id"))
        if not item_id:
            try:
                parsed = fetch("/tao/goods/parse", content=url)
            except DealError as exc:
                raise DealError("这条淘宝分享链接暂时识别不了，请发商品详情页的链接。") from exc
            item_id = str(parsed.get("goodsId") or parsed.get("itemId") or "")
    elif host in PDD_ITEM_HOSTS | PDD_SHARE_HOSTS:
        platform = "pdd"
        if host in PDD_ITEM_HOSTS and parts.path.lower().endswith(("/goods.html", "/goods.htm", "/duo_cms_mall.html")):
            item_id = link_item_id(parts, ("goods_sign", "goodsSign"))
        if not item_id:
            rows = fetch("/pdd/goods/list", keyword=url, page=1, pageSize=10).get("list")
            matches = {str(row.get("goodsSign") or row.get("goodsId") or "")
                       for row in (rows or []) if isinstance(row, dict)}
            matches.discard("")
            if len(matches) != 1:
                raise DealError("这条拼多多链接没能确定是哪件商品，请从商品详情页重新分享。")
            item_id = matches.pop()
    else:
        raise DealError("请发淘宝、天猫或拼多多的商品链接。")
    if not item_id:
        raise DealError("没识别到商品，请从商品详情页重新分享。")
    item = get_deal(platform, item_id)
    item["product_url"] = url
    return item


def https_link(value):
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        url = urlsplit(value)
        if url.scheme == "https" and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return None


def get_promotion(platform: str, item_id: str):
    """Get an operator promotion link or Taobao password for a catalogue item."""
    action = "catalogue-word" if platform == "taobao" else "word"
    data = fetch(item_path(platform, item_id, action), goodsId=item_id)
    operator_pid = None
    if platform == "taobao":
        if data.get("attributionMode") != "OPERATOR_CATALOGUE":
            raise DealError("尚未确认推广入口归属，请刷新后再试。")
        link = https_link(data.get("shortUrl")) or https_link(data.get("cpsLongUrl"))
        coupon_link = https_link(data.get("couponLink"))
        password = data.get("longTpwd") or data.get("tpwd") or data.get("shortTpwd")
    else:
        long_link = https_link(data.get("longLink"))
        operator_pid = parse_qs(urlsplit(long_link or "").query).get("pid", [None])[0]
        if operator_pid != "11268925_319040018":
            raise DealError("尚未确认推广位，请刷新后再试。")
        link = https_link(data.get("link")) or https_link(data.get("longLink"))
        coupon_link, password = None, None
    hosts = (TAOBAO_ITEM_HOSTS | TAOBAO_SHARE_HOSTS) if platform == "taobao" else (PDD_ITEM_HOSTS | PDD_SHARE_HOSTS)
    for target in (link, coupon_link):
        if target and urlsplit(target).hostname not in hosts:
            raise DealError("推广入口不在对应购物平台，请重新查询。")
    if not any((link, coupon_link, password)):
        raise DealError("暂时没拿到这件商品的推广链接。")
    return {"platform": platform, "item_id": item_id, "promotion_url": link,
            "coupon_url": coupon_link, "taobao_password": password,
            "attribution_mode": "operator", "automatic_rebate_supported": False,
            "operator_pid": operator_pid, "promotion_generated": bool(link or coupon_link),
            "commission_guaranteed": False,
            "checked_at": datetime.now(CHINA).isoformat(timespec="seconds")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search")
    search.add_argument("keyword")
    search.add_argument("--platform", choices=["all", *PLATFORMS], default="all")
    search.add_argument("--max-price")
    search.add_argument("--only-coupon", action="store_true")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--pages", type=int, default=2)
    commands.add_parser("link").add_argument("text")
    for command in ("detail", "promotion"):
        args = commands.add_parser(command)
        args.add_argument("platform", choices=PLATFORMS)
        args.add_argument("item_id")
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        result = {"search": search_deals, "detail": get_deal,
                  "promotion": get_promotion, "link": lookup_link}[command](**args)
    except DealError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
