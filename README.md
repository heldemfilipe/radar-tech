# 🎙️ Resumo Tech Diário — versão GitHub Actions (repo público)

Mesmo pipeline (RSS → Gemini → edge-tts → Telegram), mas rodando de graça
na nuvem do GitHub. Sem Docker, sem PC ligado, sem systemd.

## Estrutura do repo

```
seu-repo/
├── .github/workflows/news.yml   ← agendamento + execução
├── main.py
├── feeds.txt
├── pronuncia.txt                ← como a voz fala termos difíceis
├── historico.json               ← criado/atualizado pelo bot (não edite)
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
   | `GEMINI_API_KEY` |  chave do https://aistudio.google.com/apikey |
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
  pausa workflows agendados. Como todo episódio commita o `historico.json`,
  o repo nunca fica parado.
- **Dê `git pull` antes de mexer no repo.** O bot commita o histórico todo
  dia; sem o pull, o seu push vai ser recusado.
- **Fuso.** O cron é em UTC. `15 9 * * *` = 6h15 de Brasília. Se o horário
  de verão voltar um dia, ajuste manualmente.

## Estilo do podcast

Por padrão o áudio é um **bate-papo entre dois apresentadores** — ANA
(voz feminina, Francisca) e LEO (voz masculina, Antonio). Para voltar ao
narrador único, descomente `PODCAST_STYLE: solo` no `news.yml`. As vozes
podem ser trocadas pelas variáveis `VOICE_FEMALE` e `VOICE_MALE`
(qualquer voz do edge-tts, ex.: `pt-BR-ThalitaNeural`).

## Formato do episódio

O roteiro segue blocos fixos — abertura, **manchete do dia** (a fundo),
**radar Cloud & DevOps** (AWS, nuvens, GFT), **rodada rápida**, **termo do dia**
e encerramento — com uma pausa maior entre um bloco e outro.

- **Matéria completa:** as 5 notícias em destaque (`MAX_FULL_ARTICLES`) têm o
  texto inteiro baixado e enviado ao Gemini, pra explicação ter fatos e
  números de verdade, não só o resumo do RSS. A escolha é automática: assunto
  coberto por vários sites, cloud/DevOps/GFT e segurança/IA sobem; promoção cai.
- **Pausas naturais:** o silêncio longo que o edge-tts coloca no fim de cada
  fala é cortado e substituído por pausas curtas (reação rápida emenda quase
  colada; troca de apresentador ~0,4 s; troca de bloco ~1 s).
- **Pronúncia:** `pronuncia.txt` diz como a voz deve falar termos em inglês e
  siglas (ex.: `Kubernetes = kubernêtis`). Ouviu um erro? Adicione uma linha e
  dê push. Números romanos em nomes viram algarismos (`Diablo V` → `Diablo 5`).
- **Notas no Telegram:** em vez do roteiro inteiro, chega uma mensagem curta
  com as notícias do episódio por bloco, com link pra matéria, e o termo do dia.

## Sem notícias repetidas

Depois de enviar o episódio, o script grava em `historico.json` as notícias
usadas e o roteiro, e o workflow commita esse arquivo. No dia seguinte:

1. notícias com o mesmo link (ou título quase igual) das usadas nos últimos
   3 dias (`HISTORY_DAYS`) são descartadas antes de ir pro Gemini;
2. os roteiros desses episódios vão junto no prompt, com a ordem de não
   voltar a um assunto já comentado e de não reaproveitar bordões, piadas e
   termo do dia;
3. quando uma notícia de hoje continua algo já falado, os apresentadores
   fazem o gancho ("lembra que ontem a gente falou do...") e contam só a
   novidade.

Rodou manualmente duas vezes no mesmo dia? A segunda execução também conta
a primeira como "já falada". Pra zerar a memória, apague o `historico.json`.

## Editar feeds

É só editar `feeds.txt` e dar push — sem rebuild, o runner sempre usa a
versão atual do repo. Formatos aceitos estão explicados no topo do arquivo
(RSS, busca no Google Notícias e sitemap). Se um site bloquear o GitHub
(403/429), o script busca as notícias dele pelo Google Notícias sozinho.
