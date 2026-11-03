import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from inputs import query_metadata, validate_queries, matrix_shape
from download_data import extract

MATRIX = '%%MatrixMarket matrix coordinate pattern general\n%%GraphBLAS type bool\n3 3 2\n1 2\n2 3\n'


class InputsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.query = self.root / '1'
        self.query.mkdir()
        self.meta = self.query / 'meta.txt'
        self.meta.write_text('1 0\n1 1\n1 2\n1 -7\n')
        (self.query / '-7.txt').write_text(MATRIX)
        self.graph = self.root / '7.txt'
        self.graph.write_text(MATRIX)

    def test_signed_label(self):
        validate_queries([self.query], {'7': self.graph})
        self.assertEqual(query_metadata(self.query)[-1], [-7])

    def test_bad_metadata(self):
        for data in ['-1 0 1 1 1 2 1 7',
                     '1 0 1 1 1 2 2 7', '1 0 1 1 1 2 2 7 7',
                     '1 0 1 1 1 2 1 7 extra']:
            with self.subTest(data=data):
                self.meta.write_text(data)
                with self.assertRaises(ValueError):
                    query_metadata(self.query)

    def test_missing_graph(self):
        with self.assertRaisesRegex(ValueError, 'missing graph'):
            validate_queries([self.query], {})

    def test_state_bounds(self):
        self.meta.write_text('0 0 1 4 1 2 1 -7')
        with self.assertRaisesRegex(ValueError, 'NFA state'):
            validate_queries([self.query], {'7': self.graph})

    def test_fixed_endpoints(self):
        self.meta.write_text('1 2 1 1 1 2 1 -7')
        with self.assertRaisesRegex(ValueError, 'fixed source and destination'):
            validate_queries([self.query], {'7': self.graph})

    def test_empty_state_sets(self):
        self.meta.write_text('0 0 0 0 1 -7')
        validate_queries([self.query], {'7': self.graph})

    def test_matrix_entries(self):
        for matrix in [MATRIX.replace('1 2\n', '0 2\n'),
                       MATRIX.replace('3 3 2', '3 3 3'),
                       MATRIX + '3 1\n',
                       MATRIX.replace('2 3\n', '2 3 1\n')]:
            with self.subTest(matrix=matrix):
                self.graph.write_text(matrix)
                with self.assertRaises(ValueError):
                    matrix_shape(self.graph)

    def test_archive_traversal(self):
        archive = self.root / 'bad.zip'
        with zipfile.ZipFile(archive, 'w') as out:
            out.writestr('../escaped', 'bad')
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            extract(archive, self.root / 'out')
        self.assertFalse((self.root / 'escaped').exists())

    def test_tar_link(self):
        archive = self.root / 'bad.tar'
        with tarfile.open(archive, 'w') as out:
            member = tarfile.TarInfo('link')
            member.type = tarfile.SYMTYPE
            member.linkname = '/tmp'
            out.addfile(member)
        with self.assertRaisesRegex(ValueError, 'link'):
            extract(archive, self.root / 'out')


if __name__ == '__main__':
    unittest.main()
