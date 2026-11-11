"""Build shopping cards from current Youfan products."""
import argparse
import base64
import html
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from deals import (CHINA, DealError, NoRedirect, PDD_ITEM_HOSTS, PDD_SHARE_HOSTS,
                   TAOBAO_ITEM_HOSTS, TAOBAO_SHARE_HOSTS, get_promotion,
                   https_link, lookup_link, money, search_deals)
from datetime import datetime

LINK_HOSTS = TAOBAO_ITEM_HOSTS | TAOBAO_SHARE_HOSTS | PDD_ITEM_HOSTS | PDD_SHARE_HOSTS


def check_link(url):
    """Check storefront responses without sending requests outside supported hosts."""
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.hostname not in LINK_HOSTS or parsed.scheme != "https":
            return "unconfirmed"
        try:
            with build_opener(NoRedirect()).open(
                    Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=4) as response:
                text = response.read(90000).decode("utf-8", errors="ignore")
                if any(word in text for word in ("该宝贝不存在", "商品链接已失效", "商品已下架", "抱歉，您查看的宝贝不存在")):
                    return "expired"
                return "responded"
        except HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code in (301, 302, 303, 307, 308) and location:
                from urllib.parse import urljoin
                url = urljoin(url, location)
                continue
            return "expired" if exc.code in (404, 410) else "unconfirmed"
        except (OSError, ValueError):
            return "unconfirmed"
    return "unconfirmed"


def prepare_card(item):
    # An arbitrary source URL may belong to a different publisher or have no tracking.
    # It must never become the picture, purchase button or password fallback.
    promotion, url, status = {}, None, "unconfirmed"
    for _ in range(2):
        try:
            promotion = get_promotion(item["platform"], item["item_id"])
            candidate = promotion.get("coupon_url") or promotion.get("promotion_url")
            status = check_link(candidate) if candidate else "unconfirmed"
            if candidate and status != "expired":
                url = candidate
                break
        except DealError:
            break
    generated = bool(url)
    label = ("领券看看" if generated and promotion.get("coupon_url")
             and item["coupon"]["status"] == "reported_active" else "去看看")
    warning = "" if generated else "暂未生成推广入口，请刷新后再试。"
    coupon = item["coupon"]
    if coupon["status"] == "reported_active":
        amount = money(coupon["amount_cny"])
        badge = f"{amount.normalize():f} 元券"
        minimum = money(coupon["minimum_order_cny"])
        condition = ("门槛以商品页为准" if minimum is None else
                     "无门槛" if minimum == 0 else f"满 {minimum.normalize():f} 元可用")
        ends = datetime.fromisoformat(str(coupon["ends_at"]))
        if ends.tzinfo:
            ends = ends.astimezone(CHINA)
        expires = f"{ends.year}.{ends.month:02d}.{ends.day:02d} {ends:%H:%M} 到期"
    else:
        badge, condition, expires = "暂未查到可确认的券", "", ""
    return {**item, "buy_url": url, "button_label": label, "coupon_badge": badge,
            "coupon_condition": condition, "coupon_expires": expires,
            "link_check": status, "warning": warning,
            "promotion_generated": generated, "commission_guaranteed": False,
            "attribution_mode": "operator" if generated else "unverified",
            "operator_pid": promotion.get("operator_pid") if generated else None,
            "taobao_password": promotion.get("taobao_password") if generated else None}


def get_shopping_cards(keyword: str = "", platform: str = "all",
                       links: list[str] | None = None, max_price: str | None = None,
                       only_coupon: bool = False, limit: int = 4, pages: int = 2):
    """Fetch checked products with images, coupons and current purchase links."""
    links = links or []
    if len(links) > 8 or (not links and not keyword.strip()):
        raise DealError("请发商品链接，或告诉我想买什么。")
    if links:
        items, errors = [], {}
        for number, link in enumerate(links, 1):
            try:
                items.append(lookup_link(link))
            except DealError as exc:
                errors[str(number)] = str(exc)
        scanned = {}
    else:
        result = search_deals(keyword, platform, max_price, only_coupon, limit, pages)
        items, errors, scanned = result["items"], result["errors"], result["pages_checked"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        cards = list(pool.map(prepare_card, items))
    return {"items": cards, "errors": errors, "pages_checked": scanned,
            "checked_at": datetime.now(CHINA).isoformat(timespec="seconds")}


def render_cards(result):
    escape = html.escape
    cards = []
    for item in result["items"]:
        brand = "淘宝" if item["platform"] == "taobao" else "拼多多"
        price = money(item.get("platform_price_cny"))
        price_text = f"{price.normalize():f}" if price is not None else "待确认"
        image = https_link(item.get("image_url"))
        photo = (f'<img src="{escape(image, quote=True)}" alt="{escape(item["title"], quote=True)}" '
                 'referrerpolicy="no-referrer" loading="lazy">' if image else
                 '<div class="no-photo">暂无商品图</div>')
        # Older saved sessions must not re-enable the original-link fallback.
        url = https_link(item.get("buy_url")) if item.get("promotion_generated") is True else None
        title = escape(item["title"])
        picture = f'<a class="photo" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{photo}</a>' if url else f'<div class="photo">{photo}</div>'
        button = (f'<a class="buy" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escape(item["button_label"])}</a>'
                  if url else '<span class="buy disabled">推广入口暂不可用</span>')
        warning = f'<p class="notice">{escape(item.get("warning") or "暂未生成推广入口，请刷新后再试。")}</p>' if not url else ""
        active = item["coupon"]["status"] == "reported_active"
        coupon = (f'<div class="coupon"><strong>{escape(item["coupon_badge"])}</strong><span>{escape(item["coupon_condition"])}</span></div>'
                  if active else f'<div class="no-coupon">{escape(item["coupon_badge"])}</div>')
        original = money(item.get("original_price_cny"))
        old = f'<del>¥{original.normalize():f}</del>' if original is not None and price is not None and original > price else ""
        cards.append(f'''<article class="card">
{picture}<div class="body"><span class="platform">{brand}</span><h2>{title}</h2>
<div class="price"><span>¥</span>{price_text}{old}</div><div class="price-label">现在显示的价格</div>
{coupon}<div class="expires">{escape(item["coupon_expires"])}</div>{button}{warning}
</div></article>''')
    logo = Path(__file__).resolve().parent / "logo.png"
    logo_html = ""
    if logo.exists():
        encoded = base64.b64encode(logo.read_bytes()).decode("ascii")
        logo_html = f'<img class="logo" src="data:image/png;base64,{encoded}" alt="优返查券">'
    empty = '<p class="empty">这次没查到合适的商品，换个关键词试试。</p>' if not cards else ""
    errors = "".join(f'<p class="notice">{escape(str(message))}</p>' for message in result["errors"].values())
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>优返查券 · 看看今天的优惠</title><style>
*{box-sizing:border-box}body{margin:0;background:#fff7f2;color:#28211f;font-family:"PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1100px;margin:auto;padding:28px 20px 42px}header{display:flex;align-items:center;gap:13px;margin-bottom:26px}
.logo{width:52px;height:52px;border-radius:15px}h1{font-size:25px;margin:0 0 5px}header p{margin:0;color:#8a7066;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:20px;align-items:start}
.card{overflow:hidden;border-radius:20px;background:#fff;border:1px solid #f2e7df;box-shadow:0 6px 22px #9a4a1a09}
.photo{display:block;aspect-ratio:1;background:#f6f2ef;overflow:hidden}.photo img{width:100%;height:100%;object-fit:contain}
.no-photo{height:100%;display:grid;place-items:center;color:#9b8981}.body{padding:16px}.platform{font-size:11px;color:#ed3b19;background:#fff1e9;border-radius:5px;padding:3px 6px}
h2{font-size:15px;font-weight:550;line-height:1.6;margin:9px 0 13px;min-height:48px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.price{font-size:32px;line-height:1.1;color:#ee3920;font-weight:750;letter-spacing:-1px}.price>span{font-size:18px;margin-right:3px}.price del{font-size:12px;color:#aa9992;font-weight:400;letter-spacing:0;margin-left:10px}
.price-label{color:#9c8378;font-size:11px;margin-top:5px}.coupon{margin-top:14px;display:flex;align-items:center;gap:10px;background:#fff0e9;border:1px dashed #ff9878;color:#df341c;border-radius:7px;padding:10px}
.coupon strong{font-size:18px}.coupon span{font-size:12px;border-left:1px dashed #ff9878;padding-left:10px}.no-coupon{margin-top:14px;padding:10px 0;color:#97847b;font-size:12px}
.expires{font-size:11px;color:#a08a80;min-height:27px;padding-top:7px}.buy{display:block;text-align:center;background:linear-gradient(100deg,#ff6d20,#f23923);border-radius:10px;padding:12px;color:white;text-decoration:none;font-weight:600;font-size:14px}
.buy:hover{filter:brightness(1.06)}.buy.disabled{background:#e6dcd7;color:#8f817a}.note{font-size:12px;line-height:1.8;color:#a08779;margin-top:24px}.notice{color:#8f6252;font-size:13px}
@media(max-width:550px){main{padding:18px 12px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.body{padding:11px}h2{font-size:13px;min-height:42px}.price{font-size:26px}.coupon{gap:5px;padding:8px 6px}.coupon strong{font-size:15px}.coupon span{font-size:10px;padding-left:5px}.logo{width:44px;height:44px}h1{font-size:22px}}
</style></head><body><main><header>''' + logo_html + '''<div><h1>优返查券</h1><p>想买的好物，先看看有没有优惠</p></div></header><section class="grid">''' + "".join(cards) + empty + '''</section>''' + errors + '''<p class="note">能不能领券、最后付多少，打开商品页就能看。商品价格和优惠会变化，点击按钮前往平台。这是推广链接，购买后优返可能获得佣金，不代表用户返利承诺。</p></main></body></html>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyword", nargs="?", default="")
    parser.add_argument("--platform", choices=["all", "taobao", "pdd"], default="all")
    parser.add_argument("--link", action="append", dest="links")
    parser.add_argument("--max-price")
    parser.add_argument("--only-coupon", action="store_true")
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--output", required=True)
    args = vars(parser.parse_args())
    destination = Path(args.pop("output")).resolve()
    try:
        result = get_shopping_cards(**args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_cards(result), encoding="utf-8")
    except (DealError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"file": str(destination), "count": len(result["items"]),
                      "errors": result["errors"], "pages_checked": result["pages_checked"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())


