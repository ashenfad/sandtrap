"""Recursive-registration filter propagation and dotted (qualified) patterns.

Two previously-silent policy gaps:

1. Submodule objects reached by attribute traversal carried NO
   registration, so ``numpy.random.seed(0)`` escaped an exclude that
   ``from numpy.random import seed`` enforced. Submodules of a
   ``recursive=True`` registration now inherit the parent registration.

2. Patterns containing dots never matched (predicates saw only the bare
   member name), so agex-style ``"DataFrame.eval"`` / ``"pandas.core*"``
   excludes were inert. Members are now also checked as
   ``Owner.attr`` (every class in the MRO) and ``package.module.attr``.
"""

import urllib

from sandtrap import Policy, Sandbox


def _sandbox(**module_kwargs) -> Sandbox:
    policy = Policy()
    policy.module(urllib, recursive=True, **module_kwargs)
    return Sandbox(policy)


# -- recursive filter propagation (the numpy.random.seed hole) ----------------


def test_recursive_exclude_applies_to_submodule_attr_access():
    sb = _sandbox(exclude=("_*", "*._*", "urlsplit"))
    # attribute traversal path — previously ALLOWED
    result = sb.exec("import urllib.parse\nurllib.parse.urlsplit('http://x/y')")
    assert result.error is not None
    assert "urlsplit" in str(result.error)


def test_recursive_exclude_from_import_parity():
    sb = _sandbox(exclude=("_*", "*._*", "urlsplit"))
    result = sb.exec("from urllib.parse import urlsplit")
    assert result.error is not None
    # non-excluded members stay importable both ways
    result = sb.exec(
        "from urllib.parse import urlparse\n"
        "import urllib.parse\n"
        "a = urlparse('http://x/y').path\n"
        "b = urllib.parse.urlparse('http://x/y').path\n"
    )
    assert result.error is None
    assert result.namespace["a"] == result.namespace["b"] == "/y"


def test_recursive_default_excludes_cover_submodule_privates():
    sb = _sandbox()  # default exclude ("_*", "*._*")
    result = sb.exec("import urllib.parse\nurllib.parse._coerce_args")
    assert result.error is not None


def test_private_submodule_import_blocked_like_attr_traversal():
    sb = _sandbox()  # default exclude ("_*", "*._*")
    # xml.parsers-style private submodules: import path must match the
    # attr-traversal verdict (both routes gated by the bare "_*")
    result = sb.exec("import urllib.error as e\nimport urllib._nonexistent")
    assert result.error is not None
    assert "not allowed" in str(result.error) or "ImportError" in repr(result.error)


# -- dotted patterns: module paths ---------------------------------------------


def test_dotted_exclude_blocks_submodule_traversal_and_import():
    sb = _sandbox(exclude=("_*", "*._*", "urllib.request*"))
    result = sb.exec("import urllib.request")
    assert result.error is not None
    result = sb.exec("import urllib\nurllib.request")
    assert result.error is not None
    # sibling submodule unaffected
    result = sb.exec("import urllib.parse\nx = urllib.parse.quote('a b')")
    assert result.error is None
    assert result.namespace["x"] == "a%20b"


def test_dotted_exclude_targets_one_member_by_full_path():
    sb = _sandbox(exclude=("_*", "*._*", "urllib.parse.quote"))
    result = sb.exec("import urllib.parse\nurllib.parse.quote('a b')")
    assert result.error is not None
    result = sb.exec("from urllib.parse import quote")
    assert result.error is not None
    result = sb.exec("import urllib.parse\nx = urllib.parse.unquote('a%20b')")
    assert result.error is None
    assert result.namespace["x"] == "a b"


# -- dotted patterns: class-qualified members ----------------------------------


class Robot:
    def __init__(self, name):
        self.name = name

    def greet(self):
        return f"hi from {self.name}"

    def self_destruct(self):  # pragma: no cover - must stay uncalled
        return "boom"


class Android(Robot):
    pass


def test_class_qualified_exclude_on_instances():
    policy = Policy()
    policy.cls(Robot, exclude=("_*", "Robot.self_destruct"))
    sb = Sandbox(policy)

    result = sb.exec("r = Robot('Hal')\nmsg = r.greet()")
    assert result.error is None
    assert result.namespace["msg"] == "hi from Hal"

    result = sb.exec("Robot('Hal').self_destruct()")
    assert result.error is not None
    assert "self_destruct" in str(result.error)


def test_class_qualified_exclude_matches_through_mro():
    policy = Policy()
    policy.cls(Android, exclude=("_*", "Robot.self_destruct"))
    sb = Sandbox(policy)

    result = sb.exec("a = Android('Data')\nmsg = a.greet()")
    assert result.error is None
    # pattern names the base class; the instance is the subclass
    result = sb.exec("Android('Data').self_destruct()")
    assert result.error is not None


# -- bare patterns keep their existing meaning ----------------------------------


def test_bare_include_still_matches_bare_names():
    policy = Policy()
    policy.cls(Robot, include=["greet"])
    sb = Sandbox(policy)
    result = sb.exec("msg = Robot('Hal').greet()")
    assert result.error is None
    result = sb.exec("Robot('Hal').self_destruct()")
    assert result.error is not None
