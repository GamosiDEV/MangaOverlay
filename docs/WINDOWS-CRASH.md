# Crash no Windows ao traduzir a tela (0xC0000374)

Nota de 29/09/2026 para continuar a investigação num Windows de verdade. Os testes foram feitos em máquinas Windows do GitHub Actions (Windows Server, sem GPU, PyTorch na CPU) e interrompidos para não gastar mais minutos da CI.

**Situação:** a release `v0.1.0` não saiu. A tag `v0.1.0` existe no GitHub, mas aponta para um commit com o bug, e o job do Windows no workflow `Instalador` barrou a publicação. Antes de publicar: resolver o crash, apagar a tag (`git push origin :refs/tags/v0.1.0` e `git tag -d v0.1.0`) e criá-la de novo no commit corrigido.

## Sintoma

O app fecha sozinho, sem mensagem, pouco depois de traduzir a tela (atalho Ctrl+Alt+M ou `--translate`). Código de saída `0xC0000374` (corrupção de heap); uma vez, `0xC0000005` (violação de acesso).

- Acontecia em quase todas as rodadas (atalho a cada 30 s).
- Na primeira execução da CI passou uma vez: é intermitente.
- O `--image` (traduzir um arquivo pela linha de comando) **nunca** quebrou, com o mesmo pipeline e os mesmos modelos.
- No Linux não aparece. O heap do Windows detecta a corrupção e aborta; o glibc em geral não percebe.

Com o `faulthandler` ligado (já no código), o log mostra a pilha de todas as threads no momento do crash. Quase sempre nenhuma thread estava executando Python do app. Numa vez, a violação de acesso veio dentro do `torchvision` (pré-processamento de imagem do detector) na thread de trabalho, ou seja, a memória já estava corrompida antes.

## O que já foi descartado

Cada linha é uma variante rodada na CI, com 3 aberturas do app e 3 traduções em cada:

| Variante | Resultado | Conclusão |
|---|---|---|
| App aberto, sem traduzir nada | sem crash | Carregar os modelos não basta |
| Sem os atalhos do `pynput` | crash 3/3 | Não é o `pynput` |
| Só comandos `--hide` pela IPC, sem traduzir | sem crash | Não é a IPC (named pipe) |
| Traduzir pelo atalho, sem IPC | crash 3/3 | Não é a IPC |
| Sem a sobreposição (nada desenhado na tela) | crash 2/3 | Não é a sobreposição |
| Captura trocada por um PNG | crash 3/3 | Não é a captura de tela |
| Tudo igual, mas **sem rodar o pipeline** | sem crash (9 traduções) | **É o pipeline (modelos) no app** |
| Tarefas passadas como função em vez de subclasse de `QRunnable` | crash 5/6 | Não era o `autoDelete` do `QRunnable` (a mudança ficou, é inofensiva) |
| Thread do pool que nunca expira (`setExpiryTimeout(-1)`), modelos sempre na mesma thread | crash 6/6, já na 1ª tradução | Não é a troca de thread entre tarefas |

## Onde está o problema

O pipeline (detecção, OCR e NLLB, no PyTorch da CPU) **quebra quando roda na thread de trabalho do app** (`QThreadPool`, com o loop de eventos do Qt rodando na thread principal) e **nunca quebrou na thread principal** (`--image`). Na última variante, o app quebrava já na primeira tradução, antes de terminar de processar a tela.

Não se sabe ainda se a causa é:

- **qualquer thread que não seja a principal**, até uma `threading.Thread` pura, sem Qt;
- **o Qt rodando ao mesmo tempo** (o loop de eventos na principal enquanto o PyTorch trabalha na outra);
- **conflito entre bibliotecas OpenMP** (a do PyTorch e as que vêm com o OpenCV, o SciPy e o ONNX), que só aparece com mais de uma thread chamando essas bibliotecas.

## Como testar no Windows

Instale pelo `instalar-windows.cmd` deste branch (ou use um ambiente próprio, veja o README). Os comandos abaixo rodam da pasta do repositório; `PY` é o Python do ambiente instalado:

```powershell
$PY = "$env:LOCALAPPDATA\Programs\MangaOverlay\.venv\Scripts\python.exe"
```

### 1. Diagnóstico rápido, sem o app (alguns minutos)

`tests\windows\pipeline_em_thread.py` roda o pipeline na página de teste 6 vezes, no modo escolhido. Rode cada modo e anote qual quebra (o processo fecha com `0xC0000374` e o `faulthandler` mostra as pilhas):

```powershell
& $PY tests\windows\pipeline_em_thread.py principal --cpu    # como o --image: não deve quebrar
& $PY tests\windows\pipeline_em_thread.py thread --cpu       # threading.Thread, sem Qt
& $PY tests\windows\pipeline_em_thread.py qt-thread --cpu    # threading.Thread, com o Qt rodando
& $PY tests\windows\pipeline_em_thread.py pool --cpu         # QThreadPool, como o app
& $PY tests\windows\pipeline_em_thread.py pool --cpu --uma-thread   # idem, com torch.set_num_threads(1)
```

Como ler o resultado:

| Quebra em | Não quebra em | Provável causa | Correção |
|---|---|---|---|
| `thread`, `qt-thread` e `pool` | `principal` | PyTorch fora da thread principal | Rodar os modelos na thread principal, ou num processo separado |
| `qt-thread` e `pool` | `principal` e `thread` | Qt e PyTorch ao mesmo tempo | Mesma do anterior, ou isolar o PyTorch num processo |
| `pool` | `pool --uma-thread` | Conflito de OpenMP | `torch.set_num_threads(1)` ou `OMP_NUM_THREADS=1` no Windows (medir a perda de velocidade) |
| nenhum | | O crash depende de algo do app que o script não tem (captura, sobreposição, bandeja) | Voltar ao script do item 2 e desligar partes do app |

Tire `--cpu` para testar também com a GPU NVIDIA. A CI só testou a CPU, e o crash pode nem acontecer com CUDA.

### 2. Reproduzir no app

`tests\windows\reproduzir-crash.ps1` abre a página de teste no Paint, abre o app, aperta Ctrl+Alt+M 3 vezes com 30 s de pausa, repete 3 vezes e mostra o log de cada rodada:

```powershell
powershell -ExecutionPolicy Bypass -File tests\windows\reproduzir-crash.ps1
```

Parâmetros: `-Rodadas`, `-Traducoes`, `-Pausa` (segundos), `-Aquecimento`; `-Python` e `-Main` para usar um ambiente próprio em vez do instalado. Use-o para confirmar a correção: umas 5 rodadas (`-Rodadas 5`) sem crash, porque o erro é intermitente.

### Logs

- App: `%LOCALAPPDATA%\MangaOverlay\Logs\mangaoverlay.log` (inclui a pilha do `faulthandler` num crash nativo e uma linha por tela processada).
- Rodar com `python.exe` no lugar de `pythonw.exe` mostra a saída direto no console.

### Se nada disso achar a causa

Ligue o *page heap* do Windows para o Python do ambiente, com o `gflags` do Debugging Tools for Windows (`gflags /p /enable python.exe /full`). O crash passa a acontecer na instrução que escreve fora do lugar, não quando a corrupção é detectada, e um dump no WinDbg mostra a biblioteca culpada. Vale também testar o PyTorch 2.10 e o Python 3.13 (o instalador usa 2.11 e 3.12).

### Depois de corrigir

A importação de capítulos (`Importer`) e a tradução em lote com o NLLB (`BatchRunner`) também usam os modelos fora da thread principal (em `threading.Thread`). Teste as duas no Windows: importe alguns capítulos e rode um lote com o motor offline. Se a correção for "modelos numa thread só", o ideal é uma thread (ou processo) dedicada a tudo que usa o PyTorch, recebendo tarefas por uma fila.

Com o crash resolvido, o job do Windows no workflow `Instalador` volta a passar, e a release pode sair (veja **Situação**, no começo).

## O que já está pronto neste branch

Independente do crash:

- Log detalhado da tradução em lote ("Mostrar detalhes" na janela do lote, arquivo `traducao-em-lote.log`), testado no Linux com erros simulados.
- `faulthandler` ligado e uma linha no log do app por tela processada ou falha.
- Falha no resumo da memória da obra passa a aparecer no log do lote.
