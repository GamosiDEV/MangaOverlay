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

- [ ] Opção de fonte de quadrinhos nas Configurações.
- [ ] Área de escrita que acompanha a forma do balão (elipse) em vez do retângulo do texto, e hifenização (balões japoneses altos e estreitos).
- [ ] Caixas retangulares de narração e placas: cobertura retangular. Hoje a cobertura é recortada em elipse (pensada para balões redondos) e sobram pedaços de letras nos cantos; no arquivo gerado isso aparece mais que na tela. O mesmo vale para o fundo cinza que às vezes cobre onomatopeias em quadros.
- [ ] Guardar o balão de cada fala no banco já na importação, para a geração não precisar rodar o detector de novo.

### Fase 11 (opcional): Inpainting

- [ ] Reconstruir o desenho por baixo de textos fora dos balões (narração, placas) com o LaMa, baixado só quando o recurso for usado.

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
