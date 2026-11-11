"""Save shopping results, refine a selection, and compare confirmed quantities."""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cards import get_shopping_cards, prepare_card, render_cards
from deals import CHINA, DealError, get_deal, money


def refine(result, max_price=None, exclude=(), platform="all", only_coupon=False):
    budget = money(max_price)
    if max_price is not None and budget is None:
        raise DealError("请提供有效的预算。")
    if platform not in ("all", "taobao", "pdd"):
        raise DealError("请选择淘宝或拼多多。")
    excluded = [word.strip().casefold() for word in exclude if word.strip()]
    kept = []
    for item in result["items"]:
        price = money(item.get("platform_price_cny"))
        text = (str(item.get("title", "")) + " " + str(item.get("shop_name", ""))).casefold()
        if platform != "all" and item["platform"] != platform:
            continue
        if budget is not None and (price is None or price > budget):
            continue
        if any(word in text for word in excluded):
            continue
        if only_coupon and item["coupon"]["status"] != "reported_active":
            continue
        kept.append(prepare_card(item) if item.get("promotion_generated") is not True else item)
    kept.sort(key=lambda x: money(x.get("platform_price_cny"))
              if money(x.get("platform_price_cny")) is not None else Decimal("Infinity"))
    return {**result, "items": kept, "selection_note": "按这一批已查到的商品筛选，未把优惠券再减一次。"}


def refresh(result):
    """Refresh existing identities; failures are visible and never replaced."""
    errors = dict(result.get("errors", {}))
    def one(old):
        try:
            item = get_deal(old["platform"], old["item_id"])
            if old.get("product_url"):
                item["product_url"] = old["product_url"]
            return prepare_card(item), None
        except DealError as exc:
            return None, (f'{old["platform"]}:{old["item_id"]}', str(exc))
    with ThreadPoolExecutor(max_workers=4) as pool:
        checked = list(pool.map(one, result["items"]))
    for _, error in checked:
        if error:
            errors[error[0]] = error[1]
    return {**result, "items": [item for item, _ in checked if item],
            "errors": errors, "checked_at": datetime.now(CHINA).isoformat(timespec="seconds")}


UNITS = {"g": ("weight", Decimal(1), "每100克", Decimal(100)),
         "kg": ("weight", Decimal(1000), "每100克", Decimal(100)),
         "ml": ("volume", Decimal(1), "每100毫升", Decimal(100)),
         "l": ("volume", Decimal(1000), "每100毫升", Decimal(100)),
         "件": ("count", Decimal(1), "每件", Decimal(1)),
         "个": ("count", Decimal(1), "每件", Decimal(1))}


def compare(result, selections):
    """Never infer a priced SKU or quantity from a generic product title."""
    checked = datetime.fromisoformat(result["checked_at"])
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=CHINA)
    if (datetime.now(CHINA) - checked).total_seconds() > 1800:
        raise DealError("这批价格需要重新核对，请刷新后再确认规格对应的价格。")
    if not isinstance(selections, list) or not 2 <= len(selections) <= 8:
        raise DealError("请选两到八件商品比较。")
    keys = [(x.get("platform"), x.get("item_id")) for x in selections]
    if len(set(keys)) != len(keys):
        raise DealError("同一件商品请只选择一次。")
    index = {(x["platform"], x["item_id"]): x for x in result["items"]}
    rows, missing, dimensions = [], [], set()
    for selection in selections:
        key = (selection.get("platform"), selection.get("item_id"))
        item = index.get(key)
        quantity = money(selection.get("quantity"))
        unit = UNITS.get(str(selection.get("unit", "")).lower())
        if (not item or quantity is None or quantity == 0 or not unit
                or not selection.get("variant")
                or selection.get("quantity_source") != "user_confirmed"
                or selection.get("price_matches_variant") is not True
                or money(selection.get("confirmed_price_cny")) != money(item.get("platform_price_cny"))
                or money(item.get("platform_price_cny")) is None):
            missing.append({"platform": key[0], "item_id": key[1],
                            "message": "请确认所选规格、总容量或件数，以及这个规格对应的显示价。"})
            continue
        dimension, factor, label, scale = unit
        price = money(item["platform_price_cny"])
        value = price / (quantity * factor) * scale
        dimensions.add(dimension)
        rows.append({"platform": key[0], "item_id": key[1], "title": item["title"],
                     "variant": selection["variant"], "price_cny": str(price),
                     "unit_label": label, "unit_price_cny": str(value.quantize(Decimal("0.0001"))),
                     "buy_url": item.get("buy_url") if item.get("promotion_generated") is True else None,
                     "_value": value})
    comparable = len(rows) >= 2 and not missing and len(dimensions) == 1
    if comparable:
        rows.sort(key=lambda x: x["_value"])
    for row in rows:
        row.pop("_value")
    return {"comparable": comparable, "items": rows, "missing": missing,
            "note": ("按你确认的规格和当前显示价比较单价，不代表同款或全网最低价，未重复扣券。"
                     if comparable else "规格尚未齐全或计量不同，暂不判断哪款更便宜。")}


def save(result, output):
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = destination.with_suffix(".html")
    html_path.write_text(render_cards(result), encoding="utf-8")
    return {"data_file": str(destination), "cards_file": str(html_path),
            "count": len(result["items"]), "errors": result.get("errors", {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    search = sub.add_parser("search")
    search.add_argument("keyword", nargs="?", default="")
    search.add_argument("--link", action="append", dest="links")
    search.add_argument("--platform", choices=["all", "taobao", "pdd"], default="all")
    search.add_argument("--max-price")
    search.add_argument("--limit", type=int, default=6)
    search.add_argument("--pages", type=int, default=2)
    search.add_argument("--only-coupon", action="store_true")
    search.add_argument("--output", required=True)
    select = sub.add_parser("filter")
    select.add_argument("input")
    select.add_argument("--max-price")
    select.add_argument("--exclude", action="append", default=[])
    select.add_argument("--platform", choices=["all", "taobao", "pdd"], default="all")
    select.add_argument("--only-coupon", action="store_true")
    select.add_argument("--refresh", action="store_true")
    select.add_argument("--output", required=True)
    comparison = sub.add_parser("compare")
    comparison.add_argument("input")
    comparison.add_argument("selections")
    args = vars(parser.parse_args())
    action = args.pop("action")
    try:
        if action == "search":
            output = args.pop("output")
            result = get_shopping_cards(**args)
            answer = save(result, output)
        else:
            source = Path(args.pop("input"))
            result = json.loads(source.read_text(encoding="utf-8"))
            if action == "compare":
                selections = json.loads(Path(args["selections"]).read_text(encoding="utf-8"))
                answer = compare(result, selections)
            else:
                output = args.pop("output")
                do_refresh = args.pop("refresh")
                checked = datetime.fromisoformat(result["checked_at"])
                if checked.tzinfo is None:
                    checked = checked.replace(tzinfo=CHINA)
                if do_refresh or (datetime.now(CHINA) - checked).total_seconds() > 1800:
                    result = refresh(result)
                answer = save(refine(result, **args), output)
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    except (DealError, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
