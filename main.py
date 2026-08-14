#!/usr/bin/env python3
"""
News Podcast — coleta RSS, resume com Gemini, gera áudio com edge-tts
e envia pro Telegram. Roda 1x e encerra (pensado pra cron/systemd + docker run).
"""

import asyncio
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

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
MAX_ITEMS_PER_FEED = int(os.environ.get("MAX_ITEMS_PER_FEED", "8"))
HOURS_WINDOW = int(os.environ.get("HOURS_WINDOW", "24"))
FEEDS_FILE = os.environ.get("FEEDS_FILE", "feeds.txt")
SEND_TEXT_TOO = os.environ.get("SEND_TEXT_TOO", "true").lower() == "true"


def load_feeds(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


# Alguns sites bloqueiam o User-Agent padrão do Python; um UA de navegador resolve.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def collect_news(feeds: list[str]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_WINDOW)
    items = []
    for url in feeds:
        try:
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=30)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            source = parsed.feed.get("title", url)
            count = 0
            for entry in parsed.entries:
                if count >= MAX_ITEMS_PER_FEED:
                    break
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_dt = datetime.fromtimestamp(time.mktime(published), tz=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                summary = strip_html(entry.get("summary", ""))[:500]
                items.append(
                    {
                        "source": source,
                        "title": entry.get("title", "(sem título)"),
                        "summary": summary,
                        "link": entry.get("link", ""),
                    }
                )
                count += 1
            print(f"[OK] {source}: {count} notícias")
        except Exception as e:
            print(f"[ERRO] {url}: {e}", file=sys.stderr)
    return items


def summarize(items: list[dict]) -> str:
    corpus = "\n\n".join(
        f"FONTE: {i['source']}\nTÍTULO: {i['title']}\nRESUMO: {i['summary']}"
        for i in items
    )
    if PODCAST_STYLE == "duo":
        hoje = datetime.now()
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
        estilo = f"""Você é o roteirista de um podcast diário de notícias de tecnologia em
português do Brasil, apresentado por dois hosts com personalidades bem definidas:

ANA — a analítica da dupla. Voz charmosa e envolvente, fala com calma e confiança.
Quando surge um conceito técnico (LLM, kubernetes, zero-day, latência...), ela
explica em uma frase simples com uma analogia do dia a dia, sem soar professoral.
Gosta de dados e de apontar "o que ninguém está comentando" sobre a notícia.

LEO — o brincalhão. Energia alta, piadas leves e referências nerd, reage com
entusiasmo genuíno ("não acredito!", "olha isso!"), provoca a Ana de leve e sempre
puxa o lado prático: "tá, mas o que isso muda na vida de quem tá ouvindo?".

DINÂMICA (o que faz soar como conversa de verdade):
- LEO SEMPRE abre o episódio com um bordão de abertura criativo, estilo nerd,
  DIFERENTE a cada dia (crie um novo hoje, nunca repita), e ANA emenda com um
  comentário charmoso no estilo dela.
- Mencione o dia da semana na abertura de forma natural. {contexto_dia}
- Eles se chamam pelo nome, discordam de leve às vezes, um completa o raciocínio
  do outro, fazem gancho entre uma notícia e a próxima.
- Reações curtas no meio da conversa ("sério?", "exato", "aí complicou") pra
  quebrar blocos longos de fala.
- LEO encerra com um bordão de despedida (também novo a cada dia) e ANA fecha com
  uma última observação inteligente.

FORMATO OBRIGATÓRIO: cada fala em sua própria linha, começando com "ANA:" ou
"LEO:". Nenhuma linha fora desse formato — sem títulos, sem markdown, sem
asteriscos, sem emojis, sem rubricas como (risos) ou [vinheta].

CONTEÚDO (roteiro de 4 a 6 minutos):
- Agrupe notícias repetidas (vários sites cobrindo o mesmo assunto) em um item só.
- Priorize: lançamentos relevantes, IA, cloud/DevOps, programação, segurança.
- Ignore publieditorial, promoções e reviews de produto irrelevantes."""
    else:
        estilo = """Você é o roteirista de um podcast diário de notícias de tecnologia em português do Brasil.

Escreva um roteiro de podcast de 3 a 5 minutos:
- Comece com uma saudação curta ("Bom dia! Aqui está o seu resumo tech de hoje...").
- Agrupe notícias repetidas (vários sites cobrindo o mesmo assunto) em um item só.
- Priorize: lançamentos relevantes, IA, cloud/DevOps, programação, segurança.
- Ignore publieditorial, promoções e reviews de produto irrelevantes.
- Fale de forma natural, como um apresentador, sem markdown, sem asteriscos,
  sem emojis, sem listas — apenas texto corrido pronto para ser lido em voz alta.
- Encerre com uma despedida curta."""

    prompt = f"""{estilo}

Abaixo estão as notícias das últimas {HOURS_WINDOW} horas coletadas de vários sites.

NOTÍCIAS:
{corpus}"""

    model = GEMINI_MODEL
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = _gemini_generate(model, prompt)
            if resp.status_code == 404 and model == GEMINI_MODEL:
                # Modelo aposentado/renomeado pelo Google — descobre um
                # substituto entre os modelos que a chave tem acesso.
                model = discover_model()
                print(
                    f"[AVISO] Modelo {GEMINI_MODEL} indisponível; usando {model}.",
                    file=sys.stderr,
                )
                resp = _gemini_generate(model, prompt)
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (requests.RequestException, KeyError, IndexError) as e:
            last_error = e
            if attempt < 2:
                print(f"[AVISO] Gemini falhou ({e}); nova tentativa em 20s...", file=sys.stderr)
                time.sleep(20)
    raise RuntimeError(f"Gemini falhou após 3 tentativas: {last_error}")


def _gemini_generate(model: str, prompt: str) -> requests.Response:
    return requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=120,
    )


def discover_model() -> str:
    """Lista os modelos disponíveis pra esta chave e escolhe o melhor 'flash'."""
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
    ruins = ("lite", "live", "tts", "image", "preview", "exp", "thinking")
    flash_estavel = [n for n in names if "flash" in n and not any(r in n for r in ruins)]
    candidatos = flash_estavel or [n for n in names if "flash" in n] or names
    if not candidatos:
        raise RuntimeError("Nenhum modelo com generateContent disponível para esta chave")
    # Os nomes carregam a versão (gemini-3.7-flash > gemini-2.5-flash),
    # então o "maior" em ordem alfabética tende a ser o mais novo.
    return sorted(candidatos)[-1]


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

    # Ana: um pouco mais grave e pausada (charme, autoridade tranquila).
    # Leo: mais acelerado (energia, empolgação).
    styles = {
        "ANA": {"voice": VOICE_FEMALE, "rate": "+2%", "pitch": "-10Hz"},
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
    today = datetime.now().strftime("%d/%m/%Y")
    print(f"=== Resumo tech {today} ===")

    check_telegram()
    print("Telegram OK (token e chat_id válidos)")

    feeds = load_feeds(FEEDS_FILE)
    print(f"{len(feeds)} feeds configurados")

    items = collect_news(feeds)
    if not items:
        send_telegram_text(f"Resumo tech {today}: nenhuma notícia nova encontrada nas últimas {HOURS_WINDOW}h.")
        print("Nenhuma notícia. Encerrando.")
        return
    print(f"{len(items)} notícias coletadas")

    script = summarize(items)
    print(f"Roteiro gerado: {len(script)} caracteres")

    mp3 = os.path.join(
        tempfile.gettempdir(), f"resumo_tech_{datetime.now().strftime('%Y%m%d')}.mp3"
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
    print("Enviado pro Telegram. Fim.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Avisa no Telegram antes de falhar o job, senão o erro passa despercebido.
        try:
            send_telegram_text(f"⚠️ O resumo tech de hoje falhou: {e}")
        except Exception:
            pass
        raise
