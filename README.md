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
4. **Tradução:** pelo motor escolhido (veja abaixo). Traduções já feitas ficam guardadas, então voltar a uma página é instantâneo.
5. **Desenho:** cobre o texto original sem apagar o contorno do balão e encaixa a tradução no maior tamanho de fonte que couber.

### Colorização

1. **Onde está a página:** a partir dos balões detectados, o app expande um retângulo até encontrar a borda da página (faixa uniforme escura do leitor, ou elementos coloridos da interface). Menus e outras janelas não são coloridos. Sem balões na tela, não há o que colorir.
2. **Cor:** o modelo [manga-colorization-v2](https://github.com/qweasdd/manga-colorization-v2), exportado em ONNX e convertido para PyTorch, roda na página reduzida (cerca de 0,2 s na RTX 5050).
3. **Sem alterar o conteúdo:** da saída do modelo só se aproveita a cor. O brilho vem da captura original em resolução total, então traço, retículas e texto ficam exatamente como estavam.

As cores são uma interpretação do modelo: podem variar de uma página para outra (o cabelo de um personagem pode mudar de cor) e ficam mais fracas em cenários complexos. O repositório original do modelo não declara licença; o espelho usado (`ifritraen/manga-colorization-v2-fp32`) declara Apache-2.0. Trate como uso pessoal.

Com os modelos carregados, uma tela 2560×1600 leva cerca de 0,2 s na RTX 5050 com o tradutor offline. O primeiro uso baixa cerca de 3,6 GB de modelos.

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
- Modelos: `~/.cache/huggingface` e `~/.cache/MangaOverlay/easyocr`

## Licença

[MIT](LICENSE)
