# Plano: tradução em lote, memória por obra e lista de personagens

Plano das próximas funcionalidades do MangaOverlay, decididas em 28/09/2026. Cada fase entrega algo utilizável sozinho e é a base da seguinte. Marque as caixas conforme for implementando.

## Decisões tomadas

| Tema | Decisão |
|---|---|
| Traduções | Ficam salvas em disco, por obra, num banco local (SQLite). Uma página já traduzida nunca é paga de novo, mesmo depois de fechar o app. |
| Obra atual | O usuário escolhe no menu da bandeja qual mangá está lendo. Tudo (traduções, memória, personagens) fica associado a essa obra. |
| Lotes grandes | Importação de pasta de imagens, CBZ ou PDF. A detecção e o OCR rodam localmente (grátis); só o texto vai para a API. |
| Tamanho do bloco | O usuário escolhe quantas páginas vão por requisição. Padrão: 20. O app limita ao máximo seguro do modelo (limite de saída). |
| Falhas | Nenhuma requisição grande única: fila de blocos pequenos e independentes, salva no banco, com novas tentativas automáticas. Só o que falhou é reenviado. |
| Batch API | Opção principal para lotes grandes (50% mais barata, resultado em minutos, com garantia de até 24 h). |
| Lista de personagens | Três modos: sem lista, lista manual ou levantamento automático (com revisão do usuário). |
| Levantamento de nomes | **Rápido por padrão** (só os 2–3 primeiros capítulos, com o modelo principal). **Completo** opcional, com um modelo específico e barato (padrão `gpt-5.4-nano`). |
| Reaproveitamento | Lista de personagens e memória ficam salvas por obra. Nos lotes seguintes, o levantamento só olha os capítulos novos. |
| Revisão de nomes | Primeiro correção local e grátis (substituição de variações conhecidas). Depois, revisão com IA só das falas ambíguas. |

## Fases

### Fase 1: Banco local e obra atual

Base de tudo: sem ela, o lote não tem onde guardar os resultados.

- [x] Banco SQLite em `~/.local/share/MangaOverlay/mangaoverlay.db`, versionado: cada fase acrescenta as próprias tabelas por migração automática (a Fase 1 criou `obras` e `traducoes`). Tabelas previstas:
  - `obras`: nome, idioma de origem, data de criação.
  - `paginas`: obra, capítulo, número, origem (arquivo ou captura), assinatura da imagem.
  - `regioes`: página, posição do balão, texto original lido pelo OCR.
  - `traducoes`: região, motor, modelo, idioma de destino, texto traduzido.
  - `personagens`: obra, nome original (opcional), nome na tradução, gênero, jeito de falar, notas.
  - `memoria`: obra, glossário e resumo da história.
  - `lotes` e `requisicoes`: estado de cada envio (pendente, enviada, concluída, falhou), tentativas, id do Batch.
- [x] Menu da bandeja: "Obra atual" (escolher, criar nova, "nenhuma"). Cada obra lembra o próprio idioma de origem.
- [x] O cache de traduções em memória passa a ler e gravar no banco.
- [x] Busca de tradução pelo **texto lido pelo OCR** (e não só pela aparência do recorte), para reconhecer a página mesmo com zoom diferente.
- [x] "Esquecer as traduções desta obra…", com confirmação.

**Pronto quando:** traduzir uma página, fechar e reabrir o app e apertar o atalho na mesma página mostra a tradução na hora, sem chamada à API. ✅ Verificado (também com zoom de 85% e 120% e página em outra posição).

### Fase 2: Importação de capítulos

- [x] Importar uma pasta de imagens, um arquivo CBZ ou um PDF, informando obra e capítulo (sugestão automática pelo nome do arquivo ou pasta). Uma pasta com subpastas/CBZs/PDFs importa vários capítulos de uma vez.
- [x] Etapa local: detecção e OCR de todas as páginas na GPU, gravando tudo no banco (tabelas `capitulos`, `paginas`, `regioes`, migração 2).
- [x] Retomada: se o app fechar no meio, continua da última página processada (automática ao abrir o app).
- [x] Janela de progresso: páginas processadas e tempo estimado.
- [x] O atalho de traduzir tem prioridade sobre a importação na GPU.
- [x] Busca aproximada no banco (o OCR pode ler a mesma fala com um caractere de ruído diferente na tela e no arquivo).

**Pronto quando:** importar 3 capítulos resulta no texto original de todas as páginas no banco, e a importação sobrevive a fechar o app no meio. ✅ Verificado (processo morto com kill -9 no meio; a retomada terminou só as páginas pendentes, com resultado idêntico ao de uma importação sem interrupção).

### Fase 3: Lista de personagens

- [ ] Tela da lista, por obra: nome na tradução (obrigatório), nome original (opcional), gênero, jeito de falar e notas. Dá para digitar ou colar.
- [ ] Sugestões de nomes originais tiradas dos textos lidos pelo OCR, para escolher em vez de digitar em japonês, coreano ou chinês.
- [ ] Levantamento **rápido** (padrão): analisa só os 2–3 primeiros capítulos ainda não analisados, com o modelo principal.
- [ ] Levantamento **completo** (opcional): todos os capítulos, com o modelo definido em "Modelo para levantamento de nomes" (padrão `gpt-5.4-nano`).
- [ ] O resultado do levantamento abre na tela da lista para revisão antes de ser usado.
- [ ] Reaproveitamento: o levantamento só analisa capítulos que ainda não foram analisados.
- [ ] A lista vai junto em toda tradução daquela obra (modo página a página e lote).

**Pronto quando:** um lote traduzido com a lista mantém o mesmo nome e gênero de cada personagem em todos os capítulos.

### Fase 4: Fila de envio com blocos configuráveis

- [ ] Configuração "Páginas por requisição" (padrão 20), com teto calculado pelo limite de saída do modelo e aviso quando o valor pedido passa do teto.
- [ ] Fila salva no banco: cada bloco é uma requisição independente.
- [ ] Novas tentativas automáticas com espera crescente em erros temporários (limite de requisições, servidor, rede).
- [ ] Validação da resposta: confere se todos os balões voltaram e pede de novo só os que faltaram.
- [ ] Retomada após fechar o app: reenvia só o que estava pendente.
- [ ] Estimativa de custo antes de enviar ("~600 páginas, ~US$ 0,30") e custo real registrado no fim.

**Pronto quando:** derrubar a internet no meio de um lote de 5 capítulos e reconectar termina o lote sem retraduzir o que já estava pronto.

### Fase 5: Batch API da OpenAI

- [ ] Montar o arquivo JSONL com os blocos do lote, enviar e guardar o id do trabalho no banco.
- [ ] Acompanhar o andamento (ao abrir o app e periodicamente) e notificar quando terminar.
- [ ] Importar os resultados; reenviar em um novo Batch só os pedidos que falharam.
- [ ] Modo híbrido: os 1–2 primeiros capítulos em envio normal, para montar a memória da obra, e o resto pela Batch API.
- [ ] Opção no lote: "Envio normal (mais rápido)" ou "Batch API (50% mais barato, até 24 h)".

**Pronto quando:** enviar 30 capítulos pela Batch API, fechar o app, reabrir mais tarde e encontrar todos traduzidos no banco.

### Fase 6: Memória da obra

- [ ] Glossário automático: a cada bloco o modelo devolve também os termos e nomes novos, que vão para a memória da obra.
- [ ] Resumo da história atualizado a cada capítulo (chamada curta e barata).
- [ ] A memória vai no início de toda requisição da obra, em ordem fixa, para aproveitar o cache de prompt automático da OpenAI (entrada em cache custa de 10 a 25% do preço normal).
- [ ] Tamanho da memória limitado (~2.000 tokens), para o custo por página não crescer com o tempo.

**Pronto quando:** o custo por página de uma obra com 30 capítulos lidos é o mesmo de uma obra nova, e o uso reportado pela API mostra entrada em cache.

### Fase 7: Revisão de nomes

- [ ] Correção local: substituir nas traduções salvas as variações conhecidas de cada nome da lista.
- [ ] Revisão com IA só das falas com nomes novos ou suspeitos, com opção de revisar a obra inteira.
- [ ] Mostrar o que foi alterado antes de gravar.

**Pronto quando:** trocar a grafia de um personagem na lista corrige todos os capítulos já traduzidos sem nova tradução completa.

### Fase 8: Tempo real (pendente desde antes)

- [ ] Transmissão contínua da tela pelo portal ScreenCast/PipeWire, como opção secundária nas configurações (o atalho continua sendo o modo principal).
- [ ] Detecção de mudança (troca de página, rolagem) e nova tradução/colorização automática, usando o banco para mostrar na hora o que já foi traduzido.

## Custos de referência (gpt-4.1-mini)

Premissas: página típica com ~10 balões (~300 tokens de texto lido e ~500 de tradução); blocos de 20 páginas; Batch API.

| Lote | Sem lista | Com lista | Com levantamento completo + lista |
|---|---|---|---|
| 1 capítulo (20 págs) | ~US$ 0,010 | ~US$ 0,010 | ~US$ 0,013 |
| 10 capítulos (200 págs) | ~US$ 0,097 | ~US$ 0,098 | ~US$ 0,125 |
| 30 capítulos (600 págs) | ~US$ 0,29 | ~US$ 0,30 | ~US$ 0,37 |
| 100 capítulos (2.000 págs) | ~US$ 0,97 | ~US$ 0,98 | ~US$ 1,23 |

O levantamento rápido custa menos de US$ 0,01 em qualquer tamanho. Páginas com muito texto (20 a 30 balões, caixas de narração, páginas de explicação) custam de 2 a 4 vezes mais que a página típica.

## Fora do escopo por enquanto

- Identificar a obra automaticamente e colorir com as cores originais (decidido manter a colorização como está).
- Batch API do Claude. A fila das fases 4 e 5 fica independente do provedor, para acrescentar depois.
