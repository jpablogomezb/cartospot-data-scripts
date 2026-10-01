# CartoSpot Dataset Export v3

Utilidad de línea de comandos para exportar datasets geolocalizados de tipo **Place** desde la API de CartoSpot. Descarga los lugares, guarda sus geometrías y atributos y, opcionalmente, exporta un conjunto de **Submissions** asociado a cada lugar.

> **Comportamiento por defecto:** exporta los Places, sus adjuntos directos y los archivos GeoJSON/CSV/XLSX. **No consulta ni descarga Submissions** salvo que se especifique `--submission-set`.

## Requisitos

- Python 3.9 o superior.
- Acceso HTTP(S) al endpoint de la API de CartoSpot.
- Dependencias: `requests`, `pandas` y `openpyxl`.
- El módulo local `project_paths.py`, importable desde el script, con la función `outputs_dir()`.

### Organización recomendada

```text
cartospot-scripts/
├── src/
│   ├── cartospot_dataset_export_v3.py
│   └── project_paths.py
├── outputs/
│   └── exports/
└── .venv/
```

Con el `project_paths.py` habitual del proyecto:

```python
from pathlib import Path


def project_root() -> Path:
    here = Path(__file__).resolve().parent
    if here.name == "src":
        return here.parent
    return here


def outputs_dir(*parts: str) -> Path:
    path = project_root().joinpath("outputs", *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
```

Cuando los dos archivos Python se encuentran en `src/`, la salida predeterminada es `<raíz-del-proyecto>/outputs/exports/`. El argumento `--output-dir` permite utilizar otra ruta.

## Instalación

Desde la raíz del proyecto:

```bash
python3 -m venv .venv
source .venv/bin/activate    # macOS / Linux
python -m pip install requests pandas openpyxl
```

Si ya dispones de un entorno virtual y estas dependencias, reutilízalos.

## Uso rápido

**Importante:** pasa la **URL del dataset**, no la del mapa web. El script acepta también una URL terminada en `/places` y elimina ese sufijo para consultar primero la información del dataset.

### 1. Exportar Places y fotografías, sin Submissions

Ejemplo: *Loughlinstown Memory Map*.

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map"
```

Esto obtiene todos los Places que la API devuelve, conserva las geometrías y propiedades de cada lugar, descarga los adjuntos directos disponibles y genera los archivos GeoJSON, CSV y Excel. **No descarga Submissions.**

### 2. Exportar Places y un conjunto de Submissions

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --submission-set contributions
```

`contributions` es un **ejemplo**: debe sustituirse por el nombre real de un `SubmissionSet` asociado a los Places del dataset. Cuando lo indicas, el exportador también consulta las URLs de ese conjunto por Place, genera los CSV/XLSX de las respuestas y descarga sus adjuntos. Si un Place no contiene ese conjunto, se exporta igualmente su fila de resumen.

Para inspeccionar los conjuntos visibles en el primer lugar (requiere `jq`):

```bash
curl -fsSL \
  "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map/places" \
  | jq '.features[0].properties.submission_sets'
```

El primer lugar podría no tener todos los conjuntos disponibles en otros lugares: inspecciona más elementos de `features` si es necesario.

### 3. Exportar datos sin descargar ningún adjunto

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --skip-place-attachments
```

Sin `--submission-set`, este comando ya evita las Submissions; `--skip-place-attachments` omite además los archivos adjuntos directos. Las URLs originales permanecen en los datos exportados.

Si sí exportas Submissions y quieres omitir *ambos* tipos de adjuntos:

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal" \
  --submission-set contributions \
  --skip-place-attachments \
  --skip-attachment-download
```

### 4. Elegir una carpeta de destino

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --output-dir "./backups"
```

El script crea dentro del directorio indicado una carpeta propia con el formato `<dataset-slug>_<dataset-id>`.

### 5. Reutilizar o volver a descargar fotografías

En ejecuciones posteriores sobre la misma carpeta, reutiliza archivos de adjuntos ya existentes cuyo nombre coincida y tengan tamaño mayor que cero. Esto **no verifica** que su contenido sea idéntico al original.

Para forzar una nueva descarga de adjuntos:

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map" \
  --force
```

Los archivos tabulares y JSON se vuelven a generar en cada ejecución.

## Parámetros

| Argumento | Descripción |
| --- | --- |
| `--dataset-url URL` | **Obligatorio.** Endpoint de un dataset de CartoSpot, normalmente sin `/places`. |
| `--submission-set NAME` | **Opcional.** Exporta únicamente el conjunto de Submissions indicado para cada Place. Si se omite, no se consultan Submissions. |
| `--output-dir PATH` | Directorio raíz de exportación. Predeterminado: `outputs_dir("exports")` definido en `project_paths.py`. |
| `--skip-place-attachments` | No descarga adjuntos directamente asociados a los Places. |
| `--skip-attachment-download` | No descarga adjuntos de Submissions; no impide la descarga de adjuntos directos de Place. |
| `--force` | Descarga nuevamente los adjuntos, aunque existan archivos locales. |
| `--delay-seconds N` | Pausa de `N` segundos después de cada solicitud; por defecto `0`. |
| `--hide-noisy-fields` | Excluye algunos campos técnicos conocidos (como `csrfmiddlewaretoken`) de las columnas dinámicas tabulares; **no** modifica el GeoJSON. |
| `--api-token TOKEN` | Token opcional para peticiones autenticadas; se envía como `Authorization: Token TOKEN`. |
| `--help` | Muestra la ayuda del programa. |

También puedes consultar la ayuda directamente:

```bash
python src/cartospot_dataset_export_v3.py --help
```

## Estructura de salida

Ejemplo orientativo para un dataset con un Place de ID `123` (el ID del dataset y los nombres reales dependen de la API):

```text
outputs/
└── exports/
    └── loughlinstown-memory-map_<dataset-id>/
        ├── places.geojson
        ├── loughlinstown-memory-map_<dataset-id>_places_summary.csv
        ├── loughlinstown-memory-map_<dataset-id>_places_summary.xlsx
        ├── attachments_manifest.json
        ├── backup_report.json
        ├── api_pages/
        │   ├── page_0001.json
        │   └── ...
        └── loughlinstown-memory-map_<dataset-id>_place_123_<name>/
            ├── attachments/
            │   └── place_123_attachment_1_<hash>.jpg
            ├── submission_attachments/            # Solo cuando corresponda
            │   └── ...
            ├── ..._contributions.csv              # Solo con --submission-set
            └── ..._contributions.xlsx             # Solo con --submission-set
```

### Archivos principales

- **`places.geojson`**: una `FeatureCollection` que agrega las `features` originales obtenidas de todas las páginas. Conserva sus `geometry` y `properties` sin convertirlas a columnas; las respuestas de página completas, incluida su `metadata`, se almacenan separadamente en `api_pages/`.
- **`api_pages/page_XXXX.json`**: respuesta JSON de cada página de Places, para consulta o auditoría.
- **`*_places_summary.csv` y `.xlsx`**: una fila por Place, con coordenadas separadas (`longitude`, `latitude`), metadatos, adjuntos y campos personalizados prefijados con `place_custom__`. El libro Excel incluye hojas `data` y `metadata`.
- **`attachments/`**: archivos asociados directamente a `properties.attachments` de un Place.
- **`*_contributions.csv` y `.xlsx`** (o el nombre del conjunto seleccionado): una fila por Submission de ese conjunto, con propiedades personalizadas prefijadas con `submission_custom__` y referencias a sus adjuntos.
- **`submission_attachments/`**: archivos asociados a las Submissions, cuando se solicita su descarga.
- **`attachments_manifest.json`**: registro de los adjuntos descargados o reutilizados, indicando tipo (`place` o `submission`), identificadores, URL de origen, ruta local y estado.
- **`backup_report.json`**: resumen del dataset, número de Places, conjunto de Submissions elegido, número de archivos registrados y errores de descarga de adjuntos.

Las URLs originales de fotografías se conservan en el GeoJSON y los CSV/XLSX; el archivo de manifiesto añade las rutas locales.

## Autenticación

Para endpoints que requieren autenticación, puedes usar `--api-token`, pero es preferible evitar escribir credenciales en comandos que queden en el historial del terminal. La v3 también acepta la variable de entorno `CARTOSPOT_API_TOKEN`:

```bash
export CARTOSPOT_API_TOKEN="TU_TOKEN"
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/meitheal/datasets/loughlinstown-memory-map"
```

El token no implica por sí solo que se incluyan registros privados o invisibles: el resultado depende de los permisos y de lo que devuelva la API. Este script no añade parámetros específicos para solicitar datos privados o invisibles.

## Estado de ejecución y errores

El proceso muestra el número de lugares encontrados y los archivos exportados. Si falla la descarga de algún adjunto, registra el fallo en `backup_report.json` y continúa con los demás adjuntos.

Códigos de salida de la v3:

- `0`: exportación completada sin errores de descarga de adjuntos registrados.
- `1`: error general o HTTP que impide completar la exportación.
- `2`: exportación completada, pero uno o más adjuntos no pudieron descargarse.

La descarga se realiza con reintentos HTTP, streaming y archivos temporales `.part`, que se sustituyen por el archivo definitivo al finalizar. La extensión se determina mediante la firma del archivo, la cabecera `Content-Type` o, en último término, la URL; los formatos no identificables pueden quedar como `.bin`.

## Alcance y limitaciones

- Es un **exportador**: no modifica los registros remotos de CartoSpot.
- Exporta los Places **que el endpoint entrega**. No es una exportación automática de todos los registros de la base de datos, incluidos los privados o invisibles.
- `--submission-set` exporta **un conjunto por ejecución**; no selecciona automáticamente todos los conjuntos existentes.
- Los adjuntos directos de Place y los de Submission se descargan por separado. No se recorren arbitrariamente todos los enlaces que puedan existir dentro de respuestas de formularios.
- El GeoJSON agrega las entidades originales de las páginas; para conservar el JSON íntegro de cada respuesta HTTP utiliza `api_pages/`.
- El formato CSV/XLSX facilita el análisis, pero no reemplaza el GeoJSON como representación geográfica completa.
- La exportación no incluye por sí sola una copia íntegra de la configuración Django de la aplicación (por ejemplo, todos los modelos `PlaceType` o `PlaceQuestion`).
- El manifiesto informa de errores de descarga de adjuntos, pero no verifica mediante checksum la integridad de archivos reutilizados.
- Si los datos incluyen usuarios, comentarios o información personal, conserva las copias exportadas en una ubicación con los permisos adecuados.

## Ejemplo adicional: SCORE Photo Booth

```bash
python src/cartospot_dataset_export_v3.py \
  --dataset-url "https://api.cartospot.com/api/v2/score/datasets/score-photobooth-coastal" \
  --submission-set contributions \
  --output-dir "./outputs/exports" \
  --delay-seconds 0.3
```

En este ejemplo se exportan los Places y, cuando exista el conjunto llamado `contributions`, sus Submissions y respectivos archivos adjuntos.

---

**Script:** `src/cartospot_dataset_export_v3.py`  
**Módulo de rutas:** `src/project_paths.py`  
**Destino predeterminado:** `outputs/exports/`
