# MangaOverlay

Traduz os balões de mangá, manhwa, manhua e quadrinhos que estão na tela e mostra a tradução por cima deles, sem alterar a página original. Funciona com qualquer leitor: navegador, app de CBZ/PDF, visualizador de imagens.

Fica em segundo plano, na bandeja do sistema. A tradução não recebe cliques, então dá para continuar usando o que está embaixo.

| Atalho | Ação |
|---|---|
| **Ctrl+Alt+M** | Traduz os balões da tela (e colore, se "Colorir junto com a tradução" estiver marcado no menu) |
| **Ctrl+Alt+C** | Mostra a página de mangá colorida |
| **Ctrl+Alt+N** | Esconde tudo e volta ao original |

Roda no **Windows 10/11 (64 bits)** e no **Linux** (testado no Ubuntu com GNOME, Wayland e X11).

- [Instalação](#instalação)
  - [Windows, com o instalador](#windows-com-o-instalador)
  - [Linux, com o instalador](#linux-com-o-instalador)
  - [Sem o instalador](#sem-o-instalador)
  - [Atualizar e desinstalar](#atualizar-e-desinstalar)
- [Primeiros passos](#primeiros-passos)
- [Recursos](#recursos)
- [Motores de tradução](#motores-de-tradução)
- [Solução de problemas](#solução-de-problemas)
- [Linha de comando](#linha-de-comando)
- [Onde ficam os arquivos](#onde-ficam-os-arquivos)
- [Como funciona](#como-funciona)

## Instalação

O instalador cuida de tudo: não é preciso ter Python instalado. Ele baixa um Python próprio, o PyTorch certo para a sua placa de vídeo, as bibliotecas e os modelos de IA, e cria os atalhos. Depois é só abrir o app.

**O que precisa:**

- Internet durante a instalação. Depois disso, o motor offline funciona sem internet.
- Espaço em disco: cerca de **12 GB** com placa NVIDIA, ou **6 GB** sem ela (o PyTorch com CUDA ocupa uns 6 GB e os modelos, uns 4 GB).
- Placa NVIDIA é opcional, mas recomendada: na CPU, cada tradução leva alguns segundos a mais. O instalador detecta a placa sozinho. Com placa AMD ou Intel, o app roda na CPU.

### Windows, com o instalador

1. Baixe o **`MangaOverlay-vX.Y.Z-windows.zip`** da [página de releases](https://github.com/GamosiDEV/MangaOverlay/releases/latest).
2. Clique com o botão direito no arquivo e escolha **Extrair tudo…**.
3. Na pasta extraída, dê dois cliques em **`instalar-windows.cmd`**.
   - Se aparecer "O Windows protegeu o computador", clique em **Mais informações** e depois em **Executar assim mesmo**. O aviso aparece porque o arquivo veio da internet e não tem assinatura digital.
   - Se o computador não tiver o Microsoft Visual C++ Redistributable (necessário para o PyTorch), o Windows pede permissão de administrador para instalá-lo. É o único passo que precisa dessa permissão.
4. Espere terminar (de 10 a 30 minutos, conforme a internet). No fim, o app abre sozinho.

O MangaOverlay fica no **menu Iniciar** e na **área de trabalho**. Depois de instalado, a pasta extraída pode ser apagada.

O ícone aparece na bandeja, perto do relógio. Se não estiver lá, clique na setinha **^** dos ícones ocultos; dá para arrastá-lo para a barra para deixá-lo sempre visível.

<details>
<summary>Opções do instalador do Windows</summary>

Rode no PowerShell, de dentro da pasta extraída:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 [opções]
```

| Opção | O que faz |
|---|---|
| `-Cpu` | Instala o PyTorch sem CUDA (economiza uns 5 GB; o app roda na CPU) |
| `-Cuda cu126` | Força a variante do PyTorch (`cu126` para placas antigas, `cu128`, `cu130`) |
| `-NoModels` | Não baixa os modelos agora (cada um é baixado no primeiro uso) |
| `-NoStart` | Não abre o app no fim |
| `-NoDesktopShortcut` | Não cria o atalho na área de trabalho |
| `-Prefix PASTA` | Instala em outra pasta (padrão: `%LOCALAPPDATA%\Programs\MangaOverlay`) |

As mesmas opções funcionam no `instalar-windows.cmd`, pelo Prompt de Comando: `instalar-windows.cmd -Cpu`.

</details>

### Linux, com o instalador

1. Baixe o **`MangaOverlay-vX.Y.Z-linux.tar.gz`** da [página de releases](https://github.com/GamosiDEV/MangaOverlay/releases/latest) (ou clone o repositório).
2. No terminal:

   ```bash
   tar -xzf MangaOverlay-*-linux.tar.gz
   cd MangaOverlay-*/
   ./install.sh
   ```

   Se faltar alguma biblioteca do sistema (em geral a `libxcb-cursor0`), o instalador a instala pelo `apt`, `dnf` ou `pacman` e pede a sua senha. O resto é instalado só para o seu usuário, sem `sudo`.
3. No fim, o app abre sozinho. Depois, abra pelo menu de aplicativos ou com o comando `mangaoverlay`.

No GNOME, o ícone da bandeja precisa da extensão **AppIndicator and KStatusNotifierItem Support** (já vem ativa no Ubuntu). Os atalhos de teclado funcionam mesmo sem o ícone.

<details>
<summary>Opções do instalador do Linux</summary>

| Opção | O que faz |
|---|---|
| `--cpu` | Instala o PyTorch sem CUDA (economiza uns 5 GB; o app roda na CPU) |
| `--cuda cu126` | Força a variante do PyTorch (`cu126` para placas antigas, `cu128`, `cu130`) |
| `--no-models` | Não baixa os modelos agora (cada um é baixado no primeiro uso) |
| `--no-start` | Não abre o app no fim |
| `--prefix PASTA` | Instala em outra pasta (padrão: `~/.local/share/MangaOverlay/app`) |

</details>

### Sem o instalador

Para rodar direto do código-fonte, com o seu próprio Python (3.12 ou mais novo).

**Linux:**

```bash
sudo apt install libxcb-cursor0          # Ubuntu/Debian; no Fedora e no Arch: xcb-util-cursor
python3 -m venv .venv
.venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv/bin/pip install -r requirements.txt -c constraints.txt
.venv/bin/python main.py
```

**Windows** (PowerShell, com o [Python](https://www.python.org/downloads/) instalado):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv\Scripts\pip install -r requirements.txt -c constraints.txt
.venv\Scripts\pythonw main.py
```

Qual PyTorch instalar:

| Placa de vídeo | Endereço do `--index-url` |
|---|---|
| NVIDIA GTX 16xx, RTX 20xx ou mais nova (inclusive RTX 50xx) | `https://download.pytorch.org/whl/cu128` |
| NVIDIA mais antiga (GTX 10xx, 9xx) | `https://download.pytorch.org/whl/cu126` |
| Sem NVIDIA | `https://download.pytorch.org/whl/cpu` (e desmarque "Usar a GPU" nas configurações) |

O `constraints.txt` fixa as versões testadas das bibliotecas; sem ele, o pip instala as mais novas. Os modelos (uns 4 GB) são baixados no primeiro uso, ou de uma vez com `python main.py --download-models`.

### Atualizar e desinstalar

**Atualizar:** baixe a versão nova e rode o instalador de novo. Ele fecha o app se estiver aberto, troca os arquivos e mantém obras, traduções salvas, configurações e chaves de API.

**Desinstalar:**

- **Windows:** Configurações → Aplicativos → **MangaOverlay** → Desinstalar. O desinstalador pergunta se deve apagar também as obras, as traduções salvas, as configurações e os modelos.
- **Linux:** `~/.local/share/MangaOverlay/app/uninstall.sh`. Para apagar também obras, traduções, configurações, chaves de API e modelos: `uninstall.sh --purge`.

## Primeiros passos

1. **Abra o app.** Ele fica na bandeja; na primeira vez, carrega os modelos (alguns segundos).
2. **Escolha o idioma de origem** no menu do ícone da bandeja (japonês, coreano, chinês ou inglês). O destino padrão é português.
3. **Abra uma página de mangá** em qualquer leitor e aperte **Ctrl+Alt+M**. Um pontinho azul no canto indica que está traduzindo; em seguida, a tradução aparece por cima dos balões.
4. **Ctrl+Alt+N** esconde a tradução. Para a próxima página, aperte Ctrl+Alt+M de novo.

O motor padrão é o **offline (NLLB)**: grátis, sem conta e sem internet, com qualidade razoável. Para traduções bem melhores, use a OpenAI ou o Claude:

1. Crie uma chave de API na [OpenAI](https://platform.openai.com/api-keys) ou na [Anthropic](https://console.anthropic.com/settings/keys).
2. No menu da bandeja, abra **Configurações**, cole a chave e escolha o motor. A chave fica guardada no chaveiro do sistema (Gerenciador de Credenciais no Windows, GNOME Keyring/KWallet no Linux).

Cada capítulo custa centavos. Toda tradução fica salva, então voltar a uma página já traduzida não gera nova cobrança.

**Dica:** se for ler uma série inteira, crie uma **obra** no menu (Obra → Nova obra). Cada obra guarda as próprias traduções, a lista de personagens e um glossário, para os nomes e termos saírem sempre iguais.

Os atalhos podem ser trocados em Configurações.

## Recursos

### Obras e traduções salvas

No menu da bandeja, **Obra** escolhe o mangá que você está lendo (ou cria um novo). Cada obra guarda as próprias traduções e lembra o idioma de origem. Toda tradução fica salva em disco: voltar a uma página, mesmo depois de fechar o app, mostra a tradução na hora e não gera nova cobrança na API. Com "Nenhuma", as traduções também ficam salvas, sem obra associada.

**Só traduções salvas:** marcando "Só traduções salvas (nunca traduzir de novo)" no menu, o atalho de traduzir mostra apenas o que já está no banco (de qualquer motor ou modelo) e nunca chama tradutor nem API. Falas sem tradução salva ficam com o original visível e um contorno laranja tracejado; uma notificação diz quantas vieram do banco e quantas faltam.

"Esquecer as traduções desta obra…" apaga as traduções salvas da obra atual (pede confirmação).

A busca no banco é primeiro pelo texto exato e, se não achar, por semelhança (80% ou mais, em falas com 4 caracteres ou mais), porque o OCR às vezes lê um risco do desenho como um caractere a mais.

### Colorização

**Ctrl+Alt+C** mostra a página de mangá da tela colorida. Para colorir junto com a tradução, marque "Colorir junto com a tradução" no menu.

As cores são uma interpretação do modelo: podem variar de uma página para outra (o cabelo de um personagem pode mudar de cor) e ficam mais fracas em cenários complexos. Só a cor muda: traço, retículas e texto ficam exatamente como estavam. É preciso haver balões na tela, porque é a partir deles que o app encontra a página.

### Importar capítulos

"Importar capítulos…" no menu da bandeja lê capítulos inteiros de uma vez, para a obra atual:

- **Pasta de imagens:** um capítulo.
- **Pasta com subpastas, CBZs ou PDFs:** vários capítulos de uma vez, ordenados pelo número no nome ("Cap 2" antes de "Cap 10").
- **Arquivos CBZ, ZIP ou PDF:** um capítulo cada.

A detecção dos balões e o OCR rodam no seu computador, sem custo de API; o texto de cada balão fica no banco para a tradução em lote. As imagens não são copiadas: o banco guarda só onde está cada página. Uma janela mostra o progresso, e o atalho de traduzir continua funcionando durante a importação (tem prioridade).

Se o app fechar no meio, nada se perde: ao abrir de novo, a importação continua das páginas que faltavam ("Retomar importação" no menu também faz isso). Capítulos já importados na mesma obra são ignorados se escolhidos de novo.

### Traduzir capítulos em lote

"Traduzir capítulos…" no menu da bandeja traduz de uma vez os capítulos importados da obra atual, para depois ler na tela com a tradução aparecendo na hora, sem nova cobrança.

- Escolha os capítulos (os já traduzidos vêm desmarcados) e quantas páginas vão em cada pedido à API (padrão 20). O app mostra o máximo seguro para o modelo, calculado pelo limite de saída dele e pelo tamanho das páginas escolhidas.
- Antes de enviar aparecem as falas a traduzir e o custo estimado; no fim, o custo real informado pela API.
- Falas repetidas (はい, え?!…) vão uma vez só, e falas já traduzidas nunca são enviadas de novo.
- Erros temporários (limite de requisições, servidor, rede) são tentados de novo sozinhos; se persistirem, o lote pausa em vez de desperdiçar pedidos. Erros definitivos (chave inválida, sem crédito) pausam com a mensagem. Falas que o modelo esquecer de devolver são pedidas de novo, só elas.
- Fechar o app no meio não perde nada: o lote continua sozinho ao abrir de novo. "Pausar" deixa o lote parado até "Retomar tradução em lote" no menu (que também tenta de novo os blocos que falharam).

**Batch API da OpenAI:** com os motores da OpenAI, a tela oferece "Batch API" como modo de envio, com **50% de desconto**. O app envia todos os pedidos de uma vez e a OpenAI processa em segundo plano (em geral em minutos, com garantia de até 24 horas). Pode fechar o app ou desligar o computador: o id do lote fica no banco e o app confere o andamento a cada minuto quando está aberto. Quando termina, as traduções são gravadas e, se o modelo tiver pulado alguma fala, um novo envio leva só as que faltaram (até 2 vezes). "Pausar" cancela o lote na OpenAI guardando o que já tinha voltado; "Retomar" envia só o resto.

O lote sempre usa o modo texto (o texto já foi lido pelo OCR local). Motores: OpenAI, Claude, NLLB offline (grátis) e Google gratuito. Na leitura, qualquer tradução já paga com IA da obra é usada, mesmo que você esteja lendo com outro motor ou modelo.

### Personagens da obra

"Personagens da obra…" no menu da bandeja abre a lista de personagens da obra atual. Ela vai junto em toda tradução com IA (OpenAI ou Claude) daquela obra, para que cada personagem tenha sempre o mesmo nome, gênero e jeito de falar. O NLLB e o Google gratuito não usam a lista.

- **Manual:** só o nome como você quer ver na tradução é obrigatório; o nome original, o gênero, o jeito de falar e as notas são opcionais. Não é preciso digitar em japonês: a coluna "Encontrados no texto" mostra nomes achados no texto já lido (pelos tratamentos como さん, ちゃん, 先生, 씨, 선배 e por palavras em katakana); duplo clique adiciona.
- **Levantamento rápido:** a IA lê os 3 primeiros capítulos importados ainda não analisados, com o modelo principal, e sugere os personagens.
- **Levantamento completo:** lê todos os capítulos importados ainda não analisados, com um modelo barato (padrão `gpt-5.4-nano` ou `claude-haiku-4-5`).

Os dois levantamentos mostram o custo estimado antes de enviar e trazem as sugestões destacadas para revisão; nada é salvo sem clicar em Salvar. Capítulos já analisados não são lidos de novo nos próximos levantamentos.

### Revisar nomes

"Revisar nomes nas traduções…" no menu corrige nomes de personagens nas traduções já salvas da obra, usando a lista de personagens. Nada é gravado sem aparecer antes numa lista (original, antes, depois e motivo), onde dá para desmarcar o que não quiser.

- **Grátis:** se o original da fala cita o personagem (春斗) e a tradução traz uma grafia parecida e errada ("Harutou-kun"), troca pelo nome da lista mantendo o tratamento ("Haruto-kun"). Palavras comuns parecidas com nomes (Minha × Mina) nunca são trocadas.
- **Com IA:** falas que citam o personagem no original, mas em que o nome não aparece na tradução (o modelo usou um pronome, por exemplo), vão para o modelo principal, com o custo mostrado antes. Dá para revisar a obra inteira também.
- **Mudou a grafia na lista:** ao salvar a lista de personagens com um nome alterado, a mesma tela abre com a troca proposta em todas as traduções já feitas, sem retraduzir nada.

### Memória da obra

Além da lista de personagens, cada obra tem uma memória que vai junto em toda tradução com IA:

- **Glossário:** a cada tradução, o modelo informa os termos novos que encontrou (lugares, grupos, técnicas, bordões) e como os traduziu. Eles passam a ser usados sempre do mesmo jeito. Até 80 termos, os mais frequentes.
- **Resumo da história:** atualizado quando um capítulo termina de ser traduzido em lote, com o modelo barato (`gpt-5.4-nano` ou `claude-haiku-4-5`), só com o texto já traduzido; cerca de US$ 0,001 por capítulo. Pode ser desligado em Configurações.

A memória só muda em pontos fixos (fim de capítulo no lote, ou a cada 15 termos novos na leitura pela tela). Assim o começo dos pedidos fica idêntico durante o capítulo e o cache de prompt funciona: nos testes com a OpenAI, ~90% da entrada veio do cache (que custa 25% do preço no `gpt-4.1-mini`). "Memória da obra…" no menu mostra o resumo e o glossário e permite apagá-los.

Na Batch API, o **modo híbrido** traduz o 1º capítulo na hora para montar a memória e envia o resto à OpenAI já com ela.

## Motores de tradução

| Motor | Precisa de | Observação |
|---|---|---|
| OCR local + tradução offline (NLLB) | nada (GPU acelera) | Padrão. Grátis e offline; qualidade razoável. O NLLB tem licença CC-BY-NC (uso pessoal). |
| OCR local + Google Tradutor gratuito | internet | Endpoint não oficial; o Google bloqueia com frequência (erro 429). |
| OCR local + OpenAI GPT / Claude (texto) | chave de API | Tradução bem melhor, com contexto das páginas anteriores. Custa centavos por capítulo. |
| OpenAI GPT / Claude lê a imagem e traduz | chave de API | Melhor com textos difíceis (estilizados, sobre o desenho). Aceita origem "Detectar". |

As chaves ficam no chaveiro do sistema (Configurações, no ícone da bandeja) ou nas variáveis `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. O Claude usa por padrão o `claude-opus-5` com esforço baixo, para responder rápido, e o fallback do servidor, que evita que uma recusa indevida deixe a página sem tradução. `claude-sonnet-5` e `claude-haiku-4-5` são mais baratos e mais rápidos.

## Solução de problemas

**O atalho não faz nada.**
- Windows: outro programa pode estar usando Ctrl+Alt+M. Troque o atalho em Configurações. Com o teclado ABNT2, Ctrl+Alt equivale ao AltGr; se isso atrapalhar a digitação, escolha outro atalho.
- GNOME/Wayland: o app não pode escutar o teclado, então registra os atalhos em Configurações → Teclado → Atalhos personalizados ("MangaOverlay: traduzir a tela" e os outros). Confira se estão lá.
- Outros ambientes Wayland (KDE, Hyprland…): crie os atalhos à mão nas configurações do sistema, com os comandos `mangaoverlay --translate`, `--colorize` e `--hide`.

**A tradução não aparece por cima da janela (Linux/Wayland).** Falta a biblioteca `libxcb-cursor0` (rode o `install.sh` de novo, ou `sudo apt install libxcb-cursor0`). A interface roda via XWayland, o único jeito de uma janela ficar por cima das outras no GNOME Wayland.

**Na primeira vez, o GNOME pergunta se o app pode capturar a tela.** É o portal do sistema; permita.

**"Nenhum texto para traduzir encontrado na tela."** Confira o idioma de origem no menu. Textos fora dos balões (narração solta, onomatopeias) vêm desligados por padrão; dá para ligar em Configurações.

**Está lento.** Veja em Configurações se "Usar a GPU" está marcado. Se estiver e continuar lento, o PyTorch pode não estar enxergando a placa (driver da NVIDIA antigo): atualize o driver e rode o instalador de novo. O instalador avisa quando isso acontece.

**O app fechou ou deu erro no Windows.** Aberto pelo atalho, o app não tem janela de console; as mensagens vão para `%LOCALAPPDATA%\MangaOverlay\Logs\mangaoverlay.log`.

## Linha de comando

No Linux instalado, o comando é `mangaoverlay`; sem o instalador, `python main.py`. No Windows: `%LOCALAPPDATA%\Programs\MangaOverlay\.venv\Scripts\python.exe main.py`.

```
mangaoverlay                  # inicia na bandeja
mangaoverlay --translate      # traduz a tela agora
mangaoverlay --colorize       # mostra a página colorida
mangaoverlay --hide           # esconde tudo
mangaoverlay --download-models  # baixa todos os modelos de uma vez
mangaoverlay --image pagina.png [--out saida.png] [--engine local] [--source ja] [--color] [--no-translate]
                              # traduz e/ou colore um arquivo e grava o resultado (para testes)
```

Com o app aberto, `--translate`, `--colorize` e `--hide` só avisam a instância em execução e saem na hora.

## Onde ficam os arquivos

| | Linux | Windows |
|---|---|---|
| App (instalador) | `~/.local/share/MangaOverlay/app` | `%LOCALAPPDATA%\Programs\MangaOverlay` |
| Configurações | `~/.config/MangaOverlay/config.json` | `%LOCALAPPDATA%\MangaOverlay\config.json` |
| Obras e traduções (SQLite) | `~/.local/share/MangaOverlay/mangaoverlay.db` | `%LOCALAPPDATA%\MangaOverlay\mangaoverlay.db` |
| Modelos | `~/.cache/huggingface` e `~/.cache/MangaOverlay/easyocr` | `%USERPROFILE%\.cache\huggingface` e `%LOCALAPPDATA%\MangaOverlay\Cache\easyocr` |
| Log (sem console) | — | `%LOCALAPPDATA%\MangaOverlay\Logs\mangaoverlay.log` |

## Como funciona

1. **Captura:** um print de cada monitor (no GNOME/Wayland, pelo portal do sistema).
2. **Detecção:** o modelo [comic-text-and-bubble-detector](https://huggingface.co/ogkalu/comic-text-and-bubble-detector) (RT-DETR-v2, Apache-2.0) encontra balões e textos. Só vale o texto dentro de um balão com alta confiança, para não traduzir menus e botões.
3. **Leitura (OCR):** [manga-ocr](https://huggingface.co/kha-white/manga-ocr-base) para japonês, que lê texto vertical; EasyOCR para coreano, chinês e inglês.
4. **Tradução:** primeiro o app procura no banco local uma tradução já feita para o mesmo texto; só o que falta vai para o motor escolhido. A busca é pelo texto lido, então a página é reconhecida mesmo com outro zoom ou em outra posição da tela.
5. **Desenho:** cobre o texto original sem apagar o contorno do balão e encaixa a tradução no maior tamanho de fonte que couber.

Com os modelos carregados, uma tela 2560×1600 leva cerca de 0,2 s numa RTX 5050 com o tradutor offline.

**Colorização:** a partir dos balões detectados, o app expande um retângulo até encontrar a borda da página (faixa uniforme escura do leitor, ou elementos coloridos da interface), então menus e outras janelas não são coloridos. O modelo [manga-colorization-v2](https://github.com/qweasdd/manga-colorization-v2), exportado em ONNX e convertido para PyTorch, roda na página reduzida (cerca de 0,2 s na RTX 5050). Da saída do modelo só se aproveita a cor; o brilho vem da captura original em resolução total. O repositório original do modelo não declara licença; o espelho usado (`ifritraen/manga-colorization-v2-fp32`) declara Apache-2.0. Trate como uso pessoal.

**Instalador:** o [uv](https://github.com/astral-sh/uv) baixa um Python 3.12 próprio para a pasta do app e cria o ambiente; o PyTorch vem do índice oficial na variante da placa (cu128 para compute capability 7.5 ou mais, cu126 para placas mais antigas, CPU sem NVIDIA); as outras bibliotecas vêm nas versões do `constraints.txt`. A cada release, uma CI instala do zero no Windows e no Ubuntu, traduz uma página de teste e desinstala.

## Limitações conhecidas

- Modo atual: **sob demanda** (aperte o atalho a cada página). Se a página rolar, a tradução e a cor ficam no lugar antigo até o próximo atalho.
- Com OCR local, o idioma de origem precisa estar certo no menu da bandeja. "Detectar" só funciona nos motores em que o LLM lê a imagem.
- Textos fora dos balões vêm desligados por padrão, porque o detector os confunde com textos de sites e menus.
- Não há versão para macOS.

## Próximos passos

1. **Tempo real** (opção secundária nas configurações): transmissão contínua da tela, detecção de mudança (troca de página, rolagem) e nova tradução/colorização automática.

## Licença

[MIT](LICENSE). Os modelos têm licenças próprias: o NLLB é CC-BY-NC 4.0 (uso não comercial) e o colorizador não declara licença no repositório original.
