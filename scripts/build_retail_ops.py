# ruff: noqa: E501  （DDL 行尾注释必须写在一行，add_database.py 按行提取取值说明）
"""用 UCI Online Retail II 真实交易数据构建问数库 retail_ops（指标归因用）。

数据：Chen, D. (2019). Online Retail II [Dataset]. UCI Machine Learning Repository.
https://doi.org/10.24432/C5CG6D ，许可 CC BY 4.0。一家英国注册的线上礼品零售商
2009-12 至 2011-12 的全部交易（约 107 万行、40 多个国家）。原始文件不进仓库，由本脚本下载。

清洗口径与"跨境电商经营分析看板"项目一致（同一作者，
https://github.com/kennywu411-dev/ecommerce-dashboard 的 scripts/build_data.py 与 docs/指标定义.md）：
去完全重复行 → 只留商品行（编码以 5 位数字开头）→ 单价 > 0 → 去掉非退货单里的负数量
→ 只留 24 个完整自然月（2009-12-01 至 2011-11-30）。发票号以 C 开头的是退货单。
品类按商品名关键词归类（先匹配先得），客户编号重新编号。

本脚本需要 pandas（项目依赖里没有，临时带上即可）：
  uv run --with pandas --with openpyxl python scripts/build_retail_ops.py --download
  uv run python scripts/add_database.py retail_ops data/source/retail_ops.sqlite
已有原始文件时用 --raw 指定（.xlsx 或 .parquet，后者另需 --with pyarrow）。
构建结束会打印近 12 个月的 GMV、退货额、订单数，应与看板 site/findings.json 的 kpi_p2 一致。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
RAW_XLSX = RAW_DIR / "online_retail_II.xlsx"
OUT = ROOT / "data" / "source" / "retail_ops.sqlite"
UCI_URL = "https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip"
START, END = "2009-12-01", "2011-12-01"  # 左闭右开：24 个完整自然月
LARGE, MEDIUM = 5000.0, 1000.0  # 单据金额分档（英镑），见 DDL 注释

COUNTRY_ZH = {
    "United Kingdom": "英国", "EIRE": "爱尔兰", "Netherlands": "荷兰", "Germany": "德国",
    "France": "法国", "Australia": "澳大利亚", "Spain": "西班牙", "Switzerland": "瑞士",
    "Sweden": "瑞典", "Denmark": "丹麦", "Belgium": "比利时", "Norway": "挪威",
    "Portugal": "葡萄牙", "Japan": "日本", "Channel Islands": "海峡群岛", "Italy": "意大利",
    "Finland": "芬兰", "Cyprus": "塞浦路斯", "Austria": "奥地利", "Greece": "希腊",
    "Singapore": "新加坡", "Hong Kong": "中国香港", "Israel": "以色列", "Poland": "波兰",
    "United Arab Emirates": "阿联酋", "Unspecified": "未注明", "Canada": "加拿大",
    "Iceland": "冰岛", "USA": "美国", "Malta": "马耳他", "Lithuania": "立陶宛",
    "Bahrain": "巴林", "RSA": "南非", "Thailand": "泰国", "Korea": "韩国",
    "Czech Republic": "捷克", "Brazil": "巴西", "Lebanon": "黎巴嫩",
    "European Community": "欧洲共同体", "Saudi Arabia": "沙特阿拉伯", "Bermuda": "百慕大",
    "Nigeria": "尼日利亚", "West Indies": "西印度群岛",
}  # fmt: skip

# 品类：原数据没有品类字段，按商品名关键词归类，先匹配先得（与看板项目的规则逐条相同）
CATEGORY_RULES = [
    ("节日派对", r"CHRISTMAS|XMAS|ADVENT|SANTA|REINDEER|EASTER|BUNTING|PARTY|BIRTHDAY|CRACKER|HALLOWEEN"),
    ("灯饰蜡烛", r"LIGHT|LANTERN|CANDLE|LAMP|HLDR|INCENSE"),
    ("家居装饰", r"HOT WATER BOTTLE"),
    ("包袋收纳", r"\bBAGS?\b|SHOPPER|STORAGE|\bBOX(ES)?\b|\bTINS?\b|BASKET|\bJARS?\b|\bCASES?\b|HAMPER|DRAWER|CABINET|CRATE"),
    ("厨房餐饮", r"CAKE|\bMUGS?\b|\bCUPS?\b|PLATE|BOWL|\bTEA\b|TEACUP|CUTLERY|BAKING|LUNCH|BOTTLE|GLASS|NAPKIN|COASTER|SPOON|\bJUG\b|KITCHEN|PANTRY|RECIPE|TRAY|BEAKER|TUMBLER|APRON|OVEN|CHOPSTICK|JELLY|SUGAR|BISCUIT|STRAWS"),
    ("家居装饰", r"HEART|SIGN|FRAME|CLOCK|DOORMAT|\bMAT\b|CUSHION|ORNAMENT|DECORATION|HANGING|WICKER|MIRROR|VASE|GARDEN|PARASOL|BOARD|BLOCK WORD|PLANT|\bDOOR\b|HOOK|WREATH|CHIME|DOORSTOP|QUILT|THROW|PHOTO|SHELF"),
    ("文具纸品", r"\bCARDS?\b|PAPER|WRAP|NOTEBOOK|\bPENS?\b|PENCIL|STICKER|ENVELOPE|CRAFT|TISSUE|\bTAGS?\b|JOURNAL|RIBBON|NOTE|BOOK|ERASER|RULER|STAMP|CHALK"),
    ("儿童玩具", r"\bTOYS?\b|GAME|DOLL|CHILDREN|CHILDS|\bKIDS?\b|SPACEBOY|PUZZLE|JIGSAW|SKITTLES|DOMINO|TEDDY|FELTCRAFT|PLAYHOUSE|BUBBLES|SOLDIER|DINOSAUR|PAINT|\bBALL\b"),
    ("饰品配饰", r"NECKLACE|BRACELET|EARRING|\bRING\b|JEWEL|\bHAIR\b|CHARM|PURSE|UMBRELLA|SCARF|BROOCH|DIAMANTE|BEADS|WALLET|KEYRING"),
]  # fmt: skip

DDL = """
CREATE TABLE countries (
  country TEXT PRIMARY KEY, -- 国家（英文，原数据写法，如 United Kingdom、EIRE、Germany）
  country_zh TEXT NOT NULL, -- 国家中文名，如 英国、爱尔兰、德国
  market TEXT NOT NULL -- 市场 UK: 英国本土; Overseas: 海外（英国以外的所有国家）
);
CREATE TABLE customers (
  customer_id INTEGER PRIMARY KEY, -- 客户编号（已重新编号，从 10001 起）；没有编号的买家不在这张表里
  country TEXT REFERENCES countries(country), -- 客户所在国家（取第一张单据上的国家）
  first_order_date TEXT, -- 首单日期 YYYY-MM-DD（最早一张销售订单；只有退货单的客户为 NULL）
  value_tier TEXT NOT NULL -- 客户价值分层，按两年累计 GMV 排名 A: 前 10% 的大客户; B: 其后 40%; C: 其余
);
CREATE TABLE invoices (
  invoice_no TEXT PRIMARY KEY, -- 发票号。一张发票是一单；以 C 开头的是退货单
  invoice_type TEXT NOT NULL, -- 单据类型 sale: 销售订单; return: 退货单（含取消）
  invoice_date TEXT NOT NULL, -- 开票时间 YYYY-MM-DD HH:MM:SS；退货单是退货发生的时间
  customer_id INTEGER REFERENCES customers(customer_id), -- 客户编号；没有编号的买家为 NULL
  country TEXT NOT NULL REFERENCES countries(country), -- 买家国家（英文）
  customer_type TEXT NOT NULL, -- 新老客 new: 新客（单据在该客户首单当月）; returning: 老客; guest: 无客户编号
  size_band TEXT NOT NULL, -- 单据金额分档 large: 大额（≥ 5000 英镑）; medium: 中额（1000–5000）; small: 小额（< 1000）
  amount REAL NOT NULL -- 单据金额，英镑，各商品行金额之和；退货单也记正数
);
CREATE TABLE products (
  stock_code TEXT PRIMARY KEY, -- 商品编码
  description TEXT -- 商品名称（英文，取该编码最常见的写法）
);
CREATE TABLE invoice_lines (
  line_id INTEGER PRIMARY KEY, -- 行号
  invoice_no TEXT NOT NULL REFERENCES invoices(invoice_no), -- 所属发票
  stock_code TEXT NOT NULL REFERENCES products(stock_code), -- 商品编码
  category TEXT NOT NULL, -- 品类（按商品名关键词归类）节日派对、灯饰蜡烛、家居装饰、包袋收纳、厨房餐饮、文具纸品、儿童玩具、饰品配饰、其他
  quantity INTEGER NOT NULL, -- 件数；退货行也记正数
  unit_price REAL NOT NULL, -- 单价，英镑
  amount REAL NOT NULL -- 行金额 = 件数 × 单价，英镑；退货行也记正数
);
CREATE INDEX idx_invoices_date ON invoices(invoice_date);
CREATE INDEX idx_invoices_customer ON invoices(customer_id);
CREATE INDEX idx_lines_invoice ON invoice_lines(invoice_no);
"""


def categorize(desc: str) -> str:
    d = str(desc).upper()
    for name, pat in CATEGORY_RULES:
        if re.search(pat, d):
            return name
    return "其他"


def size_band(amount: float) -> str:
    return "large" if amount >= LARGE else "medium" if amount >= MEDIUM else "small"


def download() -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not RAW_XLSX.exists():
        zip_path = RAW_DIR / "online_retail_II.zip"
        print(f"下载 {UCI_URL}（约 45 MB）")
        urllib.request.urlretrieve(UCI_URL, zip_path)  # noqa: S310
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(RAW_DIR)
    return RAW_XLSX


def load_raw(path: Path):
    import pandas as pd

    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    sheets = pd.read_excel(path, sheet_name=None, dtype={"Invoice": str, "StockCode": str})
    return pd.concat(sheets.values(), ignore_index=True)


def clean(df):
    """与看板项目 build_data.py 相同的五步清洗。"""
    import pandas as pd

    df = df.copy()
    df["Invoice"] = df["Invoice"].astype(str)
    df["StockCode"] = df["StockCode"].astype(str)
    df["amt"] = df["Quantity"] * df["Price"]
    key = ["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"]  # fmt: skip
    df = df[~df.duplicated(subset=key)]
    df = df[df["StockCode"].str.match(r"^\d{5}")]
    df = df[df["Price"] > 0]
    df = df[df["Invoice"].str.startswith("C") | (df["Quantity"] > 0)]
    df = df[(df["InvoiceDate"] >= pd.Timestamp(START)) & (df["InvoiceDate"] < pd.Timestamp(END))]
    return df.copy()


def build(raw: Path, out: Path) -> dict:
    df = clean(load_raw(raw))
    df["is_return"] = df["Invoice"].str.startswith("C")
    df["category"] = df["Description"].map(categorize)
    df["line_amount"] = df["amt"].abs().round(2)
    ids = sorted(df["Customer ID"].dropna().unique())
    renumber = {c: 10001 + i for i, c in enumerate(ids)}  # 不输出原编号

    inv = (
        df.groupby("Invoice", sort=False)
        .agg(
            t=("InvoiceDate", "min"),
            country=("Country", "first"),
            cust=("Customer ID", "first"),
            is_return=("is_return", "first"),
            amount=("amt", "sum"),
        )
        .reset_index()
    )
    inv["amount"] = inv["amount"].abs().round(2)
    inv["customer_id"] = inv["cust"].map(renumber).astype("Int64")
    sales = inv[~inv["is_return"]]
    first = sales.dropna(subset=["customer_id"]).groupby("customer_id")["t"].min()
    total = sales.dropna(subset=["customer_id"]).groupby("customer_id")["amount"].sum()
    rank = total.rank(ascending=False, method="first")
    n = len(total)
    tier = rank.map(lambda r: "A" if r <= n * 0.1 else "B" if r <= n * 0.5 else "C")
    first_country = inv.dropna(subset=["customer_id"]).sort_values("t").groupby("customer_id")["country"].first()  # fmt: skip

    first_month = inv["customer_id"].map(first.dt.to_period("M"))
    inv_month = inv["t"].dt.to_period("M")
    inv["customer_type"] = "returning"
    inv.loc[first_month == inv_month, "customer_type"] = "new"
    inv.loc[inv["customer_id"].isna(), "customer_type"] = "guest"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    conn = sqlite3.connect(out)
    conn.executescript(DDL)
    countries = sorted(inv["country"].unique())
    conn.executemany(
        "INSERT INTO countries VALUES (?, ?, ?)",
        [
            (c, COUNTRY_ZH.get(c, c), "UK" if c == "United Kingdom" else "Overseas")
            for c in countries
        ],  # fmt: skip
    )
    conn.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?)",
        [
            (
                int(c),
                first_country[c],
                first[c].strftime("%Y-%m-%d") if c in first.index else None,
                tier.get(c, "C"),
            )
            for c in sorted(renumber.values())
        ],
    )
    conn.executemany(
        "INSERT INTO invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                r.Invoice,
                "return" if r.is_return else "sale",
                r.t.strftime("%Y-%m-%d %H:%M:%S"),
                None
                if r.customer_id is None or str(r.customer_id) == "<NA>"
                else int(r.customer_id),
                r.country,
                r.customer_type,
                size_band(r.amount),
                float(r.amount),
            )
            for r in inv.itertuples()
        ],  # fmt: skip
    )
    names = df.groupby("StockCode")["Description"].agg(lambda s: s.dropna().mode().iloc[0] if s.notna().any() else None)  # fmt: skip
    conn.executemany("INSERT INTO products VALUES (?, ?)", list(names.items()))
    conn.executemany(
        "INSERT INTO invoice_lines (invoice_no, stock_code, category, quantity, unit_price, amount) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        zip(
            df["Invoice"],
            df["StockCode"],
            df["category"],
            df["Quantity"].abs().astype(int).tolist(),
            df["Price"].astype(float).tolist(),
            df["line_amount"].astype(float).tolist(),
            strict=True,
        ),
    )
    conn.commit()
    # 统计信息让归因的月度历史查询快约 6 倍；它存在 sqlite_stat1 里，列表结构时会被过滤掉
    conn.execute("ANALYZE")
    conn.commit()
    p2 = "invoice_date >= '2010-12-01' AND invoice_date < '2011-12-01'"
    check = {
        "lines": conn.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0],
        "sale_invoices": conn.execute("SELECT COUNT(*) FROM invoices WHERE invoice_type = 'sale'").fetchone()[0],
        "return_invoices": conn.execute("SELECT COUNT(*) FROM invoices WHERE invoice_type = 'return'").fetchone()[0],
        "customers": conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0],
        "gmv_p2": conn.execute(
            "SELECT ROUND(SUM(l.amount), 2) FROM invoice_lines l JOIN invoices i USING (invoice_no) "
            f"WHERE i.invoice_type = 'sale' AND {p2}"
        ).fetchone()[0],
        "returns_p2": conn.execute(
            "SELECT ROUND(SUM(l.amount), 2) FROM invoice_lines l JOIN invoices i USING (invoice_no) "
            f"WHERE i.invoice_type = 'return' AND {p2}"
        ).fetchone()[0],
        "orders_p2": conn.execute(f"SELECT COUNT(*) FROM invoices WHERE invoice_type = 'sale' AND {p2}").fetchone()[0],
    }  # fmt: skip
    conn.close()
    return check


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument(
        "--raw",
        type=Path,
        help="原始文件（.xlsx 或 .parquet）；默认 data/raw/online_retail_II.xlsx",
    )
    ap.add_argument("--download", action="store_true", help="原始文件不存在时从 UCI 下载")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()  # fmt: skip
    raw = args.raw or (download() if args.download else RAW_XLSX)
    if not raw.exists():
        print(f"找不到原始文件 {raw}，加 --download 从 UCI 下载，或用 --raw 指定", file=sys.stderr)
        sys.exit(1)
    check = build(raw, args.out)
    print(f"已生成 {args.out}")
    for k, v in check.items():
        print(f"  {k}: {v:,}")
    print(
        "近 12 个月（2010-12 至 2011-11）应为：GMV 9,632,333.24、退货额 301,809.80、订单 18,957（看板 findings.json）"
    )


if __name__ == "__main__":
    main()
