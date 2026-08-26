const { addonBuilder, getRouter } = require('stremio-addon-sdk');
const express = require('express');
const aggregator = require('./lib/aggregator');
const axios = require('axios');
const NodeCache = require('node-cache');
const hlsproxy = require('./lib/hlsproxy');

const TMDB_KEY = process.env.TMDB_API_KEY || 'b9896a58cdbfa6752a420e406877d1a5';
const PORT = process.env.PORT || 7000;
const TMDB_BASE = 'https://api.themoviedb.org/3';

// URL pública donde queda expuesto este addon en Railway (o donde sea). Se usa
// para armar las URLs del proxy de HLS que le entregamos al reproductor.
// En Railway, seteá esta variable con la URL que te da el proyecto, ej:
// https://tuapp.up.railway.app
// Si NO la seteás, se auto-detecta con el host/protocolo de cada request
// (funciona en la mayoría de los casos, pero PUBLIC_URL explícita es más segura).
let PUBLIC_URL = (process.env.PUBLIC_URL || `http://127.0.0.1:${PORT}`).replace(/\/+$/, '');
const PUBLIC_URL_EXPLICIT = !!process.env.PUBLIC_URL;
aggregator.setPublicUrl(PUBLIC_URL);
if (!PUBLIC_URL_EXPLICIT) {
  console.warn('[AVISO] No seteaste PUBLIC_URL. Voy a intentar auto-detectarla por request, ' +
    'pero es más confiable que la definas vos mismo en las variables de entorno de Railway ' +
    '(ej: https://tuapp.up.railway.app), sin "/" al final.');
}

// cache corta para no golpear TMDB en cada scroll/click del usuario
const cache = new NodeCache({ stdTTL: 600, checkperiod: 120 });

async function tmdbGet(path, params) {
  const key = path + JSON.stringify(params || {});
  const hit = cache.get(key);
  if (hit) return hit;
  const { data } = await axios.get(TMDB_BASE + path, {
    params: Object.assign({ api_key: TMDB_KEY, language: 'es-MX' }, params),
    timeout: 10000
  });
  cache.set(key, data);
  return data;
}

// géneros de película estándar de TMDB (es-MX)
const GENRES = [
  { id: 28, name: 'Acción' }, { id: 12, name: 'Aventura' }, { id: 16, name: 'Animación' },
  { id: 35, name: 'Comedia' }, { id: 80, name: 'Crimen' }, { id: 99, name: 'Documental' },
  { id: 18, name: 'Drama' }, { id: 10751, name: 'Familia' }, { id: 14, name: 'Fantasía' },
  { id: 36, name: 'Historia' }, { id: 27, name: 'Terror' }, { id: 10402, name: 'Música' },
  { id: 9648, name: 'Misterio' }, { id: 10749, name: 'Romance' }, { id: 878, name: 'Ciencia ficción' },
  { id: 53, name: 'Suspense' }, { id: 10752, name: 'Bélica' }, { id: 37, name: 'Western' }
];
const GENRE_BY_NAME = GENRES.reduce((m, g) => (m[g.name] = g.id, m), {});

const manifest = {
  id: 'com.bflix.stremio',
  version: '1.2.0',
  name: 'BFlix',
  description: 'Addon no oficial que agrega Cinecalidad, GNULA, PelisGo y Refugio (contenido en español), con catálogo TMDB. Series: GNULA y Cinecalidad. Solo muestra streams resueltos a link directo.',
  logo: 'https://i.imgur.com/6Fjnyzl.png',
  resources: ['catalog', 'meta', 'stream'],
  types: ['movie', 'series'],
  catalogs: [
    { type: 'movie', id: 'bflix-popular', name: 'BFlix - Populares', extra: [{ name: 'skip' }] },
    { type: 'movie', id: 'bflix-top', name: 'BFlix - Mejor valoradas', extra: [{ name: 'skip' }] },
    {
      type: 'movie', id: 'bflix-genre', name: 'BFlix - Por género',
      extra: [{ name: 'genre', options: GENRES.map((g) => g.name), isRequired: true }, { name: 'skip' }]
    },
    { type: 'movie', id: 'bflix-search', name: 'BFlix - Buscar', extra: [{ name: 'search', isRequired: true }] },

    { type: 'series', id: 'bflix-series-popular', name: 'BFlix - Series populares', extra: [{ name: 'skip' }] },
    { type: 'series', id: 'bflix-series-top', name: 'BFlix - Series mejor valoradas', extra: [{ name: 'skip' }] },
    {
      type: 'series', id: 'bflix-series-genre', name: 'BFlix - Series por género',
      extra: [{ name: 'genre', options: GENRES.map((g) => g.name), isRequired: true }, { name: 'skip' }]
    },
    { type: 'series', id: 'bflix-series-search', name: 'BFlix - Buscar series', extra: [{ name: 'search', isRequired: true }] }
  ],
  idPrefixes: ['tt', 'bflix:']
};

const builder = new addonBuilder(manifest);

// ---------- helpers TMDB / IMDb ----------

async function tmdbFindMovie(imdbId) {
  const data = await tmdbGet(`/find/${imdbId}`, { external_source: 'imdb_id' });
  return (data.movie_results && data.movie_results[0]) || null;
}

async function tmdbFindSeries(imdbId) {
  const data = await tmdbGet(`/find/${imdbId}`, { external_source: 'imdb_id' });
  return (data.tv_results && data.tv_results[0]) || null;
}

function tmdbToMeta(m, type) {
  // m.imdb_id ya viene con el prefijo "tt" cuando existe
  const id = m.imdb_id ? m.imdb_id : (type === 'series' ? 'bflix:tv:' + m.id : 'bflix:' + m.id);
  const meta = {
    id,
    type,
    name: m.title || m.name,
    poster: m.poster_path ? `https://image.tmdb.org/t/p/w342${m.poster_path}` : undefined,
    background: m.backdrop_path ? `https://image.tmdb.org/t/p/w780${m.backdrop_path}` : undefined,
    description: m.overview,
    releaseInfo: (m.release_date || m.first_air_date || '').substring(0, 4),
    imdbRating: m.vote_average ? String(Math.round(m.vote_average * 10) / 10) : undefined,
    genres: (m.genres || []).map((g) => g.name)
  };
  if (type === 'series' && m.videos) meta.videos = m.videos;
  return meta;
}

function toCatalogMeta(m, type) {
  return {
    id: (type === 'series' ? 'bflix:tv:' : 'bflix:') + m.id, // se resuelve a imdb (si existe) en /meta
    type,
    name: m.title || m.name,
    poster: m.poster_path ? `https://image.tmdb.org/t/p/w342${m.poster_path}` : undefined,
    releaseInfo: (m.release_date || m.first_air_date || '').substring(0, 4)
  };
}

// Arma meta.videos (uno por episodio) pidiendo cada temporada a TMDB en
// paralelo. seriesMetaId es el id que Stremio usa para esta serie (tt... o
// bflix:tv:...) — cada video queda como `${seriesMetaId}:${season}:${episode}`,
// que es el id que después nos va a llegar en el stream handler.
async function buildSeriesVideos(tmdbId, seriesMetaId) {
  const details = await tmdbGet(`/tv/${tmdbId}`, {});
  const seasonNumbers = (details.seasons || [])
    .map((s) => s.season_number)
    .filter((n) => n > 0); // se salta "Especiales" (temporada 0)

  const seasonsData = await Promise.all(
    seasonNumbers.map((sn) => tmdbGet(`/tv/${tmdbId}/season/${sn}`, {}).catch(() => null))
  );

  const videos = [];
  seasonsData.forEach((season, idx) => {
    if (!season || !season.episodes) return;
    const sn = seasonNumbers[idx];
    season.episodes.forEach((ep) => {
      videos.push({
        id: `${seriesMetaId}:${sn}:${ep.episode_number}`,
        title: ep.name || `Episodio ${ep.episode_number}`,
        season: sn,
        episode: ep.episode_number,
        released: ep.air_date ? new Date(ep.air_date).toISOString() : undefined,
        thumbnail: ep.still_path ? `https://image.tmdb.org/t/p/w300${ep.still_path}` : undefined,
        overview: ep.overview || undefined
      });
    });
  });
  return videos;
}

// Parsea el id que Stremio manda al pedir streams de un episodio:
// "tt1234567:3:2" o "bflix:tv:94997:3:2" -> { seriesId, season, episode }
function parseSeriesStreamId(id) {
  const parts = id.split(':');
  const episode = parseInt(parts.pop(), 10);
  const season = parseInt(parts.pop(), 10);
  const seriesId = parts.join(':');
  return { seriesId, season, episode };
}

// ---------- CATALOG ----------

builder.defineCatalogHandler(async ({ type, id, extra }) => {
  try {
    const page = extra && extra.skip ? Math.floor(Number(extra.skip) / 20) + 1 : 1;
    const isSeries = type === 'series';
    const base = isSeries ? '/tv' : '/movie';
    const searchPath = isSeries ? '/search/tv' : '/search/movie';
    const discoverPath = isSeries ? '/discover/tv' : '/discover/movie';
    let data;

    if ((id === 'bflix-search' || id === 'bflix-series-search') && extra && extra.search) {
      data = await tmdbGet(searchPath, { query: extra.search, page });
    } else if (id === 'bflix-top' || id === 'bflix-series-top') {
      data = await tmdbGet(`${base}/top_rated`, { page });
    } else if ((id === 'bflix-genre' || id === 'bflix-series-genre') && extra && extra.genre) {
      const genreId = GENRE_BY_NAME[extra.genre];
      if (!genreId) return { metas: [] };
      data = await tmdbGet(discoverPath, { with_genres: genreId, sort_by: 'popularity.desc', page });
    } else {
      data = await tmdbGet(`${base}/popular`, { page });
    }

    const metas = (data.results || []).filter((m) => m.poster_path).map((m) => toCatalogMeta(m, type));
    return { metas };
  } catch (e) {
    console.error('catalog error', e.message);
    return { metas: [] };
  }
});

// ---------- META ----------

builder.defineMetaHandler(async ({ type, id }) => {
  try {
    let m;
    if (type === 'series') {
      let tmdbId;
      if (id.startsWith('bflix:tv:')) {
        tmdbId = id.split(':')[2];
      } else if (id.startsWith('tt')) {
        const found = await tmdbFindSeries(id);
        if (!found) return { meta: null };
        tmdbId = found.id;
      } else {
        return { meta: null };
      }
      const data = await tmdbGet(`/tv/${tmdbId}`, { append_to_response: 'external_ids' });
      m = data;
      m.imdb_id = data.external_ids && data.external_ids.imdb_id;
      const seriesMetaId = m.imdb_id ? m.imdb_id : 'bflix:tv:' + tmdbId;
      m.videos = await buildSeriesVideos(tmdbId, seriesMetaId);
    } else {
      if (id.startsWith('bflix:')) {
        const tmdbId = id.split(':')[1];
        const data = await tmdbGet(`/movie/${tmdbId}`, { append_to_response: 'external_ids' });
        m = data;
        m.imdb_id = data.external_ids && data.external_ids.imdb_id;
      } else if (id.startsWith('tt')) {
        m = await tmdbFindMovie(id);
        if (m) {
          const full = await tmdbGet(`/movie/${m.id}`, {});
          m = Object.assign(full, { imdb_id: id });
        }
      } else {
        return { meta: null };
      }
    }
    if (!m) return { meta: null };
    return { meta: tmdbToMeta(m, type) };
  } catch (e) {
    console.error('meta error', e.message);
    return { meta: null };
  }
});

// ---------- STREAM ----------

builder.defineStreamHandler(async ({ type, id }) => {
  try {
    if (type === 'movie') {
      let title, originalTitle, year;
      if (id.startsWith('tt')) {
        const m = await tmdbFindMovie(id);
        if (!m) return { streams: [] };
        title = m.title;
        originalTitle = m.original_title;
        year = (m.release_date || '').substring(0, 4);
      } else if (id.startsWith('bflix:')) {
        const tmdbId = id.split(':')[1];
        const data = await tmdbGet(`/movie/${tmdbId}`, {});
        title = data.title;
        originalTitle = data.original_title;
        year = (data.release_date || '').substring(0, 4);
      } else {
        return { streams: [] };
      }

      const results = await aggregator.getStreams(title, year, originalTitle);
      return { streams: normalizeStreams(results) };
    }

    if (type === 'series') {
      const { seriesId, season, episode } = parseSeriesStreamId(id);
      let title, originalTitle, year;
      if (seriesId.startsWith('tt')) {
        const m = await tmdbFindSeries(seriesId);
        if (!m) return { streams: [] };
        title = m.name;
        originalTitle = m.original_name;
        year = (m.first_air_date || '').substring(0, 4);
      } else if (seriesId.startsWith('bflix:tv:')) {
        const tmdbId = seriesId.split(':')[2];
        const data = await tmdbGet(`/tv/${tmdbId}`, {});
        title = data.name;
        originalTitle = data.original_name;
        year = (data.first_air_date || '').substring(0, 4);
      } else {
        return { streams: [] };
      }

      const results = await aggregator.getEpisodeStreams(title, year, season, episode, originalTitle);
      return { streams: normalizeStreams(results) };
    }

    return { streams: [] };
  } catch (e) {
    console.error('stream error', e.message);
    return { streams: [] };
  }
});

// Solo se muestran streams que YA quedaron con link directo: torrents (traen
// infoHash, Stremio los resuelve solo) y embeds que el resolver logró
// convertir a .m3u8/.mp4 directo. Los que quedaron sin resolver (enlace
// externo) se descartan acá -- pedido explícito para no gastar tiempo/carga
// mostrando algo que de todos modos no reproduce bien dentro de Stremio.
function normalizeStreams(results) {
  return results
    .filter((r) => (r.type === 'torrent' && r.infoHash) || r.resolved === true)
    .map((r) => {
      if (r.type === 'torrent' && r.infoHash) {
        return { name: r.name, title: r.title, infoHash: r.infoHash };
      }
      return { name: r.name, title: r.title, url: r.url };
    });
}

const app = express();
app.set('trust proxy', true); // Railway está detrás de un proxy; sin esto req.protocol/host quedan mal

// Auto-detección de PUBLIC_URL cuando no fue seteada a mano: usamos el host
// real por el que llegó CADA request (el que ve el usuario/reproductor).
app.use((req, res, next) => {
  if (!PUBLIC_URL_EXPLICIT) {
    const detected = `${req.protocol}://${req.get('host')}`.replace(/\/+$/, '');
    if (detected !== PUBLIC_URL) {
      PUBLIC_URL = detected;
      aggregator.setPublicUrl(PUBLIC_URL);
    }
  }
  next();
});

// Rutas del proxy HLS: el reproductor de Stremio pide el m3u8/segmentos a
// TRAVÉS de nosotros (mismo IP/headers que negociaron el m3u8 real).
app.get('/hlsproxy/playlist/:token/*', (req, res) => hlsproxy.handleHlsPlaylistProxy(PUBLIC_URL, req, res));
app.get('/hlsproxy/segment/:token/*', hlsproxy.handleHlsSegmentProxy);

// --- DIAGNÓSTICO: probar el resolver de Streamwish/VOE (navegador headless)
// directo contra una URL de embed, sin pasar por búsqueda ni por Stremio.
// Uso: /debug/streamwish?url=<link de streamwish.to/e/xxx, hgplaycdn.com/e/xxx, etc>
app.get('/debug/streamwish', async (req, res) => {
  const embedUrl = req.query.url;
  if (!embedUrl) return res.status(400).send('Falta ?url=<embed completa>');
  res.set('Content-Type', 'text/plain');
  const SW = require('./lib/resolvers/streamwish');
  const trace = [];
  const result = await SW.resolveStreamwishHlsViaBrowser(embedUrl, 25000, trace);
  res.send(trace.join('\n') + '\n\nResultado final: ' + (result ? JSON.stringify(result) : 'null (cayó a enlace externo)'));
});

// Mismo diagnóstico pero pasando por TODA la cadena real (vidhide -> unwrap
// player.php -> rápido -> navegador -> genérico), como en producción.
// Uso: /debug/resolve?url=<player.php de GNULA/Poseidon o embed directo>
app.get('/debug/resolve', async (req, res) => {
  const url = req.query.url;
  if (!url) return res.status(400).send('Falta ?url=');
  res.set('Content-Type', 'text/plain');
  const SW = require('./lib/resolvers/streamwish');
  try {
    const result = await SW.resolveToDirectHls(url);
    res.send('Resultado: ' + (result ? JSON.stringify(result) : 'null (cayó a enlace externo)'));
  } catch (e) {
    res.status(500).send('Error: ' + e.message);
  }
});

// Corre TODO el pipeline (búsqueda en cada fuente -> match -> fetch de
// detalle -> lista de servidores -> resolución) igual que en producción,
// pero devuelve el JSON crudo para poder ver exactamente en qué paso se
// cae un título puntual (útil para series donde la falla puede estar en la
// búsqueda/match, no en el resolver de embeds).
// Película:  /debug/episode?title=Titulo&year=2024
// Episodio:  /debug/episode?title=La%20Casa%20del%20Drag%C3%B3n&originalTitle=House%20of%20the%20Dragon&season=3&episode=7
app.get('/debug/episode', async (req, res) => {
  const { title, originalTitle, year, season, episode } = req.query;
  if (!title) return res.status(400).send('Falta ?title=');
  try {
    const results = season && episode
      ? await aggregator.getEpisodeStreams(title, year, Number(season), Number(episode), originalTitle)
      : await aggregator.getStreams(title, year, originalTitle);
    res.json({ query: { title, originalTitle, year, season, episode }, count: results.length, results });
  } catch (e) {
    res.status(500).json({ error: e.message, stack: e.stack });
  }
});

// Diagnóstico fino de la BÚSQUEDA cruda de una fuente puntual, sin pasar por
// todo el pipeline -- para saber si el problema es "el sitio no tiene nada
// con ese texto" (0 resultados) o "hay resultados pero el match no le
// pega" (bestMatch descarta todo). Cubre gnula y cinecalidad (las que
// tienen fetcher de episodio individual).
// Uso: /debug/search?source=gnula&title=La%20Casa%20del%20Drag%C3%B3n
app.get('/debug/search', async (req, res) => {
  const { source, title, originalTitle } = req.query;
  if (!source || !title) return res.status(400).send('Uso: ?source=gnula|cinecalidad&title=...&originalTitle=... (opcional)');
  const GN = require('./lib/sources/gnula');
  const CC = require('./lib/sources/cinecalidad');
  const titles = [title, originalTitle].filter(Boolean);
  try {
    const out = {};
    for (const t of titles) {
      let raw;
      if (source === 'gnula') raw = await GN.searchGnula(t);
      else if (source === 'cinecalidad') raw = await CC.searchCinecalidad(t);
      else return res.status(400).send('source debe ser gnula o cinecalidad');
      out[t] = raw;
    }
    res.json(out);
  } catch (e) {
    res.status(500).json({ error: e.message, stack: e.stack });
  }
});

// Prueba puntual del fetch de un episodio de GNULA ya con la URL armada
// (salteando la búsqueda), para separar "no encontró la serie" de "encontró
// la serie pero el fetch del episodio falla".
// Uso: /debug/gnula-episode?url=https://gnula.life/series/la-casa-del-dragon/seasons/3/episodes/7
app.get('/debug/gnula-episode', async (req, res) => {
  const url = req.query.url;
  if (!url) return res.status(400).send('Falta ?url=');
  const GN = require('./lib/sources/gnula');
  try {
    const data = await GN.fetchGnulaLifeEpisode(url);
    res.json({ url, data });
  } catch (e) {
    res.status(500).json({ error: e.message, stack: e.stack });
  }
});

// Prueba de red directa (sin navegador) contra un link YA proxeado, para ver
// si el origen responde bien y qué status/contenido trae de verdad.
// Uso: /debug/nettest?url=<tu link completo de /hlsproxy/playlist/TOKEN/master.m3u8>
app.get('/debug/nettest', async (req, res) => {
  const proxyUrl = req.query.url;
  if (!proxyUrl) return res.status(400).send('Falta el parámetro ?url=');

  const m = proxyUrl.match(/\/hlsproxy\/(?:playlist|segment)\/([^/]+)\//);
  if (!m) return res.status(400).send('Esa URL no es un link de /hlsproxy/...');

  const data = hlsproxy.decodeProxyToken(m[1]);
  if (!data) return res.status(400).send('No se pudo decodificar el token');

  res.set('Content-Type', 'text/plain');
  const start = Date.now();
  try {
    const upstream = await axios.get(data.url, {
      headers: data.headers,
      timeout: 12000,
      responseType: 'text',
      transformResponse: [(d) => d],
      validateStatus: () => true
    });
    res.send(
      'URL: ' + data.url +
      '\nTiempo: ' + (Date.now() - start) + 'ms' +
      '\nStatus: ' + upstream.status +
      '\nHeaders respuesta: ' + JSON.stringify(upstream.headers, null, 2) +
      '\n\nPrimeros 800 caracteres del body:\n' + String(upstream.data).slice(0, 800)
    );
  } catch (e) {
    res.send(
      'URL: ' + data.url +
      '\nTiempo hasta el error: ' + (Date.now() - start) + 'ms' +
      '\nError code: ' + (e.code || 'N/A') +
      '\nError message: ' + e.message
    );
  }
});
// --- FIN DIAGNÓSTICO ---

app.use(getRouter(builder.getInterface()));

app.listen(PORT, () => {
  console.log(`BFlix Stremio addon escuchando en el puerto ${PORT}`);
  console.log(`PUBLIC_URL usada para el proxy de HLS: ${PUBLIC_URL}`);
});

async function shutdown() {
  const SW = require('./lib/resolvers/streamwish');
  await SW.closeBrowser();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
