#!/usr/bin/env python3
"""
News Podcast — coleta RSS, resume com Gemini, gera áudio com edge-tts
e envia pro Telegram. Roda 1x e encerra (pensado pra cron/systemd + docker run).
"""

import asyncio
import calendar
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import feedparser
import requests

# ---------- Config via variáveis de ambiente ----------
# .strip() porque um espaço/quebra de linha colado junto no secret do GitHub
# é a causa nº 1 de "400 Bad Request" difícil de diagnosticar.
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"].strip()
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"].strip()
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"].strip()

# "gemini-flash-latest" é um alias mantido pelo Google que sempre aponta pro
# flash mais recente — evita 404 quando eles aposentam um modelo.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# "duo" = bate-papo entre dois apresentadores; "solo" = narrador único.
PODCAST_STYLE = os.environ.get("PODCAST_STYLE", "duo")
TTS_VOICE = os.environ.get("TTS_VOICE", "pt-BR-AntonioNeural")
VOICE_FEMALE = os.environ.get("VOICE_FEMALE", "pt-BR-FranciscaNeural")
VOICE_MALE = os.environ.get("VOICE_MALE", "pt-BR-AntonioNeural")
# Sem teto de verdade: o filtro que importa é a janela de HOURS_WINDOW.
# Esse número é só uma trava de segurança contra um feed defeituoso que
# devolva centenas de itens sem data.
MAX_ITEMS_PER_FEED = int(os.environ.get("MAX_ITEMS_PER_FEED", "200"))
# Quantas notícias no MÁXIMO entram no prompt do Gemini. Coletar tudo das
# últimas 24h de ~30 feeds gera um corpus enorme que faz o generateContent
# estourar o timeout (foi o "Read timed out" do dia). Este teto mantém o
# prompt enxuto e distribuído entre as fontes.
MAX_ITEMS_TO_SUMMARIZE = int(os.environ.get("MAX_ITEMS_TO_SUMMARIZE", "100"))
HOURS_WINDOW = int(os.environ.get("HOURS_WINDOW", "24"))
FEEDS_FILE = os.environ.get("FEEDS_FILE", "feeds.txt")
SEND_TEXT_TOO = os.environ.get("SEND_TEXT_TOO", "true").lower() == "true"
# Histórico dos últimos episódios (notícias usadas + roteiro), commitado no
# repo pelo workflow. Serve pra não repetir notícia nem assunto de um dia
# pro outro.
HISTORY_FILE = os.environ.get("HISTORY_FILE", "historico.json")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "3"))
BRT = timezone(timedelta(hours=-3))


def google_news_url(query: str) -> str:
    q = urllib.parse.quote_plus(f"{query} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def load_feeds(path: str) -> list[tuple[str, str]]:
    """Cada linha: `URL` ou `URL | Nome`. `gnews: <busca> | Nome` vira uma
    busca no Google Notícias; `sitemap: <url> <trecho>` lê um sitemap diário
    (veja fetch_sitemap)."""
    feeds = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            spec, _, nome = line.partition("|")
            spec, nome = spec.strip(), nome.strip()
            if spec.lower().startswith("gnews:"):
                spec = google_news_url(spec[len("gnews:"):].strip())
            feeds.append((spec, nome))
    return feeds


# Alguns sites bloqueiam o User-Agent padrão do Python; um UA de navegador
# resolve. Cloudflare e afins costumam exigir também Accept/Accept-Language
# "de gente" — sem eles, adrenaline e meiobit devolvem 403.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
        "text/xml;q=0.8, text/html;q=0.7, */*;q=0.5"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}

# Status que valem nova tentativa: rate limit (429) e instabilidade do
# servidor (5xx). 403 NÃO entra: nos logs do Actions ele é bloqueio do
# Cloudflare ao IP do GitHub (daqui de casa o mesmo feed dá 200) e não
# libera tentando de novo — vai direto pro fallback do Google Notícias.
RETRY_STATUS = {429, 500, 502, 503, 504}


def fetch_feed(url: str) -> feedparser.FeedParserDict:
    """GET com timeout separado de conexão/leitura e até 2 tentativas para
    erros transitórios. Página de desafio do Cloudflare (200 com HTML) conta
    como falha, senão o feed "funciona" com 0 notícias."""
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=(10, 30))
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if not parsed.entries and parsed.bozo:
                raise requests.RequestException(
                    f"resposta não é um feed válido ({resp.headers.get('content-type')})"
                )
            return parsed
        except requests.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if attempt == 1 or status not in RETRY_STATUS:
                raise
            time.sleep(5)
    raise RuntimeError("unreachable")  # só pro type checker


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def fetch_sitemap(spec: str) -> list[dict]:
    """`sitemap: <url com {data}> <trecho que o link precisa ter>`.

    Feito pro TecMundo, que virou seção do Estadão: o RSS da seção devolve
    matérias velhas em ordem aleatória e o Google Notícias mistura matérias
    antigas migradas com data de hoje. O sitemap diário do Estadão (hoje e
    ontem) é a única lista confiável. Não tem título, então ele sai do slug
    da URL — o Gemini reescreve tudo mesmo. Ficam de fora cupons/guia de
    compras e as matérias migradas, cujo slug começa com o ID antigo
    ("106674-confira-...")."""
    url_tpl, _, filtro = spec.removeprefix("sitemap:").strip().partition(" ")
    filtro = filtro.strip()
    agora = datetime.now(timezone.utc)
    entries = []
    erros = []
    for dia in (agora, agora - timedelta(days=1)):
        try:
            resp = requests.get(
                url_tpl.replace("{data}", dia.strftime("%Y-%m-%d")),
                headers=HTTP_HEADERS,
                timeout=(10, 30),
            )
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
        except (requests.RequestException, ElementTree.ParseError) as e:
            erros.append(e)  # o de hoje pode ainda não existir de madrugada
            continue
        for url in root.iter(f"{_SITEMAP_NS}url"):
            link = (url.findtext(f"{_SITEMAP_NS}loc") or "").strip()
            lastmod = (url.findtext(f"{_SITEMAP_NS}lastmod") or "").strip()
            slug = link.rstrip("/").rsplit("/", 1)[-1]
            if (
                filtro not in link
                or "/guia-de-compras/" in link
                or re.match(r"\d+-", slug)
                or not lastmod
            ):
                continue
            lastmod = re.sub(r"\.\d+", "", lastmod).replace("Z", "+00:00")
            dt = datetime.fromisoformat(lastmod).astimezone(timezone.utc)
            entries.append(
                {
                    "title": slug.replace("-", " ").capitalize(),
                    "link": link,
                    "summary": "",
                    "published_parsed": dt.timetuple(),
                }
            )
    if len(erros) == 2:
        raise requests.RequestException(f"sitemap indisponível: {erros[0]}")
    return entries


def _ler_fonte(url: str, nome: str) -> tuple[list, str, bool]:
    """Devolve (entries, nome da fonte, veio do Google Notícias?)."""
    alvo = url.removeprefix("sitemap:").strip()
    partes = urllib.parse.urlsplit(alvo.split(" ")[0])
    dominio = partes.netloc.lower().removeprefix("www.")
    try:
        if url.startswith("sitemap:"):
            return fetch_sitemap(url), nome or dominio, False
        parsed = fetch_feed(url)
        via_gnews = dominio == "news.google.com"
        return parsed.entries, nome or parsed.feed.get("title", url), via_gnews
    except requests.RequestException as e:
        if dominio == "news.google.com":
            raise
        # Site bloqueou o IP do runner: busca as notícias dele pelo Google
        # Notícias, que não bloqueia o GitHub.
        print(f"[AVISO] {nome or url}: {e} -> usando Google Notícias", file=sys.stderr)
        busca = f"site:{dominio}"
        if url.startswith("sitemap:"):
            busca += alvo.partition(" ")[2].strip().rstrip("/")
        parsed = fetch_feed(google_news_url(busca))
        return parsed.entries, nome or dominio, True


def collect_news(feeds: list[tuple[str, str]]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    items = []
    vistos: set[str] = set()
    for url, nome in feeds:
        try:
            entries, source, via_gnews = _ler_fonte(url, nome)
            count = 0
            for entry in entries:
                if count >= MAX_ITEMS_PER_FEED:
                    break
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                pub_dt = None
                if published:
                    # struct_time do feedparser é UTC: timegm, não mktime.
                    pub_dt = datetime.fromtimestamp(calendar.timegm(published), tz=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                title = strip_html(entry.get("title", "")) or "(sem título)"
                if via_gnews and " - " in title:
                    title = title.rsplit(" - ", 1)[0]  # tira o " - Nome do Site"
                link = entry.get("link", "")
                chave = _norm_link(link) or _norm_title(title)
                if chave in vistos:  # mesma matéria em dois feeds
                    continue
                vistos.add(chave)
                items.append(
                    {
                        "source": source,
                        "title": title,
                        "summary": strip_html(entry.get("summary", ""))[:500],
                        "link": link,
                        "ts": pub_dt.timestamp() if pub_dt else 0.0,
                    }
                )
                count += 1
            print(f"[OK] {source}: {count} notícias")
        except Exception as e:
            print(f"[ERRO] {nome or url}: {e}", file=sys.stderr)
    return items


# ---------- Histórico: não repetir notícia de um dia pro outro ----------

_STOPWORDS = set(
    "a o e é de da do das dos em no na nos nas um uma uns umas para pra por com "
    "sem que se ao aos à às ou mais como seu sua seus suas já foi ser vai "
    "the of to in on for and is are with at by from an as its it new how why what".split()
)


def _norm_link(link: str) -> str:
    """Link comparável: sem esquema, www, barra final, fragmento e utm_*."""
    if not link:
        return ""
    p = urllib.parse.urlsplit(link.strip())
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(p.query)
        if not (k.lower().startswith("utm_") or k.lower() in ("fbclid", "gclid", "ref"))
    ]
    base = p.netloc.lower().removeprefix("www.") + p.path.rstrip("/")
    return base + ("?" + urllib.parse.urlencode(query) if query else "")


def _title_tokens(title: str) -> set[str]:
    t = unicodedata.normalize("NFKD", title.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return {w for w in re.findall(r"[a-z0-9]+", t) if w not in _STOPWORDS}


def _norm_title(title: str) -> str:
    return " ".join(sorted(_title_tokens(title)))


def _titulos_parecidos(a: set[str], b: set[str]) -> bool:
    """Mesmo assunto com título levemente reescrito (outro site, correção,
    'atualizado'): 60%+ das palavras relevantes em comum."""
    if len(a) < 3 or len(b) < 3:
        return bool(a) and a == b
    return len(a & b) / len(a | b) >= 0.6


def load_history(path: str) -> list[dict]:
    """Episódios dos últimos HISTORY_DAYS dias, do mais antigo pro mais novo."""
    try:
        with open(path, encoding="utf-8") as f:
            episodios = json.load(f).get("episodios", [])
    except FileNotFoundError:
        return []
    except (ValueError, AttributeError) as e:
        print(f"[AVISO] histórico ilegível ({e}); ignorando", file=sys.stderr)
        return []
    limite = (datetime.now(BRT) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    return [ep for ep in episodios if ep.get("data", "") > limite]


def save_history(path: str, episodios: list[dict], items: list[dict], roteiro: str) -> None:
    agora = datetime.now(BRT)
    episodios = episodios + [
        {
            "data": agora.strftime("%Y-%m-%d"),
            "gerado_em": agora.isoformat(timespec="seconds"),
            "noticias": [{"titulo": i["title"], "link": i["link"]} for i in items],
            "roteiro": roteiro,
        }
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"episodios": episodios}, f, ensure_ascii=False, indent=1)
        f.write("\n")


def remove_repeated(items: list[dict], episodios: list[dict]) -> list[dict]:
    """Tira notícias que já foram pro Gemini nos episódios anteriores — pelo
    link ou por título quase igual. Pega o caso clássico: a matéria sai às
    6h, entra hoje e ainda está dentro da janela de 24h amanhã (o cron do
    Actions atrasa), ou o feed republica o item com data atualizada."""
    links_antigos = set()
    titulos_antigos = []
    for ep in episodios:
        for n in ep.get("noticias", []):
            if n.get("link"):
                links_antigos.add(_norm_link(n["link"]))
            titulos_antigos.append(_title_tokens(n.get("titulo", "")))
    novos = []
    for it in items:
        if _norm_link(it["link"]) in links_antigos:
            continue
        tokens = _title_tokens(it["title"])
        if any(_titulos_parecidos(tokens, t) for t in titulos_antigos):
            continue
        novos.append(it)
    if episodios:
        print(
            f"[INFO] {len(items) - len(novos)} notícias já usadas nos últimos "
            f"{len(episodios)} episódio(s) foram descartadas; {len(novos)} novas"
        )
    return novos


def trim_for_summary(items: list[dict], limite: int) -> list[dict]:
    """Se a coleta trouxe mais que `limite` itens, faz um rodízio entre as
    fontes (mais recentes primeiro dentro de cada fonte) até bater o teto —
    assim um feed volumoso (HN, portais) não engole o episódio inteiro."""
    if len(items) <= limite:
        return items
    por_fonte: dict[str, list[dict]] = {}
    for it in items:
        por_fonte.setdefault(it["source"], []).append(it)
    for lst in por_fonte.values():
        lst.sort(key=lambda i: i.get("ts") or 0.0, reverse=True)
    filas = [lst for lst in por_fonte.values() if lst]
    ordenados: list[dict] = []
    while filas and len(ordenados) < limite:
        for fila in list(filas):
            ordenados.append(fila.pop(0))
            if not fila:
                filas.remove(fila)
            if len(ordenados) >= limite:
                break
    print(f"[INFO] {len(items)} coletadas -> {len(ordenados)} enviadas ao Gemini (teto {limite})")
    return ordenados


def summarize(items: list[dict], episodios: list[dict]) -> str:
    corpus = "\n\n".join(
        f"FONTE: {i['source']}\nTÍTULO: {i['title']}\nRESUMO: {i['summary']}"
        for i in items
    )
    if PODCAST_STYLE == "duo":
        hoje = datetime.now(BRT)
        dias = [
            "segunda-feira", "terça-feira", "quarta-feira",
            "quinta-feira", "sexta-feira", "sábado", "domingo",
        ]
        meses = [
            "janeiro", "fevereiro", "março", "abril", "maio", "junho",
            "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
        ]
        contexto_dia = (
            f"Hoje é {dias[hoje.weekday()]}, "
            f"{hoje.day} de {meses[hoje.month - 1]} de {hoje.year}."
        )
        estilo = f"""Você é o roteirista de um podcast DIÁRIO de notícias de tecnologia em
português do Brasil, apresentado por dois hosts com personalidades bem definidas.
Este episódio cobre as últimas 24 horas — é um resumo do DIA, nunca da semana.

ANA — a analítica da dupla, e a mais intensa das duas vozes. Fala com paixão e
convicção, não tem medo de dar opinião forte ou discordar de frente. Quando
surge um conceito técnico (LLM, kubernetes, zero-day, latência...), ela explica
em uma frase simples com uma analogia do dia a dia, sem soar professoral — mas
faz isso com intensidade, como quem realmente se importa com o assunto, não
com neutralidade de manual. Provoca, questiona, aponta "o que ninguém está
comentando" sobre a notícia com firmeza. É magnética: quando ela fala, prende
a atenção.

LEO — o brincalhão. Energia alta, piadas leves e referências nerd, reage com
entusiasmo genuíno ("não acredito!", "olha isso!"), provoca a Ana de leve e sempre
puxa o lado prático: "tá, mas o que isso muda na vida de quem tá ouvindo?".

DINÂMICA (o que faz soar como conversa de verdade):
- LEO SEMPRE abre o episódio com um bordão de abertura criativo, estilo nerd,
  DIFERENTE a cada dia (crie um novo hoje, nunca repita), e ANA emenda com um
  comentário intenso e marcante no estilo dela.
- Mencione o dia da semana na abertura de forma natural. {contexto_dia}
- Eles se chamam pelo nome, discordam de verdade às vezes (a Ana puxa mais essa
  briga), um completa o raciocínio do outro, fazem gancho entre uma notícia e a
  próxima.
- Reações curtas no meio da conversa ("sério?", "exato", "aí complicou") pra
  quebrar blocos longos de fala.
- LEO encerra com um bordão de despedida (também novo a cada dia) e ANA fecha com
  uma última observação afiada e intensa.
- NUNCA chame o episódio de "resumo semanal", "boletim da semana" ou qualquer
  variação de semanal — é sempre "resumo de hoje", "episódio de hoje", "edição
  de hoje". Isso é um erro grave, evite a todo custo.

PORTUGUÊS NATURAL (muito importante):
- Escreva como brasileiros realmente falam, não como uma tradução. Frases
  curtas, contrações naturais ("tá", "pra", "cê", "né", "bora"), sem construções
  formais ou empoladas que soam artificiais em fala espontânea.
  Evite conectivos de texto escrito ("outrossim", "ademais", "não obstante") e
  frases repetidas ou genéricas de robô ("é importante ressaltar que...",
  "vale destacar que...", "em suma").
- Cada frase deve soar como algo que uma pessoa de verdade diria em voz alta
  numa conversa descontraída, com ritmo e respiração naturais — não como texto
  de artigo lido em voz alta.

FORMATO OBRIGATÓRIO: cada fala em sua própria linha, começando com "ANA:" ou
"LEO:". Nenhuma linha fora desse formato — sem títulos, sem markdown, sem
asteriscos, sem emojis, sem rubricas como (risos) ou [vinheta].

CONTEÚDO (roteiro de 4 a 6 minutos):
- Agrupe notícias repetidas (vários sites cobrindo o mesmo assunto) em um item só.
- Priorize: AWS e nuvem (Azure, Google Cloud), DevOps, notícias da GFT
  Technologies, IA, lançamentos relevantes, programação e segurança.
- Ignore publieditorial, promoções e reviews de produto irrelevantes."""
    else:
        estilo = """Você é o roteirista de um podcast DIÁRIO de notícias de tecnologia em
português do Brasil. Este episódio cobre as últimas 24 horas — é um resumo do
DIA, nunca da semana; nunca diga "resumo semanal" ou variações.

Escreva um roteiro de podcast de 3 a 5 minutos:
- Comece com uma saudação curta ("Bom dia! Aqui está o seu resumo tech de hoje...").
- Agrupe notícias repetidas (vários sites cobrindo o mesmo assunto) em um item só.
- Priorize: AWS e nuvem (Azure, Google Cloud), DevOps, notícias da GFT
  Technologies, IA, lançamentos relevantes, programação e segurança.
- Ignore publieditorial, promoções e reviews de produto irrelevantes.
- Fale de forma natural, como um apresentador, sem markdown, sem asteriscos,
  sem emojis, sem listas — apenas texto corrido pronto para ser lido em voz alta.
- Português natural e fluido, como brasileiros realmente falam: frases curtas,
  contrações naturais ("tá", "pra", "né"), sem conectivos formais de texto
  escrito e sem frases genéricas de robô ("é importante ressaltar que...").
- Encerre com uma despedida curta."""

    anteriores = ""
    if episodios:
        roteiros = "\n\n".join(
            f"--- EPISÓDIO DE {ep.get('data', '?')} ---\n{ep.get('roteiro', '')[:8000]}"
            for ep in episodios
        )
        anteriores = f"""

NÃO SEJA REPETITIVO (muito importante): abaixo estão os roteiros dos últimos
episódios, que o ouvinte já escutou.
- Não volte a comentar nenhum assunto que já apareceu neles, mesmo que hoje
  venha de outro site ou com outro título.
- Exceção: se a notícia de hoje traz um desdobramento REALMENTE novo de um
  assunto antigo, fale só da novidade, em poucas falas, com um gancho do tipo
  "lembra que a gente comentou disso ontem?".
- Não reaproveite bordões de abertura e despedida, piadas, analogias nem
  frases de efeito desses episódios.
- Se sobrarem poucas notícias inéditas, faça um episódio mais curto em vez de
  requentar assunto.

EPISÓDIOS ANTERIORES:
{roteiros}"""

    prompt = f"""{estilo}{anteriores}

Abaixo estão as notícias das últimas {HOURS_WINDOW} horas coletadas de vários sites
(as que já foram usadas em episódios anteriores já foram removidas).

NOTÍCIAS DE HOJE:
{corpus}"""

    return gerar_com_gemini(prompt)


def gerar_com_gemini(prompt: str) -> str:
    """Um 503 costuma ser aquele modelo específico lotado, não a conta/chave.
    Por isso cada rodada passa UMA vez por cada modelo da fila (sem esperar
    entre eles) e só pausa entre rodadas. Antes eram 2 tentativas + 10s de
    espera por modelo, o que demorava pra chegar num modelo livre."""
    chain = [GEMINI_MODEL]
    pausas = [0, 20, 60]  # espera antes de cada rodada
    descoberta_feita = False
    last_error: Exception | None = None

    for rodada, pausa in enumerate(pausas, 1):
        if pausa:
            print(
                f"[AVISO] rodada {rodada - 1} falhou em todos os modelos; "
                f"nova rodada em {pausa}s...",
                file=sys.stderr,
            )
            time.sleep(pausa)
        i = 0
        while i < len(chain):  # chain pode crescer durante a 1ª rodada
            model = chain[i]
            i += 1
            try:
                resp = _gemini_generate(model, prompt)
                resp.raise_for_status()
                data = resp.json()
                texto = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                print(f"[OK] roteiro gerado por {model}")
                return texto
            except (requests.RequestException, KeyError, IndexError) as e:
                last_error = e
                print(f"[AVISO] {model} falhou ({_resumo_erro(e)})", file=sys.stderr)
            # Na primeira falha, descobre outros modelos pra completar a fila.
            if not descoberta_feita:
                descoberta_feita = True
                try:
                    alternativos = discover_models(excluir=set(chain))
                    if alternativos:
                        print(
                            f"[AVISO] modelos alternativos: {', '.join(alternativos)}",
                            file=sys.stderr,
                        )
                        chain.extend(alternativos)
                except requests.RequestException as e:
                    print(f"[AVISO] não deu pra listar modelos alternativos: {e}", file=sys.stderr)

    raise RuntimeError(
        f"Gemini falhou em todos os modelos tentados ({', '.join(chain)}): {last_error}"
    )


def _resumo_erro(e: Exception) -> str:
    """'503 Service Unavailable' em vez da URL inteira repetida no log."""
    resp = getattr(e, "response", None)
    if resp is not None:
        return f"{resp.status_code} {resp.reason}"
    return str(e)


def _gemini_generate(model: str, prompt: str) -> requests.Response:
    return requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        # (conexão, leitura). Com o corpus já limitado por MAX_ITEMS_TO_SUMMARIZE
        # a geração é rápida; 180s de leitura é só folga pra API sob carga.
        timeout=(10, 180),
    )


def _model_version_key(name: str) -> tuple:
    # Prioriza modelos com número de versão explícito (gemini-3.7-flash) sobre
    # aliases (gemini-flash-latest): alfabeticamente "flash-latest" vem DEPOIS
    # de "3.7-flash" (letra > dígito), então um sort ingênuo escolhia o
    # próprio alias sobrecarregado como seu "substituto" — daí o loop de 503.
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", name)
    if m:
        return (1, int(m.group(1)), int(m.group(2) or 0))
    return (0, 0, 0)


def discover_models(excluir: set[str], limite: int = 3) -> list[str]:
    """Lista os modelos disponíveis pra esta chave e devolve até `limite`
    'flash' com versão explícita, do mais novo pro mais velho, sem repetir
    nenhum nome já tentado (aliases tipo -latest ficam de fora de propósito).
    No fim da fila entra um flash-lite: roteiro um pouco mais simples, mas
    costuma estar livre quando os flash normais estão todos dando 503."""
    resp = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": GEMINI_API_KEY},
        params={"pageSize": 1000},
        timeout=30,
    )
    resp.raise_for_status()
    names = [
        m["name"].removeprefix("models/")
        for m in resp.json().get("models", [])
        if "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    names = [n for n in names if n not in excluir]
    ruins = ("lite", "live", "tts", "image", "preview", "exp", "thinking", "latest")
    flash_estavel = [n for n in names if "flash" in n and not any(r in n for r in ruins)]
    candidatos = flash_estavel or [n for n in names if "flash" in n]
    fila = sorted(candidatos, key=_model_version_key, reverse=True)[:limite]
    ruins_lite = tuple(r for r in ruins if r != "lite")
    lites = [n for n in names if "flash-lite" in n and not any(r in n for r in ruins_lite)]
    if lites:
        fila.append(max(lites, key=_model_version_key))
    return fila


async def text_to_speech(text: str, out_path: str) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, TTS_VOICE, rate="+8%")
    await communicate.save(out_path)


def parse_dialogue(script: str) -> list[tuple[str, str]]:
    """Converte o roteiro em [(falante, fala), ...]. Linhas sem prefixo
    ANA:/LEO: são tratadas como continuação da fala anterior."""
    segments: list[tuple[str, str]] = []
    for raw in script.splitlines():
        line = raw.strip().lstrip("*-•# ").strip()
        if not line:
            continue
        m = re.match(r"(?i)^\**(ana|leo)\**\s*:\s*(.+)$", line)
        if m:
            segments.append((m.group(1).upper(), m.group(2).strip()))
        elif segments:
            speaker, text = segments[-1]
            segments[-1] = (speaker, text + " " + line)
    return segments


async def dialogue_to_speech(segments: list[tuple[str, str]], out_path: str) -> None:
    """Gera cada fala com a voz do respectivo host e costura tudo num MP3 só.
    Concatenar os bytes funciona porque o edge-tts emite MPEG puro, sem headers."""
    import edge_tts

    # Ana: grave e com mais ritmo — intensidade, não passividade.
    # Leo: mais acelerado (energia, empolgação).
    styles = {
        "ANA": {"voice": VOICE_FEMALE, "rate": "+7%", "pitch": "-12Hz"},
        "LEO": {"voice": VOICE_MALE, "rate": "+12%", "pitch": "+0Hz"},
    }
    with open(out_path, "wb") as out:
        for speaker, text in segments:
            s = styles[speaker]
            communicate = edge_tts.Communicate(
                text, s["voice"], rate=s["rate"], pitch=s["pitch"]
            )
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    out.write(chunk["data"])


def _telegram_ok(resp: requests.Response) -> None:
    """raise_for_status, mas incluindo a descrição de erro que o Telegram manda."""
    if resp.ok:
        return
    try:
        desc = resp.json().get("description", resp.text[:200])
    except ValueError:
        desc = resp.text[:200]
    raise RuntimeError(f"Telegram respondeu {resp.status_code}: {desc}")


def check_telegram() -> None:
    """Valida token e chat_id logo no início, antes de gastar Gemini e TTS."""
    resp = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat",
        params={"chat_id": TELEGRAM_CHAT_ID},
        timeout=30,
    )
    if not resp.ok:
        try:
            desc = resp.json().get("description", resp.text[:200])
        except ValueError:
            desc = resp.text[:200]
        raise RuntimeError(
            f"Telegram recusou o chat_id '{TELEGRAM_CHAT_ID}': {desc}. "
            "Confira o secret TELEGRAM_CHAT_ID (só números, sem espaços; pode "
            "começar com -) e garanta que você já mandou /start pro seu bot."
        )


def send_telegram_audio(mp3_path: str, caption: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendAudio"
    with open(mp3_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption[:1024],
                "title": caption[:60],
            },
            files={"audio": (os.path.basename(mp3_path), f, "audio/mpeg")},
            timeout=120,
        )
    _telegram_ok(resp)


def send_telegram_text(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram limita mensagens a 4096 chars
    for i in range(0, len(text), 4000):
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i : i + 4000]},
            timeout=60,
        )
        _telegram_ok(resp)


def main() -> None:
    today = datetime.now(BRT).strftime("%d/%m/%Y")
    print(f"=== Resumo tech {today} ===")

    check_telegram()
    print("Telegram OK (token e chat_id válidos)")

    feeds = load_feeds(FEEDS_FILE)
    print(f"{len(feeds)} feeds configurados")

    items = collect_news(feeds)
    print(f"{len(items)} notícias coletadas")
    episodios = load_history(HISTORY_FILE)
    items = remove_repeated(items, episodios)
    if not items:
        send_telegram_text(f"Resumo tech {today}: nenhuma notícia nova encontrada nas últimas {HOURS_WINDOW}h.")
        print("Nenhuma notícia nova. Encerrando.")
        return

    items = trim_for_summary(items, MAX_ITEMS_TO_SUMMARIZE)
    script = summarize(items, episodios)
    print(f"Roteiro gerado: {len(script)} caracteres")

    mp3 = os.path.join(
        tempfile.gettempdir(), f"resumo_tech_{datetime.now(BRT).strftime('%Y%m%d')}.mp3"
    )
    segments = parse_dialogue(script) if PODCAST_STYLE == "duo" else []
    if len(segments) >= 4:
        print(f"Bate-papo com {len(segments)} falas (ANA e LEO)")
        asyncio.run(dialogue_to_speech(segments, mp3))
    else:
        if PODCAST_STYLE == "duo":
            print("[AVISO] Roteiro não veio em formato de diálogo; usando voz única.", file=sys.stderr)
        asyncio.run(text_to_speech(script, mp3))
    print(f"Áudio gerado: {mp3} ({os.path.getsize(mp3) // 1024} KB)")

    send_telegram_audio(mp3, f"🎙️ Resumo Tech — {today}")
    if SEND_TEXT_TOO:
        send_telegram_text(script)
    print("Enviado pro Telegram.")

    # Só grava depois de enviar: se algo falhar antes, amanhã essas notícias
    # continuam valendo.
    save_history(HISTORY_FILE, episodios, items, script)
    print(f"Histórico salvo em {HISTORY_FILE} ({len(episodios) + 1} episódio(s)). Fim.")


if __name__ == "__main__":
    # Sem isso o stdout fica em buffer e os [AVISO] (stderr) aparecem no log
    # do Actions antes do "=== Resumo tech ===", fora de ordem.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        main()
    except Exception as e:
        # Avisa no Telegram antes de falhar o job, senão o erro passa despercebido.
        try:
            send_telegram_text(f"⚠️ O resumo tech de hoje falhou: {e}")
        except Exception:
            pass
        raise
