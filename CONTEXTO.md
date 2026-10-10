# Instrucciones para Antigravity — vacantes-agro-robot, mejoras v2 (9-oct-2026)

Contexto: el robot corre a las 5:05 a. m. (cron-job.org → workflow `barrido`), sin IA. Un agente (Claude) lee `data/estado.json` y `data/nuevas.json` y decide qué entra al tablero de Cristian. Con 12 días de uso se midió que de ~15 filas diarias de `nuevas.json`, solo 0–4 sirven. Estas mejoras apuntan a que el robot entregue menos ruido y datos más listos. Trabaja en una rama, abre PR, no cambies el formato actual de `nuevas.json` (solo añade campos). Corre `python -m pytest -q tests` y agrega tests para cada cambio.

## 1. PRIORIDAD ALTA — Sembrar `vistos` con lo que ya revisó el tablero (causa del mayor ruido)

Problema: el robot nació el 27-sep y no conoce lo anterior. Cada día reaparecen como "nuevas" ofertas viejas que Cristian ya descartó (hoy: "Director Agropecuario – Telcos/Manpower" en KitEmpleo, publicada el 8-sep; "Jefe técnico comercial Antioquia – Acepalma", ya descartada; "Líder Agrícola – Gente Útil", 5 versiones ya descartadas).

Tarea:
- Agrega el archivo adjunto `data/semilla_tablero.json` (ids por portal + claves cargo/empresa/ciudad de 181 ofertas ya revisadas).
- Al iniciar `barrido.py`, si existe `data/semilla_tablero.json`, fusiónalo: cada id → `vistos[portal][id]` con fecha de hoy (o la del archivo `generado`), y cada clave `cargo|empresa|ciudad` entra al conjunto de claves vistas, con **la misma normalización** que `clave_vacante()`.
- La semilla NO se purga a los 45 días (usa una sección `fijos` o la fecha de la siembra se renueva en cada corrida). Hoy esas ofertas se "olvidan" a los 45 días y volverían a salir como nuevas.
- Cada cierto tiempo (cuando Cristian lo pida) se reemplazará el archivo por una semilla regenerada; el robot debe tolerar que el archivo cambie.
- Tests: una fila con id nuevo pero misma clave que una de la semilla (p. ej. KitEmpleo "Director Agropecuario (Bogotá)" vs "Director/a Agropecuario (Ing. Agrónomo...)") debe quedar descartada.

## 2. Deduplicación entre portales más robusta

En `clave_repetida`/`clave_vacante`:
- Mismo cargo (o uno es prefijo del otro, ya existe) + empresa **compatible**: tratar como la misma empresa si una contiene a la otra tras quitar sufijos y frases "vía X", "(cliente ...)", "empresa confidencial/importante empresa/reconocida empresa" (empresa confidencial = comodín que casa con cualquiera), y "Manpower/Adecco/Acierta/Michael Page/Zohorecruit/Gente Útil/Supernumerarios/Human One" cuando el nombre real aparece en el cargo o la descripción.
- Para cargos con palabra de rol (líder/jefe/coordinador/director/gerente/asesor) y empresa confidencial, comparar además ciudad y salario cuando existan.
- Los repostajes de KitEmpleo/JobLeads con distinta fecha no son nuevas: si la clave ya existe, no es nueva, aunque el id cambie.

## 3. Fechas normalizadas y vencimientos (para el filtro de fecha de Cristian)

Cristian pidió: publicada ≤30 días y abierta se agrega; >30 días no, salvo cierre futuro explícito. Hoy el robot entrega texto crudo ("Hace 16 horas", "27 sep", "8 Oct 2026") y Claude tiene que traducirlo y abrir el detalle.
- Añade a cada fila de `nuevas.json`: `publicada_iso` (AAAA-MM-DD, o "" si no se puede), `edad_dias` (entero o null), `cierre_iso` (si el detalle trae "fecha de cierre") y `vencida` (true si cierre < hoy). Para fechas sin año ("27 sep"), usa el año en que esa fecha queda en el pasado más reciente. Ya existe `fecha_desde_texto`: reutilízala.
- KitEmpleo: abre el detalle de las candidatas nuevas (como ya haces en elempleo) para leer cierre y fecha real. Hoy se colaron: un aviso con cierre en 2018 (Ofertasynegocios) y otro con cierre 13-ago-2026 (Swiss Forum/CGIAR).
- Descarta en el robot lo `vencida=true` y lo de más de 30 días sin cierre futuro; **pero guárdalo** en `data/descartadas_por_antiguas.json` (id, cargo, empresa, motivo) para auditoría.
- Aplica el límite de antigüedad de forma uniforme (hoy elempleo=15 días, KitEmpleo=30, LinkedIn=7 días vía f_TPR): usa 30 como tope en todos y deja el ordenamiento por fecha.

## 4. Filtro: menos ruido (config.yaml)

Falsos positivos de esta semana: Jefe de Sistemas "para el sector agrícola", Supervisor de personal en empresas de palma, SEO Manager (Simpalm Staffing), Promotor técnico comercial (Novalfarm = veterinaria), Abogado y Analista de laboratorio (Cenipalma), Convocatorias "Ofertasynegocios", Chef comercial (Arroz Federal), Ing. agrícola con postgrado en hidráulica.
- `filtro.excluir` += `sistemas|software|\bseo\b|abogad|contador|contabil|tesorer|recursos humanos|talento humano|reclutad|chef|ferreter|supervisor de personal|promotor|mallas|molienda`
- Nuevo `filtro.excluir_empresa` (regex sobre la empresa): `ofertasynegocios|novalfarm|simpalm|elite flower|supernumerarios`
- Nuevo `filtro.excluir_cargo_empresa`: para empresa con nombre de palma/agro (Cenipalma, Fedepalma…) descartar los cargos que no sean agro/técnicos: `abogad|analista de laboratorio|contab|compras|jur[ií]dic`. No aplicar al portal pandape completo ("todo es agro"): aplicarlo ahí también, pero solo con esta lista corta.
- Si se puede, que el robot lea el texto del detalle (ya lo hace en elempleo) para excluir si la descripción pide "técnico/tecnólogo" sin "profesional/ingeniero".

## 5. Segunda pasada para títulos sin palabra agro (hueco ya documentado)

Casos perdidos que otras fuentes hallaron: Ducol "Líder de Territorio Puntos de Venta" (pide Ing. Agrónomo), Syngenta "Field Trialist", Geosystem "Especialista en Agricultura (drones)", Swiss Forum "Coordinador de Investigación – Yuca".
- Para tarjetas que NO pasan P pero cuyo título contiene `l[ií]der|jefe|coordinador|director|gerente|asesor|representante|ingeniero|especialista|investigador|desarrollista`, abre el detalle (máx. 30 por corrida, 1 s entre peticiones) y vuelve a evaluar P sobre la descripción con una regex más estricta: `ingenier[oa] agr[oó]n|agr[oó]nomo|administrador agropecuario|ingenier[ií]a agron|agropecuari|cultivos? de|palma de aceite|arroz`.
- Marca esas filas con `via_descripcion: true` para que Claude sepa por qué pasaron.

## 6. Campos de ayuda para Claude

Añade a cada fila de `nuevas.json`:
- `perfil_sugerido` (1/2/3) con reglas simples: P2 si título contiene `t[eé]cnico.?comercial|representante|asesor|ventas|comercial`; P3 si `agr[oó]nom|extensi|investigaci|ensayo|auditor`; P1 si `director|jefe|gerente|administrador|coordinador|l[ií]der|proyectos`. Si coinciden varios, el orden P3 > P1 > P2.
- `zona_preferida` (true si la ciudad/departamento es Casanare, Meta, Vichada, Arauca, Cesar, Magdalena, Santander, Nariño, Tolima o Huila).
- `empresa_confidencial` (bool).
- `repost_probable` (true si se detectó la misma clave en `historial.csv` con otro id).
Claude usa estos campos solo para ordenar y comentar, nunca para descartar.

## 7. Fuentes nuevas para que el robot haga lo que Claude hoy hace a mano (ahorra ~25 peticiones diarias)

Todas son páginas estáticas o JSON públicos (probar desde GitHub Actions; si alguna da 403, marcar `bloqueado` como el resto). Guardar una "huella" por fuente (ids + títulos) y reportar solo lo nuevo:
- **Agrohunters** `https://agrohunters.com/ofertas-laborales/` (cada oferta lleva `codigo-NNNN` en la URL; series 28xx = agro Colombia, 5xxx = nutrición animal [no agro], 80xx = exterior [no]). Descarta flores (2839, 2840 lo eran) y nutrición animal.
- **elempleo por empresa** (patrón `/co/ofertas-empleo/<slug>`): acepalma, palmar-del-oriente, grupo-diana, disan-colombia-sas, interoc-sa-sucursal-colombia, federacion-nacional-cafeteros-colombia; y `/co/empleos-empresas/fedearroz/5028`. Reportar ids mayores a la huella guardada.
- **ADAMA** `https://careers.adama.com/search/?q=&locationsearch=Colombia&sortColumn=referencedate&sortDirection=desc` (la huella debe ser la lista de ids, no el conteo).
- **Syngenta** `https://jobs.syngenta.com/api/jobs?country=CO`, **Yara** `https://jobs.yara.com/search/?q=&locationsearch=Colombia`, **Riopaila** `https://trabaja-con-nosotros.riopaila-castilla.com/search/?q=&sortColumn=referencedate&sortDirection=desc`, **Manuelita** (tres subdominios `*.na.teamtailor.com/jobs`), **Fedepalma** `https://fedepalma.org/trabaje-con-nosotros/` (avisos en texto/imagen: reportar solo cambio de huella), **Agrosavia** `https://talento.agrosavia.co/search/`, **FAO/UN** `https://colombia.un.org/es/jobs` (filtrar por palabras agro, forestal y pecuario van aparte).
- **Magneto** `https://www.magneto365.com/co/trabajos/ofertas-empleo-de-ingeniero-agronomo` (y `-administrador-agropecuario`, `-asesor-tecnico-comercial`): listados de Comfama (Antioquia) con ids numéricos.
- **Jooble**: pedir una API key gratuita en `https://jooble.org/api/about` y llamar a su API (POST con la key como secret `JOOBLE_API_KEY`) en vez de scrapear la web que bloquea con Cloudflare.
- Indeed: dejar `activa: false` o seguir marcándolo `bloqueado` (Claude ya tiene su conector oficial de Indeed). No gastar tiempo en este.

Fuentes que NO conviene meter (rendimiento nulo o requieren sesión): JobLeads (necesita login de Cristian), ICA/UPRA/Finagro/ADR/Palmas del Cesar/Pajonales (sin avisos de empleo vigentes desde hace meses; Claude las revisa 1 vez por semana).

## 8. Fiabilidad y trazabilidad

- `estado.json`: añade `version_config` (hash de config.yaml), `semilla_aplicada` (fecha) y, por fuente, `filtradas_por_X`, `filtradas_por_P`, `duplicadas`, `antiguas` para saber dónde se pierde cada día.
- Si `ultima_corrida` no es de hoy a las 8:30 a. m., que el workflow de respaldo ya existente deje además `data/alerta.txt` con el motivo (token de cron-job.org vencido, etc.). Recordatorio: el token fine-grained de cron-job.org vence; anotar la fecha de vencimiento en el README.
- `docs/index.html`: mostrar por tarjeta `edad_dias`, `perfil_sugerido` y un distintivo "posible repost".

## 9. Criterios de aceptación

1. Corrida de prueba con la semilla: las 15 filas del 9-oct deben reducirse a ≤3 (solo "Asesor técnico comercial agrónomo – Tolima, El Espinal" debe sobrevivir como nueva válida).
2. Ninguna fila con `vencida=true` en `nuevas.json`.
3. `pytest` verde; `nuevas.json` mantiene los campos actuales.
4. Cada fuente nueva aparece en `estado.json` con `ok` o `bloqueado` y su huella.
