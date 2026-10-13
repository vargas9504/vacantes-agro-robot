import json
import os
from pathlib import Path
import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "robot"))
import barrido as b

FILTRO = b.Filtro(b.cargar_config()["filtro"])


# ---------------------------------------------------------------------------
# 1. Filtro: excluir, excluir_empresa, excluir_cargo_empresa
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cargo,empresa,esperado", [
    # Exclusiones de cargo nuevas
    ("Jefe de Sistemas", "Agropecuaria SAS", False),
    ("Desarrollador de software", "Cultivos del Llano", False),
    ("SEO Manager", "Agro Digital", False),
    ("Abogado corporativo", "Hacienda La Palma", False),
    ("Contador general", "Agroinsumos del Meta", False),
    ("Auxiliar de tesorería", "Palmar", False),
    ("Director de recursos humanos", "Agro", False),
    ("Analista de talento humano", "Semillas SAS", False),
    ("Reclutador de campo", "Flores del Campo", False),
    ("Chef comercial", "Arroz Federal", False),
    ("Vendedor de ferretería agrícola", "Ferretería", False),
    ("Supervisor de personal", "Palma de Aceite", False),
    ("Promotor técnico comercial", "Veterinaria", False),
    ("Operario de mallas", "Agro", False),
    ("Operario de molienda", "Molino", False),
    # Exclusiones por empresa
    ("Ingeniero Agrónomo", "Ofertasynegocios", False),
    ("Asesor técnico", "Novalfarm", False),
    ("Líder de campo", "Simpalm Staffing", False),
    ("Ingeniero agrícola", "Elite Flower", False),
    ("Coordinador agronómico", "Supernumerarios", False),
    # Exclusiones por cargo en empresa palmera / agro (Cenipalma, Fedepalma, etc.)
    ("Abogado júnior", "Cenipalma", False),
    ("Analista de laboratorio", "Cenipalma", False),
    ("Auxiliar contable", "Fedepalma", False),
    ("Coordinador de compras", "Fedearroz", False),
    ("Asesor jurídico", "Agrosavia", False),
    # Casos agro válidos en esas mismas empresas deben PASAR
    ("Investigador de palma", "Cenipalma", True),
    ("Agrónomo extensionista", "Fedepalma", True),
    ("Ingeniero agrónomo", "Fedearroz", True),
    ("Investigador agrícola", "Agrosavia", True),
])
def test_filtro_nuevas_reglas(cargo, empresa, esperado):
    assert FILTRO.pasa(cargo, empresa=empresa, aplicar_p=True) is esperado


# ---------------------------------------------------------------------------
# 2. Deduplicación entre portales (reposts de ADAMA / KitEmpleo con distinto id)
# ---------------------------------------------------------------------------

def test_dedupe_repost_adama_kitempleo():
    # Suponemos que ADAMA publicó la vacante original
    k_adama = b.clave_vacante("Representante Técnico Comercial", "ADAMA", "Ibagué, Tolima")
    claves = {k_adama: "2026-10-10"}

    # KitEmpleo la repostea con otro ID, título extendido o sufijo de empresa
    k_repost1 = b.clave_vacante("Representante Técnico Comercial - ADAMA", "ADAMA Colombia", "Ibagué")
    assert b.clave_repetida(k_repost1, claves)

    # KitEmpleo la repostea con empresa 'Kit Empleo' o 'Confidencial'
    k_repost2 = b.clave_vacante("Representante Técnico Comercial - ADAMA", "Kit Empleo", "Ibagué")
    assert b.clave_repetida(k_repost2, claves)


def test_dedupe_items_entre_portales():
    items = [
        {"id": "adama-1001", "cargo": "Representante Técnico Comercial", "empresa": "ADAMA", "ciudad": "Ibagué"},
        {"id": "kitempleo-9999", "cargo": "Representante Técnico Comercial - ADAMA", "empresa": "Kit Empleo", "ciudad": "Ibagué"},
        {"id": "computrabajo-555", "cargo": "Representante Técnico Comercial", "empresa": "ADAMA Andina SAS", "ciudad": "Ibagué, Tolima"},
    ]
    unicos = b.deduplicar(items)
    # Solo debe quedar la primera oferta
    assert len(unicos) == 1
    assert unicos[0]["id"] == "adama-1001"


# ---------------------------------------------------------------------------
# 3. Jooble API y manejo de key
# ---------------------------------------------------------------------------

def test_jooble_sin_api_key_lanza_bloqueado(monkeypatch):
    monkeypatch.delenv("JOOBLE_API_KEY", raising=False)
    ctx = {"cfg": {"rutas": ["/trabajo-agronomo"]}, "http": None}
    with pytest.raises(b.Bloqueado) as exc:
        b.fuente_jooble(ctx)
    assert "JOOBLE_API_KEY" in str(exc.value)


def test_jooble_con_api_key(monkeypatch):
    monkeypatch.setenv("JOOBLE_API_KEY", "dummy_key_123")

    class FakeHttp:
        def post(self, url, json=None, **kw):
            assert "dummy_key_123" in url
            class FakeResp:
                status_code = 200
                def json(self):
                    return {
                        "jobs": [
                            {
                                "id": "12345",
                                "title": "Ingeniero Agrónomo",
                                "company": "Finca SAS",
                                "location": "Yopal",
                                "salary": "$4.500.000",
                                "updated": "2026-10-12",
                                "snippet": "Cultivo de arroz",
                                "link": "https://jooble.org/desc/12345"
                            }
                        ]
                    }
            return FakeResp()

    ctx = {"cfg": {"rutas": ["/trabajo-ingeniero-agronomo"]}, "http": FakeHttp()}
    items = b.fuente_jooble(ctx)
    assert len(items) == 1
    assert items[0]["id_local"] == "12345"
    assert items[0]["cargo"] == "Ingeniero Agrónomo"
    assert items[0]["tarjeta"] == "Cultivo de arroz"


# ---------------------------------------------------------------------------
# 4. Parsers de las 11 fuentes nuevas
# ---------------------------------------------------------------------------

def test_parse_agrohunters():
    html = '''
    <article class="post-1">
        <h3>Responsable comercial y mercadeo de frutas - Código 2849</h3>
        <a href="https://agrohunters.com/oferta-laboral/frutas-codigo-2849/">Ver oferta</a>
        <div class="ubicacion">Antioquia, Colombia</div>
    </article>
    <article class="post-2">
        <h3>Nutrición animal bovinos - Código 2846</h3>
        <a href="https://agrohunters.com/oferta-laboral/nutricion-codigo-2846/">Ver oferta</a>
    </article>
    <article class="post-3">
        <h3>Supervisor de rosas y flores - Código 2839</h3>
        <a href="https://agrohunters.com/oferta-laboral/flores-codigo-2839/">Ver oferta</a>
    </article>
    <article class="post-4">
        <h3>Ventas México - Código 8012</h3>
        <a href="https://agrohunters.com/oferta-laboral/mexico-codigo-8012/">Ver oferta</a>
    </article>
    '''
    items = b.parse_agrohunters(html)
    # Solo debe quedar la oferta agro Colombia válida (2849)
    assert len(items) == 1
    assert items[0]["id_local"] == "2849"
    assert "Responsable comercial" in items[0]["cargo"]
    assert items[0]["empresa"] == "Agrohunters"


def test_parse_adama():
    html = '''
    <tr class="data-row">
        <td><a class="jobTitle-link" href="/job/Bogota-Ingeniero-Desarrollo-145020/145020/">Ingeniero de Desarrollo</a></td>
        <td><span class="jobLocation">Bogotá, Colombia</span></td>
        <td><span class="jobDate">Oct 10, 2026</span></td>
    </tr>
    '''
    items = b.parse_adama(html)
    assert len(items) == 1
    assert items[0]["id_local"] == "145020"
    assert items[0]["cargo"] == "Ingeniero de Desarrollo"
    assert items[0]["empresa"] == "ADAMA"
    assert items[0]["ciudad"] == "Bogotá, Colombia"


def test_parse_magneto():
    html = '''
    <div class="card">
        <a href="/co/empleos/ingenieroa-agronomoa-de-campo-1626506756-12-1015102">Ingeniero/a Agrónomo/a de campo</a>
        <p>Servicio de Empleo Comfama | $ 5.500.000 | Medellín, Antioquia</p>
    </div>
    '''
    items = b.parse_magneto(html, "agronomo")
    assert len(items) == 1
    assert items[0]["id_local"] == "1015102"
    assert items[0]["cargo"] == "Ingeniero/a Agrónomo/a de campo"
    assert items[0]["empresa"] == "Comfama"
    assert items[0]["salario"] == "$ 5.500.000"


def test_parse_magneto_otra_empresa():
    html = '''
    <div class="card">
        <a href="/co/empleos/ingenieroa-agronomoa-1626506756-12-1015105">Ingeniero/a Agrónomo/a</a>
        <p>Ducol Group SAS | $ 4.000.000 | Medellín</p>
    </div>
    '''
    items = b.parse_magneto(html, "agronomo")
    assert len(items) == 1
    assert items[0]["id_local"] == "1015105"
    assert items[0]["cargo"] == "Ingeniero/a Agrónomo/a"
    assert items[0]["empresa"] == "Ducol Group SAS"
    assert items[0]["empresa"] != "Comfama"
    assert items[0]["salario"] == "$ 4.000.000"


def test_parse_magneto_empresa_no_indicada():
    html = '''
    <div class="card">
        <a href="/co/empleos/ingenieroa-agronomoa-1626506756-12-1015106">Ingeniero/a Agrónomo/a</a>
        <p>Medellín, Antioquia</p>
    </div>
    '''
    items = b.parse_magneto(html, "agronomo")
    assert len(items) == 1
    assert items[0]["empresa"] == "Magneto (empresa no indicada)"


def test_parse_fao_un():
    html = '''
    <div class="views-row">
        <span>02 octubre 2026</span>
        <a href="https://fao.taleo.net/careersection/fao_external/jobdetail.ftl?job=2602062">Profesional misional en producción agrícola</a>
        <span>Fecha de cierre | 16 octubre 2026</span>
        <span>FAO: Food and Agriculture Organization</span>
    </div>
    <div class="views-row">
        <span>05 octubre 2026</span>
        <a href="https://fao.taleo.net/careersection/fao_external/jobdetail.ftl?job=2602099">Analista financiero de sistemas</a>
        <span>Fecha de cierre | 20 octubre 2026</span>
    </div>
    '''
    items = b.parse_fao_un(html)
    # Solo debe quedar la oferta agro (2602062)
    assert len(items) == 1
    assert items[0]["id_local"] == "2602062"
    assert items[0]["cargo"] == "Profesional misional en producción agrícola"
    assert items[0]["empresa"] == "FAO"
    assert items[0]["_cierre_texto"] == "16 octubre 2026"


def test_parse_fao_un_descarte_fao_no_agro():
    html = '''
    <div class="views-row">
        <span>01 octubre 2026</span>
        <a href="https://fao.taleo.net/careersection/fao_external/jobdetail.ftl?job=2602088">Profesional especializado en Power Platform y Analítica de Datos</a>
        <span>Fecha de cierre | 18 octubre 2026</span>
        <span>FAO: Food and Agriculture Organization</span>
    </div>
    <div class="views-row">
        <span>02 octubre 2026</span>
        <a href="https://fao.taleo.net/careersection/fao_external/jobdetail.ftl?job=2602062">Profesional misional en producción agrícola</a>
        <span>Fecha de cierre | 16 octubre 2026</span>
        <span>FAO: Food and Agriculture Organization</span>
    </div>
    '''
    items = b.parse_fao_un(html)
    # Oferta TI de FAO descartada; oferta agro pasa
    assert len(items) == 1
    assert items[0]["id_local"] == "2602062"
    assert items[0]["cargo"] == "Profesional misional en producción agrícola"
    assert items[0]["empresa"] == "FAO"
    assert not any("Power Platform" in it["cargo"] for it in items)


def test_parse_fao_un_empresa_onu():
    html = '''
    <div class="views-row">
        <a href="https://colombia.un.org/es/jobs/12345">Especialista en Desarrollo Rural Sostenible</a>
        <span>Naciones Unidas Colombia</span>
    </div>
    '''
    items = b.parse_fao_un(html)
    assert len(items) == 1
    assert items[0]["empresa"] == "ONU Colombia"


def test_parse_fedepalma():
    html = '''
    <div class="contSlideVacante">
        <h3>Investigador Asociado I-Líder de Mecanización</h3>
        <p>Postúlate antes del 15 de Octubre 2026</p>
        <p>Zona Oriental | Investigación y Extensión</p>
        <a href="https://fedepalma.org/wp-content/uploads/2026/09/vacante-inv-scaled.jpg">Más información</a>
    </div>
    '''
    items = b.parse_fedepalma(html)
    assert len(items) == 1
    assert items[0]["id_local"] == "vacante-inv-scaled"
    assert items[0]["cargo"] == "Investigador Asociado I-Líder de Mecanización"
    assert items[0]["empresa"] == "Fedepalma"
    assert items[0]["ciudad"] == "Zona Oriental"
    assert items[0]["_cierre_texto"] == "15 de Octubre 2026"


def test_parse_agrosavia():
    html_empty = '<p>En este momento, no hay ningún cargo vacante acorde a sus preferencias</p>'
    assert b.parse_agrosavia(html_empty) == []

    html_jobs = '''
    <tr class="data-row">
        <td><a class="jobTitle-link" href="/job/Mosquera-Investigador-Master/123999/">Investigador Máster</a></td>
        <td><span class="jobLocation">Mosquera, Cundinamarca</span></td>
        <td><span class="jobDate">Oct 11, 2026</span></td>
    </tr>
    '''
    items = b.parse_agrosavia(html_jobs)
    assert len(items) == 1
    assert items[0]["id_local"] == "123999"
    assert items[0]["cargo"] == "Investigador Máster"
    assert items[0]["empresa"] == "Agrosavia"


def test_parse_syngenta():
    data = {
        "jobs": [
            {
                "id": "744000152520560",
                "title": "Field Trialist",
                "city": "Villavicencio",
                "published": "29.09.2026",
                "department": "Development",
                "url": "https://jobs.smartrecruiters.com/syngenta/trialist"
            }
        ]
    }
    items = b.parse_syngenta(data)
    assert len(items) == 1
    assert items[0]["id_local"] == "744000152520560"
    assert items[0]["cargo"] == "Field Trialist"
    assert items[0]["empresa"] == "Syngenta"
    assert items[0]["ciudad"] == "Villavicencio"


def test_parse_yara():
    html = '''
    <tr class="data-row">
        <td><a class="jobTitle-link" href="/job/Cartagena-Agronomist-Advisor/1445999/">Agronomist Advisor</a></td>
        <td><span class="jobLocation">Cartagena, Bolívar</span></td>
        <td><span class="jobDate">Oct 08, 2026</span></td>
    </tr>
    '''
    items = b.parse_yara(html)
    assert len(items) == 1
    assert items[0]["id_local"] == "1445999"
    assert items[0]["cargo"] == "Agronomist Advisor"
    assert items[0]["empresa"] == "Yara"


def test_parse_riopaila():
    html = '''
    <li class="job-tile job-id-1334025262">
        <a class="jobTitle-link" href="/job/Soldador-I-Agricola/1334025262/">Soldador I Agrícola Especial</a>
        <span class="date">12 oct 2026</span>
    </li>
    '''
    items = b.parse_riopaila(html)
    assert len(items) == 1
    assert items[0]["id_local"] == "1334025262"
    assert items[0]["cargo"] == "Soldador I Agrícola Especial"
    assert items[0]["empresa"] == "Riopaila Castilla"
    assert items[0]["publicada"] == "12 oct 2026"


def test_parse_manuelita():
    html = '''
    <li>
        <a href="https://manuelitaaceitesyenergia.na.teamtailor.com/jobs/716985-operario-inspeccion">Operario Inspección y Ensayo</a>
        <span>San Carlos de Guaroa, Meta</span>
    </li>
    '''
    items = b.parse_manuelita(html, "https://manuelitaaceitesyenergia.na.teamtailor.com/jobs", "manuelitaaceitesyenergia")
    assert len(items) == 1
    assert items[0]["id_local"] == "716985"
    assert items[0]["cargo"] == "Operario Inspección y Ensayo"
    assert items[0]["empresa"] == "Manuelita"


# ---------------------------------------------------------------------------
# 5. Huella y orden de fuentes en FUENTES
# ---------------------------------------------------------------------------

def test_fuentes_orden_exacto():
    keys = list(b.FUENTES.keys())
    idx_agrohunters = keys.index("agrohunters")
    esperado_orden = [
        "agrohunters", "adama", "magneto", "elempleo_empresa",
        "fao_un", "fedepalma", "agrosavia", "syngenta", "yara",
        "riopaila", "manuelita"
    ]
    assert keys[idx_agrohunters:idx_agrohunters + len(esperado_orden)] == esperado_orden
