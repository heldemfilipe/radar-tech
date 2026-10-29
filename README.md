# 🎙️ Resumo Tech Diário — versão GitHub Actions (repo público)

Mesmo pipeline (RSS → Gemini → edge-tts → Telegram), mas rodando de graça
na nuvem do GitHub. Sem Docker, sem PC ligado, sem systemd.

## Estrutura do repo

```
seu-repo/
├── .github/workflows/news.yml   ← agendamento + execução
├── main.py
├── feeds.txt
├── apresentadores.json          ← elenco, vozes e rodízio de apresentadores
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
- **Repescagem automática.** Se o episódio das 6h15 falhar (Gemini fora do
  ar, voz da Microsoft recusando), o workflow tenta de novo às 9h15 e às
  12h15. Se só a voz falhou, o roteiro já vai em texto na hora e a
  repescagem manda só o áudio, sem gerar outro roteiro. Nos dias em que tudo
  dá certo, essas execuções terminam em segundos sem fazer nada.
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

Por padrão o áudio é um **bate-papo entre dois apresentadores**, escolhidos
em **rodízio a cada 2 dias** a partir do `apresentadores.json`. A cada 3
trocas a dupla é **de duas mulheres** (alternando quem conduz); nas outras, uma
mulher e um homem. Quem explica os conceitos é sempre uma das mulheres. Hoje o
elenco é:

| | Voz | Personalidade |
|---|---|---|
| **Ana** | Francisca | analítica, intensa e sensual, opinião forte |
| **Clara** | Thalita | sedutora bem-humorada, irônica, charmosa |
| **Leo** | Antonio | brincalhão, energia alta |
| **Caio** | Antonio (mais grave e calmo) | cético tranquilo, humor seco |
| **Teo** | Antonio (mais agudo e rápido) | entusiasta atrapalhado |

As duplas variam (Ana+Clara, Ana+Caio, Clara+Teo, Clara+Ana, Ana+Leo,
Clara+Caio...) em vez de repetir sempre o mesmo par. As apresentadoras falam de
um jeito sensual e envolvente em qualquer dupla: no tom, nunca no conteúdo
(nada explícito). A conta depende só da data: a repescagem do mesmo dia cai na mesma
dupla, e o `historico.json` guarda quem apresentou cada episódio.

- **Trocar ou adicionar alguém:** edite o `apresentadores.json` (nome sem
  acento, `voz`, `rate`, `pitch`, `persona`). `rodizio_dias` muda a frequência
  da troca e `dupla_feminina_a_cada` a da dupla feminina (0 desliga). Na
  persona, descreva o jeito da pessoa **sem frases prontas entre aspas**: o
  Gemini copia exemplo literal todo dia e vira bordão. Ana e Leo devem continuar lá: os episódios antigos foram deles.
- **Forçar uma dupla** (teste): variável `DUPLA`, ex.: `DUPLA=Clara,Caio` ou
  `DUPLA=Ana,Clara` (precisa ter pelo menos uma mulher).
- **Só existe uma voz masculina pt-BR** no edge-tts (Antonio); os homens se
  diferenciam por ritmo e tom. Dá pra usar uma voz multilingual (ex.:
  `en-US-AndrewMultilingualNeural`) no campo `voz`, mas o sotaque muda — ouça
  antes de pôr no ar.
- Para voltar ao narrador único, descomente `PODCAST_STYLE: solo` no `news.yml`.

### Diálogo mais humano

O prompt tem uma seção "SOAR HUMANO" (falas de tamanhos variados com reações
curtas, interrupções, hesitação, pouca exclamação, pouco "Ana, ..." / "Leo,
...", transições variadas, sem formato de entrevista) e não tem mais bordão de
abertura, despedida, pergunta ou termo do dia: cada fala nasce da conversa.
Além disso, o script procura nos episódios anteriores os **começos de frase
que se repetiram** ("e aí, Ana, pronta pra ver", "tá, mas o que isso muda",
"dá pra explicar isso"...) e manda essa lista no prompt como proibida. A cada episódio o log do
Actions mostra uma linha `[DIÁLOGO]` com as métricas e um `[AVISO]` se o
roteiro voltou a soar como locutor ou repetiu frase gasta. Base medida nos episódios de 11 e 13/10,
antes da mudança: 0% de falas curtas, ~45 palavras por fala, ~75% das falas
com "!" e o nome do colega em ~35% delas.

## Formato do episódio

O roteiro segue blocos fixos — abertura, **manchete do dia** (a fundo),
**radar Cloud & DevOps** (pelo menos 1 notícia de AWS, nuvens ou DevOps), **radar Games** (pelo menos 1
notícia da indústria de games), **radar Mobile** (pelo menos 1 da indústria
mobile/telecom), **radar Política** (de 1 a 3 notícias de política brasileira,
com cobertura imparcial: fatos, posições atribuídas a quem as disse, sem
apresentadores tomando partido), **rodada rápida**, **termo do dia**
e encerramento — com uma pausa maior entre um bloco e outro.

- **Matéria completa:** as 5 notícias em destaque (`MAX_FULL_ARTICLES`) têm o
  texto inteiro baixado e enviado ao Gemini, pra explicação ter fatos e
  números de verdade, não só o resumo do RSS. A escolha é automática: assunto
  coberto por vários sites, cloud/DevOps e segurança/IA sobem; promoção cai;
  a melhor notícia de cloud, a de games, a de mobile e a de política têm vaga garantida (se a
  manchete já é de uma dessas áreas, a vaga vai pra segunda melhor dela).
  Se as notas do episódio vierem sem algum desses blocos, o log do Actions
  mostra um `[AVISO]`.
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
(RSS, busca no Google Notícias e sitemap). Um feed de política leva o 4º
campo `politica` (`URL | Nome | 24h | politica`) e alimenta o Radar Política.
Se um site bloquear o GitHub
(403/429), o script busca as notícias dele pelo Google Notícias sozinho.
