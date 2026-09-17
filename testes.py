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
        self.fs.inicializar()

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
        for tamanho in (0, 1, 2047, 2048, 2049, 24576, 24577, 48000):
            dados = ('áβ🙂'.encode() * 6001)[:tamanho]
            self.alterar(self.fs.escrever, i, dados)
            self.assertEqual(self.conteudo('f'), dados)
            self.integridade()
        self.comandos('rm f')
        self.assertEqual(sum(self.fs.bitmap_inode), 1)
        self.assertEqual(sum(self.fs.bitmap_bloco), self.fs.INICIO_DADOS + 1)
        self.integridade()

    def test_esgotamento_real_inodes_preserva_original(self):
        self.comandos('echo "ORIGINAL" > f')
        fs = self.fs
        with fs.transacao():
            entradas = fs.ler_dir(0)
            # Deixa espaço inicial para os i-nodes de continuação da raiz.
            for n in range(fs.MAX_INODES - 8):
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
        tamanho = (fs.TOTAL_BLOCOS - fs.INICIO_DADOS - 1) * fs.TAM_BLOCO
        self.alterar(fs.escrever, fs.resolver('grande'), b'x' * tamanho)
        self.assertEqual(fs.bitmap_bloco.count(0), 0)
        self.assertEqual(len(self.conteudo('grande')), tamanho)
        self.assertGreater(tamanho, 24 * 1024 * 1024)
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
        self.fs.inicializar()
        self.assertEqual(self.conteudo('link'), b'persistiu')
        self.integridade()

    def test_migracao_tabela_antiga(self):
        self.comandos('mkdir a', 'echo "antigo" > a/f', 'ln -s /a/f link')
        fs = self.fs
        dados = pickle.dumps((fs.inodes[:1024], fs.bitmap_inode[:1024], fs.bitmap_bloco))
        fs.f.seek(0)
        fs.f.write(len(dados).to_bytes(8, 'little') + dados)
        fs.f.close()
        fs.inicializar()
        self.assertEqual(len(fs.inodes), 8192)
        self.assertEqual(self.conteudo('link'), b'antigo')
        self.comandos('echo " novo" >> a/f')
        fs.f.close()
        fs.inicializar()
        self.assertEqual(self.conteudo('link'), b'antigo novo')
        self.integridade()

    def test_cabecalho_invalido_recusado(self):
        self.fs.f.seek(0)
        self.fs.f.write((128 * 1024 * 1024).to_bytes(8, 'little'))
        antes = self.resumo_disco()
        self.fs.f.close()
        with self.assertRaisesRegex(ValueError, 'metadados'):
            self.fs.inicializar()
        self.assertEqual(antes, self.resumo_disco())

    def test_dois_processos_e_eof(self):
        self.fs.f.close()
        for entrada, esperado in [('echo "persistiu" > f\nexit\n', 'iniciado'),
                                  ('cat /f\n', 'persistiu')]:
            result = subprocess.run([sys.executable, str(MAIN)], cwd=self.pasta.name,
                                    input=entrada, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(esperado, result.stdout)
            self.assertNotIn('EOF when reading', result.stdout)
        self.fs.inicializar()
        self.integridade()

    def test_ctrl_c_fecha_imagem(self):
        self.fs.f.close()
        with patch('builtins.input', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
