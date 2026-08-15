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
# 2c. TRADUCCIÓN DEL TÍTULO vía TMDB (es-MX): esta es la fuente de datos que
# el propio LaMovie usa internamente (se nota por las rutas de imágenes tipo
# "/oc3be3waruLd0PB9h4bomN7Le3v.jpg", formato típico de TMDB), así que el
# título en es-MX (español latino) que da TMDB es el que MÁS probablemente
# coincida con el slug real del sitio -- más confiable que Wikidata, que a
# veces tiene el campo "es" vacío o sin traducir (ej: Captain America: Brave
# New World, que en TMDB es-MX sí está como "Capitán América: un Nuevo
# Mundo", pero en Wikidata queda igual al inglés).
#
# Requiere una API key gratuita de TMDB (variable de entorno TMDB_API_KEY).
# Si no está configurada, esta función se salta sola y se sigue con Wikidata.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")


def get_spanish_title_via_tmdb(imdb_id: str, media_type: str = "movie"):
    if not TMDB_API_KEY:
        return None
    try:
        resp = requests.get(
            f"https://api.themoviedb.org/3/find/{imdb_id}",
            params={"api_key": TMDB_API_KEY, "external_source": "imdb_id", "language": "es-MX"},
            timeout=6,
        )
        if resp.status_code != 200:
            print(f"TMDB: find falló para {imdb_id}, status={resp.status_code}")
            return None
        data = resp.json()
        results_key = "tv_results" if media_type == "series" else "movie_results"
        results = data.get(results_key, [])
        if not results:
            print(f"TMDB: no hay resultados de tipo {results_key} para {imdb_id}")
            return None
        es_title = results[0].get("title") or results[0].get("name")
        if es_title:
            print(f"TMDB: {imdb_id} -> '{es_title}' (es-MX)")
            return es_title
        return None
    except Exception as e:
        print(f"Error consultando TMDB: {e}")
        return None


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


# 2c. BÚSQUEDA VÍA LA API INTERNA REAL DEL SITIO (descubierta inspeccionando
# el Network tab del navegador): https://lamovie.org/wp-api/v1/search
# Esto NO es la API REST estándar de WordPress (esa no devuelve nada útil acá)
# sino un endpoint propio del tema, que da JSON limpio con slug, título
# original en inglés, y tipo (movies/tvshows). Es rápido (sin navegador) y
# mucho más confiable que adivinar traducciones, porque comparamos directo
# contra "original_title" en inglés.
def search_lamovie_api(query: str, per_page: int = 8):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        resp = requests.get(
            f"{BASE_URL}/wp-api/v1/search",
            params={"postType": "any", "q": query, "postsPerPage": per_page},
            timeout=8,
            headers=headers,
        )
        if resp.status_code != 200:
            print(f"API de búsqueda interna: status={resp.status_code} para q='{query}'")
            return []
        posts = resp.json().get("data", {}).get("posts", [])
        print(f"API de búsqueda interna para '{query}': {len(posts)} resultado(s)")
        return posts
    except Exception as e:
        print(f"Error en API de búsqueda interna: {e}")
        return []


def _norm_compare(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


# Elige, entre los resultados de la API, el que mejor matchee contra el
# título en inglés (comparando "original_title") y, si lo tenemos, el año.
_STOPWORDS = {"el", "la", "los", "las", "de", "del", "y", "e", "a", "en", "un", "una", "unos", "unas",
              "the", "of", "and", "an", "to"}


def _significant_words(s: str):
    words = re.sub(r'[^a-z0-9\s]', ' ', (s or '').lower()).split()
    return [w for w in words if w and w not in _STOPWORDS and len(w) > 1]


# Puntaje por superposición de palabras significativas -- a diferencia de un
# match exacto o de substring, esto tolera diferencias como un número de
# saga insertado ("Las Crónicas de Narnia 3: ...") o pequeñas variaciones de
# puntuación, mientras la mayoría de las palabras clave sigan coincidiendo.
def _word_overlap_score(query_words, candidate_text):
    if not query_words:
        return 0.0
    cand_words = set(_significant_words(candidate_text))
    matched = sum(1 for w in query_words if w in cand_words)
    return matched / len(query_words)


# 2c-bis. SLUG EXACTO DE EPISODIO vía la API real de episodios del sitio
# (descubierta igual que la de búsqueda): dado el "_id" interno (post ID de
# WordPress) de la serie, este endpoint devuelve el listado real de
# episodios de una temporada, cada uno con su slug EXACTO -- así no
# adivinamos el patrón "-temporada-N-episodio-M", lo leemos directo.
def get_episode_slug_via_api(series_post_id, season: str, episode: str):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    page = 1
    while True:
        try:
            resp = requests.get(
                f"{BASE_URL}/wp-api/v1/single/episodes/list",
                params={"_id": series_post_id, "season": season, "page": page, "postsPerPage": 100},
                timeout=8,
                headers=headers,
            )
            if resp.status_code != 200:
                print(f"API de episodios: status={resp.status_code} para _id={series_post_id} season={season}")
                return None
            payload = resp.json().get("data", {})
            posts = payload.get("posts", [])
            for p in posts:
                if str(p.get("season_number")) == str(season) and str(p.get("episode_number")) == str(episode):
                    print(f"Slug exacto de episodio encontrado vía API: {p.get('slug')}")
                    return p.get("slug")
            pagination = payload.get("pagination", {})
            last_page = pagination.get("last_page", 1)
            if page >= last_page:
                print(f"API de episodios: temporada {season} no tiene el episodio {episode} (o no existe aún)")
                return None
            page += 1
        except Exception as e:
            print(f"Error consultando API de episodios: {e}")
            return None


# 2b-bis. LISTA DE EPISODIOS VÍA LA API INTERNA: en vez de adivinar el patrón
# "temporada-X-episodio-Y" a mano, pedimos la lista real de episodios de la
# temporada (usando el "_id" interno de la serie que ya nos dio la búsqueda) y
# sacamos el slug EXACTO del episodio que buscamos. Mucho más confiable,
# porque no depende de que el patrón de nombres se mantenga siempre igual.
def get_episode_slug_via_api(series_id, season: str, episode: str):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        resp = requests.get(
            f"{BASE_URL}/wp-api/v1/single/episodes/list",
            params={"_id": series_id, "season": season, "page": 1, "postsPerPage": 50},
            timeout=8,
            headers=headers,
        )
        if resp.status_code != 200:
            print(f"API de episodios: status={resp.status_code} para _id={series_id} season={season}")
            return None
        posts = resp.json().get("data", {}).get("posts", [])
        print(f"API de episodios para _id={series_id} temporada {season}: {len(posts)} episodio(s)")
        for ep in posts:
            if str(ep.get("episode_number")) == str(episode):
                print(f"Episodio encontrado por API: {ep.get('slug')}")
                return ep.get("slug")
        print(f"No se encontró el episodio {episode} en la lista de la temporada {season}")
        return None
    except Exception as e:
        print(f"Error consultando la API de episodios: {e}")
        return None


def pick_best_api_match(posts, title: str, year: str, want_series: bool):
    if not posts:
        return None
    target = _norm_compare(title)
    query_words = _significant_words(title)

    def type_matches(p):
        t = (p.get("type") or "").lower()
        if want_series:
            return t in ("tvshows", "series", "shows")
        return t in ("movies", "movie", "")

    same_type = [p for p in posts if type_matches(p)] or posts

    # 1) Match exacto de original_title (+ año si lo tenemos)
    for p in same_type:
        if _norm_compare(p.get("original_title", "")) == target:
            if not year or str(year) in str(p.get("release_date", "")):
                return p
    # 2) Match exacto de original_title, sin filtrar por año
    for p in same_type:
        if _norm_compare(p.get("original_title", "")) == target:
            return p

    # 3) Superposición de palabras (contra original_title Y contra title, el
    # que dé mejor puntaje), exigiendo que coincida la gran mayoría de las
    # palabras clave para no aceptar algo poco relacionado. Si además
    # tenemos el año y coincide, es un empate a favor casi seguro.
    best, best_score = None, 0.0
    for p in same_type:
        score = max(
            _word_overlap_score(query_words, p.get("original_title", "")),
            _word_overlap_score(query_words, p.get("title", "")),
        )
        if year and str(year) in str(p.get("release_date", "")):
            score += 0.15  # empujoncito si el año también coincide
        if score > best_score:
            best, best_score = p, score

    if best and best_score >= 0.8:
        print(f"Match por superposición de palabras (score={best_score:.2f}): {best.get('slug')}")
        return best

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


# 3b. EXTRACTOR ESPECÍFICO PARA SERIES: a diferencia de las películas, la URL
# de una serie SIEMPRE es /series/{slug}/ (nunca cambia por episodio). Elegir
# temporada/episodio pasa 100% por interacciones de JS en la misma página:
# botón "Episodios" -> selector de "Temporada N" -> clic en el episodio
# exacto (identificado por su etiqueta "S×E", ej "1×2"). Recién ese clic
# dispara la carga del reproductor de ESE episodio.
async def extract_series_episode_m3u8(series_url: str, season: str, episode: str):
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

        async def intercept_route(route):
            request_url = route.request.url.lower()
            ad_keywords = ["/ads/", "vast", "vpaid", "pop", "tracker", "analytics", "doubleclick", "adservice"]
            if any(ad in request_url for ad in ad_keywords):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", intercept_route)

        async def close_popups(new_page):
            try:
                await new_page.close()
            except Exception:
                pass

        context.on("page", close_popups)

        async def handle_request(request):
            if result["url"]:
                return
            url_lower = request.url.lower()
            if ".m3u8" in url_lower or ".mp4" in url_lower or "master.json" in url_lower:
                print(f"👉 ¡VIDEO DETECTADO!: {request.url}")
                headers = request.headers
                result["url"] = request.url
                result["headers"] = {
                    "Referer": headers.get("referer", series_url),
                    "Origin": headers.get("origin", "/".join(series_url.split("/")[:3])),
                    "User-Agent": headers.get(
                        "user-agent",
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    ),
                }

        page.on("request", handle_request)

        click_selectors = [
            "#player .--pl",
            ".jw-icon-playback",
            ".vjs-big-play-button",
            ".plyr__control--overlaid",
            "#player",
            "video",
        ]

        try:
            print(f"Navegando a la serie: {series_url}")
            await page.goto(series_url, wait_until="domcontentloaded", timeout=15000)

            titulo = await page.title()
            print(f"Título de la página: {titulo}")
            if "no encontrada" in titulo.lower() or "404" in titulo:
                print("Página de la serie no encontrada, saltando.")
                return None, None

            # 1) Abrir la lista de episodios
            try:
                await page.get_by_text("Episodios", exact=True).first.click(timeout=5000)
                print("Clic en 'Episodios' ejecutado.")
            except Exception as e:
                print(f"No se pudo hacer clic en 'Episodios': {e}")
            await asyncio.sleep(1)

            # 2) Si la temporada por defecto no es la que buscamos, cambiarla.
            # IMPORTANTE: si esto falla (ej: la temporada pedida no existe
            # todavía en el sitio, como "temporada 37" de una serie que solo
            # llega a la 36), NO seguimos adelante -- si no, terminaríamos
            # capturando el video que ya estaba cargado por defecto (T1E1) y
            # devolviéndolo como si fuera el episodio correcto, sirviendo
            # contenido equivocado con total confianza.
            season_changed_ok = True
            try:
                season_button = page.locator(".ss-button[aria-haspopup='listbox']").first
                if await season_button.count() > 0:
                    current_text = await season_button.inner_text()
                    if f"Temporada {season}" not in current_text:
                        await season_button.click(timeout=5000)
                        print("Selector de temporadas abierto.")
                        await asyncio.sleep(0.5)
                        option = page.get_by_text(f"Temporada {season}", exact=True).first
                        if await option.count() > 0:
                            await option.click(timeout=5000)
                            print(f"Temporada {season} seleccionada.")
                            await asyncio.sleep(1.5)  # esperar que la lista de episodios recargue (AJAX)
                        else:
                            print(f"La temporada {season} no existe en el selector -- probablemente no está disponible todavía.")
                            season_changed_ok = False
                    else:
                        print(f"Ya estaba en la temporada {season}, no hace falta cambiar.")
            except Exception as e:
                print(f"No se pudo cambiar de temporada: {e}")
                season_changed_ok = False

            if not season_changed_ok:
                print("Abortando: no se pudo confirmar la temporada pedida, no se va a servir un episodio equivocado.")
                return None, None

            # 3) Buscar y clickear el episodio exacto por su etiqueta "S×E".
            # Mismo criterio: si no aparece en la lista, cortamos acá en vez
            # de capturar cualquier video que ya esté cargado por defecto.
            episode_label = f"{season}×{episode}"
            try:
                ep_item = page.locator(".episode-item", has_text=episode_label).first
                if await ep_item.count() > 0:
                    await ep_item.click(timeout=5000)
                    print(f"Clic en el episodio '{episode_label}' ejecutado.")
                else:
                    print(f"No se encontró ningún episodio con etiqueta '{episode_label}' en la lista -- probablemente no está disponible todavía.")
                    return None, None
            except Exception as e:
                print(f"Error al clickear el episodio: {e}")
                return None, None

            await asyncio.sleep(1.5)

            # 4) Igual que con películas: puede hacer falta un clic de play
            #    adicional sobre el reproductor, y saltar anuncios.
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

            for _ in range(15):
                if result["url"]:
                    break
                try:
                    skip_btn = page.get_by_text(re.compile(r"saltar|skip|close|cerrar", re.IGNORECASE)).first
                    if await skip_btn.is_visible():
                        await skip_btn.click()
                        print("Botón Saltar presionado. Dando un segundo para procesar...")
                        await asyncio.sleep(1)
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
            print(f"Error en Playwright (serie): {e}")
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

    is_series = type == "series" and season and episode
    slug_title = slugify(title)

    def build_url(slug: str) -> str:
        # Las series SIEMPRE viven en /series/{slug}/ -- nunca hay una URL
        # distinta por episodio, la temporada/episodio se elige haciendo clic
        # dentro de la misma página (ver extract_series_episode_m3u8).
        if is_series:
            return f"{BASE_URL}/series/{slug}/"
        return f"{BASE_URL}/peliculas/{slug}/"

    async def try_url(slug: str):
        target_url = build_url(slug)
        print(f"Extrayendo de: {target_url}")
        if is_series:
            return await extract_series_episode_m3u8(target_url, season, episode)
        return await extract_m3u8_playwright(target_url)

    m3u8_url, req_headers = None, None

    # 1) Anulación manual (título traducido distinto por el propio sitio)
    if imdb_id in MANUAL_SLUG_OVERRIDES:
        override_slug = MANUAL_SLUG_OVERRIDES[imdb_id]
        print(f"Usando anulación manual para {imdb_id}: {build_url(override_slug)}")
        m3u8_url, req_headers = await try_url(override_slug)

    # 2) API interna con el título en inglés (resuelve el problema de idioma
    # de raíz sin depender de traducir nosotros, comparando "original_title").
    if not m3u8_url:
        api_posts = search_lamovie_api(title)
        api_match = pick_best_api_match(api_posts, title, year, want_series=is_series)
        if api_match:
            found_slug = api_match.get("slug", "")
            print(f"Match encontrado por API interna: {found_slug}")
            m3u8_url, req_headers = await try_url(found_slug)

    # 3) Slug adivinado directo (título+año tal cual, sin traducir)
    if not m3u8_url:
        guess_slug = slug_title if is_series else (f"{slug_title}-{year}" if year else slug_title)
        m3u8_url, req_headers = await try_url(guess_slug)

    # 4) Traducción del título (TMDB es-MX -> Wikidata -> Wikipedia) + reintento
    if not m3u8_url:
        es_title = get_spanish_title_via_tmdb(imdb_id, type) or get_spanish_title_via_wikidata(imdb_id) or get_spanish_title_via_wikipedia(title, year)
        if es_title:
            es_slug_guess = slugify(es_title) if is_series else (f"{slugify(es_title)}-{year}" if year else slugify(es_title))
            print(f"Reintentando con título en español: {build_url(es_slug_guess)}")
            m3u8_url, req_headers = await try_url(es_slug_guess)

            # Si tampoco existe con ese slug exacto, probamos la API interna
            # con el título en español (hace match parcial/fuzzy, encuentra
            # variantes como "Las Crónicas de Narnia 3: ...").
            if not m3u8_url:
                api_posts_es = search_lamovie_api(es_title)
                api_match_es = pick_best_api_match(api_posts_es, es_title, year, want_series=is_series) \
                    or next((p for p in api_posts_es if _norm_compare(es_title) in _norm_compare(p.get("title", ""))), None)
                if api_match_es:
                    found_slug_es = api_match_es.get("slug", "")
                    print(f"Match encontrado por API interna (español): {found_slug_es}")
                    m3u8_url, req_headers = await try_url(found_slug_es)

            if not m3u8_url:
                title = es_title

    # 5) Último respaldo: buscador visual con navegador
    if not m3u8_url:
        expect_path = "series" if is_series else "peliculas"
        found_url = await search_lamovie_playwright(title, expect_path)
        if found_url:
            found_slug = found_url.rstrip("/").split("/")[-1]
            print(f"Reintentando con slug encontrado por búsqueda: {found_slug}")
            m3u8_url, req_headers = await try_url(found_slug)

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
