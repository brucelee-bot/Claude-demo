"""
企业信息查询 — 通过公开搜索结果提取企业注册信息。
"""
import json, os, re, requests
from datetime import datetime

CACHE_FILE = os.path.join(os.path.dirname(__file__), "company_cache.json")


def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _clean_text(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[^;]+;", " ", text)
    return re.sub(r"\s+", " ", text)


def _search_text(company_name: str) -> str:
    queries = [
        f"site:qcc.com {company_name} 经营范围 注册资本 成立日期",
        f"企查查 {company_name} 经营范围 注册资本 成立日期",
        f"{company_name} 经营范围 注册资本 成立日期 国家高新技术企业",
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    chunks = []
    for query in queries:
        try:
            url = f"https://www.bing.com/search?q={requests.utils.quote(query)}"
            r = requests.get(url, headers=headers, timeout=8)
            r.encoding = "utf-8"
            chunks.append(_clean_text(r.text))
        except Exception:
            continue
    return " ".join(chunks)


def _extract_year(text: str) -> int | None:
    patterns = [
        r"成立日期[：:\s]*(\d{4})[年\-/]",
        r"成立时间[：:\s]*(\d{4})[年\-/]",
        r"成立于?\s*(\d{4})",
        r"(\d{4})年[^，。；;]{0,20}成立",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            year = int(m.group(1))
            if 1949 <= year <= datetime.now().year:
                return year
    return None


def _extract_capital(text: str) -> str:
    patterns = [
        r"注册资本[：:\s]*([0-9,.]+)\s*万人民币",
        r"注册资本[：:\s]*([0-9,.]+)\s*万元人民币",
        r"注册资本[：:\s]*人民币\s*([0-9,.]+)\s*万元",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).replace(",", "")
    return ""


def _extract_scope(text: str) -> str:
    m = re.search(r"经营范围[：:\s]*([^。]{20,500}?)(?:登记机关|股东信息|主要人员|许可项目|一般项目|$)", text)
    if not m:
        return ""
    scope = m.group(1).strip(" ：:;；，,")
    scope = re.sub(r"企业依法自主选择经营项目.*", "", scope)
    return scope[:500].strip()


def _extract_region(text: str) -> tuple[str, str]:
    provinces = "北京|天津|上海|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆|香港|澳门"
    m = re.search(rf"({provinces})(?:省|市|自治区|壮族自治区|回族自治区|维吾尔自治区)?\s*([一-鿿]{{2,12}}?市|[一-鿿]{{2,12}}?区|[一-鿿]{{2,12}}?县)", text)
    if m:
        province = m.group(1)
        city = m.group(2)
        return province, city
    return "", ""


def _extract_hitech(text: str) -> str:
    if "国家高新技术企业" in text or "高新技术企业" in text:
        return "yes"
    return ""


def lookup(company_name: str) -> dict:
    name = company_name.strip()
    if not name:
        return {"success": False, "name": name, "error": "企业名称为空"}

    cache = _load_cache()
    if name in cache:
        entry = cache[name]
        year = entry.get("established_year")
        if year:
            entry["market_years"] = datetime.now().year - int(year)
        return {"success": True, "name": name, **entry, "source": "cache"}

    text = _search_text(name)
    year = _extract_year(text)
    province, city = _extract_region(text)
    result = {
        "success": True,
        "name": name,
        "established_year": year or "",
        "market_years": datetime.now().year - year if year else "",
        "registered_capital": _extract_capital(text),
        "business_scope": _extract_scope(text),
        "is_hitech": _extract_hitech(text),
        "province": province,
        "city": city,
        "source": "web",
        "updated_at": datetime.now().isoformat(),
    }
    found = any(result.get(k) for k in ["established_year", "registered_capital", "business_scope", "is_hitech", "province", "city"])
    if not found:
        return {"success": False, "name": name, "error": "未查询到可自动填写的企业信息，请手动填写"}

    cache[name] = {k: v for k, v in result.items() if k not in ["success", "name", "source"]}
    _save_cache(cache)
    return result
