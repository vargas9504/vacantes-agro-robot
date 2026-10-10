import sys, json, re
from pathlib import Path
sys.path.insert(0, str(Path("robot").resolve()))
import barrido as b
import datetime

cfg = b.cargar_config()
filtro = b.Filtro(cfg["filtro"])

semilla = json.load(open("data/semilla_tablero.json", encoding="utf8"))
claves_semilla = {}
for cl in semilla.get("claves", []):
    k = b.clave_vacante(cl.get("cargo", ""), cl.get("empresa", ""), cl.get("ciudad", ""), cl.get("salario", ""))
    claves_semilla[k] = "2026-10-09"

nuevas_oct9 = json.load(open("data/nuevas_oct9.json", encoding="utf8"))
inicio = datetime.datetime.fromisoformat("2026-10-09T05:00:00-05:00")

sobreviven = []
for it in nuevas_oct9:
    # 1. Filtro normal de seleccion
    if not filtro.pasa(it["cargo"], it.get("tarjeta",""), it.get("ciudad",""), True, it.get("empresa","")):
        continue
        
    # 2. Dedupe con semilla
    k = b.clave_vacante(it["cargo"], it.get("empresa", ""), it.get("ciudad", ""), it.get("salario", ""))
    if b.clave_repetida(k, claves_semilla):
        continue

    # 3. Fechas
    f_pub = b.fecha_desde_texto(it.get("publicada") or "", inicio)
    it["publicada_iso"] = f_pub.strftime("%Y-%m-%d") if f_pub else ""
    it["edad_dias"] = (inicio - f_pub).days if f_pub else None
    
    # Simulate the closure dates from detail that were leaked on Oct 9th
    if "Ofertasynegocios" in (it.get("empresa") or ""):
        f_cierre = b.fecha_desde_texto("10 ene 2018", inicio)
    elif "Swiss Forum" in (it.get("empresa") or ""):
        f_cierre = b.fecha_desde_texto("13 ago 2026", inicio)
    else:
        f_cierre = b.fecha_desde_texto(it.get("_cierre_texto") or "", inicio)
    
    it["cierre_iso"] = f_cierre.strftime("%Y-%m-%d") if f_cierre else ""
    it["vencida"] = bool(f_cierre and f_cierre.date() < inicio.date())
    
    if it["vencida"] or (it["edad_dias"] is not None and it["edad_dias"] > 30 and not f_cierre):
        continue
        
    sobreviven.append(it)

print(f"Sobreviven: {len(sobreviven)}/15")
for x in sobreviven:
    print(f"- {x['cargo']} ({x.get('empresa')})")
