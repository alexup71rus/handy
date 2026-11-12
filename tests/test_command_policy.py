import unittest
from folder_memory.command_policy import is_forbidden_argv, parse_simple_command, validate_read_command, validate_read_recipe


class CommandPolicyTests(unittest.TestCase):
    def test_literal_read_only(self):
        self.assertEqual(validate_read_command('ls -l folder'), ['ls', '-l', 'folder'])
        self.assertEqual(validate_read_command('grep -i error app.log'), ['grep', '-i', 'error', 'app.log'])

    def test_reject_execution_and_shell(self):
        for command in ['rm -r folder', 'cp a b', 'find . -exec rm x', 'python3 -c pass', 'perl -e print',
                        'ls; rm file', 'ls | cat', 'cat file > out', 'ls $(touch x)', 'ls `id`',
                        'ls *.txt', 'FOO=bar ls', 'ls\nwhoami', 'ls --output=x', '/bin/ls -l']:
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_read_command(command)

    def test_reference_recipes_allow_literal_patterns_and_pipelines(self):
        self.assertEqual(validate_read_recipe("find . -name '*.txt' -type f -print | wc -l"),
                         [['find', '.', '-name', '*.txt', '-type', 'f', '-print'], ['wc', '-l']])
        self.assertEqual(validate_read_recipe('ls -lah .'), [['ls', '-lah', '.']])
        self.assertEqual(validate_read_recipe("grep -E '^(warn|error)[0-9]+$' app.log"),
                         [['grep', '-E', '^(warn|error)[0-9]+$', 'app.log']])
        with self.assertRaises(ValueError): validate_read_command('ls -lah .')

    def test_reference_recipes_reject_hidden_mutations_and_shell(self):
        for command in ["find . -name '*.txt' -exec rm x", 'find . -delete', 'find . -fprint output',
                        'ls -l | rm -r /tmp/x', 'ls || rm x', 'ls > output',
                        'ls $(rm x)', 'ls `rm x`', 'ls *.txt', 'ls && cat x',
                        'grep pattern file | tee output', 'sort -o output input',
                        'uniq input output', 'find . -name x -execdir rm x',
                        'find . -name x -ok rm x', 'find . -fprintf out x', 'find . -fls out']:
            with self.subTest(command=command), self.assertRaises(ValueError):
                validate_read_recipe(command)

    def test_launch_parser_is_not_authorization(self):
        self.assertEqual(parse_simple_command('python3 -m http.server 8080 --bind 127.0.0.1'),
                         ['python3', '-m', 'http.server', '8080', '--bind', '127.0.0.1'])
        self.assertFalse(is_forbidden_argv(['python3', '-m', 'http.server', '8080']))
        self.assertTrue(is_forbidden_argv(['python3', '-c', 'print(1)']))
        self.assertTrue(is_forbidden_argv(['/bin/rm', '-r', 'x']))
        self.assertTrue(is_forbidden_argv(['node', '--eval=1']))
        with self.assertRaises(ValueError): parse_simple_command('npm run dev && rm x')


if __name__ == '__main__': unittest.main()
