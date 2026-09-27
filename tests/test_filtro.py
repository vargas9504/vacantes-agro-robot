import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "robot"))
import barrido as b  # noqa: E402

FILTRO = b.Filtro(b.cargar_config()["filtro"])


@pytest.mark.parametrize("cargo,esperado", [
    ("Técnico agrónomo", False),
    ("Técnico comercial agrícola", True),
    ("Jefe MIPE clavel", False),
    ("Auxiliar de extensión I (Cenipalma)", True),
    ("Senior Estimator (La Palma)", False),
    ("Gerente de Planta – Molinos de arroz", True),
    ("INGENIERO AGRÓNOMO", True),
    ("Auxiliar de bodega agrícola", False),
    ("Aprendiz SENA agropecuario", False),
    ("Contador público", False),
    ("Tecnòlogo de extensiòn sanitaria de palma", False),
    ("Convocatoria cargos de infraestructura vial", False),
])
def test_filtro_cargo(cargo, esperado):
    assert FILTRO.pasa(cargo) is esperado


def test_palmira_no_es_palma():
    assert not FILTRO.pasa("Analista contable", tarjeta="Comfenalco, Palmira, Valle del Cauca")


def test_p_usa_texto_de_tarjeta():
    assert FILTRO.pasa("Jefe de producción", tarjeta="Empresa palmera en Casanare")
    assert not FILTRO.pasa("Jefe de producción", tarjeta="Fábrica de muebles en Bogotá")


def test_sin_p_para_pandape():
    assert FILTRO.pasa("Analista de laboratorio", aplicar_p=False)
    assert not FILTRO.pasa("Aprendiz de laboratorio", aplicar_p=False)


def test_ubicacion_argentina():
    assert not FILTRO.pasa("Ingeniero agrónomo", ciudad="Pergamino, Buenos Aires, Argentina")
    assert FILTRO.pasa("Ingeniero agrónomo", ciudad="Yopal, Casanare")


def _v(id_, cargo, empresa, ciudad=""):
    return {"id": id_, "cargo": cargo, "empresa": empresa, "ciudad": ciudad}


def test_deduplicar_por_id_y_cargo_empresa():
    items = [
        _v("computrabajo-1", "Ingeniero Agrónomo", "Agro S.A.S.", "Yopal"),
        _v("computrabajo-1", "Ingeniero Agrónomo", "Agro S.A.S.", "Yopal"),
        _v("linkedin-9", "ingeniero agronomo", "AGRO SAS", "Villavicencio"),
        _v("elempleo-5", "Ingeniero agrónomo - Yopal", "Agro", "Yopal"),
        _v("elempleo-6", "Ingeniero de campo", "Agro", "Yopal"),
    ]
    out = b.deduplicar(items)
    assert [v["id"] for v in out] == ["computrabajo-1", "elempleo-6"]


def test_confidencial_no_fusiona_ciudades():
    items = [_v("a-1", "Ingeniero agrónomo", "Confidencial", "Yopal"),
             _v("a-2", "Ingeniero agrónomo", "Empresa confidencial", "Neiva")]
    assert len(b.deduplicar(items)) == 2


def test_seleccionar_marca_vistos_y_solo_devuelve_nuevas():
    vistos = {"computrabajo": {"aaa": "2026-09-01"}}
    listados = [b._item("aaa", "Ingeniero agrónomo", "u1", "s"),
                b._item("bbb", "Ingeniero agrónomo arroz", "u2", "s"),
                b._item("bbb", "Ingeniero agrónomo arroz", "u2", "s2"),
                b._item("ccc", "Contador", "u3", "s")]
    cand, n = b.seleccionar("computrabajo", listados, FILTRO, vistos, "2026-09-27")
    assert [c["id"] for c in cand] == ["computrabajo-bbb"]
    assert cand[0]["fuente_busqueda"] == "s | s2"
    assert n == 2
    assert set(vistos["computrabajo"]) == {"aaa", "bbb", "ccc"}


def test_purgar_vistos():
    v = {"linkedin": {"1": "2026-07-01", "2": "2026-09-20"}}
    assert b.purgar_vistos(v, 45, "2026-09-27") == {"linkedin": {"2": "2026-09-20"}}


def test_fechas():
    hoy = b.datetime(2026, 9, 27, tzinfo=b.TZ_CO)
    assert b.fecha_desde_texto("Publicado 12 Sep 2026", hoy).day == 12
    assert b.fecha_desde_texto("hace 3 días", hoy).day == 24
    assert b.fecha_desde_texto("2026-09-20", hoy).day == 20


def test_parse_linkedin():
    html = '''<li><div class="base-card" data-entity-urn="urn:li:jobPosting:4012345678">
    <h3 class="base-search-card__title"> Ingeniero Agrónomo </h3><h4><a>Fedearroz</a></h4>
    <span class="job-search-card__location">Yopal, Casanare, Colombia</span>
    <time datetime="2026-09-25">Hace 2 días</time></div></li>'''
    it = b.parse_linkedin(html, "agrónomo")[0]
    assert it["id_local"] == "4012345678" and it["empresa"] == "Fedearroz"
    assert it["url"] == "https://www.linkedin.com/jobs/view/4012345678"


def test_parse_computrabajo():
    html = '''<article class="box_offer"><h2 class="fs18"><a class="js-o-link fc_base" href="/ofertas-de-trabajo/oferta-de-trabajo-de-ingeniero-agronomo-en-yopal-3F1A2B3C4D5E6F708192A3B4C5D6E7F8#lc=x">Ingeniero agrónomo</a>
    <div class="tags"><span class="tag postulated hide">Postulado</span><span class="tag hide">Vista</span></div></h2>
    <p class="dFlex vm_fx fs16 fc_base mt5"><span class="fx_none mr10"><span class="fwB">4,4</span><span class="star"></span></span>
    <a class="fc_base t_ellipsis" offer-grid-article-company-url="">Arrocera SAS</a></p>
    <p class="fs16 fc_base mt5"><span class="mr10">Yopal, Casanare</span></p>
    <div class="fs13 mt15"><span class="dIB mr10"><span class="icon i_salary"></span>$ 4.000.000,00 (Mensual)</span></div>
    <p class="fs13 fc_aux mt15">Hace 3 horas</p></article>'''
    it = b.parse_computrabajo(html, "x")[0]
    assert it["id_local"] == "3f1a2b3c4d5e6f70"
    assert it["cargo"] == "Ingeniero agrónomo"
    assert (it["empresa"], it["ciudad"]) == ("Arrocera SAS", "Yopal, Casanare")
    assert it["salario"] == "$ 4.000.000,00 (Mensual)" and it["publicada"] == "Hace 3 horas"


def test_parse_kitempleo_y_dedup_por_ciudad_en_parentesis():
    card = '''<a href="https://www.kitempleo.com.co/empleo/{id}/administrador-agropecuario"><div class="blog-three-mini">
    <h3>Administrador agropecuario ({c})</h3><div class="blog-three-attrib visible-lg-block">
    <div><i class="fa fa-calendar"></i> 24 sep</div>|<div><i class="fa fa-pencil"></i> PUNTA DE GARZAS</div>|
    <div><i class="fa fa-map-marker"></i> {c}</div></div></div></a>'''
    html = card.format(id="92070061", c="Cumaribo") + card.format(id="92102202", c="Tequendama) (Colombia")
    its = b.parse_kitempleo(html, "x")
    assert [i["empresa"] for i in its] == ["PUNTA DE GARZAS"] * 2
    assert its[0]["ciudad"] == "Cumaribo" and its[0]["publicada"] == "24 sep"
    vs = [dict(i, id="kitempleo-" + i["id_local"]) for i in its]
    assert len(b.deduplicar(vs)) == 1


def test_parse_pandape():
    html = '''<div id="VacancyList"><a class="card card-vacancy" href="/Detail/13728818"><div class="card-body">
    <h3 class="link" title="Ingeniero agrónomo Comercial">Ingeniero agrónomo Comercial</h3><div class="vacancy-detail"><div class="d-flex">
    <div class="align-middle mr-20"><div class="icon-container align-middle"><i class="icon icon-location-pin-1"></i></div> Valledupar</div>
    <div class="align-middle text-medium"><div class="icon-container align-middle"><i class="icon icon-wallet"></i></div> 5.000.000 $</div>
    </div><div class="vacancy-date">25 sept.</div></div></div></a></div>'''
    it = b.parse_pandape(html, "https://cenipalma.pandape.computrabajo.com", "cenipalma")[0]
    assert (it["id_local"], it["ciudad"], it["salario"], it["publicada"]) == ("13728818", "Valledupar", "5.000.000 $", "25 sept.")
    assert it["url"] == "https://cenipalma.pandape.computrabajo.com/Detail/13728818"


def test_parse_elempleo_json():
    html = '''<div class="result-item"><div data-ga4-offerdata='{"id":1886771490,"title":"Representante tecnico comercial","company":"ACEPALMA","location":"Bucaramanga","salary":"$6 a $8 millones"}'>
    <h2><a class="js-offer-title" href="/co/ofertas-trabajo/representante-tecnico-comercial-1886771490">x</a></h2>
    <span class="js-offer-date">Hace 4 días</span></div></div>'''
    it = b.parse_elempleo(html, "x")[0]
    assert (it["id_local"], it["empresa"], it["ciudad"], it["salario"]) == ("1886771490", "ACEPALMA", "Bucaramanga", "$6 a $8 millones")
