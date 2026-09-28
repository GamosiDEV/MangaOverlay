# MangaOverlay

Traduz os balões de mangá, manhwa, manhua e quadrinhos que estão na tela e mostra a tradução por cima deles, sem alterar a página original. Funciona com qualquer leitor: navegador, app de CBZ/PDF, visualizador de imagens.

Fica em segundo plano, na bandeja. A sobreposição não recebe cliques, então dá para continuar usando o que está embaixo.

| Atalho | Ação |
|---|---|
| **Ctrl+Alt+M** | Traduz os balões da tela (e colore, se "Colorir junto com a tradução" estiver marcado no menu) |
| **Ctrl+Alt+C** | Mostra a página de mangá colorida |
| **Ctrl+Alt+N** | Esconde tudo e volta ao original |

> Modo atual: **sob demanda** (aperte o atalho a cada página). O modo em tempo real, que acompanha a tela sozinho, é o próximo passo.

## Como funciona

1. **Captura:** um print de cada monitor (no GNOME/Wayland, pelo portal do sistema).
2. **Detecção:** o modelo [comic-text-and-bubble-detector](https://huggingface.co/ogkalu/comic-text-and-bubble-detector) (RT-DETR-v2, Apache-2.0) encontra balões e textos. Só vale o texto dentro de um balão com alta confiança, para não traduzir menus e botões.
3. **Leitura (OCR):** [manga-ocr](https://huggingface.co/kha-white/manga-ocr-base) para japonês, que lê texto vertical; EasyOCR para coreano, chinês e inglês.
4. **Tradução:** primeiro o app procura no banco local uma tradução já feita para o mesmo texto; só o que falta vai para o motor escolhido (veja abaixo). A busca é pelo texto lido, então a página é reconhecida mesmo com outro zoom ou em outra posição da tela.
5. **Desenho:** cobre o texto original sem apagar o contorno do balão e encaixa a tradução no maior tamanho de fonte que couber.

### Colorização

1. **Onde está a página:** a partir dos balões detectados, o app expande um retângulo até encontrar a borda da página (faixa uniforme escura do leitor, ou elementos coloridos da interface). Menus e outras janelas não são coloridos. Sem balões na tela, não há o que colorir.
2. **Cor:** o modelo [manga-colorization-v2](https://github.com/qweasdd/manga-colorization-v2), exportado em ONNX e convertido para PyTorch, roda na página reduzida (cerca de 0,2 s na RTX 5050).
3. **Sem alterar o conteúdo:** da saída do modelo só se aproveita a cor. O brilho vem da captura original em resolução total, então traço, retículas e texto ficam exatamente como estavam.

As cores são uma interpretação do modelo: podem variar de uma página para outra (o cabelo de um personagem pode mudar de cor) e ficam mais fracas em cenários complexos. O repositório original do modelo não declara licença; o espelho usado (`ifritraen/manga-colorization-v2-fp32`) declara Apache-2.0. Trate como uso pessoal.

Com os modelos carregados, uma tela 2560×1600 leva cerca de 0,2 s na RTX 5050 com o tradutor offline. O primeiro uso baixa cerca de 3,6 GB de modelos.

## Obras e traduções salvas

No menu da bandeja, **Obra** escolhe o mangá que você está lendo (ou cria um novo). Cada obra guarda as próprias traduções e lembra o idioma de origem. Toda tradução fica salva em disco: voltar a uma página, mesmo depois de fechar o app, mostra a tradução na hora e não gera nova cobrança na API. Com "Nenhuma", as traduções também ficam salvas, sem obra associada.

"Esquecer as traduções desta obra…" apaga as traduções salvas da obra atual (pede confirmação).

A busca no banco é primeiro pelo texto exato e, se não achar, por semelhança (80% ou mais, em falas com 4 caracteres ou mais), porque o OCR às vezes lê um risco do desenho como um caractere a mais.

## Importar capítulos

"Importar capítulos…" no menu da bandeja lê capítulos inteiros de uma vez, para a obra atual:

- **Pasta de imagens:** um capítulo.
- **Pasta com subpastas, CBZs ou PDFs:** vários capítulos de uma vez, ordenados pelo número no nome ("Cap 2" antes de "Cap 10").
- **Arquivos CBZ, ZIP ou PDF:** um capítulo cada.

A detecção dos balões e o OCR rodam na sua GPU, sem custo de API; o texto de cada balão fica no banco para a tradução em lote (próximas fases). As imagens não são copiadas: o banco guarda só onde está cada página. Uma janela mostra o progresso, e o atalho de traduzir continua funcionando durante a importação (tem prioridade).

Se o app fechar no meio, nada se perde: ao abrir de novo, a importação continua das páginas que faltavam ("Retomar importação" no menu também faz isso). Capítulos já importados na mesma obra são ignorados se escolhidos de novo.

## Personagens da obra

"Personagens da obra…" no menu da bandeja abre a lista de personagens da obra atual. Ela vai junto em toda tradução com IA (OpenAI ou Claude) daquela obra, para que cada personagem tenha sempre o mesmo nome, gênero e jeito de falar. O NLLB e o Google gratuito não usam a lista.

- **Manual:** só o nome como você quer ver na tradução é obrigatório; o nome original, o gênero, o jeito de falar e as notas são opcionais. Não é preciso digitar em japonês: a coluna "Encontrados no texto" mostra nomes achados no texto já lido (pelos tratamentos como さん, ちゃん, 先生, 씨, 선배 e por palavras em katakana); duplo clique adiciona.
- **Levantamento rápido:** a IA lê os 3 primeiros capítulos importados ainda não analisados, com o modelo principal, e sugere os personagens.
- **Levantamento completo:** lê todos os capítulos importados ainda não analisados, com um modelo barato (padrão `gpt-5.4-nano` ou `claude-haiku-4-5`).

Os dois levantamentos mostram o custo estimado antes de enviar e trazem as sugestões destacadas para revisão; nada é salvo sem clicar em Salvar. Capítulos já analisados não são lidos de novo nos próximos levantamentos.

## Motores

| Motor | Precisa de | Observação |
|---|---|---|
| OCR local + tradução offline (NLLB) | GPU (ou CPU, mais lento) | Padrão. Grátis e offline; qualidade razoável. O NLLB tem licença CC-BY-NC (uso pessoal). |
| OCR local + Google Tradutor gratuito | internet | Endpoint não oficial; o Google bloqueia com frequência (erro 429). |
| OCR local + OpenAI GPT / Claude (texto) | chave de API | Tradução bem melhor, com contexto das páginas anteriores. Custa centavos por capítulo. |
| OpenAI GPT / Claude lê a imagem e traduz | chave de API | Melhor com textos difíceis (estilizados, sobre o desenho). Aceita origem "Detectar". |

As chaves ficam no chaveiro do sistema (Configurações, no ícone da bandeja) ou nas variáveis `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. O Claude usa por padrão o `claude-opus-5` com esforço baixo, para responder rápido, e o fallback do servidor, que evita que uma recusa indevida deixe a página sem tradução. `claude-sonnet-5` e `claude-haiku-4-5` são mais baratos e mais rápidos.

## Instalação (Linux)

```bash
sudo apt install libxcb-cursor0
python3 -m venv .venv
.venv/bin/pip install --index-url https://download.pytorch.org/whl/cu128 torch torchvision
.venv/bin/pip install -r requirements.txt
.venv/bin/python main.py
```

Sem GPU NVIDIA, instale o PyTorch comum (`pip install torch torchvision`) e desmarque "Usar a GPU" nas configurações.

## Wayland (GNOME)

- A captura usa o portal do sistema. Na primeira vez, o GNOME pergunta se o app pode capturar a tela.
- Os atalhos são registrados em Configurações → Teclado → Atalhos personalizados ("MangaOverlay: traduzir a tela", "colorir a tela" e "esconder a tradução"). Eles executam `main.py --translate`, `--colorize` e `--hide`.
- A interface roda via XWayland, o único jeito de uma janela ficar por cima das outras no GNOME Wayland. Para desativar: `MANGAOVERLAY_NATIVE_WAYLAND=1`.

## Linha de comando

```
python main.py                  # inicia na bandeja
python main.py --translate      # traduz a tela agora
python main.py --colorize       # mostra a página colorida
python main.py --hide           # esconde tudo
python main.py --image pagina.png [--out saida.png] [--engine local] [--source ja] [--color] [--no-translate]
                                # traduz e/ou colore um arquivo e grava o resultado (para testes)
```

## Limitações conhecidas

- Textos fora dos balões (narração solta, onomatopeias) vêm desligados por padrão, porque o detector os confunde com textos de sites e menus. Dá para ligar nas configurações.
- Com OCR local, o idioma de origem precisa estar certo no menu da bandeja. "Detectar" só funciona nos motores em que o LLM lê a imagem.
- Se a página rolar, a tradução e a cor ficam no lugar antigo até o próximo atalho. O modo em tempo real resolve isso.
- Testado no Linux (GNOME 50, Wayland, dois monitores). O código de Windows/X11 vem do PiPScreenTranslate, mas ainda não foi testado aqui.

## Próximos passos

1. **Tempo real** (opção secundária nas configurações): transmissão contínua da tela pelo portal ScreenCast/PipeWire, detecção de mudança (troca de página, rolagem) e nova tradução/colorização automática.

## Arquivos

- Configurações: `~/.config/MangaOverlay/config.json`
- Obras e traduções salvas: `~/.local/share/MangaOverlay/mangaoverlay.db` (SQLite)
- Modelos: `~/.cache/huggingface` e `~/.cache/MangaOverlay/easyocr`

## Licença

[MIT](LICENSE)
