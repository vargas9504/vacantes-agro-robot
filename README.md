# Robot de vacantes agro

Robot en Python que corre **gratis todos los días en GitHub Actions** (sin IA) y hace la parte mecánica
de la búsqueda de empleo: baja los listados de los portales, filtra por cargo, deduplica y publica
**solo las candidatas nuevas del día**. Así el agente de IA solo tiene que leer unas pocas vacantes.

- Página (GitHub Pages): **https://vargas9504.github.io/vacantes-agro-robot/**
- Nuevas del día (JSON): https://raw.githubusercontent.com/vargas9504/vacantes-agro-robot/main/data/nuevas.json
- Estado de las fuentes: https://raw.githubusercontent.com/vargas9504/vacantes-agro-robot/main/data/estado.json

## Qué hace

Cada día a las **5:30 a. m. (hora Colombia)** el workflow `barrido`:

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

## Para el agente de IA

1. Leer `data/estado.json`. Si `corrida_inicial` es `true`, ignorar `nuevas.json` de ese día
   (o tratarlo como línea base). Si una fuente aparece en `fuentes_iniciales`, sus nuevas son la línea base.
2. Leer `data/nuevas.json` (lista, normalmente pocas) y revisar solo esas.
3. Las fuentes con `estado` distinto de `ok` hay que barrerlas a mano ese día.

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
