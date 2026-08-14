# 🎙️ Resumo Tech Diário — versão GitHub Actions (repo público)

Mesmo pipeline (RSS → Gemini → edge-tts → Telegram), mas rodando de graça
na nuvem do GitHub. Sem Docker, sem PC ligado, sem systemd.

## Estrutura do repo

```
seu-repo/
├── .github/workflows/news.yml   ← agendamento + execução
├── main.py
├── feeds.txt
└── requirements.txt
```

## Setup (10 min)

1. **Crie um repo público** no seu GitHub pessoal (ex: `resumo-tech`).

2. **Suba os 4 arquivos** deste projeto:

   ```bash
   git init
   git add .
   git commit -m "resumo tech diário"
   git branch -M main
   git remote add origin git@github.com:SEU_USER/resumo-tech.git
   git push -u origin main
   ```

3. **Cadastre os secrets** (as chaves NUNCA vão no código):
   Repo → **Settings → Secrets and variables → Actions → New repository secret**

   | Nome | Valor |
   |---|---|
   | `GEMINI_API_KEY` | chave do https://aistudio.google.com/apikey |
   | `TELEGRAM_BOT_TOKEN` | token do @BotFather |
   | `TELEGRAM_CHAT_ID` | seu chat_id (via `/getUpdates`) |

4. **Teste sem esperar o horário agendado:**
   Aba **Actions** → "Resumo Tech Diário" → **Run workflow**.
   Em ~1 min o áudio chega no Telegram.

5. Pronto. Todo dia ~6h30 (BRT) ele roda sozinho.

## O que você precisa saber (limitações reais)

- **Horário não é exato.** O cron do Actions entra numa fila — por isso o
  gatilho está às 6h15 (BRT), pra entrega real cair em torno das 6h30.
  Se pontualidade de minuto importa, a versão no seu PC (systemd) é melhor.
- **Repo público = código e feeds visíveis.** Não há nada sensível neles;
  as chaves ficam em Secrets (criptografados, nunca aparecem em log).
- **Inatividade desativa o cron.** Após 60 dias sem atividade o GitHub
  pausa workflows agendados. O step de *keepalive* no workflow resolve
  isso automaticamente (faz um commit vazio quando necessário).
- **Fuso.** O cron é em UTC. `15 9 * * *` = 6h15 de Brasília. Se o horário
  de verão voltar um dia, ajuste manualmente.

## Estilo do podcast

Por padrão o áudio é um **bate-papo entre dois apresentadores** — ANA
(voz feminina, Francisca) e LEO (voz masculina, Antonio). Para voltar ao
narrador único, descomente `PODCAST_STYLE: solo` no `news.yml`. As vozes
podem ser trocadas pelas variáveis `VOICE_FEMALE` e `VOICE_MALE`
(qualquer voz do edge-tts, ex.: `pt-BR-ThalitaNeural`).

## Editar feeds

É só editar `feeds.txt` e dar push — sem rebuild, o runner sempre usa a
versão atual do repo.
