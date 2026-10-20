"""Apply HTTP deadlines without mutating shared SDK clients or requests globals."""
from types import FunctionType
from .context import request_timeout


class BudgetRequests:
    def __init__(self, requests_module):
        self.original = requests_module

    def __getattr__(self, name):
        method = getattr(self.original, name)
        if name not in {"get", "post", "put", "delete", "head", "request"}:
            return method

        def call(*args, **kwargs):
            budget = request_timeout()
            supplied = kwargs.get("timeout")
            if isinstance(supplied, tuple):
                kwargs["timeout"] = tuple(min(t or budget, budget) for t in supplied)
            else:
                kwargs["timeout"] = min(supplied or budget, budget)
            return method(*args, **kwargs)
        return call


def call_with_http_budget(fn, **kwargs):
    """AKShare lacks timeout parameters on Sina endpoints.

    Rebind its Python functions to private globals, including same-module helpers
    (minute adjustment calls daily). Never monkeypatch the imported SDK module.
    """
    request_timeout()
    fn = getattr(fn, "__wrapped__", fn)
    if not isinstance(fn, FunctionType) or "requests" not in fn.__globals__:
        return fn(**kwargs)
    namespace = dict(fn.__globals__)
    namespace["requests"] = BudgetRequests(namespace["requests"])
    def bind(value):
        rebound = FunctionType(value.__code__, namespace, value.__name__, value.__defaults__, value.__closure__)
        rebound.__kwdefaults__ = value.__kwdefaults__
        return rebound

    for name, value in list(namespace.items()):
        if isinstance(value, FunctionType) and value.__globals__ is fn.__globals__:
            namespace[name] = bind(value)
    # Tencent imports this cached helper from another module; it also performs HTTP.
    helper = fn.__globals__.get("get_tx_start_year")
    underlying = getattr(helper, "__wrapped__", helper)
    if isinstance(underlying, FunctionType) and underlying.__globals__ is not fn.__globals__:
        namespace["get_tx_start_year"] = lambda **args: call_with_http_budget(helper, **args)
    return bind(fn)(**kwargs)


class BudgetTushareApi:
    def __init__(self, api):
        self.api = api

    def __getattr__(self, name):
        def call(*args, **kwargs):
            budget = request_timeout()
            # Preserve endpoint and token, but use a private client per SDK call.
            if "_DataApi__timeout" in vars(self.api):
                client = object.__new__(type(self.api))
                client.__dict__.update(vars(self.api))
                client._DataApi__timeout = min(vars(self.api)["_DataApi__timeout"], budget)
            else:
                client = self.api
            return getattr(client, name)(*args, **kwargs)
        return call
