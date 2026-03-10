"""Basic tests for the Policy registration API."""

import math

from sandtrap import Policy


def test_fn_registration():
    policy = Policy()
    policy.fn(math.sqrt)
    assert "sqrt" in policy.functions
    assert policy.functions["sqrt"].func is math.sqrt


def test_fn_registration_with_name():
    policy = Policy()
    policy.fn(math.sqrt, name="square_root")
    assert "square_root" in policy.functions
    assert "sqrt" not in policy.functions


def test_fn_as_decorator():
    policy = Policy()

    @policy.fn
    def my_func(x):
        return x + 1

    assert "my_func" in policy.functions
    assert my_func(1) == 2  # decorator returns the original function


def test_fn_as_decorator_with_args():
    policy = Policy()

    @policy.fn(network_access=True)
    def fetch(url):
        return url

    assert "fetch" in policy.functions
    assert policy.functions["fetch"].network_access is True
    assert fetch("http://example.com") == "http://example.com"


def test_cls_registration():
    policy = Policy()

    class MyClass:
        pass

    policy.cls(MyClass, include=["method_a", "method_b"])
    assert "MyClass" in policy.classes
    reg = policy.classes["MyClass"]
    assert reg.include == ["method_a", "method_b"]
    assert reg.constructable is True


def test_cls_not_constructable():
    policy = Policy()

    class Service:
        pass

    policy.cls(Service, constructable=False)
    assert policy.classes["Service"].constructable is False


def test_module_registration():
    policy = Policy()
    policy.module(math)
    assert "math" in policy.modules
    assert policy.modules["math"].obj is math


def test_module_registration_with_include():
    policy = Policy()
    policy.module(math, include=["sqrt", "sin", "cos"])
    assert policy.modules["math"].include == ["sqrt", "sin", "cos"]


def test_module_instance_requires_name():
    policy = Policy()
    obj = {"key": "value"}
    try:
        policy.module(obj)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "name is required" in str(e)


def test_module_instance_with_name():
    policy = Policy()
    obj = {"key": "value"}
    policy.module(obj, name="db", include=["get", "set"])
    assert "db" in policy.modules


def test_import_allowed():
    policy = Policy()
    policy.module(math)
    assert policy.is_import_allowed("math") is True
    assert policy.is_import_allowed("os") is False


def test_import_recursive():
    policy = Policy()
    policy.module(math, recursive=True)
    assert policy.is_import_allowed("math") is True
    assert policy.is_import_allowed("math.special") is True
    assert policy.is_import_allowed("mathx") is False


def test_resolve_lazy_submodule():
    """Recursive resolve_module handles submodules not yet loaded as attributes."""
    import email

    policy = Policy()
    policy.module(email, recursive=True)

    # email.mime.text is a lazy submodule — not an attribute on email until imported
    mod = policy.resolve_module("email.mime.text")
    assert mod.__name__ == "email.mime.text"

    member = policy.resolve_module_member("email.mime.text", "MIMEText")
    assert member.__name__ == "MIMEText"


def test_attr_allowed_dunders():
    policy = Policy()
    assert policy.is_attr_allowed(None, "__init__") is True
    assert policy.is_attr_allowed(None, "__str__") is True
    assert policy.is_attr_allowed(None, "__code__") is False
    assert policy.is_attr_allowed(None, "__subclasses__") is False


def test_attr_allowed_private():
    policy = Policy()
    assert policy.is_attr_allowed(None, "_private") is False
    assert policy.is_attr_allowed(None, "public") is True


def test_default_policy():
    policy = Policy()
    assert policy.allow_network is False
    assert policy.timeout == 30.0
    assert policy.memory_limit is None


# ------------------------------------------------------------------
# needs_network()
# ------------------------------------------------------------------


def test_needs_network_default():
    assert Policy().needs_network() is False


def test_needs_network_global_flag():
    assert Policy(allow_network=True).needs_network() is True


def test_needs_network_fn_registration():
    policy = Policy()
    policy.fn(lambda: None, name="fetch", network_access=True)
    assert policy.needs_network() is True


def test_needs_network_fn_without_network():
    policy = Policy()
    policy.fn(lambda: None, name="compute")
    assert policy.needs_network() is False


def test_needs_network_cls_registration():
    policy = Policy()
    policy.cls(type("C", (), {}), name="C", network_access=True)
    assert policy.needs_network() is True


def test_needs_network_cls_member_spec():
    from sandtrap.policy import MemberSpec

    policy = Policy()
    policy.cls(
        type("C", (), {"fetch": lambda self: None}),
        name="C",
        configure={"fetch": MemberSpec(network_access=True)},
    )
    assert policy.needs_network() is True


def test_needs_network_module_registration():
    import types

    mod = types.ModuleType("mymod")
    policy = Policy()
    policy.module(mod, name="mymod", network_access=True)
    assert policy.needs_network() is True


def test_needs_network_module_member_spec():
    import types

    from sandtrap.policy import MemberSpec

    mod = types.ModuleType("mymod")
    mod.fetch = lambda: None
    policy = Policy()
    policy.module(
        mod,
        name="mymod",
        configure={"fetch": MemberSpec(network_access=True)},
    )
    assert policy.needs_network() is True


# ------------------------------------------------------------------
# needs_host_fs()
# ------------------------------------------------------------------


def test_needs_host_fs_default():
    assert Policy().needs_host_fs() is False


def test_needs_host_fs_fn_registration():
    policy = Policy()
    policy.fn(lambda: None, name="save", host_fs_access=True)
    assert policy.needs_host_fs() is True


def test_needs_host_fs_fn_without():
    policy = Policy()
    policy.fn(lambda: None, name="compute")
    assert policy.needs_host_fs() is False


def test_needs_host_fs_cls_registration():
    policy = Policy()
    policy.cls(type("C", (), {}), name="C", host_fs_access=True)
    assert policy.needs_host_fs() is True


def test_needs_host_fs_cls_member_spec():
    from sandtrap.policy import MemberSpec

    policy = Policy()
    policy.cls(
        type("C", (), {"save": lambda self: None}),
        name="C",
        configure={"save": MemberSpec(host_fs_access=True)},
    )
    assert policy.needs_host_fs() is True


def test_needs_host_fs_module_registration():
    import types

    mod = types.ModuleType("mymod")
    policy = Policy()
    policy.module(mod, name="mymod", host_fs_access=True)
    assert policy.needs_host_fs() is True


def test_needs_host_fs_module_member_spec():
    import types

    from sandtrap.policy import MemberSpec

    mod = types.ModuleType("mymod")
    mod.save = lambda: None
    policy = Policy()
    policy.module(
        mod,
        name="mymod",
        configure={"save": MemberSpec(host_fs_access=True)},
    )
    assert policy.needs_host_fs() is True
