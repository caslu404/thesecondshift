import os
import re
import time
from dataclasses import dataclass
from shutil import which
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup
from flask import Flask, request, render_template_string
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


# =========================
# CONFIG
# =========================
TAG_A = "01l98f-20"
TAG_B = "xiwhd-20"
MAX_ASINS = 30

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

ASIN_RE = re.compile(r"\b(B[0-9A-Z]{9})\b", re.I)


# =========================
# UTILS
# =========================
def extrair_asins_do_texto(texto: str) -> List[str]:
    encontrados = ASIN_RE.findall(texto or "")
    return list(dict.fromkeys([x.upper() for x in encontrados]).keys())


def _limpar_espacos(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _padronizar_rs(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.replace("\xa0", " ")
    s = _limpar_espacos(s)
    s = re.sub(r"\bR\$\s*", "R$ ", s)
    return _limpar_espacos(s)


def _extrair_float_brl(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    s = s.replace("\xa0", " ")
    m = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})", s)
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        return float(num)
    except:
        return None


def _pegar_texto(soup: BeautifulSoup, selector: str) -> Optional[str]:
    el = soup.select_one(selector)
    if not el:
        return None
    return _limpar_espacos(el.get_text(" ", strip=True))


def _dedupe_preserve(seq: List[str]) -> List[str]:
    out, seen = [], set()
    for x in seq:
        if x and x not in seen:
            out.append(x)
            seen.add(x)
    return out


# =========================
# PARSERS (DOM)
# =========================
def _extract_title(soup: BeautifulSoup) -> str:
    return (
        _pegar_texto(soup, "#productTitle")
        or _pegar_texto(soup, "h1#title span#productTitle")
        or _pegar_texto(soup, "h1 span")
        or "NOME NÃO ENCONTRADO"
    )


def _extract_price_to_pay_total(soup: BeautifulSoup) -> Optional[str]:
    selectors = [
        "div.a-section.apex-core-price-identifier span.a-price.priceToPay.apex-pricetopay-value span.a-offscreen",
        "span.a-price.priceToPay.apex-pricetopay-value span.a-offscreen",
        "#corePriceDisplay_desktop_feature_div span.a-price.priceToPay span.a-offscreen",
        "#corePrice_feature_div span.a-price.priceToPay span.a-offscreen",
        "#corePriceDisplay_mobile_feature_div span.a-price.priceToPay span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ]
    for sel in selectors:
        t = _pegar_texto(soup, sel)
        t = _padronizar_rs(t)
        if t and "R$" in t and _extrair_float_brl(t):
            return t

    for el in soup.select(
        "#corePrice_feature_div span.a-price span.a-offscreen, "
        "#corePriceDisplay_desktop_feature_div span.a-price span.a-offscreen"
    ):
        parent = el.parent
        if parent and parent.has_attr("class"):
            cls = " ".join(parent.get("class", []))
            if "apex-basisprice-value" in cls:
                continue
        t = _padronizar_rs(_limpar_espacos(el.get_text(" ", strip=True)))
        if t and "R$" in t and _extrair_float_brl(t):
            return t

    return None


def _extract_list_price_de(soup: BeautifulSoup) -> Optional[str]:
    selectors = [
        "span.a-price.a-text-price.apex-basisprice-value span.a-offscreen",
        "span.a-text-price.apex-basisprice-value span.a-offscreen",
        "span.basisPrice span.a-price.a-text-price span.a-offscreen",
    ]
    for sel in selectors:
        t = _padronizar_rs(_pegar_texto(soup, sel))
        if t and "R$" in t and _extrair_float_brl(t):
            return t
    return None


def _extract_savings_percent_dom(soup: BeautifulSoup) -> Optional[int]:
    el = soup.select_one("span.apex-savings-percentage")
    if not el:
        return None
    txt = _limpar_espacos(el.get_text(" ", strip=True))
    m = re.search(r"(-?\d{1,3})\s*%", txt)
    if not m:
        return None
    try:
        return abs(int(m.group(1)))
    except:
        return None


def _extract_installments(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    bloco = soup.find("div", id="installmentCalculator_feature_div")
    if bloco:
        offer = bloco.find("span", id=re.compile(r"^best-offer-string"))
        if offer:
            t = _limpar_espacos(offer.get_text(" ", strip=True).replace("\xa0", " "))
            m = re.search(
                r"ou\s*(R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}).*?em até\s*(\d{1,2})x\s*de\s*"
                r"(R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}).*?sem juros",
                t,
                flags=re.I,
            )
            if m:
                total = _padronizar_rs(m.group(1))
                n = int(m.group(2))
                parcela = _padronizar_rs(m.group(3))
                return total, n, parcela

    full = soup.get_text(" ", strip=True).replace("\xa0", " ")
    full = re.sub(r"\s+", " ", full)
    m2 = re.search(
        r"ou\s*(R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}).{0,80}?em até\s*(\d{1,2})x\s*"
        r"(?:de\s*)?(R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}).{0,20}?sem juros",
        full,
        flags=re.I,
    )
    if m2:
        total = _padronizar_rs(m2.group(1))
        n = int(m2.group(2))
        parcela = _padronizar_rs(m2.group(3))
        return total, n, parcela

    return None, None, None


def _extract_pix(soup: BeautifulSoup) -> Tuple[Optional[str], Optional[int]]:
    full_text = soup.get_text(" ", strip=True).replace("\xa0", " ")
    pix_price = None

    m = re.search(r"(R\$\s*\d{1,3}(?:\.\d{3})*,\d{2}).{0,40}à vista no Pix", full_text, flags=re.I)
    if m:
        pix_price = _padronizar_rs(m.group(1))

    pix_pct = None
    m2 = re.search(r"(\d{1,2})\s*%\s*off\s*no\s*pix", full_text, flags=re.I)
    if m2:
        try:
            pix_pct = int(m2.group(1))
        except:
            pix_pct = None

    return pix_price, pix_pct


def _extract_coupons(soup: BeautifulSoup) -> List[str]:
    cupons = []

    badges = soup.find_all("label", id=re.compile(r"greenBadge"))
    for badge in badges:
        label_txt = _limpar_espacos(badge.get_text(" ", strip=True))
        parent = badge.parent
        msg_span = parent.find("span", id=re.compile(r"promoMessage")) if parent else None
        regra_txt = _limpar_espacos(msg_span.get_text(" ", strip=True)) if msg_span else ""
        full_txt = _limpar_espacos(f"{label_txt} {regra_txt}".strip())
        full_txt = re.sub(r"Ver itens participantes.*", "", full_txt, flags=re.I).strip()
        full_txt = re.sub(r"Comprar itens elegíveis.*", "", full_txt, flags=re.I).strip()
        full_txt = _limpar_espacos(full_txt)
        if full_txt:
            cupons.append(full_txt)

    for sp in soup.find_all("span", {"class": "promoPriceBlockMessage"}):
        t = _limpar_espacos(sp.get_text(" ", strip=True))
        t = re.sub(r"Ver itens participantes.*", "", t, flags=re.I).strip()
        t = re.sub(r"Comprar itens elegíveis.*", "", t, flags=re.I).strip()
        t = _limpar_espacos(t)
        if t:
            cupons.append(t)

    return _dedupe_preserve(cupons)


# =========================
# DISCOUNT + CONFIDENCE
# =========================
def _calc_discount_percent(de_str: Optional[str], por_str: Optional[str], dom_pct: Optional[int]) -> Optional[int]:
    de_v = _extrair_float_brl(de_str)
    por_v = _extrair_float_brl(por_str)

    if de_v is None or por_v is None or de_v <= 0 or por_v <= 0 or por_v >= de_v:
        return dom_pct

    pct = round((1 - (por_v / de_v)) * 100)
    if pct < 1 or pct > 85:
        if dom_pct and 1 <= dom_pct <= 85:
            return dom_pct
        return None
    return pct


def _calc_extra_pix_percent(por_str: Optional[str], pix_str: Optional[str]) -> Optional[int]:
    por_v = _extrair_float_brl(por_str)
    pix_v = _extrair_float_brl(pix_str)
    if por_v is None or pix_v is None or por_v <= 0 or pix_v <= 0 or pix_v >= por_v:
        return None
    pct = round((1 - (pix_v / por_v)) * 100)
    if 1 <= pct <= 25:
        return pct
    return None


def _confidence_score(
    title_ok: bool,
    price_ok: bool,
    de_ok: bool,
    pct_ok: bool,
    has_installments: bool,
    installment_total: Optional[str],
    installment_value: Optional[str],
    total_price: Optional[str],
) -> int:
    score = 100
    if not title_ok:
        score -= 35
    if not price_ok:
        score -= 45
    if not de_ok:
        score -= 10
    if de_ok and price_ok and not pct_ok:
        score -= 10

    if has_installments:
        if not installment_total:
            score -= 25
        inst_val = _extrair_float_brl(installment_value)
        inst_total = _extrair_float_brl(installment_total)
        tot = _extrair_float_brl(total_price)

        if inst_val is not None and tot is not None and inst_val >= tot:
            score -= 45
        if inst_total is not None and tot is not None and inst_total < tot * 0.6:
            score -= 20

    return max(0, min(100, score))


def _confidence_class(score: int) -> str:
    if score >= 90:
        return "good"
    if score >= 70:
        return "mid"
    return "bad"


def _build_block(
    asin: str,
    tag: str,
    title: str,
    de_str: Optional[str],
    por_total: Optional[str],
    pct: Optional[int],
    n_parc: Optional[int],
    val_parc: Optional[str],
    pix_price: Optional[str],
    pix_pct: Optional[int],
    pix_extra_pct: Optional[int],
    coupons: List[str],
) -> str:
    linhas = []
    linhas.append(f"*{title}*")
    linhas.append("")

    if de_str:
        linhas.append(f"DE: ~{de_str}~")

    por_line = "POR: *"
    if por_total:
        por_line += por_total
        if pct is not None:
            por_line += f" ({pct}% OFF)"
        por_line += "*"
    else:
        por_line += "Sem preço disponível no momento*"

    if n_parc and val_parc:
        por_line += f" em até {n_parc}x de {val_parc} sem juros"
    linhas.append(por_line)

    if pix_price:
        pix_line = f"ou {pix_price} à vista no Pix"
        if pix_pct is not None:
            pix_line += f" ({pix_pct}% OFF no Pix)"
        elif pix_extra_pct is not None:
            pix_line += f" ({pix_extra_pct}% OFF no Pix)"
        linhas.append(pix_line)

    if coupons:
        linhas.append("")
        for c in coupons:
            linhas.append(c)

    linhas.append("")
    linhas.append(f"https://www.amazon.com.br/dp/{asin}?tag={tag}")

    return "\n".join(linhas)


# =========================
# SELENIUM
# =========================
@dataclass
class ScrapeResult:
    asin: str
    ok: bool
    title: str
    de_str: Optional[str]
    por_total: Optional[str]
    pct: Optional[int]
    inst_total: Optional[str]
    n_parc: Optional[int]
    val_parc: Optional[str]
    pix_price: Optional[str]
    pix_pct: Optional[int]
    coupons: List[str]
    confidence: int
    confidence_class: str
    elapsed: float
    error: Optional[str]
    block: str


def _apply_fast_rules(driver: webdriver.Chrome) -> None:
    # Bloqueia assets pesados (mas mantém JS) para o DOM/strings continuarem vindo.
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd(
            "Network.setBlockedURLs",
            {
                "urls": [
                    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.svg", "*.ico",
                    "*.css",
                    "*.woff", "*.woff2", "*.ttf", "*.otf",
                    "*.mp4", "*.m4v", "*.webm",
                ]
            },
        )
    except Exception:
        pass


def _make_driver() -> webdriver.Chrome:
    opts = webdriver.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1200,1400")
    opts.add_argument(f"user-agent={UA}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-sync")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-features=Translate,BackForwardCache,AcceptCHFrame")

    # CHAVE: não espera load completo
    opts.page_load_strategy = "none"

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
    }
    opts.add_experimental_option("prefs", prefs)

    chrome_bin = (
        os.environ.get("CHROME_BIN")
        or which("chromium")
        or which("chromium-browser")
        or which("google-chrome")
    )
    if chrome_bin:
        opts.binary_location = chrome_bin

    driver_path = os.environ.get("CHROMEDRIVER_PATH") or which("chromedriver")
    if not driver_path:
        raise RuntimeError("chromedriver não encontrado no ambiente. Verifique o Dockerfile.")

    driver = webdriver.Chrome(service=Service(driver_path), options=opts)

    try:
        driver.set_page_load_timeout(18)
    except Exception:
        pass

    _apply_fast_rules(driver)
    return driver


def _close_popups_if_any(driver: webdriver.Chrome):
    try:
        btns = driver.find_elements(By.XPATH, "//button[contains(., 'Continuar comprando')]")
        if btns:
            btns[0].click()
            time.sleep(0.10)
    except:
        pass


def scrapar_asin(driver: webdriver.Chrome, asin: str, tag: str) -> ScrapeResult:
    t0 = time.time()
    url = f"https://www.amazon.com.br/dp/{asin}"

    try:
        try:
            driver.get(url)
        except TimeoutException:
            # com page_load_strategy=none isso pode acontecer menos, mas se acontecer, seguimos
            pass

        _close_popups_if_any(driver)

        # Espera só pelo que importa
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "productTitle"))
            )
        except:
            pass

        # Corta o resto do carregamento imediatamente
        try:
            driver.execute_script("window.stop();")
        except:
            pass

        # micro sleep só pra estabilizar page_source
        time.sleep(0.05)

        html = driver.page_source
        soup = BeautifulSoup(html, "html.parser")

        title = _extract_title(soup)
        de_str = _padronizar_rs(_extract_list_price_de(soup))

        inst_total, n_parc, val_parc = _extract_installments(soup)
        inst_total = _padronizar_rs(inst_total)
        val_parc = _padronizar_rs(val_parc)

        price_to_pay = _padronizar_rs(_extract_price_to_pay_total(soup))
        por_total = inst_total or price_to_pay

        dom_pct = _extract_savings_percent_dom(soup)

        pix_price, pix_pct = _extract_pix(soup)
        pix_price = _padronizar_rs(pix_price)

        coupons = _extract_coupons(soup)

        title_ok = bool(title and "NÃO ENCONTRADO" not in title)
        price_ok = bool(por_total and _extrair_float_brl(por_total))
        de_ok = bool(de_str and _extrair_float_brl(de_str))

        pct = _calc_discount_percent(de_str, por_total, dom_pct)
        pct_ok = pct is not None

        has_installments = bool(n_parc and val_parc)

        confidence = _confidence_score(
            title_ok=title_ok,
            price_ok=price_ok,
            de_ok=de_ok,
            pct_ok=pct_ok,
            has_installments=has_installments,
            installment_total=inst_total,
            installment_value=val_parc,
            total_price=por_total,
        )
        conf_class = _confidence_class(confidence)

        pix_extra_pct = None
        if pix_price and pix_pct is None:
            pix_extra_pct = _calc_extra_pix_percent(por_total, pix_price)

        ok = bool(title_ok and price_ok)

        block = (
            _build_block(
                asin=asin,
                tag=tag,
                title=title,
                de_str=de_str,
                por_total=por_total,
                pct=pct,
                n_parc=n_parc,
                val_parc=val_parc,
                pix_price=pix_price,
                pix_pct=pix_pct,
                pix_extra_pct=pix_extra_pct,
                coupons=coupons,
            )
            if ok
            else ""
        )

        err = None if ok else "Sem dados suficientes (título ou preço principal não encontrado)."

        return ScrapeResult(
            asin=asin,
            ok=ok,
            title=title,
            de_str=de_str,
            por_total=por_total,
            pct=pct,
            inst_total=inst_total,
            n_parc=n_parc,
            val_parc=val_parc,
            pix_price=pix_price,
            pix_pct=pix_pct,
            coupons=coupons,
            confidence=confidence,
            confidence_class=conf_class,
            elapsed=time.time() - t0,
            error=err,
            block=block,
        )

    except Exception as e:
        return ScrapeResult(
            asin=asin,
            ok=False,
            title="(erro ao carregar)",
            de_str=None,
            por_total=None,
            pct=None,
            inst_total=None,
            n_parc=None,
            val_parc=None,
            pix_price=None,
            pix_pct=None,
            coupons=[],
            confidence=0,
            confidence_class="bad",
            elapsed=time.time() - t0,
            error=str(e),
            block="",
        )


# =========================
# FLASK UI
# =========================
app = Flask(__name__)

TEMPLATE = r"""
<!doctype html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PromoTagger</title>
  <style>
    :root{
      --bg1:#fff7ed; --bg2:#ffedd5;
      --card:rgba(255,255,255,0.78);
      --cardBorder:rgba(15,23,42,0.12);
      --text:#0f172a; --muted:#475569;
      --accent:#f97316; --accent2:#fb923c; --accentDark:#c2410c;
      --shadow:0 10px 30px rgba(2,6,23,0.10);
      --radius:16px;
    }
    body{
      margin:0; font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      color:var(--text);
      background:radial-gradient(1200px 800px at 20% 10%, var(--bg2), transparent 60%),
                 linear-gradient(135deg, var(--bg1), #ffffff);
      min-height:100vh;
    }
    .container{ max-width:1180px; margin:0 auto; padding:18px 14px 34px; }
    .topbar{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; flex-wrap:wrap; }
    .title{ font-size:28px; font-weight:800; letter-spacing:-0.02em; margin:0; }
    .panel{ background:var(--card); border:1px solid var(--cardBorder); border-radius:var(--radius); box-shadow:var(--shadow); padding:14px; }
    textarea{
      width:100%; min-height:170px; box-sizing:border-box;
      border-radius:14px; border:1px solid rgba(15,23,42,0.14);
      background:rgba(255,255,255,0.92); color:var(--text);
      padding:12px; font-size:13px; resize:vertical; outline:none; margin-top:10px;
      font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;
    }
    textarea:focus{ border-color:rgba(249,115,22,0.55); box-shadow:0 0 0 4px rgba(249,115,22,0.12); }
    .actions{ display:flex; justify-content:flex-end; margin-top:10px; }
    .btn{ border:none; cursor:pointer; border-radius:999px; padding:10px 16px; font-weight:900; font-size:13px; transition:transform .06s ease, filter .15s ease; }
    .btn:active{ transform:translateY(1px); }
    .btnPrimary{ background:linear-gradient(135deg, var(--accent), var(--accent2)); color:#fff; box-shadow:0 10px 20px rgba(249,115,22,0.18); }
    .btnPrimary:hover{ filter:brightness(0.98); }

    .toggleWrap{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
    .toggle{
      width:min(360px, 92vw); height:40px;
      background:rgba(15,23,42,0.06);
      border:1px solid rgba(15,23,42,0.10);
      border-radius:999px; position:relative; cursor:pointer; user-select:none;
      display:flex; align-items:center; padding:3px;
    }
    .toggleKnob{
      position:absolute; top:3px; left:3px;
      width:calc(50% - 3px); height:calc(100% - 6px);
      border-radius:999px;
      background:linear-gradient(135deg, var(--accent), var(--accent2));
      box-shadow:0 8px 18px rgba(0,0,0,0.10);
      transition:transform 0.25s ease;
    }
    .toggle.isB .toggleKnob{ transform:translateX(100%); }
    .toggle span{
      width:50%; text-align:center; font-size:12px; font-weight:900;
      color:rgba(15,23,42,0.55); z-index:2;
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
      padding:0 10px;
    }
    .toggle .active{ color:rgba(15,23,42,0.95); }

    .results{ margin-top:16px; display:flex; flex-direction:column; gap:12px; }
    .card{
      background:rgba(255,255,255,0.86);
      border:1px solid rgba(15,23,42,0.10);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      padding:12px; overflow:hidden;
    }
    .cardTop{ display:flex; align-items:flex-start; justify-content:space-between; gap:10px; flex-wrap:wrap; }
    .asinLine{ font-size:12px; color:var(--muted); font-weight:700; }
    .productLine{
      font-size:14px; font-weight:800; color:var(--text);
      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:680px;
    }
    @media (max-width:980px){ .productLine{ max-width:100%; } }

    .rightTools{ display:flex; align-items:center; gap:8px; flex-shrink:0; }

    .conf{
      font-size:11px; font-weight:950; padding:6px 10px; border-radius:999px;
      border:1px solid rgba(15,23,42,0.10);
    }
    .conf.bad{ background:rgba(239,68,68,0.12); color:#991b1b; }
    .conf.mid{ background:rgba(245,158,11,0.14); color:#92400e; }
    .conf.good{ background:rgba(34,197,94,0.14); color:#166534; }

    .iconBtn{
      width:38px; height:38px; border-radius:999px;
      border:1px solid rgba(15,23,42,0.14);
      background:rgba(255,255,255,0.85);
      display:inline-flex; align-items:center; justify-content:center;
      cursor:pointer; transition:filter 0.15s ease;
    }
    .iconBtn:hover{ filter:brightness(0.98); }

    .copyBtn{
      border:none; border-radius:999px; padding:9px 12px;
      font-weight:950; font-size:12px; cursor:pointer; color:#fff;
      background:linear-gradient(135deg, var(--accentDark), var(--accent));
      box-shadow:0 10px 20px rgba(2,6,23,0.08);
    }

    .grid{ display:grid; grid-template-columns:1fr; gap:12px; margin-top:12px; }

    .codeOut{
      width:100%; min-height:130px; box-sizing:border-box;
      border-radius:14px; border:1px solid rgba(15,23,42,0.14);
      background:rgba(255,255,255,0.92); color:var(--text);
      padding:10px; font-size:12px; resize:vertical; outline:none;
      font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;
    }
    .errorLine{ margin-top:8px; font-size:12px; color:#b91c1c; font-weight:900; }

    .loading{
      position:fixed; inset:0;
      background:rgba(255,255,255,0.72);
      backdrop-filter:blur(6px);
      display:none; align-items:center; justify-content:center;
      z-index:9999;
    }
    .loading.show{ display:flex; }
    .loadingBox{
      width:320px;
      background:rgba(255,255,255,0.90);
      border:1px solid rgba(15,23,42,0.10);
      border-radius:18px;
      box-shadow:var(--shadow);
      padding:18px;
      display:flex;
      flex-direction:column;
      gap:14px;
      align-items:center;
    }
    .loadingTitle{ font-weight:950; font-size:14px; }
    .spinner{
      width:34px;
      height:34px;
      border-radius:999px;
      border:4px solid rgba(15,23,42,0.14);
      border-top-color:var(--accent);
      animation:spin 0.9s linear infinite;
    }
    @keyframes spin{ to{ transform:rotate(360deg); } }
  </style>
</head>
<body>
  <div id="loading" class="loading">
    <div class="loadingBox">
      <div class="loadingTitle">Gerando Links...</div>
      <div class="spinner" aria-hidden="true"></div>
    </div>
  </div>

  <div class="container">
    <div class="topbar">
      <h1 class="title">The Second Shift</h1>

      <div class="toggleWrap">
        <div id="tagToggle" class="toggle {% if selected_tag == tag_b %}isB{% endif %}" role="button" tabindex="0">
          <div class="toggleKnob"></div>
          <span id="lblA" class="{% if selected_tag == tag_a %}active{% endif %}">{{ tag_a }}</span>
          <span id="lblB" class="{% if selected_tag == tag_b %}active{% endif %}">{{ tag_b }}</span>
        </div>
      </div>
    </div>

    <div class="panel">
      <form id="formMain" method="post" action="/">
        <input type="hidden" name="selected_tag" id="selected_tag" value="{{ selected_tag }}">
        <textarea name="input_text" placeholder="Cole aqui um texto com ASINs (máx {{ max_asins }} por vez)">{{ input_text or "" }}</textarea>
        <div class="actions">
          <button class="btn btnPrimary" type="submit">Gerar</button>
        </div>
      </form>
    </div>

    {% if asins %}
      <div style="margin-top:10px; font-size:12px; color: var(--muted); font-weight:900;">
        Encontrados {{ asins|length }} ASINs {% if truncated %}(limitado a {{ max_asins }}){% endif %}: {{ ", ".join(asins) }}
      </div>
    {% endif %}

    {% if resultados %}
      <div class="results">
        {% for r in resultados %}
          <div class="card">
            <div class="cardTop">
              <div>
                <div class="asinLine">ASIN {{ r.asin }}</div>
                <div class="productLine" title="{{ r.title }}">{{ r.title }}</div>
              </div>

              <div class="rightTools">
                <div class="conf {{ r.confidence_class }}">Confiança: {{ r.confidence }}</div>

                {% if r.ok %}
                  <button class="copyBtn" type="button" onclick="copyBlock('blk-{{ loop.index0 }}')">Copiar</button>

                  <a class="iconBtn" href="https://www.amazon.com.br/dp/{{ r.asin }}?tag={{ selected_tag }}" target="_blank" title="Abrir" aria-label="Abrir">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <path d="M14 3h7v7" stroke="rgba(15,23,42,0.78)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                      <path d="M21 3l-9 9" stroke="rgba(15,23,42,0.78)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                      <path d="M10 7H7a4 4 0 0 0-4 4v6a4 4 0 0 0 4 4h6a4 4 0 0 0 4-4v-3" stroke="rgba(15,23,42,0.78)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                  </a>
                {% endif %}
              </div>
            </div>

            <div class="grid">
              <div>
                {% if r.ok %}
                  <textarea readonly id="blk-{{ loop.index0 }}" class="codeOut">{{ r.block }}</textarea>
                {% else %}
                  <div class="errorLine">{{ r.error or "Sem dados suficientes." }}</div>
                {% endif %}
                <div style="margin-top:8px; font-size:11px; color: rgba(15,23,42,0.55); font-weight:900;">
                  {{ "%.2f"|format(r.elapsed) }}s
                </div>
              </div>
            </div>
          </div>
        {% endfor %}
      </div>
    {% endif %}
  </div>

  <script>
    const TAG_A = "{{ tag_a }}";
    const TAG_B = "{{ tag_b }}";

    function applyTheme(tag){
      const root = document.documentElement;
      if(tag === TAG_B){
        root.style.setProperty('--bg1', '#fff1f2');
        root.style.setProperty('--bg2', '#ffe4e6');
        root.style.setProperty('--accent', '#ec4899');
        root.style.setProperty('--accent2', '#fb7185');
        root.style.setProperty('--accentDark', '#be185d');
      }else{
        root.style.setProperty('--bg1', '#fff7ed');
        root.style.setProperty('--bg2', '#ffedd5');
        root.style.setProperty('--accent', '#f97316');
        root.style.setProperty('--accent2', '#fb923c');
        root.style.setProperty('--accentDark', '#c2410c');
      }
    }

    function setToggleUI(tag){
      const toggle = document.getElementById('tagToggle');
      const lblA = document.getElementById('lblA');
      const lblB = document.getElementById('lblB');

      if(tag === TAG_B){
        toggle.classList.add('isB');
        lblA.classList.remove('active');
        lblB.classList.add('active');
      }else{
        toggle.classList.remove('isB');
        lblB.classList.remove('active');
        lblA.classList.add('active');
      }
      document.getElementById('selected_tag').value = tag;
      applyTheme(tag);
    }

    applyTheme(document.getElementById('selected_tag').value);

    document.getElementById('tagToggle').addEventListener('click', () => {
      const cur = document.getElementById('selected_tag').value;
      const next = (cur === TAG_A) ? TAG_B : TAG_A;
      setToggleUI(next);
    });

    document.getElementById('formMain').addEventListener('submit', function(){
      document.getElementById('loading').classList.add('show');
    });

    function copyBlock(id){
      const el = document.getElementById(id);
      if(!el) return;
      el.select();
      el.setSelectionRange(0, 999999);
      navigator.clipboard.writeText(el.value);
    }
  </script>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    input_text = ""
    resultados: List[ScrapeResult] = []
    asins: List[str] = []
    truncated = False

    selected_tag = TAG_A

    if request.method == "POST":
        input_text = request.form.get("input_text", "") or ""
        selected_tag = request.form.get("selected_tag", TAG_A) or TAG_A
        if selected_tag not in (TAG_A, TAG_B):
            selected_tag = TAG_A

        asins = extrair_asins_do_texto(input_text)
        if len(asins) > MAX_ASINS:
            asins = asins[:MAX_ASINS]
            truncated = True

        if asins:
            driver = _make_driver()
            try:
                for asin in asins:
                    resultados.append(scrapar_asin(driver, asin, selected_tag))
            finally:
                try:
                    driver.quit()
                except:
                    pass

    return render_template_string(
        TEMPLATE,
        input_text=input_text,
        resultados=resultados,
        asins=asins,
        truncated=truncated,
        max_asins=MAX_ASINS,
        selected_tag=selected_tag,
        tag_a=TAG_A,
        tag_b=TAG_B,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5010"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
