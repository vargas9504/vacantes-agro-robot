#!/usr/bin/env python3
"""Robot de vacantes agro: baja listados de portales públicos, filtra,
deduplica y publica solo las candidatas nuevas del día. Sin IA.

Uso:  python robot/barrido.py [--solo computrabajo,linkedin] [--datos data] [--docs docs]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import html
import json
import logging
import os
import re
import time
import traceback
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

RAIZ = Path(__file__).resolve().parent.parent
TZ_CO = timezone(timedelta(hours=-5), "America/Bogota")  # Colombia no tiene horario de verano
CAMPOS = ["id", "portal", "cargo", "empresa", "ciudad", "publicada", "salario", "url",
          "fuente_busqueda", "encontrada",
          "publicada_iso", "edad_dias", "cierre_iso", "vencida",
          "perfil_sugerido", "zona_preferida", "empresa_confidencial", "repost_probable", "via_descripcion"]

log = logging.getLogger("barrido")


# --------------------------------------------------------------------------- utilidades

def ahora_co() -> datetime:
    return datetime.now(TZ_CO)


def limpiar(txt) -> str:
    if txt is None:
        return ""
    return re.sub(r"\s+", " ", str(txt)).strip()


def sin_tildes(txt: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", txt) if unicodedata.category(c) != "Mn")


def normalizar(txt: str) -> str:
    t = sin_tildes(limpiar(txt).lower())
    t = re.sub(r"[^a-z0-9ñ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_SUFIJOS_EMPRESA = re.compile(r"\b(s ?a ?s|s ?a|ltda|limitada|sa|inc|e ?s ?p|y cia|cia|bic|zomac)\b")
_EMPRESA_VACIA = {"", "confidencial", "empresa confidencial", "importante empresa",
                  "empresa lider", "reconocida empresa", "empresa del sector", "anonimo"}


_CIUDAD_GENERICA = {"", "colombia", "remoto", "nacional", "teletrabajo"}


def ciudad_base(ciudad: str) -> str:
    """'Valledupar, Cesar, Colombia' -> 'valledupar'; 'Bogotá, D.C.' -> 'bogota'; 'Colombia' -> ''."""
    c = normalizar((ciudad or "").split(",")[0])
    c = re.sub(r"\b(area metropolitana de|alrededores|d c|distrito capital)\b", " ", c)
    c = re.sub(r"\s+", " ", c).strip()
    return "" if c in _CIUDAD_GENERICA else c


def clave_vacante(cargo: str, empresa: str, ciudad: str = "", salario: str = "") -> str:
    """Clave 'cargo|empresa@ciudad#salario' normalizada. Empresa confidencial -> '?'."""
    c = re.sub(r"/a\b|/o\b|/as\b|/os\b", "", (cargo or "").lower())
    c = normalizar(re.sub(r"(\s*\([^()]*\))+\s*$", "", c))
    c = re.sub(r"\b(en|para|de)? ?(yopal|villavicencio|casanare|meta|tolima|cesar|huila|bogota|colombia)\b", " ", c)
    c = re.sub(r"\s+", " ", c).strip()
    
    e = normalizar(empresa)
    e = re.sub(r"\bvia\s+[a-z0-9 ]+", " ", e)
    e = re.sub(r"\(?cliente\b.*?\)?", " ", e)
    e = re.sub(r"\s+", " ", _SUFIJOS_EMPRESA.sub(" ", e)).strip()
    
    agencias = ["manpower", "adecco", "acierta", "michael page", "zohorecruit", "gente util", "supernumerarios", "human one"]
    portales = ["elempleo", "kitempleo", "kit empleo", "jobleads"]
    if e in _EMPRESA_VACIA or "confidencial" in e or "reconocida empresa" in e or "importante empresa" in e or "otra empresa" in e or any(p in e for p in portales) or any(ag in e for ag in agencias):
        e = "?"
        
    k = f"{c}|{e}@{ciudad_base(ciudad)}"
    if e == "?" and re.search(r"\b(lider|jefe|coordinador|director|gerente|asesor)\b", c):
        k = f"{k}#{normalizar(salario or '')}"
    return k


def texto(el) -> str:
    return limpiar(el.get_text(" ", strip=True)) if el is not None else ""


MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
         "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}


def fecha_desde_texto(txt: str, hoy: datetime | None = None) -> datetime | None:
    """Interpreta 'Publicado 12 Sep 2026', '12/09/2026', '2026-09-12', 'hace 3 días', 'ayer'..."""
    hoy = hoy or ahora_co()
    t = sin_tildes(limpiar(txt).lower())
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=TZ_CO)
    m = re.search(r"(\d{1,2})\s*(?:de\s+)?([a-z]{3})[a-z]*\.?\s*(?:de\s+|,\s*)?(\d{4})", t)
    if m and m[2] in MESES:
        try:
            return datetime(int(m[3]), MESES[m[2]], int(m[1]), tzinfo=TZ_CO)
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        try:
            return datetime(int(m[3]), int(m[2]), int(m[1]), tzinfo=TZ_CO)
        except ValueError:
            pass
    if re.search(r"\b(hoy|hace (unos )?(segundos?|minutos?|horas?|\d+ ?(min|h)))", t):
        return hoy
    if "ayer" in t:
        return hoy - timedelta(days=1)
    m = re.search(r"hace (\d+|un|una) (dia|semana|mes)", t)
    if m:
        n = 1 if m[1] in ("un", "una") else int(m[1])
        dias = {"dia": 1, "semana": 7, "mes": 30}[m[2]]
        return hoy - timedelta(days=n * dias)
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-z]{3})[a-z]*\b", t)
    if m and m[2] in MESES:  # "12 de septiembre" sin año
        try:
            f = datetime(hoy.year, MESES[m[2]], int(m[1]), tzinfo=TZ_CO)
            return f.replace(year=hoy.year - 1) if f > hoy + timedelta(days=1) else f
        except ValueError:
            pass
    return None


# --------------------------------------------------------------------------- filtro

_GRAVES = str.maketrans("àèìòùÀÈÌÒÙ", "áéíóúÁÉÍÓÚ")


class Filtro:
    """X y P se evalúan sobre el texto con tildes graves corregidas ("Tecnòlogo" -> "Tecnólogo")."""

    def __init__(self, reglas: dict):
        f = re.IGNORECASE | re.UNICODE
        self.x = re.compile(limpiar(reglas["excluir"]), f)
        self.p = re.compile(limpiar(reglas["incluir"]), f)
        self.u = re.compile(limpiar(reglas.get("excluir_ubicacion") or r"(?!x)x"), f)
        self.x_emp = re.compile(limpiar(reglas.get("excluir_empresa") or r"(?!x)x"), f)
        self.x_cargo_emp = re.compile(limpiar(reglas.get("excluir_cargo_empresa") or r"(?!x)x"), f)

    @staticmethod
    def _busca(rx, txt: str) -> bool:
        """Con y sin tildes: "Prácticante" también cae en `practicant`."""
        t = (txt or "").translate(_GRAVES)
        return bool(rx.search(t) or rx.search(sin_tildes(t)))

    def excluido(self, cargo: str, empresa: str = "") -> bool:
        if self._busca(self.x, cargo):
            return True
        if empresa and self._busca(self.x_emp, empresa):
            return True
        # For palm/agro companies, exclude non-agro roles
        if empresa and re.search(r"(palma|agro|cenipalma|fedepalma|fedearroz|agrosavia)", empresa, re.I):
            if self._busca(self.x_cargo_emp, cargo):
                return True
        return False

    def incluido(self, cargo: str, tarjeta: str = "") -> bool:
        return self._busca(self.p, cargo) or self._busca(self.p, tarjeta)

    def ubicacion_excluida(self, ciudad: str) -> bool:
        return self._busca(self.u, ciudad)

    def pasa(self, cargo: str, tarjeta: str = "", ciudad: str = "", aplicar_p: bool = True, empresa: str = "") -> bool:
        if not cargo or self.excluido(cargo, empresa) or self.ubicacion_excluida(ciudad):
            return False
        return self.incluido(cargo, tarjeta) if aplicar_p else True


def cargar_config(ruta: Path | None = None) -> dict:
    with open(ruta or RAIZ / "robot" / "config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# --------------------------------------------------------------------------- HTTP

class Bloqueado(Exception):
    """El portal bloquea la IP (403, captcha, etc.)."""


class Http:
    """Sesión con UA de navegador, timeout, reintentos con backoff y ritmo por dominio."""

    def __init__(self, gen: dict, fuente: str, snapshots: Path | None = None):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": gen.get("user_agent"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-CO,es;q=0.9,en;q=0.6",
        })
        self.timeout = gen.get("timeout", 30)
        self.intervalo = gen.get("intervalo_dominio", 1.1)
        self.ultimo: dict[str, float] = {}
        self.fuente = fuente
        self.snapshots = snapshots
        self.n_snap = 0
        self.peticiones = 0

    def _esperar(self, dominio: str, intervalo: float):
        espera = self.ultimo.get(dominio, 0) + intervalo - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        self.ultimo[dominio] = time.monotonic()

    def pedir(self, metodo: str, url: str, *, intentos: int = 3, intervalo: float | None = None,
              reintentar: tuple = (500, 502, 503, 504), espera_429: float = 6, intentos_429: int = 3,
              **kw) -> requests.Response:
        dominio = urlparse(url).netloc
        intervalo = self.intervalo if intervalo is None else intervalo
        n429 = 0
        intento = 0
        while True:
            self._esperar(dominio, intervalo)
            self.peticiones += 1
            try:
                r = self.s.request(metodo, url, timeout=self.timeout, **kw)
            except requests.RequestException:
                intento += 1
                if intento >= intentos:
                    raise
                time.sleep(2 * 2 ** intento)
                continue
            self._snapshot(url, r)
            if r.status_code == 429:
                n429 += 1
                if n429 > intentos_429:
                    raise Bloqueado(f"HTTP 429 persistente en {dominio}")
                log.info("[%s] 429, espero %ss (%d/%d)", self.fuente, espera_429, n429, intentos_429)
                time.sleep(espera_429)
                continue
            if r.status_code in reintentar:
                intento += 1
                if intento < intentos:
                    time.sleep(2 * 2 ** intento)
                    continue
            if r.status_code in (401, 403, 247) or self._es_captcha(r):
                raise Bloqueado(f"HTTP {r.status_code} / captcha en {dominio}")
            r.raise_for_status()
            return r

    def get(self, url, **kw):
        return self.pedir("GET", url, **kw)

    def post(self, url, **kw):
        return self.pedir("POST", url, **kw)

    @staticmethod
    def _es_captcha(r: requests.Response) -> bool:
        if "text/html" not in r.headers.get("Content-Type", ""):
            return False
        cab = r.text[:6000].lower()
        return any(s in cab for s in ("cf-challenge", "challenge-platform", "hcaptcha", "g-recaptcha",
                                       "verify you are human", "just a moment...", "px-captcha",
                                       "request blocked", "access denied", "rbzns", "kramericaindustries", "radware"))

    def _snapshot(self, url: str, r: requests.Response):
        if not self.snapshots or self.n_snap >= 4:
            return
        self.n_snap += 1
        self.snapshots.mkdir(parents=True, exist_ok=True)
        nombre = self.snapshots / f"{self.fuente}-{self.n_snap}.html"
        nombre.write_text(f"<!-- {r.status_code} {url} -->\n{r.text}", encoding="utf-8")


# --------------------------------------------------------------------------- fuentes
# Cada fuente devuelve una lista de listados (dicts) con: id_local, cargo, empresa,
# ciudad, publicada, salario, url, fuente_busqueda, tarjeta. Los errores parciales
# (una URL que falla) se anotan en ctx["avisos"]; un error total se propaga.

def _aviso(ctx, msg):
    log.warning("[%s] %s", ctx["nombre"], msg)
    ctx["avisos"].append(msg[:300])


def _item(id_local, cargo, url, busqueda, empresa="", ciudad="", publicada="", salario="", tarjeta=""):
    return {"id_local": str(id_local), "cargo": limpiar(cargo), "empresa": limpiar(empresa),
            "ciudad": limpiar(ciudad), "publicada": limpiar(publicada), "salario": limpiar(salario),
            "url": url, "fuente_busqueda": busqueda, "tarjeta": limpiar(tarjeta)}


def _salario(txt: str) -> str:
    m = re.search(r"(\$\s?[\d.,]+(?:\s*(?:a|-|–)\s*\$?\s?[\d.,]+)?(?:\s*\(?(?:mensual|neto|bruto)[^)]*\)?)?"
                  r"|a convenir|salario a convenir|\d[\d.,]*\s*(?:millones|smmlv|smlv))", txt, re.I)
    return limpiar(m[0]) if m else ""


def _ciudad_colombia(txt: str) -> str:
    deps = (r"Casanare|Meta|Tolima|Cesar|Huila|Cundinamarca|Antioquia|Valle del Cauca|Santander|"
            r"Norte de Santander|Bolívar|Bolivar|Magdalena|Córdoba|Cordoba|Sucre|Atlántico|Atlantico|"
            r"Boyacá|Boyaca|Caldas|Risaralda|Quindío|Quindio|Nariño|Cauca|Arauca|Vichada|Guaviare|"
            r"Caquetá|Caqueta|Putumayo|La Guajira|Chocó|Choco|Bogotá(?:, D\.C\.)?|Bogota|Amazonas")
    m = re.search(r"([A-ZÁÉÍÓÚÑ][\wáéíóúñ .]{1,40}?,\s*(?:%s))\b" % deps, txt)
    if m:
        return limpiar(m[1])
    m = re.search(r"\b(%s)\b" % deps, txt)
    return m[1] if m else ""


# ---- A. Computrabajo

def urls_computrabajo(cfg: dict) -> list[str]:
    base = "https://co.computrabajo.com"
    urls = [f"{base}/trabajo-de-{s}?pubdate=7" for s in cfg.get("slugs_7dias", [])]
    urls += [f"{base}/trabajo-de-{s}-en-{z}?pubdate=15"
             for s in cfg.get("slugs_zona", []) for z in cfg.get("zonas", [])]
    urls += [f"{base}/trabajo-de-{s}" for s in cfg.get("slugs_sin_fecha", [])]
    urls += [urljoin(base, r) for r in cfg.get("rutas_extra", [])]
    return urls


def parse_computrabajo(html_txt: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out = []
    for art in sopa.find_all("article"):
        a = (art.select_one("h2 a.js-o-link[href]") or art.select_one("h2 a[href]")
             or art.find("a", href=re.compile(r"/ofertas-de-trabajo/")))
        if not a:
            continue
        href = urljoin("https://co.computrabajo.com", a["href"].split("#")[0])
        m = re.search(r"-([0-9A-Fa-f]{16,})(?:$|[/?#])", urlparse(href).path + "/")
        if not m:
            continue
        for basura in art.select(".tags, .opt_dots, .box_show_offer, .fx_none, .star"):
            basura.decompose()  # "Postulado", "Vista", menú, calificación "4,4"
        cargo = texto(a)
        emp_el = art.select_one("a[offer-grid-article-company-url]") or art.select_one("p.dFlex")
        ciu_el = next((p for p in art.select("p.fs16") if "dFlex" not in (p.get("class") or [])), None)
        sal_el = art.select_one(".i_salary")
        pub_el = art.select_one("p.fc_aux")
        full = texto(art)
        out.append(_item(m[1][:16].lower(), cargo, href, busqueda, texto(emp_el),
                         texto(ciu_el.select_one("span") or ciu_el) if ciu_el else _ciudad_colombia(full),
                         texto(pub_el), texto(sal_el.parent) if sal_el else "", full))
    return out


def fuente_computrabajo(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    urls = urls_computrabajo(cfg)
    items, fallos = [], 0
    for url in urls:
        busqueda = url.replace("https://co.computrabajo.com/", "")
        for pag in range(1, cfg.get("paginas_max", 3) + 1):
            u = url if pag == 1 else url + ("&" if "?" in url else "?") + f"p={pag}"
            try:
                r = http.get(u)
            except Bloqueado:
                raise
            except Exception as e:
                fallos += 1
                _aviso(ctx, f"{u}: {e}")
                break
            nuevos = parse_computrabajo(r.text, busqueda)
            items += nuevos
            if len(nuevos) < 20:
                break
    if fallos == len(urls):
        raise RuntimeError("todas las URLs fallaron")
    return items


# ---- B. elempleo

def parse_elempleo(html_txt: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron = re.compile(r"/co/ofertas-trabajo/([^/?#]+?)-(\d{5,})")
    for card in sopa.select(".result-item"):
        a = card.find("a", href=patron)
        if not a:
            continue
        oid = patron.search(a["href"])[2]
        if oid in vistos:
            continue
        vistos.add(oid)
        try:
            d = json.loads(card.select_one("[data-ga4-offerdata]")["data-ga4-offerdata"])
        except (TypeError, ValueError, KeyError):
            d = {}
        pub = texto(card.select_one(".js-offer-date, .info-publish-date"))
        out.append(_item(oid, d.get("title") or limpiar(a.get("title")) or texto(a),
                         urljoin("https://www.elempleo.com", a["href"].split("?")[0]), busqueda,
                         d.get("company") or texto(card.select_one(".js-offer-company")),
                         d.get("location") or texto(card.select_one(".js-offer-city")), pub,
                         d.get("salary", ""), f"{texto(card)} {d.get('equivalentPositions', '')} {d.get('tags', '')}"))
    return out


def _contenedor(a, ids_en, max_sube: int = 8):
    """Sube desde el enlace hasta el mayor contenedor que solo tiene esa oferta."""
    el = a
    for _ in range(max_sube):
        p = el.parent
        if p is None or p.name in ("body", "html", "[document]") or len(ids_en(p)) > 1:
            break
        el = p
    return el


def detalle_elempleo(ctx, item: dict) -> bool:
    """Abre el detalle: fecha de publicación, salario, ciudad. False = descartar (vieja)."""
    f = fecha_desde_texto(item["publicada"]) if item["publicada"] else None
    if f and (ahora_co() - f).days > ctx["cfg"].get("max_dias", 30) + 7:
        return False  # el listado ya dice "hace 1 mes": no vale la pena abrir el detalle
    r = ctx["http"].get(item["url"])
    sopa = BeautifulSoup(r.text, "html.parser")
    full = texto(sopa.body or sopa)
    m = re.search(r"Publicad[oa]\s*(?:el|:)?\s*(\d{1,2}\s+[A-Za-zé]{3,}\.?\s+\d{4}|hace [^.|]{1,20}|hoy|ayer)", full, re.I)
    if m:
        item["publicada"] = limpiar(m[1])
    for oculto in sopa.select(".hide, .hidden, script, style"):
        oculto.decompose()
    for sel, campo in ((".js-joboffer-salary, [class*=salary]", "salario"),
                       (".js-joboffer-city, [class*=city]", "ciudad")):
        el = sopa.select_one(sel)
        if el and texto(el) and (not item[campo] or re.search(r"confidencial", item[campo], re.I)):
            item[campo] = texto(el)[:120]
    if not item["salario"]:
        item["salario"] = _salario(full)
    if not item["ciudad"]:
        item["ciudad"] = _ciudad_colombia(full)
    f = fecha_desde_texto(item["publicada"]) if item["publicada"] else None
    if f and (ahora_co() - f).days > ctx["cfg"].get("max_dias", 30):
        return False
    return True


def fuente_elempleo(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    items, fallos = [], 0
    for slug in cfg.get("slugs", []):
        url = f"https://www.elempleo.com/co/ofertas-empleo/trabajo-{slug}"
        try:
            r = http.get(url)
        except Bloqueado:
            raise
        except Exception as e:
            fallos += 1
            _aviso(ctx, f"{url}: {e}")
            continue
        items += parse_elempleo(r.text, f"trabajo-{slug}")
    if fallos == len(cfg.get("slugs", [])):
        raise RuntimeError("todas las URLs fallaron")
    return items


# ---- C. LinkedIn (API pública de invitados)

def parse_linkedin(html_txt: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out = []
    for card in sopa.find_all(attrs={"data-entity-urn": re.compile(r"jobPosting:\d+")}):
        jid = re.search(r"jobPosting:(\d+)", card["data-entity-urn"])[1]
        li = card.find_parent("li") or card
        t = li.find("time")
        pub = (t.get("datetime") or texto(t)) if t else ""
        out.append(_item(jid, texto(li.find("h3")), f"https://www.linkedin.com/jobs/view/{jid}",
                         busqueda, texto(li.find("h4")),
                         texto(li.select_one(".job-search-card__location")), pub,
                         texto(li.select_one(".job-search-card__salary-info")), texto(li)))
    return out


def fuente_linkedin(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    busquedas = [(t, "Colombia") for t in cfg.get("terminos_colombia", [])]
    busquedas += [(t, r) for t in cfg.get("terminos_region", []) for r in cfg.get("regiones", [])]
    items, fallos = [], 0
    for termino, lugar in busquedas:
        for pag in range(cfg.get("paginas_max", 10)):
            url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
                   f"keywords={quote(termino)}&location={quote(lugar)}&f_TPR=r604800&sortBy=DD&start={pag * 10}")
            try:
                r = http.get(url, intervalo=cfg.get("pausa", 1.5), espera_429=6, intentos_429=6)
            except Bloqueado:
                if not items:
                    raise
                _aviso(ctx, f"429 persistente en '{termino}' / {lugar}; sigo con lo leído")
                return items
            except Exception as e:
                fallos += 1
                _aviso(ctx, f"'{termino}' / {lugar} p{pag}: {e}")
                break
            nuevos = parse_linkedin(r.text, f"{termino} @ {lugar}")
            items += nuevos
            if len(nuevos) < 10:
                break
    if fallos == len(busquedas):
        raise RuntimeError("todas las búsquedas fallaron")
    return items


# ---- D. KitEmpleo

def parse_kitempleo(html_txt: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    patron = re.compile(r"/empleo/(\w{6,10})/([^/?#]+)")
    out, vistos = [], set()
    for a in sopa.find_all("a", href=patron):
        m = patron.search(a["href"])
        oid = m[1]
        if oid in vistos:
            continue
        vistos.add(oid)

        def icono(nombre):
            i = a.select_one(f".blog-three-attrib i.{nombre}")
            return texto(i.parent) if i else ""

        h = a.find(["h3", "h4"])
        cargo = texto(h) if h else (texto(a) or m[2].replace("-", " "))
        full = texto(a)
        out.append(_item(oid, cargo, urljoin("https://www.kitempleo.com.co", a["href"]), busqueda,
                         icono("fa-pencil"), icono("fa-map-marker"), icono("fa-calendar"),
                         _salario(full), full))
    return out


def detalle_kitempleo(ctx, item: dict) -> bool:
    f = fecha_desde_texto(item["publicada"]) if item["publicada"] else None
    if f and (ahora_co() - f).days > ctx["cfg"].get("max_dias", 30) + 7:
        return False
    r = ctx["http"].get(item["url"])
    sopa = BeautifulSoup(r.text, "html.parser")
    full = texto(sopa.body or sopa)
    
    m_pub = re.search(r"Publicado\s*:\s*(.*?)(?:\s*-|$)", full, re.I)
    if m_pub:
        item["publicada"] = m_pub[1]
    
    m_cierre = re.search(r"(?:cierre|vencimiento|vence)\s*[:el]*\s*(\d{1,2}\s+(?:de\s+)?[a-z]{3}\.?[\sde]*\d{4})", full, re.I)
    if m_cierre:
        item["_cierre_texto"] = m_cierre[1]
    item['_desc_text'] = full
    return True


def fuente_kitempleo(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    items, fallos = [], 0
    for termino in cfg.get("terminos", []):
        try:
            r = http.post("https://www.kitempleo.com.co/search/", data={"keywords": termino, "submit": ""})
        except Bloqueado:
            raise
        except Exception as e:
            fallos += 1
            _aviso(ctx, f"'{termino}': {e}")
            continue
        for it in parse_kitempleo(r.text, termino):
            f = fecha_desde_texto(it["publicada"]) if it["publicada"] else None
            if f and (ahora_co() - f).days > cfg.get("max_dias", 30):
                it["_viejo"] = True  # avisos de 2016: se marcan vistos pero nunca son candidatos
            items.append(it)
    if fallos == len(cfg.get("terminos", [])):
        raise RuntimeError("todas las búsquedas fallaron")
    return items


# ---- E. Indeed

FALSOS_JK = {"0123456789abcdef", "0000000000000000"}


def parse_indeed(html_txt: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out = []
    for card in sopa.select(".job_seen_beacon"):
        a = card.select_one("a[data-jk]")
        if not a or a["data-jk"] in FALSOS_JK:
            continue
        jk = a["data-jk"]
        tit = card.select_one("h2 span[title]")
        cargo = tit["title"] if tit else texto(card.find("h2"))
        full = texto(card)
        out.append(_item(jk, cargo, f"https://co.indeed.com/viewjob?jk={jk}", busqueda,
                         texto(card.select_one("[data-testid=company-name], .companyName")),
                         texto(card.select_one("[data-testid=text-location], .companyLocation")),
                         texto(card.select_one("[data-testid=myJobsStateDate], .date")),
                         texto(card.select_one(".salary-snippet-container, [data-testid*=salary]")), full))
    return out


def fuente_indeed(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    items = []
    for termino in cfg.get("terminos", []):
        url = f"https://co.indeed.com/jobs?q={quote_plus(termino)}&fromage=7&sort=date"
        r = http.get(url, intentos=1, intentos_429=0)  # si bloquea, no insistimos
        items += parse_indeed(r.text, termino)
    return items


# ---- F. Pandapé

def parse_pandape(html_txt: str, base: str, empresa: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    patron = re.compile(r"/Detail/(\d+)", re.I)
    out, vistos = [], set()
    for a in sopa.find_all("a", href=patron):
        oid = patron.search(a["href"])[1]
        if oid in vistos:
            continue
        vistos.add(oid)
        card = a if a.find(["h2", "h3", "h4"]) else a.find_parent(class_=re.compile("card")) or a

        def icono(nombre):
            i = card.select_one(f"i.{nombre}")
            if not i:
                return ""
            cont = i.find_parent(class_="icon-container")
            return texto(cont.parent if cont else i.parent)

        h = card.find(["h2", "h3", "h4"])
        cargo = limpiar(h.get("title")) or texto(h) if h else (limpiar(a.get("title")) or texto(a))
        out.append(_item(oid, cargo, urljoin(base, a["href"]), empresa, empresa.capitalize(),
                         icono("icon-location-pin-1"), texto(card.select_one(".vacancy-date")),
                         icono("icon-wallet"), texto(card)))
    return out


def fuente_pandape(ctx) -> list[dict]:
    """Primera página del listado + "Ver 20 ofertas más", que el micrositio hace con
    $.post("/ListVacancies", {PageNumber, PageSize}) y responde JSON {view: html, isLast}."""
    cfg, http = ctx["cfg"], ctx["http"]
    items, fallos = [], 0
    for empresa, url in cfg.get("portales", {}).items():
        try:
            r = http.get(url)
        except Bloqueado:
            raise
        except Exception as e:
            fallos += 1
            _aviso(ctx, f"{empresa}: {e}")
            continue
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        sopa = BeautifulSoup(r.text, "html.parser")
        lote = parse_pandape(r.text, base, empresa)
        ids = {i["id_local"] for i in lote}
        items += lote
        ultimo = sopa.find(id="hdn_isLast")
        es_ultima = not sopa.find(id="btLoadMore") or (ultimo is not None and ultimo.get("value") == "True")
        pagina, sin_nuevos = 1, 0
        while not es_ultima and pagina < 15:
            pagina += 1
            try:
                rr = http.post(urljoin(base, "/ListVacancies"), data={"PageNumber": pagina, "PageSize": 20},
                               headers={"X-Requested-With": "XMLHttpRequest", "Referer": url})
                d = rr.json()
            except Exception as e:  # noqa: BLE001
                _aviso(ctx, f"{empresa}: 'Ver más ofertas' página {pagina}: {e}")
                break
            nuevos = [i for i in parse_pandape(d.get("view") or "", base, empresa) if i["id_local"] not in ids]
            ids |= {i["id_local"] for i in nuevos}
            items += nuevos
            es_ultima = bool(d.get("isLast"))
            sin_nuevos = 0 if nuevos else sin_nuevos + 1
            if sin_nuevos >= 2:
                break
    if fallos == len(cfg.get("portales", {})):
        raise RuntimeError("todos los portales fallaron")
    return items


# ---- G. Jooble

def parse_jooble(html_txt: str, busqueda: str) -> list[dict]:
    return []


def fuente_jooble(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    items = []
    api_key = os.environ.get("JOOBLE_API_KEY")
    if not api_key:
        raise Bloqueado("Falta JOOBLE_API_KEY en variables de entorno (bloqueado)")
    for ruta in cfg.get("rutas", []):
        keyword = ruta.replace("/trabajo-", "").replace("-", " ")
        r = http.post(f"https://jooble.org/api/{api_key}", json={"keywords": keyword, "location": "Colombia"}, intentos=2, intentos_429=1)
        if r.status_code != 200:
            if r.status_code in (401, 403):
                raise Bloqueado(f"Jooble API HTTP {r.status_code} (key inválida o bloqueada)")
            raise ValueError(f"Jooble API HTTP {r.status_code}")
        for j in r.json().get("jobs", []):
            items.append(_item(
                j.get("id"),
                j.get("title", ""),
                j.get("link", ""),
                ruta.strip("/"),
                empresa=j.get("company", ""),
                ciudad=j.get("location", ""),
                publicada=j.get("updated", ""),
                salario=j.get("salary", ""),
                tarjeta=j.get("snippet", "")
            ))
    return items


# ---- H. Agrohunters

def parse_agrohunters(html_txt: str, busqueda: str = "ofertas-laborales") -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron_id = re.compile(r"codigo-(\d+)", re.I)
    for a in sopa.find_all("a", href=patron_id):
        href = a.get("href", "")
        m = patron_id.search(href)
        if not m:
            continue
        oid = m[1]
        if oid in vistos:
            continue
        vistos.add(oid)

        # Series: 28xx = agro Colombia, 5xxx = nutrición animal (no), 80xx = exterior (no)
        if oid.startswith("5") or oid.startswith("80"):
            continue

        card = a.find_parent("article") or a.find_parent("div", class_=re.compile(r"post|item|card|entry")) or a.parent
        card_txt = texto(card)
        h = card.find(["h2", "h3", "h4"]) if card else None
        cargo = texto(h) if h else (texto(a) or card_txt.split(" - ")[0])
        cargo = re.sub(r"-\s*C[oó]digo\s*\d+.*$", "", cargo, flags=re.I).strip()

        # Descartar flores (2839, 2840...) y nutricion animal
        if oid in ("2839", "2840") or re.search(r"\b(flores?|rosas?|claveles?|crisantemo|pomp[oó]n|nutrici[oó]n animal)\b", f"{cargo} {card_txt}", re.I):
            continue

        ciu = _ciudad_colombia(card_txt)
        out.append(_item(oid, cargo, href, busqueda, empresa="Agrohunters", ciudad=ciu, tarjeta=card_txt))
    return out


def fuente_agrohunters(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://agrohunters.com/ofertas-laborales/")
    r = http.get(url)
    return parse_agrohunters(r.text)


# ---- I. ADAMA

def parse_adama(html_txt: str, busqueda: str = "Colombia") -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron_job = re.compile(r"/job/([^/?#]+)/(\d+)", re.I)

    for row in sopa.select("tr.data-row, li.job-tile, .searchResults tr"):
        a = row.select_one("a.jobTitle-link, a[href*='/job/']")
        if not a:
            continue
        href = a.get("href", "")
        m = patron_job.search(href)
        oid = m[2] if m else None
        if not oid:
            m_alt = re.search(r"(\d{5,})", href)
            oid = m_alt[1] if m_alt else ""
        if not oid or oid in vistos:
            continue
        vistos.add(oid)

        cargo = texto(a)
        url = urljoin("https://careers.adama.com", href)
        loc = row.select_one(".jobLocation, [class*=location]")
        ciu = texto(loc) or _ciudad_colombia(texto(row)) or "Colombia"
        date_el = row.select_one(".jobDate, [class*=date]")
        pub = texto(date_el)
        out.append(_item(oid, cargo, url, busqueda, empresa="ADAMA", ciudad=ciu, publicada=pub, tarjeta=texto(row)))

    if not out:
        for a in sopa.find_all("a", href=patron_job):
            href = a.get("href", "")
            m = patron_job.search(href)
            oid = m[2]
            if oid in vistos:
                continue
            vistos.add(oid)
            cargo = texto(a)
            url = urljoin("https://careers.adama.com", href)
            out.append(_item(oid, cargo, url, busqueda, empresa="ADAMA", ciudad="Colombia", tarjeta=cargo))
    return out


def fuente_adama(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://careers.adama.com/search/?q=&locationsearch=Colombia&sortColumn=referencedate&sortDirection=desc")
    r = http.get(url)
    return parse_adama(r.text)


# ---- J. Magneto

def parse_magneto(html_txt: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron = re.compile(r"/co/empleos/[^/]+-(\d+)")
    for a in sopa.find_all("a", href=patron):
        href = a.get("href", "")
        m = patron.search(href)
        if not m:
            continue
        oid = m[1]
        if oid in vistos:
            continue
        vistos.add(oid)

        cargo = texto(a)
        if not cargo:
            continue
        url = urljoin("https://www.magneto365.com", href)
        card = a.find_parent("div", class_=re.compile(r"card|item|job|border")) or a.parent
        card_txt = texto(card)

        # Extraer empresa de la tarjeta: patrón "Empresa | salario | ciudad"
        emp = ""
        candidatos = []
        for el in card.find_all(True):
            if el != card and el != a and "|" in el.get_text():
                candidatos.append(el)
        if candidatos:
            el_pipe = min(candidatos, key=lambda e: len(texto(e)))
            emp = texto(el_pipe).split("|")[0].strip()
        elif "|" in card_txt:
            txt_resto = card_txt
            if cargo and txt_resto.startswith(cargo):
                txt_resto = txt_resto[len(cargo):].strip()
            elif cargo and cargo in txt_resto:
                txt_resto = txt_resto.replace(cargo, "", 1).strip()
            if "|" in txt_resto:
                emp = txt_resto.split("|")[0].strip()

        if cargo and emp.startswith(cargo):
            emp = emp[len(cargo):].strip()
        emp = limpiar(emp.strip(" -|:\t\r\n"))
        if normalizar(emp) == "servicio de empleo comfama":
            emp = "Comfama"
        if not emp:
            emp = "Magneto (empresa no indicada)"
        sal = _salario(card_txt)
        ciu = _ciudad_colombia(card_txt)
        out.append(_item(oid, cargo, url, busqueda, empresa=emp, ciudad=ciu, salario=sal, tarjeta=card_txt))
    return out


def fuente_magneto(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    rutas = cfg.get("rutas", [
        "/co/trabajos/ofertas-empleo-de-ingeniero-agronomo",
        "/co/trabajos/ofertas-empleo-de-administrador-agropecuario",
        "/co/trabajos/ofertas-empleo-de-asesor-tecnico-comercial"
    ])
    items = []
    for r_path in rutas:
        url = urljoin("https://www.magneto365.com", r_path)
        try:
            r = http.get(url)
            items += parse_magneto(r.text, r_path.split("/")[-1])
        except Bloqueado:
            raise
        except Exception as e:
            _aviso(ctx, f"{url}: {e}")
            continue
    return items


# ---- K. elempleo_empresa

def fuente_elempleo_empresa(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    slugs = cfg.get("slugs", [
        "acepalma", "palmar-del-oriente", "grupo-diana", "disan-colombia-sas",
        "interoc-sa-sucursal-colombia", "federacion-nacional-cafeteros-colombia"
    ])
    rutas_extra = cfg.get("rutas_extra", ["/co/empleos-empresas/fedearroz/5028"])
    items = []
    for slug in slugs:
        url = f"https://www.elempleo.com/co/ofertas-empleo/{slug}"
        try:
            r = http.get(url)
            items += parse_elempleo(r.text, slug)
        except Bloqueado:
            raise
        except Exception as e:
            _aviso(ctx, f"{url}: {e}")
            continue
    for ruta in rutas_extra:
        url = urljoin("https://www.elempleo.com", ruta)
        try:
            r = http.get(url)
            items += parse_elempleo(r.text, ruta.split("/")[-2] if "/" in ruta else ruta)
        except Bloqueado:
            raise
        except Exception as e:
            _aviso(ctx, f"{url}: {e}")
            continue
    return items


# ---- L. FAO / UN

def parse_fao_un(html_txt: str, busqueda: str = "Colombia") -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron_link = re.compile(r"fao\.taleo\.net|colombia\.un\.org/.*jobs|unsdg\.un\.org/jobs|jobdetail", re.I)
    patron_job_id = re.compile(r"job=(\d+)|/jobs/(\d+)", re.I)

    for a in sopa.find_all("a", href=patron_link):
        href = a.get("href", "")
        m = patron_job_id.search(href)
        if not m:
            continue
        oid = m[1] or m[2]
        if oid in vistos:
            continue
        vistos.add(oid)

        cargo = texto(a)
        if not cargo:
            continue

        card = a.find_parent("div", class_=re.compile(r"views-row|node|card|item")) or a.parent
        card_txt = texto(card)

        # Filtrar por palabras agro, forestal y pecuario van aparte
        full_txt = f"{cargo} {card_txt}"
        rx_fao_agro = r"\b(agr[oó]|agr[ií]col|forestal|pecuari|aliment|suelo|riego|rural)"
        if not re.search(rx_fao_agro, full_txt, re.I):
            continue

        emp = "FAO" if "fao" in href.lower() or "fao" in full_txt.lower() else "ONU Colombia"
        ciu = _ciudad_colombia(card_txt) or "Colombia"

        m_pub = re.search(r"(\d{1,2}\s+[a-z]{3,}\s+\d{4})", card_txt, re.I)
        pub = m_pub[1] if m_pub else ""

        cierre = ""
        m_cie = re.search(r"cierre\s*\|?\s*(\d{1,2}\s+[a-z]{3,}\s+\d{4})", card_txt, re.I)
        if m_cie:
            cierre = m_cie[1]

        item = _item(oid, cargo, href, busqueda, empresa=emp, ciudad=ciu, publicada=pub, tarjeta=card_txt)
        if cierre:
            item["_cierre_texto"] = cierre
        out.append(item)
    return out


def fuente_fao_un(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://colombia.un.org/es/jobs")
    r = http.get(url)
    return parse_fao_un(r.text)


# ---- M. Fedepalma

def parse_fedepalma(html_txt: str, busqueda: str = "Fedepalma") -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    slides = sopa.select(".contSlideVacante, .vancante")
    for s in slides:
        full = texto(s)
        h = s.find(["h2", "h3", "h4", "p", "div"])
        title = texto(h) if h else ""
        if not title or len(title) < 5 or title.lower().startswith("post") or title.lower().startswith("excelentes"):
            parts = [p.strip() for p in s.get_text(separator="\n", strip=True).splitlines() if p.strip()]
            title = parts[0] if parts else ""
        if not title:
            continue

        img_a = s.find("a", href=lambda h: h and any(ext in h.lower() for ext in [".jpg", ".png", ".jpeg", ".webp", ".pdf"]))
        if img_a and img_a.get("href"):
            href = img_a["href"]
            oid = re.sub(r"\.[^.]+$", "", href.split("/")[-1])
        else:
            oid = re.sub(r"[^a-z0-9]+", "-", normalizar(title))

        if oid in vistos:
            continue
        vistos.add(oid)

        url = img_a["href"] if img_a else "https://fedepalma.org/trabaje-con-nosotros/"
        ciu = _ciudad_colombia(full) or "Colombia"
        if not ciu or ciu == "Colombia":
            if "zona oriental" in full.lower():
                ciu = "Zona Oriental"
            elif "zona norte" in full.lower():
                ciu = "Zona Norte"

        m_cie = re.search(r"post[uú]late antes del\s*(\d{1,2}\s+(?:de\s+)?[a-z]{3,}\s+\d{4})", full, re.I)
        cierre = m_cie[1] if m_cie else ""

        item = _item(oid, title, url, busqueda, empresa="Fedepalma", ciudad=ciu, tarjeta=full)
        if cierre:
            item["_cierre_texto"] = cierre
        out.append(item)
    return out


def fuente_fedepalma(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://fedepalma.org/trabaje-con-nosotros/")
    r = http.get(url)
    return parse_fedepalma(r.text)


# ---- N. Agrosavia

def parse_agrosavia(html_txt: str, busqueda: str = "Colombia") -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron_job = re.compile(r"/job/([^/?#]+)/(\d+)", re.I)

    for row in sopa.select("tr.data-row, li.job-tile, .searchResults tr"):
        a = row.select_one("a.jobTitle-link, a[href*='/job/']")
        if not a:
            continue
        href = a.get("href", "")
        m = patron_job.search(href)
        oid = m[2] if m else None
        if not oid:
            m_alt = re.search(r"(\d{5,})", href)
            oid = m_alt[1] if m_alt else ""
        if not oid or oid in vistos:
            continue
        vistos.add(oid)

        cargo = texto(a)
        url = urljoin("https://talento.agrosavia.co", href)
        loc = row.select_one(".jobLocation, [class*=location]")
        ciu = texto(loc) or _ciudad_colombia(texto(row)) or "Colombia"
        date_el = row.select_one(".jobDate, [class*=date]")
        pub = texto(date_el)
        out.append(_item(oid, cargo, url, busqueda, empresa="Agrosavia", ciudad=ciu, publicada=pub, tarjeta=texto(row)))
    return out


def fuente_agrosavia(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://talento.agrosavia.co/search/")
    r = http.get(url)
    return parse_agrosavia(r.text)


# ---- O. Syngenta

def parse_syngenta(data: dict | list, busqueda: str = "Colombia") -> list[dict]:
    out = []
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    for j in jobs:
        jid = str(j.get("id", ""))
        if not jid:
            continue
        cargo = limpiar(j.get("title", ""))
        url = j.get("url") or j.get("local_url") or f"https://jobs.syngenta.com/job/{jid}"
        ciu = limpiar(j.get("city") or j.get("location") or "Colombia")
        pub = limpiar(j.get("published", ""))
        sal = limpiar(j.get("compensation", ""))
        full = f"{cargo} {j.get('department', '')} {j.get('function', '')} {j.get('job_type', '')}"
        out.append(_item(jid, cargo, url, busqueda, empresa="Syngenta", ciudad=ciu, publicada=pub, salario=sal, tarjeta=full))
    return out


def fuente_syngenta(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://jobs.syngenta.com/api/jobs?country=CO")
    r = http.get(url)
    return parse_syngenta(r.json())


# ---- P. Yara

def parse_yara(html_txt: str, busqueda: str = "Colombia") -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron_job = re.compile(r"/job/([^/?#]+)/(\d+)", re.I)

    for row in sopa.select("tr.data-row, li.job-tile, .searchResults tr"):
        a = row.select_one("a.jobTitle-link, a[href*='/job/']")
        if not a:
            continue
        href = a.get("href", "")
        m = patron_job.search(href)
        oid = m[2] if m else None
        if not oid:
            m_alt = re.search(r"(\d{5,})", href)
            oid = m_alt[1] if m_alt else ""
        if not oid or oid in vistos:
            continue
        vistos.add(oid)

        cargo = texto(a)
        url = urljoin("https://jobs.yara.com", href)
        loc = row.select_one(".jobLocation, [class*=location]")
        ciu = texto(loc) or _ciudad_colombia(texto(row)) or "Colombia"
        date_el = row.select_one(".jobDate, [class*=date]")
        pub = texto(date_el)
        out.append(_item(oid, cargo, url, busqueda, empresa="Yara", ciudad=ciu, publicada=pub, tarjeta=texto(row)))
    return out


def fuente_yara(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://jobs.yara.com/search/?q=&locationsearch=Colombia")
    r = http.get(url)
    return parse_yara(r.text)


# ---- Q. Riopaila

def parse_riopaila(html_txt: str, busqueda: str = "Colombia") -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron_job = re.compile(r"/job/([^/?#]+)/(\d+)", re.I)

    for row in sopa.select("li.job-tile, tr.data-row, .searchResults tr"):
        a = row.select_one("a.jobTitle-link, a[href*='/job/']")
        if not a:
            continue
        href = a.get("href", "")
        m = patron_job.search(href)
        oid = m[2] if m else None
        if not oid:
            for cls in row.get("class", []):
                if cls.startswith("job-id-"):
                    oid = cls.replace("job-id-", "")
                    break
        if not oid or oid in vistos:
            continue
        vistos.add(oid)

        cargo = texto(a)
        url = urljoin("https://trabaja-con-nosotros.riopaila-castilla.com", href)
        loc = row.select_one(".jobLocation, [class*=location]")
        ciu = texto(loc) or _ciudad_colombia(texto(row)) or "Valle del Cauca"
        date_el = row.select_one(".jobDate, [class*=date], .date")
        pub = texto(date_el)
        if not pub:
            m_f = re.search(r"Fecha\s*\|\s*(\d{1,2}\s+[a-z]{3}\s+\d{4})", texto(row), re.I)
            if m_f:
                pub = m_f[1]
        out.append(_item(oid, cargo, url, busqueda, empresa="Riopaila Castilla", ciudad=ciu, publicada=pub, tarjeta=texto(row)))
    return out


def fuente_riopaila(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    url = cfg.get("url", "https://trabaja-con-nosotros.riopaila-castilla.com/search/?q=&sortColumn=referencedate&sortDirection=desc")
    r = http.get(url)
    return parse_riopaila(r.text)


# ---- R. Manuelita

def parse_manuelita(html_txt: str, base_url: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    out, vistos = [], set()
    patron = re.compile(r"/jobs/(\d+)-([a-z0-9-]+)", re.I)
    for a in sopa.find_all("a", href=patron):
        href = a.get("href", "")
        m = patron.search(href)
        if not m:
            continue
        oid = m[1]
        if oid in vistos:
            continue
        vistos.add(oid)

        cargo = texto(a)
        if not cargo:
            continue

        url = urljoin(base_url, href)
        li = a.find_parent("li") or a.parent
        li_txt = texto(li)
        ciu = _ciudad_colombia(li_txt) or "Colombia"
        out.append(_item(oid, cargo, url, busqueda, empresa="Manuelita", ciudad=ciu, tarjeta=li_txt))
    return out


def fuente_manuelita(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    subdominios = cfg.get("subdominios", [
        "grupomanuelita",
        "manuelitaazucaryenergia",
        "manuelitaaceitesyenergia",
        "manuelitafrutasyhortalizas"
    ])
    items = []
    for sub in subdominios:
        url = f"https://{sub}.na.teamtailor.com/jobs"
        try:
            r = http.get(url)
            items += parse_manuelita(r.text, url, sub)
        except Bloqueado:
            raise
        except Exception as e:
            _aviso(ctx, f"{url}: {e}")
            continue
    return items


# Orden = prioridad al deduplicar por cargo+empresa (la primera fuente gana).
FUENTES = {
    "pandape": {"listar": fuente_pandape, "aplicar_p": False},
    "computrabajo": {"listar": fuente_computrabajo},
    "elempleo": {"listar": fuente_elempleo, "detalle": detalle_elempleo},
    "linkedin": {"listar": fuente_linkedin},
    "indeed": {"listar": fuente_indeed},
    "jooble": {"listar": fuente_jooble, "permitir_vacio": True},
    "kitempleo": {"listar": fuente_kitempleo, "detalle": detalle_kitempleo},
    "agrohunters": {"listar": fuente_agrohunters, "permitir_vacio": True},
    "adama": {"listar": fuente_adama, "permitir_vacio": True},
    "magneto": {"listar": fuente_magneto, "permitir_vacio": True},
    "elempleo_empresa": {"listar": fuente_elempleo_empresa, "permitir_vacio": True},
    "fao_un": {"listar": fuente_fao_un, "permitir_vacio": True},
    "fedepalma": {"listar": fuente_fedepalma, "permitir_vacio": True},
    "agrosavia": {"listar": fuente_agrosavia, "permitir_vacio": True},
    "syngenta": {"listar": fuente_syngenta, "permitir_vacio": True},
    "yara": {"listar": fuente_yara, "permitir_vacio": True},
    "riopaila": {"listar": fuente_riopaila, "permitir_vacio": True},
    "manuelita": {"listar": fuente_manuelita, "permitir_vacio": True},
}


# --------------------------------------------------------------------------- estado y salidas

def leer_json(ruta: Path, defecto):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def escribir_json(ruta: Path, datos):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def purgar_vistos(vistos: dict, dias: int, hoy: str) -> dict:
    limite = (datetime.fromisoformat(hoy) - timedelta(days=dias)).date().isoformat()
    return {portal: {k: f for k, f in ids.items() if f >= limite} for portal, ids in vistos.items()}


def _partes(k: str) -> tuple[str, str, str, str]:
    base, _, salario = k.partition("#")
    base, _, ciudad = base.partition("@")
    cargo, _, empresa = base.partition("|")
    return cargo, empresa, ciudad, salario


def _ciudades_compatibles(a: str, b: str) -> bool:
    if not a or not b or a.startswith(b) or b.startswith(a):
        return True
    if ("bogota" in a and "cundinamarca" in b) or ("bogota" in b and "cundinamarca" in a):
        return True
    if ("metropolitana" in a and ("lebrija" in b or "bucaramanga" in b)) or ("metropolitana" in b and ("lebrija" in a or "bucaramanga" in a)):
        return True
    return False


def clave_repetida(k: str, claves, mirar_ciudad: bool = True) -> bool:
    cargo, empresa, ciudad, salario = _partes(k)
    for otra in claves:
        c2, e2, ciudad2, salario2 = _partes(otra)
        if not (c2 == cargo or (min(len(c2), len(cargo)) >= 14 and (c2.startswith(cargo) or cargo.startswith(c2)))):
            continue
        if e2 != "?" and empresa != "?" and not (e2 in empresa or empresa in e2):
            continue
        if (mirar_ciudad or empresa == "?" or e2 == "?") and not _ciudades_compatibles(ciudad, ciudad2):
            continue
        if (empresa == "?" or e2 == "?") and re.search(r"\b(lider|jefe|coordinador|director|gerente|asesor)\b", cargo):
            if salario and salario2 and salario != salario2:
                continue
        return True
    return False


def deduplicar(items: list[dict]) -> list[dict]:
    """Quita repetidos dentro de una misma corrida por id y por cargo+empresa. La misma
    vacante publicada en varias ciudades sale una vez, con las otras ciudades agregadas."""
    out, ids, claves = [], set(), {}
    for it in items:
        k = clave_vacante(it["cargo"], it["empresa"], it.get("ciudad", ""), it.get("salario", ""))
        if it["id"] in ids:
            continue
        previa = next((claves[o] for o in claves if clave_repetida(k, [o], mirar_ciudad=False)), None)
        if previa is not None:
            ciudad = limpiar(it.get("ciudad", ""))
            if ciudad_base(ciudad) and ciudad_base(ciudad) not in normalizar(previa.get("ciudad", "")):
                previa["ciudad"] = f'{previa.get("ciudad", "")} · {ciudad}'.strip(" ·")
            continue
        ids.add(it["id"])
        claves[k] = it
        out.append(it)
    return out


def seleccionar(portal: str, listados: list[dict], filtro: Filtro, vistos: dict,
                hoy: str, aplicar_p: bool = True) -> tuple[list[dict], int]:
    """Filtra y separa las nuevas. Registra en `vistos` todos los ids leídos.
    Devuelve (candidatas nuevas pendientes de detalle, n que pasan el filtro)."""
    ya = vistos.setdefault(portal, {})
    unicos: dict[str, dict] = {}
    for it in listados:
        previo = unicos.get(it["id_local"])
        if previo:
            if it["fuente_busqueda"] not in previo["fuente_busqueda"].split(" | "):
                previo["fuente_busqueda"] += " | " + it["fuente_busqueda"]
            continue
        unicos[it["id_local"]] = it
    candidatas, n_filtro = [], 0
    for idl, it in unicos.items():
        es_nuevo = idl not in ya
        if es_nuevo:
            ya[idl] = hoy
        pasa = filtro.pasa(it["cargo"], it["tarjeta"], it["ciudad"], aplicar_p, it.get("empresa", ""))
        if not pasa and aplicar_p and not filtro.excluido(it["cargo"], it.get("empresa", "")) and not filtro.ubicacion_excluida(it.get("ciudad", "")):
            if re.search(r"\b(l[ií]der|jefe|coordinador|director|gerente|asesor|representante|ingeniero|especialista|investigador|desarrollista)\b", it["cargo"], re.IGNORECASE | re.UNICODE):
                it["_requiere_detalle"] = True
                pasa = True

        if it.get("_viejo") or not pasa:
            continue
        n_filtro += 1
        if not es_nuevo:
            continue
        it["id"] = f"{portal}-{idl}"
        it["portal"] = portal
        candidatas.append(it)
    return candidatas, n_filtro


def correr_fuente(nombre: str, cfg: dict, gen: dict, snapshots: Path | None) -> dict:
    ctx = {"nombre": nombre, "cfg": cfg, "http": Http(gen, nombre, snapshots), "avisos": []}
    t0 = time.monotonic()
    res = {"nombre": nombre, "ctx": ctx, "listados": [], "estado": "ok", "error": ""}
    try:
        res["listados"] = FUENTES[nombre]["listar"](ctx)
        if not res["listados"] and not FUENTES[nombre].get("permitir_vacio", False):
            res["estado"] = "vacio"
            res["error"] = "0 listados leídos (¿cambió el HTML?)"
    except Bloqueado as e:
        res["estado"], res["error"] = "bloqueado", str(e)
    except Exception as e:  # noqa: BLE001 — una fuente nunca tumba a las demás
        res["estado"], res["error"] = "error", f"{type(e).__name__}: {e}"
        log.debug(traceback.format_exc())
    res["duracion_s"] = round(time.monotonic() - t0, 1)
    log.info("[%s] %s: %d listados en %ss (%d peticiones) %s", nombre, res["estado"],
             len(res["listados"]), res["duracion_s"], ctx["http"].peticiones, res["error"])
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--solo", help="fuentes separadas por coma")
    ap.add_argument("--datos", default=str(RAIZ / "data"))
    ap.add_argument("--docs", default=str(RAIZ / "docs"))
    ap.add_argument("--config", default=str(RAIZ / "robot" / "config.yaml"))
    ap.add_argument("--snapshots", default=os.environ.get("SNAPSHOT_DIR") or None,
                    help="carpeta donde guardar el HTML crudo (depuración)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = cargar_config(Path(args.config))
    gen = config.get("general", {})
    filtro = Filtro(config["filtro"])
    datos, docs = Path(args.datos), Path(args.docs)
    snapshots = Path(args.snapshots) if args.snapshots else None

    inicio = ahora_co()
    hoy = inicio.date().isoformat()
    ruta_vistos = datos / "vistos.json"
    corrida_inicial = not ruta_vistos.exists()
    vistos = purgar_vistos(leer_json(ruta_vistos, {}), gen.get("purgar_vistos_dias", 45), hoy)
    # claves sin "@ciudad" son del formato anterior (no distinguían ciudad): se descartan
    claves = {k: f for k, f in vistos.pop("_claves", {}).items() if "@" in k}
    claves = completar_claves(claves, leer_historial(datos / "historial.csv"),
                              gen.get("purgar_vistos_dias", 45), hoy)
    estado_prev = leer_json(datos / "estado.json", {}).get("fuentes", {})

    semilla_aplicada = ""
    semilla = leer_json(datos / "semilla_tablero.json", {})
    if semilla:
        semilla_aplicada = hoy
        f_sem = hoy
        for portal, list_ids in semilla.get("ids", {}).items():
            ya_portal = vistos.setdefault(portal, {})
            for id_v in list_ids:
                ya_portal[str(id_v)] = f_sem
        for cl in semilla.get("claves", []):
            k = clave_vacante(cl.get("cargo", ""), cl.get("empresa", ""), cl.get("ciudad", ""), cl.get("salario", ""))
            claves[k] = f_sem

    activas = [n for n in FUENTES if config["fuentes"].get(n, {}).get("activa", True)]
    if args.solo:
        pedidas = {s.strip() for s in args.solo.split(",")}
        activas = [n for n in activas if n in pedidas]

    with cf.ThreadPoolExecutor(max_workers=len(activas) or 1) as ex:
        futuros = {n: ex.submit(correr_fuente, n, config["fuentes"].get(n, {}), gen, snapshots) for n in activas}
        resultados = {n: f.result() for n, f in futuros.items()}

    estado_fuentes, nuevas = {}, []
    descartadas = []
    encontrada = inicio.strftime("%Y-%m-%d %H:%M")
    n_segunda_pasada = 0
    
    # Check history for repost
    historial_full = leer_historial(datos / "historial.csv")
    claves_historicas = {clave_vacante(r.get("cargo", ""), r.get("empresa", ""), (r.get("ciudad") or "").split(" · ")[0], r.get("salario", "")) for r in historial_full}

    for nombre in activas:  # en orden de prioridad
        res = resultados[nombre]
        spec = FUENTES[nombre]
        portal_inicial = not vistos.get(nombre)
        cand, n_filtro = seleccionar(nombre, res["listados"], filtro, vistos, hoy,
                                     spec.get("aplicar_p", True))
        n_nuevas = 0
        antiguas_filtradas = 0
        for it in cand:
            k = clave_vacante(it["cargo"], it["empresa"], it.get("ciudad", ""), it.get("salario", ""))
            if clave_repetida(k, claves):  # misma vacante ya vista en otra ciudad/portal
                continue
            if spec.get("detalle"):
                try:
                    if not spec["detalle"](res["ctx"], it):
                        continue
                except Exception as e:  # noqa: BLE001
                    _aviso(res["ctx"], f"detalle {it['url']}: {e}")
                    
            if it.get("_requiere_detalle"):
                desc = it.get("_desc_text", "")
                if not desc:
                    if n_segunda_pasada < 30:
                        n_segunda_pasada += 1
                        time.sleep(1)
                        try:
                            r_det = res["ctx"]["http"].get(it["url"])
                            s_det = BeautifulSoup(r_det.text, "html.parser")
                            for oculto in s_det.select(".hide, .hidden, script, style"):
                                oculto.decompose()
                            desc = texto(s_det.body or s_det)
                        except Exception as e:
                            _aviso(res["ctx"], f"segunda pasada {it['url']}: {e}")
                            continue
                    else:
                        continue # Skip because we reached limit
                
                rx_agro = r"\b(ingenier[oa] agr[oó]n|agr[oó]nomo|administrador agropecuario|ingenier[ií]a agron|agropecuari|cultivos? de|palma de aceite|arroz)\b"
                if re.search(rx_agro, desc, re.IGNORECASE | re.UNICODE):
                    it["via_descripcion"] = True
                else:
                    continue
                    
            f_pub = fecha_desde_texto(it.get("publicada") or "", inicio)
            it["publicada_iso"] = f_pub.strftime("%Y-%m-%d") if f_pub else ""
            it["edad_dias"] = (inicio - f_pub).days if f_pub else None
            
            f_cierre = fecha_desde_texto(it.get("_cierre_texto") or "", inicio)
            it["cierre_iso"] = f_cierre.strftime("%Y-%m-%d") if f_cierre else ""
            it["vencida"] = bool(f_cierre and f_cierre.date() < inicio.date())
            
            if it["vencida"] or (it["edad_dias"] is not None and it["edad_dias"] > 30 and not f_cierre):
                it["motivo"] = "vencida" if it["vencida"] else "antigua >30"
                descartadas.append({"id": it.get("id"), "cargo": it.get("cargo"), "empresa": it.get("empresa"), "motivo": it["motivo"]})
                antiguas_filtradas += 1
                continue
                
            c_lo = normalizar(it["cargo"])
            if re.search(r"\b(agr[oó]nom|extensi|investigaci|ensayo|auditor)\b", c_lo):
                it["perfil_sugerido"] = 3
            elif re.search(r"\b(director|jefe|gerente|administrador|coordinador|l[ií]der|proyectos)\b", c_lo):
                it["perfil_sugerido"] = 1
            elif re.search(r"\b(t[eé]cnico.?comercial|representante|asesor|ventas|comercial)\b", c_lo):
                it["perfil_sugerido"] = 2
            
            z_lo = normalizar(it.get("ciudad", ""))
            it["zona_preferida"] = bool(re.search(r"\b(casanare|meta|vichada|arauca|cesar|magdalena|santander|nariño|tolima|huila)\b", z_lo))
            
            e_lo = normalizar(it.get("empresa", ""))
            it["empresa_confidencial"] = (e_lo in _EMPRESA_VACIA or "confidencial" in e_lo)
            it["repost_probable"] = (k in claves_historicas)
            it.setdefault("via_descripcion", False)
            
            claves[k] = hoy
            it["encontrada"] = encontrada
            nuevas.append({c: it.get(c, "") for c in CAMPOS})
            n_nuevas += 1
        # registrar también las claves de candidatas ya vistas, para dedup entre portales
        for it in res["listados"]:
            if filtro.pasa(it["cargo"], it["tarjeta"], it["ciudad"], spec.get("aplicar_p", True), it.get("empresa", "")):
                claves.setdefault(clave_vacante(it["cargo"], it["empresa"], it.get("ciudad", ""), it.get("salario", "")), hoy)
                
        # Calculate filtradas_por_X and filtradas_por_P for estado.json
        f_x = sum(1 for it in res["listados"] if filtro.excluido(it["cargo"], it.get("empresa", "")))
        f_p = sum(1 for it in res["listados"] if not filtro.excluido(it["cargo"], it.get("empresa", "")) and not filtro.incluido(it["cargo"], it.get("tarjeta", "")))
        dups = len(res["listados"]) - len({i["id_local"] for i in res["listados"]})

        huella = {
            "ids": sorted(list({str(it["id_local"]) for it in res["listados"]})),
            "titulos": sorted(list({it["cargo"] for it in res["listados"] if it.get("cargo")}))
        }
        if not huella["ids"] and estado_prev.get(nombre, {}).get("huella"):
            huella = estado_prev[nombre]["huella"]
        
        estado_fuentes[nombre] = {
            "estado": res["estado"],
            "listados": len(res["listados"]),
            "unicos": len({i["id_local"] for i in res["listados"]}),
            "candidatas": n_filtro,
            "nuevas": n_nuevas,
            "filtradas_por_X": f_x,
            "filtradas_por_P": f_p,
            "duplicadas": dups,
            "antiguas": antiguas_filtradas,
            "duracion_s": res["duracion_s"],
            "peticiones": res["ctx"]["http"].peticiones,
            "error": res["error"],
            "avisos": res["ctx"]["avisos"][:10],
            "inicial": portal_inicial and res["estado"] == "ok",
            "huella": huella,
        }
    # fuentes no corridas (--solo): conservar su último estado
    for n, e in estado_prev.items():
        estado_fuentes.setdefault(n, e)

    nuevas = deduplicar(nuevas)
    vistos["_claves"] = claves
    escribir_json(ruta_vistos, vistos)
    escribir_json(datos / "nuevas.json", nuevas)
    escribir_csv(datos / "nuevas.csv", nuevas, modo="w")
    escribir_csv(datos / "historial.csv", nuevas, modo="a")
    
    if descartadas:
        ruta_desc = datos / "descartadas_por_antiguas.json"
        prev_desc = leer_json(ruta_desc, [])
        prev_desc.extend(descartadas)
        escribir_json(ruta_desc, prev_desc)

    estado = {
        "ultima_corrida": inicio.strftime("%Y-%m-%d %H:%M"),
        "ultima_corrida_iso": inicio.isoformat(timespec="seconds"),
        "corrida_inicial": corrida_inicial,
        "fuentes_iniciales": [n for n, e in estado_fuentes.items() if e.get("inicial")],
        "total_nuevas": len(nuevas),
        "duracion_s": round((ahora_co() - inicio).total_seconds(), 1),
        "version_config": hashlib.md5(Path(args.config).read_bytes()).hexdigest(),
        "semilla_aplicada": semilla_aplicada,
        "fuentes": estado_fuentes,
    }
    escribir_json(datos / "estado.json", estado)
    generar_pagina(docs / "index.html", estado, nuevas, leer_historial(datos / "historial.csv"),
                   gen.get("dias_pagina", 7))
    log.info("Listo: %d nuevas. Estado: %s", len(nuevas),
             {n: e["estado"] for n, e in estado_fuentes.items()})
    return 0


def escribir_csv(ruta: Path, filas: list[dict], modo: str):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    cabecera = modo == "w" or not ruta.exists() or ruta.stat().st_size == 0
    with open(ruta, modo, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS, extrasaction="ignore")
        if cabecera:
            w.writeheader()
        w.writerows(filas)


def completar_claves(claves: dict, historial: list[dict], dias: int, hoy: str) -> dict:
    """Asegura que toda candidata publicada en los últimos `dias` tenga su clave
    cargo|empresa@ciudad, aunque `vistos.json` la haya perdido (p. ej. al cambiar de formato)."""
    limite = (datetime.fromisoformat(hoy) - timedelta(days=dias)).date().isoformat()
    for r in historial:
        fecha = (r.get("encontrada") or "")[:10]
        if fecha >= limite:
            claves.setdefault(clave_vacante(r.get("cargo", ""), r.get("empresa", ""),
                                            (r.get("ciudad") or "").split(" · ")[0]), fecha)
    return claves


def leer_historial(ruta: Path) -> list[dict]:
    try:
        with open(ruta, newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


# --------------------------------------------------------------------------- página

ETIQUETA = {"ok": ("OK", "ok"), "vacio": ("Sin datos", "warn"), "error": ("Error", "bad"),
            "bloqueado": ("Bloqueado", "bad")}


def _tarjeta_html(v: dict) -> str:
    e = html.escape
    extra_parts = [e(x) for x in (v.get("ciudad"), v.get("publicada")) if x]
    if v.get("edad_dias") is not None:
        extra_parts.append(f"hace {v['edad_dias']} días")
    extra = " · ".join(extra_parts)
    
    sal = f'<p class="sal">{e(v["salario"])}</p>' if v.get("salario") else ""
    
    tags = ""
    if v.get("perfil_sugerido"):
        tags += f'<span class="portal" style="background:#444">P{v["perfil_sugerido"]}</span> '
    if v.get("repost_probable"):
        tags += f'<span class="portal" style="background:#b7791f">Posible Repost</span> '
        
    return (f'<article class="card"><div class="top"><div>{tags}<span class="portal">{e(v.get("portal", ""))}</span></div>'
            f'<span class="when">{e(v.get("encontrada", ""))}</span></div>'
            f'<h3>{e(v.get("cargo", ""))}</h3><p class="emp">{e(v.get("empresa") or "Empresa no indicada")}</p>'
            f'<p class="meta">{extra}</p>{sal}'
            f'<a class="btn" href="{e(v.get("url", ""))}" target="_blank" rel="noopener">Ver oferta</a></article>')


def generar_pagina(ruta: Path, estado: dict, nuevas: list[dict], historial: list[dict], dias: int):
    e = html.escape
    limite = (ahora_co() - timedelta(days=dias)).strftime("%Y-%m-%d")
    ids_hoy = {v["id"] for v in nuevas}
    recientes = [v for v in reversed(historial) if v.get("encontrada", "") >= limite and v["id"] not in ids_hoy]
    filas = []
    for n, f in estado["fuentes"].items():
        txt, cls = ETIQUETA.get(f["estado"], (f["estado"], "warn"))
        err = f'<div class="err">{e(f["error"])}</div>' if f.get("error") else ""
        filas.append(f'<li><span class="dot {cls}"></span><b>{e(n)}</b> <span class="st {cls}">{txt}</span>'
                     f'<span class="num">{f["listados"]} leídos · {f["candidatas"]} filtradas · '
                     f'<b>{f["nuevas"]}</b> nuevas</span>{err}</li>')
    aviso = ('<p class="aviso">Primera corrida: todo aparece como nuevo. Desde mañana solo verás lo realmente nuevo.</p>'
             if estado.get("corrida_inicial") else "")
    tarjetas = "".join(_tarjeta_html(v) for v in nuevas) or '<p class="vacio">Hoy no hay candidatas nuevas.</p>'
    viejas = "".join(_tarjeta_html(v) for v in recientes) or '<p class="vacio">Nada en estos días.</p>'
    pagina = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vacantes agro</title>
<style>
:root{{--bg:#f5f6f2;--card:#fff;--tx:#1d2418;--mu:#5f6b57;--ac:#2f7d32;--ok:#2f7d32;--warn:#b7791f;--bad:#c0392b;--bd:#dfe3d8}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12160f;--card:#1c2218;--tx:#e8eee2;--mu:#a3ad9a;--ac:#6fbf73;--bd:#2d3527}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font:16px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}}h1{{font-size:1.4rem;margin:.2em 0}}h2{{font-size:1.1rem;margin:1.4em 0 .6em}}
.sub{{color:var(--mu);margin:0 0 12px}}.aviso{{background:#fff3cd;color:#664d03;padding:10px 12px;border-radius:10px}}
ul.fuentes{{list-style:none;padding:0;margin:0;background:var(--card);border:1px solid var(--bd);border-radius:12px}}
ul.fuentes li{{padding:9px 12px;border-top:1px solid var(--bd);display:flex;flex-wrap:wrap;gap:4px 8px;align-items:center}}
ul.fuentes li:first-child{{border-top:0}}.num{{color:var(--mu);font-size:.85rem;width:100%;padding-left:18px}}
.dot{{width:10px;height:10px;border-radius:50%;display:inline-block}}.dot.ok{{background:var(--ok)}}.dot.warn{{background:var(--warn)}}.dot.bad{{background:var(--bad)}}
.st{{font-size:.8rem;font-weight:600}}.st.ok{{color:var(--ok)}}.st.warn{{color:var(--warn)}}.st.bad{{color:var(--bad)}}
.err{{color:var(--bad);font-size:.8rem;width:100%;padding-left:18px;word-break:break-word}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px;margin:0 0 12px}}
.card h3{{margin:.35em 0 .15em;font-size:1.05rem}}.card p{{margin:.15em 0}}.emp{{font-weight:600}}.meta,.when{{color:var(--mu);font-size:.85rem}}
.sal{{color:var(--ac);font-weight:600}}.top{{display:flex;justify-content:space-between;gap:8px}}
.portal{{background:var(--ac);color:#fff;border-radius:999px;padding:1px 10px;font-size:.75rem;text-transform:uppercase;letter-spacing:.03em}}
.btn{{display:inline-block;margin-top:10px;background:var(--ac);color:#fff;text-decoration:none;padding:9px 16px;border-radius:9px;font-weight:600}}
details{{margin-top:16px}}summary{{cursor:pointer;font-weight:600;padding:10px 0}}.vacio{{color:var(--mu)}}
footer{{color:var(--mu);font-size:.8rem;margin:24px 0}}footer a{{color:var(--mu)}}
</style></head><body><main>
<h1>Vacantes agro</h1>
<p class="sub">Última corrida: <b>{e(estado["ultima_corrida"])}</b> (hora Colombia) · {estado["total_nuevas"]} nuevas</p>
{aviso}
<ul class="fuentes">{"".join(filas)}</ul>
<h2>Nuevas de hoy ({len(nuevas)})</h2>
{tarjetas}
<details><summary>Últimos {dias} días ({len(recientes)})</summary>{viejas}</details>
<footer>Generado automáticamente por el robot de vacantes · datos: <a href="nuevas.json">nuevas.json</a> · <a href="estado.json">estado.json</a></footer>
</main></body></html>
"""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(pagina, encoding="utf-8")
    # copia de los JSON para servirlos junto a la página
    for nombre, datos in (("nuevas.json", nuevas), ("estado.json", estado)):
        escribir_json(ruta.parent / nombre, datos)


if __name__ == "__main__":
    raise SystemExit(main())
