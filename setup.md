# 商品卡片

WorkBuddy 默认生成可预览的 HTML 商品卡片，并把生成文件展示在结果区。商品图片和信息由查询结果提供。默认只查价格与优惠；只有用户明确要领券或购买入口时才生成推广链接。把已经生成的页面作为文件或网页预览交给用户，无需再次改写里面的商品价格和券。

- 按商品找卡片：`python cards.py "抽纸" --platform pdd --max-price 30 --limit 3 --pages 2 --output outputs/贝礼好物.html`
- 同时查两平台：分别用 `--platform taobao` 和 `--platform pdd` 查询，或 `--platform all --limit 6` 一起查。
- 粘贴链接找卡片：`python cards.py --link "淘宝商品链接" --link "拼多多商品链接" --output outputs/贝礼好物.html`
- 有原生 MCP 时调用 `get_shopping_cards` 默认取得图片、价格和券信息；用户明确要外部入口时才设置 `include_links=true`。能展示网页时生成同样的卡片，无法展示时用带图片和可点击标题的简短排版。

卡片生成时会核对商品详情，检查平台入口是否响应。明确失效时重新取同一件商品的推广链接。仅成功取得运营推广入口时，才保留商品图和购买按钮的跳转。要求登录不等于佣金已归属；取链失败或入口明确失效时禁用购买跳转，显示「推广入口暂不可用」，不回退原商品链接。用户给的是某件商品时，不换成另一件商品。

# 查询说明

无法展示卡片时，每件查到的商品直接用 `price_summary_zh` 作为价格和券的说明，只在前面加商品名和平台，在后面加商品链接。每件商品只展示一次。最后一个链接后结束回复，不加补充说明、备选或价格推算。用户只想查券时，无需另生成推广链接。用户要求可分享的购买入口时，调用推广工具；失败时不提供原商品链接作为购买兜底，提示刷新后再试。

| 场景 | MCP | CLI |
| --- | --- | --- |
| 粘贴商品链接 | `lookup_link(text)` | `python deals.py link "分享链接或分享文案"` |
| 按商品名或预算找 | `search_deals` | `python deals.py search "抽纸" --platform pdd --max-price 30 --only-coupon --limit 3 --pages 2` |
| 核对候选详情 | `get_deal` | `python deals.py detail pdd <item_id>` |
| 按需生成推广入口 | `get_promotion` | `python deals.py promotion pdd <item_id>` |

WorkBuddy 从技能目录执行 CLI。若无法执行脚本或连接贝礼，直接说「这次暂时查不到，请稍后再试」，不要生成示例商品冒充实查结果。

查询默认每个平台两页，可设置一到三页。已选商品用详情结果核对。只将 `reported_active` 当作平台报告当前有效的券。将其他未确认情况改写成自然中文，不向用户输出状态枚举、字段名、命令、日志或商品编号。仅把明确返回的 `coupon_url` 作为领券入口。链接查券返回的 `product_url` 是用户给出的原链接。

CLI 需要 Python 3.10+，访问贝礼公开接口。MCP 使用本机 stdio，启动命令为 `uv run --script <技能目录>/mcp_server.py`。WorkBuddy 个人技能库可上传 ZIP。

推广入口生成成功只证明取得了后台运营推广链接，不证明成交、佣金到账或个人返利。原始商品链接和用户发来的其他推广链接都不能代替后台成功生成的入口。原商品链接仅用于识别商品。旧结果缺少 promotion_generated 标记时须重新取链后再展示按钮。

用户明确索要购买或领券入口后，才在 cards.py 命令中加 `--with-links`；先说明推广关系和访问位置，不自动打开或复制口令。默认无购买跳转不代表接口失败。
