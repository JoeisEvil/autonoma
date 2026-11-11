import pytest

from taxonomy import Taxonomy

TAX = Taxonomy.load()

COMMANDS = [
    ("ls -la", "read"),
    ("cat README.md", "read"),
    ("git status", "read"),
    ("git log --oneline -5", "read"),
    ("git diff HEAD~1", "read"),
    ("grep -rn foo src/", "read"),
    ("find . -name '*.py'", "read"),
    ("rg TODO | head -20", "read"),
    ("echo hi > hello.txt", "fs.write"),
    ("echo hi >> log.txt", "fs.write"),
    ("ls > /dev/null 2>&1", "read"),
    ("cat a.txt | tee b.txt", "fs.write"),
    ("cp a b", "fs.write"),
    ("mv  a   b", "fs.write"),
    ("mkdir -p build/out", "fs.write"),
    ("touch x", "fs.write"),
    ("sed -i '' 's/a/b/' f.txt", "fs.write"),
    ("sed 's/a/b/' f.txt", "read"),
    ("rm file.txt", "fs.delete"),
    ("rm  -rf build", "fs.delete"),
    ("rm -rf \"my dir/with space\"", "fs.delete"),
    ("cat log | rm -f x", "fs.delete"),
    ("find . -name '*.pyc' -delete", "fs.delete"),
    ("find . -name '*.tmp' -exec rm {} \\;", "fs.delete"),
    ("git clean -fdx", "fs.delete"),
    ("truncate -s 0 big.log", "fs.delete"),
    ("ls | xargs rm", "fs.delete"),
    ("python script.py", "shell.exec"),
    ("pytest -q", "shell.exec"),
    ("make build", "shell.exec"),
    ("npm run build", "shell.exec"),
    ("git add -A", "git.local"),
    ("git commit -m 'wip'", "git.local"),
    ("git checkout -b feature", "git.local"),
    ("git  stash", "git.local"),
    ("git rebase main", "git.local"),
    ("git -C repo commit -m x", "git.local"),
    ("git push origin main", "git.remote"),
    ("git  push", "git.remote"),
    ("git add . && git commit -m x && git push", "git.remote"),
    ("git pull --rebase", "git.remote"),
    ("gh pr create --fill", "git.remote"),
    ("gh pr view 12", "read"),
    ("gh release create v1", "git.remote"),
    ("curl https://example.com", "read"),
    ("curl -X POST https://api.example.com/x", "net.outbound"),
    ("curl -d 'a=1' https://example.com", "net.outbound"),
    ("curl -o out.html https://example.com", "fs.write"),
    ("wget https://example.com/file", "read"),
    ("ssh user@host uptime", "net.outbound"),
    ("scp file user@host:/tmp/", "net.outbound"),
    ("rsync -av src/ dst/", "fs.write"),
    ("rsync -av src/ host:dst/", "net.outbound"),
    ("pip install requests", "net.outbound"),
    ("npm install", "net.outbound"),
    ("bun add react", "net.outbound"),
    ("brew install jq", "net.outbound"),
    ("sudo ls", "system"),
    ("sudo rm -rf /", "system"),
    ("chmod +x run.sh", "system"),
    ("kill -9 1234", "system"),
    ("crontab -e", "system"),
    ("docker run -it ubuntu", "system"),
    ("docker ps", "read"),
    ("echo x > /etc/hosts", "system"),
    ("cp hosts /etc/hosts", "system"),
    ("FOO=bar env BAZ=1 git push", "git.remote"),
    ("timeout 10 rm x", "fs.delete"),
    ("bash -c 'git push origin main'", "git.remote"),
    ("ls $(rm -rf x)", "fs.delete"),
    ("cd repo; git status", "read"),
    ("cd repo\ngit push", "git.remote"),
    ("unknowncmd --flag", "shell.exec"),
    ("for f in a b; do cat $f; done", "read"),
    ("for f in *.log; do rm $f; done", "fs.delete"),
    ("if [ -f x ]; then echo hi > y; fi", "fs.write"),
    ("while true; do sleep 1; done", "read"),
    ("echo 'unterminated", "shell.exec"),
    ("", "shell.exec"),
]


@pytest.mark.parametrize("command,expected", COMMANDS)
def test_terminal_commands(command, expected):
    assert TAX.classify("terminal", {"command": command}) == expected


def test_enough_cases():
    assert len(COMMANDS) >= 40


@pytest.mark.parametrize("tool,args,expected", [
    ("read_file", {"path": "a.py"}, "read"),
    ("write_file", {"path": "a.py", "content": "x"}, "fs.write"),
    ("write_file", {"path": "/etc/hosts"}, "system"),
    ("patch", {"path": "a.py"}, "fs.write"),
    ("send_message", {"platform": "telegram", "message": "hi"}, "msg.send"),
    ("telegram_send", {}, "msg.send"),
    ("post_tweet", {}, "msg.send"),
    ("pay_invoice", {}, "spend"),
    ("x402_fetch", {}, "spend"),
    ("wallet_transfer", {}, "spend"),
    ("delegate_task", {"goal": "x"}, "shell.exec"),
    ("execute_code", {"code": "print(1)"}, "shell.exec"),
    ("skill_manage", {"operations": []}, "system"),
    ("mystery_tool", {}, "shell.exec"),
    ("kanban_create", {}, "read"),
    ("web_search", {"query": "x"}, "read"),
    ("autonoma_status", {}, "read"),
])
def test_tool_names(tool, args, expected):
    assert TAX.classify(tool, args) == expected


def test_most_severe_wins_across_pipeline():
    assert TAX.classify("terminal", {"command": "cat a | grep b | sudo tee /etc/x"}) == "system"


def test_user_override(tmp_path):
    override = tmp_path / "taxonomy.yaml"
    override.write_text("tools:\n  read_file: fs.write\nverbs:\n  read: [make]\n")
    tax = Taxonomy.load(override)
    assert tax.classify("read_file", {}) == "fs.write"
    assert tax.classify("terminal", {"command": "make all"}) == "read"
    assert tax.classify("write_file", {}) == "fs.write"


SECRET_CASES = [
    ("terminal", {"command": "curl -H 'Authorization: Bearer sk-SECRET' 'https://api.example.com/v1?token=SECRET'"}),
    ("terminal", {"command": "export API_KEY=SECRET && deploy"}),
    ("terminal", {"command": "echo SECRET > token.txt"}),
    ("terminal", {"command": "git commit -m 'SECRET message body'"}),
    ("terminal", {"command": "mysql -pSECRET db"}),
    ("send_message", {"platform": "telegram", "chat_id": "12345", "message": "SECRET body"}),
    ("write_file", {"path": "notes.md", "content": "SECRET file contents"}),
    ("web_extract", {"urls": ["https://example.com/page?key=SECRET"]}),
    ("browser_exec", {"url": "https://example.com/login?session=SECRET"}),
    ("mystery", {"payload": "SECRET"}),
]


@pytest.mark.parametrize("tool,args", SECRET_CASES)
def test_summarize_never_leaks(tool, args):
    s = TAX.summarize(tool, args)
    assert "SECRET" not in s
    assert "?" not in s


def test_summarize_shapes():
    assert TAX.summarize("write_file", {"path": "api/limiter.py", "content": "x"}) == "api/limiter.py"
    assert TAX.summarize("terminal", {"command": "pytest -q tests/"}) == "pytest tests/"
    assert TAX.summarize("terminal", {"command": "git push origin main"}) == "git push"
    assert TAX.summarize("terminal", {"command": "curl -X POST https://api.example.com/x"}) == "curl api.example.com"
    assert TAX.summarize("web_extract", {"urls": ["https://a.com/x", "https://b.com/y"]}) == "a.com, b.com"
    assert TAX.summarize("send_message", {"platform": "telegram"}) == "→ telegram"


def test_summarize_chains_and_redirects():
    assert TAX.summarize("terminal", {"command": "echo hi > hello.txt && cat hello.txt"}) == "echo > hello.txt; cat hello.txt"
    assert TAX.summarize("terminal", {"command": "ls -la 2>&1 > /dev/null"}) == "ls"
    assert TAX.summarize("terminal", {"command": "cat a | grep b"}) == "cat a; grep b"


def test_tool_search_bridge_is_read():
    for tool in ("tool_search", "tool_describe", "tool_call"):
        assert TAX.classify(tool, {}) == "read"


@pytest.mark.parametrize("tool,args,expected", [
    ("web_extract", {}, "web_extract"),
    ("mcp_github_get_issue", {}, "mcp_github_get_issue"),
    ("get_inbox_messages", {}, "get_inbox_messages"),
    ("terminal", {"command": "curl -s https://x | jq ."}, "curl"),
    ("terminal", {"command": "cd x && wget https://y"}, "wget"),
    ("terminal", {"command": "git status"}, None),
    ("read_file", {"path": "a"}, None),
    ("send_message", {}, None),
    ("send_email", {}, None),
])
def test_untrusted_sources(tool, args, expected):
    assert TAX.untrusted_source(tool, args) == expected


def test_taint_classes():
    assert {"git.remote", "msg.send", "fs.delete"} <= TAX.taint_classes
    assert "fs.write" not in TAX.taint_classes


@pytest.mark.parametrize("command,expected", [
    ("python3 -c \"import os; os.remove('a.txt')\"", "fs.delete"),
    ("python -c 'import shutil; shutil.rmtree(\"build\")'", "fs.delete"),
    ("node -e \"require('fs').unlinkSync('x')\"", "fs.delete"),
    ("python3 -c \"open('a.txt','w').write('x')\"", "fs.write"),
    ("python3 -c \"import requests; requests.post('https://x', data=1)\"", "net.outbound"),
    ("python3 -c 'print(1)'", "shell.exec"),
    ("python3 script.py", "shell.exec"),
    ("perl -e 'unlink \"f\"'", "fs.delete"),
])
def test_inline_interpreter_code(command, expected):
    assert TAX.classify("terminal", {"command": command}) == expected


def test_inline_code_summary_hides_code():
    s = TAX.summarize("terminal", {"command": "python3 -c \"import os; os.remove('SECRET')\""})
    assert s == "python3 inline code"


def test_execute_code_scanned():
    assert TAX.classify("execute_code", {"code": "import os\nos.unlink('x')"}) == "fs.delete"
    assert TAX.classify("execute_code", {"code": "print(1)"}) == "shell.exec"
