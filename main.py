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
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TTS_VOICE = os.environ.get("TTS_VOICE", "pt-BR-AntonioNeural")
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
    prompt = f"""Você é o roteirista de um podcast diário de notícias de tecnologia em português do Brasil.
Abaixo estão as notícias das últimas {HOURS_WINDOW} horas coletadas de vários sites.

Escreva um roteiro de podcast de 3 a 5 minutos:
- Comece com uma saudação curta ("Bom dia! Aqui está o seu resumo tech de hoje...").
- Agrupe notícias repetidas (vários sites cobrindo o mesmo assunto) em um item só.
- Priorize: lançamentos relevantes, IA, cloud/DevOps, programação, segurança.
- Ignore publieditorial, promoções e reviews de produto irrelevantes.
- Fale de forma natural, como um apresentador, sem markdown, sem asteriscos,
  sem emojis, sem listas — apenas texto corrido pronto para ser lido em voz alta.
- Encerre com uma despedida curta.

NOTÍCIAS:
{corpus}"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                headers={"x-goog-api-key": GEMINI_API_KEY},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (requests.RequestException, KeyError, IndexError) as e:
            last_error = e
            if attempt < 2:
                print(f"[AVISO] Gemini falhou ({e}); nova tentativa em 20s...", file=sys.stderr)
                time.sleep(20)
    raise RuntimeError(f"Gemini falhou após 3 tentativas: {last_error}")


async def text_to_speech(text: str, out_path: str) -> None:
    import edge_tts

    communicate = edge_tts.Communicate(text, TTS_VOICE, rate="+8%")
    await communicate.save(out_path)


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
    resp.raise_for_status()


def send_telegram_text(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram limita mensagens a 4096 chars
    for i in range(0, len(text), 4000):
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[i : i + 4000]},
            timeout=60,
        ).raise_for_status()


def main() -> None:
    today = datetime.now().strftime("%d/%m/%Y")
    print(f"=== Resumo tech {today} ===")

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
