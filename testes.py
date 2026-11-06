"""Regressões com discos temporários. Execute: python -m unittest -v testes."""
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MAIN = Path(__file__).with_name('main.py')


class SistemaArquivosTest(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        spec = importlib.util.spec_from_file_location('fs_teste', MAIN)
        self.fs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.fs)
        self.fs.DISCO = str(Path(self.pasta.name) / 'filesystem.img')
        self.fs.ITERACOES_HASH = 1000  # acelera os testes
        self.fs.inicializar()
        # A maioria dos testes opera como root, que ignora as permissões.
        self.fs.usuario_atual = 'root'
        self.base_inodes = sum(self.fs.bitmap_inode)

    def reabrir(self):
        # Simula uma nova execução e volta a operar como root.
        self.fs.inicializar()
        self.fs.usuario_atual = 'root'

    def tearDown(self):
        if self.fs.f is not None:
            self.fs.f.close()
        self.pasta.cleanup()

    def comandos(self, *linhas):
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            for linha in linhas:
                self.fs.executar(linha)
        return saida.getvalue()

    def conteudo(self, caminho):
        return self.fs.ler(self.fs.resolver(caminho))

    def alterar(self, funcao, *args):
        with self.fs.transacao():
            return funcao(*args)

    def resumo_disco(self):
        if self.fs.f is not None and not self.fs.f.closed:
            self.fs.f.flush()
        resumo = hashlib.sha256()
        with open(self.fs.DISCO, 'rb') as arquivo:
            for parte in iter(lambda: arquivo.read(1024 * 1024), b''):
                resumo.update(parte)
        return resumo.digest()

    def integridade(self):
        """Confere árvore, tamanhos, cadeias e mapas, inclusive recursos órfãos."""
        fs = self.fs
        usados = set()
        blocos = set(range(fs.INICIO_DADOS))
        pendentes = [(0, 0, '/')]
        while pendentes:
            i, pai, nome = pendentes.pop()
            self.assertNotIn(i, usados)
            principal = fs.inodes[i]
            self.assertEqual(principal['nome'], nome)
            self.assertEqual(len(fs.ler(i)), principal['tamanho'])
            if principal['tipo'] == 'diretorio':
                entradas = fs.ler_dir(i)
                self.assertEqual(entradas['.'], i)
                self.assertEqual(entradas['..'], pai)
                pendentes.extend((filho, i, n) for n, filho in entradas.items()
                                 if n not in ('.', '..'))
            atual = i
            quantidade = 0
            while atual != -1:
                self.assertNotIn(atual, usados)
                usados.add(atual)
                self.assertEqual(fs.bitmap_inode[atual], 1)
                inode = fs.inodes[atual]
                for bloco in inode['blocos']:
                    if bloco == -1:
                        continue
                    self.assertNotIn(bloco, blocos)
                    self.assertLess(bloco, fs.TOTAL_BLOCOS)
                    blocos.add(bloco)
                    quantidade += 1
                atual = inode['proximo']
            self.assertEqual(quantidade, (principal['tamanho'] + fs.TAM_BLOCO - 1)
                             // fs.TAM_BLOCO)
        self.assertEqual(usados, {i for i, b in enumerate(fs.bitmap_inode) if b})
        self.assertEqual(usados, {i for i, inode in enumerate(fs.inodes) if inode})
        self.assertEqual(blocos, {i for i, b in enumerate(fs.bitmap_bloco) if b})

    def test_operacoes_absolutas_e_relativas(self):
        for absoluto in (False, True):
            with self.subTest(absoluto=absoluto):
                pasta = '/absolutos' if absoluto else '/relativos'
                self.comandos('mkdir ' + pasta, 'cd ' + pasta)
                prefixo = pasta + '/' if absoluto else ''
                self.comandos(f'touch {prefixo}vazio', f'mkdir {prefixo}sub',
                              f'echo "olá" > {prefixo}a', f'echo " mundo" >> {prefixo}a',
                              f'echo "novo" >> {prefixo}criado', f'cp {prefixo}a {prefixo}copia',
                              f'mv {prefixo}copia {prefixo}renomeado', f'ln -s {prefixo}a {prefixo}link')
                self.assertEqual(self.comandos(f'cat {prefixo}link'), 'olá mundo\n')
                self.assertEqual(self.conteudo(prefixo + 'criado'), b'novo')
                self.assertIn('renomeado', self.comandos(f'ls {prefixo}./'))
                self.comandos(f'cd {prefixo}sub', 'cd ..', f'rm {prefixo}link',
                              f'rm {prefixo}renomeado', f'rmdir {prefixo}sub', 'cd /')
        self.integridade()

    def test_movimento_diretorios_e_pai(self):
        self.comandos('mkdir a', 'mkdir a/b', 'mkdir destino', 'echo "x" > a/b/f',
                      'mv a destino', 'mv destino/a destino/novo', 'cd /destino/novo/b', 'cd ..')
        self.assertEqual(self.fs.atual, self.fs.resolver('/destino/novo'))
        self.assertEqual(self.conteudo('b/f'), b'x')
        self.integridade()

    def test_protecao_ponto_pai_e_atual(self):
        self.comandos('mkdir a', 'mkdir b', 'cd a')
        antes = self.resumo_disco()
        for cmd in ('rmdir .', 'rmdir /a', 'rmdir /a/.', 'mv . /b/novo', 'mv .. /b/novo',
                    'rmdir ..', 'rmdir /', 'mv / /novo', 'mv /a /a/filho', 'mv /a /a'):
            with self.subTest(cmd=cmd), self.assertRaises((ValueError, OSError)):
                self.fs.executar(cmd)
        self.assertEqual(antes, self.resumo_disco())
        self.integridade()

    def test_diretorio_nao_vazio_e_descendente(self):
        self.comandos('mkdir a', 'mkdir a/b', 'ln -s /a/b descendente')
        for cmd in ('rmdir a', 'mv a a/b', 'mv a descendente'):
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                self.fs.executar(cmd)
        self.integridade()

    def test_links_relativos_intermediarios_e_pendentes(self):
        self.comandos('mkdir a', 'mkdir outro', 'echo "alvo" > a/f', 'ln -s f a/link',
                      'ln -s /a absoluto', 'ln -s a relativo', 'cd outro')
        for path in ('/a/link', '/absoluto/link', '../relativo/f'):
            self.assertEqual(self.conteudo(path), b'alvo')
        atual = self.fs.atual
        self.comandos('ln -s novo /a/pendente', 'echo "criado" > /a/pendente')
        self.assertEqual(self.fs.atual, atual)
        self.assertEqual(self.conteudo('/a/novo'), b'criado')
        self.comandos('rm /a/link')
        self.assertEqual(self.conteudo('/a/f'), b'alvo')
        self.integridade()

    def test_ciclos_e_cadeias_de_links(self):
        self.comandos('ln -s b a', 'ln -s a b', 'ln -s si si')
        for path in ('a', 'b', 'si'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Limite de links'):
                self.fs.resolver(path)
        self.comandos('echo "fim" > alvo')
        with self.fs.transacao():
            anterior = 'alvo'
            for n in range(self.fs.MAX_LINKS):
                nome = f'l{n}'
                self.fs.ln(anterior, nome)
                anterior = nome
        self.assertEqual(self.conteudo(anterior), b'fim')
        self.comandos(f'ln -s {anterior} excessivo')
        with self.assertRaisesRegex(ValueError, 'Limite de links'):
            self.fs.resolver('excessivo')
        self.integridade()

    def test_cp_mv_para_link_diretorio(self):
        self.comandos('mkdir d', 'ln -s /d l', 'echo "x" > f', 'cp f l', 'mv f l/movido')
        self.assertEqual(self.conteudo('/d/f'), b'x')
        self.assertEqual(self.conteudo('/d/movido'), b'x')
        self.comandos('echo "y" > g', 'mv g l')
        self.assertEqual(self.conteudo('/d/g'), b'y')
        self.integridade()

    def test_cp_nome_link_e_mesmo_arquivo(self):
        self.comandos('echo "x" > original', 'ln -s original atalho', 'mkdir d', 'cp atalho d')
        self.assertEqual(self.conteudo('/d/atalho'), b'x')
        with self.assertRaises(ValueError):
            self.comandos('cp original atalho')
        self.assertEqual(self.conteudo('original'), b'x')
        self.integridade()

    def test_barra_final_nao_remove_alvo_link(self):
        self.comandos('mkdir d', 'ln -s d l', 'echo "x" > f')
        antes = self.resumo_disco()
        for cmd in ('rmdir l/', 'rm l/', 'mv l/ novo', 'rm f/', 'touch ausente/',
                    'echo "x" > ausente/', 'mv f ausente/', 'ln -s f novo/'):
            with self.subTest(cmd=cmd), self.assertRaises((ValueError, OSError)):
                self.fs.executar(cmd)
        self.assertEqual(antes, self.resumo_disco())
        self.comandos('cd l/', 'cd /', 'rmdir d/')
        self.integridade()

    def test_echo_operadores_aspas_e_sintaxe(self):
        self.comandos('echo ">" > a', 'echo ">>" >> a',
                      'echo " espaço  # texto " > "nome com espaço"',
                      'echo "á">sem_espaco', 'echo "β">>sem_espaco')
        self.assertEqual(self.conteudo('a'), b'>>>')
        self.assertEqual(self.conteudo('nome com espaço'), ' espaço  # texto '.encode())
        self.assertEqual(self.conteudo('sem_espaco'), 'áβ'.encode())
        antes = self.resumo_disco()
        for cmd in ('echo "x" >', 'echo "x" > a ignorado', 'echo "x" >>> a',
                    'echo "x" > a > b', 'echo "sem fechar', 'exit extra', 'touch'):
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                self.fs.executar(cmd)
        self.assertEqual(antes, self.resumo_disco())
        self.integridade()

    def test_nomes_limite_utf8(self):
        self.comandos('touch ' + 'a' * 255)
        antes = self.resumo_disco()
        for nome in ('a' * 256, 'á' * 128):
            for comando in ('touch', 'mkdir'):
                with self.subTest(comando=comando), self.assertRaises(ValueError):
                    self.fs.executar(comando + ' ' + nome)
        self.assertEqual(antes, self.resumo_disco())
        self.integridade()

    def test_fronteiras_blocos_e_liberacao(self):
        self.comandos('touch f')
        i = self.fs.resolver('f')
        blocos = sum(self.fs.bitmap_bloco)
        for tamanho in (0, 1, 2047, 2048, 2049, 24576, 24577, 48000):
            dados = ('áβ🙂'.encode() * 6001)[:tamanho]
            self.alterar(self.fs.escrever, i, dados)
            self.assertEqual(self.conteudo('f'), dados)
            self.integridade()
        self.comandos('rm f')
        self.assertEqual(sum(self.fs.bitmap_inode), self.base_inodes)
        self.assertEqual(sum(self.fs.bitmap_bloco), blocos)
        self.integridade()

    def test_esgotamento_real_inodes_preserva_original(self):
        self.comandos('echo "ORIGINAL" > f')
        fs = self.fs
        with fs.transacao():
            entradas = fs.ler_dir(0)
            # Deixa espaço inicial para os i-nodes de continuação da raiz.
            for n in range(fs.bitmap_inode.count(0) - 8):
                nome = f'v{n}'
                entradas[nome] = fs.novo_inode(nome, 'arquivo')
            fs.salvar_dir(0, entradas)
            while 0 in fs.bitmap_inode:
                nome = 'extra' + str(fs.bitmap_inode.index(0))
                entradas[nome] = fs.novo_inode(nome, 'arquivo')
            fs.salvar_dir(0, entradas)
        self.assertNotIn(0, fs.bitmap_inode)
        antes = self.resumo_disco()
        with self.assertRaisesRegex(OSError, 'i-nodes'):
            self.alterar(fs.escrever, fs.resolver('f'), b'x' * 24577)
        with self.assertRaisesRegex(OSError, 'i-nodes'):
            self.comandos('touch novo')
        self.assertEqual(self.conteudo('f'), b'ORIGINAL')
        self.assertEqual(antes, self.resumo_disco())
        self.integridade()

    def test_disco_cheio_e_capacidade_acima_24_mib(self):
        fs = self.fs
        self.comandos('touch grande')
        tamanho = fs.bitmap_bloco.count(0) * fs.TAM_BLOCO
        self.alterar(fs.escrever, fs.resolver('grande'), b'x' * tamanho)
        self.assertEqual(fs.bitmap_bloco.count(0), 0)
        self.assertEqual(len(self.conteudo('grande')), tamanho)
        self.assertGreater(tamanho, 24 * 1024 * 1024)
        fs.f.close()
        self.reabrir()
        self.assertEqual(self.conteudo('grande'), b'x' * tamanho)
        antes = self.resumo_disco()
        with self.assertRaisesRegex(OSError, 'Disco cheio'):
            self.comandos('echo "x" >> grande')
        with self.assertRaisesRegex(OSError, 'Disco cheio'):
            self.comandos('echo "x" > novo')
        self.assertEqual(antes, self.resumo_disco())
        self.assertNotIn('novo', fs.ler_dir(0))
        self.integridade()
        self.comandos('echo "menor" > grande')
        self.assertEqual(self.conteudo('grande'), b'menor')
        self.integridade()

    def test_erro_apos_preparar_mv_desfaz_tudo(self):
        self.comandos('mkdir a', 'mkdir b', 'echo "original" > a/f')
        antes = self.resumo_disco()
        with patch.object(self.fs, 'remover_entrada', side_effect=OSError('Falha simulada')):
            with self.assertRaises(OSError):
                self.comandos('mv a/f b')
        self.assertEqual(antes, self.resumo_disco())
        self.assertEqual(self.conteudo('a/f'), b'original')
        self.assertNotIn('f', self.fs.ler_dir(self.fs.resolver('b')))
        self.integridade()

    def test_limite_metadados_desfaz_comando(self):
        self.comandos('echo "original" > f')
        antes = self.resumo_disco()
        # Força excesso só na serialização final, sem aceitar nomes enormes.
        with patch.object(self.fs.pickle, 'dumps', return_value=b'x' * (4 * 1024 * 1024)):
            with self.assertRaisesRegex(OSError, 'Metadados'):
                self.comandos('touch f')
        self.assertEqual(antes, self.resumo_disco())
        self.assertEqual(self.conteudo('f'), b'original')
        self.integridade()

    def test_leituras_nao_salvam(self):
        self.comandos('mkdir a', 'echo "x" > f')
        with patch.object(self.fs, 'salvar', side_effect=AssertionError('Não deveria salvar')):
            self.comandos('cat f', 'ls /', 'cd a', 'cd ..', '', 'exit')

    def test_persistencia_reabertura(self):
        self.comandos('mkdir a', 'echo "persistiu" > a/f', 'ln -s /a/f link')
        self.fs.f.close()
        self.reabrir()
        self.assertEqual(self.conteudo('link'), b'persistiu')
        self.integridade()

    def test_migracao_tabela_antiga(self):
        self.comandos('mkdir a', 'echo "antigo" > a/f', 'ln -s /a/f link')
        fs = self.fs
        dados = pickle.dumps((fs.inodes[:1024], fs.bitmap_inode[:1024], fs.bitmap_bloco))
        fs.f.seek(0)
        fs.f.write(len(dados).to_bytes(8, 'little') + dados)
        fs.f.close()
        self.reabrir()
        self.assertEqual(len(fs.inodes), 8192)
        self.assertEqual(self.conteudo('link'), b'antigo')
        self.comandos('echo " novo" >> a/f')
        fs.f.close()
        self.reabrir()
        self.assertEqual(self.conteudo('link'), b'antigo novo')
        self.integridade()

    def test_cabecalho_invalido_recusado(self):
        self.fs.f.seek(0)
        self.fs.f.write((128 * 1024 * 1024).to_bytes(8, 'little'))
        antes = self.resumo_disco()
        self.fs.f.close()
        with self.assertRaisesRegex(ValueError, 'metadados'):
            self.reabrir()
        self.assertEqual(antes, self.resumo_disco())

    def test_dois_processos_e_eof(self):
        self.fs.f.close()
        for entrada, esperado in [('raiz\nroot\nraiz\n' + 'echo "persistiu" > /f\nexit\n', 'iniciado'),
                                  ('root\nraiz\ncat /f\n', 'persistiu')]:
            result = subprocess.run([sys.executable, str(MAIN)], cwd=self.pasta.name,
                                    input=entrada, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(esperado, result.stdout)
            self.assertNotIn('EOF when reading', result.stdout)
        self.reabrir()
        self.integridade()

    def test_ctrl_c_fecha_imagem(self):
        self.fs.f.close()
        # ler_senha também é simulado: no Windows, getpass lê direto do console.
        with patch('builtins.input', side_effect=KeyboardInterrupt), \
                patch.object(self.fs, 'ler_senha', side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.fs.main(), 0)
        self.assertTrue(self.fs.f.closed)

    def test_importar_nao_cria_imagem(self):
        with tempfile.TemporaryDirectory() as pasta:
            codigo = ('import importlib.util; '
                      f's=importlib.util.spec_from_file_location("fs", {str(MAIN)!r}); '
                      'm=importlib.util.module_from_spec(s); s.loader.exec_module(m)')
            result = subprocess.run([sys.executable, '-c', codigo], cwd=pasta,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(os.listdir(pasta), [])


    def test_operacoes_intercaladas_com_modelo_de_referencia(self):
        import random
        rng = random.Random(20260921)
        modelo = {}
        self.comandos('mkdir a', 'mkdir b')
        for n in range(240):
            destino = f'/{rng.choice(("a", "b"))}/f{rng.randrange(30)}'
            acao = rng.choice(('escrever', 'append', 'copiar', 'mover', 'remover'))
            if acao in ('escrever', 'append'):
                texto = f'dado-{n}-á-🙂'
                operador = '>>' if acao == 'append' else '>'
                self.comandos(f'echo "{texto}" {operador} {destino}')
                modelo[destino] = ((modelo.get(destino, b'') if acao == 'append' else b'')
                                   + texto.encode())
            elif modelo:
                origem = rng.choice(sorted(modelo))
                if acao == 'remover':
                    self.comandos('rm ' + origem)
                    del modelo[origem]
                elif acao == 'copiar' and origem != destino:
                    self.comandos(f'cp {origem} {destino}')
                    modelo[destino] = modelo[origem]
                elif acao == 'mover' and destino not in modelo:
                    self.comandos(f'mv {origem} {destino}')
                    modelo[destino] = modelo.pop(origem)
            if n % 40 == 0:
                self.fs.f.close()
                self.reabrir()
                self.integridade()
                for caminho, esperado in modelo.items():
                    self.assertEqual(self.conteudo(caminho), esperado)
        self.integridade()
        for pasta in ('a', 'b'):
            encontrados = set(self.fs.ler_dir(self.fs.resolver('/' + pasta))) - {'.', '..'}
            esperados = {p.rsplit('/', 1)[1] for p in modelo if p.startswith('/' + pasta + '/')}
            self.assertEqual(encontrados, esperados)
        for caminho, esperado in modelo.items():
            self.assertEqual(self.conteudo(caminho), esperado)

    def test_mover_link_e_remover_link_de_diretorio(self):
        self.comandos('mkdir a', 'mkdir b', 'echo "A" > a/f', 'echo "B" > b/f',
                      'ln -s f a/link', 'mv a/link b')
        self.assertEqual(self.conteudo('/b/link'), b'B')
        self.comandos('ln -s /a linkdir', 'rm linkdir')
        self.assertEqual(self.conteudo('/a/f'), b'A')
        self.comandos('rm /b/f')
        with self.assertRaises(FileNotFoundError):
            self.fs.resolver('/b/link')
        self.comandos('echo "recriado" > /b/link')
        self.assertEqual(self.conteudo('/b/f'), b'recriado')
        self.integridade()

    def test_nomes_unicode_validos_e_conteudo_vazio(self):
        nome = '🙂' * 63 + 'abc'
        self.assertEqual(len(nome.encode()), 255)
        self.comandos('mkdir "pasta com espaço"', f'touch "/pasta com espaço/{nome}"',
                      'echo "" > vazio', 'echo "" >> vazio', 'cp vazio copia',
                      'echo "olá" > "pasta com espaço/ação"')
        self.assertEqual(self.conteudo('vazio'), b'')
        self.assertEqual(self.conteudo('copia'), b'')
        self.assertEqual(self.conteudo('//pasta com espaço///ação'), 'olá'.encode())
        self.integridade()

    def test_terminal_continua_apos_erros_de_usuario(self):
        self.fs.f.close()
        entrada = ('raiz\nroot\nraiz\n' + 'cat ausente\necho "x" >\nmkdir pasta\necho "ok" > pasta/f\n'
                   'rmdir pasta\ncat pasta/f\ncomando_inexistente\nexit\n')
        resultado = subprocess.run([sys.executable, str(MAIN)], cwd=self.pasta.name,
                                   input=entrada, text=True, capture_output=True, timeout=10)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertEqual(resultado.stdout.count('Erro:'), 4)
        self.assertIn('ok\n', resultado.stdout)
        self.assertNotIn('Traceback', resultado.stderr)
        self.reabrir()
        self.assertEqual(self.conteudo('/root/pasta/f'), b'ok')
        self.integridade()

    def test_destinos_duplicados_e_tipos_invalidos_preservam_imagem(self):
        self.comandos('mkdir a', 'mkdir b', 'echo "A" > a/f', 'echo "B" > b/f')
        antes = self.resumo_disco()
        for cmd in ('mkdir a', 'mv a/f b/f', 'mv a/f b', 'ln -s a outro/',
                    'ln -s a b/f', 'rm a', 'rmdir a/f', 'cp a novo',
                    'cd a/f', 'echo "x" > a', 'mkdir a/f/sub'):
            with self.subTest(cmd=cmd), self.assertRaises((ValueError, OSError)):
                self.fs.executar(cmd)
        self.assertEqual(antes, self.resumo_disco())
        self.integridade()

    def test_metadados_touch_tamanho_datas_e_permissoes(self):
        with patch.object(self.fs, 'agora', return_value='2026-09-21 10:00:00'):
            self.comandos('echo "á🙂" > f')
        i = self.fs.resolver('f')
        inode = self.fs.inodes[i]
        self.assertEqual(inode['tamanho'], 6)
        self.assertEqual(inode['criacao'], '2026-09-21 10:00:00')
        self.assertEqual(inode['modificacao'], inode['criacao'])
        self.assertEqual(inode['permissoes'], 'rwr-')
        self.assertEqual(inode['dono'], 'root')
        self.assertEqual(inode['criador'], 'root')
        with patch.object(self.fs, 'agora', return_value='2026-09-21 11:00:00'):
            self.comandos('touch f')
        self.assertEqual(self.fs.inodes[i]['modificacao'], '2026-09-21 11:00:00')
        self.assertEqual(self.fs.inodes[i]['criacao'], '2026-09-21 10:00:00')
        self.assertEqual(self.conteudo('f'), 'á🙂'.encode())
        self.fs.f.close()
        self.reabrir()
        self.assertEqual(self.fs.inodes[i]['modificacao'], '2026-09-21 11:00:00')
        self.integridade()

    def test_falhas_de_criacao_nao_deixam_recursos_orfaos(self):
        original = self.fs.adicionar
        def adicionar_e_falhar(*args):
            original(*args)
            raise OSError('Falha simulada após preparar a entrada')
        antes = self.resumo_disco()
        for cmd in ('touch novo', 'mkdir novo', 'ln -s ausente novo', 'echo "x" > novo'):
            with self.subTest(cmd=cmd):
                with patch.object(self.fs, 'adicionar', side_effect=adicionar_e_falhar):
                    with self.assertRaises(OSError):
                        self.comandos(cmd)
                self.integridade()
                self.assertEqual(sum(self.fs.bitmap_inode), self.base_inodes)
        self.assertEqual(antes, self.resumo_disco())

    def test_excesso_metadados_descarta_blocos_preparados(self):
        self.comandos('echo "antes" > existente')
        dumps = self.fs.pickle.dumps
        def serializar(objeto, *args, **kwargs):
            if isinstance(objeto, tuple):
                return b'x' * (4 * 1024 * 1024)
            return dumps(objeto, *args, **kwargs)
        antes = self.resumo_disco()
        with patch.object(self.fs.pickle, 'dumps', side_effect=serializar):
            with self.assertRaisesRegex(OSError, 'Metadados'):
                self.comandos('echo "novo conteúdo" > novo')
        self.assertEqual(antes, self.resumo_disco())
        self.assertEqual(self.conteudo('existente'), b'antes')
        self.assertNotIn('novo', self.fs.ler_dir(0))
        self.integridade()


    def test_roteiro_demonstracao_do_pacote(self):
        entrada = MAIN.with_name('roteiro_demonstracao.txt').read_text(encoding='utf-8')
        self.fs.f.close()
        resultado = subprocess.run([sys.executable, str(MAIN)], cwd=self.pasta.name,
                                   input=entrada, text=True, capture_output=True, timeout=10)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        # O roteiro inclui de propósito 13 tentativas que devem ser recusadas.
        self.assertEqual(resultado.stdout.count('Erro:'), 13, resultado.stdout)
        self.assertEqual(resultado.stdout.count('conteudo legal e mais conteudo'), 3)
        self.assertIn('texto publico (agora o arquivo e do bob)', resultado.stdout)
        self.reabrir()
        self.assertEqual(self.conteudo('/destino/demonstracao/arquivo.txt'),
                         b'conteudo legal e mais conteudo')
        self.integridade()

    # -------------------- SEGURANÇA --------------------

    def criar_usuarios(self, *nomes):
        with self.fs.transacao():
            self.fs.definir_senha('root', 'raiz')
            for nome in nomes:
                self.fs.useradd(nome, 'senha_' + nome)

    def entrar(self, nome):
        self.fs.logout()
        self.fs.login(nome, 'raiz' if nome == 'root' else 'senha_' + nome)

    def test_senhas_com_hash_e_sal(self):
        self.criar_usuarios('alice', 'bob')
        with self.fs.transacao():
            self.fs.passwd('bob', 'senha_alice')  # mesma senha da alice
        shadow = self.conteudo('/etc/shadow').decode()
        self.assertNotIn('senha_alice', shadow)
        self.assertNotIn('raiz', shadow)
        contas = self.fs.ler_shadow()
        self.assertNotEqual(contas['alice'], contas['bob'])  # sal diferente
        self.assertTrue(self.fs.conferir_senha('bob', 'senha_alice'))
        self.assertFalse(self.fs.conferir_senha('bob', 'senha_bob'))
        self.assertFalse(self.fs.conferir_senha('ninguem', 'x'))
        with self.assertRaises(PermissionError):
            self.fs.login('alice', 'errada')
        self.integridade()

    def test_shadow_inacessivel_a_usuario_comum(self):
        self.criar_usuarios('alice')
        self.entrar('alice')
        antes = self.resumo_disco()
        for cmd in ('cat /etc/shadow', 'cp /etc/shadow copia', 'echo "x" > /etc/shadow',
                    'echo "x" >> /etc/shadow', 'rm /etc/shadow', 'mv /etc/shadow s',
                    'chmod rwrw /etc/shadow', 'touch /etc/shadow', 'touch /etc/novo'):
            with self.subTest(cmd=cmd), self.assertRaises(PermissionError):
                self.comandos(cmd)
        self.assertEqual(antes, self.resumo_disco())
        self.comandos('ln -s /etc/shadow atalho')
        with self.assertRaises(PermissionError):
            self.comandos('cat atalho')
        self.assertIn('alice', self.fs.ler_shadow())
        self.integridade()

    def test_home_privada_e_permissoes_dono_demais(self):
        self.criar_usuarios('alice', 'bob')
        self.entrar('alice')
        self.assertEqual(self.fs.atual, self.fs.resolver('/home/alice'))
        self.comandos('echo "oi" > f', 'mkdir sub')
        self.assertEqual(self.fs.inodes[self.fs.resolver('f')]['dono'], 'alice')
        self.entrar('bob')
        for cmd in ('ls /home/alice', 'cd /home/alice', 'cat /home/alice/f',
                    'mkdir /home/alice/x', 'mkdir /home/novo', 'touch /f'):
            with self.subTest(cmd=cmd), self.assertRaises(PermissionError):
                self.comandos(cmd)
        self.entrar('alice')
        self.comandos('chmod rwr- /home/alice')
        self.entrar('bob')
        self.assertEqual(self.comandos('cat /home/alice/f'), 'oi\n')
        self.comandos('cp /home/alice/f minha_copia')
        for cmd in ('echo "x" >> /home/alice/f', 'rm /home/alice/f',
                    'mv /home/alice/f /home/bob/g', 'chmod rwrw /home/alice/f'):
            with self.subTest(cmd=cmd), self.assertRaises(PermissionError):
                self.comandos(cmd)
        self.entrar('alice')
        self.comandos('chmod 66 f', 'chmod ---- sub')
        self.assertEqual(self.fs.inodes[self.fs.resolver('f')]['permissoes'], 'rwrw')
        with self.assertRaises(PermissionError):
            self.comandos('ls sub')  # nem o dono lê sem r
        self.entrar('bob')
        self.comandos('echo " bob" >> /home/alice/f')
        self.assertEqual(self.conteudo('/home/alice/f'), b'oi bob')
        self.integridade()

    def test_chown_somente_root(self):
        self.criar_usuarios('alice', 'bob')
        self.entrar('alice')
        self.comandos('echo "x" > f')
        with self.assertRaises(PermissionError):
            self.comandos('chown bob f')
        self.entrar('root')
        with self.assertRaises(ValueError):
            self.comandos('chown fulano /home/alice/f')
        self.comandos('chown bob /home/alice/f', 'chmod rwr- /home/alice')
        self.entrar('bob')
        self.comandos('chmod rw-- /home/alice/f', 'echo "do bob" > /home/alice/f')
        self.entrar('alice')
        with self.assertRaises(PermissionError):
            self.comandos('cat f')
        self.integridade()

    def test_chmod_formatos(self):
        for modo, esperado in (('64', 'rwr-'), ('60', 'rw--'), ('46', 'r-rw'),
                               ('00', '----'), ('r-r-', 'r-r-')):
            self.assertEqual(self.fs.converter_modo(modo), esperado)
        for modo in ('7', '77', '65', 'rwx', 'wr--', '640'):
            with self.subTest(modo=modo), self.assertRaises(ValueError):
                self.fs.converter_modo(modo)

    def test_comandos_de_root_e_passwd(self):
        self.criar_usuarios('alice', 'bob')
        self.entrar('alice')
        with self.assertRaises(PermissionError):
            self.comandos('useradd carol')
        with self.assertRaises(PermissionError):
            self.alterar(self.fs.passwd, 'bob', 'x')
        self.alterar(self.fs.passwd, 'alice', 'nova')
        self.assertTrue(self.fs.conferir_senha('alice', 'nova'))
        self.entrar('root')
        for nome in ('alice', 'Maiuscula', 'com:dois', ''):
            with self.subTest(nome=nome), self.assertRaises((ValueError, OSError)):
                self.alterar(self.fs.useradd, nome, 'x')
        with self.assertRaises(ValueError):
            self.alterar(self.fs.useradd, 'carol', '')
        self.integridade()

    def test_permissoes_persistem_e_imagem_antiga_migra(self):
        self.criar_usuarios('alice')
        self.entrar('alice')
        self.comandos('echo "x" > f', 'chmod rw-- f')
        self.fs.f.close()
        self.fs.inicializar()
        self.assertIsNone(self.fs.usuario_atual)
        self.fs.usuario_atual = 'root'
        inode = self.fs.inodes[self.fs.resolver('/home/alice/f')]
        self.assertEqual((inode['dono'], inode['permissoes']), ('alice', 'rw--'))
        self.assertTrue(self.fs.conferir_senha('alice', 'senha_alice'))
        # Imagem da versão anterior: "rwxr-x", dono "usuario" e sem /etc/shadow.
        self.fs.usuario_atual = 'root'
        with self.fs.transacao():
            self.fs.inodes[0]['permissoes'] = 'rwxr-x'
            self.fs.inodes[0]['dono'] = 'usuario'
            self.fs.rm('/etc/shadow')
        self.fs.f.close()
        self.reabrir()
        self.assertEqual((self.fs.inodes[0]['dono'], self.fs.inodes[0]['permissoes']),
                         ('root', 'rwr-'))
        self.assertEqual(self.fs.ler_shadow(), {})
        self.integridade()

    def test_terminal_exige_login(self):
        self.fs.f.close()
        entrada = ('raiz\nroot\nerrada\nroot\nraiz\nuseradd ana\nsenha_ana\nlogout\n'
                   'ana\nsenha_ana\nwhoami\ncat /etc/shadow\nexit\n')
        resultado = subprocess.run([sys.executable, str(MAIN)], cwd=self.pasta.name,
                                   input=entrada, text=True, capture_output=True, timeout=20)
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn('ana@myfs> ana', resultado.stdout)
        self.assertEqual(resultado.stdout.count('Erro:'), 2)
        with open(self.fs.DISCO, 'rb') as imagem:
            self.assertNotIn(b'senha_ana', imagem.read())


if __name__ == '__main__':
    unittest.main(verbosity=2)
