# polybot-weather

Bot de **análise e recomendação** para mercados climáticos na Polymarket (temperatura máxima/mínima em estações específicas, neve, furacões). Ele **não opera automaticamente por padrão** — ele lê o mercado, constrói uma distribuição de probabilidade a partir de um ensemble meteorológico, compara com o livro de ofertas (CLOB) e mostra quais apostas têm valor esperado positivo.

---

## Sumário

1. [O que o bot faz, em uma frase](#o-que-o-bot-faz-em-uma-frase)
2. [Como o modelo funciona (para leigos)](#como-o-modelo-funciona-para-leigos)
3. [Arquitetura e fluxo de dados](#arquitetura-e-fluxo-de-dados)
4. [Comandos disponíveis (CLI)](#comandos-disponíveis-cli)
5. [Pipeline de análise passo a passo](#pipeline-de-análise-passo-a-passo)
6. [Como a recomendação é decidida](#como-a-recomendação-é-decidida)
7. [Dimensionamento da aposta (Kelly)](#dimensionamento-da-aposta-kelly)
8. [Banco de dados](#banco-de-dados)
9. [Loop de aprendizado (calibração e backtest)](#loop-de-aprendizado-calibração-e-backtest)
10. [Mapa de arquivos do código](#mapa-de-arquivos-do-código)
11. [Configuração (`.env`)](#configuração-env)
12. [Segurança e avisos importantes](#segurança-e-avisos-importantes)

---

## O que o bot faz, em uma frase

> Dado um mercado tipo "Qual a temperatura máxima em Seul em 20 de abril?", o bot pega **119 cenários meteorológicos diferentes**, converte cada cenário em uma probabilidade para cada faixa (bin) do mercado, compara com o preço de venda no livro da Polymarket, e avisa se você está comprando barato.

---

## Como o modelo funciona (para leigos)

Imagine que você quer apostar em "A temperatura máxima em Seul amanhã vai ficar entre 14°C e 15°C?".

Ninguém sabe o futuro, mas centros meteorológicos do mundo todo (GFS nos EUA, ECMWF na Europa, ICON na Alemanha) rodam seus modelos **muitas vezes com pequenas perturbações** nas condições iniciais. Isso gera um **ensemble** — dezenas de versões do "dia de amanhã".

O bot faz assim:

1. **Pega o ensemble** — Open-Meteo entrega 30 a 119 "membros" (cenários), cada um com a temperatura hora-a-hora prevista.
2. **Para cada membro, calcula o pico do dia** — 119 valores como "15.2°C, 13.8°C, 14.1°C, 14.9°C...".
3. **Corrige viés histórico** — se o modelo costuma errar +1°C em Seul em abril, subtrai 1°C de cada membro (o bot aprende isso com o tempo).
4. **Conta quantos caem em cada faixa do mercado** — se 18 dos 119 membros caem em "14-15°C", a probabilidade modelada é ~15%.
5. **Mistura com a climatologia** (10% de peso) — histórico de 30 anos do mesmo dia do ano, como uma âncora de sanidade.
6. **Compara com o preço de venda** — se o mercado vende "14-15°C" a 36¢ e o modelo diz 15%, então comprar YES é ruim (você paga mais do que vale). Comprar NO a 66¢ é bom (vale ~85¢).
7. **Filtra por liquidez e prazo** — só recomenda se dá pra executar (livro fundo o bastante) e se o mercado resolve logo (≤72h por padrão).
8. **Dimensiona pela fórmula de Kelly fracionária** — quanto maior a vantagem esperada, maior a aposta, mas nunca mais que 5% da banca.

É exatamente como um pôquer quantitativo: você não sabe se a carta vai cair, mas sabe quantos caminhos o ensemble "vê" indo para cada resultado, e só aposta quando a cotação oferece pagar menos do que o justo.

---

## Arquitetura e fluxo de dados

```
         Polymarket                         Open-Meteo                      ERA5 (arquivo)
         Gamma / CLOB                       Ensemble + Forecast             reanalysis histórica
              │                                    │                                 │
              ▼                                    ▼                                 ▼
   ┌──────────────────┐            ┌─────────────────────────┐       ┌──────────────────────┐
   │  descoberta de   │            │ 30-119 membros, bias     │       │ extremos realizados  │
   │  mercados        │            │ correction, climatologia │       │ (para calibrar)      │
   └────────┬─────────┘            └────────────┬────────────┘       └──────────┬───────────┘
            │                                    │                              │
            └────────────┬───────────────────────┘                              │
                         ▼                                                      │
              ┌──────────────────────┐                                          │
              │  probabilidade por   │                                          │
              │  faixa (bin)         │                                          │
              └──────────┬───────────┘                                          │
                         ▼                                                      │
              ┌──────────────────────┐                                          │
              │  edge = p - ask·1.05 │ ← fee de 5% embutido                     │
              │  EV/$                │                                          │
              │  liquidity gate      │                                          │
              └──────────┬───────────┘                                          │
                         ▼                                                      │
              ┌──────────────────────┐          ┌─────────────────────────┐     │
              │  Kelly sizing        │          │     SQLite              │     │
              │  (25% do Kelly,      │─────────▶│  market / forecast /    │◀────┘
              │   cap 5% banca)      │          │  recommendation / etc.  │
              └──────────┬───────────┘          └──────────┬──────────────┘
                         ▼                                  │
              ┌──────────────────────┐                      │
              │  dashboard / TUI /   │                      │
              │  JSON / CLI          │                      │
              └──────────────────────┘                      │
                                                            ▼
                                                 ┌─────────────────────┐
                                                 │ calibrate (bias)    │
                                                 │ backtest (ROI, hit) │
                                                 └─────────────────────┘
```

---

## Comandos disponíveis (CLI)

Todos são executados como `polybot <comando>` depois que o pacote está instalado no `venv`.

| Comando | O que faz |
|---|---|
| `polybot scan` | Lista todos os mercados climáticos ativos hoje na Polymarket. Não consulta modelo nem livro — só descobre o universo. |
| `polybot analyze <slug>` | Análise completa de **um** mercado: probabilidades modeladas, preços atuais, edge, EV/$, recomendação, tamanho Kelly. Aceita `--json` e `--save/--no-save`. |
| `polybot recommend [--min-edge 0.05]` | Faz `scan` + `analyze` em lote e mostra **apenas** os mercados com edge positivo depois de todos os filtros. É o comando principal do dia-a-dia. |
| `polybot dash` | Abre a TUI interativa (Rich/live) — leaderboard, distribuição, livro, probabilidades. Roda em loop, atualizando mercado a mercado. |
| `polybot resolve [--lookback-days 21]` | Para mercados cuja resolução já passou, puxa o valor realizado do arquivo ERA5 e grava em `outcome`. ERA5 tem lag de ~5 dias, então rode periodicamente. |
| `polybot calibrate` | Olha todos os `(forecast, outcome)` pareados, calcula o erro médio assinado por `(estação × modelo × mês)` e atualiza a `bias_entry`. Exige ≥5 amostras por bucket. |
| `polybot backtest [--from YYYY-MM-DD]` | Reproduz cada recomendação passada contra o realizado: Brier, log-loss, hit rate, ROI simulado, calibração em decis. Deduplica múltiplos scans do mesmo mercado. |
| `polybot reset-training --scope bias\|data\|all --confirm` | Limpa tabelas de treino (nunca `market`). `bias` zera só `bias_entry`; `data` também `forecast`+`recommendation`+`outcome`; `all` faz tudo. |
| `polybot trade <slug> <outcome> --size 10 --confirm` | Envia ordem real ao CLOB. **Desligado por padrão**: exige `POLYBOT_EXECUTION_ENABLED=true` no `.env` **e** `--confirm` na chamada. |

---

## Pipeline de análise passo a passo

O que acontece dentro de `analyze_market()` (`src/polybot_weather/analysis.py`):

### 1. Parsear o mercado
`polymarket/parsers.py::parse_market` lê o texto da pergunta, descrição e `resolutionSource` para extrair:
- **Métrica**: `max_temp`, `min_temp`, `snowfall`, etc.
- **Estação de resolução**: código ICAO (KLGA, EGLL, RKSI, SBGR…). Se a pergunta não nomeia a estação, cai para um default por cidade.
- **Data de resolução**: e.g. "April 20" + ano vindo do `end_date`.
- **Faixas (bins)**: converte "≤14°C", "15°C", "16°C", "≥17°C" em objetos `TempBin(low, high, unit)`.
- **Unidade**: voto majoritário das unidades parseadas nos outcomes (F ou C), **sobrepondo** dicas soltas no texto da descrição (boilerplate como "toggle between Fahrenheit and Celsius" não engana).

### 2. Resolver a estação
`weather/stations.py` tem um dicionário `{código ICAO → (lat, lon, timezone, unidade_default)}` com ~80 estações. Polymarket resolve contra a estação **específica**, não o centro da cidade — KLGA e KNYC podem divergir 3-5°F no mesmo dia. Se o código não está no dicionário, a análise **aborta** (é melhor pular a pular um mercado do que apostar na coordenada errada).

### 3. Puxar o ensemble
`weather/openmeteo.py::ensemble_for_date` faz uma chamada para `ensemble-api.open-meteo.com` combinando **GFS + ECMWF IFS + ICON** (30-51 membros cada, ~119 totais). Para cada membro, pega a série horária de temperatura e reduz a **máx ou mín do dia local da estação**. Importante: a "janela do dia" respeita o timezone da estação — um mercado em Tóquio fecha ~15:00 UTC, não meia-noite UTC.

### 4. Aplicar correção de viés
`probability/calibration.py::BiasTable.correction_f(estação, modelo, mês)` retorna o erro médio histórico daquele bucket, se houver ≥5 amostras. A correção é **somada** a cada membro individualmente. Exemplo: se historicamente em abril em KLGA o modelo errou +2°F para baixo, cada membro ganha +2°F antes de ser binned.

### 5. Arredondar para resolução
Polymarket resolve em **inteiro**. Um membro prevendo 64.4 → 64, 64.6 → 65 (round-half-up, como NWS). Isso é feito ANTES do binning (`probability/bins.py::round_to_resolution`), senão massa probabilística vaza para faixas adjacentes.

### 6. Atribuir às faixas (binning + Laplace)
Cada membro arredondado cai na primeira bin que o contém. Adiciona-se **α = 0.5 membro virtual por bin** (suavização de Laplace) — isso evita que uma cauda receba probabilidade exatamente zero, o que faria o Kelly explodir depois.

Fórmula: `P(bin_i) = (contagem_i + 0.5) / (N + 0.5 × número_de_bins)`

### 7. Misturar com climatologia (10%)
`probability/climatology.py` puxa os últimos 30 anos do mesmo dia via ERA5, bina da mesma forma, e faz uma **mistura convexa**: `P_final = 0.9 × P_ensemble + 0.1 × P_climatologia`. Isso ancora caudas extremas — se o ensemble está muito confiante em algo historicamente improvável, a climatologia puxa um pouco de volta.

### 8. Puxar o livro CLOB para cada outcome
`polymarket/clob.py::book(token_id)` retorna os níveis (bid/ask com tamanho) para cada outcome, em paralelo via `asyncio.gather`. Pega `best_ask`, `best_ask.size` e `mid`.

### 9. Calcular edge e EV/$
Para cada outcome (ver `edge/value.py`):
```
effective_ask = ask × (1 + fee_rate)           # fee de 5% embutido
edge          = p_model - effective_ask         # pontos de probabilidade
EV/$          = p_model / effective_ask − 1     # retorno esperado por dólar
liquidity_usd = ask_size × ask                  # dólares efetivamente disponíveis no topo do livro
```

### 10. Dimensionar (Kelly) e persistir
Se todos os gates passarem, calcula o tamanho Kelly (seção abaixo) e grava tudo no SQLite.

---

## Como a recomendação é decidida

Um outcome vira `recommend=True` se **todos** os filtros abaixo passam (`edge/value.py::evaluate`):

| Filtro | Default | Motivo |
|---|---|---|
| `edge > min_edge` | `0.05` (5pp) | Evita falso sinal por ruído do ensemble / spread fino. |
| `EV/$ > min_ev` | `0.10` (10%) | Edge absoluto de 5pp num ask de 90¢ rende só 5.5% → ruim. |
| `liquidity_usd ≥ min_liquidity_usd` | `$50` | Abaixo disso qualquer ordem de $20+ já move o preço. |
| `hours_to_resolution ≤ max_hours_to_resolution` | `72h` | Ensemble meteorológico perde skill rápido acima de 3-5 dias. |

Se algum falha, `rejection_reason` é preenchido e o row ainda é salvo (para aparecer no dashboard e no backtest depois), mas **não é recomendado**.

---

## Dimensionamento da aposta (Kelly)

Dado `p` (probabilidade modelada) e `ask` (preço de compra), o Kelly **cheio** para um contrato que paga $1 no win é:

```
b       = (1 − ask) / ask                # net odds
f_full  = (p·b − (1−p)) / b              # fração cheia de Kelly
f_used  = min(kelly_fraction × f_full, max_bet_fraction)
size    = f_used × bankroll_usd
```

Com defaults `kelly_fraction=0.25` e `max_bet_fraction=0.05`:

- **Kelly fracionário a 25%** — reduz drawdown. Kelly cheio otimiza crescimento log mas tem ruína frequente sob incerteza no `p`.
- **Cap de 5% da banca por aposta** — livros finos + ensemble errando sistematicamente destrói Kelly cheio. O cap é a proteção contra "achar que sabe".

Exemplo: banca $1000, ask 0.30, p=0.60 → `b=2.33`, `f_full=0.43` → `f_used = min(0.25×0.43, 0.05) = 0.05` → **aposta $50**.

---

## Banco de dados

SQLite local (`polybot.db`). 5 tabelas, todas com ORM em `src/polybot_weather/storage/models.py`:

### `market` — um registro por mercado único
| Coluna | Tipo | O que é |
|---|---|---|
| `id` | INT PK | auto-increment interno |
| `polymarket_id` | STR UNIQUE | ID numérico do Gamma |
| `slug` | STR | slug legível (ex. `highest-temperature-in-seoul-…`) |
| `question` | STR(1024) | texto completo da pergunta |
| `metric` | STR(32) | `max_temp`, `min_temp`, `snowfall`, … |
| `station_code` | STR(8) | ICAO oficial |
| `unit` | STR(2) | `"F"` ou `"C"` — crítico para calibração |
| `resolution_date` | DATETIME | fim do dia local da estação em UTC naive |
| `created_at` | DATETIME | quando foi descoberto |

### `forecast` — um snapshot por scan
Cada vez que `analyze_market` roda para um mercado, grava uma linha aqui. Pode haver **centenas** de forecast por mercado (dashboard rescaneia).

| Coluna | O que é |
|---|---|
| `market_id` | FK → market |
| `run_at` | timestamp da scan |
| `member_count` | quantos membros do ensemble (tipicamente 30-119) |
| `bias_correction_f` | correção aplicada aos membros |
| `used_climatology` | se misturou climatologia |
| `spread_f` | `max(membros) − min(membros)` — mede incerteza |
| `sources_failed` | lista separada por vírgula (se GFS falhou mas ECMWF ok, etc.) |
| `forecast_mean_f` | média dos membros corrigidos — usado para calibrar |

### `recommendation` — uma linha por outcome por forecast
| Coluna | O que é |
|---|---|
| `forecast_id` | FK → forecast |
| `outcome_label` | rótulo canônico (`"14-15°C"`, `"NOT 15°C"`, `"≤61°F"`) |
| `p_model` | probabilidade calculada |
| `ask`, `mid` | melhor ask e ponto médio do livro |
| `edge`, `ev_per_dollar` | métricas derivadas |
| `liquidity_usd` | `ask_size × ask` no topo |
| `kelly_size_usd` | tamanho sugerido (0 se rejeitado) |
| `recommend` | flag booleana |
| `rejection_reason` | string curta explicando por que não (quando `recommend=False`) |

### `outcome` — um registro por mercado resolvido
`polybot resolve` preenche aqui após a data passar.
| Coluna | O que é |
|---|---|
| `market_id` | FK (UNIQUE) |
| `winning_outcome_label` | rótulo formal (`"max_temp=14.82C"`) |
| `realized_value` | valor numérico observado (na unidade do mercado) |
| `resolved_at` | timestamp |

### `bias_entry` — tabela de viés histórica
`polybot calibrate` reescreve esta tabela a partir das `(forecast, outcome)` casadas.
| Coluna | O que é |
|---|---|
| `station`, `model`, `month` | chave composta |
| `mean_error_f` | média assinada (`realized − forecast_mean`). Positivo = modelo sub-prevê. |
| `sample_count` | n (≥5 para ser usada em inferência) |
| `updated_at` | timestamp |

**Deduplicação importante**: ao calibrar e ao fazer backtest, múltiplas linhas do mesmo mercado (rescans) são colapsadas pela mais recente (`run_at` máximo). Sem isso, um mercado muito "escaneado" dominaria o bucket de viés.

---

## Loop de aprendizado (calibração e backtest)

O bot não é estático — ele aprende com cada mercado resolvido:

```
 dia D     polybot recommend    →  grava forecast + recommendation
 dia D+1   mercado resolve       →  polybot resolve grava outcome (ERA5 lag ~5 dias)
 semana    polybot calibrate     →  bias_entry é atualizada
 semana    polybot backtest      →  Brier, log-loss, ROI, calibração
 próximo   polybot recommend     →  agora usa bias corrigido
```

**Calibração** (`training/calibrator.py`):
- Emparelha cada forecast com o outcome do mesmo mercado.
- Deduplica (mantém só o forecast mais recente por mercado).
- Agrupa por `(estação, modelo, mês)` e calcula `mean(realized − forecast_mean_f)`.
- Escreve em `bias_entry` se `n ≥ 5`.

**Backtest** (`training/backtester.py`):
- Deduplica recomendações pelo (`market_id`, `outcome_label`) mantendo a última.
- Reparseia o `outcome_label` para saber se aquela aposta ganhou dado o `realized_value` (com conversão F↔C se preciso).
- Métricas:
  - **Brier** = média `(p − y)²`
  - **Log-loss** com clamp `ε=1e-6`
  - **Hit rate**
  - **ROI simulado** com fee embutido no `effective_ask` (compra `kelly_size_usd / effective_ask` contratos; payout = $1/contrato se win; lucro = payout − stake)
  - **Calibração em decis** (predito vs empírico por faixa de probabilidade)

---

## Mapa de arquivos do código

```
src/polybot_weather/
├── cli.py                     # entrypoint Typer — todos os comandos
├── config.py                  # Settings via pydantic-settings (.env)
├── analysis.py                # orquestra o pipeline: market → forecast → edge
│
├── polymarket/
│   ├── gamma.py               # descoberta de mercados (filtro regex weather+negativos)
│   ├── clob.py                # livro, bid/ask, midpoint (read-only)
│   ├── parsers.py             # extrai métrica, estação, bins, unidade, data
│   └── rate_limiter.py        # rate limit por categoria de endpoint
│
├── weather/
│   ├── openmeteo.py           # cliente único — forecast + ensemble + archive + climatologia
│   ├── stations.py            # ICAO → (lat, lon, tz, unit). ~80 estações globais
│   ├── cache.py               # JSON cache em disco com TTL
│   ├── nws.py                 # cliente NWS (legado, não usado no pipeline atual)
│   └── nhc.py                 # furacões (ainda não plugado)
│
├── probability/
│   ├── bins.py                # rounding half-up, binning, Laplace, mix climatologia
│   ├── ensemble.py            # combine() — junta membros + bias + climatologia
│   ├── calibration.py         # BiasTable + BiasEntry + apply_bias
│   └── climatology.py         # climatology_distribution (pseudo-ensemble histórico)
│
├── edge/
│   ├── value.py               # EdgeInputs, EdgeThresholds, evaluate() — gates
│   └── kelly.py               # Kelly fracionário + cap
│
├── execution/
│   ├── trader.py              # place_order() via py-clob-client (desligado por default)
│   └── wallet.py              # carrega chave privada do .env
│
├── reporting/
│   ├── dashboard.py           # render_market_analysis em Rich table/JSON
│   └── tui.py                 # TUI live com Rich Layout (leaderboard + detalhe)
│
├── storage/
│   ├── models.py              # ORM SQLAlchemy (5 tabelas)
│   └── repo.py                # Repo — queries, upserts, migrações online
│
└── training/
    ├── resolver.py            # puxa ERA5 para mercados expirados
    ├── calibrator.py          # reescreve bias_entry
    └── backtester.py          # Brier, log-loss, ROI simulado, calibração
```

---

## Configuração (`.env`)

Copie `.env.example` para `.env` e ajuste. Tudo é prefixado com `POLYBOT_`.

| Variável | Default | O que controla |
|---|---|---|
| `POLYBOT_USER_AGENT` | `polybot-weather (unset@example.com)` | User-Agent para APIs (NWS exige contato real). |
| `POLYBOT_BANKROLL_USD` | `1000.0` | Banca para dimensionamento Kelly. |
| `POLYBOT_MIN_EDGE` | `0.05` | Edge mínimo (pp) para recomendar. |
| `POLYBOT_MIN_EV` | `0.10` | EV/$ mínimo. |
| `POLYBOT_MIN_LIQUIDITY_USD` | `50.0` | Mínimo em dólares no topo do ask. |
| `POLYBOT_MAX_HOURS_TO_RESOLUTION` | `72` | Janela máxima até resolução. |
| `POLYBOT_FEE_RATE` | `0.05` | Taker fee embutido em todos os cálculos. |
| `POLYBOT_KELLY_FRACTION` | `0.25` | Fração do Kelly cheio. |
| `POLYBOT_MAX_BET_FRACTION` | `0.05` | Cap por aposta (% da banca). |
| `POLYBOT_DB_URL` | `sqlite:///./polybot.db` | Onde persistir. |
| `POLYBOT_CACHE_DIR` | `./.cache` | Caches JSON (forecast/archive). |
| `POLYBOT_FORECAST_TTL_SECONDS` | `1800` | Cache do forecast (30 min). |
| `POLYBOT_CLIMATOLOGY_TTL_SECONDS` | `86400` | Cache da climatologia (1 dia). |
| `POLYBOT_EXECUTION_ENABLED` | `false` | **Habilita envio real de ordens**. |
| `POLYBOT_PRIVATE_KEY` | — | Chave privada (nunca commite). |
| `POLYBOT_FUNDER_ADDRESS` | — | Endereço funder (Polymarket proxy wallet). |

---

## Segurança e avisos importantes

- **Execução real é off por default.** Precisa `POLYBOT_EXECUTION_ENABLED=true` no `.env` **E** `--confirm` na chamada `polybot trade`. Sem ambos, o bot recusa.
- **Chave privada só sai do `.env`.** Nunca commite. `config.py` usa `repr=False` no campo para não vazar em logs.
- **Estação certa vale dinheiro.** KLGA vs KNYC diverge 3-5°F. O parser lê `resolutionSource` e tenta o ICAO explícito antes de cair para default de cidade. Se vir "defaulting to generic city center" nos logs, **confira o mercado manualmente**.
- **ERA5 tem lag de ~5 dias.** Mercados muito recentes retornam `None` no `resolve`. Rode diário, não instantâneo.
- **Backtest deduplica, mas o DB não.** Múltiplos scans do mesmo mercado geram múltiplos `forecast`+`recommendation`. Calibração e backtest sabem disso (mantêm só o mais recente), mas contagens brutas no SQLite não refletem "apostas únicas".
- **Unidade é sagrada.** `market.unit` é travada no momento da análise. Se calibrar com forecast em °F mas outcome em °C, o bias fica lixo. O schema e o resolver forçam a mesma unidade ponta-a-ponta.

---

## Quickstart

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                    # edite POLYBOT_USER_AGENT com email real
polybot scan                            # lista mercados climáticos ativos
polybot recommend --min-edge 0.05       # roda análise completa em todos
polybot dash                            # TUI interativa
polybot resolve                         # após mercados resolverem (rodar diário)
polybot calibrate && polybot backtest   # métricas do modelo
```

## Cronograma de refino do modelo (4 semanas)

Plano para sair de `bias_entry` vazia até um modelo calibrado com backtest validado out-of-sample. **Perfil de operação: 1 sessão por dia, 20h.** Início: **Seg 2026-04-20**. Pressupõe `source venv/bin/activate` antes dos comandos.

### Princípios de design

1. **Um fluxo canônico diário** — `scan → resolve → recommend`, nessa ordem, todo dia. Sem `--no-save`: se rodar, grava. Múltiplos forecasts por market em dias diferentes **não são duplicatas**, são série temporal por horizonte (72h→48h→24h) — insumo legítimo de calibração.
2. **Por que a ordem importa**: `scan` registra mercados novos primeiro; `resolve` fecha os vencidos (para `recommend` não perder tempo neles); `recommend` grava o snapshot do dia.
3. **Calibrate e backtest são semanais**, não diários: só valem com outcomes acumulados. Calibrar todo dia sem bucket novo é desperdício.
4. **Nenhum comando duplicado entre dias.** Se `resolve` roda segunda, não precisa repetir terça "por garantia" — ele é idempotente e já roda terça no fluxo canônico.

### Fluxo canônico diário (usado em todas as semanas)

```bash
polybot scan && polybot resolve && polybot recommend
```

Abreviado como **DIÁRIO** nas tabelas abaixo.

### Semana 1 — Acumulação (20/04 a 26/04)

Meta: construir o pool inicial de forecasts pareados com mercados que vão resolver nos próximos 5–7 dias. ERA5 ainda não tem os outcomes → sem calibrate/backtest.

| Dia | Horário | Ação |
|---|---|---|
| Seg–Dom | 20:00 | DIÁRIO |

### Semana 2 — Primeiros outcomes + calibração piloto (27/04 a 03/05)

Meta: a partir de ~25/04, ERA5 começa a cobrir os mercados da Sem1. No fim da semana deve haver ≥15 outcomes e ≥1 bucket de bias com n≥5.

| Dia | Horário | Ação |
|---|---|---|
| Seg–Sex | 20:00 | DIÁRIO |
| Sáb | 20:00 | DIÁRIO && `polybot calibrate` |
| Dom | 20:00 | DIÁRIO && `polybot backtest --from 2026-04-20` |

Registre Brier e ROI simulado do backtest num CSV externo — é seu baseline.

### Semana 3 — Tuning de thresholds (04/05 a 10/05)

Meta: achar `min_edge` e `max_hours_to_resolution` que minimizam Brier **e** mantêm ROI simulado ≥ 0. Os experimentos ficam concentrados no **domingo** — durante a semana o DB só acumula dados limpos.

| Dia | Horário | Ação |
|---|---|---|
| Seg–Sex | 20:00 | DIÁRIO |
| Sáb | 20:00 | DIÁRIO && `polybot calibrate` |
| Dom | 20:00 | DIÁRIO, depois bateria de backtests (abaixo) |

Bateria de domingo (anote Brier/ROI de cada rodada num CSV):

```bash
polybot backtest                                             # baseline atual
POLYBOT_MIN_EDGE=0.03 polybot backtest
POLYBOT_MIN_EDGE=0.08 polybot backtest
POLYBOT_MAX_HOURS_TO_RESOLUTION=48 polybot backtest
POLYBOT_MAX_HOURS_TO_RESOLUTION=96 polybot backtest
```

No fim do domingo, **fixe no `.env`** a combinação vencedora.

### Semana 4 — Validação out-of-sample (11/05 a 17/05)

Thresholds congelados da Sem3. O backtest final restringe a janela aos dias da Sem4 — se os ganhos sobrevivem fora da amostra de tuning, o modelo generaliza.

| Dia | Horário | Ação |
|---|---|---|
| Seg–Sex | 20:00 | DIÁRIO |
| Sáb | 20:00 | DIÁRIO && `polybot calibrate` |
| Dom | 20:00 | DIÁRIO && `polybot backtest --from 2026-05-11` |

**Gate para ligar `POLYBOT_EXECUTION_ENABLED=true`:** (a) ≥30 outcomes totais, (b) calibração por decil próxima da diagonal, (c) ROI simulado out-of-sample positivo.

### Rotina estável (após Sem4)

| Dia | Horário | Ação |
|---|---|---|
| Seg–Sáb | 20:00 | DIÁRIO |
| Dom | 20:00 | DIÁRIO && `polybot calibrate && polybot backtest --from $(date -d '30 days ago' +%Y-%m-%d)` |

### Notas operacionais

- **Exploração ao vivo** (não grava no DB): `polybot dash --no-save`. Use quando quiser olhar edges sem poluir os dados de treino.
- **`--no-save` fora disso é raro** — tem propósito só em análises pontuais de um market (`polybot analyze <slug> --no-save`) quando você não quer que entre no pool.
- **Priorize mercados com 24–72h até resolução.** Com 1 sessão/dia você perde o "segundo olhar" pouco antes do close — mercados de <24h ficam subamostrados.
- **Priorize estações de alto volume** (KLGA, KJFK, EGLL, RKSI) para atingir n≥5 por bucket mais rápido.
- **Backup antes de calibrate/reset**: `cp polybot.db polybot.db.bak-$(date +%Y%m%d-%H%M)`.
- **Ajuste as datas** se começar em dia diferente de 2026-04-20.

---

## Licença

MIT.
