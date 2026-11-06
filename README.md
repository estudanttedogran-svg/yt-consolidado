# consolidado — MCP server de YouTube

Um servidor MCP (Python 3.12) que junta três servidores open-source de YouTube em um só
catálogo de 15 tools e acrescenta a camada que faltava: **descobrir canais emergentes por
filtros compostos** — idade do canal, pico de views, duração média, país e faixa de inscritos —
com cota medida e shortfall honesto.

Responde a esta pergunta, que nenhum dos três originais responde sozinho:

> Quais canais publicaram pela primeira vez há menos de 30 dias, já têm um vídeo acima de
> 100 mil views, falam com público dos EUA e têm duração média entre 8 e 30 minutos?

---

## Instalação em 3 comandos

```powershell
git clone <este-projeto> C:\caminho\consolidado
cd C:\caminho\consolidado
uv sync --python 3.12
```

Depois registre no cliente MCP (bloco pronto abaixo) e reinicie o app. Tempo real de setup em
máquina limpa: **~2 min** de download + 10 s de configuração.

## 1. Pré-requisitos

| item | versão | obrigatório | por quê |
|---|---|---|---|
| Python | 3.12+ | sim | `requires-python >=3.12` no `pyproject.toml` |
| [uv](https://docs.astral.sh/uv/) | 0.9+ | sim | resolve e tranca as dependências (`uv.lock`) |
| Chave da YouTube Data API v3 | — | sim, para as tools de API | 100 unidades por `search.list` |
| ffmpeg | — | **não** | os tools de frame capture do tube-bridge não foram portados |
| Node.js | — | **não** | o servidor é Python puro |

Conferir se ficou tudo no lugar:

```powershell
uv run python -c "import mcp, googleapiclient, yt_dlp, feedparser, youtube_transcript_api; print('ok')"
```

## 2. Variáveis de ambiente

Obrigatória para as tools de Data API:

```powershell
$env:YOUTUBE_API_KEYS = "AIza...key1,AIza...key2,AIza...key3"   # pool CSV, rotaciona em quotaExceeded
```

Aceita também key única:

```powershell
$env:YOUTUBE_API_KEY = "AIza...key1"
```

Opcionais: **nenhuma**. O servidor não lê cache em disco, proxy ou timeout por variável de
ambiente — só as duas acima (`auth.py:128-129`). Sem key, as tools de API devolvem
`{"success": false, "error": "No YouTube API key configured..."}` e as de cota zero
(`youtube_get_channel_videos`, `get_channel_rss_videos`, `youtube_get_transcript`) continuam
funcionando.

## 3. Claude Desktop

Edite `%APPDATA%\Claude\claude_desktop_config.json` (feche o app antes; ele sobrescreve o
arquivo ao sair):

```json
{
  "mcpServers": {
    "yt-consolidado": {
      "command": "C:\\caminho\\consolidado\\.venv\\Scripts\\python.exe",
      "args": ["-m", "consolidado.server"],
      "env": {
        "YOUTUBE_API_KEYS": "AIza...key1,AIza...key2"
      }
    }
  }
}
```

## 4. Cursor

Edite `%USERPROFILE%\.cursor\mcp.json`:

```json
{
  "mcpServers": {
    "yt-consolidado": {
      "command": "C:\\caminho\\consolidado\\.venv\\Scripts\\python.exe",
      "args": ["-m", "consolidado.server"],
      "env": {
        "YOUTUBE_API_KEYS": "AIza...key1,AIza...key2"
      }
    }
  }
}
```

Transporte HTTP (para clientes remotos):

```powershell
uv run python -m consolidado.server --transport streamable-http --host 127.0.0.1 --port 8000
uv run python -m consolidado.server --print-tools      # catálogo das 15 tools em JSON
```

## 5. Primeiro uso — prompt e resposta esperada

**Exemplo A — busca simples (custo 103):**

> "Busque 3 vídeos sobre 'personal finance' publicados nos últimos 30 dias, ordene por data e
> devolva título, canal, views e duração."

O agente chama `search_videos` (100) e depois `youtube_get_video_info` por id (1 cada).
Resposta real obtida (aqui resumida):

| título | canal | views | duração |
|---|---|---:|---|
| The rich never budget — New Personal Finance drops daily… | yodi montali | 2 | 1:09 |
| Forgot a paycheck? Your state may have it. | Deep Frame | 0 | 0:31 |
| I hate his smile — @Caleb Hammer | Seeking Finance clips | 122 | 1:14 |

**Exemplo B — o produto, execução original (custo 193; anterior à correção da base de idade):**

> "Quero 10 canais que estejam bombando agora, primeira publicação há menos de 30 dias, algum
> vídeo com mais de 100 mil views, público norte-americano, duração média entre 8 e 30 minutos."

```json
{
  "success": true,
  "query": "personal finance",
  "n_requested": 10,
  "n_returned": 0,
  "channels": [],
  "near_misses": [
    { "channel_title": "Ben Talks Finance", "max_views": 28126, "reason": "max_views below 100000" },
    { "channel_title": "Deep Frame", "max_views": 1615, "reason": "max_views below 100000" }
  ],
  "rejected_counts": { "not_new": 10, "low_views": 15, "duration": 0, "verify_error": 0 },
  "quota_total": 193,
  "quota_breakdown": { "discovery": 100, "enrichment": 1, "verification": 92 },
  "shortfalls": "Returned 0 of 10 requested. 36 unique channels found in discovery; 26 survived country/subscriber filters; 10 rejected: first publication older than 30 days; 15 rejected: no video above 100000 views; 0 rejected: average duration outside 8.0-30.0 min; 1 candidates skipped by max_candidates_checked=25. Filters were not relaxed automatically - widen them deliberately if you want more."
}
```

**Zero não é bug.** O `near_misses` mostra o teto real da população naquele nicho (28 mil views,
não 100 mil). O produto devolve o que os filtros permitem e diz exatamente quem caiu em qual
corte — não relaxa nada sozinho.

**Exemplo C — base de idade corrigida (execução ao vivo, custo 210):**

`max_channel_age_days` passou a medir a data do **primeiro vídeo publicado**, nunca a data de
criação da conta. Cada candidato tem a playlist de uploads enumerada (até `first_video_pages`
páginas) para provar quando começou; quem não prova é rejeitado como `age_unproven` em vez de
estimado pela conta.

```json
{
  "success": true,
  "query": "world war 2 documentary",
  "n_requested": 15,
  "n_returned": 0,
  "near_misses": [
    { "channel_title": "Why Though?", "first_video_published_at": "2026-10-14T23:00:24Z",
      "max_views": 240116, "avg_duration_minutes": 2.3, "reason": "avg duration outside 8.0-30.0 min" },
    { "channel_title": "WW2 HIDDEN CHAPTERS", "first_video_published_at": "2026-10-23T18:45:16Z",
      "max_views": 4012, "avg_duration_minutes": null, "reason": "max_views below 100000" }
  ],
  "rejected_counts": { "not_new": 6, "low_views": 14, "duration": 1, "verify_error": 0, "age_unproven": 4 },
  "quota_total": 210,
  "quota_breakdown": { "discovery": 100, "enrichment": 1, "verification": 109 },
  "shortfalls": "Returned 0 of 15 requested. 48 unique channels found in discovery; 28 survived country/subscriber filters; 6 rejected: first publication older than 30 days; 14 rejected: no video above 100000 views; 1 rejected: average duration outside 8.0-30.0 min; 4 rejected: first video not proven within 3 upload pages; 3 candidates skipped by max_candidates_checked=25. Filters were not relaxed automatically - widen them deliberately if you want more."
}
```

Artefato bruto: `prova-de-tela/raw/live-age-basis.json`.

## 6. Tools e custo de cota

Unidades oficiais da YouTube Data API v3 (`auth.py::ENDPOINT_COST`).

| tool | origem | camada | cota por chamada | o que a torna cara |
|---|---|---|---:|---|
| `search_videos` | youtube-mcp | API → yt-dlp | **100** | `search.list` é o endpoint mais caro da API |
| `youtube_search` | tube-bridge | API → yt-dlp | **100** | idem; sem key cai para yt-dlp (0) |
| `search_channels` | youtube-mcp | API | **101** | `search.list` (100) + `channels.list` (1) |
| `get_channel_details` | youtube-mcp | API | **1** | um único `channels.list` |
| `find_breakout_creators` | youtube-mcp | API | **101 + 1–3/canal** | `search.list` + `channels.list` em lote + enumeração do primeiro vídeo por candidato |
| `channels_getChannel` | youtube-mcp-server (TS) | API | **1** (ou **101**) | 101 quando resolve o último upload via `search.list`; `include_latest_upload=false` derruba para 1 |
| `get_channel_rss_videos` | youtube-mcp | RSS | **0** | Atom público, sem key |
| `youtube_get_video_info` | tube-bridge | API → yt-dlp | **1** | `videos.list`; sem key, yt-dlp (0) |
| `youtube_get_channel_videos` | tube-bridge | yt-dlp → RSS | **0** | scraping, sem key |
| `youtube_get_transcript` | tube-bridge | timedtext público | **0** | sem key (pode ser bloqueado por IP — ver limitações) |
| `get_first_published_date` | nova | API | **1 + 1/50** | `channels.list` + `playlistItems.list` paginado (1 por página de 50) |
| `max_views_in_channel` | nova | API | **1 + páginas + lotes** | `playlistItems.list` por página + `videos.list` em lote de 50 |
| `avg_duration` | nova | API | **2** | resolução do canal (1) + `videos.list` (1) |
| `find_emerging_channels` | nova (produto) | API | **100/página + 1/50 + 2–6/canal** | 100 fixos na descoberta; 193 medidos antes da correção de idade, 210 depois (25 candidatos, `first_video_pages=3`) |
| `get_quota_status` | nova | — | **0** | lê o `QuotaTracker` da sessão |

Regras práticas:

- Duas buscas (`search_videos` + `youtube_search`) custam 200 unidades. `search.list` é 100 fixo,
  mesmo com `max_results=1`.
- `dry_run=true` no orquestrador custa 100 e mostra os candidatos **antes** de gastar verificação.
- `get_quota_status()` devolve o extrato da sessão: unidades por tool, por endpoint e keys restantes.

## 7. `find_emerging_channels` — a tool do produto

```
find_emerging_channels(
    query,                    # nicho
    n=10,                     # canais desejados (não é garantia de retorno)
    max_channel_age_days=30,  # primeira publicação há menos de N dias
    min_video_views=100000,   # algum vídeo acima de X views
    region_code="US",
    relevance_language="en",
    min_avg_duration_min=8.0,
    max_avg_duration_min=30.0,
    subscriber_min=0,
    subscriber_max=100000,
    dry_run=False,            # True para na descoberta (100 unidades)
    discovery_pages=1,        # 100 unidades por página de 50 vídeos
    max_candidates_checked=25,
    sample_size=10,           # vídeos lidos por canal na verificação
    small_channel_video_cap=100,  # compatibilidade; a idade não usa mais este cap
    first_video_pages=3,      # páginas de uploads lidas por canal para provar o primeiro vídeo
)
```

Cinco etapas: descoberta (`search.list`), enriquecimento (`channels.list` em lote de 50),
verificação dura (primeiro vídeo publicado, pico de views, duração média), ranking e saída.

**Base da idade.** O corte `max_channel_age_days` mede a data do **primeiro vídeo publicado**,
nunca a data de criação da conta. Um canal aberto há anos que começou a postar semana passada
passa — desde que o primeiro vídeo seja provado dentro de `first_video_pages` páginas de uploads.
Sem prova, o canal sai como `age_unproven`; a conta não é usada como estimativa.

A mesma base vale para `find_breakout_creators`: `channel_age_months` vem do primeiro vídeo. É uma
quebra deliberada de paridade com o `find_breakout_growth_channels` do youtube-mcp, que envelhecia
pelo cadastro; a saída mantém `created_at` (conta) e acrescenta `first_video_published_at`.

Três garantias:

1. **Nada é relaxado automaticamente.** Se voltarem menos que `n`, o campo `shortfalls` explica
   quem caiu em qual filtro e o resultado sai como está.
2. **`near_misses` está sempre no output.** Os canais que passaram a idade e falharam em
   views/duração vêm com `max_views` e `avg_duration_minutes`, ordenados por pico — é o teto real
   da população antes de você afrouxar o corte.
3. **`dry_run=True` para na etapa 1.** 100 unidades para inspecionar os candidatos antes de
   gastar verificação.

## 8. Limitações conhecidas

| limitação | efeito | contorno |
|---|---|---|
| **Transcript bloqueado por IP** | `youtube_get_transcript` devolve erro in-band quando o YouTube bloqueia o IP (comum em cloud/hosting/VPN). Não há dado de transcrição nesse caso. | rodar de IP residencial; a paridade de dados dessa tool ficou **não provada** na FASE 6 por esse motivo |
| **Erro in-band, não exceção** | falha de upstream em `youtube_get_video_info`, `youtube_get_channel_videos`, `youtube_get_transcript` e `channels_getChannel` vem como `{"error": "..."}` com resultado vazio — o MCP não marca `isError` | cheque `error`/`success` no payload, não só o status do protocolo |
| **Shortfall é normal em nicho estreito** | `n_returned < n` quando a população não tem canais que passem em todos os filtros | leia `near_misses` e `rejected_counts`, afrouxe o filtro que você escolher |
| **Contadores ao vivo** | `view_count` muda entre duas chamadas | compare com o mesmo instante, não entre execuções |
| **Sem cache entre processos** | cada chamada paga cota de novo (o tube-bridge original tinha cache SQLite) | use `dry_run` e `get_quota_status` para controlar |
| **`search.list` é caro** | 100 unidades por chamada, `max_results` não muda o preço | prefira `search_videos` uma vez e enriqueça com `youtube_get_video_info` (1 cada) |
| **Chamadas serializadas** | o cliente da API roda **uma requisição por vez** (lock process-wide); em paralelo elas enfileiram em vez de estourar SSL | comportamento intencional — contra `SSLError`/timeout; um `search` que falha 3× segura a fila ~6 s |
| **Retry automático** | erro de rede (SSL, timeout, conexão) é re-tentado 3× com espera de 2 s e 4 s antes de virar erro | não há retry para erro de cota: cota esgotada rotaciona de key, não repete |
| **Timeout de socket 30 s** | definido em `auth.py` (`httplib2.Http(timeout=30)`); sem ele o cliente esperava o timeout do sistema operacional | acima disso a chamada já é lenta demais para um cliente MCP |
| **Verificação em lotes de 10** | `find_emerging_channels` para de verificar assim que bate `n`; `skipped_after_goal` diz quantos candidatos ficaram sem verificar | se você quer o censo completo, chame com `n` alto e `near_misses` traz o resto |

## 9. Testes

```powershell
uv run pytest tests\ -v
```

31 testes, nenhum toca a rede: parser ISO 8601, escolha de proxy, corte por data, média e
arredondamento, cota por chamada, staging do orquestrador, shortfall sem preenchimento
artificial, ranking e cap de candidatos. O `FakeClient` cobra as mesmas unidades oficiais.

## 10. Estrutura

```
src/consolidado/
  auth.py            pool de keys + QuotaTracker + ENDPOINT_COST
  youtube_client.py  wrapper da API v3, rotação em 403/quota
  ytdlp_client.py    camada 3 (0 cota)
  rss_client.py      camada 4 (0 cota)
  server.py          registro das 15 tools, stdio/sse/http
  tools/
    search.py        search_videos, youtube_search
    channels.py      search_channels, get_channel_details, find_breakout_creators,
                     channels_getChannel, get_channel_rss_videos
    videos.py        youtube_get_video_info, youtube_get_channel_videos,
                     youtube_get_transcript
    custom.py        get_first_published_date, max_views_in_channel, avg_duration
    orchestrator.py  find_emerging_channels
tests/
  conftest.py        FakeClient com cota real
  test_custom.py
  test_orchestrator.py
```

## 11. Origem e paridade

10 tools portadas de `youtube-mcp` (5), `tube-bridge` (4) e `youtube-mcp-server` (1), com
paridade semântica verificada caso a caso em `06-paridade.md` (30 pares, 0 divergência de dado
essencial, 8 desvios documentados). 5 tools novas: `get_first_published_date`,
`max_views_in_channel`, `avg_duration`, `find_emerging_channels`, `get_quota_status`.

Decisões de projeto, desvios de spec e evidências brutas: `../../05-reconstruir.md`,
`../../06-paridade.md` e `../../07-checklist-final.md`.