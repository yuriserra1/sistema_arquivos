# Bibliotecas utilizadas pelo sistema.
import os
import pickle
import shlex
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime

# Configuração do disco virtual e dos blocos.
DISCO = "filesystem.img"
TAM_DISCO = 128 * 1024 * 1024
TAM_BLOCO = 2048
TOTAL_BLOCOS = TAM_DISCO // TAM_BLOCO

# Limite de i-nodes, início dos dados e apontadores por i-node.
MAX_INODES = 8192
INICIO_DADOS = 2048
PONTEIROS_POR_INODE = 12
MAX_NOME_BYTES = 255
MAX_LINKS = 40

# Estado do sistema em memória: disco, i-nodes e mapas de ocupação.
f = None
inodes = [None] * MAX_INODES
# Nos mapas, 0 significa livre e 1 significa ocupado.
bitmap_inode = [0] * MAX_INODES
bitmap_bloco = [0] * TOTAL_BLOCOS
# I-node do diretório atual; 0 representa a raiz.
atual = 0
# Blocos preparados pelo comando atual; None significa fora de uma alteração.
gravacoes = None
proximo_bloco_livre = INICIO_DADOS


def agora():
    # Retorna a data e a hora para os metadados.
    return str(datetime.now())[:19]


# -------------------- PERSISTÊNCIA --------------------


def salvar():
    """Valida tudo antes de gravar os blocos e os metadados preparados."""
    dados = pickle.dumps((inodes, bitmap_inode, bitmap_bloco))
    if 8 + len(dados) > INICIO_DADOS * TAM_BLOCO:
        raise OSError("Metadados excedem a área reservada")

    for bloco, conteudo in (gravacoes or {}).items():
        if bitmap_bloco[bloco]:
            f.seek(bloco * TAM_BLOCO)
            f.write(conteudo)
    f.seek(0)
    f.write(len(dados).to_bytes(8, "little"))
    f.write(dados)
    f.flush()
    os.fsync(f.fileno())


@contextmanager
def transacao():
    """Prepara um comando em memória; erros de preparação não alteram a imagem.

    Não é um journal: falha de energia ou de E/S durante salvar() exige
    encerrar a sessão e recuperar uma cópia da imagem, se necessário.
    """
    global inodes, bitmap_inode, bitmap_bloco, gravacoes, proximo_bloco_livre
    if gravacoes is not None:
        raise RuntimeError("Alteração já em andamento")
    anterior = (deepcopy(inodes), bitmap_inode.copy(), bitmap_bloco.copy(),
                proximo_bloco_livre)
    gravacoes = {}
    try:
        yield
        salvar()
    except BaseException:
        inodes, bitmap_inode, bitmap_bloco, proximo_bloco_livre = anterior
        raise
    finally:
        gravacoes = None


def carregar():
    """Carrega a imagem; amplia em memória a tabela de versões anteriores."""
    global inodes, bitmap_inode, bitmap_bloco
    if os.fstat(f.fileno()).st_size != TAM_DISCO:
        raise ValueError("Imagem inválida: tamanho diferente de 128 MiB")
    f.seek(0)
    tamanho = int.from_bytes(f.read(8), "little")
    if not 0 < tamanho <= INICIO_DADOS * TAM_BLOCO - 8:
        raise ValueError("Imagem inválida: tamanho dos metadados")
    tabela, mapa_inode, mapa_bloco = pickle.loads(f.read(tamanho))
    if (not 1 <= len(tabela) <= MAX_INODES or len(mapa_inode) != len(tabela)
            or len(mapa_bloco) != TOTAL_BLOCOS
            or any(valor not in (0, 1) for valor in mapa_inode + mapa_bloco)
            or not all(mapa_bloco[:INICIO_DADOS])
            or not tabela[0] or tabela[0]["tipo"] != "diretorio"):
        raise ValueError("Imagem inválida: tabela ou mapas inconsistentes")
    faltam = MAX_INODES - len(tabela)
    inodes = tabela + [None] * faltam
    bitmap_inode = mapa_inode + [0] * faltam
    bitmap_bloco = mapa_bloco
    raiz = ler_dir(0)
    if raiz.get(".") != 0 or raiz.get("..") != 0:
        raise ValueError("Imagem inválida: diretório raiz")


# -------------------- I-NODES E BLOCOS --------------------


def novo_inode(nome, tipo):
    try:
        i = bitmap_inode.index(0)
    except ValueError:
        raise OSError("Sem i-nodes livres") from None
    bitmap_inode[i] = 1
    inodes[i] = {
        "nome": nome, "tipo": tipo, "criador": "usuario", "dono": "usuario",
        "tamanho": 0, "criacao": agora(), "modificacao": agora(),
        "permissoes": "rwxr-x",
        "blocos": [-1] * PONTEIROS_POR_INODE,
        "proximo": -1
    }
    return i


def novo_bloco():
    """Busca a partir da última posição; volta ao início para reutilizar livres."""
    global proximo_bloco_livre
    try:
        bloco = bitmap_bloco.index(0, proximo_bloco_livre)
    except ValueError:
        try:
            bloco = bitmap_bloco.index(0, INICIO_DADOS, proximo_bloco_livre)
        except ValueError:
            raise OSError("Disco cheio") from None
    bitmap_bloco[bloco] = 1
    proximo_bloco_livre = bloco + 1
    return bloco


def limpar_inode(i):
    # Libera o conteúdo, mantendo o i-node principal disponível para uso.
    inode = inodes[i]

    # Devolve os blocos diretos ao mapa de espaço livre.
    for b in inode["blocos"]:
        if b != -1:
            bitmap_bloco[b] = 0

    prox = inode["proximo"]
    inode["blocos"] = [-1] * PONTEIROS_POR_INODE
    inode["proximo"] = -1
    inode["tamanho"] = 0

    # Libera também os i-nodes usados na continuação do conteúdo.
    while prox != -1:
        p = inodes[prox]
        # Guarda a continuação antes de liberar o i-node.
        seguinte = p["proximo"]

        for b in p["blocos"]:
            if b != -1:
                bitmap_bloco[b] = 0

        inodes[prox] = None
        bitmap_inode[prox] = 0
        prox = seguinte


def apagar_inode(i):
    # Remove o conteúdo e libera o próprio i-node.
    limpar_inode(i)
    inodes[i] = None
    bitmap_inode[i] = 0


def escrever(i, dados):
    # Converte texto em bytes antes de armazenar.
    if isinstance(dados, str):
        dados = dados.encode()

    if gravacoes is None:
        raise RuntimeError("Escrita exige uma transação")
    # Conta recursos necessários e os que este arquivo pode reutilizar.
    blocos_atuais = 0
    continuacoes = 0
    parte_inode = i
    while parte_inode != -1:
        inode = inodes[parte_inode]
        blocos_atuais += sum(b != -1 for b in inode["blocos"])
        parte_inode = inode["proximo"]
        if parte_inode != -1:
            continuacoes += 1
    necessarios = (len(dados) + TAM_BLOCO - 1) // TAM_BLOCO
    extras = max(0, (necessarios + PONTEIROS_POR_INODE - 1)
                 // PONTEIROS_POR_INODE - 1)
    if necessarios > bitmap_bloco.count(0) + blocos_atuais:
        raise OSError("Disco cheio")
    if extras > bitmap_inode.count(0) + continuacoes:
        raise OSError("Sem i-nodes livres")
    limpar_inode(i)

    # pos conta os bytes gravados; atual_i identifica o i-node em uso.
    pos = 0
    atual_i = i

    while pos < len(dados):
        inode = inodes[atual_i]

        # Distribui o conteúdo entre os apontadores disponíveis.
        for j in range(PONTEIROS_POR_INODE):
            if pos >= len(dados):
                break

            b = novo_bloco()
            inode["blocos"][j] = b

            parte = dados[pos:pos + TAM_BLOCO]
            # Prepara o bloco completo em memória; salvar() grava ao final.
            gravacoes[b] = parte.ljust(TAM_BLOCO, b"\0")
            pos += len(parte)

        if pos < len(dados):
            # Encadeia outro i-node quando ainda há dados para gravar.
            prox = novo_inode(inodes[i]["nome"], inodes[i]["tipo"])
            inode["proximo"] = prox
            atual_i = prox

    inodes[i]["tamanho"] = len(dados)
    inodes[i]["modificacao"] = agora()


def ler(i):
    """Lê também os blocos preparados pelo comando, sem concatenar a cada volta."""
    tamanho = inodes[i]["tamanho"]
    partes = []
    atual_i = i
    while atual_i != -1:
        inode = inodes[atual_i]
        for bloco in inode["blocos"]:
            if bloco == -1:
                break
            if gravacoes is not None and bloco in gravacoes:
                partes.append(gravacoes[bloco])
            else:
                f.seek(bloco * TAM_BLOCO)
                partes.append(f.read(TAM_BLOCO))
        atual_i = inode["proximo"]
    return b"".join(partes)[:tamanho]


# -------------------- DIRETÓRIOS --------------------


def ler_dir(i):
    # Recupera a tabela de nomes e i-nodes de um diretório.
    if inodes[i]["tipo"] != "diretorio":
        raise NotADirectoryError("Não é diretório")

    dados = ler(i)
    return pickle.loads(dados) if dados else {}


def salvar_dir(i, d):
    # Armazena as entradas do diretório nos seus blocos.
    escrever(i, pickle.dumps(d))


def adicionar(pai, nome, i):
    # Associa um nome ao i-node dentro do diretório pai.
    d = ler_dir(pai)

    if nome in d:
        raise FileExistsError("Já existe")

    d[nome] = i
    salvar_dir(pai, d)


def remover_entrada(pai, nome):
    # Retira somente a referência do diretório pai.
    d = ler_dir(pai)

    if nome not in d:
        raise FileNotFoundError("Caminho não encontrado")

    del d[nome]
    salvar_dir(pai, d)


# -------------------- CAMINHOS --------------------


def resolver(caminho, seguir_ultimo=True, inicio=None, criar_arquivo=False):
    """Percorre o caminho e expande links, sem alterar o diretório atual.

    criar_arquivo permite criar só a última entrada ausente, inclusive o alvo
    de um link pendente. Diretórios intermediários devem existir.
    """
    if not caminho or "\0" in caminho:
        raise ValueError("Caminho inválido")
    i = 0 if caminho.startswith("/") else (atual if inicio is None else inicio)
    partes = deque(x for x in caminho.split("/") if x)
    if caminho.endswith("/") and seguir_ultimo:
        partes.append(".")
    links = 0
    while partes:
        nome = partes.popleft()
        pai = i
        d = ler_dir(pai)
        if nome not in d:
            if not criar_arquivo or partes:
                raise FileNotFoundError("Caminho não encontrado")
            validar_nome(nome)
            i = novo_inode(nome, "arquivo")
            adicionar(pai, nome, i)
        else:
            i = d[nome]
        if inodes[i]["tipo"] == "link" and (seguir_ultimo or partes):
            links += 1
            if links > MAX_LINKS:
                raise ValueError("Limite de links excedido; possível ciclo")
            alvo = ler(i).decode()
            if not alvo:
                raise FileNotFoundError("Alvo do link vazio")
            restantes = list(partes)
            partes = deque(x for x in alvo.split("/") if x)
            if alvo.endswith("/"):
                partes.append(".")
            partes.extend(restantes)
            i = 0 if alvo.startswith("/") else pai
    if caminho.endswith("/") and inodes[i]["tipo"] != "diretorio":
        raise NotADirectoryError("Barra final exige um diretório")
    return i


def validar_nome(nome):
    """Valida nomes de entradas, sem impedir . e .. na navegação."""
    if not nome or nome in (".", "..") or "/" in nome or "\0" in nome:
        raise ValueError("Nome inválido: não altere . ou ..")
    if len(nome.encode("utf-8")) > MAX_NOME_BYTES:
        raise ValueError("Nome excede 255 bytes UTF-8")


def pai_nome(caminho):
    """Separa o pai e valida o nome final para criação, remoção ou movimento."""
    caminho = caminho.rstrip("/")
    pai, separador, nome = caminho.rpartition("/")
    validar_nome(nome)
    return (resolver(pai or "/") if separador else atual), nome


# -------------------- ARQUIVOS --------------------


def touch(caminho):
    """Cria um arquivo vazio ou atualiza a data da entrada existente."""
    i = resolver(caminho, criar_arquivo=True)
    inodes[i]["modificacao"] = agora()


def rm(caminho):
    # Remove o arquivo ou o próprio link, sem seguir seu alvo.
    i = resolver(caminho, False)

    if inodes[i]["tipo"] == "diretorio":
        raise ValueError("Use rmdir")

    pai, nome = pai_nome(caminho)
    remover_entrada(pai, nome)
    apagar_inode(i)


def cat(caminho):
    # Lê o arquivo e mostra seu conteúdo no terminal.
    i = resolver(caminho)

    if inodes[i]["tipo"] == "diretorio":
        raise IsADirectoryError("É diretório")

    print(ler(i).decode())


def echo(texto, caminho, append=False):
    i = resolver(caminho, criar_arquivo=True)
    if inodes[i]["tipo"] != "arquivo":
        raise IsADirectoryError("Destino não é arquivo")
    dados = ler(i) if append else b""
    escrever(i, dados + texto.encode())


def cp(origem, destino):
    i = resolver(origem)
    if inodes[i]["tipo"] != "arquivo":
        raise IsADirectoryError("Origem não é arquivo")
    try:
        d = resolver(destino)
        if inodes[d]["tipo"] == "diretorio":
            nome = origem.rstrip("/").rsplit("/", 1)[-1]
            destino = destino.rstrip("/") + "/" + nome
    except FileNotFoundError:
        pass
    d = resolver(destino, criar_arquivo=True)
    if inodes[d]["tipo"] != "arquivo":
        raise IsADirectoryError("Destino não é arquivo")
    if d == i:
        raise ValueError("Origem e destino são o mesmo arquivo")
    escrever(d, ler(i))


# -------------------- DIRETÓRIOS --------------------


def mkdir(caminho):
    # Cria um diretório e o registra no diretório pai.
    pai, nome = pai_nome(caminho)
    d = ler_dir(pai)

    if nome in d:
        raise FileExistsError("Já existe")

    i = novo_inode(nome, "diretorio")
    # '.' aponta para si mesmo; '..' aponta para o pai.
    salvar_dir(i, {".": i, "..": pai})
    adicionar(pai, nome, i)


def rmdir(caminho):
    # Remove apenas diretórios vazios, preservando a raiz.
    i = resolver(caminho, False)

    if i == 0:
        raise ValueError("Não pode remover /")

    if inodes[i]["tipo"] != "diretorio":
        raise NotADirectoryError("Não é diretório")

    if i == atual:
        raise ValueError("Não pode remover o diretório atual")

    d = ler_dir(i)

    # Um diretório vazio contém somente '.' e '..'.
    if len(d) > 2:
        raise ValueError("Diretório não vazio")

    pai, nome = pai_nome(caminho)
    remover_entrada(pai, nome)
    apagar_inode(i)


def ls(caminho="."):
    # Lista as entradas do diretório ou o nome de um arquivo.
    i = atual if caminho == "." else resolver(caminho)

    if inodes[i]["tipo"] != "diretorio":
        print(inodes[i]["nome"])
        return

    for nome in ler_dir(i):
        print(nome)


def cd(caminho):
    global atual

    # Atualiza o i-node usado como diretório atual.
    i = resolver(caminho)

    if inodes[i]["tipo"] != "diretorio":
        raise NotADirectoryError("Não é diretório")

    atual = i


# -------------------- MOVE E LINK --------------------


def mv(origem, destino):
    # Move ou renomeia a entrada, mantendo o mesmo i-node.
    i = resolver(origem, False)

    if i == 0:
        raise ValueError("Não pode mover /")

    pai1, nome1 = pai_nome(origem)

    try:
        existe = resolver(destino)

        if inodes[existe]["tipo"] == "diretorio":
            pai2 = existe
            nome2 = nome1
        else:
            raise FileExistsError("Destino já existe")
    except FileNotFoundError:
        if destino.endswith("/"):
            raise
        pai2, nome2 = pai_nome(destino)

    # Não deixa mover diretório para dentro dele mesmo.
    if inodes[i]["tipo"] == "diretorio":
        p = pai2
        while True:
            if p == i:
                raise ValueError("Não pode mover diretório para dentro dele mesmo")
            if p == 0:
                break
            # Sobe pela hierarquia até chegar à raiz.
            p = ler_dir(p)[".."]

    if nome2 in ler_dir(pai2):
        raise FileExistsError("Destino já existe")

    # Prepara primeiro o destino. A transação desfaz toda a operação se falhar.
    if pai1 == pai2:
        entradas = ler_dir(pai1)
        del entradas[nome1]
        entradas[nome2] = i
        salvar_dir(pai1, entradas)
    else:
        adicionar(pai2, nome2, i)
        remover_entrada(pai1, nome1)
    inodes[i]["nome"] = nome2
    inodes[i]["modificacao"] = agora()

    if inodes[i]["tipo"] == "diretorio":
        d = ler_dir(i)
        # Ajusta o pai do diretório após a mudança.
        d[".."] = pai2
        salvar_dir(i, d)


def ln(alvo, link):
    # O alvo pode até não existir agora: link simbólico guarda o caminho.
    if not alvo or "\0" in alvo:
        raise ValueError("Alvo inválido")
    if link.endswith("/"):
        raise ValueError("O nome do novo link não deve terminar com /")
    pai, nome = pai_nome(link)

    if nome in ler_dir(pai):
        raise FileExistsError("Já existe")

    i = novo_inode(nome, "link")
    # Armazena o caminho do alvo, sem copiar seu conteúdo.
    escrever(i, alvo)
    adicionar(pai, nome, i)


# -------------------- INICIALIZAÇÃO --------------------


def inicializar():
    global f, inodes, bitmap_inode, bitmap_bloco, atual, proximo_bloco_livre
    inodes = [None] * MAX_INODES
    bitmap_inode = [0] * MAX_INODES
    bitmap_bloco = [0] * TOTAL_BLOCOS
    atual = 0
    proximo_bloco_livre = INICIO_DADOS
    novo = not os.path.exists(DISCO)
    f = open(DISCO, "w+b" if novo else "r+b")
    try:
        if novo:
            f.truncate(TAM_DISCO)
            for i in range(INICIO_DADOS):
                bitmap_bloco[i] = 1
            with transacao():
                raiz = novo_inode("/", "diretorio")
                salvar_dir(raiz, {".": 0, "..": 0})
        else:
            carregar()
    except BaseException:
        f.close()
        f = None
        raise


def executar(linha):
    """Valida a sintaxe; somente comandos de alteração abrem uma transação."""
    p = shlex.split(linha)
    if not p:
        return True
    cmd = p[0]
    if cmd == "echo":
        # Pontuação permite > e >> sem espaços; aspas preservam o conteúdo.
        lexer = shlex.shlex(linha, posix=True, punctuation_chars=">")
        lexer.whitespace_split = True
        lexer.commenters = ""
        p = list(lexer)
        if len(p) != 4 or p[2] not in (">", ">>"):
            raise ValueError('Use echo "texto" > arquivo ou echo "texto" >> arquivo')
        with transacao():
            echo(p[1], p[3], p[2] == ">>")
    elif cmd == "cat" and len(p) == 2:
        cat(p[1])
    elif cmd == "ls" and len(p) <= 2:
        ls(p[1] if len(p) == 2 else ".")
    elif cmd == "cd" and len(p) == 2:
        cd(p[1])
    elif cmd == "exit" and len(p) == 1:
        return False
    else:
        # A tabela evita repetir a mesma validação para cada comando.
        comandos = {"touch": (touch, 1), "rm": (rm, 1), "cp": (cp, 2),
                    "mv": (mv, 2), "mkdir": (mkdir, 1), "rmdir": (rmdir, 1)}
        if cmd == "ln" and len(p) == 4 and p[1] == "-s":
            with transacao():
                ln(p[2], p[3])
        elif cmd in comandos and len(p) == comandos[cmd][1] + 1:
            funcao, _ = comandos[cmd]
            with transacao():
                funcao(*p[1:])
        else:
            raise ValueError("Comando inválido ou quantidade de argumentos incorreta")
    return True


# -------------------- TERMINAL INTERATIVO --------------------


def main():
    try:
        inicializar()
    except (OSError, ValueError, pickle.UnpicklingError, EOFError,
            KeyError, TypeError, IndexError) as erro:
        print("Erro ao abrir a imagem:", erro)
        return 1
    print("Sistema de arquivos iniciado. Digite exit para sair.")
    try:
        while True:
            try:
                linha = input("myfs> ")
                if executar(linha) is False:
                    break
            except (EOFError, KeyboardInterrupt):
                print()
                break
            except OSError as erro:
                # Erros previstos (capacidade/caminhos) não possuem errno.
                # Falha real de E/S encerra a sessão para não continuar sobre
                # uma imagem cuja gravação pode ter sido interrompida.
                print("Erro:", erro)
                if erro.errno is not None:
                    return 1
            except ValueError as erro:
                print("Erro:", erro)
    finally:
        f.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
