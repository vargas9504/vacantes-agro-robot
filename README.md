# Robot de vacantes agro

Robot en Python que corre **gratis todos los días en GitHub Actions** (sin IA) y hace la parte mecánica
de la búsqueda de empleo: baja los listados de los portales, filtra por cargo, deduplica y publica
**solo las candidatas nuevas del día**. Así el agente de IA solo tiene que leer unas pocas vacantes.

- Página (GitHub Pages): **https://vargas9504.github.io/vacantes-agro-robot/**
- Nuevas del día (JSON): https://raw.githubusercontent.com/vargas9504/vacantes-agro-robot/main/data/nuevas.json
- Estado de las fuentes: https://raw.githubusercontent.com/vargas9504/vacantes-agro-robot/main/data/estado.json

## Qué hace

Cada día a las **5:17 a. m. (hora Colombia)** el workflow `barrido`:

1. Lee Computrabajo, elempleo, LinkedIn (API pública de invitados), KitEmpleo, Indeed, Pandapé
   (Cenipalma, Poligrow) y Jooble. Cada fuente va por separado: si una falla, se anota y siguen las demás.
2. Aplica el filtro (reglas en `robot/config.yaml`):
   - **X (excluir)**: si el cargo coincide (aprendiz, auxiliar, técnico, flores, junior, etc.) se descarta.
   - **P (incluir)**: solo pasa si el cargo o el texto de la tarjeta tiene algo agro (agrónomo, palma, arroz, finca…).
     En Pandapé (Cenipalma/Poligrow) solo se aplica X.
3. Descarta lo ya visto (por id de cada portal y por cargo+empresa normalizados, para no repetir la misma
   vacante publicada en varias ciudades o portales).
4. Guarda y hace commit de:

| Archivo | Contenido |
|---|---|
| `data/nuevas.json`, `data/nuevas.csv` | Solo las candidatas nuevas de esta corrida |
| `data/historial.csv` | Acumulado de todas las candidatas (se agrega al final) |
| `data/vistos.json` | Ids ya vistos por portal con la fecha de la primera vez (se olvidan a los 45 días) |
| `data/estado.json` | Por fuente: `ok` / `error` / `bloqueado` / `vacio`, listados leídos, candidatas, nuevas, duración, error. Además `ultima_corrida` y `corrida_inicial` |
| `docs/index.html` | Página para el celular: estado de fuentes, tarjetas de nuevas y las de los últimos 7 días |

Campos de cada candidata: `id` (`<portal>-<id>`), `portal`, `cargo`, `empresa`, `ciudad`, `publicada`,
`salario`, `url`, `fuente_busqueda`, `encontrada` (AAAA-MM-DD HH:MM hora Colombia).

> **Primera corrida**: como aún no existe `vistos.json`, todo sale como "nuevo". Por eso `estado.json`
> trae `"corrida_inicial": true`: esa corrida no se debe leer como "hay 100 vacantes nuevas".
> Igual pasa con una fuente que funciona por primera vez: aparece en `fuentes_iniciales`.

## Estado de las fuentes (probado desde GitHub Actions, sept. 2026)

| Fuente | Funciona | Notas |
|---|---|---|
| Computrabajo | ✅ | ~40 búsquedas, ~500 listados, 1 min |
| elempleo | ✅ | Datos del listado (JSON `data-ga4-offerdata`) + detalle de las candidatas nuevas (fecha, salario); descarta > 15 días |
| LinkedIn | ✅ | API de invitados, ~180 llamadas, ~4,5 min |
| KitEmpleo | ✅ | Muchos repetidos: se deduplica por cargo+empresa (quitando "(Ciudad)") |
| Pandapé (Cenipalma, Poligrow) | ✅ | Incluye "Ver 20 ofertas más" (`POST /ListVacancies`) |
| Indeed | ❌ bloqueado | Responde 403 "Security Check" a las IPs de GitHub; queda como `bloqueado` |
| Jooble | ❌ bloqueado | Cloudflare "Just a moment..." (403); queda como `bloqueado` |

## Para el agente de IA

1. Leer `data/estado.json`. Si `corrida_inicial` es `true`, ignorar `nuevas.json` de ese día
   (o tratarlo como línea base). Si una fuente aparece en `fuentes_iniciales`, sus nuevas son la línea base.
2. Leer `data/nuevas.json` (lista, normalmente pocas) y revisar solo esas.
3. Las fuentes con `estado` distinto de `ok` hay que barrerlas a mano ese día.

## Horario y respaldos

GitHub no garantiza la hora exacta de las tareas programadas: en las horas en punto y a las y media hay
tanta congestión que a veces se retrasan o no se disparan (y en un repositorio recién creado la primera
puede no ocurrir). Por eso el cron usa minutos poco comunes y tiene dos respaldos:

| Hora Colombia | Cron (UTC) | Papel |
|---|---|---|
| 5:17 a. m. | `17 10 * * *` | principal |
| 6:43 a. m. | `43 11 * * *` | respaldo |
| 8:07 a. m. | `7 13 * * *` | último respaldo |

Un respaldo solo corre si `data/estado.json` no tiene ya una corrida de hoy; si la hay, termina en
segundos sin tocar nada (así no reemplaza `nuevas.json` por una lista vacía). "Run workflow" a mano
siempre corre. Para el agente: comparar la fecha de `ultima_corrida` con la de hoy antes de confiar en `nuevas.json`.

## Correrlo a mano

- En GitHub: pestaña **Actions → barrido → Run workflow**. Se puede poner en `solo` una lista de fuentes
  (por ejemplo `linkedin,computrabajo`) y marcar `depurar` para guardar el HTML crudo en `debug/`.
- En tu computador:

```bash
pip install -r requirements.txt
python robot/barrido.py                      # todas las fuentes
python robot/barrido.py --solo linkedin      # solo una
python -m pytest -q tests                    # tests del filtro y la deduplicación
```

## Cambiar términos o reglas

Todo está en `robot/config.yaml`, no hace falta tocar el código:

- `filtro.excluir` / `filtro.incluir`: las regex X y P (sin distinguir mayúsculas).
- `filtro.excluir_ubicacion`: países/ciudades a descartar (KitEmpleo mezcla avisos de Argentina).
- `fuentes.<portal>`: slugs, términos, regiones, número de páginas, `activa: false` para apagar una fuente.
- `general.purgar_vistos_dias`, `general.dias_pagina`, `general.intervalo_dominio` (ritmo entre peticiones).

Edita el archivo en GitHub (lápiz ✏️), haz commit y la próxima corrida ya usa las reglas nuevas.

## GitHub Pages

La página se publica con GitHub Actions desde `docs/`. Si no aparece, activa una vez
**Settings → Pages → Build and deployment → Source: GitHub Actions**.

## Buenas prácticas

User-Agent de navegador, timeout de 30 s, reintentos con espera creciente y como máximo ~1 petición
por segundo por dominio (LinkedIn 1,5 s y espera de 6 s ante un 429). Solo páginas públicas, sin iniciar sesión.
