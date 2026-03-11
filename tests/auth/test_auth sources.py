from anvil.auth import AuthSource, infer_auth_source


def test_infer_auth_source_environment(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    assert infer_auth_source(None) is AuthSource.ENVIRONMENT


def test_infer_auth_source_oidc(monkeypatch):
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn")
    assert infer_auth_source(None) is AuthSource.OIDC
