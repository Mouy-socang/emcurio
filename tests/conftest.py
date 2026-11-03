"""全部测试共用：汇率请求（ECB）默认被拦截，测试从不连接外部主机；要模拟 ECB 的测试自己传 get。"""
import pytest


@pytest.fixture(autouse=True)
def no_ecb(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError('测试不得连接 ECB')
    monkeypatch.setattr('curio.fx._get', refuse)
