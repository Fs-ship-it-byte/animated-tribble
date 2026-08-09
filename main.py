from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
import uvicorn
import requests
import re
import asyncio
import os
from urllib.parse import quote_plus

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://lamovie.org"

# Algunos títulos (sobre todo estrenos muy recientes) usan una traducción
# "inventada" por el propio sitio que no coincide ni con el título en inglés
# ni con el título oficial en español de Wikidata/Wikipedia -- para esos casos
# puntuales no hay forma de adivinar automáticamente, así que se cargan acá
# a mano una vez que se descubren. Formato: "ttXXXXXXX": "slug-del-sitio-año"
MANUAL_SLUG_OVERRIDES = {
    "tt12042730": "proyecto-fin-del-mundo-2026",                          # Project Hail Mary
    "tt33612209": "el-diablo-viste-a-la-moda-2-2026",                     # The Devil Wears Prada 2
    "tt18259538": "avatar-aang-el-ultimo-maestro-aire-2026",              # Avatar: Aang, El Último Maestro del Aire
    "tt2488496": "star-wars-el-despertar-de-la-fuerza-2015",              # Star Wars: Episodio VII
    "tt2250912": "spider-man-de-regreso-a-casa-2017",                     # Spider-Man: Homecoming
    "tt0363771": "las-cronicas-de-narnia-el-leon-la-bruja-y-el-ropero-2005",  # Narnia: El León, la Bruja y el Ropero
}


# 1. FUNCIÓN PARA TRANSFORMAR EL TÍTULO EN UN SLUG (Ej: "The Matrix" -> "the-matrix")
def slugify(text: str) -> str:
    text = text.lower()
    # Transliterar acentos/ñ a su equivalente ASCII ANTES de eliminar caracteres
    # no permitidos -- si no, "último" quedaba "ltimo" en vez de "ultimo"
    # (se perdía la vocal en vez de convertirse), armando una URL inexistente.
    accents = {
        'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ü': 'u',
        'à': 'a', 'è': 'e', 'ì': 'i', 'ò': 'o', 'ù': 'u',
        'ñ': 'n', 'ç': 'c',
    }
    for accented, plain in accents.items():
        text = text.replace(accented, plain)
    text = re.sub(r'[^a-z0-9\s-]', '', text)
    text = re.sub(r'[\s-]+', '-', text).strip('-')
    return text


# 2. FUNCIÓN PARA OBTENER EL NOMBRE Y AÑO DESDE CINEMETA (API OFICIAL DE STREMIO)
def get_metadata_from_cinemeta(media_type: str, imdb_id: str):
    try:
        url = f"https://v3-cinemeta.strem.io/meta/{media_type}/{imdb_id}.json"
        response = requests.get(url, timeout=5).json()
        meta = response.get("meta", {})

        title = meta.get("name")
        year = meta.get("year", "")

        if year and "-" in str(year):
            year = str(year).split("-")[0]

        return title, year
    except Exception as e:
        print(f"Error consultando Cinemeta: {e}")
        return None, None


# 2c. TRADUCCIÓN DEL TÍTULO vía Wikidata (gratis, sin API key, y MUCHO más
# confiable que buscar por texto en Wikipedia): Wikidata permite buscar el
# artículo exacto por el ID DE IMDb (que ya tenemos), sin ambigüedad de
# títulos ni desambiguaciones. Una vez encontrado el ítem, pedimos su
# etiqueta ("label") en español.
def get_spanish_title_via_wikidata(imdb_id: str):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    print(f"Consultando Wikidata para traducir vía IMDb ID: {imdb_id}")
    try:
        # 1) Buscar el ítem de Wikidata que tenga justo ese IMDb ID (P345).
        search_resp = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": f"haswbstatement:P345={imdb_id}",
                "format": "json",
                "srlimit": 1,
            },
            timeout=6,
            headers=headers,
        )
        if search_resp.status_code != 200:
            print(f"Wikidata: búsqueda por IMDb ID falló, status={search_resp.status_code}")
            return None
        results = search_resp.json().get("query", {}).get("search", [])
        if not results:
            print(f"Wikidata: no hay ningún ítem con IMDb ID {imdb_id}")
            return None
        qid = results[0]["title"]  # ej: "Q123456"

        # 2) Pedir la etiqueta en español de ese ítem.
        label_resp = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": qid,
                "props": "labels",
                "languages": "es",
                "format": "json",
            },
            timeout=6,
            headers=headers,
        )
        if label_resp.status_code != 200:
            print(f"Wikidata: fallo al pedir la etiqueta de {qid}, status={label_resp.status_code}")
            return None
        entity = label_resp.json().get("entities", {}).get(qid, {})
        es_label = entity.get("labels", {}).get("es", {}).get("value")
        if es_label:
            print(f"Wikidata: {imdb_id} ({qid}) -> '{es_label}' (en español)")
            return es_label
        print(f"Wikidata: {qid} no tiene etiqueta en español.")
        return None
    except Exception as e:
        print(f"Error consultando Wikidata: {e}")
        return None


# 2d. TRADUCCIÓN DEL TÍTULO vía Wikipedia (respaldo si Wikidata no tiene el
# IMDb ID cargado): busca por texto y sigue el link entre idiomas. Menos
# confiable que Wikidata porque puede matchear la página equivocada.
def get_spanish_title_via_wikipedia(title: str, year: str = ""):
    print(f"Consultando Wikipedia para traducir: '{title}' (año {year})")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        # 1) Encontrar el título EXACTO del artículo en inglés (opensearch es
        # tolerante a variaciones menores de texto).
        search_resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "opensearch", "search": title, "limit": 3, "format": "json"},
            timeout=6,
            headers=headers,
        )
        candidates = search_resp.json()[1] if search_resp.status_code == 200 else []
        if not candidates:
            print(f"Wikipedia: no se encontró ningún artículo para '{title}' (status={search_resp.status_code})")
            return None

        # Preferimos el candidato que incluya el año (mejor desambiguación) si
        # hay más de una opción.
        en_title = candidates[0]
        if year:
            for c in candidates:
                if str(year) in c:
                    en_title = c
                    break

        # 2) Pedirle a Wikipedia el link equivalente en español para ese
        # mismo artículo.
        lang_resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "titles": en_title, "prop": "langlinks", "lllang": "es", "format": "json"},
            timeout=6,
            headers=headers,
        )
        # 2) Conseguir el ID de Wikidata del artículo (Wikipedia moderna ya no
        # guarda los links entre idiomas en el "langlinks" clásico -- eso está
        # desactualizado desde hace años. El link real entre idiomas vive en
        # Wikidata, así que primero sacamos el Q-id del artículo en inglés.
        props_resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "titles": en_title, "prop": "pageprops", "format": "json"},
            timeout=6,
            headers=headers,
        )
        if props_resp.status_code != 200:
            print(f"Wikipedia: fallo al pedir pageprops para '{en_title}' (status={props_resp.status_code})")
            return None
        pages = props_resp.json().get("query", {}).get("pages", {})
        wikidata_id = None
        for page in pages.values():
            wikidata_id = page.get("pageprops", {}).get("wikibase_item")
            if wikidata_id:
                break
        if not wikidata_id:
            print(f"Wikipedia: '{en_title}' no tiene wikibase_item asociado.")
            return None

        # 3) Con el Q-id, le pedimos a Wikidata el sitelink en español.
        wd_resp = requests.get(
            "https://www.wikidata.org/w/api.php",
            params={"action": "wbgetentities", "ids": wikidata_id, "props": "sitelinks", "format": "json"},
            timeout=6,
            headers=headers,
        )
        if wd_resp.status_code != 200:
            print(f"Wikidata: fallo al pedir sitelinks para {wikidata_id} (status={wd_resp.status_code})")
            return None
        entity = wd_resp.json().get("entities", {}).get(wikidata_id, {})
        es_sitelink = entity.get("sitelinks", {}).get("eswiki", {})
        es_title = es_sitelink.get("title")
        if es_title:
            es_title = re.sub(r'\s*\((?:película|serie de televisión|serie)[^)]*\)\s*$', '', es_title, flags=re.IGNORECASE)
            print(f"Wikidata: '{title}' -> '{es_title}' (en español)")
            return es_title.strip()
        print(f"Wikidata: {wikidata_id} no tiene sitelink en español (eswiki).")
        return None
    except Exception as e:
        print(f"Error consultando Wikipedia para traducción: {e}")
        return None


# 2d. BÚSQUEDA EN EL SITIO: por si el slug "titulo-año" no coincide exacto
# (acentos, títulos en otro idioma que Cinemeta nos da en inglés, etc).
#
# Intento 1 (rápido, sin navegador): la API REST nativa de WordPress
# (/wp-json/wp/v2/...) suele estar habilitada por defecto y no depende de JS.
# Probamos varios post-types típicos de sitios con tema Dooplay (movies,
# tvshows, post genérico) hasta encontrar uno que devuelva resultados.
def search_lamovie_restapi(title: str, expect_path: str):
    post_types_to_try = ["movies", "tvshows", "posts"] if expect_path == "peliculas" else ["tvshows", "movies", "posts"]
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    for post_type in post_types_to_try:
        try:
            resp = requests.get(
                f"{BASE_URL}/wp-json/wp/v2/{post_type}",
                params={"search": title, "per_page": 5},
                timeout=8,
                headers=headers,
            )
            print(f"REST API [{post_type}] status={resp.status_code}, body[:200]={resp.text[:200]!r}")
            if resp.status_code != 200:
                continue
            data = resp.json()
            if not isinstance(data, list) or not data:
                print(f"REST API [{post_type}]: respuesta no es una lista con resultados.")
                continue
            for item in data:
                link = item.get("link", "")
                if f"/{expect_path}/" in link:
                    print(f"Resultado por REST API ({post_type}): {link}")
                    return link
            print(f"REST API [{post_type}]: encontró {len(data)} items pero ninguno con /{expect_path}/ en el link.")
        except Exception as e:
            print(f"REST API search falló para post_type={post_type}: {e}")
            continue
    return None


# Intento 2 (respaldo, con navegador): el buscador visual del sitio, que
# puede estar armado con JS/AJAX (tema Dooplay), así que necesita Playwright
# para ejecutarse de verdad en vez de un GET plano.
async def search_lamovie_playwright(title: str, expect_path: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()
            search_url = f"{BASE_URL}/?s={quote_plus(title)}"
            print(f"Buscando en el sitio: {search_url}")
            await page.goto(search_url, wait_until="networkidle", timeout=15000)

            final_url = page.url
            page_title = await page.title()
            print(f"Página de búsqueda cargada. URL final: {final_url} | título: {page_title}")

            # Juntamos todos los <a href> de la página ya renderizada (JS incluido).
            # IMPORTANTE: no aceptamos ciegamente el primer link "/peliculas/"
            # que aparezca -- esos resultados suelen mezclarse con secciones de
            # "populares/recomendados" que no tienen nada que ver con la
            # búsqueda (esto causó un bug real: buscar "La asistenta" devolvía
            # "Vinski - El superhéroe invisible" como si fuera un match válido).
            # En cambio, exigimos que el slug del link comparta al menos una
            # palabra significativa con el título buscado.
            hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            print(f"Total de links <a> encontrados en la página de búsqueda: {len(hrefs)}")
            candidatos = [h for h in hrefs if f"/{expect_path}/" in h]
            print(f"De esos, {len(candidatos)} contienen '/{expect_path}/': {candidatos[:5]}")

            stopwords = {"el", "la", "los", "las", "de", "del", "y", "a", "en", "un", "una", "the", "of", "and", "a"}
            title_words = {w for w in re.sub(r'[^a-z0-9\s]', '', title.lower()).split() if w and w not in stopwords and len(w) > 2}

            seen = set()
            for href in candidatos:
                if href in seen:
                    continue
                seen.add(href)
                if href.rstrip("/") == f"{BASE_URL}/{expect_path}".rstrip("/"):
                    continue
                slug_part = href.rstrip("/").split("/")[-1]
                slug_words = set(re.sub(r'[^a-z0-9\s]', ' ', slug_part.replace("-", " ")).split())
                overlap = title_words & slug_words
                if overlap:
                    print(f"Resultado de búsqueda encontrado (coincide en {overlap}): {href}")
                    return href
                else:
                    print(f"Descartado por no compartir palabras con el título buscado: {href}")

            print("Ningún candidato de la búsqueda comparte palabras con el título -- no se acepta ninguno.")
            return None
        except Exception as e:
            print(f"Error buscando en LaMovie: {e}")
            return None
        finally:
            await browser.close()


# 3. EXTRACTOR CON PLAYWRIGHT (VERSIÓN INTEGRAL: SEGUNDO CLIC Y BLOQUEO SUAVE)
async def extract_m3u8_playwright(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        result = {"url": None, "headers": None}

        # Bloqueador SUAVE: Solo bloqueamos anuncios puros, dejamos pasar estilos y scripts del reproductor
        async def intercept_route(route):
            request_url = route.request.url.lower()
            ad_keywords = ["/ads/", "vast", "vpaid", "pop", "tracker", "analytics", "doubleclick", "adservice"]

            if any(ad in request_url for ad in ad_keywords):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", intercept_route)

        # Cierre de ventanas emergentes (Popups)
        async def close_popups(new_page):
            try:
                await new_page.close()
            except Exception:
                pass

        context.on("page", close_popups)

        # El detective de red (Busca cualquier rastro del video), guardando
        # también los headers reales con los que se pidió, para poder
        # reproducirlo después (Referer/Origin/User-Agent).
        async def handle_request(request):
            if result["url"]:
                return
            url_lower = request.url.lower()
            if ".m3u8" in url_lower or ".mp4" in url_lower or "master.json" in url_lower:
                print(f"👉 ¡VIDEO DETECTADO!: {request.url}")
                headers = request.headers
                result["url"] = request.url
                result["headers"] = {
                    "Referer": headers.get("referer", url),
                    "Origin": headers.get("origin", "/".join(url.split("/")[:3])),
                    "User-Agent": headers.get(
                        "user-agent",
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    ),
                }

        page.on("request", handle_request)

        try:
            print(f"Navegando a: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)

            titulo = await page.title()
            print(f"Título de la página: {titulo}")

            # Si la URL no existe (404), no tiene sentido perder ~15s haciendo
            # clic y esperando: cortamos acá para pasar más rápido al respaldo
            # de búsqueda.
            if "no encontrada" in titulo.lower() or "404" in titulo:
                print("Página no encontrada, saltando intento de reproducción.")
                return None, None

            # Intento de clic robusto: probamos varios selectores típicos de
            # reproductores (no solo "#player .--pl", que puede no existir
            # según el player embebido), y si ninguno existe, hacemos clic en
            # el centro del iframe/página como último recurso.
            click_selectors = [
                "#player .--pl",
                ".jw-icon-playback",
                ".vjs-big-play-button",
                ".plyr__control--overlaid",
                "#player",
                "video",
            ]
            clicked = False
            for sel in click_selectors:
                if result["url"]:
                    break
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        await el.click(timeout=3000)
                        clicked = True
                        print(f"Clic ejecutado sobre selector: {sel}")
                        break
                except Exception:
                    continue

            if not clicked and not result["url"]:
                try:
                    box = await page.viewport_size()
                    if box:
                        await page.mouse.click(box["width"] // 2, box["height"] // 2)
                        print("Clic de respaldo en el centro de la página ejecutado.")
                except Exception:
                    pass

            for _ in range(15):  # ~15 segundos de paciencia para el anuncio / carga
                if result["url"]:
                    break

                try:
                    skip_btn = page.get_by_text(re.compile(r"saltar|skip|close|cerrar", re.IGNORECASE)).first
                    if await skip_btn.is_visible():
                        await skip_btn.click()
                        print("Botón Saltar presionado. Dando un segundo para procesar...")
                        await asyncio.sleep(1)

                        # Reintento de clic tras saltar el anuncio, por si el
                        # player quedó en pausa.
                        for sel in click_selectors:
                            try:
                                el = page.locator(sel).first
                                if await el.count() > 0:
                                    await el.click(timeout=2000)
                                    print(f"Segundo clic ejecutado sobre selector: {sel}")
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass

                await asyncio.sleep(1)

        except Exception as e:
            print(f"Error en Playwright: {e}")
        finally:
            await browser.close()

        return result["url"], result["headers"]


# 4. ENDPOINT DEL MANIFEST (CONFIGURADO PARA PELÍCULAS Y SERIES)
@app.get("/manifest.json")
def get_manifest():
    return {
        "id": "com.lamovie.extractor.custom",
        "version": "1.2.0",
        "name": "LaMovie Extractor Addon",
        "description": "Extrae streams de películas y series de LaMovie usando Playwright.",
        "types": ["movie", "series"],
        "catalogs": [],
        "resources": ["stream"],
        "idPrefixes": ["tt"],
    }


# 5. ENDPOINT DE STREAMING (CONSTRUCCIÓN DE URL)
@app.get("/stream/{type}/{id}.json")
async def get_stream(type: str, id: str):
    imdb_id = id
    season = None
    episode = None

    if ":" in id:
        imdb_id, season, episode = id.split(":")

    title, year = get_metadata_from_cinemeta(type, imdb_id)

    if not title:
        return {"streams": []}

    slug_title = slugify(title)

    # Si este IMDb ID tiene una anulación manual cargada (título traducido de
    # forma distinta por el propio sitio), la usamos directo sin adivinar.
    if imdb_id in MANUAL_SLUG_OVERRIDES:
        override_slug = MANUAL_SLUG_OVERRIDES[imdb_id]
        if type == "series" and season and episode:
            target_url = f"{BASE_URL}/episodio/{override_slug}-temporada-{season}-episodio-{episode}/"
        else:
            target_url = f"{BASE_URL}/peliculas/{override_slug}/"
        print(f"Usando anulación manual para {imdb_id}: {target_url}")
    # Construimos la URL según el patrón real del sitio:
    # - Películas: /peliculas/{slug}-{año}/
    # - Episodios de serie: /episodio/{slug}-temporada-{n}-episodio-{n}/
    elif type == "series" and season and episode:
        target_url = f"{BASE_URL}/episodio/{slug_title}-temporada-{season}-episodio-{episode}/"
    else:
        target_url = f"{BASE_URL}/peliculas/{slug_title}-{year}/" if year else f"{BASE_URL}/peliculas/{slug_title}/"

    print(f"Extrayendo de: {target_url}")

    m3u8_url, req_headers = await extract_m3u8_playwright(target_url)

    # Si el slug en inglés no existe en el sitio (muy probable, ya que LaMovie
    # es 100% en español), probamos con el título real en español antes de
    # gastar tiempo en la búsqueda con navegador.
    if not m3u8_url:
        es_title = get_spanish_title_via_wikidata(imdb_id) or get_spanish_title_via_wikipedia(title, year)
        if es_title:
            es_slug = slugify(es_title)
            if type == "series" and season and episode:
                es_url = f"{BASE_URL}/episodio/{es_slug}-temporada-{season}-episodio-{episode}/"
            else:
                es_url = f"{BASE_URL}/peliculas/{es_slug}-{year}/" if year else f"{BASE_URL}/peliculas/{es_slug}/"
            print(f"Reintentando con título en español: {es_url}")
            m3u8_url, req_headers = await extract_m3u8_playwright(es_url)
            # Guardamos el título en español para usarlo también en la
            # búsqueda de respaldo si esto tampoco funcionó.
            if not m3u8_url:
                title = es_title

    # Si nada de lo anterior encontró nada, probamos primero la API REST
    # (rápida) y si no, el buscador visual con navegador (último respaldo).
    if not m3u8_url:
        if type == "series" and season and episode:
            series_url = search_lamovie_restapi(title, "series") or await search_lamovie_playwright(title, "series")
            if series_url:
                real_slug = series_url.rstrip("/").split("/")[-1]
                found_url = f"{BASE_URL}/episodio/{real_slug}-temporada-{season}-episodio-{episode}/"
                print(f"Reintentando con URL encontrada por búsqueda: {found_url}")
                m3u8_url, req_headers = await extract_m3u8_playwright(found_url)
        else:
            found_url = search_lamovie_restapi(title, "peliculas") or await search_lamovie_playwright(title, "peliculas")
            if found_url:
                print(f"Reintentando con URL encontrada por búsqueda: {found_url}")
                m3u8_url, req_headers = await extract_m3u8_playwright(found_url)

    if m3u8_url:
        stream_title = "LaMovie 🎬"
        if season and episode:
            stream_title += f" (T{season} - E{episode})"

        stream_obj = {
            "name": "LaMovie",
            "title": stream_title,
            "url": m3u8_url,
        }
        # Si logramos capturar los headers reales con los que se pidió el
        # video, se los pasamos a Stremio para que los reenvíe al reproducir
        # (si no, muchos CDNs rechazan la petición por Referer/Origin).
        if req_headers:
            stream_obj["behaviorHints"] = {
                "notWebReady": True,
                "proxyHeaders": {"request": req_headers},
            }

        return {"streams": [stream_obj]}

    return {"streams": []}


# 6. RUTA RAÍZ PARA EVITAR EL ERROR "NOT FOUND"
@app.get("/")
def home():
    return {
        "status": "online",
        "addon": "LaMovie Extractor",
        "instruction": "Copia la ruta /manifest.json e instálala en Stremio.",
    }


if __name__ == "__main__":
    # Toma el puerto que asigne Railway, o usa 8080 por defecto
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
