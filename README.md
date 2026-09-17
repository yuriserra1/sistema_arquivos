# Sistema de arquivos baseado em i-nodes

Simulador didático em Python, com disco de 128 MiB, blocos de 2.048 bytes, arquivos, diretórios, links simbólicos e persistência. O programa mantém a organização em funções e dicionários, com bibliotecas padrão do Python.

## Executar

Recomendado: Python 3.10 ou superior. Verificado com Python 3.12.

Extraia o ZIP, abra o terminal na pasta `sistema_de_arquivos` e execute:

```bash
python main.py
```

No Linux, se necessário, use `python3 main.py`. Não é preciso instalar pacotes.

O terminal mostra `myfs>`. Digite `exit` para sair. `Ctrl+C` e fim da entrada também encerram o programa. A imagem `filesystem.img` é criada na pasta de execução; use sempre essa mesma pasta para reencontrar os dados. Os caminhos digitados no simulador pertencem ao disco virtual, não ao sistema de arquivos do computador.

## Arquivos do projeto

| Arquivo | Finalidade |
| --- | --- |
| `main.py` | Implementação e terminal interativo |
| `README.md` | Instruções de uso e explicação da arquitetura |
| `CORRECOES.md` | Mudanças realizadas e validação |
| `testes.py` | Testes automatizados com discos temporários |

## Disco e gerenciamento

| Estrutura | Configuração |
| --- | --- |
| Capacidade da imagem | `128 * 1024 * 1024` = 134.217.728 bytes (128 MiB) |
| Tamanho do bloco | 2.048 bytes |
| Total de blocos | 65.536 |
| Área de metadados | Primeiros 2.048 blocos = 4 MiB |
| Área de dados | 63.488 blocos = 124 MiB |
| Tabela de i-nodes | Até 8.192 posições |
| Apontadores diretos por i-node | 12 |
| Comprimento máximo de nome novo | 255 bytes em UTF-8 |
| Limite de expansões de links em um caminho | 40 |

`INICIO_DADOS` é o índice do primeiro bloco de dados. Para acessar um bloco, o deslocamento em bytes é `numero_do_bloco * TAM_BLOCO`.

Os mapas `bitmap_inode` e `bitmap_bloco` são listas: `0` significa livre e `1`, ocupado. Não são mapas compactados em bits. I-nodes e mapas são serializados com `pickle` dentro da imagem. Os primeiros oito bytes indicam o comprimento dessa serialização. O programa verifica se o cabeçalho e os metadados cabem na área reservada antes de gravá-los.

A tabela foi ampliada de 1.024 para 8.192 i-nodes para remover o antigo teto de 24 MiB de blocos endereçáveis. Os 124 MiB de dados são compartilhados por arquivos, diretórios e links. Muitos arquivos pequenos ainda podem esgotar os i-nodes antes dos blocos. A raiz também ocupa dados: no teste com um único arquivo grande, ele armazenou 130.021.376 bytes, restando um bloco para a raiz.

## Estrutura dos i-nodes

Cada i-node contém nome, tipo, criador, dono, tamanho em bytes, data de criação, data de modificação, permissões, 12 apontadores de blocos e o apontador `proximo`.

Os 12 blocos diretos armazenam até 24 KiB. Se o conteúdo ultrapassar esse tamanho, o programa reserva outro i-node e o encadeia por `proximo`. O valor `-1` representa um apontador não utilizado. O tamanho total fica no i-node principal; os demais continuam seus blocos.

Criador e dono são `usuario`. As seis permissões `rwxr-x` representam leitura, escrita e execução para dono e demais usuários. **São metadados nesta versão:** não há autenticação, troca de usuário nem aplicação de permissões. Essa limitação é explícita; o enunciado fornecido exige os campos, mas não especifica esses comandos adicionais.

## Comandos de arquivos

| Comando | Comportamento |
| --- | --- |
| `touch arquivo` | Cria arquivo vazio; se a entrada existir, atualiza a data de modificação |
| `rm arquivo` | Remove arquivo ou o próprio link, preservando seu alvo |
| `echo "texto" > arquivo` | Cria arquivo ou substitui seu conteúdo |
| `echo "texto" >> arquivo` | Acrescenta conteúdo, criando o arquivo se necessário |
| `cat arquivo` | Mostra o texto do arquivo |
| `cp origem destino` | Copia o conteúdo; se destino for diretório, usa o nome da origem |
| `mv origem destino` | Move ou renomeia, mantendo o i-node principal |
| `ln -s alvo link` | Cria um link simbólico que armazena o caminho do alvo |

No `echo`, o texto deve ser um único argumento: use aspas quando houver espaços. `>` e `>>` funcionam com ou sem espaços ao redor. O conteúdo entre aspas pode conter esses símbolos. O `echo` não acrescenta quebra de linha automática; `cat` acrescenta uma quebra apenas na exibição.

`cp` pode substituir o conteúdo de um arquivo de destino, mas recusa copiar sobre o mesmo i-node, inclusive por link. `mv` recusa destino já ocupado, evitando sobrescrita acidental. Não há cópia recursiva de diretórios nem opções adicionais de shell.

## Comandos de diretórios

| Comando | Comportamento |
| --- | --- |
| `mkdir diretorio` | Cria um diretório; o pai deve existir |
| `rmdir diretorio` | Remove somente se estiver vazio e não for a raiz ou o diretório atual |
| `ls diretorio` | Lista as entradas; sem argumento, usa o diretório atual |
| `cd diretorio` | Altera o diretório atual |
| `mv origem destino` | Move ou renomeia diretório e atualiza seu `..` |
| `ln -s alvo link` | Também cria links para diretórios |

Todo diretório guarda uma tabela de nomes e números de i-node em seus blocos. `.` aponta para ele próprio; `..`, para seu pai. Na raiz, ambos apontam para 0. Essas entradas aparecem em `ls`, não impedem a remoção de um diretório vazio e não podem ser removidas ou movidas diretamente.

Não se pode mover um diretório para dentro dele mesmo ou de seus descendentes, inclusive por um link de destino.

## Caminhos e links

Todos os comandos aceitam caminhos absolutos, como `/pasta/arquivo`, e relativos, como `arquivo`, `./arquivo` e `../arquivo`. Diretórios intermediários devem existir. Nomes com espaços precisam de aspas.

O alvo relativo de um link é resolvido a partir do diretório que contém o link. O programa não precisa alterar o diretório atual para isso. Links intermediários são seguidos. `rm` e a origem de `mv` operam sobre o próprio link final.

Links podem apontar para alvos ainda inexistentes. `touch` e `echo` criam o arquivo de destino ausente, inclusive através desses links, desde que seu diretório pai exista. `cp` e `mv` reconhecem um link para diretório como diretório de destino. Ciclos e cadeias acima de 40 expansões são recusados com mensagem de erro.

Uma barra final exige diretório nas operações de acesso. Por proteção, `rm link/`, `rmdir link/` e `mv link/ destino` são recusados quando a última entrada é um link. Para remover o link, use `rm link`.

## Persistência e tratamento de erros

Cada comando de alteração usa `transacao()`:

1. Guarda uma cópia dos i-nodes e mapas em memória.
2. Prepara os blocos alterados em um dicionário, sem escrever na imagem. As leituras do mesmo comando enxergam os blocos preparados.
3. Se houver erro de preparação, restaura os metadados e descarta esses blocos.
4. Se tudo terminar e os metadados couberem na área reservada, grava blocos e metadados, executando `flush()` e `fsync()`.

`escrever()` também verifica antecipadamente a quantidade de blocos e i-nodes necessária, incluindo os recursos que o próprio arquivo pode reutilizar. Isso evita perder o conteúdo antigo por falta de espaço. Uma falha depois de preparar parte de uma criação ou movimento também desfaz o comando completo.

`cat`, `ls` e `cd` não regravam metadados. Comandos de alteração concluídos já ficam persistidos; `exit` não precisa salvar novamente. Ao reabrir, o diretório atual começa na raiz.

**Limite da proteção:** essa transação protege contra erros durante a preparação e validação. Não implementa journal nem recuperação automática de queda de energia, encerramento forçado ou falha de E/S durante a gravação final. Falhas reais de E/S encerram a sessão. O mecanismo também usa memória proporcional aos blocos alterados; `echo >>` ainda relê e prepara o conteúdo completo para manter o algoritmo simples. Use uma única instância por imagem.

Imagens saudáveis da versão anterior, com 1.024 i-nodes, podem ser abertas. A tabela é ampliada em memória e persistida no próximo comando de alteração. Essa compatibilidade não repara imagens já corrompidas. Como a imagem usa `pickle`, abra apenas imagens produzidas por este projeto e de procedência conhecida.


