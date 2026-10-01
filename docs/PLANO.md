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
| Capítulos traduzidos em imagem (30/09/2026) | Gerados no PC a partir dos capítulos importados, para ler em qualquer leitor (inclusive no celular). Formato padrão **CBZ**, com opção de **pasta de imagens**. Sempre oferece traduzir as falas que faltam, com o custo antes. Cada usuário gera o que quiser para uso próprio; distribuir as imagens é responsabilidade de quem gera. |

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

- [x] Tela da lista, por obra: nome na tradução (obrigatório), nome original (opcional), gênero, jeito de falar e notas. Dá para digitar ou colar.
- [x] Sugestões de nomes originais tiradas dos textos lidos pelo OCR, para escolher em vez de digitar em japonês ou coreano (pelos tratamentos さん/ちゃん/先生/씨/선배… e por katakana). Chinês fica sem sugestões locais: não tem um padrão confiável de tratamento.
- [x] Levantamento **rápido** (padrão): analisa só os 3 primeiros capítulos ainda não analisados, com o modelo principal.
- [x] Levantamento **completo** (opcional): todos os capítulos, com o modelo de levantamento (padrão `gpt-5.4-nano`; `claude-haiku-4-5` para o Claude). Textos grandes são divididos em pedidos de ~30 mil tokens.
- [x] O resultado do levantamento abre na tela da lista para revisão antes de ser usado; os capítulos só são marcados como analisados ao salvar.
- [x] Reaproveitamento: o levantamento só analisa capítulos que ainda não foram analisados.
- [x] A lista vai junto em toda tradução com IA daquela obra (hoje no modo página a página; o lote usa a mesma função nas fases 4 e 5).
- [x] Custo estimado mostrado antes de enviar (tabela de preços em `pricing.py`, reaproveitada na Fase 4).

**Pronto quando:** um lote traduzido com a lista mantém o mesmo nome e gênero de cada personagem em todos os capítulos. ⏳ A parte "lote" depende das fases 4 e 5; a lista já vai em toda tradução página a página, e o levantamento foi validado com chamadas reais à OpenAI (`gpt-4.1-mini` e `gpt-5.4-nano`).

### Fase 4: Fila de envio com blocos configuráveis

- [x] Configuração "Páginas por requisição" (padrão 20), com teto calculado pelo limite de saída do modelo e pelo tamanho estimado das páginas escolhidas; aviso e botão desativado acima do teto.
- [x] Fila salva no banco (migração 4: `lotes`, `requisicoes`, `requisicao_paginas`): cada bloco é uma requisição independente. As falas são calculadas na hora de enviar, só com o que falta; repetidas vão uma vez.
- [x] Novas tentativas automáticas com espera crescente em erros temporários (2, 5, 15, 30 e 60 s); se persistirem, o lote pausa. Erros definitivos pausam com a mensagem.
- [x] Validação da resposta: pede de novo só as falas que faltaram (até 2 rodadas extras); respostas fora do formato também ganham nova rodada.
- [x] Retomada após fechar o app: reenvia só o que estava pendente (automático ao abrir). "Pausar" pelo usuário só volta pelo menu.
- [x] Estimativa de custo antes de enviar e custo real registrado no fim (uso de tokens informado pela API, inclusive cache).
- [x] Na leitura, qualquer tradução já paga com IA da obra é reaproveitada, mesmo com outro motor ou modelo.

**Pronto quando:** derrubar a internet no meio de um lote de 5 capítulos e reconectar termina o lote sem retraduzir o que já estava pronto. ✅ Verificado com tradutor simulado (erros temporários, app fechado no meio, retomada só dos blocos restantes, nenhuma fala enviada duas vezes) e com a API real (lote de 6 falas: estimativa US$ 0,00049, custo real US$ 0,00027; leitura na tela depois, com o modo visão e com o NLLB, sem nenhuma chamada nova).

### Fase 5: Batch API da OpenAI

- [x] Montar o arquivo JSONL com os blocos do lote (endpoint `/v1/responses`, o mesmo do envio normal), enviar e guardar o id do trabalho no banco (migração 5).
- [x] Acompanhar o andamento (ao abrir o app e a cada minuto) e notificar quando terminar.
- [x] Importar os resultados; falas que o modelo pulou e pedidos que voltaram no arquivo de erros vão num novo envio, só eles (até 2 rodadas extras).
- [ ] ~~Modo híbrido~~ → movido para a Fase 6: o contexto entre blocos usa só o texto original, que já está no banco antes de qualquer tradução, então os primeiros capítulos em envio normal só fazem sentido quando existir a memória da obra.
- [x] Opção no lote: "Envio normal (mais rápido)" ou "Batch API (50% mais barato, até 24 h)", com o custo das duas.
- [x] Pausar cancela o lote na OpenAI guardando o que já voltou; retomar envia só o resto. Lote recusado na validação pausa com o motivo.

**Pronto quando:** enviar 30 capítulos pela Batch API, fechar o app, reabrir mais tarde e encontrar todos traduzidos no banco. ✅ Verificado com uma Batch API simulada (esquecimento de fala + pedido no arquivo de erros → 2º envio só com as 9 falas que faltaram; pausa durante o processamento → parcial guardado e retomada; lote recusado → pausa com o motivo) e com um lote real pequeno na OpenAI.

### Fase 6: Memória da obra

- [x] Glossário automático: a cada bloco (e a cada página na tela) o modelo devolve também os termos novos (`new_terms`), que entram como pendentes na memória da obra (migração 6).
- [x] Resumo da história atualizado a cada capítulo concluído (modelo barato, só com o texto traduzido; ~US$ 0,001 por capítulo; pode ser desligado).
- [x] A memória vai no início de toda requisição da obra, em ordem fixa, e só muda em pontos fixos (fim de capítulo; a cada 15 termos na tela), para aproveitar o cache de prompt. OpenAI: `prompt_cache_key` por obra (sem ela o cache não acontecia nos testes). Claude: cache marcado no fim das instruções.
- [x] Tamanho da memória limitado (80 termos + resumo de ~180 palavras), para o custo por página não crescer com o tempo.
- [x] Modo híbrido do lote (vindo da Fase 5): o 1º capítulo traduzido na hora para montar a memória; o resto vai à Batch API já com ela.
- [x] Tela "Memória da obra…" para ver o resumo e o glossário e apagá-los.

**Pronto quando:** o custo por página de uma obra com 30 capítulos lidos é o mesmo de uma obra nova, e o uso reportado pela API mostra entrada em cache. ✅ Verificado: a memória tem teto fixo (não cresce com os capítulos) e, com a API real, a 2ª e a 3ª chamadas com a mesma memória vieram com 1.408 de ~1.570 tokens em cache.

### Fase 7: Revisão de nomes

- [x] Correção local: substituir nas traduções salvas as variações de cada nome da lista (grafia parecida, mantendo o tratamento; palavras comuns como "Minha" nunca são trocadas) e propagar trocas de grafia feitas na lista.
- [x] Revisão com IA só das falas que citam o personagem no original mas não trazem o nome na tradução, com opção de revisar a obra inteira; custo estimado antes e real depois.
- [x] Mostrar o que foi alterado antes de gravar (tela com antes → depois, motivo e caixas para desmarcar).

**Pronto quando:** trocar a grafia de um personagem na lista corrige todos os capítulos já traduzidos sem nova tradução completa. ✅ Verificado: Mina → Mena na lista propôs a troca nas duas traduções afetadas e gravou só a marcada; a revisão com IA real corrigiu "O professor chegou!" → "O professor Sakura chegou!" e "Minha, vamos!" → "Mina, vamos!" (US$ 0,00016).

### Fase 8: Tempo real (pendente desde antes)

- [ ] Transmissão contínua da tela pelo portal ScreenCast/PipeWire, como opção secundária nas configurações (o atalho continua sendo o modo principal).
- [ ] Detecção de mudança (troca de página, rolagem) e nova tradução/colorização automática, usando o banco para mostrar na hora o que já foi traduzido.

### Fase 9: Gerar capítulos traduzidos (CBZ ou pasta de imagens)

Decidida em 30/09/2026. A ideia: traduzir no PC e ler no celular, num leitor comum (Mihon/Tachiyomi, Perfect Viewer), sem overlay.

- [x] Geração a partir do que já está no banco: abre a página original (`load_page`), busca a tradução de cada fala salva (qualquer motor da obra, com preferência pelo atual) e desenha com o mesmo `paint_items` do overlay, na resolução original.
- [x] O detector roda de novo em cada página só para recuperar o contorno do balão (o banco guarda apenas a caixa do texto); o desenho não apaga a borda do balão.
- [x] Saída em **CBZ** (padrão, com `ComicInfo.xml`) ou **pasta de imagens**, escolhida na janela. Páginas em JPG de alta qualidade; páginas em branco, não lidas ou com erro vão como no original.
- [x] Falas sem tradução salva: a janela sempre mostra quantas são e oferece traduzi-las com o motor atual, com o custo estimado antes. Sem a opção, o original fica visível.
- [x] Botão "Gerar capítulo(s) traduzido(s)…" na janela de obras e item no menu da bandeja; janela de progresso com Parar e "Abrir pasta" no fim.
- [x] Linha de comando: `--gerar PASTA` com `--obra` (e `--formato`, `--capitulo`, `--traduzir-faltantes`).
- [x] Testes com uma página sintética (sem modelos) e seção no README, com a nota de responsabilidade.

- [x] Traduções feitas quando a obra tinha outro idioma de origem também valem (o texto lido é o mesmo; só a chave mudou). Achado no teste com um banco real: uma obra passou de "en" para "ja" depois de traduzida e nada era encontrado.

**Pronto quando:** um capítulo importado e traduzido vira um CBZ que abre num leitor de celular com as falas traduzidas nos balões, sem nenhuma chamada nova à API. ✅ Verificado com um volume real (PDF, 202 páginas): 1.071 falas desenhadas em 34 s na RTX 5050, nenhuma chamada à API, ~150 MB. Falta abrir o arquivo num leitor de celular. Defeito visível anotado na Fase 10 (caixas retangulares).

### Fase 10: Qualidade do texto nas imagens geradas

- [x] Fonte de quadrinhos incluída no app (Comic Neue, SIL Open Font License, em `assets/fonts`), com o botão "Usar Comic Neue" nas Configurações. O padrão continua sendo a fonte do sistema.
- [x] Texto que acompanha a forma do balão: num balão redondo, cada linha tem a largura da elipse naquela altura; só é usado se a letra ficar pelo menos do tamanho do jeito antigo (balões cheios de texto às vezes rendem menos na elipse).
- [x] Hifenização com o pyphen (pt, en, es, fr, de, it), só em palavras que não cabem inteiras nem numa linha vazia e só se a fonte ficar 15% maior; pelo menos 2 letras antes e 3 depois do hífen; palavras com hífen não são quebradas de novo. Sinais soltos ("-", "!", "...") ficam grudados na palavra vizinha.
- [x] Caixas retangulares de narração e placas (contorno reto até perto dos cantos, papel logo por dentro): a caixa inteira é coberta, com a cor do papel medida junto do contorno, e o texto usa a caixa toda. Acabou com os pedaços de letras nos cantos e com o fundo cinza nas onomatopeias em caixas.
- [x] Balão e área de escrita de cada fala guardados no banco na importação (migração 7, coluna `regioes.forma`); a geração só roda o detector nas falas importadas antes disto. Vão junto na exportação, numa chave à parte que versões anteriores ignoram.

**Pronto quando:** as páginas de teste (Monster vol. 9, caixas de narração; Dorohedoro vol. 1, balões redondos) saem sem restos do texto original e com letra igual ou maior que antes. ✅ Verificado nas duas, na geração e no desenho do overlay (`--image`). Continua igual: texto fora de balão (narração solta sobre o desenho) é coberto por um retângulo, o que a Fase 11 resolveria.

### Fase 11: Reconstruir o desenho por baixo do texto (inpainting)

- [x] Modelo LaMa (Apache-2.0), na exportação ONNX de `Carve/LaMa-ONNX` (Apache-2.0, ~200 MB), convertido para PyTorch como o colorizador; baixado no primeiro uso (e pelo instalador, com os outros modelos). ~0,3 s por texto na RTX 5050.
- [x] Opção "Reconstruir o desenho por baixo do texto fora dos balões" na janela de gerar capítulos (lembrada) e `--reconstruir` na linha de comando. Desligada por padrão.
- [x] Só o texto sobre o desenho é reconstruído: borda em volta que não é papel liso **e** muitos meios-tons dentro da caixa (medido em 1.887 falas de dois volumes: texto em papel fica em 0,05–0,13; sobre desenho, ~0,19–0,25). "Não ter balão" não serve de critério: com o limiar baixo dos arquivos, o detector vê balões em volta de onomatopeias.
- [x] O texto reconstruído recebe a tradução no lugar do original, sem cobertura, com o contorno de legibilidade dos textos soltos.
- [x] De quebra: falas marcadas duas vezes pelo detector são desenhadas uma vez só (sem cobertura, as duas traduções apareciam uma sobre a outra), e a cobertura de texto numa mancha branca sobre o desenho usa a cor do papel dentro da caixa, e não a mediana cinza da borda (acabou com os retângulos cinza, também sem a reconstrução e no overlay).

**Pronto quando:** texto de narração sobre o desenho sai sem retângulo, com o desenho refeito por baixo. ✅ Verificado no Berserk vol. 11 (tijolos e retícula reconstruídos); o volume inteiro (248 páginas) levou 42 s com a opção, contra 31 s sem.

### Fase 12: Memória antes do lote e envio em partes

Decidida em 01/10/2026. Generaliza o modo híbrido (que traduzia só o 1º capítulo na hora).

- [x] **Capítulos de memória:** na Batch API, os X primeiros capítulos marcados são traduzidos na hora, em pedidos de y páginas, para montar o glossário e o resumo antes do resto ir à OpenAI. O tamanho y é opcional: sem escolher, vale o mesmo dos blocos do lote.
- [x] **Envio em Z partes:** o resto vai à Batch API em Z envios seguidos, divididos nos limites dos capítulos. Depois de cada parte, os capítulos completos são resumidos e o glossário é consolidado, e a parte seguinte vai com a memória nova.
- [x] **Nomes automáticos:** antes de cada grupo (capítulos de memória, cada parte, ou o lote inteiro no envio normal), os capítulos ainda não analisados passam pelo levantamento de nomes (modelo barato) e os personagens novos são **salvos direto** na lista (dá para revisar depois). Só com motores de IA; uma falha no levantamento só gera um aviso.
- [x] Custo estimado separando capítulos de memória (preço normal), partes (Batch API, 50%) e levantamento de nomes.

**Pronto quando:** um lote na Batch API com capítulos de memória e 3 partes manda uma parte de cada vez, com o resumo e os nomes atualizados entre elas. ✅ Verificado com a OpenAI simulada (`tests/test_batch_parts.py`: uma parte por vez, rodadas recomeçando na parte nova, nomes levantados só dos capítulos da parte).

### Fase 13: Fluxo completo (obra → capítulos → tradução → resultado)

- [ ] Assistente com tudo configurado de uma vez: obra (existente ou nova), capítulos (importar novos e/ou escolher já importados), tradução (motor, modelo, modo, blocos, memória, partes, nomes) e resultado (gerar ou não, CBZ ou pasta, pasta de destino, reconstruir o desenho).
- [ ] **Limite de custo:** capítulos novos só têm custo conhecido depois da leitura; se a estimativa passar do limite, o fluxo pausa e pergunta, senão segue sozinho.
- [ ] Executor que roda uma etapa atrás da outra (importar → traduzir → gerar), com o estado salvo no banco: fechar o app no meio (por exemplo, esperando a Batch API) não perde o fluxo, que continua ao abrir de novo.

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
