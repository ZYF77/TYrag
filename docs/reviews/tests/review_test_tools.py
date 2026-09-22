import ast
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]

def functions(path, names, ns):
    tree = ast.parse((ROOT / path).read_text())
    nodes = [n for n in tree.body if getattr(n, 'name', None) in names]
    for node in nodes:
        if hasattr(node, 'decorator_list'):
            node.decorator_list = []
    ns.setdefault('__package__', 'enterprise.gateway.query')
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), ns)
    return ns
