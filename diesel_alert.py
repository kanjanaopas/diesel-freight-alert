#!/usr/bin/env python3
from __future__ import annotations  # รองรับ Python 3.9 (type hint | syntax)
"""
diesel_alert.py — ระบบตรวจสอบราคา Hi Diesel B7 (PTT) และแจ้งเตือนค่าขนส่งคอนกรีต
GitHub Actions version — ทำงานบน Linux ได้เต็มที่ ไม่ต้องเปิด MacBook

แหล่งข้อมูล (เรียงลำดับความสำคัญ):
  1. PTT OR price board  — https://www.pttor.com/oil_price_board?lang=th  (official)
  2. PTT OR SOAP API     — https://orapiweb.pttor.com/oilservice/OilPrice.asmx
  3. kapook.com          — fallback
  4. yotathai.com        — fallback
  5. Bangchak API        — fallback สุดท้าย

วิธีรัน:
  python3 diesel_alert.py
  python3 diesel_alert.py --dry-run          (แสดงผลแต่ไม่ส่ง LINE)
  python3 diesel_alert.py --force-price 39.50  (ระบุราคาตรงๆ สำหรับ test)
  python3 diesel_alert.py --dump-soap        (แสดง raw XML จาก PTT SOAP API แล้วออก)
"""

import json
import os
import sys
import re
import time
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# CONFIG & STATE
# ──────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "freight_config.json")
STATE_FILE  = os.path.join(SCRIPT_DIR, "freight_state.json")

THAI_MONTHS_SHORT = [
    "", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."
]

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    # GitHub Secrets override ค่าใน file
    cfg["lineNotifyToken"] = os.environ.get("LINE_TOKEN") or cfg.get("lineNotifyToken", "")
    cfg["lineUserId"]      = os.environ.get("LINE_USER_ID") or cfg.get("lineUserId", "")
    return cfg

def load_state():
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print("💾 State file อัปเดตแล้ว")

def thai_date(d: date) -> str:
    """แปลง date เป็น เช่น '26 เม.ย. 2569'"""
    return f"{d.day} {THAI_MONTHS_SHORT[d.month]} {d.year + 543}"

def today_th() -> date:
    """วันที่ปัจจุบันตาม timezone ไทย (UTC+7)"""
    from datetime import timezone
    utc_now = datetime.now(timezone.utc)
    thai_now = utc_now + timedelta(hours=7)
    return thai_now.date()


# ──────────────────────────────────────────────
# PRICE SCRAPING — หลาย source
# ──────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "th-TH,th;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def _parse_price(text: str) -> float | None:
    """ดึงตัวเลขราคาจาก string เช่น '40.20 บาท' → 40.20"""
    m = re.search(r"(\d{2,3}\.\d{1,2})", text.replace(",", ""))
    if m:
        v = float(m.group(1))
        if 20.0 <= v <= 80.0:   # range ราคาน้ำมันสมเหตุสมผล
            return v
    return None

# ──────────────────────────────────────────────
# SOURCE 1 — PTT OR Oil Price Board (official page)
# ──────────────────────────────────────────────

# Pattern สำหรับ oil_price_board — รวม "ดีเซล" ทั่วไป (ไม่มี B7) ด้วย
DIESEL_BOARD_PATTERN = re.compile(
    r"(Hi.?Diesel|ไฮ.?ดีเซล|HiDiesel|ดีเซล(?!\s*เบนซิน)|Diesel(?!.*Gasoline))",
    re.IGNORECASE
)

def scrape_pttor_oilboard(debug: bool = False) -> float | None:
    """
    Source 1: PTT official oil price board
    https://www.pttor.com/oil_price_board?lang=th
    แสดงราคาปัจจุบันอย่างเป็นทางการ อัปเดตเมื่อมีการปรับราคา
    เป็นหน้าเดียวกับที่สัญญาอ้างอิง

    หมายเหตุ: หน้านี้อาจ render ด้วย JavaScript → ถ้า BeautifulSoup
    ไม่เจอข้อมูล จะลองใช้ Playwright (ถ้า install ไว้)
    """
    url = "https://www.pttor.com/oil_price_board?lang=th"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()

        if debug:
            print(f"\n──── oil_price_board RAW HTML (first 3000 chars) ────")
            print(resp.text[:3000])
            print("─────────────────────────────────────\n")
            print(f"──── oil_price_board TEXT ────")
            from bs4 import BeautifulSoup as _BS
            print(_BS(resp.text, "html.parser").get_text(separator="\n")[:1500])
            print("─────────────────────────────────────\n")

        soup = BeautifulSoup(resp.text, "html.parser")

        # Strategy 1: ตารางใน <table>
        tables = soup.find_all("table")
        for table in tables:
            rows = table.find_all("tr")
            for row in rows:
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                row_text = " ".join(cells)
                if DIESEL_BOARD_PATTERN.search(row_text):
                    for cell in cells:
                        price = _parse_price(cell)
                        if price:
                            print(f"  PTT oilboard (table) → {row_text[:70]} → {price}")
                            return price

        # Strategy 2: element ที่มี class เกี่ยวกับราคา
        for tag in soup.find_all(string=DIESEL_BOARD_PATTERN):
            parent = tag.find_parent()
            if parent:
                for sib in [parent.find_next_sibling(), parent.find_parent()]:
                    if sib:
                        price = _parse_price(sib.get_text())
                        if price:
                            print(f"  PTT oilboard (sibling) → {tag.strip()[:50]} → {price}")
                            return price

        # Strategy 3: ค้นหาใน text ทีละบรรทัด
        lines = soup.get_text(separator="\n").split("\n")
        for i, line in enumerate(lines):
            if DIESEL_BOARD_PATTERN.search(line):
                window = " ".join(lines[i:i+4])
                price = _parse_price(window)
                if price:
                    print(f"  PTT oilboard (lines) → {line.strip()[:60]} → {price}")
                    return price

        # Strategy 4: regex กว้างๆ ใน raw HTML
        m = re.search(
            r"(?:Hi\s*Diesel|ไฮ\s*ดีเซล|ดีเซล)[^<\n]{0,60}?(\d{2,3}\.\d{1,2})",
            resp.text, re.IGNORECASE
        )
        if m:
            price = float(m.group(1))
            if 20.0 <= price <= 80.0:
                print(f"  PTT oilboard (html-regex) → {price}")
                return price

        # หน้านี้อาจเป็น JS-rendered → ลอง Playwright
        print("  PTT oilboard: BeautifulSoup ไม่เจอราคา → ลอง Playwright...")
        return _scrape_oilboard_playwright(url, debug)

    except Exception as e:
        print(f"  ⚠️ PTT oilboard error: {e}")
    return None


def _scrape_oilboard_playwright(url: str, debug: bool = False) -> float | None:
    """
    ใช้ Playwright render JavaScript แล้ว parse ราคาจาก oil_price_board
    ยังดัก network requests เพื่อจับ API endpoint ที่หน้าเว็บเรียก
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        print("  PTT oilboard: Playwright ไม่ได้ install → ข้ามไป")
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                extra_http_headers={
                    "Accept-Language": "th-TH,th;q=0.9",
                    "User-Agent": HEADERS["User-Agent"],
                }
            )
            page = context.new_page()

            # ── ดัก network responses ──────────────────────────
            api_hits: list[tuple[str, bytes]] = []

            def on_response(response):
                try:
                    rurl = response.url
                    ct = response.headers.get("content-type", "")
                    if response.status == 200 and any(x in ct for x in ("json", "xml", "soap")):
                        body = response.body()
                        lower = body.lower()
                        if any(k in lower for k in (b"diesel", b"oil", b"price",
                                                     "ดีเซล".encode(), "ราคา".encode())):
                            api_hits.append((rurl, body))
                except Exception:
                    pass

            page.on("response", on_response)

            # ── โหลดหน้า ──────────────────────────────────────
            page.goto(url, timeout=30000, wait_until="networkidle")
            try:
                page.wait_for_timeout(3000)
            except Exception:
                pass

            # ── ดึงข้อมูลทั้งหมดก่อน close browser ────────────
            html = page.content()

            # JS: ดึง table rows (label + price ในแถวเดียวกัน)
            table_rows = []
            try:
                table_rows = page.evaluate("""
                    () => {
                        const rows = [];
                        document.querySelectorAll('tr').forEach(row => {
                            const cells = Array.from(row.querySelectorAll('td, th'))
                                .map(c => (c.innerText || c.textContent || '').trim())
                                .filter(t => t.length > 0);
                            if (cells.length >= 2) rows.push(cells);
                        });
                        return rows;
                    }
                """) or []
            except Exception as e:
                print(f"  ⚠️ JS table: {e}")

            # JS: ดึง text nodes ทั้งหมด (สำรอง)
            all_nodes = []
            try:
                all_nodes = page.evaluate("""
                    () => {
                        const items = [];
                        const walker = document.createTreeWalker(
                            document.body, 0x4, null, false
                        );
                        let node;
                        while (node = walker.nextNode()) {
                            const t = node.textContent.trim();
                            if (t.length > 1 && t.length < 120) items.push(t);
                        }
                        return items;
                    }
                """) or []
            except Exception as e:
                print(f"  ⚠️ JS nodes: {e}")

            browser.close()  # ← close หลังจาก evaluate ทั้งหมดเสร็จแล้ว

        # ── 1) ลอง parse จาก API responses ──────────────────
        for api_url, body in api_hits:
            print(f"  PTT oilboard → พบ API: {api_url[:80]}")
            try:
                # ลอง JSON
                data = json.loads(body)
                text = json.dumps(data, ensure_ascii=False)
                m = re.search(
                    r"(?:diesel|ดีเซล)[^}\"]{0,80}?(\d{2,3}\.\d{1,2})",
                    text, re.IGNORECASE
                )
                if m:
                    price = float(m.group(1))
                    if 20.0 <= price <= 80.0:
                        print(f"  PTT oilboard (api-json) → {price}")
                        return price
            except (json.JSONDecodeError, ValueError):
                pass
            # ลอง text/XML
            m = re.search(
                rb"(?:diesel|\xe0\xb8\x94\xe0\xb8\xb5\xe0\xb9\x80\xe0\xb8\x8b\xe0\xb8\xa5)"
                rb".{0,100}?(\d{2,3}\.\d{1,2})",
                body, re.IGNORECASE
            )
            if m:
                price = float(m.group(1))
                if 20.0 <= price <= 80.0:
                    print(f"  PTT oilboard (api-raw) → {price}")
                    return price

        # ── 2) ใช้ JavaScript evaluate ดึง table structure จาก rendered DOM ─
        try:
            # ดึง rows ทั้งหมดจาก table (label + prices ในแถวเดียวกัน)
            table_rows = page.evaluate("""
                () => {
                    const rows = [];
                    document.querySelectorAll('tr').forEach(row => {
                        const cells = Array.from(row.querySelectorAll('td, th'))
                            .map(c => (c.innerText || c.textContent || '').trim())
                            .filter(Boolean);
                        if (cells.length) rows.push(cells);
                    });
                    return rows;
                }
            """)
            if debug:
                print(f"  Playwright table rows: {table_rows[:15]}")

            for row in (table_rows or []):
                row_text = " ".join(row)
                if DIESEL_BOARD_PATTERN.search(row_text):
                    for cell in row:
                        price = _parse_price(cell)
                        if price:
                            print(f"  PTT oilboard (playwright-js-table) → {row_text[:70]} → {price}")
                            return price
        except Exception as e_js:
            print(f"  ⚠️ JS table evaluate: {e_js}")

        # ── 3) ใช้ JavaScript ดึง text nodes ทั้งหมด ─────────────────────
        try:
            all_nodes = page.evaluate("""
                () => {
                    const items = [];
                    const walker = document.createTreeWalker(
                        document.body, 0x4, null, false
                    );
                    let node;
                    while (node = walker.nextNode()) {
                        const t = node.textContent.trim();
                        if (t.length > 1 && t.length < 120) items.push(t);
                    }
                    return items;
                }
            """) or []

            if debug:
                print(f"  Playwright all text nodes ({len(all_nodes)}): {all_nodes[:30]}")

            # หา "ดีเซล" แล้วดูตัวเลขในบรรทัดถัดๆ ไป (window 5 nodes)
            for i, node in enumerate(all_nodes):
                if DIESEL_BOARD_PATTERN.search(node):
                    window = " ".join(all_nodes[i:i+6])
                    price = _parse_price(window)
                    if price:
                        print(f"  PTT oilboard (playwright-nodes) → {node.strip()[:50]} → {price}")
                        return price

            # ถ้ายังไม่เจอ label+price คู่กัน ลองดูจาก timestamp + ตัวเลขถัดไป
            # (หน้า oil_price_board อาจแสดงราคาโดยไม่มี label ในแต่ละ node)
            prices_found = [float(m.group(1)) for n in all_nodes
                            for m in [re.search(r"^(\d{2,3}\.\d{1,2})$", n.strip())]
                            if m and 20.0 <= float(m.group(1)) <= 80.0]
            if debug:
                print(f"  Playwright price nodes: {prices_found}")

        except Exception as e_nodes:
            print(f"  ⚠️ JS nodes evaluate: {e_nodes}")

        # ── 4) parse จาก rendered HTML ───────────────────────────────────
        if debug:
            print(f"\n──── Playwright rendered HTML (first 3000) ────")
            print(html[:3000])
            print("──────────────────────────────────────\n")

        soup = BeautifulSoup(html, "html.parser")

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                row_text = " ".join(cells)
                if DIESEL_BOARD_PATTERN.search(row_text):
                    for cell in cells:
                        price = _parse_price(cell)
                        if price:
                            print(f"  PTT oilboard (playwright-bs-table) → {row_text[:70]} → {price}")
                            return price

        lines = soup.get_text(separator="\n").split("\n")
        for i, line in enumerate(lines):
            if DIESEL_BOARD_PATTERN.search(line):
                window = " ".join(lines[i:i+6])
                price = _parse_price(window)
                if price:
                    print(f"  PTT oilboard (playwright-bs-lines) → {line.strip()[:60]} → {price}")
                    return price

        sample_lines = [l.strip() for l in lines if len(l.strip()) > 3]
        print(f"  PTT oilboard: Playwright ก็ไม่เจอราคา")
        print(f"  text nodes sample: {sample_lines[:15]}")
        if api_hits:
            print(f"  พบ {len(api_hits)} API call แต่ parse ราคาไม่ได้")

    except Exception as e:
        print(f"  ⚠️ Playwright error: {e}")
    return None


# ──────────────────────────────────────────────
# SOURCE 2 — PTT OR SOAP API (official)
# ──────────────────────────────────────────────

PTTOR_SOAP_URL    = "https://orapiweb.pttor.com/oilservice/OilPrice.asmx"
PTTOR_SOAP_ACTION = "https://orapiweb.pttor.com/GetOilPriceProvincial"
PTTOR_NS          = "http://www.pttor.com"   # ← namespace จริงจาก WSDL

# ชื่อจังหวัดที่ใช้ (สัญญาอ้างอิง กทม.-ปริมณฑล)
PTTOR_PROVINCE = "กรุงเทพมหานคร"

# pattern ชื่อน้ำมัน Hi Diesel B7 ในภาษาไทย/อังกฤษ
HI_DIESEL_PATTERN = re.compile(
    r"(Hi.?Diesel|ไฮ.?ดีเซล|HiDiesel|B7|ดีเซล.*B7|Diesel.*B7|ดีเซลบี7)",
    re.IGNORECASE
)

def _build_soap_envelope(dd: int, mm: int, yyyy: int, lang: str = "TH", province: str = "") -> bytes:
    """สร้าง SOAP 1.1 XML body สำหรับ GetOilPriceProvincial"""
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetOilPriceProvincial xmlns="{PTTOR_NS}">
      <Language>{lang}</Language>
      <DD>{dd}</DD>
      <MM>{mm}</MM>
      <YYYY>{yyyy}</YYYY>
      <Province>{province}</Province>
    </GetOilPriceProvincial>
  </soap:Body>
</soap:Envelope>"""
    return body.encode("utf-8")


def _call_pttor_soap_raw(body: bytes) -> bytes | None:
    """HTTP POST ไปยัง PTT SOAP API และคืน raw bytes"""
    try:
        resp = requests.post(
            PTTOR_SOAP_URL,
            data=body,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction":   f'"{PTTOR_SOAP_ACTION}"',
                "User-Agent":   "Mozilla/5.0 (compatible; FreightCostAlert/1.0)",
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"  ⚠️ PTT SOAP call error: {e}")
        return None


def _extract_inner_xml(raw_soap: bytes) -> str | None:
    """
    PTT API คืน XML ที่ถูก HTML-escape ซ้อนอยู่ใน <GetOilPriceProvincialResult>:
      &lt;PTTOR_DS&gt;&lt;Table&gt;...&lt;/PTTOR_DS&gt;
    แกะออกมาเป็น XML จริงเพื่อ parse ต่อ
    """
    import html as html_lib
    try:
        root = ET.fromstring(raw_soap)
    except ET.ParseError as e:
        print(f"  ⚠️ SOAP XML ParseError: {e}")
        return None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag.endswith("Result") and elem.text:
            inner = html_lib.unescape(elem.text.strip())
            print(f"  PTT inner XML ({len(inner)} chars): {inner[:120]}")
            return inner
    return None


def _parse_inner_xml(inner_xml: str) -> float | None:
    """
    Parse XML dataset ที่แกะออกมาจาก SOAP result
    field names ที่คาดหวัง: OIL_NAME / RETAIL_PRICE หรือ similar
    """
    EMPTY = ("<PTTOR_DS></PTTOR_DS>", "<PTTOR_DS/>", "")
    if not inner_xml or inner_xml.strip() in EMPTY:
        print("  PTT SOAP → dataset ว่าง (Province/Date ไม่มีข้อมูล)")
        return None
    try:
        root = ET.fromstring(inner_xml)
    except ET.ParseError as e:
        print(f"  ⚠️ Inner XML ParseError: {e}")
        return None

    def strip_ns(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    records: list[dict[str, str]] = []
    def collect(node: ET.Element):
        children = list(node)
        if children and all(len(list(c)) == 0 for c in children):
            records.append({strip_ns(c.tag).lower(): (c.text or "").strip() for c in children})
        else:
            for c in children:
                collect(c)
    collect(root)

    fields = list(records[0].keys()) if records else []
    print(f"  PTT SOAP → {len(records)} records, fields: {fields}")

    # แสดงชื่อสินค้าทั้งหมดเพื่อ debug (ถ้าไม่เจอ match)
    prod_field = next((k for k in fields if "product" in k or "name" in k or "oil" in k), None)
    if prod_field:
        prod_names = [r.get(prod_field, "?") for r in records]
        print(f"  PTT SOAP → product names: {prod_names}")

    # Pattern ค้นหา Hi Diesel B7 — รวม "ดีเซล" เพียงอย่างเดียว (API อาจใช้ชื่อสั้น)
    HI_DIESEL_BROAD = re.compile(
        r"(Hi.?Diesel|ไฮ.?ดีเซล|HiDiesel|B7|ดีเซล.*B7|Diesel.*B7|ดีเซลบี7"
        r"|ดีเซล(?!.*B20|.*พรีเมียม|.*ธรรมดา)"  # "ดีเซล" ที่ไม่ใช่ B20 หรือธรรมดา
        r"|^Diesel$|^diesel$)",
        re.IGNORECASE
    )

    price_keys = ["retail_price", "price", "retailprice", "oilprice", "amount", "value"]
    for rec in records:
        all_vals = " ".join(rec.values())
        if HI_DIESEL_BROAD.search(all_vals) or HI_DIESEL_PATTERN.search(all_vals):
            for pk in price_keys:
                if pk in rec:
                    p = _parse_price(rec[pk])
                    if p:
                        print(f"  PTT SOAP ✅ {all_vals[:80]!r} → {p}")
                        return p
            for k, v in rec.items():
                if not any(x in k for x in ("date", "province")):
                    p = _parse_price(v)
                    if p:
                        print(f"  PTT SOAP ✅ (field:{k}={v}) → {p}")
                        return p

    print("  PTT SOAP → ไม่พบ Hi Diesel B7 ใน records")
    return None


PTTOR_BASE_URL = "https://orapiweb.pttor.com"

def fetch_pttor_current() -> float | None:
    """
    ลอง 2 endpoints ที่ไม่ต้องระบุจังหวัด:
      1. CurrentOilPrice  — ราคาปัจจุบัน (ไม่ต้องวันที่)
      2. GetOilPrice      — ราคาตามวันที่ ไม่มี Province parameter
    WSDL: https://orapiweb.pttor.com/oilservice/OilPrice.asmx?WSDL
    """
    today = today_th()

    # ── 1. CurrentOilPrice ────────────────────────────────
    for lang in ["TH", "EN"]:
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <CurrentOilPrice xmlns="{PTTOR_NS}">
      <Language>{lang}</Language>
    </CurrentOilPrice>
  </soap:Body>
</soap:Envelope>""".encode("utf-8")
        try:
            resp = requests.post(
                PTTOR_SOAP_URL,
                data=body,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{PTTOR_BASE_URL}/CurrentOilPrice"',
                    "User-Agent": "Mozilla/5.0 (compatible; FreightCostAlert/1.0)",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                print(f"  PTT CurrentOilPrice (lang={lang}) → HTTP 200")
                inner = _extract_inner_xml(resp.content)
                if inner and inner.strip() not in ("<PTTOR_DS></PTTOR_DS>", "<PTTOR_DS/>", ""):
                    price = _parse_inner_xml(inner)
                    if price:
                        return price
            else:
                print(f"  PTT CurrentOilPrice (lang={lang}) → HTTP {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️ CurrentOilPrice error: {e}")

    # ── 2. GetOilPrice (ไม่มี Province) — ย้อนหลัง 7 วัน ────────────
    print("  PTT SOAP → ลอง GetOilPrice (ไม่มี Province)...")
    for days_back in range(0, 8):
        check_date = today - timedelta(days=days_back)
        dd, mm, yyyy = check_date.day, check_date.month, check_date.year
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetOilPrice xmlns="{PTTOR_NS}">
      <Language>TH</Language>
      <DD>{dd}</DD>
      <MM>{mm}</MM>
      <YYYY>{yyyy}</YYYY>
    </GetOilPrice>
  </soap:Body>
</soap:Envelope>""".encode("utf-8")
        try:
            resp = requests.post(
                PTTOR_SOAP_URL,
                data=body,
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{PTTOR_BASE_URL}/GetOilPrice"',
                    "User-Agent": "Mozilla/5.0 (compatible; FreightCostAlert/1.0)",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                inner = _extract_inner_xml(resp.content)
                if inner and inner.strip() not in ("<PTTOR_DS></PTTOR_DS>", "<PTTOR_DS/>", ""):
                    print(f"  GetOilPrice ({check_date}) → HTTP 200, parsing...")
                    price = _parse_inner_xml(inner)
                    if price:
                        return price
            else:
                print(f"  GetOilPrice ({check_date}) → HTTP {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️ GetOilPrice error: {e}")

    return None


def fetch_pttor_soap(today: date, dump: bool = False) -> float | None:
    """
    ดึงราคาจาก PTT OR Official SOAP API

    หมายเหตุ: GetOilPriceProvincial คืนข้อมูลเฉพาะวันที่ PTT ประกาศราคา
    (ไม่ใช่ทุกวัน) → ต้องย้อนหลังสูงสุด 7 วัน
    API อาจใช้ปี พ.ศ. (YYYY+543) ดังนั้นลองทั้งสองแบบ
    """
    # Strategy 1: ลอง CurrentOilPrice ก่อน (ไม่ต้องระบุวันที่)
    print("  PTT SOAP → ลอง CurrentOilPrice (www1.pttor.com)...")
    price = fetch_pttor_current()
    if price:
        return price

    # Strategy 2: GetOilPriceProvincial ย้อนหลังสูงสุด 7 วัน
    # ลองทั้งปี ค.ศ. (2026) และ พ.ศ. (2569) เพราะ API ไม่ชัดเจน
    province = "กรุงเทพมหานคร"
    for days_back in range(0, 8):
        check_date = today - timedelta(days=days_back)
        dd, mm = check_date.day, check_date.month
        ce_year = check_date.year
        be_year = ce_year + 543

        label = "วันนี้" if days_back == 0 else f"ย้อนหลัง {days_back} วัน ({check_date})"

        # ลองปี ค.ศ. เท่านั้น (BE year=2569 คืน 400 BAD REQUEST ทุกครั้ง)
        print(f"  PTT SOAP → {label} (YYYY={ce_year})...")
        raw = _call_pttor_soap_raw(_build_soap_envelope(dd, mm, ce_year, "TH", province))
        if raw is None:
            continue

        if dump and days_back == 0:
            print(f"\n──── RAW XML ({check_date}) ────")
            print(raw.decode("utf-8", errors="replace"))
            print("─────────────────────────────────────\n")

        inner = _extract_inner_xml(raw)
        if not inner:
            continue

        price = _parse_inner_xml(inner)
        if price:
            if days_back > 0:
                print(f"  (ราคาจาก {check_date} — วันที่ประกาศล่าสุด)")
            return price

    return None


# ──────────────────────────────────────────────
# SOURCE 2 — kapook.com (fallback)
# ──────────────────────────────────────────────

def scrape_kapook() -> float | None:
    """
    Source 3: gasprice.kapook.com
    หน้าตารางราคาน้ำมันทุกบริษัท — อัปเดตทุกวัน
    """
    url = "https://gasprice.kapook.com/gasprice.php"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        # ใช้ resp.content (bytes) ให้ BeautifulSoup ตรวจ charset จาก meta tag เอง
        # (ไม่ใช้ resp.text เพราะ requests อาจ detect encoding ผิดเป็น Latin-1)
        soup = BeautifulSoup(resp.content, "html.parser")
        full_text = soup.get_text(separator="\n")

        # kapook เรียก Hi Diesel B7 ว่า "ดีเซล" (สั้นๆ)
        # "ดีเซลพรีเมียม" = น้ำมันอื่น (ราคาสูงกว่า), "ดีเซล B20" = B20
        # Pattern ตรงกับ "ดีเซล" แบบ standalone เท่านั้น
        KAPOOK_DIESEL_PAT = re.compile(
            r"(Hi.?Diesel|ไฮ.?ดีเซล|HiDiesel|B7|ดีเซล\s*B7"
            r"|(?<![ก-๛])ดีเซล(?!\s*(?:พรีเมียม|B20|บี20|ธรรมดา)))",
            re.IGNORECASE
        )

        # Strategy 1: ตาราง — หาแถว "ดีเซล" ใน PTT section
        for table in soup.find_all("table"):
            table_text = table.get_text()
            in_ptt = re.search(r"ปตท|PTT", table_text, re.I)
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                row_text = " ".join(cells)
                if KAPOOK_DIESEL_PAT.search(row_text):
                    for cell in cells:
                        price = _parse_price(cell)
                        # Hi Diesel B7 ควรอยู่ในช่วง 35-50 บาท
                        if price and 35.0 <= price <= 50.0:
                            src = "(PTT table)" if in_ptt else "(table)"
                            print(f"  kapook {src} → {row_text[:70]} → {price}")
                            return price

        # Strategy 2: ทีละบรรทัด — หา "ดีเซล" standalone แล้วดูราคาถัดไป
        lines = full_text.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            # ต้องเป็น "ดีเซล" แบบ standalone หรือ Hi Diesel
            if KAPOOK_DIESEL_PAT.search(stripped) and len(stripped) < 30:
                # ดูตัวเลขในบรรทัดเดียวกันหรือถัดไป 3 บรรทัด
                for j in range(0, 4):
                    if i + j < len(lines):
                        price = _parse_price(lines[i + j])
                        if price and 35.0 <= price <= 50.0:
                            print(f"  kapook (lines) → {stripped[:50]} → {price}")
                            return price

        # Strategy 3: regex กว้าง
        m = re.search(
            r"(?:Hi\s*Diesel|ไฮ\s*ดีเซล|ดีเซล\s*B7|(?<![ก-๛])ดีเซล(?!\s*(?:พรีเมียม|B20)))"
            r"[^\n]{0,60}?(\d{2,3}\.\d{1,2})",
            full_text, re.IGNORECASE
        )
        if m:
            price = float(m.group(1))
            if 35.0 <= price <= 50.0:
                print(f"  kapook (regex) → {price}")
                return price

        print(f"  kapook → ไม่พบ Hi Diesel B7 / ดีเซล (35-50 บาท)")

    except Exception as e:
        print(f"  ⚠️ kapook error: {e}")
    return None


def scrape_yotathai() -> float | None:
    """
    Source 3: yotathai.com/oil.html
    แสดงราคาน้ำมันทุกชนิดและทุกบริษัท — format ค่อนข้างคงที่
    """
    url = "https://www.yotathai.com/oil.html"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")  # ใช้ content ไม่ใช่ text

        full_text = soup.get_text()
        # ค้นหา pattern ราคา Hi Diesel
        m = re.search(
            r"(?:Hi\s*Diesel|ไฮดีเซล|Diesel\s*B7|ดีเซล\s*B7)[^\n]*?(\d{2,3}\.\d{1,2})",
            full_text, re.IGNORECASE
        )
        if m:
            price = float(m.group(1))
            if 20.0 <= price <= 80.0:
                print(f"  yotathai → {price}")
                return price

        # ลองหาจากตาราง
        for tag in soup.find_all(string=re.compile(r"Hi.?Diesel|B7", re.I)):
            parent = tag.find_parent("tr")
            if parent:
                cells = parent.find_all("td")
                for cell in cells:
                    price = _parse_price(cell.get_text())
                    if price:
                        print(f"  yotathai (table) → {price}")
                        return price

    except Exception as e:
        print(f"  ⚠️ yotathai error: {e}")
    return None


def scrape_bangchak_history() -> float | None:
    """
    Source 4: bangchak.co.th/th/oilprice/historical (JSON API)
    Bangchak ราคาเดียวกับ PTT ในกรณีปกติ — เป็น fallback สุดท้าย
    """
    url = "https://www.bangchak.co.th/api/oilprice/getLatest?lang=th"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # ค้น key ที่เกี่ยวกับ B7 หรือ diesel
            text = json.dumps(data, ensure_ascii=False)
            m = re.search(r'"(?:B7|HiDiesel|hi_diesel|diesel_b7)"[^}]*?"price"[^:]*:.*?(\d{2,3}\.\d{1,2})', text, re.I)
            if m:
                price = float(m.group(1))
                if 20.0 <= price <= 80.0:
                    print(f"  bangchak API → {price}")
                    return price
            # ลองหาทั่วไป
            for item in (data if isinstance(data, list) else data.get("data", [])):
                name = str(item.get("name", "") or item.get("product", ""))
                if re.search(r"B7|diesel", name, re.I):
                    price = _parse_price(str(item.get("price", "")))
                    if price:
                        print(f"  bangchak API item → {name}: {price}")
                        return price
    except Exception as e:
        print(f"  ⚠️ bangchak API error: {e}")
    return None


def get_diesel_price(today: date, dump_soap: bool = False, debug_board: bool = False) -> tuple[float | None, bool, str]:
    """
    ดึงราคาจาก sources เรียงตามลำดับความน่าเชื่อถือ:
      1. PTT OR oil_price_board  ← หน้าราคาอย่างเป็นทางการ (อ้างอิงสัญญา)
      2. PTT OR SOAP API         ← API ทางการ (ลองปี ค.ศ. + พ.ศ.)
      3. kapook.com              ← fallback
      4. yotathai.com            ← fallback
      5. bangchak historical API ← fallback สุดท้าย
    คืน (ราคา, confirmed, source_name)
    """
    # ── 1. PTT OR oil_price_board ───────────────
    print("🔍 [1/5] PTT OR oil_price_board (official)...")
    price = scrape_pttor_oilboard(debug=debug_board)
    if price:
        return price, True, "PTT OR oil_price_board (official)"
    print("  → ไม่สำเร็จ ลอง SOAP API\n")
    time.sleep(1)

    # ── 2. PTT OR SOAP API ──────────────────────
    print("🔍 [2/5] PTT OR Official SOAP API...")
    price = fetch_pttor_soap(today, dump=dump_soap)
    if price:
        return price, True, "PTT OR SOAP API (official)"
    print("  → ไม่สำเร็จ ลอง fallback\n")
    time.sleep(1)

    # ── 3. kapook.com ───────────────────────────
    print("🔍 [3/5] kapook.com...")
    price = scrape_kapook()
    if price:
        return price, True, "kapook.com"
    time.sleep(2)

    # ── 4. yotathai.com ─────────────────────────
    print("🔍 [4/5] yotathai.com...")
    price = scrape_yotathai()
    if price:
        return price, True, "yotathai.com"
    time.sleep(2)

    # ── 5. bangchak historical ──────────────────
    print("🔍 [5/5] Bangchak historical API...")
    price = scrape_bangchak_history()
    if price:
        return price, True, "Bangchak API (fallback)"

    return None, False, "ไม่พบแหล่งข้อมูล"


# ──────────────────────────────────────────────
# ROUND LOGIC
# ──────────────────────────────────────────────

def get_round_info(d: date) -> dict:
    """
    คืนข้อมูลรอบปัจจุบัน:
      num        — รอบที่ (1–4)
      is_ref_day — วันนี้เป็นวันอ้างอิงหรือไม่
      ref_day    — วันที่ของ ref day (int)
      rate_start — วันเริ่มใช้อัตรา (date)
      rate_end   — วันสุดท้ายของอัตรา (date)
    """
    day = d.day
    m   = d.month
    y   = d.year

    def last_day(yr, mo):
        if mo == 12:
            return date(yr+1, 1, 1) - timedelta(days=1)
        return date(yr, mo+1, 1) - timedelta(days=1)

    # รอบ 1 (1–7): ref = 27 ของเดือนก่อน
    # รอบ 2 (8–14): ref = 5 ของเดือนนี้
    # รอบ 3 (15–21): ref = 12
    # รอบ 4 (22–eos): ref = 19

    if 1 <= day <= 7:
        prev_m = m - 1 if m > 1 else 12
        prev_y = y if m > 1 else y - 1
        return {
            "num": 1,
            "is_ref_day": day == 27 and False,  # ref ของรอบนี้อยู่เดือนก่อน
            "ref_day": 27,
            "rate_start": date(y, m, 1),
            "rate_end":   date(y, m, 7),
            "ref_date":   date(prev_y, prev_m, 27),
        }
    elif 8 <= day <= 14:
        return {
            "num": 2,
            "is_ref_day": day == 5,
            "ref_day": 5,
            "rate_start": date(y, m, 8),
            "rate_end":   date(y, m, 14),
            "ref_date":   date(y, m, 5),
        }
    elif 15 <= day <= 21:
        return {
            "num": 3,
            "is_ref_day": day == 12,
            "ref_day": 12,
            "rate_start": date(y, m, 15),
            "rate_end":   date(y, m, 21),
            "ref_date":   date(y, m, 12),
        }
    else:  # 22–สิ้นเดือน
        return {
            "num": 4,
            "is_ref_day": day == 19,
            "ref_day": 19,
            "rate_start": date(y, m, 22),
            "rate_end":   last_day(y, m),
            "ref_date":   date(y, m, 19),
        }

def is_ref_day(d: date) -> bool:
    """วันนี้เป็นวันอ้างอิงปกติหรือไม่ (5, 12, 19, 27)"""
    return d.day in (5, 12, 19, 27)


# ──────────────────────────────────────────────
# LINE MESSAGING
# ──────────────────────────────────────────────

def send_line(token: str, user_id: str, message: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("\n──── DRY RUN — ข้อความที่จะส่ง ────")
        print(message)
        print("──────────────────────────────────\n")
        return True

    url = "https://api.line.me/v2/bot/message/push"
    payload = json.dumps({
        "to": user_id,
        "messages": [{"type": "text", "text": message}]
    }).encode("utf-8")

    try:
        resp = requests.post(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"✅ LINE sent: HTTP 200")
            return True
        else:
            print(f"❌ LINE error: HTTP {resp.status_code} — {resp.text}")
            return False
    except Exception as e:
        print(f"❌ LINE exception: {e}")
        return False


# ──────────────────────────────────────────────
# MESSAGE BUILDER
# ──────────────────────────────────────────────

def build_message(
    today: date,
    today_price: float | None,
    last_adj_price: float,
    last_adj_date: str,
    decision: str,          # "none" | "regular" | "special"
    round_info: dict,
    cumulative_change: float,
    single_change: float = 0.0,   # เปลี่ยนครั้งนี้ (สำหรับ special)
    source: str = "",
    dashboard_url: str = "",
) -> str:
    today_str = thai_date(today)
    last_adj_str = thai_date(date.fromisoformat(last_adj_date))

    if today_price is None:
        lines_err = [
            f"⚠️ ระบบแจ้งเตือนราคาน้ำมัน | {today_str}",
            "",
            f"❌ ไม่สามารถดึงราคาน้ำมัน Hi Diesel B7 ได้วันนี้",
            f"📌 อ้างอิงล่าสุด: {last_adj_price:.2f} บาท ({last_adj_str})",
            "",
            f"🔍 กรุณาตรวจสอบด้วยตนเองที่:",
            f"https://www.pttor.com/th/oilprice",
        ]
        if dashboard_url:
            lines_err.append(f"📊 Dashboard: {dashboard_url}")
        return "\n".join(lines_err)

    cumul_sign  = "+" if cumulative_change >= 0 else ""
    single_sign = "+" if single_change >= 0 else ""
    abs_single  = abs(single_change)
    abs_cumul   = abs(cumulative_change)

    if decision == "none":
        lines = [
            f"🛢️ รายงานราคาน้ำมัน | {today_str}",
            "",
            f"💰 Hi Diesel B7 (PTT): {today_price:.2f} บาท/ลิตร",
            f"📌 อ้างอิงล่าสุด: {last_adj_price:.2f} บาท ({last_adj_str})",
            f"📊 เปลี่ยนสะสม: {cumul_sign}{cumulative_change:.2f} บาท/ลิตร",
            "",
            "✅ ไม่ต้องปรับอัตราค่าขนส่ง",
            f"📏 ห่างจากเกณฑ์พิเศษอีก {2.00 - abs_cumul:.2f} บาท",
        ]
        if is_ref_day(today):
            lines.append(
                f"📋 วันอ้างอิงรอบ {round_info['num']} — "
                f"เปลี่ยน {cumul_sign}{cumulative_change:.2f} บาท (น้อยกว่าเกณฑ์ 0.50 บาท)"
            )

    elif decision == "regular":
        rs  = thai_date(round_info["rate_start"])
        re_ = thai_date(round_info["rate_end"])
        lines = [
            f"📋 ปรับอัตราค่าขนส่ง (รอบปกติ) | {today_str}",
            "",
            f"💰 Hi Diesel B7 (PTT): {today_price:.2f} บาท/ลิตร",
            f"📌 อ้างอิงรอบก่อน: {last_adj_price:.2f} บาท ({last_adj_str})",
            f"📊 เปลี่ยนสะสม: {cumul_sign}{cumulative_change:.2f} บาท/ลิตร",
            "",
            f"✅ ปรับอัตราค่าขนส่งรอบ {round_info['num']}",
            f"📅 มีผลวันที่ {rs} – {re_}",
            f"🔄 อ้างอิงใหม่: {today_price:.2f} บาท/ลิตร",
        ]

    else:  # special
        tomorrow = today + timedelta(days=1)
        lines = [
            f"🚨 ต้องปรับอัตราค่าขนส่ง (กรณีพิเศษ) | {today_str}",
            "",
            f"💰 Hi Diesel B7 (PTT): {today_price:.2f} บาท/ลิตร",
            f"📌 อ้างอิงล่าสุด: {last_adj_price:.2f} บาท ({last_adj_str})",
            f"📊 เปลี่ยนสะสม: {cumul_sign}{cumulative_change:.2f} บาท/ลิตร ⚠️",
            "",
            "⚡ เกินเกณฑ์ 2.00 บาท → กรณีพิเศษ",
            f"📅 มีผลพรุ่งนี้ ({thai_date(tomorrow)})",
            f"💡 อ้างอิงราคาใหม่: {today_price:.2f} บาท/ลิตร",
        ]

    if source:
        lines.append(f"\n📡 แหล่งข้อมูล: {source}")
    if dashboard_url:
        lines.append(f"📊 Dashboard: {dashboard_url}")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",      action="store_true", help="แสดงผลแต่ไม่ส่ง LINE และไม่อัปเดต state")
    parser.add_argument("--force-price",  type=float,          help="ระบุราคาน้ำมันตรงๆ (ข้าม scraping)")
    parser.add_argument("--dump-soap",    action="store_true", help="แสดง raw XML จาก PTT SOAP API แล้วออก (debug)")
    parser.add_argument("--debug-board",  action="store_true", help="แสดง raw HTML จาก PTT oil_price_board แล้วออก (debug)")
    args = parser.parse_args()

    print("=" * 50)
    print("🛢️  Diesel Alert System — GitHub Actions")
    print("=" * 50)

    # 1) โหลด config & state
    cfg   = load_config()
    state = load_state()

    last_adj_price  = float(state["lastAdjPrice"])
    last_adj_date   = state["lastAdjDate"]
    # lastSeenPrice = ราคา PTT ที่เห็นล่าสุด (ใช้ตรวจ special — single-day change)
    last_seen_price = float(state.get("lastSeenPrice", last_adj_price))

    # 2) วันที่วันนี้
    today = today_th()
    print(f"📅 วันที่: {thai_date(today)} ({today})")

    # 3) ราคาน้ำมัน
    if args.force_price:
        today_price, confirmed, source = args.force_price, True, "force-price arg"
        print(f"💉 Force price: {today_price}")
    else:
        today_price, confirmed, source = get_diesel_price(
            today, dump_soap=args.dump_soap, debug_board=args.debug_board
        )
        if args.dump_soap or args.debug_board:
            print("\n✅ debug เสร็จแล้ว ออกจากโปรแกรม")
            sys.exit(0)

    print(f"⛽ ราคาวันนี้: {today_price} ({'confirmed' if confirmed else 'fallback'})")

    # 4) วันอ้างอิงและรอบ
    round_info = get_round_info(today)
    ref_today  = is_ref_day(today)
    print(f"🗓️  รอบ: {round_info['num']} | วันอ้างอิง: {'ใช่' if ref_today else 'ไม่ใช่'} (วันที่ {today.day})")

    # 5) คำนวณการเปลี่ยนแปลง
    if today_price is not None:
        # single_change: เปรียบกับราคาล่าสุดที่เห็น → ใช้ตรวจ special
        single_change     = round(today_price - last_seen_price, 2)
        # cumulative_change: สะสมจากการปรับล่าสุด → ใช้ตรวจ regular
        cumulative_change = round(today_price - last_adj_price, 2)
        abs_single        = abs(single_change)
        abs_cumul         = abs(cumulative_change)
        print(f"📊 เปลี่ยน (single):    {single_change:+.2f} บาท (จาก lastSeen {last_seen_price:.2f})")
        print(f"📊 เปลี่ยน (cumulative): {cumulative_change:+.2f} บาท (จาก lastAdj {last_adj_price:.2f})")
    else:
        single_change = cumulative_change = 0.0
        abs_single = abs_cumul = 0.0

    # 6) ตัดสินใจ
    # พิเศษ: cumulative change > 2.00 ฿ (สะสมจากการปรับล่าสุด — ทั้ง special และ regular)
    # ปกติ:  cumulative ≥ 0.50 ฿ บนวันอ้างอิง (สะสมจากการปรับล่าสุด)
    decision = "none"
    if today_price is not None:
        if abs_cumul > cfg["rules"]["specialThreshold"]:               # > 2.00 (cumulative)
            decision = "special"
        elif ref_today and abs_cumul >= cfg["rules"]["normalThreshold"]:  # ≥ 0.50
            decision = "regular"

    print(f"⚖️  ผลการตัดสินใจ: {decision}")

    # 7) สร้างข้อความ
    message = build_message(
        today             = today,
        today_price       = today_price,
        last_adj_price    = last_adj_price,
        last_adj_date     = last_adj_date,
        decision          = decision,
        round_info        = round_info,
        cumulative_change = cumulative_change,
        single_change     = single_change,
        source            = source,
        dashboard_url     = cfg.get("dashboardUrl", ""),
    )

    # 8) ส่ง LINE
    line_ok = send_line(
        token    = cfg["lineNotifyToken"],
        user_id  = cfg["lineUserId"],
        message  = message,
        dry_run  = args.dry_run,
    )

    # 9) อัปเดต state (ถ้าไม่ใช่ dry-run)
    if not args.dry_run:
        today_str = today.isoformat()
        state["lastRunDate"]   = today_str
        state["lastRunResult"] = f"decision={decision} | ราคา={today_price} | single={single_change:+.2f} | cumul={cumulative_change:+.2f}"
        state["updatedAt"]     = today_str

        # อัปเดต lastSeenPrice ทุกวันที่รู้ราคา (ใช้สำหรับ special check ครั้งหน้า)
        if today_price is not None:
            state["lastSeenPrice"] = today_price

        # อัปเดต priceHistory อัตโนมัติ (dashboard ใช้แสดงราคาบน calendar)
        if today_price is not None:
            today_iso = today.isoformat()
            history   = state.get("priceHistory", [])
            existing_idx = next(
                (i for i, h in enumerate(history) if h.get("date") == today_iso), -1
            )
            if existing_idx >= 0:
                history[existing_idx]["price"] = today_price
            else:
                history.append({"date": today_iso, "price": today_price})
            history.sort(key=lambda x: x["date"])
            state["priceHistory"] = history[-90:]
            if "historyStartPrice" not in state and history:
                state["historyStartPrice"] = history[0]["price"]

        if decision in ("regular", "special") and today_price is not None:
            state["lastAdjPrice"] = today_price
            state["lastAdjDate"]  = today_str
            state["lastAdjType"]  = decision
            chg_for_note = single_change if decision == "special" else cumulative_change
            state["lastAdjNote"]  = f"{'กรณีพิเศษ' if decision == 'special' else 'ปรับปกติ'}: {chg_for_note:+.2f} ฿ → อ้างอิงใหม่ {today_price:.2f} ฿"

            if decision == "regular":
                # คำนวณ nextRegularRefDate (ref day ของรอบถัดไป)
                next_ref = _next_ref_date(today)
                state["nextRegularRefDate"]  = next_ref.isoformat()
                state["nextRegularRoundStart"] = (next_ref + timedelta(days=_days_to_rate_start(next_ref))).isoformat()
                state["currentRound"] = {
                    "num":      round_info["num"],
                    "refDate":  today_str,
                    "refPrice": today_price,
                    "rateStart": round_info["rate_start"].isoformat(),
                    "rateEnd":   round_info["rate_end"].isoformat(),
                }

        if not line_ok:
            state["lastError"] = f"LINE ส่งไม่ได้ — {today_str}"
        else:
            state.pop("lastError", None)

        save_state(state)

    print("\n✅ เสร็จสิ้น")


def _next_ref_date(d: date) -> date:
    """หา ref date ถัดไป (5, 12, 19, 27 ของเดือนนี้หรือเดือนหน้า)"""
    ref_days = [5, 12, 19, 27]
    for rd in ref_days:
        if rd > d.day:
            try:
                return date(d.year, d.month, rd)
            except ValueError:
                pass
    # ไปเดือนหน้า
    next_m = d.month + 1 if d.month < 12 else 1
    next_y = d.year if d.month < 12 else d.year + 1
    return date(next_y, next_m, 5)

def _days_to_rate_start(ref: date) -> int:
    day = ref.day
    if day == 27: return 4    # rate starts 1st next month
    if day == 5:  return 3    # starts 8th
    if day == 12: return 3    # starts 15th
    if day == 19: return 3    # starts 22nd
    return 1


if __name__ == "__main__":
    main()
