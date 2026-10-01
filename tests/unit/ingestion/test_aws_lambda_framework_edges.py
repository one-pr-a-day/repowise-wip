"""AWS Lambda handlers named in serverless.yml and SAM templates as graph edges.

The cases go through traverse -> parse -> graph -> framework edges -> dead code,
the path a real index takes.
"""

from __future__ import annotations

from pathlib import Path

from repowise.core.analysis.dead_code import DeadCodeAnalyzer
from repowise.core.ingestion import ASTParser, FileTraverser, GraphBuilder

_SERVERLESS_NODE = """\
service: users
provider:
  name: aws
  runtime: nodejs20.x
functions:
  create:
    handler: src/handlers/users.create
  legacy:
    handler: src/handlers/legacy.run
  missing:
    handler: src/handlers/users.doesNotExist
  variable:
    handler: ${self:custom.handler}
  nowhere:
    handler: src/handlers/absent.main
"""

_SERVERLESS_PYTHON = """\
service: reports
provider:
  name: aws
  runtime: python3.12
functions:
  main:
    handler: app.handlers.lambda_handler
"""

_SAM_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Globals:
  Function:
    Runtime: nodejs20.x
    CodeUri: functions/
Resources:
  IndexFn:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
      Handler: index.handler
      Role: !GetAtt Role.Arn
      Environment:
        Variables:
          TABLE: !Ref Table
          URL: !Sub "https://${Api}.example.com"
  AppFn:
    Type: AWS::Serverless::Function
    Properties:
      Handler: app.lambdaHandler
      Policies:
        - !Ref Policy
  Table:
    Type: AWS::DynamoDB::Table
"""

_APP = {
    "node-svc/serverless.yml": _SERVERLESS_NODE,
    "node-svc/src/handlers/users.mjs": (
        "import { save } from '../lib/db.mjs';\n"
        "export const create = async (event) => save(format(event));\n"
        "export function format(x) { return x; }\n"
        "export function orphanExport() { return 1; }\n"
    ),
    "node-svc/src/handlers/legacy.js": "module.exports.run = async (event) => event;\n",
    "node-svc/src/lib/db.mjs": "export function save(x) { return x; }\n",
    "node-svc/src/lib/report.mjs": (
        "import { format } from '../handlers/users.mjs';\nexport const render = (x) => format(x);\n"
    ),
    "py-svc/serverless.yml": _SERVERLESS_PYTHON,
    "py-svc/app/__init__.py": "",
    "py-svc/app/handlers.py": (
        "def lambda_handler(event, context):\n    return shared(event)\n\n\n"
        "def shared(x):\n    return x\n\n\n"
        "def unused_py():\n    return 2\n"
    ),
    "py-svc/app/jobs.py": "from app.handlers import shared\n\n\ndef run():\n    return shared(1)\n",
    "sam-app/template.yaml": _SAM_TEMPLATE,
    "sam-app/src/index.mjs": "export const handler = async (e) => e;\n",
    "sam-app/functions/app.mjs": (
        "export const lambdaHandler = async (e) => e;\nexport const helper = (x) => x;\n"
    ),
    "sam-app/functions/use.mjs": "import { helper } from './app.mjs';\nexport const u = helper(1);\n",
    # Not a mapping and not valid YAML: both must be skipped quietly.
    "broken/serverless.yml": "functions: [unclosed\n",
    "list/template.yml": "- just\n- a list\n",
}


def _write(repo: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return repo


def _graph(repo: Path) -> tuple[object, set[tuple[str, str, str]]]:
    builder = GraphBuilder(repo)
    parser = ASTParser()
    source_map: dict[str, bytes] = {}
    for fi in FileTraverser(repo).traverse():
        src = Path(fi.abs_path).read_bytes()
        builder.add_file(parser.parse_file(fi, src))
        source_map[fi.path] = src
    builder.set_source_map(source_map)
    builder.build()
    builder.add_framework_edges([])
    report = DeadCodeAnalyzer(
        builder.graph(), {}, parsed_files=builder._parsed_files, source_map=source_map, repo_root=repo
    ).analyze({})
    dead = {(f.kind.value, f.file_path, f.symbol_name or "") for f in report.findings}
    return builder.graph(), dead


def _framework_names(graph, source: str, target: str) -> list[str]:
    data = graph.get_edge_data(source, target) or {}
    assert data.get("edge_type") == "framework", data
    return data["imported_names"]


def test_node_handler_by_file_path_and_export(tmp_path: Path) -> None:
    graph, dead = _graph(_write(tmp_path, _APP))
    names = _framework_names(graph, "node-svc/serverless.yml", "node-svc/src/handlers/users.mjs")
    assert names == ["create"]
    assert ("unused_export", "node-svc/src/handlers/users.mjs", "create") not in dead
    # The rest of the handler file is still judged on its own uses.
    assert ("unused_export", "node-svc/src/handlers/users.mjs", "orphanExport") in dead


def test_commonjs_handler_keeps_its_file_reachable(tmp_path: Path) -> None:
    graph, dead = _graph(_write(tmp_path, _APP))
    assert _framework_names(graph, "node-svc/serverless.yml", "node-svc/src/handlers/legacy.js") == [
        "run"
    ]
    assert ("unreachable_file", "node-svc/src/handlers/legacy.js", "") not in dead


def test_python_handler_by_dotted_module(tmp_path: Path) -> None:
    graph, dead = _graph(_write(tmp_path, _APP))
    assert _framework_names(graph, "py-svc/serverless.yml", "py-svc/app/handlers.py") == [
        "lambda_handler"
    ]
    binds = graph.get_edge_data(
        "py-svc/serverless.yml::__module__", "py-svc/app/handlers.py::lambda_handler"
    )
    assert binds["edge_type"] == "framework_binds"
    assert ("unused_export", "py-svc/app/handlers.py", "lambda_handler") not in dead
    assert ("unused_export", "py-svc/app/handlers.py", "unused_py") in dead


def test_sam_code_uri_and_globals(tmp_path: Path) -> None:
    graph, dead = _graph(_write(tmp_path, _APP))
    assert _framework_names(graph, "sam-app/template.yaml", "sam-app/src/index.mjs") == ["handler"]
    assert _framework_names(graph, "sam-app/template.yaml", "sam-app/functions/app.mjs") == [
        "lambdaHandler"
    ]
    assert ("unused_export", "sam-app/src/index.mjs", "handler") not in dead
    assert ("unused_export", "sam-app/functions/app.mjs", "lambdaHandler") not in dead


def _lambda_targets(graph, config: str) -> set[str]:
    return {
        target
        for source in (config, f"{config}::__module__")
        for _, target, data in graph.out_edges(source, data=True)
        if data.get("edge_type") in ("framework", "framework_binds")
    }


def test_unresolvable_handlers_add_nothing(tmp_path: Path) -> None:
    graph, _dead = _graph(_write(tmp_path, _APP))
    # A function the file does not define, a variable and a missing file.
    names = _framework_names(graph, "node-svc/serverless.yml", "node-svc/src/handlers/users.mjs")
    assert "doesNotExist" not in names
    assert _lambda_targets(graph, "node-svc/serverless.yml") == {
        "node-svc/src/handlers/users.mjs",
        "node-svc/src/handlers/users.mjs::create",
        "node-svc/src/handlers/legacy.js",
    }
    # Invalid YAML and a top-level list are skipped without raising.
    assert _lambda_targets(graph, "broken/serverless.yml") == set()
    assert _lambda_targets(graph, "list/template.yml") == set()
