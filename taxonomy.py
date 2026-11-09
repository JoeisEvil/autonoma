import os
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

import yaml

READ = "read"
DEFAULT_PATH = Path(__file__).parent / "data" / "taxonomy.yaml"

_SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")", "|&", ";;"}
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
_PREFIXES = {"env", "nohup", "time", "nice", "exec", "command", "builtin", "stdbuf", "caffeinate"}
_PREFIXES_WITH_ARG = {"timeout", "gtimeout"}
_SHELLS = {"bash", "sh", "zsh", "dash", "fish"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REMOTE_PATH = re.compile(r"^(?:[\w.-]+@)?[\w.-]+:(?!//)")
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)

_GIT = {
    "git.remote": {"push", "pull", "fetch", "remote", "clone", "submodule", "send-email", "request-pull"},
    "fs.delete": {"clean", "rm"},
    READ: {"status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-tree", "rev-parse",
           "describe", "shortlog", "reflog", "whatchanged", "cat-file", "rev-list", "help",
           "--version", "version", "show-ref", "for-each-ref", "name-rev", "check-ignore"},
}
_GH_REMOTE = {("pr", "create"), ("pr", "merge"), ("pr", "close"), ("pr", "comment"), ("pr", "edit"),
              ("pr", "review"), ("release", None), ("repo", "create"), ("repo", "delete"),
              ("repo", "fork"), ("repo", "edit"), ("repo", "rename"), ("issue", "create"),
              ("issue", "comment"), ("issue", "close"), ("issue", "edit"), ("workflow", "run"),
              ("secret", None), ("gist", "create")}
_PKG_MANAGERS = {"pip", "pip3", "pipx", "uv", "npm", "pnpm", "yarn", "bun", "brew", "apt", "apt-get",
                 "yum", "dnf", "gem", "cargo", "go", "port", "conda", "mamba", "poetry", "npx", "bunx"}
_PKG_INSTALL = {"install", "add", "i", "upgrade", "update", "get", "sync", "uninstall", "remove", "x"}
_DOCKER_SYSTEM = {"run", "rm", "rmi", "kill", "stop", "exec", "start", "restart", "prune", "down", "up"}
_DOCKER_READ = {"ps", "images", "logs", "inspect", "version", "info", "stats", "top"}
_CURL_MUTATING_FLAGS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "-F",
                        "--form", "-T", "--upload-file", "--json"}
_CURL_OUTPUT_FLAGS = {"-o", "--output", "-O", "--remote-name"}
_HTTP_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_BUILTIN_VERBS = {
    "printenv": READ,
    "sudo": "system",
    "mkfs": "system",
}

_FILE_ARG_KEYS = ("path", "file_path", "filepath", "file", "target_file", "filename")
_URL_ARG_KEYS = ("url", "urls", "href", "endpoint")
_RECIPIENT_KEYS = ("platform", "channel_type", "service")


def _load_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _merge(base, override):
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = {**out[key], **val}
        elif key == "tool_patterns" and isinstance(val, list):
            out[key] = list(val) + list(out.get(key, []))
        elif key == "verbs" and isinstance(val, dict):
            out[key] = {**out.get(key, {}), **val}
        else:
            out[key] = val
    return out


class Taxonomy:
    def __init__(self, rules: dict):
        self.rules = rules
        self.tools = dict(rules.get("tools") or {})
        self.patterns = [(re.compile(p, re.I), c) for p, c in rules.get("tool_patterns") or []]
        self.verbs = dict(_BUILTIN_VERBS)
        for cls, verbs in (rules.get("verbs") or {}).items():
            for v in verbs or []:
                self.verbs[str(v)] = cls
        self.system_paths = tuple(rules.get("system_paths") or ())
        self.rank = {}
        for i, tier in enumerate(rules.get("severity") or []):
            for cls in tier if isinstance(tier, list) else [tier]:
                self.rank[cls] = len(rules["severity"]) - i
        for cls in list(self.tools.values()) + list(self.verbs.values()) + [c for _, c in self.patterns]:
            self.rank.setdefault(cls, self.rank.get("shell.exec", 1))
        self.classes = sorted(set(self.rank) - {READ}, key=lambda c: -self.rank[c])

    @classmethod
    def load(cls, override_path=None, default_path=DEFAULT_PATH):
        rules = _load_yaml(default_path)
        if override_path:
            rules = _merge(rules, _load_yaml(override_path))
        return cls(rules)

    def most_severe(self, classes):
        classes = [c for c in classes if c]
        return max(classes, key=lambda c: self.rank.get(c, 0)) if classes else READ

    def classify(self, tool_name: str, args=None) -> str:
        args = args if isinstance(args, dict) else {}
        if tool_name == "terminal":
            return self.classify_command(args.get("command") or args.get("code") or "")
        if tool_name in self.tools:
            return self._escalate_path(self.tools[tool_name], args)
        for rx, cls in self.patterns:
            if rx.search(tool_name):
                return cls
        return "shell.exec"

    def _escalate_path(self, cls, args):
        if cls in ("fs.write", "fs.delete"):
            path = _first(args, _FILE_ARG_KEYS)
            if path and self._is_system_path(str(path)):
                return "system"
        return cls

    def _is_system_path(self, path):
        p = os.path.expanduser(path)
        return any(p == s or p.startswith(s.rstrip("/") + "/") for s in self.system_paths)

    def classify_command(self, command: str) -> str:
        try:
            segments = split_segments(command)
        except ValueError:
            return "shell.exec"
        if not segments:
            return "shell.exec"
        return self.most_severe(self.classify_segment(s) for s in segments)

    def classify_segment(self, tokens) -> str:
        found = []
        tokens = list(tokens)
        redirect_targets = []
        clean = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in _WRITE_REDIRECTS:
                if i + 1 < len(tokens):
                    redirect_targets.append(tokens[i + 1])
                i += 2
                continue
            if t in {"<", "<<", "<<<", ">&", "<&"}:
                i += 2
                continue
            if t == "$" or (t.isdigit() and i + 1 < len(tokens) and tokens[i + 1] in _WRITE_REDIRECTS | {">&"}):
                i += 1
                continue
            clean.append(t)
            i += 1

        for target in redirect_targets:
            if target in ("/dev/null", "/dev/stderr", "/dev/stdout") or target.startswith("&"):
                continue
            found.append("system" if self._is_system_path(target) else "fs.write")

        verb, rest, elevated = _strip_prefixes(clean)
        if elevated:
            found.append("system")
        if verb:
            found.append(self._classify_verb(verb, rest))
        return self.most_severe(found)

    def _classify_verb(self, verb, rest):
        base = os.path.basename(verb)
        positional = [a for a in rest if not a.startswith("-")]
        sub = positional[0] if positional else None

        if base == "git":
            sub = _git_subcommand(rest)
            if sub is None:
                return READ
            for cls, subs in _GIT.items():
                if sub in subs:
                    return cls
            if sub == "config" and not any(a in ("--global", "--system") for a in rest):
                return READ if not positional[1:] or len(positional) <= 2 else "git.local"
            return "git.local"
        if base == "gh":
            second = positional[1] if len(positional) > 1 else None
            if (sub, second) in _GH_REMOTE or (sub, None) in _GH_REMOTE:
                return "git.remote"
            if sub == "api":
                return "git.remote" if _http_method(rest) in _HTTP_MUTATING or _has_any(rest, {"-f", "-F", "--field", "--raw-field", "--input"}) else READ
            return READ
        if base in ("curl", "http", "https", "xh", "wget"):
            return self._classify_http(base, rest)
        if base == "sed":
            return "fs.write" if any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in rest) else READ
        if base in ("find", "fd"):
            if "-delete" in rest:
                return "fs.delete"
            if any(a in ("-exec", "-execdir", "-ok", "-x", "--exec") for a in rest):
                idx = next(i for i, a in enumerate(rest) if a in ("-exec", "-execdir", "-ok", "-x", "--exec"))
                inner = [a for a in rest[idx + 1:] if a not in ("{}", ";", "+", "\\;")]
                if inner:
                    return self.most_severe([READ, self._classify_verb(inner[0], inner[1:])])
            return READ
        if base == "xargs":
            inner = [a for a in rest if not a.startswith("-")]
            return self._classify_verb(inner[0], rest[rest.index(inner[0]) + 1:]) if inner else "shell.exec"
        if base in _SHELLS and "-c" in rest:
            idx = rest.index("-c")
            return self.classify_command(rest[idx + 1]) if idx + 1 < len(rest) else "shell.exec"
        if base in _PKG_MANAGERS:
            if base in ("npx", "bunx"):
                return "net.outbound"
            if base == "go" and sub not in ("get", "install"):
                return "shell.exec"
            if sub in _PKG_INSTALL or sub == "ci" or (base == "yarn" and sub is None):
                return "net.outbound"
            return "shell.exec"
        if base in ("docker", "podman"):
            if sub == "compose":
                sub = positional[1] if len(positional) > 1 else None
            if sub in _DOCKER_SYSTEM:
                return "system"
            if sub in _DOCKER_READ:
                return READ
            return "shell.exec"
        if base in ("rsync", "scp"):
            return "net.outbound" if any(_REMOTE_PATH.match(a) for a in positional) else "fs.write"
        if base == "open":
            return READ

        cls = self.verbs.get(base) or self.verbs.get(verb)
        if cls in ("fs.write", "fs.delete") and any(self._is_system_path(a) for a in positional):
            return "system"
        return cls or "shell.exec"

    def _classify_http(self, base, rest):
        method = _http_method(rest)
        if base in ("http", "https", "xh"):
            words = [a for a in rest if not a.startswith("-")]
            if words and words[0].upper() in _HTTP_MUTATING:
                return "net.outbound"
            if any(re.match(r"^[\w-]+(:=|=|@)", w) for w in words[1:]):
                return "net.outbound"
            return READ
        if base == "wget":
            if any(a.startswith(("--post-data", "--post-file", "--method", "--body-data")) for a in rest):
                return "net.outbound"
            return READ
        if method in _HTTP_MUTATING or _has_any(rest, _CURL_MUTATING_FLAGS) or any(
                a.startswith(("--data", "--json", "--form")) for a in rest):
            return "net.outbound"
        if _has_any(rest, _CURL_OUTPUT_FLAGS):
            return "fs.write"
        return READ

    def summarize(self, tool_name: str, args=None) -> str:
        args = args if isinstance(args, dict) else {}
        if tool_name == "terminal":
            return summarize_command(args.get("command") or args.get("code") or "")
        path = _first(args, _FILE_ARG_KEYS)
        if path:
            return _clip(str(path), 160)
        url = _first(args, _URL_ARG_KEYS)
        if url:
            if isinstance(url, list):
                hosts = sorted({_host(u) for u in url if isinstance(u, str)})
                return _clip(", ".join(h for h in hosts if h), 120)
            return _host(str(url))
        cls = self.classify(tool_name, args)
        if cls == "msg.send":
            where = _first(args, _RECIPIENT_KEYS)
            return f"→ {_clip(str(where), 40)}" if where and not str(where).lstrip("-").isdigit() else "→ recipient"
        if tool_name == "execute_code":
            return "python code"
        if tool_name == "delegate_task":
            return "subagent"
        keys = sorted(k for k in args if isinstance(k, str))
        return "keys: " + ", ".join(keys[:6]) if keys else ""


def _first(args, keys):
    for k in keys:
        v = args.get(k)
        if v:
            return v
    return None


def _clip(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _host(url):
    try:
        if not _URL.match(url):
            url = "//" + url
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


def _has_any(tokens, flags):
    return any(t in flags for t in tokens)


def _http_method(tokens):
    for i, t in enumerate(tokens):
        if t in ("-X", "--request", "--method") and i + 1 < len(tokens):
            return tokens[i + 1].upper()
        if t.startswith("-X") and len(t) > 2:
            return t[2:].upper()
        if t.startswith("--request="):
            return t.split("=", 1)[1].upper()
    return None


def _git_subcommand(rest):
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def split_segments(command: str):
    lex = shlex.shlex(command, posix=True, punctuation_chars="();<>|&\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    segments, cur = [], []
    for tok in lex:
        if tok in _SEPARATORS or set(tok) <= set(";&|\n()") and tok not in _WRITE_REDIRECTS:
            if cur:
                segments.append(cur)
            cur = []
            continue
        if tok.startswith("$(") or tok == "`":
            continue
        cur.append(tok.lstrip("`").rstrip("`") if "`" in tok else tok)
    if cur:
        segments.append(cur)
    return [s for s in segments if s]


def _strip_prefixes(tokens):
    elevated = False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if _ASSIGN.match(t):
            i += 1
            continue
        base = os.path.basename(t)
        if base in ("sudo", "doas"):
            elevated = True
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 2 if tokens[i] in ("-u", "-g", "-C", "-p") else 1
            continue
        if base in _PREFIXES:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            continue
        if base in _PREFIXES_WITH_ARG:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            i += 1
            continue
        return t, tokens[i + 1:], elevated
    return None, [], elevated


def summarize_command(command: str) -> str:
    try:
        segments = split_segments(command)
    except ValueError:
        return "(unparsed command)"
    parts = []
    for seg in segments:
        clean, target, i = [], None, 0
        while i < len(seg):
            t = seg[i]
            if t in _WRITE_REDIRECTS:
                nxt = seg[i + 1] if i + 1 < len(seg) else ""
                if nxt and not nxt.startswith(("&", "/dev/")):
                    target = nxt
                i += 2
                continue
            if t in {"<", "<<", "<<<", ">&", "<&"} or (t.isdigit() and i + 1 < len(seg) and seg[i + 1] in _WRITE_REDIRECTS | {">&"}):
                i += 1 if t.isdigit() else 2
                continue
            clean.append(t)
            i += 1
        verb, rest, elevated = _strip_prefixes(clean)
        if not verb:
            continue
        base = os.path.basename(verb)
        if base in _SHELLS and "-c" in rest:
            parts.append(f"{base} -c …")
            continue
        arg = _safe_arg(base, rest)
        words = [("sudo" if elevated else ""), base, arg, (f"> {_clip(target, 60)}" if target else "")]
        parts.append(" ".join(x for x in words if x))
    if not parts:
        return ""
    head = "; ".join(parts[:3])
    if len(parts) > 3:
        head += f" (+{len(parts) - 3})"
    return _clip(head, 160)


def _safe_arg(base, rest):
    positional = [a for a in rest if not a.startswith("-")]
    if base in ("git", "gh", "docker", "podman") or base in _PKG_MANAGERS:
        sub = _git_subcommand(rest) if base == "git" else (positional[0] if positional else "")
        return sub or ""
    for a in positional:
        if _URL.match(a) or base in ("curl", "wget", "http", "https", "xh", "ssh", "scp", "rsync"):
            if _URL.match(a):
                return _host(a)
            if _REMOTE_PATH.match(a):
                return a.split(":", 1)[0].split("@")[-1]
            continue
        if _ASSIGN.match(a) or "=" in a or len(a) > 80:
            continue
        if base in ("echo", "printf"):
            return ""
        return a
    return ""
