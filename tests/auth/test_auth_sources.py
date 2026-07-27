from anvil.providers.aws.auth import AuthSource, infer_auth_source


def test_infer_auth_source_environment(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    assert infer_auth_source(None) is AuthSource.ENVIRONMENT


def test_infer_auth_source_oidc(monkeypatch):
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn")
    assert infer_auth_source(None) is AuthSource.OIDC


def test_infer_auth_source_sso_session_profile(monkeypatch, tmp_path):
    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir()
    (aws_dir / "config").write_text(
        "\n".join(
            [
                "[profile chronicles]",
                "sso_session = chronicles",
                "sso_account_id = 123456789012",
                "sso_role_name = AdministratorAccess",
                "region = us-east-1",
                "",
                "[sso-session chronicles]",
                "sso_region = us-east-1",
                "sso_start_url = https://example.awsapps.com/start",
                "sso_registration_scopes = sso:account:access",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert infer_auth_source("chronicles") is AuthSource.SSO
