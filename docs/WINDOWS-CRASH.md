# Crash no Windows ao traduzir a tela (0xC0000374) — resolvido

Registro do problema que impediu a release `v0.1.0`, da causa e de como verificar, para o caso de algo parecido voltar. Investigado entre 29/09/2026 e 30/09/2026: primeiro nas máquinas Windows do GitHub Actions (sem GPU), depois resolvido num Windows 11 com GTX 1050 Ti.

## Sintoma

O app fechava sozinho, sem mensagem, logo depois de traduzir a tela (atalho Ctrl+Alt+M ou `--translate`), em geral na primeira tradução depois de abrir. Código de saída `0xC0000374` (corrupção de heap) e, às vezes, `0xC0000005` (violação de acesso), com a pilha parando no pré-processamento de imagem do detector (`torchvision.transforms.functional.pil_to_tensor`).

- Acontecia com GPU e na CPU, em quase todas as tentativas.
- O `--image` nunca quebrava, com o mesmo pipeline e os mesmos modelos.
- No Linux não aparecia (o heap do glibc não detecta esse tipo de corrupção como o do Windows).

## Causa

O app rodava os modelos num `QThreadPool` do Qt, **uma tarefa por vez, cada uma separada**: o aquecimento dos modelos era uma tarefa, cada tradução da tela outra.

A thread do `QThreadPool` não é uma thread do Python. A cada tarefa, o PySide cria um estado de thread do Python (`PyThreadState`) e o destrói no fim. O PyTorch, pelo pybind11, guarda um ponteiro para esse estado numa variável local da thread (TLS). Na tarefa seguinte, na mesma thread do sistema, o ponteiro aponta para memória já liberada, e o PyTorch corrompe o heap ao usá-lo.

Por isso quebrava sempre na **segunda tarefa** que usava os modelos na mesma thread do pool:

| Situação | Tarefas na mesma thread do pool | Resultado |
|---|---|---|
| App normal | aquecimento, depois 1ª tradução | crash na 1ª tradução |
| App sem aquecimento | 1ª tradução, depois 2ª | crash na 2ª tradução |
| Script com o laço inteiro numa tarefa só | uma | nunca quebra |
| `--image` | nenhuma (thread principal) | nunca quebra |

A importação de capítulos e a tradução em lote não têm o problema: rodam em `threading.Thread`, e o estado de thread do Python dura a vida inteira da thread.

## Correção

`mangaoverlay/app.py`: o `QThreadPool` foi trocado por `_Worker`, uma `threading.Thread` do Python permanente que executa as tarefas de uma fila, uma por vez. O resultado volta para a thread principal por um sinal do próprio app (`_job_finished`), sem objeto Qt por tarefa.

**Regra para o futuro:** código que usa o PyTorch (ou outra extensão com pybind11) não deve rodar em `QThreadPool`/`QRunnable`. Use `threading.Thread`.

## Verificação (Windows 11, GTX 1050 Ti, PyTorch 2.11 cu126)

| Teste | Antes | Depois |
|---|---|---|
| `tests/windows/app_ciclo.py` (app completo, 6 traduções), GPU | crash 3/3 | 18 traduções em 3 execuções, sem crash |
| O mesmo, na CPU | crash | 6 traduções, sem crash |
| `tests/windows/reproduzir-crash.ps1` (app real, captura da tela, atalho físico, pausas de 30 s) | crash 3/3 rodadas | 9 traduções em 3 rodadas, sem crash |
| Importação (`Importer`) de um capítulo de 56 páginas e tradução em lote com o NLLB (`BatchRunner`), GPU | — | 56 páginas, 139 falas lidas, 6/6 blocos traduzidos, sem crash |

## Como verificar de novo

Da pasta do repositório, com o Python do ambiente instalado:

```powershell
$PY = "$env:LOCALAPPDATA\Programs\MangaOverlay\.venv\Scripts\python.exe"
& $PY tests\windows\app_ciclo.py --imagem tests\fixtures\pagina-sintetica.png          # rápido, sem mexer no teclado
powershell -ExecutionPolicy Bypass -File tests\windows\reproduzir-crash.ps1 -Main .\main.py   # app real, aperta o atalho sozinho
```

`app_ciclo.py` abre o app de verdade e traduz em ciclos; as opções `--sem-aquecimento`, `--sem-atalhos`, `--sem-sobreposicao`, `--sem-janela`, `--sem-banco`, `--sem-lote`, `--sem-ipc` e `--cpu` desligam partes do app, para isolar um problema novo. `pipeline_em_thread.py` roda só o pipeline, na thread principal, numa `threading.Thread`, com o Qt rodando ou num `QThreadPool`.

Num crash nativo, o `faulthandler` (ligado no app) grava a pilha de todas as threads em `%LOCALAPPDATA%\MangaOverlay\Logs\mangaoverlay.log`.

## Pistas que não eram a causa

Descartadas durante a investigação, cada uma com teste próprio: o `pynput` (atalhos), a IPC por named pipe, a janela da sobreposição, a captura de tela, a ordem de importação do Qt e do PyTorch (o PySide6 traz o runtime do Visual C++ 14.44 e o Windows tem o 14.51), o cache de blocos do Pillow, o banco SQLite, a bandeja e as notificações, a expiração da thread ociosa do pool (`setExpiryTimeout(-1)` não resolveu) e o `autoDelete` do `QRunnable`.
