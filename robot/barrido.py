#!/usr/bin/env python3
"""Robot de vacantes agro: baja listados de portales públicos, filtra,
deduplica y publica solo las candidatas nuevas del día. Sin IA.

Uso:  python robot/barrido.py [--solo computrabajo,linkedin] [--datos data] [--docs docs]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
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
          "fuente_busqueda", "encontrada"]

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


def clave_vacante(cargo: str, empresa: str, ciudad: str = "") -> str:
    """Clave cargo+empresa normalizados para detectar la misma vacante en varias
    ciudades o portales. Si la empresa es confidencial se añade la ciudad para no
    fusionar vacantes distintas con un cargo genérico."""
    c = normalizar(re.sub(r"\([^()]*\)\s*$", "", cargo or ""))  # "Cargo (Ciudad)" de KitEmpleo
    c = re.sub(r"\b(en|para|de)? ?(yopal|villavicencio|casanare|meta|tolima|cesar|huila|bogota|colombia)\b", " ", c)
    c = re.sub(r"\s+", " ", c).strip()
    e = re.sub(r"\s+", " ", _SUFIJOS_EMPRESA.sub(" ", normalizar(empresa))).strip()
    if e in _EMPRESA_VACIA or "confidencial" in e:
        return f"{c}|?|{normalizar(ciudad)}"
    return f"{c}|{e}"


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

class Filtro:
    def __init__(self, reglas: dict):
        f = re.IGNORECASE | re.UNICODE
        self.x = re.compile(limpiar(reglas["excluir"]), f)
        self.p = re.compile(limpiar(reglas["incluir"]), f)
        self.u = re.compile(limpiar(reglas.get("excluir_ubicacion") or r"(?!x)x"), f)

    def excluido(self, cargo: str) -> bool:
        return bool(self.x.search(cargo or ""))

    def incluido(self, cargo: str, tarjeta: str = "") -> bool:
        return bool(self.p.search(cargo or "") or self.p.search(tarjeta or ""))

    def ubicacion_excluida(self, ciudad: str) -> bool:
        return bool(self.u.search(ciudad or ""))

    def pasa(self, cargo: str, tarjeta: str = "", ciudad: str = "", aplicar_p: bool = True) -> bool:
        if not cargo or self.excluido(cargo) or self.ubicacion_excluida(ciudad):
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
            if r.status_code in (401, 403) or self._es_captcha(r):
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
                                       "request blocked", "access denied"))

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
    if f and (ahora_co() - f).days > ctx["cfg"].get("max_dias", 15) + 7:
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
    if f and (ahora_co() - f).days > ctx["cfg"].get("max_dias", 15):
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


def _pandape_mas(ctx, url: str, sopa, pagina: int) -> str | None:
    """Replica la llamada de "Ver 20 ofertas más". El endpoint sale del JS del micrositio
    (bundles/microsite/vacancy/index.min.js); se prueban las rutas que aparezcan allí."""
    http = ctx["http"]
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    if "endpoints" not in ctx:
        ctx["endpoints"] = []
        js = sopa.find("script", src=re.compile(r"vacancy/index", re.I))
        if js:
            try:
                codigo = http.get(urljoin(base, js["src"])).text
                if http.snapshots:
                    (http.snapshots / "pandape-index.min.js").write_text(codigo, encoding="utf-8")
                ctx["endpoints"] = sorted(set(re.findall(r"[\"'](/?Vacanc[\w/]*)[\"']", codigo, re.I)))
            except Exception as e:  # noqa: BLE001
                _aviso(ctx, f"JS de paginación: {e}")
    datos = {"PageNumber": pagina, "PageSize": 20, "Company.IsPreview": "False"}
    for ep in ctx["endpoints"] + ["/Vacancies"]:
        for metodo in ("POST", "GET"):
            try:
                if metodo == "POST":
                    r = http.post(urljoin(base, ep), data=datos,
                                  headers={"X-Requested-With": "XMLHttpRequest"}, intentos=1)
                else:
                    r = http.get(urljoin(base, ep), params=datos,
                                 headers={"X-Requested-With": "XMLHttpRequest"}, intentos=1)
            except Exception:  # noqa: BLE001
                continue
            if re.search(r"/Detail/\d+", r.text):
                return r.text
    return None


def fuente_pandape(ctx) -> list[dict]:
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
        ctx.pop("endpoints", None)
        pagina = 1
        while sopa.find(id="btLoadMore") and pagina < 10:
            ultimo = sopa.find(id="hdn_isLast")
            if ultimo is not None and ultimo.get("value", "").lower() == "true":
                break
            pagina += 1
            txt = _pandape_mas(ctx, url, sopa, pagina)
            nuevos = [i for i in parse_pandape(txt or "", base, empresa) if i["id_local"] not in ids]
            if not nuevos:
                if pagina == 2:
                    _aviso(ctx, f"{empresa}: no se pudo replicar 'Ver más ofertas' (solo primeras {len(ids)})")
                break
            ids |= {i["id_local"] for i in nuevos}
            items += nuevos
            sopa = BeautifulSoup(txt, "html.parser")
            if not sopa.find(id="btLoadMore"):
                sopa = BeautifulSoup(r.text, "html.parser")  # respuesta parcial: seguir hasta que no haya nuevos
    if fallos == len(cfg.get("portales", {})):
        raise RuntimeError("todos los portales fallaron")
    return items


# ---- G. Jooble

def parse_jooble(html_txt: str, busqueda: str) -> list[dict]:
    sopa = BeautifulSoup(html_txt, "html.parser")
    patron = re.compile(r"/(?:jdp|desc)/(-?\d+)")
    out, vistos = [], set()
    for a in sopa.find_all("a", href=patron):
        oid = patron.search(a["href"])[1]
        if oid in vistos:
            continue
        vistos.add(oid)
        card = _contenedor(a, lambda el: {patron.search(x["href"])[1] for x in el.find_all("a", href=patron)})
        h = card.find(["h2", "h3"])
        cargo = texto(h) if h else texto(a)
        full = texto(card)
        emp = card.select_one("[data-test-name=_companyName], [class*=company]")
        ciu = card.select_one("[data-test-name=_jobLocation], [class*=location]")
        pub = ""
        mf = re.search(r"(hace [^,.|]{1,20}|hoy|ayer)", full, re.I)
        if mf:
            pub = mf[1]
        out.append(_item(oid.lstrip("-"), cargo, urljoin("https://co.jooble.org", a["href"]), busqueda,
                         texto(emp), texto(ciu) or _ciudad_colombia(full), pub, _salario(full), full))
    return out


def fuente_jooble(ctx) -> list[dict]:
    cfg, http = ctx["cfg"], ctx["http"]
    items = []
    for ruta in cfg.get("rutas", []):
        r = http.get(urljoin("https://co.jooble.org", ruta), intentos=2, intentos_429=1)
        items += parse_jooble(r.text, ruta.strip("/"))
    return items


# Orden = prioridad al deduplicar por cargo+empresa (la primera fuente gana).
FUENTES = {
    "pandape": {"listar": fuente_pandape, "aplicar_p": False},
    "computrabajo": {"listar": fuente_computrabajo},
    "elempleo": {"listar": fuente_elempleo, "detalle": detalle_elempleo},
    "linkedin": {"listar": fuente_linkedin},
    "indeed": {"listar": fuente_indeed},
    "jooble": {"listar": fuente_jooble},
    "kitempleo": {"listar": fuente_kitempleo},
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


def deduplicar(items: list[dict]) -> list[dict]:
    """Quita repetidos dentro de una misma corrida por id y por cargo+empresa."""
    out, ids, claves = [], set(), set()
    for it in items:
        k = clave_vacante(it["cargo"], it["empresa"], it.get("ciudad", ""))
        if it["id"] in ids or k in claves:
            continue
        ids.add(it["id"])
        claves.add(k)
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
        if it.get("_viejo") or not filtro.pasa(it["cargo"], it["tarjeta"], it["ciudad"], aplicar_p):
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
        if not res["listados"]:
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
    claves = vistos.pop("_claves", {})
    estado_prev = leer_json(datos / "estado.json", {}).get("fuentes", {})

    activas = [n for n in FUENTES if config["fuentes"].get(n, {}).get("activa", True)]
    if args.solo:
        pedidas = {s.strip() for s in args.solo.split(",")}
        activas = [n for n in activas if n in pedidas]

    with cf.ThreadPoolExecutor(max_workers=len(activas) or 1) as ex:
        futuros = {n: ex.submit(correr_fuente, n, config["fuentes"].get(n, {}), gen, snapshots) for n in activas}
        resultados = {n: f.result() for n, f in futuros.items()}

    estado_fuentes, nuevas = {}, []
    encontrada = inicio.strftime("%Y-%m-%d %H:%M")
    for nombre in activas:  # en orden de prioridad
        res = resultados[nombre]
        spec = FUENTES[nombre]
        portal_inicial = not vistos.get(nombre)
        cand, n_filtro = seleccionar(nombre, res["listados"], filtro, vistos, hoy,
                                     spec.get("aplicar_p", True))
        n_nuevas = 0
        for it in cand:
            k = clave_vacante(it["cargo"], it["empresa"], it["ciudad"])
            if k in claves:  # misma vacante ya vista en otra ciudad/portal
                continue
            if spec.get("detalle"):
                try:
                    if not spec["detalle"](res["ctx"], it):
                        continue
                except Exception as e:  # noqa: BLE001
                    _aviso(res["ctx"], f"detalle {it['url']}: {e}")
            claves[k] = hoy
            it["encontrada"] = encontrada
            nuevas.append({c: it.get(c, "") for c in CAMPOS})
            n_nuevas += 1
        # registrar también las claves de candidatas ya vistas, para dedup entre portales
        for it in res["listados"]:
            if filtro.pasa(it["cargo"], it["tarjeta"], it["ciudad"], spec.get("aplicar_p", True)):
                claves.setdefault(clave_vacante(it["cargo"], it["empresa"], it["ciudad"]), hoy)
        estado_fuentes[nombre] = {
            "estado": res["estado"],
            "listados": len(res["listados"]),
            "unicos": len({i["id_local"] for i in res["listados"]}),
            "candidatas": n_filtro,
            "nuevas": n_nuevas,
            "duracion_s": res["duracion_s"],
            "peticiones": res["ctx"]["http"].peticiones,
            "error": res["error"],
            "avisos": res["ctx"]["avisos"][:10],
            "inicial": portal_inicial and res["estado"] == "ok",
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
    estado = {
        "ultima_corrida": inicio.strftime("%Y-%m-%d %H:%M"),
        "ultima_corrida_iso": inicio.isoformat(timespec="seconds"),
        "corrida_inicial": corrida_inicial,
        "fuentes_iniciales": [n for n, e in estado_fuentes.items() if e.get("inicial")],
        "total_nuevas": len(nuevas),
        "duracion_s": round((ahora_co() - inicio).total_seconds(), 1),
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
    extra = " · ".join(e(x) for x in (v.get("ciudad"), v.get("publicada")) if x)
    sal = f'<p class="sal">{e(v["salario"])}</p>' if v.get("salario") else ""
    return (f'<article class="card"><div class="top"><span class="portal">{e(v.get("portal", ""))}</span>'
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
