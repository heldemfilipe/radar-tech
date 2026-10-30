#!/usr/bin/env python3
"""
News Podcast — coleta RSS, resume com Gemini, gera áudio com edge-tts
e envia pro Telegram. Roda 1x e encerra (pensado pra cron/systemd + docker run).
"""

import asyncio
import calendar
import html
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
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
# Elenco de apresentadores (veja apresentadores.json). DUPLA="Clara,Caio"
# força uma dupla específica, útil pra testar.
APRESENTADORES_FILE = os.environ.get("APRESENTADORES_FILE", "apresentadores.json")
DUPLA_FORCADA = os.environ.get("DUPLA", "").strip()
# Frase fixa da abertura (a "vinheta" falada), igual em todo episódio desde
# o começo do podcast. Não é bordão: é a assinatura do programa.
VINHETA = os.environ.get(
    "VINHETA", "tá no ar a edição de hoje do seu podcast diário de tecnologia"
)
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
# Quantas notícias em destaque têm a matéria completa baixada e enviada ao
# Gemini (o RSS só traz ~500 caracteres, pouco pra explicar direito).
MAX_FULL_ARTICLES = int(os.environ.get("MAX_FULL_ARTICLES", "6"))
PRONUNCIA_FILE = os.environ.get("PRONUNCIA_FILE", "pronuncia.txt")
# Preenchidos pelo workflow. REPESCAGEM: execução extra do fim da manhã, que
# só gera episódio se o das 06:15 não saiu. PROXIMA_TENTATIVA: horário (BRT)
# da próxima execução agendada, pra citar no aviso de falha.
REPESCAGEM = os.environ.get("REPESCAGEM", "false").lower() == "true"
PROXIMA_TENTATIVA = os.environ.get("PROXIMA_TENTATIVA", "").strip()

DIAS_SEMANA = [
    "segunda-feira", "terça-feira", "quarta-feira",
    "quinta-feira", "sexta-feira", "sábado", "domingo",
]
MESES = [
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


_EDICOES_GNEWS = {
    "pt": "hl=pt-BR&gl=BR&ceid=BR:pt-419",
    "en": "hl=en-US&gl=US&ceid=US:en",
}


def google_news_url(query: str, horas: int = 24, edicao: str = "pt") -> str:
    """A edição importa: a brasileira quase não indexa site em inglês
    (VentureBeat via fallback dava 0 notícias; na edição dos EUA, 12)."""
    q = urllib.parse.quote_plus(f"{query} when:{max(1, -(-horas // 24))}d")
    return f"https://news.google.com/rss/search?q={q}&{_EDICOES_GNEWS[edicao]}"


def load_feeds(path: str) -> list[tuple[str, str, int, str]]:
    """Cada linha: `URL`, `URL | Nome` ou `URL | Nome | 72h`. O terceiro
    campo amplia a janela daquele feed (padrão: HOURS_WINDOW) — pra blogs
    que postam poucas vezes por semana (Kubernetes, CNCF, Docker...), que
    com 24h quase sempre davam 0. O histórico impede que a mesma notícia
    entre em dois episódios. O quarto campo é uma tag (`politica`: as
    notícias do feed entram no bloco Radar Política). `gnews: <busca> | Nome`
    vira uma busca no Google Notícias; `sitemap: <url> <trecho>` lê um
    sitemap diário (veja fetch_sitemap)."""
    feeds = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            spec, nome, janela, tag = (line.split("|") + ["", "", ""])[:4]
            spec, nome, tag = spec.strip(), nome.strip(), tag.strip().lower()
            m = re.fullmatch(r"\s*(\d+)\s*h?\s*", janela)
            horas = int(m.group(1)) if m else HOURS_WINDOW
            if spec.lower().startswith("gnews:"):
                spec = google_news_url(spec[len("gnews:"):].strip(), horas)
            feeds.append((spec, nome, horas, tag))
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


def _ler_fonte(url: str, nome: str, horas: int) -> tuple[list, str, bool]:
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
        parsed = fetch_feed(google_news_url(busca, horas, "pt"))
        if not parsed.entries:  # site em inglês: só a edição dos EUA tem
            parsed = fetch_feed(google_news_url(busca, horas, "en"))
        return parsed.entries, nome or dominio, True


def collect_news(feeds: list[tuple[str, str, int, str]]) -> list[dict]:
    agora = datetime.now(timezone.utc)
    items = []
    vistos: set[str] = set()
    for url, nome, horas, tag in feeds:
        cutoff = agora - timedelta(hours=horas)
        try:
            entries, source, via_gnews = _ler_fonte(url, nome, horas)
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
                        "tag": tag,
                    }
                )
                count += 1
            janela = f" (janela {horas}h)" if horas != HOURS_WINDOW else ""
            print(f"[OK] {source}: {count} notícias{janela}")
        except Exception as e:
            print(f"[ERRO] {nome or url}: {e}", file=sys.stderr)
    return items


# ---------- Apresentadores: elenco e rodízio ----------

LEGADO = ("Ana", "Leo")  # episódios gravados antes do rodízio
_ELENCO_PADRAO = {
    "rodizio_dias": 2,
    "mulheres": [
        {
            "nome": "Ana",
            "voz": "pt-BR-FranciscaNeural",
            "rate": "+7%",
            "pitch": "-12Hz",
            "persona": "a analítica da dupla, intensa e de opinião forte. Explica "
            "conceitos técnicos com analogias do dia a dia.",
        }
    ],
    "homens": [
        {
            "nome": "Leo",
            "voz": "pt-BR-AntonioNeural",
            "rate": "+12%",
            "pitch": "+0Hz",
            "persona": "o brincalhão, energia alta, puxa o lado prático.",
        }
    ],
}
_elenco: dict | None = None


def carregar_elenco() -> dict:
    """Lê apresentadores.json (ou o elenco padrão Ana e Leo, se faltar ou
    estiver quebrado) e acrescenta a cada pessoa o `rotulo` usado no roteiro
    ("CLARA:") e o `genero`."""
    global _elenco
    if _elenco is not None:
        return _elenco
    try:
        with open(APRESENTADORES_FILE, encoding="utf-8") as f:
            dados = json.load(f)
        assert dados["mulheres"] and dados["homens"]
        for a in dados["mulheres"] + dados["homens"]:
            assert a["nome"] and a["voz"] and a["persona"]
    except FileNotFoundError:
        dados = _ELENCO_PADRAO
    except (ValueError, KeyError, TypeError, AssertionError) as e:
        print(f"[AVISO] {APRESENTADORES_FILE} inválido ({e!r}); usando Ana e Leo", file=sys.stderr)
        dados = _ELENCO_PADRAO
    for chave, genero in (("mulheres", "F"), ("homens", "M")):
        for a in dados[chave]:
            a.setdefault("rate", "+0%")
            a.setdefault("pitch", "+0Hz")
            a["genero"], a["rotulo"] = genero, a["nome"].strip().upper()
    _elenco = dados
    return _elenco


def todos_apresentadores() -> dict[str, dict]:
    elenco = carregar_elenco()
    return {a["rotulo"]: a for a in elenco["mulheres"] + elenco["homens"]}


def escolher_dupla(hoje: date | None = None) -> tuple[dict, dict]:
    """(quem explica, quem conduz) do dia. Quem explica é sempre mulher. A
    dupla muda a cada `rodizio_dias` dias; a cada `dupla_feminina_a_cada`
    trocas entra uma dupla de duas mulheres (alternando quem conduz). Como o
    elenco feminino e o masculino têm tamanhos diferentes, as duplas mistas
    também vão variando. A conta depende só da data, então a repescagem do
    mesmo dia cai na mesma dupla."""
    elenco = carregar_elenco()
    mulheres, homens = elenco["mulheres"], elenco["homens"]
    if DUPLA_FORCADA:
        todos = todos_apresentadores()
        achados = [todos.get(n.strip().upper()) for n in DUPLA_FORCADA.split(",")]
        if len(achados) == 2 and all(achados):
            a, b = achados
            if a["genero"] == "F":
                return a, b
            if b["genero"] == "F":
                return b, a
        print(f"[AVISO] DUPLA='{DUPLA_FORCADA}' precisa de 2 nomes do elenco, com 1 mulher; sorteando", file=sys.stderr)
    dias = max(1, int(elenco.get("rodizio_dias", 2)))
    bloco = (hoje or datetime.now(BRT).date()).toordinal() // dias
    a_cada = int(elenco.get("dupla_feminina_a_cada", 3)) if len(mulheres) >= 2 else 0
    if a_cada > 0 and bloco % a_cada == a_cada - 1:
        pares = [(x, y) for k, x in enumerate(mulheres) for y in mulheres[k + 1 :]]
        vez = bloco // a_cada
        a, b = pares[vez % len(pares)]
        return (a, b) if (vez // len(pares)) % 2 == 0 else (b, a)
    mista = bloco - (bloco // a_cada if a_cada > 0 else 0)
    return mulheres[mista % len(mulheres)], homens[mista % len(homens)]


def dupla_do_episodio(ep: dict) -> tuple[dict, dict]:
    """Dupla que gravou um episódio já salvo (a repescagem do áudio precisa
    das mesmas vozes), na ordem gravada: (quem explica, quem conduz). Sem
    registro, é um episódio antigo de Ana e Leo."""
    elenco = todos_apresentadores()
    achados = [elenco.get(n.upper()) for n in (ep.get("apresentadores") or LEGADO)]
    if len(achados) == 2 and all(achados):
        return achados[0], achados[1]
    print("[AVISO] apresentadores do episódio fora do elenco atual; usando a dupla de hoje", file=sys.stderr)
    return escolher_dupla()


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


def save_history(
    path: str,
    episodios: list[dict],
    items: list[dict],
    roteiro: str,
    manchete: str = "",
    audio_ok: bool = True,
    apresentadores: tuple[str, str] | None = None,
) -> None:
    """`audio_ok: false` marca um episódio cujo texto já foi pro Telegram mas
    o áudio falhou — a repescagem do workflow refaz só o áudio dele."""
    agora = datetime.now(BRT)
    episodios = episodios + [
        {
            "data": agora.strftime("%Y-%m-%d"),
            "gerado_em": agora.isoformat(timespec="seconds"),
            "noticias": [{"titulo": i["title"], "link": i["link"]} for i in items],
            "roteiro": roteiro,
            "manchete": manchete,
            "audio_ok": audio_ok,
            "apresentadores": list(apresentadores or LEGADO),
        }
    ]
    gravar_historico(path, episodios)


def gravar_historico(path: str, episodios: list[dict]) -> None:
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


# ---------- Matéria completa das notícias em destaque ----------

_PRIORIDADE = re.compile(
    r"\b(aws|amazon web services|azure|google cloud|gcp|kubernetes|k8s|devops|"
    r"terraform|docker|cloud|nuvem)\b",
    re.I,
)
_RELEVANTE = re.compile(
    r"\b(ia|ai|openai|anthropic|llm|gemini|chatgpt|seguran[çc]a|vulnerabilidade|"
    r"falha|brecha|hackers?|vazamento|ataque|security|breach|lan[çc]a|launch)\b",
    re.I,
)
_PROMO = re.compile(
    r"(oferta|desconto|cupom|promo[çc][ãa]o|% ?off|achados|menor pre[çc]o|"
    r"\bdeals?\b|\bsale\b|giving away|for free|\bget (the|a|an|\w+['’]s)\b|"
    r"review|melhores .* para|como (atualizar|baixar|usar|ativar)|how to)",
    re.I,
)


# Categorias com vaga garantida no episódio (bloco próprio) e na matéria
# completa. "console" e "switch" soltos ficam de fora: AWS Console e switch
# de rede não são games. "vivo"/"claro"/"tim" também: são palavras comuns.
_GAMES = re.compile(
    r"\b(games?|gaming|gamers?|videogames?|jogos?|playstation|ps5|xbox|nintendo|"
    r"steam|gta|ubisoft|epic games|valve|blizzard|capcom|bandai|rockstar|"
    r"est[úu]dio de (jogos|games))\b",
    re.I,
)
# Política: as fontes marcadas com `politica` no feeds.txt já valem; este
# regex pega só o que aparece no TÍTULO de feeds de tecnologia (regulação,
# STF, Congresso...).
_POLITICA = re.compile(
    r"\b(congresso|senado|senador(a|es)?|c[âa]mara dos deputados|deputad[oa]s?|"
    r"stf|supremo|tse|planalto|governo|ministr[oa]s?|presidente da rep[úu]blica|"
    r"lula|elei[çc][õo]es|eleitoral|projeto de lei|marco civil|big techs?|anpd)\b",
    re.I,
)
_MOBILE = re.compile(
    r"\b(smartphones?|phones?|tablets?|celular(es)?|iphone|android|galaxy|motorola|xiaomi|pixel|"
    r"oneplus|operadoras?|5g|6g|anatel|telecom|telefonia|mobile|dobr[áa]ve(l|is)|"
    r"foldables?|ios)\b",
    re.I,
)


def categoria(it: dict) -> str | None:
    if it.get("tag") == "politica":
        return "politica"
    texto = f"{it['title']} {it['summary']} {it['source']}"
    if _PRIORIDADE.search(texto):
        return "cloud"
    # O título decide primeiro: "chip de celular focado em gaming" é mobile.
    games_titulo, mobile_titulo = _GAMES.search(it["title"]), _MOBILE.search(it["title"])
    if games_titulo and not mobile_titulo:
        return "games"
    if mobile_titulo:
        return "mobile"
    if _GAMES.search(texto):
        return "games"
    if _MOBILE.search(texto):
        return "mobile"
    if _POLITICA.search(it["title"]):
        return "politica"
    return None


def escolher_destaques(items: list[dict]) -> list[dict]:
    """Ordena as candidatas a "matéria completa" sem gastar chamada extra no
    Gemini: sobe quem vários sites cobriram (sinal de assunto grande), quem
    é de cloud/DevOps e o que parece relevante; derruba promoção. Logo
    depois da melhor de todas vêm a melhor de cloud, de games, de mobile e de
    política, pra esses blocos também terem conteúdo de verdade (se a melhor de todas já é
    de uma dessas áreas, a vaga vai pra segunda melhor daquela área, porque o
    bloco precisa de outra notícia além da manchete)."""
    tokens = [_title_tokens(i["title"]) for i in items]
    pontuadas = []
    for idx, it in enumerate(items):
        if it["link"].startswith("https://news.google.com/") or not it["link"]:
            continue  # link de redirecionamento do Google: não dá pra baixar
        texto = f"{it['title']} {it['summary']}"
        fontes = {
            items[j]["source"]
            for j in range(len(items))
            if j != idx
            and len(tokens[idx] & tokens[j]) >= 3
            and len(tokens[idx] & tokens[j]) / len(tokens[idx] | tokens[j]) >= 0.3
        } - {it["source"]}
        score = 2 * min(len(fontes), 3)  # teto: assunto grande não atropela cloud
        score += 4 if _PRIORIDADE.search(texto) else 0
        score += 1 if _RELEVANTE.search(texto) else 0
        score -= 6 if _PROMO.search(texto) else 0
        pontuadas.append((score, it.get("ts") or 0.0, idx))
    pontuadas.sort(reverse=True)
    ordem = [items[idx] for score, _, idx in pontuadas if score > 0]
    topo = ordem[0] if ordem else None
    # inseridas na posição 1, nesta ordem: cloud, games, mobile, política
    for cat in ("politica", "mobile", "games", "cloud"):
        melhor = next(
            (
                items[idx]
                for score, _, idx in pontuadas
                if score >= 0 and items[idx] is not topo and categoria(items[idx]) == cat
            ),
            None,
        )
        if melhor is not None:
            if melhor in ordem:
                ordem.remove(melhor)
            ordem.insert(min(1, len(ordem)), melhor)
    return ordem


def baixar_materia(url: str) -> str:
    import trafilatura

    resp = requests.get(url, headers=HTTP_HEADERS, timeout=(10, 20))
    resp.raise_for_status()
    texto = trafilatura.extract(
        resp.text, include_comments=False, include_tables=False, favor_precision=True
    )
    return re.sub(r"\s+", " ", texto or "").strip()


def anexar_materias_completas(items: list[dict]) -> None:
    """Coloca `full_text` nas MAX_FULL_ARTICLES melhores notícias. Site que
    bloqueia ou página sem texto aproveitável é pulado e vai pra próxima."""
    if MAX_FULL_ARTICLES <= 0:
        return
    feitas = 0
    tentativas = 0
    assuntos: list[tuple[set[str], int]] = []  # (palavras do título, matérias baixadas)
    for it in escolher_destaques(items):
        if tentativas >= MAX_FULL_ARTICLES * 3:
            break
        # No máximo 2 matérias por assunto (a manchete ganha contexto de dois
        # sites); sem isso, num dia de iOS novo as 5 vagas iam pro iOS.
        tokens = _title_tokens(it["title"])
        k = next((n for n, (t, _) in enumerate(assuntos) if len(tokens & t) >= 2), None)
        if k is not None and assuntos[k][1] >= 2:
            continue
        tentativas += 1
        try:
            texto = baixar_materia(it["link"])
        except Exception as e:
            print(f"[AVISO] matéria completa indisponível ({it['source']}): {_resumo_erro(e)}", file=sys.stderr)
            continue
        if len(texto) < 400:
            continue
        it["full_text"] = texto[:4000]
        feitas += 1
        if k is None:
            assuntos.append((tokens, 1))
        else:
            assuntos[k] = (assuntos[k][0] | tokens, assuntos[k][1] + 1)
        print(f"[OK] matéria completa: {it['source']} — {it['title'][:70]}")
        if feitas >= MAX_FULL_ARTICLES:
            break
    print(f"[INFO] {feitas} notícia(s) em destaque com matéria completa")


def _rotulo_dia(data_iso: str) -> str:
    """'ontem', 'anteontem' ou 'segunda-feira (14/09)' — pros ganchos do roteiro."""
    try:
        d = datetime.strptime(data_iso, "%Y-%m-%d").date()
    except ValueError:
        return data_iso
    delta = (datetime.now(BRT).date() - d).days
    if delta == 0:
        return "hoje mais cedo"
    if delta == 1:
        return f"ontem, {DIAS_SEMANA[d.weekday()]} ({d:%d/%m})"
    if delta == 2:
        return f"anteontem, {DIAS_SEMANA[d.weekday()]} ({d:%d/%m})"
    return f"{DIAS_SEMANA[d.weekday()]} ({d:%d/%m})"


def _estilo_duo(dupla: tuple[dict, dict], gastas: list[str]) -> str:
    """`dupla` = (quem explica, quem conduz). Quem explica é sempre uma
    mulher; quem conduz pode ser homem ou mulher (dia de dupla feminina)."""
    m, h = dupla
    M, H, Mn, Hn = m["rotulo"], h["rotulo"], m["nome"], h["nome"]
    pro_h = "pra" if h["genero"] == "F" else "pro"
    art_m, art_h = "a", "a" if h["genero"] == "F" else "o"
    proibidas = (
        "- Estas expressões já se repetiram nos últimos episódios e estão PROIBIDAS "
        "hoje (nem parecidas): " + "; ".join(f'"{g}"' for g in gastas) + "."
        if gastas
        else ""
    )
    hoje = datetime.now(BRT)
    contexto_dia = (
        f"Hoje é {DIAS_SEMANA[hoje.weekday()]}, "
        f"{hoje.day} de {MESES[hoje.month - 1]} de {hoje.year}."
    )
    return f"""Você é o roteirista de um podcast DIÁRIO de notícias de tecnologia, com um
bloco de política, em português do Brasil, apresentado por dois hosts com
personalidades bem definidas. Este episódio cobre as últimas 24 horas — é um
resumo do DIA, nunca da semana. O elenco do podcast muda a cada poucos dias:
hoje quem apresenta são {Mn} e {Hn}. Escreva só pra essa dupla. {Mn} é quem
explica os conceitos; {Hn} conduz (abre, faz as viradas e encerra).

{M} — {m["persona"]}

{H} — {h["persona"]}

TOM GERAL: descontraído, com charme e personalidade. Provocação bem-humorada
entre os dois, sempre leve. Sem palavrão pesado, sem ofender ninguém.

AS APRESENTADORAS (vale pra qualquer dupla, até quando as duas são mulheres):
falam de um jeito sensual e envolvente — voz macia e segura, frase de efeito
dita devagar, charme, elogio provocante, um duplo sentido leve e elegante de
vez em quando, a confiança de quem sabe que está sendo ouvida. A sedução está
no tom e no jeito, nunca no conteúdo: nada explícito, nada vulgar, nada sexual.
Cada uma do seu jeito (veja a persona), sem virar caricatura.

NADA DE BORDÃO NEM FRASE FEITA (o ouvinte já percebeu a repetição):
- Não existe bordão de abertura, de despedida, de pergunta nem de transição.
  Cada fala nasce do que acabou de ser dito: a pergunta seguinte depende da
  resposta anterior, a reação depende do tamanho e do clima da notícia.
- Pergunta muda de forma e de função: às vezes é dúvida real, às vezes
  provocação, às vezes uma hipótese, às vezes nem é pergunta ("não entendi
  essa parte"). Nunca use a mesma estrutura de pergunta duas vezes no episódio
  — nada de uma pergunta-padrão sobre "o que muda pra quem ouve" em toda
  notícia; o impacto prático entra quando faz sentido, cada vez de um jeito.
- As frases entre aspas neste prompt são só pra mostrar o tom: NÃO as use
  como estão. Invente as suas, a partir do assunto do dia.
{proibidas}

APRESENTAÇÃO DO PROGRAMA (obrigatória, SEMPRE nesta ordem, no começo):
- A primeira fala é de {H} e traz a vinheta do programa com estas palavras
  exatas: "{VINHETA}", seguida do dia da semana e da data. {contexto_dia}
  Antes da vinheta pode vir uma frase curta e criativa sobre o clima do dia
  (diferente a cada episódio); a vinheta em si não muda.
- Logo depois da vinheta, os dois se apresentam pelo nome: {H} diz que é
  {art_h} {Hn} (na mesma fala da vinheta ou na seguinte) e {M} diz, na fala
  dela, que é {art_m} {Mn}. Cada apresentação tem um toque pessoal no estilo de
  cada um, com palavras diferentes a cada dia — só o nome e a ideia de "eu
  sou" se repetem.
- Só depois disso vem a conversa e o gancho do assunto principal.

DINÂMICA (o que faz soar como conversa de verdade):
- Depois das apresentações, o primeiro minuto soa como duas pessoas se
  encontrando: uma reação, um comentário sobre o dia, e aí o gancho da
  manchete.
- Cada um fala SEMPRE em primeira pessoa ("eu acho", "eu falei"). Fora da
  apresentação do começo, ninguém fala de si mesmo na terceira pessoa nem diz
  o próprio nome: numa fala de
  {M}, o único nome que aparece é "{Hn}", e vice-versa. Quem faz uma pergunta
  é quem recebe a resposta — nunca jogue a pergunta de volta pra quem
  perguntou.
- Eles discordam de verdade às vezes, um completa o raciocínio do outro, fazem
  gancho entre uma notícia e a próxima.
- {H} encerra ligando com algo que rolou no próprio episódio (nada de bordão)
  e {M} fica com a última palavra.
- NUNCA chame o episódio de "resumo semanal", "boletim da semana" ou qualquer
  variação de semanal — é sempre "resumo de hoje", "episódio de hoje", "edição
  de hoje". Isso é um erro grave, evite a todo custo.

SOAR HUMANO, NÃO LOCUTOR (a parte mais importante do roteiro):
- É uma conversa entre duas pessoas que trabalham juntas e se gostam, não dois
  locutores lendo notícia em revezamento. Um reage ao que o outro acabou de
  dizer ANTES de seguir pro próximo assunto.
- Misture o tamanho das falas. Umas bem curtas, de 1 a 6 palavras (reação
  de verdade ao que o outro disse); várias de 10 a 25 palavras; e só
  de vez em quando uma de até 45 palavras (na manchete). Pelo menos 1 em cada
  4 falas é curta. NUNCA duas falas seguidas com mais de 35 palavras.
- Quem fala pode ser cortado: interrompa uma frase no meio com "—" e deixe o
  outro entrar; quem foi cortado retoma depois ("Então, como eu ia dizendo,
  ..."). Faça isso de 3 a 5 vezes no episódio.
- Marcas de fala real, sem exagero: hesitação ("é... tipo", "como é que eu
  falo"), autocorreção ("quer dizer, não, peraí"), pensar em voz alta, e
  palavrinhas como "olha", "cara", "pô", "nossa", "hum", "ué", "sei lá".
- Exclamação é rara: no máximo 1 em cada 4 falas termina em "!". O resto
  termina em ponto, interrogação ou reticências. Tom de locutor de propaganda
  ("Olha isso!" a cada fala) é proibido.
- Não transforme em entrevista. Não é "{H} pergunta, {M} responde" em toda
  notícia: os dois trazem informação, os dois opinam, às vezes a pergunta vem
  de {M}, às vezes um sabe uma coisa que o outro não sabia e reage.
- Chamar o outro pelo nome é raro: no máximo 1 em cada 6 falas. Gente de
  verdade não repete o nome do colega o tempo todo.
- Transições entre notícias variadas e naturais, cada uma diferente. "E por
  falar em...", "e pra fechar", "chegou a hora do..." e "e agora segura essa"
  estão proibidas. Prefira sair do que foi dito: uma reação, uma ligação de
  ideia entre as duas notícias, ou só uma virada seca.
- Humor que nasce da notícia: uma tirada, uma comparação inesperada, uma ironia
  com a situação. Nada de piada de manual. Se rir, escreva a reação falada
  ("ai, essa foi boa", "não, para") — nunca "haha" nem "(risos)".
- Números do jeito que se fala: "uns quarenta por cento", "quase três
  bilhões". No máximo 2 números por fala; ficha técnica inteira vira conversa.
- Uma ideia por fala. Se tem três informações, são três falas, com reação no
  meio.

PORTUGUÊS NATURAL (muito importante):
- Escreva como brasileiros realmente falam, não como uma tradução. Frases
  curtas, contrações naturais ("tá", "pra", "cê", "né", "bora"), sem construções
  formais ou empoladas que soam artificiais em fala espontânea.
  Evite conectivos de texto escrito ("outrossim", "ademais", "não obstante") e
  frases genéricas de robô ("é importante ressaltar que...", "vale destacar
  que...", "em suma").
- Cada frase deve soar como algo que uma pessoa de verdade diria em voz alta
  numa conversa descontraída, com ritmo e respiração naturais — não como texto
  de artigo lido em voz alta.

FORMATO OBRIGATÓRIO: cada fala em sua própria linha, começando com "{M}:" ou
"{H}:". Entre um bloco e outro do episódio, uma linha contendo apenas ---
(vira uma pausa no áudio). O --- aparece SÓ na troca de bloco (umas 8 vezes
no episódio inteiro), NUNCA entre uma fala e outra do mesmo bloco — mesmo que
os episódios anteriores tenham feito diferente. Nenhuma outra linha fora desse formato — sem
títulos, sem markdown, sem asteriscos, sem emojis, sem rubricas como (risos)
ou [vinheta].

ESTRUTURA DO EPISÓDIO (7 a 9 minutos, nesta ordem):
1. ABERTURA (curta): vinheta do programa com dia e data, {H} e {M} se
   apresentam, e um gancho do assunto principal pra prender a atenção.
2. MANCHETE DO DIA (uns 2 minutos): o assunto mais importante, a fundo. Cubra
   o que aconteceu, o contexto (como chegamos aqui), por que importa e o que
   muda na prática pra quem ouve. Use os fatos da MATÉRIA COMPLETA quando ela
   existir.
3. RADAR CLOUD & DEVOPS: pelo menos 1 notícia de AWS, Azure, Google Cloud,
   DevOps, Kubernetes ou infraestrutura — é a área de quem ouve, então é o
   bloco mais importante depois da manchete. Se houver várias, fale de 2 ou 3.
4. RADAR GAMES: pelo menos 1 notícia da INDÚSTRIA de games — lançamentos
   importantes, estúdios, publishers, consoles, vendas, aquisições, demissões,
   regulação. Não vale promoção, cupom nem "jogo grátis por tempo limitado".
5. RADAR MOBILE: pelo menos 1 notícia da indústria mobile e de tecnologia de
   consumo — smartphones, sistemas (iOS/Android), fabricantes, operadoras, 5G,
   mercado de apps. Mesma regra: nada de oferta ou review de produto.
6. RADAR POLÍTICA: de 1 a 3 notícias de política brasileira — Congresso,
   governo federal, STF/TSE, eleições, regulação de tecnologia e internet.
   Prefira o que mexe com a vida de quem ouve (economia, impostos, regras pra
   internet, IA e plataformas). REGRAS DESTE BLOCO: imparcialidade total.
   Relate fatos, atribua posições a quem as disse ("segundo o ministro", "o
   relator defende"), mostre os dois lados quando há disputa e deixe claro se é
   decisão, proposta ou só declaração. Os apresentadores NÃO tomam partido, não
   torcem, não ironizam político, partido, governo ou eleitor e não usam
   adjetivo carregado. A personalidade deles aparece no ritmo e na reação ao
   impacto prático ("e isso muda o quê no bolso de quem ouve?"), nunca em
   opinião política. Linguagem simples: explique sigla e rito (relator, PEC,
   medida provisória).
7. RODADA RÁPIDA: de 2 a 4 notícias curtas, poucas falas cada, com ritmo.
8. TERMO DO DIA: um termo técnico que apareceu no episódio, explicado por
   {M} com uma analogia do dia a dia. Nunca repita um termo já explicado nos
   episódios anteriores. Os PAPÉIS são fixos; as palavras mudam todo dia:
   a) {H} puxa o termo a partir da conversa (algo que apareceu numa notícia,
      uma dúvida que ficou, uma provocação) e passa a vez pra {Mn}. A frase
      é diferente a cada episódio — nunca a fórmula "{Mn}, o que é ...?" nem
      um pedido de "analogia do dia a dia".
   b) {M} responde na fala seguinte, em primeira pessoa, já explicando — ela
      NÃO repete a pergunta, NÃO devolve a vez {pro_h} {Hn} e NÃO pede pra
      alguém explicar.
   c) {H} pode reagir ou puxar UMA dúvida de acompanhamento, que {M}
      responde.
   Errado (nunca faça): {M} perguntando o termo {pro_h} {Hn}; {H} respondendo
   "explica aí, {Mn}" pra uma pergunta que a própria {Mn} fez; qualquer um
   falando o próprio nome fora da apresentação.
9. ENCERRAMENTO: {H} fecha ligando com o que rolou no episódio e {M} dá a
   última palavra. Nada de bordão.
Os blocos CLOUD & DEVOPS, GAMES, MOBILE e POLÍTICA são obrigatórios. Se a
manchete do dia já for de uma dessas áreas, o bloco correspondente ainda
precisa de OUTRA notícia daquela área. Só pule um deles se realmente não
existir nenhuma notícia da área na lista (nesse caso, sem comentar a ausência).

EXPLICAR BEM (o ouvinte precisa entender, não só saber que aconteceu):
- Toda notícia responde "o que aconteceu" e "por que isso importa".
- Sigla ou termo técnico: explique em uma frase simples na primeira vez.
- Traga números, nomes e detalhes concretos das notícias. Não invente fatos,
  números ou declarações que não estejam no material; se a informação é vaga,
  fale menos daquele assunto.
- Agrupe notícias repetidas (vários sites cobrindo o mesmo assunto) em um item só.
- Priorize: AWS e nuvem (Azure, Google Cloud), DevOps, IA, lançamentos
  relevantes, programação, segurança e a política que mexe com tecnologia.
- Ignore publieditorial, promoções e reviews de produto irrelevantes."""


_ESTILO_SOLO = """Você é o roteirista de um podcast DIÁRIO de notícias de tecnologia em
português do Brasil. Este episódio cobre as últimas 24 horas — é um resumo do
DIA, nunca da semana; nunca diga "resumo semanal" ou variações.

Escreva um roteiro de podcast de 3 a 5 minutos:
- Comece com a vinheta do programa: "{VINHETA}", com o dia da semana e a data.
- Agrupe notícias repetidas (vários sites cobrindo o mesmo assunto) em um item só.
- Priorize: AWS e nuvem (Azure, Google Cloud), DevOps, IA, lançamentos relevantes,
  programação e segurança.
- Ignore publieditorial, promoções e reviews de produto irrelevantes.
- Ordem: manchete do dia a fundo (o que aconteceu, por que importa, o que muda),
  depois pelo menos 1 de cloud/DevOps, depois pelo menos 1 da indústria de games e
  pelo menos 1 da indústria mobile (nada de promoção), depois 1 de política
  brasileira (imparcial: só fatos, posições atribuídas a quem as disse, sem
  tomar partido), depois notas rápidas, e um "termo do dia" explicado com
  analogia. Entre um bloco e outro, uma linha contendo apenas ---.
- Não invente fatos que não estejam no material; use a MATÉRIA COMPLETA quando houver.
- Fale de forma natural, como um apresentador, sem markdown, sem asteriscos,
  sem emojis, sem listas — apenas texto corrido pronto para ser lido em voz alta.
- Português natural e fluido, como brasileiros realmente falam: frases curtas,
  contrações naturais ("tá", "pra", "né"), sem conectivos formais de texto
  escrito e sem frases genéricas de robô ("é importante ressaltar que...").
- Encerre com uma despedida curta."""


def montar_prompt(items: list[dict], episodios: list[dict], dupla: tuple[dict, dict]) -> str:
    blocos = []
    for n, i in enumerate(items, 1):
        bloco = f"[{n}] FONTE: {i['source']}\nTÍTULO: {i['title']}\nRESUMO: {i['summary']}"
        if i.get("full_text"):
            bloco += f"\nMATÉRIA COMPLETA: {i['full_text']}"
        blocos.append(bloco)
    corpus = "\n\n".join(blocos)
    estilo = (
        _estilo_duo(dupla, expressoes_gastas(episodios))
        if PODCAST_STYLE == "duo"
        else _ESTILO_SOLO.replace("{VINHETA}", VINHETA)
    )

    anteriores = ""
    if episodios:
        roteiros = "\n\n".join(
            f"--- EPISÓDIO DE {_rotulo_dia(ep.get('data', '?')).upper()} "
            f"(apresentado por {' e '.join(ep.get('apresentadores') or LEGADO)}) ---\n"
            f"{ep.get('roteiro', '')[:8000]}"
            for ep in episodios
        )
        anteriores = f"""

EPISÓDIOS ANTERIORES: abaixo estão os roteiros dos últimos episódios, que o
ouvinte já escutou.

NÃO SEJA REPETITIVO (muito importante):
- Não volte a comentar nenhum assunto que já apareceu neles, mesmo que hoje
  venha de outro site ou com outro título.
- Não reaproveite bordões de abertura e despedida, piadas, analogias, frases
  de efeito nem o termo do dia desses episódios. Também não copie o formato
  nem o ritmo deles: as regras de formato, de SOAR HUMANO e o roteiro do TERMO
  DO DIA acima valem mais que qualquer coisa que esses episódios tenham feito
  (por exemplo, não imite falas longas cheias de exclamação).
- Se sobrarem poucas notícias inéditas, faça um episódio mais curto em vez de
  requentar assunto.

GANCHOS COM EPISÓDIOS ANTERIORES (faz o podcast parecer uma série):
- Quando uma notícia de hoje for continuação, consequência ou reação a algo
  desses episódios, faça a ligação de forma natural citando quando foi, cada
  vez com palavras diferentes (não comece sempre com "lembra que..."). Aí
  fale só da novidade, sem recontar a história.
- Os apresentadores mudam a cada poucos dias (veja o cabeçalho de cada
  episódio): só diga "eu falei/eu disse" se quem fala estava naquele episódio;
  senão use "a gente falou" ou "o pessoal comentou".
- De 1 a 3 ganchos por episódio, só quando a ligação for real — nunca force.

{roteiros}"""

    prompt = f"""{estilo}

ESCRITA PRA VOZ (o roteiro vai ser lido por uma voz sintética):
- Números em nomes de jogos, produtos e versões sempre com algarismos arábicos:
  "Diablo 4", "GTA 6", "Final Fantasy 7" — NUNCA números romanos (IV, V, VII),
  porque a voz lê como letra.
- Escreva nomes de empresas e produtos com a grafia normal (a pronúncia é
  ajustada depois).

NOTAS DO EPISÓDIO (obrigatório): depois da última fala, escreva uma linha
exatamente assim: ===NOTAS===
E abaixo dela, uma linha por notícia comentada, na ordem do episódio:
NÚMERO | BLOCO | título curto em português
- NÚMERO é o número entre colchetes da notícia na lista abaixo (se juntou
  várias, use a principal).
- BLOCO é um destes: Manchete, Cloud & DevOps, Games, Mobile, Política, Rodada rápida.
- Por último, uma linha: TERMO | Termo do dia | termo — definição em uma frase{anteriores}

Abaixo estão as notícias das últimas {HOURS_WINDOW} horas coletadas de vários sites
(as que já foram usadas em episódios anteriores já foram removidas).

NOTÍCIAS DE HOJE:
{corpus}"""

    return prompt


def roteirizar(items: list[dict], episodios: list[dict], dupla: tuple[dict, dict]) -> str:
    """Prompt completo nas primeiras rodadas; na última, uma versão enxuta
    (metade das notícias, sem matéria completa nem episódios anteriores
    longos). Em 13/10 todos os modelos deram 500 por minutos seguidos — se
    for o tamanho do pedido pesando no servidor, a versão enxuta passa."""
    enxutos = [{k: v for k, v in it.items() if k != "full_text"} for it in items[:60]]
    ultimo = [{**ep, "roteiro": ep.get("roteiro", "")[:3000]} for ep in episodios[-1:]]
    return gerar_com_gemini(
        [montar_prompt(items, episodios, dupla), montar_prompt(enxutos, ultimo, dupla)]
    )


def gerar_com_gemini(prompts: list[str]) -> str:
    """Um 503 costuma ser aquele modelo específico lotado, não a conta/chave.
    Por isso cada rodada passa UMA vez por cada modelo da fila (sem esperar
    entre eles) e só pausa entre rodadas. Antes eram 2 tentativas + 10s de
    espera por modelo, o que demorava pra chegar num modelo livre. A última
    rodada usa o último prompt da lista (o enxuto)."""
    chain = [GEMINI_MODEL]
    # Espera antes de cada rodada. Instabilidade geral do Gemini (500 em
    # todos os modelos) costuma durar minutos, então as pausas crescem; se
    # nem assim der, a repescagem do workflow tenta de novo horas depois.
    pausas = [0, 30, 90, 180]
    descoberta_feita = False
    last_error: Exception | None = None

    for rodada, pausa in enumerate(pausas, 1):
        prompt = prompts[-1] if rodada == len(pausas) else prompts[0]
        if pausa:
            print(
                f"[AVISO] rodada {rodada - 1} falhou em todos os modelos; "
                f"nova rodada em {pausa}s"
                + (" com prompt enxuto" if prompt is not prompts[0] else "")
                + "...",
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
                texto = _extrair_texto(resp.json())
                print(f"[OK] roteiro gerado por {model}")
                return texto
            except (requests.RequestException, KeyError, IndexError, ValueError) as e:
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
        f"Gemini falhou em todos os modelos tentados ({', '.join(chain)}): "
        f"{_resumo_erro(last_error) if last_error else '?'}"
    )


def _extrair_texto(data: dict) -> str:
    """Junta TODAS as partes de texto da resposta (modelos com raciocínio às
    vezes dividem em várias; pegar só a primeira cortava o roteiro no meio,
    como no episódio de 15/09) e recusa resposta interrompida."""
    cand = data["candidates"][0]
    motivo = cand.get("finishReason", "STOP")
    partes = cand["content"]["parts"]
    texto = "".join(p.get("text", "") for p in partes if not p.get("thought")).strip()
    if motivo not in ("STOP", "FINISH_REASON_UNSPECIFIED") or not texto:
        raise ValueError(f"resposta incompleta (finishReason={motivo}, {len(texto)} chars)")
    return texto


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
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            # Folga grande: o raciocínio interno do modelo também consome
            # tokens de saída e, com o limite padrão, o roteiro saía cortado.
            "generationConfig": {"maxOutputTokens": 32768},
        },
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


# ---------- Texto pra voz: pronúncia ----------

_ROMANOS = {
    "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9,
    "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16,
}
# "Diablo V" -> "Diablo 5": romano solto logo depois de uma palavra com
# inicial maiúscula. "I" e "X" ficam de fora de propósito ("iPhone X",
# "rede social X", "Mega Man X" não são números).
_ROMANO_RE = re.compile(
    r"\b([A-ZÀ-Ý][\w'’:]*) (" + "|".join(sorted(_ROMANOS, key=len, reverse=True)) + r")(?![\w-])"
)
_pronuncias: list[tuple[re.Pattern, str]] | None = None


def _carregar_pronuncias() -> list[tuple[re.Pattern, str]]:
    """Lê pronuncia.txt (`Termo = como falar`). Termo todo em maiúsculas
    (sigla) casa exatamente; o resto ignora maiúsculas/minúsculas."""
    global _pronuncias
    if _pronuncias is not None:
        return _pronuncias
    regras = []
    try:
        with open(PRONUNCIA_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                termo, _, fala = (x.strip() for x in line.partition("="))
                if termo and fala:
                    regras.append((termo, fala))
    except FileNotFoundError:
        pass
    regras.sort(key=lambda r: len(r[0]), reverse=True)  # "ChatGPT" antes de "GPT"
    _pronuncias = [
        (
            re.compile(
                r"(?<!\w)" + re.escape(termo) + r"(?!\w)",
                0 if termo.isupper() else re.I,
            ),
            fala,
        )
        for termo, fala in regras
    ]
    return _pronuncias


def texto_para_fala(texto: str) -> str:
    """Ajustes que valem só pro áudio (o texto no Telegram fica intacto)."""
    texto = _ROMANO_RE.sub(lambda m: f"{m.group(1)} {_ROMANOS[m.group(2)]}", texto)
    texto = re.sub(r"(?<=\w) x (?=\w)", " versus ", texto)
    for padrao, fala in _carregar_pronuncias():
        texto = padrao.sub(fala, texto)
    return texto


# ---------- Áudio ----------

_MP3_BITRATES = {
    "1": [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
    "2": [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160],
}
_MP3_SAMPLERATES = {"1": [44100, 48000, 32000], "2": [22050, 24000, 16000], "2.5": [11025, 12000, 8000]}


def _info_frame(h: bytes) -> tuple[int, int, int] | None:
    """Cabeçalho de frame MP3 Layer III -> (bytes do frame, sample rate,
    amostras por frame), ou None se não for um cabeçalho válido."""
    if len(h) < 4 or h[0] != 0xFF or h[1] & 0xE0 != 0xE0:
        return None
    versao = {3: "1", 2: "2", 0: "2.5"}.get((h[1] >> 3) & 0b11)
    idx_br, idx_sr = h[2] >> 4, (h[2] >> 2) & 0b11
    if not versao or (h[1] >> 1) & 0b11 != 1 or idx_br in (0, 15) or idx_sr == 3:
        return None
    bitrate = _MP3_BITRATES["1" if versao == "1" else "2"][idx_br] * 1000
    sr = _MP3_SAMPLERATES[versao][idx_sr]
    tamanho = (144 if versao == "1" else 72) * bitrate // sr + ((h[2] >> 1) & 1)
    return tamanho, sr, 1152 if versao == "1" else 576


def _frames_mp3(dados: bytes) -> list[tuple[int, int]]:
    """[(início, tamanho), ...] de cada frame do MP3."""
    frames, i = [], 0
    while i + 4 <= len(dados):
        info = _info_frame(dados[i : i + 4])
        if not info:
            i += 1
            continue
        frames.append((i, info[0]))
        i += info[0]
    return frames


def silencio_mp3(amostra: bytes, ms: int) -> bytes:
    """Frames MP3 de silêncio no MESMO formato do áudio do edge-tts (lido do
    cabeçalho do primeiro frame), pra poder costurar bytes sem ffmpeg. Um
    frame Layer III com side info zerada decodifica como silêncio."""
    frames = _frames_mp3(amostra[:4096])
    if not frames or ms <= 0:
        return b""
    h = amostra[frames[0][0] : frames[0][0] + 4]
    # Sem CRC (bit de proteção = 1) e sem padding.
    header = bytes([h[0], h[1] | 0x01, h[2] & 0xFD, h[3]])
    tamanho, sr, amostras_frame = _info_frame(header)
    frame = header + bytes(tamanho - 4)
    return frame * max(1, round(ms / 1000 * sr / amostras_frame))


def aparar_silencio(mp3: bytes, margem_ms: int = 60) -> bytes:
    """Corta o silêncio que o edge-tts põe antes (~300 ms) e DEPOIS (1,5 a
    2,5 s!) de cada fala. Sem isso cada troca de apresentador tinha um vácuo
    de uns 2 segundos — boa parte do ar robótico do bate-papo. As pausas
    passam a ser só as de _pausa_ms. Na dúvida (sem decodificador, áudio
    estranho), devolve o áudio intacto."""
    frames = _frames_mp3(mp3)
    if not frames:
        return mp3
    _, sr, amostras_frame = _info_frame(mp3[frames[0][0] : frames[0][0] + 4])
    try:
        import miniaudio

        # Mesma taxa e canal do MP3: sem isso o miniaudio reamostra pra
        # 44,1 kHz estéreo e a conta amostra -> frame sai errada.
        dec = miniaudio.decode(
            mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=sr
        )
    except Exception as e:
        print(f"[AVISO] não deu pra aparar silêncio: {e}", file=sys.stderr)
        return mp3
    amostras = dec.samples
    limiar = 100  # ~ -50 dBFS
    passo = 4
    voz = [k for k in range(0, len(amostras), passo) if abs(amostras[k]) > limiar]
    if not voz:
        return mp3
    margem = margem_ms * sr // 1000
    # Mantém 2 frames extras no começo: o frame seguinte pode usar bits do
    # anterior (bit reservoir do MP3), e cortar colado gera estalo.
    ini = max(0, (voz[0] - margem) // amostras_frame - 2)
    fim = min(len(frames), -(-(voz[-1] + margem * 2) // amostras_frame) + 1)
    if fim <= ini:
        return mp3
    return b"".join(mp3[o : o + t] for o, t in frames[ini:fim])


async def _sintetizar(texto: str, voz: str, rate: str, pitch: str = "+0Hz") -> bytes:
    """Uma chamada ao edge-tts com até 3 tentativas. O serviço da Microsoft
    às vezes recusa a conexão (403/timeout) por alguns segundos; quando a
    recusa é permanente (mudaram a autenticação, como em 12/10), a saída é
    atualizar o edge-tts — o workflow já instala a versão mais nova."""
    import edge_tts

    for tentativa in range(3):
        try:
            communicate = edge_tts.Communicate(texto, voz, rate=rate, pitch=pitch)
            audio = b"".join(
                [chunk["data"] async for chunk in communicate.stream() if chunk["type"] == "audio"]
            )
            if audio:
                return audio
            raise RuntimeError("edge-tts não devolveu áudio")
        except Exception as e:
            if tentativa == 2:
                raise RuntimeError(f"voz (edge-tts) indisponível: {_resumo_erro_tts(e)}") from e
            print(f"[AVISO] edge-tts falhou ({_resumo_erro_tts(e)}); tentando de novo", file=sys.stderr)
            await asyncio.sleep(5 * (tentativa + 1))
    raise RuntimeError("unreachable")


def _resumo_erro_tts(e: Exception) -> str:
    """'403, Invalid response status' sem a URL gigante com o token."""
    return re.sub(r",?\s*url=\S+", "", str(e)).strip() or type(e).__name__


async def text_to_speech(text: str, out_path: str) -> None:
    text = re.sub(r"(?m)^\s*-{3,}\s*$", "", text)
    audio = await _sintetizar(texto_para_fala(text), TTS_VOICE, rate="+8%")
    with open(out_path, "wb") as out:
        out.write(audio)


def normalizar_roteiro(script: str, dupla: tuple[dict, dict]) -> str:
    """Conserta dois vícios do Gemini antes do áudio (e do histórico, pra ele
    não imitar amanhã):

    - Rótulo trocado: fala em que o apresentador chama A SI MESMO pelo nome
      ("ANA: Explica aí pra gente, Ana"), sem citar o outro — era pra ser do
      outro apresentador. Troca o rótulo.
    - '---' entre TODAS as falas (aconteceu de 09/10 em diante): virava uma
      pausa de bloco a cada troca e a conversa ficava picotada. Se há bem mais
      separadores que blocos possíveis, eles são removidos."""
    nomes = {a["rotulo"]: a["nome"] for a in dupla}
    outro = {dupla[0]["rotulo"]: dupla[1]["rotulo"], dupla[1]["rotulo"]: dupla[0]["rotulo"]}
    padrao = "|".join(re.escape(r) for r in nomes)
    linhas = []
    for linha in script.splitlines():
        m = re.match(rf"^\s*\**({padrao})\**\s*:\s*(.*)$", linha, re.I)
        if m:
            spk, fala = m.group(1).upper(), m.group(2)
            proprio, do_outro = nomes[spk], nomes[outro[spk]]
            vocativo = re.search(
                rf"(^|[,.!?;]\s*){proprio}\s*[,:!?]|,\s*{proprio}\s*[.!?:]*\s*$", fala
            )
            apresentacao = re.search(
                rf"\b(sou|aqui é|quem fala é)\s+(a\s+|o\s+)?{proprio}\b|\b{proprio}\s+aqui\b", fala, re.I
            )
            if vocativo and not apresentacao and not re.search(rf"\b{do_outro}\b", fala):
                print(f"[AVISO] fala com rótulo trocado corrigida: {spk}: {fala[:60]}", file=sys.stderr)
                spk = outro[spk]
            linha = f"{spk}: {fala}"
        linhas.append(linha)
    separadores = sum(1 for l in linhas if re.fullmatch(r"\s*-{3,}\s*", l))
    if separadores > 14:
        print(f"[AVISO] {separadores} separadores --- no roteiro (máx. ~9); removidos", file=sys.stderr)
        linhas = [l for l in linhas if not re.fullmatch(r"\s*-{3,}\s*", l)]
    return "\n".join(linhas)


def parse_dialogue(script: str) -> list[tuple[str, str]]:
    """Converte o roteiro em [(falante, fala), ...] — o falante é o rótulo de
    qualquer apresentador do elenco. Uma linha só com --- vira ("PAUSA", "")
    (troca de bloco). Linhas sem rótulo são continuação da fala anterior."""
    padrao = "|".join(re.escape(r) for r in todos_apresentadores())
    segments: list[tuple[str, str]] = []
    for raw in script.splitlines():
        if re.fullmatch(r"\s*-{3,}\s*", raw):
            if segments and segments[-1][0] != "PAUSA":
                segments.append(("PAUSA", ""))
            continue
        line = raw.strip().lstrip("*-•# ").strip()
        if not line:
            continue
        m = re.match(rf"(?i)^\**({padrao})\**\s*:\s*(.+)$", line)
        if m:
            segments.append((m.group(1).upper(), m.group(2).strip()))
        elif segments and segments[-1][0] != "PAUSA":
            speaker, text = segments[-1]
            segments[-1] = (speaker, text + " " + line)
    while segments and segments[-1][0] == "PAUSA":
        segments.pop()
    return segments


def _aberturas(fala: str) -> set[str]:
    """Começo (3 e 4 palavras) de cada frase da fala, sem pontuação e com os
    nomes do elenco trocados por <nome> — é onde mora bordão e frase feita
    ("e aí, <nome>, pronta pra ver", "tá mas o que"). Trecho com nome próprio
    fora do elenco ("a Google Cloud") é assunto, não vício: fica de fora."""
    nomes = {n.lower() for n in todos_apresentadores()} | {n.lower() for n in LEGADO}
    saida = set()
    for frase in re.split(r"(?<=[.!?])\s+", fala):
        w = []
        for k, x in enumerate(re.findall(r"[\wÀ-ÿ']+", frase)):
            if x.lower() in nomes:
                w.append("<nome>")
            elif k > 0 and x[0].isupper():
                break
            else:
                w.append(x.lower())
        saida.update(" ".join(w[:n]) for n in (3, 4) if len(w) >= n)
    return saida


def expressoes_gastas(episodios: list[dict], limite: int = 25) -> list[str]:
    """Começos de frase que apareceram em 2+ episódios anteriores. Assunto
    não se repete entre episódios (o histórico já barra), então o que se
    repete é vício de escrita: viram lista proibida no prompt de hoje."""
    por_ep = Counter()
    for ep in episodios:
        vistos: set[str] = set()
        for spk, fala in parse_dialogue(ep.get("roteiro", "")):
            if spk != "PAUSA":
                vistos |= _aberturas(fala)
        por_ep.update(vistos)
    vinheta = " ".join(re.findall(r"[\wÀ-ÿ']+", VINHETA.lower()))
    fixas = re.compile(r"^(e |oi |olá )?(eu sou|aqui é|e eu)\b")
    repetidas = sorted(
        (g for g, n in por_ep.items() if n >= 2 and g not in vinheta and not fixas.match(g)),
        key=lambda g: (-por_ep[g], -len(g)),
    )
    escolhidas: list[str] = []
    for g in repetidas:  # "tá mas o que" cobre "tá mas o"
        if not any(e.startswith(g) for e in escolhidas):
            escolhidas.append(g)
    return escolhidas[:limite]


def avaliar_dialogo(
    segments: list[tuple[str, str]], dupla: tuple[dict, dict], gastas: list[str] = ()
) -> None:
    """Mede o quanto o roteiro parece conversa (e não locutores revezando) e
    avisa no log quando regride. Linha de base dos episódios de 11 e 13/10,
    antes das regras de SOAR HUMANO: 0 falas curtas, ~45 palavras por fala,
    ~75% das falas com '!' e nome do colega em ~35% delas."""
    falas = [(spk, t) for spk, t in segments if spk != "PAUSA"]
    if len(falas) < 4:
        return
    n = len(falas)
    palavras = [len(t.split()) for _, t in falas]
    curtas = sum(p <= 6 for p in palavras)
    longas = sum(p > 40 for p in palavras)
    exclamacao = sum("!" in t for _, t in falas)
    cortes = sum("—" in t or "--" in t for _, t in falas)
    nomes = [a["nome"] for a in dupla]
    vocativos = sum(any(re.search(rf"\b{nm}\b", t) for nm in nomes) for _, t in falas)
    por_falar = sum(len(re.findall(r"por falar em", t, re.I)) for _, t in falas)
    gastas_usadas = sorted({g for _, t in falas for g in _aberturas(t) if g in set(gastas)})
    print(
        f"[DIÁLOGO] {n} falas, média {sum(palavras) / n:.0f} palavras | curtas {curtas / n:.0%} | "
        f"longas {longas / n:.0%} | com '!' {exclamacao / n:.0%} | nome do colega {vocativos / n:.0%} | "
        f"interrupções {cortes} | 'por falar em' {por_falar} | frases gastas {len(gastas_usadas)}"
    )
    problemas = []
    if curtas / n < 0.15:
        problemas.append("poucas falas curtas (<15%)")
    if longas / n > 0.25:
        problemas.append("falas longas demais (>25%)")
    if exclamacao / n > 0.5:
        problemas.append("exclamação demais (>50%)")
    if vocativos / n > 0.3:
        problemas.append("repete o nome do colega (>30%)")
    if por_falar > 1:
        problemas.append("'por falar em' repetido")
    if gastas_usadas:
        problemas.append("repetiu frase de episódio anterior: " + ", ".join(gastas_usadas[:5]))
    if problemas:
        print(f"[AVISO] diálogo ainda com cara de locutor: {'; '.join(problemas)}", file=sys.stderr)


def _pausa_ms(anterior: tuple[str, str], atual: tuple[str, str]) -> int:
    """Respiro entre falas (o silêncio embutido do edge-tts já foi cortado):
    reação curta ("sério?", "exato") emenda rápido, como numa conversa de
    verdade; troca de apresentador é um pouco maior que continuar falando.
    Somam-se ~230 ms de margem que aparar_silencio deixa em cada ponta."""
    if len(anterior[1]) < 40 or len(atual[1]) < 40:
        return 60
    return 220 if anterior[0] != atual[0] else 150


PAUSA_BLOCO_MS = 800  # troca de bloco (linha --- no roteiro)


async def dialogue_to_speech(segments: list[tuple[str, str]], out_path: str) -> None:
    """Gera cada fala com a voz do respectivo host, apara o silêncio embutido
    e costura tudo num MP3 só com pausas controladas. Concatenar os bytes
    funciona porque o edge-tts emite MPEG puro, sem headers."""
    # Voz, ritmo e tom de cada apresentador vêm do apresentadores.json
    # (só há uma voz masculina pt-BR; rate e pitch diferenciam os homens).
    styles = {
        rotulo: {"voice": a["voz"], "rate": a["rate"], "pitch": a["pitch"]}
        for rotulo, a in todos_apresentadores().items()
    }
    amostra = b""
    anterior: tuple[str, str] | None = None
    troca_de_bloco = False
    with open(out_path, "wb") as out:
        for seg in segments:
            speaker, text = seg
            if speaker == "PAUSA":
                troca_de_bloco = True
                continue
            s = styles[speaker]
            audio = await _sintetizar(texto_para_fala(text), s["voice"], s["rate"], s["pitch"])
            audio = aparar_silencio(audio)
            amostra = amostra or audio
            if anterior:
                pausa = PAUSA_BLOCO_MS if troca_de_bloco else _pausa_ms(anterior, seg)
                out.write(silencio_mp3(amostra, pausa))
            out.write(audio)
            anterior, troca_de_bloco = seg, False


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
                "title": caption.split("\n")[0][:60],
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


# ---------- Notas do episódio ----------

# Palavras que identificam o bloco escrito pelo Gemini na linha das notas
# ("Radar Cloud e DevOps", "DevOps", "Jogos"... tudo cai no lugar certo).
_ALIASES_BLOCO = {
    "Manchete": ("manchete",),
    "Cloud & DevOps": ("cloud", "devops", "nuvem", "aws", "infra"),
    "Games": ("game", "jogo"),
    "Mobile": ("mobile", "celular", "smartphone", "telecom"),
    "Política": ("polític", "politic", "congresso", "governo", "stf"),
    "Rodada rápida": ("rodada", "rápida", "rapida"),
}
BLOCOS_OBRIGATORIOS = ("Cloud & DevOps", "Games", "Mobile", "Política")

_BLOCOS_NOTAS = [
    ("Manchete", "📌"),
    ("Cloud & DevOps", "☁️"),
    ("Games", "🎮"),
    ("Mobile", "📱"),
    ("Política", "🏛️"),
    ("Rodada rápida", "⚡"),
]


def separar_notas(resposta: str) -> tuple[str, str]:
    """Divide a resposta do Gemini em (roteiro falado, bloco de notas)."""
    partes = re.split(r"(?mi)^[^\w\n]*=+\s*NOTAS\s*=+[^\w\n]*$", resposta, maxsplit=1)
    if len(partes) == 2:
        return partes[0].strip(), partes[1].strip()
    return resposta.strip(), ""


def montar_notas(notas: str, items: list[dict], today: str) -> tuple[str, str]:
    """Transforma as linhas `NÚMERO | BLOCO | título` em mensagem HTML do
    Telegram com links. Devolve (mensagem, título da manchete) — mensagem
    vazia se o Gemini não mandou notas aproveitáveis."""
    por_bloco: dict[str, list[str]] = {nome: [] for nome, _ in _BLOCOS_NOTAS}
    termo = ""
    manchete = ""
    for line in notas.splitlines():
        campos = [c.strip() for c in line.strip().strip("-•* ").split("|")]
        if len(campos) < 3:
            continue
        num, bloco, titulo = campos[0], campos[1], " | ".join(campos[2:])
        if num.upper() == "TERMO" or "termo" in bloco.lower():
            termo = titulo
            continue
        nome_bloco = next(
            (
                nome
                for nome, _ in _BLOCOS_NOTAS
                if any(alias in bloco.lower() for alias in _ALIASES_BLOCO[nome])
            ),
            "Rodada rápida",
        )
        n = re.sub(r"\D", "", num)
        item = items[int(n) - 1] if n and 0 < int(n) <= len(items) else None
        linha = html.escape(titulo)
        if item and item.get("link"):
            linha = f'<a href="{html.escape(item["link"], quote=True)}">{linha}</a>'
        if item:
            linha += f" <i>({html.escape(item['source'])})</i>"
        por_bloco[nome_bloco].append(f"• {linha}")
        if nome_bloco == "Manchete" and not manchete:
            manchete = titulo
    if not any(por_bloco.values()):
        return "", ""
    faltando = [nome for nome in BLOCOS_OBRIGATORIOS if not por_bloco[nome]]
    if faltando:
        print(
            f"[AVISO] notas do episódio sem o(s) bloco(s): {', '.join(faltando)} "
            "(o Gemini pulou ou não achou notícia da área)",
            file=sys.stderr,
        )
    partes = [f"🎙️ <b>Resumo Tech — {today}</b>"]
    for nome, emoji in _BLOCOS_NOTAS:
        if por_bloco[nome]:
            partes.append(f"{emoji} <b>{nome}</b>\n" + "\n".join(por_bloco[nome]))
    if termo:
        partes.append(f"📖 <b>Termo do dia:</b> {html.escape(termo)}")
    return "\n\n".join(partes), manchete


def send_telegram_html(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Quebra por linha (nunca no meio de uma tag) respeitando os 4096 chars.
    pedacos, atual = [], ""
    for linha in text.split("\n"):
        if atual and len(atual) + len(linha) + 1 > 4000:
            pedacos.append(atual)
            atual = ""
        atual = f"{atual}\n{linha}" if atual else linha
    pedacos.append(atual)
    for pedaco in pedacos:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": pedaco,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=60,
        )
        _telegram_ok(resp)


def gerar_audio(script: str, mp3: str, dupla: tuple[dict, dict]) -> None:
    segments = parse_dialogue(script) if PODCAST_STYLE == "duo" else []
    falas = sum(1 for spk, _ in segments if spk != "PAUSA")
    if falas >= 4:
        blocos = sum(1 for spk, _ in segments if spk == "PAUSA") + 1
        print(f"Bate-papo com {falas} falas ({dupla[0]['nome']} e {dupla[1]['nome']}) em {blocos} blocos")
        asyncio.run(dialogue_to_speech(segments, mp3))
    else:
        if PODCAST_STYLE == "duo":
            print("[AVISO] Roteiro não veio em formato de diálogo; usando voz única.", file=sys.stderr)
        asyncio.run(text_to_speech(script, mp3))
    print(f"Áudio gerado: {mp3} ({os.path.getsize(mp3) // 1024} KB)")


def _legenda(today: str, manchete: str) -> str:
    return f"🎙️ Resumo Tech — {today}" + (f"\n📌 {manchete}" if manchete else "")


def _caminho_mp3() -> str:
    return os.path.join(
        tempfile.gettempdir(), f"resumo_tech_{datetime.now(BRT).strftime('%Y%m%d')}.mp3"
    )


def refazer_audio_pendente(episodios: list[dict], ep: dict, today: str) -> None:
    """O texto do episódio de hoje já foi pro Telegram, mas a voz falhou:
    gera só o áudio do roteiro salvo, sem chamar o Gemini de novo."""
    print("Episódio de hoje já tem roteiro, mas ficou sem áudio; refazendo só o áudio.")
    mp3 = _caminho_mp3()
    gerar_audio(ep["roteiro"], mp3, dupla_do_episodio(ep))
    send_telegram_audio(mp3, _legenda(today, ep.get("manchete", "")))
    ep["audio_ok"] = True
    gravar_historico(HISTORY_FILE, episodios)
    print("Áudio pendente enviado. Fim.")


def main() -> None:
    today = datetime.now(BRT).strftime("%d/%m/%Y")
    print(f"=== Resumo tech {today} ===")

    check_telegram()
    print("Telegram OK (token e chat_id válidos)")

    episodios = load_history(HISTORY_FILE)
    hoje_iso = datetime.now(BRT).strftime("%Y-%m-%d")
    ep_hoje = next((ep for ep in reversed(episodios) if ep.get("data") == hoje_iso), None)
    if ep_hoje and ep_hoje.get("audio_ok") is False:
        refazer_audio_pendente(episodios, ep_hoje, today)
        return
    if ep_hoje and REPESCAGEM:
        print("Repescagem: o episódio de hoje já foi entregue. Nada a fazer.")
        return

    feeds = load_feeds(FEEDS_FILE)
    print(f"{len(feeds)} feeds configurados")

    items = collect_news(feeds)
    print(f"{len(items)} notícias coletadas")
    items = remove_repeated(items, episodios)
    if not items:
        send_telegram_text(f"Resumo tech {today}: nenhuma notícia nova encontrada nas últimas {HOURS_WINDOW}h.")
        print("Nenhuma notícia nova. Encerrando.")
        return

    items = trim_for_summary(items, MAX_ITEMS_TO_SUMMARIZE)
    anexar_materias_completas(items)
    dupla = escolher_dupla()
    print(f"Apresentadores de hoje: {dupla[0]['nome']} e {dupla[1]['nome']}")
    script, notas = separar_notas(roteirizar(items, episodios, dupla))
    script = normalizar_roteiro(script, dupla)
    print(f"Roteiro gerado: {len(script)} caracteres")
    if PODCAST_STYLE == "duo":
        avaliar_dialogo(parse_dialogue(script), dupla, expressoes_gastas(episodios))
    mensagem, manchete = montar_notas(notas, items, today)
    if not mensagem:
        print("[AVISO] Gemini não mandou as notas do episódio; envio o roteiro como texto.", file=sys.stderr)

    mp3 = _caminho_mp3()
    try:
        gerar_audio(script, mp3, dupla)
    except Exception as e:
        # O roteiro (a parte cara e instável) já está pronto: não joga fora.
        # Manda o texto agora e marca o áudio como pendente pra repescagem.
        print(f"[ERRO] áudio falhou: {e}", file=sys.stderr)
        aviso = f"⚠️ A voz do resumo de hoje falhou ({_texto_curto(e)})."
        aviso += (
            f" Mando o roteiro em texto; o áudio sai na nova tentativa (~{PROXIMA_TENTATIVA})."
            if PROXIMA_TENTATIVA
            else " Segue o roteiro em texto."
        )
        send_telegram_text(aviso)
        if mensagem:
            send_telegram_html(mensagem)
        send_telegram_text(script)
        save_history(
            HISTORY_FILE, episodios, items, script, manchete, audio_ok=False,
            apresentadores=(dupla[0]["nome"], dupla[1]["nome"]),
        )
        print("Texto enviado e áudio marcado como pendente no histórico.")
        sys.exit(1)  # job vermelho, sem o aviso genérico de falha do __main__

    send_telegram_audio(mp3, _legenda(today, manchete))
    if SEND_TEXT_TOO:
        if mensagem:
            send_telegram_html(mensagem)
        else:
            send_telegram_text(script)
    print("Enviado pro Telegram.")

    # Só grava depois de enviar: se algo falhar antes, amanhã essas notícias
    # continuam valendo.
    save_history(
        HISTORY_FILE, episodios, items, script, manchete, apresentadores=(dupla[0]["nome"], dupla[1]["nome"])
    )
    print(f"Histórico salvo em {HISTORY_FILE} ({len(episodios) + 1} episódio(s)). Fim.")


def _texto_curto(e: Exception) -> str:
    """Mensagem de erro legível no Telegram: sem URLs/tokens, até 300 chars."""
    texto = re.sub(r"https?://\S+|wss?://\S+", "", _resumo_erro_tts(e))
    texto = re.sub(r"\s+", " ", texto).strip(" ,:")
    return (texto[:300] + "…") if len(texto) > 300 else texto


if __name__ == "__main__":
    # Sem isso o stdout fica em buffer e os [AVISO] (stderr) aparecem no log
    # do Actions antes do "=== Resumo tech ===", fora de ordem.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        main()
    except Exception as e:
        # Avisa no Telegram antes de falhar o job, senão o erro passa despercebido.
        aviso = f"⚠️ O resumo tech de hoje falhou: {_texto_curto(e)}"
        if PROXIMA_TENTATIVA:
            aviso += f"\n🔁 Nova tentativa automática por volta das {PROXIMA_TENTATIVA}."
        try:
            send_telegram_text(aviso)
        except Exception:
            pass
        raise
